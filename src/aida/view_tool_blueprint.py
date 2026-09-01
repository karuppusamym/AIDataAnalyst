"""N11: deterministic view-to-tool ("tool generator B") blueprint generation.

A database VIEW is already a human-authored, pre-curated query: someone
deliberately wrote and named it to answer a real question. That makes it the
highest-quality-per-unit-of-effort source for auto-generating a governed
tool -- unlike an LLM guessing at a useful query, or even SM-5's
`multi_table_blueprint.py` (a mechanical FK-join, not a curated one), a
view's own SELECT list represents real analytical intent someone already
had. This module never re-derives that logic: the generated tool's SQL
template is always ``SELECT <view's own column list> FROM <view> WHERE
<parameterized predicates>`` -- the view stays the single source of truth
for its own SELECT/JOIN/aggregation logic, and this only adds a governed,
parameterized *read surface* on top of it. Inlining or rewriting the view's
internal SQL would defeat the "pre-curated, best quality" premise and risk
breaking on views the parser cannot fully reconstruct.

Two halves, mirroring `multi_table_blueprint.py`'s split exactly:

* `build_view_tool_blueprint` is pure and DB-free. Given a view's own output
  columns (name, physical type, ordinal position -- data every view already
  carries via its own `MetadataTable`/`MetadataColumn` rows, see below), it
  deterministically renders a candidate SQL template plus parameter schema.
* `resolve_view_tool_source` is the (only) DB-touching piece. It resolves
  the view's columns *and* enforces the redaction/screening gate described
  below. A view this function refuses is a view the pure builder never sees.

**What column/type data is actually available, and why parameterization is
FULL, not partial or none.** A view gets its own `MetadataTable` row (per
envelope 1.1) and its own `MetadataColumn` rows populated the exact same way
a base table's are -- `information_schema.columns` (and each connector's
equivalent) exposes a view's *result* columns, with their physical types,
identically to a table's. This is tier-1 data ("have", not "missing,
load-bearing" -- see `Docs/review-2026-08/target/01-metadata-graph-wiki.md`
§2) and it exists **independently of whether the view's DDL text itself
parsed, redacted or screened cleanly**. So the honest answer to "what
column-to-source-table mapping does the parser capture" turns out not to
gate parameter typing at all: every eligible output column's own declared
physical type -- not a guess, not an inference through `ViewLineageEdge`'s
column-level lineage -- is what types its parameter, via the exact same
`physical_type_family` bucketing `multi_table_blueprint.py` already uses.
A column whose type does not resolve to a known, filterable family (an
array, JSON, geometry, or other complex/unrecognized type) is still
selected into the tool's output -- it is not omitted -- but is never offered
as an equality parameter, because this generator does not invent a filter
type it cannot honestly validate.

**Redaction gating.** What *does* gate generation entirely is the view's own
`MetadataViewDefinition` (its DDL *text*, not its column list) -- mirroring
`mcp_server.py`'s AT-19 `_view_definition_transformation_detail` exactly: a
missing definition, one whose `availability` is `UNAVAILABLE`, whose
`redaction_status` is not `PARSED`, or whose `screening_status` is not
`CLEAN` (quarantined by prompt-risk screening) refuses tool generation
outright. A tool whose underlying logic cannot be shown to a reviewer cannot
responsibly be published, even in draft -- so this generator does not build
one. This is a *read-surface* generator, not a proof that the view's SQL
compiles cleanly; but building a governed access path onto a view a steward
cannot even inspect the text of is not "low effort, best quality", it is
exactly the shortcut this row exists to avoid.

"Deterministically rendered" carries the identical meaning
`multi_table_blueprint.py` established: the same view columns always
produce byte-identical SQL and an identically-ordered parameter list,
independent of caller-supplied order or dict/set iteration -- columns are
always canonicalized by `(ordinal_position, name)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp

from aida.envelope_models import AVAILABLE, MetadataViewDefinition
from aida.ingest_screening import is_eligible_for_model_context
from aida.models import MetadataColumn, MetadataSchema, MetadataTable
from aida.relationship_naming import physical_type_family
from aida.schemas import ToolParameterDefinition

# Mirrors multi_table_blueprint.py's _PARAMETER_TYPE_BY_PHYSICAL_FAMILY
# exactly, minus "OTHER": a family this coarse bucketer cannot place is a
# type this generator will not guess a filter type for -- the column is
# still selected, just never turned into a WHERE parameter.
_PARAMETER_TYPE_BY_PHYSICAL_FAMILY: dict[str, str] = {
    "NUMERIC": "NUMBER",
    "BOOLEAN": "BOOLEAN",
    "DATE_TIME": "DATE",
    "STRING": "STRING",
    "BINARY": "STRING",
}

_PARAMETER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ViewToolBlueprintError(ValueError):
    """Raised for a structurally invalid blueprint request (no columns,
    unknown table, ...)."""


class ViewNotEligibleError(ViewToolBlueprintError):
    """Raised when the view's own definition text is missing, withheld,
    unparsed or quarantined -- refused, never guessed. Mirrors AT-19's
    `_view_definition_transformation_detail` gating in `mcp_server.py`."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"view is not eligible for tool generation: {reason}")


@dataclass(frozen=True, slots=True)
class ViewToolColumn:
    """One active output column of the view being turned into a tool."""

    name: str
    physical_type: str
    ordinal_position: int


@dataclass(frozen=True, slots=True)
class ViewToolSource:
    """The resolved view selected for the blueprint, with its active
    output columns."""

    table_id: UUID
    qualified_name: str  # "schema.view_name"
    columns: tuple[ViewToolColumn, ...]


@dataclass(frozen=True, slots=True)
class ViewToolBlueprint:
    """The deterministic render output -- ready to drop into
    `GovernedToolVersionCreate.sql_template` / `.parameters`."""

    sql_template: str
    parameters: tuple[ToolParameterDefinition, ...]
    referenced_tables: tuple[str, ...]
    selected_columns: tuple[str, ...]
    parameterized_columns: tuple[str, ...]


def _quoted_table(qualified_name: str) -> exp.Table:
    schema_name, _, table_name = qualified_name.partition(".")
    table = exp.to_table(f"{schema_name}.{table_name}")
    for identifier in table.find_all(exp.Identifier):
        identifier.set("quoted", True)
    return table


def _col(name: str) -> exp.Column:
    return exp.column(exp.to_identifier(name, quoted=True))


def build_view_tool_blueprint(view: ViewToolSource, *, dialect: str) -> ViewToolBlueprint:
    """Pure, DB-free. Deterministic in ``(view, dialect)`` alone -- calling
    this twice with equal (by value) arguments always returns a
    `ViewToolBlueprint` with byte-identical `sql_template`.

    Raises:
        ViewToolBlueprintError: the view has no active columns.
    """
    if not view.columns:
        raise ViewToolBlueprintError("view has no active columns to expose")

    ordered_columns = sorted(
        view.columns, key=lambda column: (column.ordinal_position, column.name)
    )

    table = _quoted_table(view.qualified_name)
    select_expressions = [_col(column.name) for column in ordered_columns]
    query = exp.select(*select_expressions).from_(table)

    parameters: list[ToolParameterDefinition] = []
    parameterized: list[str] = []
    used_parameter_names: set[str] = set()
    where_condition: exp.Expr | None = None
    for column in ordered_columns:
        family = physical_type_family(column.physical_type)
        parameter_type = _PARAMETER_TYPE_BY_PHYSICAL_FAMILY.get(family)
        if parameter_type is None:
            continue
        # A placeholder name must satisfy ToolParameterDefinition's naming
        # pattern; it need not match the real (possibly mixed-case,
        # possibly reserved) column identifier used in the quoted SQL
        # reference below -- the two are deliberately decoupled.
        parameter_name = column.name.lower()
        if not _PARAMETER_NAME_RE.match(parameter_name) or parameter_name in used_parameter_names:
            # Not a name this generator can honestly offer as a filter
            # (leading digit, non-ASCII, a case-collision with another
            # column already claimed) -- the column stays selected, it is
            # just never turned into a WHERE parameter.
            continue
        used_parameter_names.add(parameter_name)
        parameters.append(
            ToolParameterDefinition(
                name=parameter_name,
                parameter_type=parameter_type,
                required=False,
            )
        )
        # Optional equality filter: unset (NULL) leaves every row in, same
        # convention as multi_table_blueprint.py -- a fresh draft is safely
        # runnable with no arguments at all, matching the maker-checker
        # review flow, where a reviewer should be able to see real,
        # unfiltered results before approving.
        clause = exp.or_(
            exp.condition(exp.Placeholder(this=parameter_name)).is_(exp.Null()),
            exp.condition(_col(column.name)).eq(exp.Placeholder(this=parameter_name)),
        )
        where_condition = clause if where_condition is None else exp.and_(where_condition, clause)
        parameterized.append(parameter_name)

    if where_condition is not None:
        query = query.where(where_condition)

    sql_template = query.sql(dialect=dialect, pretty=True)

    return ViewToolBlueprint(
        sql_template=sql_template,
        parameters=tuple(parameters),
        referenced_tables=(view.qualified_name,),
        selected_columns=tuple(column.name for column in ordered_columns),
        parameterized_columns=tuple(parameterized),
    )


# ---------------------------------------------------------------------------
# The one DB-touching function in this module. Everything above this line is
# pure and DB-free; everything below only *reads* existing catalog and view-
# definition state -- it creates no new persisted state, and it is the sole
# boundary a caller needs to fake/replace to unit-test
# `build_view_tool_blueprint` without a database.
# ---------------------------------------------------------------------------


def _require_eligible_view_definition(view_definition: MetadataViewDefinition | None) -> None:
    if view_definition is None:
        raise ViewNotEligibleError("no captured view definition for this table")
    if view_definition.status != "ACTIVE":
        raise ViewNotEligibleError(
            f"view definition status is {view_definition.status}, not ACTIVE"
        )
    if view_definition.availability != AVAILABLE:
        raise ViewNotEligibleError(
            "view definition text is UNAVAILABLE "
            f"({view_definition.unavailable_reason or 'no reason recorded'})"
        )
    if view_definition.redaction_status != "PARSED":
        raise ViewNotEligibleError(
            f"view definition redaction status is {view_definition.redaction_status}, "
            "not PARSED"
        )
    if not is_eligible_for_model_context(view_definition.screening_status):
        raise ViewNotEligibleError(
            "view definition is quarantined by prompt-risk screening "
            f"(screening_status={view_definition.screening_status})"
        )


async def resolve_view_tool_source(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    table_id: UUID,
) -> ViewToolSource:
    """Fetch `table_id`'s active columns and turn them into the plain
    dataclass `build_view_tool_blueprint` consumes -- but only after
    confirming the view is eligible for tool generation at all.

    Raises:
        ViewToolBlueprintError: `table_id` is not an ACTIVE table belonging
            to `datasource_id` in this organization.
        ViewNotEligibleError: the table has no captured
            `MetadataViewDefinition`, or its definition text is
            unavailable, unparsed, or quarantined by prompt-risk screening.
    """
    row = (
        await session.execute(
            select(MetadataTable, MetadataSchema)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.id == table_id,
                MetadataTable.organization_id == organization_id,
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).first()
    if row is None:
        raise ViewToolBlueprintError("unknown or inactive table id for this datasource")
    table, schema = row

    view_definition = await session.scalar(
        select(MetadataViewDefinition).where(MetadataViewDefinition.table_id == table.id)
    )
    _require_eligible_view_definition(view_definition)

    columns = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()

    return ViewToolSource(
        table_id=table.id,
        qualified_name=f"{schema.name}.{table.name}",
        columns=tuple(
            ViewToolColumn(
                name=column.name,
                physical_type=column.physical_type,
                ordinal_position=column.ordinal_position,
            )
            for column in sorted(columns, key=lambda column: (column.ordinal_position, column.name))
        ),
    )
