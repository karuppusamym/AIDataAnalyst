"""Column-level dbt lineage extraction (LN-5).

`dbt_artifacts.parse_dbt_manifest` already derives *table-level* lineage
edges by intersecting a resource's `depends_on_unique_ids` with the other
resources known to the same manifest. This module adds a finer-grained
layer on top of that: for a resource whose `compiled_sql_redacted` was
successfully parsed, it runs the same sqlglot-based engine used for
standalone SQL view/procedure lineage (`sql_lineage_parser`) over that
value-safe SQL and resolves each extracted column edge's raw
`source_table` string back to one of the resource's declared manifest
dependencies.

Nothing here executes or inspects real data: `compiled_sql_redacted` is
already literal-free by the time it reaches this module, and
`sql_lineage_parser` never executes SQL either.

An edge whose `source_table` cannot be resolved to a declared dependency is
dropped rather than fabricated -- the same convention
`dbt_artifacts.parse_dbt_manifest` already follows for table-level edges
(`if dependency_id in known_ids`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aida.dbt_artifacts import ParsedDbtResource
from aida.sql_lineage_parser import TransformationType, parse_view_lineage

# Bounds the number of column edges emitted per resource, in the same spirit
# (and rough order of magnitude) as the per-manifest bounds in
# `dbt_artifacts.py` (MAX_RESOURCES, MAX_EDGES) and the per-resource column
# cap already applied when parsing manifest column metadata (`[:2000]`).
MAX_COLUMN_EDGES_PER_RESOURCE = 2_000

_COLUMN_NAME_LIMIT = 255


@dataclass(frozen=True, slots=True)
class DependencyResource:
    """The subset of a sibling manifest resource needed to resolve a raw
    `source_table` string (as it appears in another resource's compiled SQL)
    back to a dbt `unique_id`. Deliberately ORM-agnostic -- callers can build
    this from a `ParsedDbtResource`, a `DbtResource` row, or any other object
    exposing the same fields.
    """

    unique_id: str
    relation_name: str | None
    database_name: str | None
    schema_name: str | None
    name: str


@dataclass(frozen=True, slots=True)
class ColumnLineageEdge:
    """One resolved column-level lineage edge, ready to persist as a
    `DbtLineageEdge` row with `edge_type="COLUMN_DEPENDS_ON"`."""

    source_unique_id: str
    source_column: str
    target_column: str
    transformation_type: str
    confidence: str


def _normalize_identifier_path(text: str) -> str:
    """Lowercase a dotted identifier path and strip quoting from each part.

    dbt's `relation_name` is typically rendered fully quoted, e.g.
    `"bank"."analytics"."customer_summary"` -- stripping quote characters
    from the whole string would only affect its two ends, so each
    dot-separated segment is cleaned independently before rejoining.
    sqlglot's own `Table.catalog` / `.db` / `.name` are already unquoted, so
    normalizing them again here is a harmless no-op.
    """
    parts = [part.strip(" \t`\"[]") for part in text.split(".")]
    return ".".join(part for part in parts if part).lower()


def _dependency_lookup(
    dependencies: Sequence[DependencyResource],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    by_relation: dict[str, str] = {}
    by_composite: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for dependency in dependencies:
        if dependency.relation_name:
            by_relation.setdefault(
                _normalize_identifier_path(dependency.relation_name), dependency.unique_id
            )
        if dependency.database_name and dependency.schema_name:
            composite = f"{dependency.database_name}.{dependency.schema_name}.{dependency.name}"
            by_composite.setdefault(_normalize_identifier_path(composite), dependency.unique_id)
        by_name.setdefault(_normalize_identifier_path(dependency.name), dependency.unique_id)
    return by_relation, by_composite, by_name


def extract_column_lineage(
    resource: ParsedDbtResource,
    dependencies: Sequence[DependencyResource],
    dialect: str,
) -> list[ColumnLineageEdge]:
    """Extract column-level lineage edges for one manifest resource.

    Returns an empty list when the resource's compiled SQL was not
    successfully parsed (`sql_parse_status != "PARSED"`), when it has no
    declared dependencies to resolve against, when `sql_lineage_parser` finds
    no edges (unsupported dialect, unparseable statement shape, etc.), or
    when every edge it finds is non-column-level evidence this module does
    not model (a `SELECT *` table-level `TABLE_STAR` edge, a filter-only
    `FILTERED` edge) -- it never raises.
    """
    if resource.sql_parse_status != "PARSED" or not resource.compiled_sql_redacted:
        return []
    if not dependencies:
        return []

    result = parse_view_lineage(resource.compiled_sql_redacted, dialect)
    if not result.edges:
        return []

    by_relation, by_composite, by_name = _dependency_lookup(dependencies)

    edges: list[ColumnLineageEdge] = []
    for edge in result.edges:
        # AT-D2 taught sql_lineage_parser two new evidence kinds that do not
        # fit this module's contract (and its `ColumnLineageEdge` shape) of
        # specifically column-level `COLUMN_DEPENDS_ON` edges between a real
        # source column and a real compiled output column: `TABLE_STAR`
        # (honest table-level `*` evidence, `source_column="*"`, no real
        # output column resolved) and `FILTERED` (filter-only evidence for a
        # column that is never actually a selected/output column, targeting
        # the reserved `FILTER_EVIDENCE_TARGET_COLUMN` marker). Both are real
        # facts for standalone view/procedure lineage, but neither is a
        # column-to-column dependency, so both are dropped here for the same
        # reason an unresolved reference already is -- never fabricate a
        # specific column edge from evidence that isn't actually column-level.
        if edge.transformation_type in (
            TransformationType.TABLE_STAR.value,
            TransformationType.FILTERED.value,
        ):
            continue
        normalized_source = _normalize_identifier_path(edge.source_table)
        source_unique_id = (
            by_relation.get(normalized_source)
            or by_composite.get(normalized_source)
            or by_name.get(normalized_source)
        )
        if source_unique_id is None:
            # Reference to something outside this resource's declared
            # dependencies (an alias sqlglot could not resolve, an external
            # table, an unrelated CTE artifact, ...) -- drop it rather than
            # fabricate an edge to an unknown node.
            continue
        edges.append(
            ColumnLineageEdge(
                source_unique_id=source_unique_id,
                source_column=edge.source_column[:_COLUMN_NAME_LIMIT],
                target_column=edge.target_column[:_COLUMN_NAME_LIMIT],
                transformation_type=edge.transformation_type,
                confidence=edge.confidence,
            )
        )
        if len(edges) >= MAX_COLUMN_EDGES_PER_RESOURCE:
            break
    return edges
