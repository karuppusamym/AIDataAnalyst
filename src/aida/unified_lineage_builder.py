"""Unified lineage graph builder: bounds, edge providers, one merged graph.

R02 named `unified_lineage_api._build_unified_graph` (465 lines) as a hotspot
whose useful boundary is "graph service, edge providers, policy, bounds,
response composition". This module holds the first four; response composition
stays in `unified_lineage_api.build_unified_lineage_graph_payload`, which is
where the caching and the API DTOs already live.

**One authoritative bound.** `BoundedGraph` is the only thing in the merge that
knows what `node_limit` and `edge_limit` mean. Before this split the bound was
expressed twice: once as the real cap (`register_node` / `register_link`) and
once as eight ad-hoc post-query comparisons that used three different
operators (`==`, `>=`, `>`) because their queries used three different `LIMIT`
expressions (`n`, `n + 1`). Those are all the same rule stated three ways --
"a result set that filled its LIMIT probably had more behind it" -- so
`note_scan_bound` states it once, against the limit the query actually used.
Every provider reports its scan through that method; none of them append a
truncation reason directly.

**Providers own one edge kind each.** Each `collect_*` coroutine issues its own
queries and offers nodes and links to the shared `BoundedGraph`; none of them
can exceed the budget, because admission is the accumulator's decision, not
theirs. They run in a fixed order (foreign keys, view/procedure definitions,
suggested relationships, dbt, OpenLineage) because that order is the response's
edge order and callers render it.

**Policy is a filter on rows, not on the graph.** Two switches decide what a
provider may even see: `suggestion_status` selects `RelationshipCandidate`
rows by review state, and `include_pending_edges` decides whether parser- and
importer-produced edges that are still `PROPOSED` (P1-05) render at all.
Nothing downstream re-filters, so those two parameters are the whole story.
This function does no access control: `datasource` arrives already authorized.
"""

from __future__ import annotations

from collections.abc import Sequence, Sized
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.models import (
    DataSource,
    DbtArtifactImport,
    DbtLineageEdge,
    DbtProject,
    DbtResource,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    OpenLineageRunEvent,
    OpenLineageTableEdge,
    ProcedureLineageEdge,
    RelationshipCandidate,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.unified_lineage import UnifiedLink

SuggestionStatus = Literal["ALL", "PENDING", "APPROVED", "REJECTED"]

#: Every edge source the merged graph can carry. `counts_by_source` always
#: reports all six, so a caller can tell "no dbt edges" from "dbt not merged".
EDGE_SOURCES: tuple[str, ...] = (
    "FOREIGN_KEY",
    "SUGGESTED_RELATIONSHIP",
    "DBT_DEPENDENCY",
    "OPENLINEAGE_ETL",
    "VIEW_DEFINITION",
    "PROCEDURE_DEFINITION",
)

DBT_NODE_KIND_BY_RESOURCE_TYPE = {
    "MODEL": "DBT_MODEL",
    "SOURCE": "DBT_SOURCE",
    "SEED": "DBT_SEED",
    "SNAPSHOT": "DBT_SNAPSHOT",
}

# `ViewLineageEdge.confidence` / `ProcedureLineageEdge.confidence` store
# `aida.sql_lineage_parser.Confidence`'s string value (FULL/PARTIAL/LOW), not
# a float -- map it onto the same 0..1 scale every other unified-lineage edge
# kind reports confidence on. An unrecognised value degrades to LOW rather
# than raising, matching the parser's own fail-open posture.
DEFINITION_LINEAGE_CONFIDENCE = {"FULL": 1.0, "PARTIAL": 0.6, "LOW": 0.3}


@dataclass(slots=True)
class UnifiedGraphNode:
    """One node in the merged graph. `resolved` says whether it is backed by a
    catalog table; an unresolved node carries no `matched_table_id`."""

    id: str
    node_kind: str
    label: str
    qualified_name: str
    matched_table_id: UUID | None
    resolved: bool


@dataclass(slots=True)
class UnifiedGraph:
    """A merged graph and why it may be incomplete."""

    nodes: dict[str, UnifiedGraphNode]
    links: list[UnifiedLink]
    counts_by_source: dict[str, int]
    truncation_reasons: list[str]


@dataclass(slots=True)
class BoundedGraph:
    """The single authority on this graph's node and edge budgets.

    Admission is decided here and nowhere else, which is what makes the bound
    trustworthy: a provider cannot register a node past `node_limit` or a link
    past `edge_limit`, and a link whose endpoints did not both make it in is
    refused rather than left dangling -- a response that named a node it did
    not return would be worse than a truncated one.
    """

    node_limit: int
    edge_limit: int
    nodes: dict[str, UnifiedGraphNode] = field(default_factory=dict)
    links: list[UnifiedLink] = field(default_factory=list)
    counts_by_source: dict[str, int] = field(default_factory=lambda: dict.fromkeys(EDGE_SOURCES, 0))
    truncation_reasons: set[str] = field(default_factory=set)

    def register_node(self, info: UnifiedGraphNode) -> bool:
        """Admit a node, or refuse it and record NODE_LIMIT. Re-registering an
        id already present succeeds without consuming budget."""
        if info.id in self.nodes:
            return True
        if len(self.nodes) >= self.node_limit:
            self.truncation_reasons.add("NODE_LIMIT")
            return False
        self.nodes[info.id] = info
        return True

    def register_link(self, link: UnifiedLink) -> bool:
        """Admit a link, or refuse it. A link to an unadmitted node is dropped
        silently -- it is a consequence of the node bound, already reported."""
        if link.source_id not in self.nodes or link.target_id not in self.nodes:
            return False
        if len(self.links) >= self.edge_limit:
            self.truncation_reasons.add("EDGE_LIMIT")
            return False
        self.links.append(link)
        self.counts_by_source[link.edge_source] += 1
        return True

    def note_scan_bound(self, rows: Sized, scan_limit: int, reason: str) -> None:
        """Record that a provider's query filled its own LIMIT.

        A result set as large as the LIMIT it was issued with says nothing
        about what was left behind, so the graph is reported as truncated even
        when every row it did return was admitted.
        """
        if len(rows) >= scan_limit:
            self.truncation_reasons.add(reason)

    def note_truncated(self, reason: str) -> None:
        self.truncation_reasons.add(reason)

    def snapshot(self) -> UnifiedGraph:
        return UnifiedGraph(
            nodes=self.nodes,
            links=self.links,
            counts_by_source=self.counts_by_source,
            truncation_reasons=sorted(self.truncation_reasons),
        )


async def collect_tables(
    session: AsyncSession, datasource: DataSource, graph: BoundedGraph
) -> set[UUID]:
    """Register the datasource's ACTIVE tables and return their ids.

    Ordered by qualified name before the bound is applied, so which tables
    survive a small `node_limit` is deterministic rather than insertion-ordered.
    The returned set is the frame every other provider joins against: an edge
    whose endpoint is not in it is not part of this datasource's graph.
    """
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .limit(graph.node_limit)
        )
    ).all()
    graph.note_scan_bound(rows, graph.node_limit, "NODE_LIMIT")
    table_ids: set[UUID] = set()
    for table, schema, catalog in rows:
        table_ids.add(table.id)
        graph.register_node(
            UnifiedGraphNode(
                id=str(table.id),
                node_kind="TABLE",
                label=table.name,
                qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
                matched_table_id=table.id,
                resolved=True,
            )
        )
    return table_ids


async def collect_foreign_keys(
    session: AsyncSession,
    datasource: DataSource,
    graph: BoundedGraph,
    table_ids: set[UUID],
) -> None:
    """Declared database foreign keys: the only edge kind with no inference in
    it, hence confidence 1.0 and status DECLARED."""
    if not table_ids:
        return
    constraints = (
        await session.scalars(
            select(MetadataConstraint)
            .where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
                MetadataConstraint.table_id.in_(table_ids),
                MetadataConstraint.referenced_table_id.in_(table_ids),
            )
            .limit(graph.edge_limit)
        )
    ).all()
    for constraint in constraints:
        if constraint.referenced_table_id is None:
            continue
        graph.register_link(
            UnifiedLink(
                edge_id=f"fk:{constraint.id}",
                source_id=str(constraint.table_id),
                target_id=str(constraint.referenced_table_id),
                edge_source="FOREIGN_KEY",
                status="DECLARED",
                confidence=1.0,
                source_columns=tuple(constraint.columns),
                target_columns=tuple(constraint.referenced_columns),
                evidence={"source": "DATABASE_CONSTRAINT", "source_values_inspected": False},
            )
        )
    graph.note_scan_bound(constraints, graph.edge_limit, "EDGE_LIMIT")


#: A row `_register_definition_edges` folds: a view's parsed edge, or a
#: procedure's -- from pasted SQL, or from a captured routine's body.
DefinitionEdgeRow = ViewLineageEdge | ProcedureLineageEdge | DeepProcedureLineageEdge


def _register_definition_edges(
    graph: BoundedGraph,
    rows: Sequence[DefinitionEdgeRow],
    edge_source: Literal["VIEW_DEFINITION", "PROCEDURE_DEFINITION"],
    view_definitions_by_table_id: dict[UUID, tuple[str, str]] | None = None,
    routine_references_by_id: dict[UUID, tuple[str, str]] | None = None,
) -> None:
    """Collapse column-level parser rows into one table-level edge per pair.

    `view_lineage_api.py` persists one row per *column* pair
    (source_table/source_column -> target_table/target_column, where target is
    the view or procedure output). Only rows the parser matched to a real
    catalog table on both ends are foldable into this table-level graph -- an
    unmatched free-text table name (`source_table_id is None`) cannot be
    deduplicated against a real `MetadataTable` without risking a false merge
    across schemas that share a table name, so those rows are left for the
    dedicated `/view-lineage` / `/procedure-lineage` endpoints rather than
    guessed here. Multiple column-level rows between the same two tables
    collapse into one edge, exactly like dbt's COLUMN_DEPENDS_ON rows.

    Takes each model concretely (rather than as a `type[X | Y]` parameter) so
    the ORM row type stays precise for the type checker.
    """
    grouped: dict[tuple[UUID, UUID], list[DefinitionEdgeRow]] = {}
    for row in rows:
        if row.source_table_id is None or row.target_table_id is None:
            continue
        if row.source_table_id == row.target_table_id:
            continue
        grouped.setdefault((row.source_table_id, row.target_table_id), []).append(row)
    for (source_table_id, target_table_id), edges in grouped.items():
        # The view/procedure (target_table_id) is the dependent node; the base
        # table it selects from (source_table_id) is what it depends on -- same
        # source-depends-on-target convention as FOREIGN_KEY and DBT_DEPENDENCY.
        evidence: dict[str, object] = {
            "source": edge_source,
            "dialect": edges[0].dialect,
            "sql_hash": edges[0].sql_hash,
            "column_edge_count": len(edges),
        }
        # AT-19: a VIEW_DEFINITION edge's target_table_id IS the view's own
        # MetadataTable.id, and MetadataViewDefinition.table_id is unique per
        # table (envelope 1.1) -- a genuine 1:1 lookup, so the edge can carry a
        # reference the caller can actually resolve via the
        # get_transformation_detail MCP tool, plus redaction status in-line so
        # "does this edge have code, and is it redacted" never needs a round
        # trip on its own. PROCEDURE_DEFINITION edges get neither:
        # ProcedureLineageEdge carries no identity back to a specific
        # MetadataRoutine row (no FK, no specific_name -- see
        # `mcp_server.py::_view_definition_transformation_detail`), so no
        # reference is fabricated here. A row from the routine-aware table
        # (`DeepProcedureLineageEdge`) does know its routine: an edge such rows
        # establish names those routines, and when exactly one routine -- still
        # captured -- establishes it, the edge carries the same resolvable
        # reference a VIEW_DEFINITION edge does, to that routine's own body.
        routine_ids = sorted(
            {
                str(edge.routine_id)
                for edge in edges
                if isinstance(edge, DeepProcedureLineageEdge)
            }
        )
        if routine_ids:
            evidence["routine_ids"] = routine_ids
        if len(routine_ids) == 1 and routine_references_by_id is not None:
            found_routine = routine_references_by_id.get(UUID(routine_ids[0]))
            if found_routine is not None:
                redaction_status, availability = found_routine
                evidence["transformation_reference"] = {
                    "tool": "get_transformation_detail",
                    "entity_id": routine_ids[0],
                    "kind": "ROUTINE_BODY",
                }
                evidence["redaction_status"] = redaction_status
                evidence["availability"] = availability
        if view_definitions_by_table_id is not None:
            found = view_definitions_by_table_id.get(target_table_id)
            if found is not None:
                redaction_status, availability = found
                evidence["transformation_reference"] = {
                    "tool": "get_transformation_detail",
                    "entity_id": str(target_table_id),
                    "kind": "VIEW_DEFINITION",
                }
                evidence["redaction_status"] = redaction_status
                evidence["availability"] = availability
        graph.register_link(
            UnifiedLink(
                edge_id=f"{edge_source.lower()}:{edges[0].id}",
                source_id=str(target_table_id),
                target_id=str(source_table_id),
                edge_source=edge_source,
                status="ACTIVE",
                confidence=min(
                    DEFINITION_LINEAGE_CONFIDENCE.get(edge.confidence, 0.3) for edge in edges
                ),
                source_columns=tuple(sorted({edge.target_column for edge in edges})),
                target_columns=tuple(sorted({edge.source_column for edge in edges})),
                evidence=evidence,
            )
        )


async def _load_view_definition_references(
    session: AsyncSession, datasource: DataSource, view_rows: Sequence[ViewLineageEdge]
) -> dict[UUID, tuple[str, str]]:
    """Fetch only the three narrow columns `transformation_reference` needs.

    Never `definition_sql_redacted` itself: this stays a bounded reference
    lookup (ADR-0010) rather than a way to smuggle DDL text into an
    already-bounded graph payload.
    """
    target_ids = {row.target_table_id for row in view_rows if row.target_table_id}
    if not target_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MetadataViewDefinition.table_id,
                MetadataViewDefinition.redaction_status,
                MetadataViewDefinition.availability,
            ).where(
                MetadataViewDefinition.datasource_id == datasource.id,
                MetadataViewDefinition.table_id.in_(target_ids),
            )
        )
    ).all()
    return {
        table_id: (redaction_status, availability)
        for table_id, redaction_status, availability in rows
    }


async def _load_routine_references(
    session: AsyncSession,
    datasource: DataSource,
    routine_rows: Sequence[DeepProcedureLineageEdge],
) -> dict[UUID, tuple[str, str]]:
    """The routine-aware counterpart of `_load_view_definition_references`:
    the two narrow columns a routine's reference carries, never its body."""
    routine_ids = {row.routine_id for row in routine_rows}
    if not routine_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MetadataRoutine.id,
                MetadataRoutine.redaction_status,
                MetadataRoutine.availability,
            ).where(
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.id.in_(routine_ids),
            )
        )
    ).all()
    return {
        routine_id: (redaction_status, availability)
        for routine_id, redaction_status, availability in rows
    }


async def collect_definition_lineage(
    session: AsyncSession,
    datasource: DataSource,
    graph: BoundedGraph,
    table_ids: set[UUID],
    *,
    include_pending_edges: bool,
) -> None:
    """View and stored-procedure SQL-parsed lineage (LN-2), and the lineage
    parsed from captured routine bodies (N3).

    P1-05: PROPOSED rows are parser output nobody has reviewed. They belong in
    the review queue, not in the shared graph, so they render only for a caller
    that asked for them explicitly. That includes every edge the lineage agent
    (ADR-0029) writes, until a person approves it.
    """
    if not table_ids:
        return
    view_stmt = (
        select(ViewLineageEdge)
        .where(
            ViewLineageEdge.datasource_id == datasource.id,
            ViewLineageEdge.source_table_id.in_(table_ids),
            ViewLineageEdge.target_table_id.in_(table_ids),
        )
        .order_by(ViewLineageEdge.id)
        .limit(graph.edge_limit)
    )
    if not include_pending_edges:
        view_stmt = view_stmt.where(ViewLineageEdge.review_status == "ACTIVE")
    view_rows = (await session.scalars(view_stmt)).all()
    graph.note_scan_bound(view_rows, graph.edge_limit, "EDGE_LIMIT")
    _register_definition_edges(
        graph,
        view_rows,
        "VIEW_DEFINITION",
        await _load_view_definition_references(session, datasource, view_rows),
    )

    procedure_stmt = (
        select(ProcedureLineageEdge)
        .where(
            ProcedureLineageEdge.datasource_id == datasource.id,
            ProcedureLineageEdge.source_table_id.in_(table_ids),
            ProcedureLineageEdge.target_table_id.in_(table_ids),
        )
        .order_by(ProcedureLineageEdge.id)
        .limit(graph.edge_limit)
    )
    if not include_pending_edges:
        procedure_stmt = procedure_stmt.where(ProcedureLineageEdge.review_status == "ACTIVE")
    procedure_rows = (await session.scalars(procedure_stmt)).all()
    graph.note_scan_bound(procedure_rows, graph.edge_limit, "EDGE_LIMIT")

    # The routine-aware procedure table, under the same review filter. A hop
    # into a temp table is the procedure's own plumbing and never folds in.
    routine_stmt = (
        select(DeepProcedureLineageEdge)
        .where(
            DeepProcedureLineageEdge.datasource_id == datasource.id,
            DeepProcedureLineageEdge.source_table_id.in_(table_ids),
            DeepProcedureLineageEdge.target_table_id.in_(table_ids),
            DeepProcedureLineageEdge.is_intermediate.is_(False),
        )
        .order_by(DeepProcedureLineageEdge.id)
        .limit(graph.edge_limit)
    )
    if not include_pending_edges:
        routine_stmt = routine_stmt.where(DeepProcedureLineageEdge.review_status == "ACTIVE")
    routine_rows = (await session.scalars(routine_stmt)).all()
    graph.note_scan_bound(routine_rows, graph.edge_limit, "EDGE_LIMIT")
    # One PROCEDURE_DEFINITION edge per table pair, whichever table states it.
    _register_definition_edges(
        graph,
        [*procedure_rows, *routine_rows],
        "PROCEDURE_DEFINITION",
        routine_references_by_id=await _load_routine_references(
            session, datasource, routine_rows
        ),
    )


async def collect_relationship_candidates(
    session: AsyncSession,
    datasource: DataSource,
    graph: BoundedGraph,
    table_ids: set[UUID],
    *,
    suggestion_status: SuggestionStatus,
) -> None:
    """Inferred column relationships, filtered by review state.

    `suggestion_status="ALL"` means no review filter at all -- including
    REJECTED rows, which a reviewer surface legitimately wants to see. It is
    the only place review state selects what this graph renders for candidates;
    `include_pending_edges` governs parser and importer rows instead.
    """
    candidates: Sequence[RelationshipCandidate] = []
    if table_ids:
        filters = [
            RelationshipCandidate.datasource_id == datasource.id,
            RelationshipCandidate.source_table_id.in_(table_ids),
            RelationshipCandidate.target_table_id.in_(table_ids),
        ]
        if suggestion_status != "ALL":
            filters.append(RelationshipCandidate.status == suggestion_status)
        candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(graph.edge_limit)
            )
        ).all()
    column_ids = {candidate.source_column_id for candidate in candidates} | {
        candidate.target_column_id for candidate in candidates
    }
    columns_by_id = (
        {
            column.id: column.name
            for column in (
                await session.scalars(
                    select(MetadataColumn).where(MetadataColumn.id.in_(column_ids))
                )
            ).all()
        }
        if column_ids
        else {}
    )
    for candidate in candidates:
        source_column = columns_by_id.get(candidate.source_column_id)
        target_column = columns_by_id.get(candidate.target_column_id)
        graph.register_link(
            UnifiedLink(
                edge_id=f"candidate:{candidate.id}",
                source_id=str(candidate.source_table_id),
                target_id=str(candidate.target_table_id),
                edge_source="SUGGESTED_RELATIONSHIP",
                status=candidate.status,
                confidence=candidate.confidence,
                source_columns=(source_column,) if source_column else (),
                target_columns=(target_column,) if target_column else (),
                evidence=dict(candidate.evidence),
            )
        )
    graph.note_scan_bound(candidates, graph.edge_limit, "EDGE_LIMIT")


async def collect_dbt_dependencies(
    session: AsyncSession,
    datasource: DataSource,
    graph: BoundedGraph,
    table_ids: set[UUID],
    *,
    include_pending_edges: bool,
) -> None:
    """dbt manifest dependency edges from the latest imported snapshot per project.

    A dbt resource matched to a catalog table reuses that table's node rather
    than adding a parallel one, so a model and the table it materialises are
    one node in this graph, not two. Column-level (LN-5) edges are consumed via
    the dedicated dbt lineage read surface: folding them in here would render
    one redundant parallel link per column pair between the same two nodes.
    """
    projects = (
        await session.scalars(
            select(DbtProject).where(
                DbtProject.datasource_id == datasource.id, DbtProject.status == "ACTIVE"
            )
        )
    ).all()
    resource_node_id: dict[UUID, str] = {}
    dbt_edge_total = 0
    for project in projects:
        latest_import = (
            await session.scalars(
                select(DbtArtifactImport)
                .where(
                    DbtArtifactImport.dbt_project_id == project.id,
                    DbtArtifactImport.status == "IMPORTED",
                )
                .order_by(DbtArtifactImport.created_at.desc())
                .limit(1)
            )
        ).first()
        if latest_import is None:
            continue
        resource_scan_limit = graph.node_limit + 1
        resources = (
            await session.scalars(
                select(DbtResource)
                .where(DbtResource.artifact_import_id == latest_import.id)
                .limit(resource_scan_limit)
            )
        ).all()
        graph.note_scan_bound(resources, resource_scan_limit, "NODE_LIMIT")
        for resource in resources:
            if resource.matched_table_id is not None and resource.matched_table_id in table_ids:
                resource_node_id[resource.id] = str(resource.matched_table_id)
                continue
            node_kind = DBT_NODE_KIND_BY_RESOURCE_TYPE.get(resource.resource_type)
            if node_kind is None:
                continue
            node_id = f"dbt:{resource.id}"
            registered = graph.register_node(
                UnifiedGraphNode(
                    id=node_id,
                    node_kind=node_kind,
                    label=resource.name,
                    qualified_name=resource.relation_name or resource.unique_id,
                    matched_table_id=None,
                    resolved=False,
                )
            )
            if registered:
                resource_node_id[resource.id] = node_id

        edge_scan_limit = graph.edge_limit + 1
        dbt_stmt = (
            select(DbtLineageEdge)
            .where(
                DbtLineageEdge.artifact_import_id == latest_import.id,
                DbtLineageEdge.edge_type == "DEPENDS_ON",
            )
            .limit(edge_scan_limit)
        )
        if not include_pending_edges:
            dbt_stmt = dbt_stmt.where(DbtLineageEdge.review_status == "ACTIVE")
        edges = (await session.scalars(dbt_stmt)).all()
        graph.note_scan_bound(edges, edge_scan_limit, "EDGE_LIMIT")
        for edge in edges:
            source_node = resource_node_id.get(edge.source_resource_id)
            target_node = resource_node_id.get(edge.target_resource_id)
            if source_node is None or target_node is None or source_node == target_node:
                continue
            if graph.register_link(
                UnifiedLink(
                    edge_id=f"dbt:{edge.id}",
                    source_id=source_node,
                    target_id=target_node,
                    edge_source="DBT_DEPENDENCY",
                    status="ACTIVE",
                    confidence=1.0,
                    evidence={"source": "DBT_MANIFEST", "edge_type": edge.edge_type},
                )
            ):
                dbt_edge_total += 1
    # Admitted dbt edges filling the whole edge budget is its own signal: the
    # accumulator only reports EDGE_LIMIT when a link is actually refused, and
    # a run that fits exactly would otherwise look complete.
    if dbt_edge_total >= graph.edge_limit:
        graph.note_truncated("EDGE_LIMIT")


async def collect_openlineage_edges(
    session: AsyncSession,
    datasource: DataSource,
    graph: BoundedGraph,
    table_ids: set[UUID],
    *,
    include_pending_edges: bool,
) -> None:
    """OpenLineage run-event table edges.

    The one provider that can introduce nodes of its own: a dataset the
    importer could not match to a catalog table still exists in the pipeline,
    so it renders as an UNRESOLVED_DATASET keyed by namespace and name. Both
    endpoints must be admitted before the edge is -- an unresolved node that
    lost the node budget takes its edge with it.
    """
    stmt = (
        select(OpenLineageTableEdge)
        .join(
            OpenLineageRunEvent,
            OpenLineageRunEvent.id == OpenLineageTableEdge.run_event_id,
        )
        .where(OpenLineageRunEvent.datasource_id == datasource.id)
        .order_by(OpenLineageTableEdge.created_at.desc())
        .limit(graph.edge_limit)
    )
    if not include_pending_edges:
        stmt = stmt.where(OpenLineageTableEdge.review_status == "ACTIVE")
    rows = (await session.scalars(stmt)).all()
    for edge in rows:
        input_node_id = (
            str(edge.input_table_id)
            if edge.input_table_id is not None and edge.input_table_id in table_ids
            else f"openlineage:{edge.input_dataset_namespace}:{edge.input_dataset_name}"
        )
        output_node_id = (
            str(edge.output_table_id)
            if edge.output_table_id is not None and edge.output_table_id in table_ids
            else f"openlineage:{edge.output_dataset_namespace}:{edge.output_dataset_name}"
        )
        if input_node_id == output_node_id:
            continue
        input_registered = graph.register_node(
            UnifiedGraphNode(
                id=input_node_id,
                node_kind="UNRESOLVED_DATASET",
                label=edge.input_dataset_name,
                qualified_name=f"{edge.input_dataset_namespace}.{edge.input_dataset_name}",
                matched_table_id=edge.input_table_id,
                resolved=edge.input_table_id is not None,
            )
        )
        output_registered = graph.register_node(
            UnifiedGraphNode(
                id=output_node_id,
                node_kind="UNRESOLVED_DATASET",
                label=edge.output_dataset_name,
                qualified_name=f"{edge.output_dataset_namespace}.{edge.output_dataset_name}",
                matched_table_id=edge.output_table_id,
                resolved=edge.output_table_id is not None,
            )
        )
        if not input_registered or not output_registered:
            continue
        graph.register_link(
            UnifiedLink(
                edge_id=f"openlineage:{edge.id}",
                source_id=output_node_id,
                target_id=input_node_id,
                edge_source="OPENLINEAGE_ETL",
                status="ACTIVE",
                confidence=1.0,
                evidence={"source": "OPENLINEAGE", "edge_kind": edge.edge_kind},
            )
        )
    graph.note_scan_bound(rows, graph.edge_limit, "EDGE_LIMIT")


async def build_unified_graph(
    session: AsyncSession,
    datasource: DataSource,
    *,
    node_limit: int,
    edge_limit: int,
    suggestion_status: SuggestionStatus,
    include_pending_edges: bool = False,
) -> UnifiedGraph:
    """Merge every lineage source for one datasource into one bounded graph.

    Provider order is the response's edge order and is therefore part of the
    contract, not an implementation detail. `datasource` arrives authorized:
    nothing here decides who may read it.
    """
    graph = BoundedGraph(node_limit=node_limit, edge_limit=edge_limit)
    table_ids = await collect_tables(session, datasource, graph)
    await collect_foreign_keys(session, datasource, graph, table_ids)
    await collect_definition_lineage(
        session, datasource, graph, table_ids, include_pending_edges=include_pending_edges
    )
    await collect_relationship_candidates(
        session, datasource, graph, table_ids, suggestion_status=suggestion_status
    )
    await collect_dbt_dependencies(
        session, datasource, graph, table_ids, include_pending_edges=include_pending_edges
    )
    await collect_openlineage_edges(
        session, datasource, graph, table_ids, include_pending_edges=include_pending_edges
    )
    return graph.snapshot()
