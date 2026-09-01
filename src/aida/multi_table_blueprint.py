"""SM-5: deterministic multi-table governed-tool blueprint generation.

Today's governed tools (module 14, `GovernedToolVersion`) are hand-authored,
single-source-of-truth SQL templates: an author writes the JOIN(s) -- if
any -- by hand in `sql_template` and submits it through
`tool_api.create_tool_version`. This module adds a second, generative path
for the common "join these tables together" case: given a set of table ids
and the already-declared/approved relationships connecting them, it renders
a candidate multi-table JOIN SQL template plus its parameter schema, in
exactly the shape `aida.schemas.GovernedToolVersionCreate` expects -- so the
result can be submitted through the *same* draft-creation/maker-checker path
as a hand-written tool, unchanged.

Two halves, deliberately separated:

* `build_multi_table_blueprint` is pure and DB-free. It takes plain
  dataclasses describing the selected tables and the join edges between
  them and returns a `MultiTableBlueprint` -- nothing here touches a
  session, a clock, or anything else that could make the same inputs
  produce different output on different calls.
* `resolve_blueprint_tables_and_edges` is the (only) DB-touching piece. It
  reads existing catalog/relationship state -- `MetadataConstraint` rows of
  type ``FOREIGN_KEY`` and reviewer-``APPROVED`` `RelationshipCandidate`
  rows -- and turns them into the plain dataclasses the pure builder
  consumes. It creates no new persisted relationship state: a join this
  function cannot see (no FK, no approved candidate) is a join the builder
  above will refuse to invent (see `UnjoinableTablesError`).

"Deterministically rendered" (tracker SM-5's exit bar) means: the same set
of table ids plus the same declared relationship data always produce
byte-identical SQL and an identically-ordered parameter list, regardless of
the order table ids were requested in or the order the database happened to
return rows in -- mirroring `context_compiler.py`'s `artifact_hash`
determinism convention. That is enforced by never depending on
caller-supplied list order or dict/set iteration for anything that affects
the rendered text: tables are canonicalized by ``(qualified_name,
table_id)``, and whenever more than one edge could extend the join tree,
the edge is picked by a fully-ordered sort key, never by insertion order.

Known, honest gap: only single-datasource joins are supported (a governed
tool has exactly one `datasource_id`), and only two relationship sources are
read -- declared foreign keys and approved *single-column*
`RelationshipCandidate` rows. Composite `RelationshipCandidateGroup`
candidates and explicit semantic-model join declarations are not consumed
here yet; at the time this was written the semantic layer (module 07,
`SemanticModelVersion`/`SemanticMetricVersion`) declares metrics against a
single `source_table_id` and does not itself declare cross-table joins, so
there was no such declaration to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp

from aida.models import (
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    RelationshipCandidate,
)
from aida.relationship_naming import physical_type_family
from aida.schemas import ToolParameterDefinition

JoinEdgeKind = Literal["DECLARED_FOREIGN_KEY", "APPROVED_RELATIONSHIP_CANDIDATE"]

# Priority used when more than one declared relationship connects the same
# pair of tables (e.g. both a database FK and an independently approved
# RelationshipCandidate) -- lower wins. A database-declared FK is stronger
# evidence than a reviewer-approved statistical candidate, so it always wins
# the tie deterministically rather than by whichever happened to be fetched
# first.
_EDGE_KIND_PRIORITY: dict[str, int] = {
    "DECLARED_FOREIGN_KEY": 0,
    "APPROVED_RELATIONSHIP_CANDIDATE": 1,
}

_PARAMETER_TYPE_BY_PHYSICAL_FAMILY: dict[str, str] = {
    "NUMERIC": "NUMBER",
    "BOOLEAN": "BOOLEAN",
    "DATE_TIME": "DATE",
    "STRING": "STRING",
    "BINARY": "STRING",
    "OTHER": "STRING",
}


class MultiTableBlueprintError(ValueError):
    """Raised for a structurally invalid blueprint request (too few tables,
    duplicate table ids, ...)."""


class UnjoinableTablesError(MultiTableBlueprintError):
    """Raised when the selected tables cannot all be connected using only
    already-declared/approved relationships. Never guessed: this is the
    refusal path, not a fallback that invents a join key."""

    def __init__(self, unreachable_qualified_names: list[str]) -> None:
        self.unreachable_tables: tuple[str, ...] = tuple(unreachable_qualified_names)
        super().__init__(
            "no declared or approved relationship connects the following table(s) "
            "to the rest of the selected join set: "
            + ", ".join(unreachable_qualified_names)
        )


@dataclass(frozen=True, slots=True)
class BlueprintColumn:
    """One active column of a table being joined."""

    name: str
    physical_type: str
    ordinal_position: int


@dataclass(frozen=True, slots=True)
class BlueprintTable:
    """One table selected for the blueprint, with its active columns."""

    table_id: UUID
    qualified_name: str  # "schema.table" -- matches QueryExecutionGateway.allowed_tables
    columns: tuple[BlueprintColumn, ...]


@dataclass(frozen=True, slots=True)
class BlueprintJoinEdge:
    """One already-declared/approved relationship between two selected
    tables. `left_columns`/`right_columns` are ordered and same-length (a
    composite key has more than one pair)."""

    kind: JoinEdgeKind
    left_table_id: UUID
    left_columns: tuple[str, ...]
    right_table_id: UUID
    right_columns: tuple[str, ...]
    # The MetadataConstraint id / RelationshipCandidate id this edge came
    # from, stringified -- purely a deterministic tie-breaker and an audit
    # trail, never interpreted.
    source_id: str


@dataclass(frozen=True, slots=True)
class JoinStepSummary:
    """One edge actually used to build the join tree, in the order it was
    added -- the audit-friendly summary of what the blueprint relied on."""

    parent_table_id: UUID
    child_table_id: UUID
    kind: JoinEdgeKind
    source_id: str
    parent_columns: tuple[str, ...]
    child_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MultiTableBlueprint:
    """The deterministic render output -- ready to drop into
    `GovernedToolVersionCreate.sql_template` / `.parameters`."""

    sql_template: str
    parameters: tuple[ToolParameterDefinition, ...]
    referenced_tables: tuple[str, ...]
    table_order: tuple[UUID, ...]  # t1..tN, alias order
    join_steps: tuple[JoinStepSummary, ...]


def _parameter_type_for(physical_type: str) -> str:
    return _PARAMETER_TYPE_BY_PHYSICAL_FAMILY[physical_type_family(physical_type)]


def _quoted_table(qualified_name: str, alias: str) -> exp.Table:
    schema_name, _, table_name = qualified_name.partition(".")
    table = exp.to_table(f"{schema_name}.{table_name}")
    for identifier in table.find_all(exp.Identifier):
        identifier.set("quoted", True)
    table.set("alias", exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
    return table


def _col(alias: str, name: str) -> exp.Column:
    return exp.column(
        exp.to_identifier(name, quoted=True), table=exp.to_identifier(alias, quoted=True)
    )


_EdgeSortKey = tuple[int, list[str], list[str], str]
_FrontierSortKey = tuple[str, str, _EdgeSortKey]


def _edge_sort_key(edge: BlueprintJoinEdge) -> _EdgeSortKey:
    return (
        _EDGE_KIND_PRIORITY[edge.kind],
        list(edge.left_columns),
        list(edge.right_columns),
        edge.source_id,
    )


def build_multi_table_blueprint(
    tables: list[BlueprintTable],
    edges: list[BlueprintJoinEdge],
    *,
    dialect: str,
) -> MultiTableBlueprint:
    """Pure, DB-free. Deterministic in ``(tables, edges, dialect)`` alone --
    calling this twice with equal (by value) arguments always returns a
    `MultiTableBlueprint` with byte-identical `sql_template`.

    Raises:
        MultiTableBlueprintError: fewer than two tables, or duplicate table ids.
        UnjoinableTablesError: the selected tables cannot all be reached from
            each other using only the supplied edges -- no join is guessed.
    """
    if len(tables) < 2:
        raise MultiTableBlueprintError("a multi-table blueprint needs at least two tables")
    table_ids = [table.table_id for table in tables]
    if len(set(table_ids)) != len(table_ids):
        raise MultiTableBlueprintError("duplicate table ids in blueprint request")

    tables_by_id = {table.table_id: table for table in tables}
    selected_ids = set(tables_by_id)
    canonical_order = sorted(tables, key=lambda t: (t.qualified_name, str(t.table_id)))

    # Dedupe to at most one edge per unordered table pair -- among the
    # selected tables only -- picking the strongest/most-canonical edge
    # deterministically when more than one declared relationship connects
    # the same pair.
    best_edge_by_pair: dict[frozenset[UUID], BlueprintJoinEdge] = {}
    for edge in edges:
        if edge.left_table_id not in selected_ids or edge.right_table_id not in selected_ids:
            continue
        if edge.left_table_id == edge.right_table_id:
            continue
        pair = frozenset((edge.left_table_id, edge.right_table_id))
        current = best_edge_by_pair.get(pair)
        if current is None or _edge_sort_key(edge) < _edge_sort_key(current):
            best_edge_by_pair[pair] = edge

    # Deterministic BFS/Prim-style spanning tree from the canonical anchor
    # (first table in (qualified_name, id) order): at each step, expand via
    # whichever available edge leads to the lexicographically-smallest new
    # table, so the result depends only on the data, never on dict/list
    # iteration order.
    anchor = canonical_order[0]
    reached = {anchor.table_id}
    alias_order = [anchor.table_id]
    join_steps: list[JoinStepSummary] = []
    remaining_pairs = dict(best_edge_by_pair)
    while len(reached) < len(tables):
        frontier: list[tuple[_FrontierSortKey, frozenset[UUID], BlueprintJoinEdge]] = []
        for pair, edge in remaining_pairs.items():
            boundary = pair & reached
            if len(boundary) != 1:
                continue
            new_id = next(iter(pair - reached))
            new_table = tables_by_id[new_id]
            sort_key = (new_table.qualified_name, str(new_id), _edge_sort_key(edge))
            frontier.append((sort_key, pair, edge))
        if not frontier:
            break
        frontier.sort(key=lambda item: item[0])
        _, pair, edge = frontier[0]
        new_id = next(iter(pair - reached))
        parent_id = next(iter(pair & reached))
        reached.add(new_id)
        alias_order.append(new_id)
        del remaining_pairs[pair]
        if edge.left_table_id == new_id:
            parent_columns, child_columns = edge.right_columns, edge.left_columns
        else:
            parent_columns, child_columns = edge.left_columns, edge.right_columns
        join_steps.append(
            JoinStepSummary(
                parent_table_id=parent_id,
                child_table_id=new_id,
                kind=edge.kind,
                source_id=edge.source_id,
                parent_columns=parent_columns,
                child_columns=child_columns,
            )
        )

    if len(reached) < len(tables):
        unreachable = sorted(tables_by_id[tid].qualified_name for tid in selected_ids - reached)
        raise UnjoinableTablesError(unreachable)

    alias_by_table_id = {table_id: f"t{i + 1}" for i, table_id in enumerate(alias_order)}

    select_expressions = []
    for table_id in alias_order:
        alias = alias_by_table_id[table_id]
        table = tables_by_id[table_id]
        for column in sorted(table.columns, key=lambda c: (c.ordinal_position, c.name)):
            output_name = f"{alias}_{column.name}"
            select_expressions.append(
                exp.alias_(_col(alias, column.name), exp.to_identifier(output_name, quoted=True))
            )

    anchor_alias = alias_by_table_id[alias_order[0]]
    query = exp.select(*select_expressions).from_(
        _quoted_table(tables_by_id[alias_order[0]].qualified_name, anchor_alias)
    )

    parameters: list[ToolParameterDefinition] = []
    where_condition: exp.Expr | None = None
    for step in join_steps:
        parent_alias = alias_by_table_id[step.parent_table_id]
        child_alias = alias_by_table_id[step.child_table_id]
        on_condition: exp.Expr | None = None
        for parent_column, child_column in zip(
            step.parent_columns, step.child_columns, strict=True
        ):
            predicate = exp.condition(_col(parent_alias, parent_column)).eq(
                _col(child_alias, child_column)
            )
            on_condition = predicate if on_condition is None else exp.and_(on_condition, predicate)
        query = query.join(
            _quoted_table(tables_by_id[step.child_table_id].qualified_name, child_alias),
            on=on_condition,
            join_type="inner",
        )

        child_table = tables_by_id[step.child_table_id]
        columns_by_name = {column.name: column for column in child_table.columns}
        for column_name in step.child_columns:
            parameter_name = f"{child_alias}_{column_name}"
            column_meta = columns_by_name.get(column_name)
            parameters.append(
                ToolParameterDefinition(
                    name=parameter_name,
                    parameter_type=_parameter_type_for(
                        column_meta.physical_type if column_meta is not None else ""
                    ),
                    required=False,
                )
            )
            # An optional equality filter: unset (NULL) leaves every row in,
            # so a fresh draft is safely runnable with no arguments at all --
            # matching the maker-checker review flow, where a reviewer should
            # be able to see real, unfiltered results before approving.
            clause = exp.or_(
                exp.condition(exp.Placeholder(this=parameter_name)).is_(exp.Null()),
                exp.condition(_col(child_alias, column_name)).eq(
                    exp.Placeholder(this=parameter_name)
                ),
            )
            where_condition = (
                clause if where_condition is None else exp.and_(where_condition, clause)
            )

    if where_condition is not None:
        query = query.where(where_condition)

    sql_template = query.sql(dialect=dialect, pretty=True)
    referenced_tables = tuple(sorted(table.qualified_name for table in tables))

    return MultiTableBlueprint(
        sql_template=sql_template,
        parameters=tuple(parameters),
        referenced_tables=referenced_tables,
        table_order=tuple(alias_order),
        join_steps=tuple(join_steps),
    )


# ---------------------------------------------------------------------------
# The one DB-touching function in this module. Everything above this line is
# pure and DB-free; everything below only *reads* existing catalog and
# relationship state -- it creates no new persisted relationship row, and it
# is the sole boundary a caller needs to fake/replace to unit-test
# `build_multi_table_blueprint` without a database.
# ---------------------------------------------------------------------------


async def resolve_blueprint_tables_and_edges(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    table_ids: list[UUID],
) -> tuple[list[BlueprintTable], list[BlueprintJoinEdge]]:
    """Fetch the join-relevant metadata for `table_ids` and turn it into the
    plain dataclasses `build_multi_table_blueprint` consumes.

    Reads exactly two already-declared/approved relationship sources -- see
    the module docstring for why not more:

    * `MetadataConstraint` rows of type ``FOREIGN_KEY`` with
      ``status == "ACTIVE"`` (declared by the source database itself), and
    * `RelationshipCandidate` rows with ``status == "APPROVED"`` (a reviewer
      accepted the candidate through the existing maker-checker relationship
      review flow in `intelligence_api.py`).

    Raises:
        MultiTableBlueprintError: any requested table id is not an ACTIVE
            table belonging to `datasource_id` in this organization.
    """
    unique_table_ids = list(dict.fromkeys(table_ids))
    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.id.in_(unique_table_ids),
                MetadataTable.organization_id == organization_id,
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    tables_by_id: dict[UUID, tuple[MetadataTable, MetadataSchema]] = {
        table.id: (table, schema) for table, schema in table_rows
    }
    missing = sorted(str(table_id) for table_id in unique_table_ids if table_id not in tables_by_id)
    if missing:
        raise MultiTableBlueprintError(
            "unknown or inactive table id(s) for this datasource: " + ", ".join(missing)
        )

    columns = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id.in_(tables_by_id.keys()),
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    columns_by_id: dict[UUID, MetadataColumn] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
        columns_by_id[column.id] = column

    tables = [
        BlueprintTable(
            table_id=table_id,
            qualified_name=f"{schema.name}.{table.name}",
            columns=tuple(
                BlueprintColumn(
                    name=column.name,
                    physical_type=column.physical_type,
                    ordinal_position=column.ordinal_position,
                )
                for column in sorted(
                    columns_by_table.get(table_id, ()),
                    key=lambda column: (column.ordinal_position, column.name),
                )
            ),
        )
        for table_id, (table, schema) in tables_by_id.items()
    ]

    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.datasource_id == datasource_id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
                MetadataConstraint.table_id.in_(tables_by_id.keys()),
                MetadataConstraint.referenced_table_id.in_(tables_by_id.keys()),
            )
        )
    ).all()
    edges: list[BlueprintJoinEdge] = [
        BlueprintJoinEdge(
            kind="DECLARED_FOREIGN_KEY",
            left_table_id=constraint.table_id,
            left_columns=tuple(constraint.columns),
            right_table_id=constraint.referenced_table_id,
            right_columns=tuple(constraint.referenced_columns),
            source_id=str(constraint.id),
        )
        for constraint in constraints
        if constraint.referenced_table_id is not None
        and constraint.columns
        and len(constraint.columns) == len(constraint.referenced_columns)
    ]

    candidates = (
        await session.scalars(
            select(RelationshipCandidate).where(
                RelationshipCandidate.datasource_id == datasource_id,
                RelationshipCandidate.target_datasource_id == datasource_id,
                RelationshipCandidate.status == "APPROVED",
                RelationshipCandidate.source_table_id.in_(tables_by_id.keys()),
                RelationshipCandidate.target_table_id.in_(tables_by_id.keys()),
            )
        )
    ).all()
    for candidate in candidates:
        source_column = columns_by_id.get(candidate.source_column_id)
        target_column = columns_by_id.get(candidate.target_column_id)
        if source_column is None or target_column is None:
            # The candidate's column was deactivated/dropped since it was
            # approved -- stale evidence, not a usable edge.
            continue
        edges.append(
            BlueprintJoinEdge(
                kind="APPROVED_RELATIONSHIP_CANDIDATE",
                left_table_id=candidate.source_table_id,
                left_columns=(source_column.name,),
                right_table_id=candidate.target_table_id,
                right_columns=(target_column.name,),
                source_id=str(candidate.id),
            )
        )

    return tables, edges
