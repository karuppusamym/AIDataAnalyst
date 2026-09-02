"""Unified Lineage Explorer API.

Milestone 1 of the Collibra-parity lineage plan (see
`Docs/competitors/08-collibra-lineage-and-platform-analysis-2026-08.md` and
`Docs/20-modules/09-lineage.md`): one canonical graph that merges declared
foreign keys, human-approved/candidate column relationships, dbt manifest
dependency edges, OpenLineage table edges, and SQL-parsed view/procedure
lineage edges (LN-2, folded in for LN-7 -- table pairs resolved to the
catalog on both ends only), plus transitive, cross-kind, bounded
upstream/downstream impact traversal (`traverse` in `aida.unified_lineage`)
in place of direct-reference counting.

This intentionally does not yet cover: authoritative column-level mappings
(dbt UI still matches columns by name -- see `transformation-workbench.js`),
unmatched (free-text) view/procedure table names, BI/report nodes, AI
decision edges, or export. Those remain tracked as LN-3, LN-4, LN-10, LN-12
in `Docs/20-modules/09-lineage.md` and EA.9, EC.6+ in
`Docs/60-delivery/02-epic-backlog.md`.

AT-19: a `VIEW_DEFINITION` edge's `evidence` also carries a bounded, resolvable
`transformation_reference` (`{tool: "get_transformation_detail", entity_id,
kind}`) plus `redaction_status`/`availability`, sourced from envelope 1.1's
`MetadataViewDefinition` (unique per `table_id`, so the lookup is exact, not
guessed) -- never the DDL text itself, keeping this graph response's size
bound (ADR-0010) intact. `PROCEDURE_DEFINITION` edges deliberately do NOT get
one: `ProcedureLineageEdge` carries no identity back to the specific
`MetadataRoutine` row it was parsed from, so no reference is fabricated for
it (see `mcp_server.py::_view_definition_transformation_detail`).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.domain_service import check_cross_boundary_grant
from aida.envelope_models import MetadataViewDefinition
from aida.graph_store import (
    GraphStoreUnavailable,
    PostgresGraphStore,
    SnapshotBuilder,
    build_graph_store,
    resolve_graph_store_backend,
)
from aida.lineage_cache import get_lineage_cache
from aida.models import (
    DataDomain,
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
from aida.schemas import (
    DomainLineageGraphRead,
    UnifiedLineageEdgeRead,
    UnifiedLineageGraphRead,
    UnifiedLineageImpactRead,
    UnifiedLineageNodeRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.unified_lineage import UnifiedLink

router = APIRouter(prefix="/v1", tags=["unified-lineage"])


class LineageNodeNotFoundError(ValueError):
    """Raised by the reusable payload builders (shared by the REST routes below
    and the native MCP lineage tools in `mcp_server.py`) when a node id is not
    part of the caller's unified graph. Kept independent of FastAPI's
    `HTTPException` so it means the same thing over HTTP (404) and over MCP
    (a tool-call error content block) without either caller special-casing the
    other transport."""


UNIFIED_LINEAGE_READER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "MetadataReviewer",
    "Analyst",
    "Auditor",
    "Viewer",
)

_DBT_NODE_KIND_BY_RESOURCE_TYPE = {
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
_DEFINITION_LINEAGE_CONFIDENCE = {"FULL": 1.0, "PARTIAL": 0.6, "LOW": 0.3}


@dataclass(slots=True)
class _NodeInfo:
    id: str
    node_kind: str
    label: str
    qualified_name: str
    matched_table_id: UUID | None
    resolved: bool


@dataclass(slots=True)
class _UnifiedGraph:
    nodes: dict[str, _NodeInfo]
    links: list[UnifiedLink]
    counts_by_source: dict[str, int]
    truncation_reasons: list[str]


async def _build_unified_graph(
    session: AsyncSession,
    datasource: DataSource,
    *,
    node_limit: int,
    edge_limit: int,
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"],
) -> _UnifiedGraph:
    truncation_reasons: list[str] = []
    nodes: dict[str, _NodeInfo] = {}
    links: list[UnifiedLink] = []
    counts_by_source: dict[str, int] = {
        "FOREIGN_KEY": 0,
        "SUGGESTED_RELATIONSHIP": 0,
        "DBT_DEPENDENCY": 0,
        "OPENLINEAGE_ETL": 0,
        "VIEW_DEFINITION": 0,
        "PROCEDURE_DEFINITION": 0,
    }

    def register_node(info: _NodeInfo) -> bool:
        if info.id in nodes:
            return True
        if len(nodes) >= node_limit:
            truncation_reasons.append("NODE_LIMIT")
            return False
        nodes[info.id] = info
        return True

    def register_link(link: UnifiedLink) -> bool:
        if link.source_id not in nodes or link.target_id not in nodes:
            return False
        if len(links) >= edge_limit:
            truncation_reasons.append("EDGE_LIMIT")
            return False
        links.append(link)
        counts_by_source[link.edge_source] += 1
        return True

    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .limit(node_limit)
        )
    ).all()
    if len(table_rows) == node_limit:
        truncation_reasons.append("NODE_LIMIT")
    table_ids: set[UUID] = set()
    for table, schema, catalog in table_rows:
        node_id = str(table.id)
        table_ids.add(table.id)
        register_node(
            _NodeInfo(
                id=node_id,
                node_kind="TABLE",
                label=table.name,
                qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
                matched_table_id=table.id,
                resolved=True,
            )
        )

    # --- Declared foreign keys ---
    constraints = (
        (
            await session.scalars(
                select(MetadataConstraint)
                .where(
                    MetadataConstraint.datasource_id == datasource.id,
                    MetadataConstraint.status == "ACTIVE",
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.table_id.in_(table_ids),
                    MetadataConstraint.referenced_table_id.in_(table_ids),
                )
                .limit(edge_limit)
            )
        ).all()
        if table_ids
        else []
    )
    for constraint in constraints:
        if constraint.referenced_table_id is None:
            continue
        register_link(
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
    if len(constraints) >= edge_limit:
        truncation_reasons.append("EDGE_LIMIT")

    # --- View and stored-procedure SQL-parsed lineage (LN-2) ---
    # `view_lineage_api.py` persists one row per *column* pair
    # (source_table/source_column -> target_table/target_column, where target
    # is the view or procedure output). Only rows the parser matched to a
    # real catalog table on both ends are foldable into this table-level
    # graph -- an unmatched free-text table name (source_table_id is NULL)
    # cannot be safely deduplicated against a real MetadataTable without
    # risking a false merge across schemas that share a table name, so those
    # rows are left for the dedicated `/view-lineage` / `/procedure-lineage`
    # list endpoints instead of silently guessed here. Multiple column-level
    # rows between the same two tables collapse into one edge, exactly like
    # the dbt COLUMN_DEPENDS_ON rows above. `register_definition_edges` takes
    # each model concretely (rather than as a `type[X | Y]` parameter) so the
    # ORM row type stays precise for the type checker.
    def register_definition_edges(
        rows: Sequence[ViewLineageEdge] | Sequence[ProcedureLineageEdge],
        edge_source: Literal["VIEW_DEFINITION", "PROCEDURE_DEFINITION"],
        view_definitions_by_table_id: dict[UUID, tuple[str, str]] | None = None,
    ) -> None:
        grouped: dict[tuple[UUID, UUID], list[ViewLineageEdge | ProcedureLineageEdge]] = {}
        for row in rows:
            if row.source_table_id is None or row.target_table_id is None:
                continue
            if row.source_table_id == row.target_table_id:
                continue
            grouped.setdefault((row.source_table_id, row.target_table_id), []).append(row)
        for (source_table_id, target_table_id), edges in grouped.items():
            # The view/procedure (target_table_id) is the dependent node; the
            # base table it selects from (source_table_id) is what it depends
            # on -- same source-depends-on-target convention as FOREIGN_KEY
            # and DBT_DEPENDENCY above.
            evidence: dict[str, object] = {
                "source": edge_source,
                "dialect": edges[0].dialect,
                "sql_hash": edges[0].sql_hash,
                "column_edge_count": len(edges),
            }
            # AT-19: a VIEW_DEFINITION edge's target_table_id IS the view's own
            # MetadataTable.id, and MetadataViewDefinition.table_id is unique
            # per table (envelope 1.1) -- a genuine 1:1 lookup, so the edge can
            # carry a reference the caller can actually resolve via the
            # get_transformation_detail MCP tool, plus redaction status
            # in-line so "does this edge have code, and is it redacted" never
            # needs a round trip on its own. PROCEDURE_DEFINITION edges get
            # neither: ProcedureLineageEdge carries no identity back to a
            # specific MetadataRoutine row (no FK, no specific_name -- see
            # `mcp_server.py::_view_definition_transformation_detail`'s
            # docstring), so no reference is fabricated here.
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
            register_link(
                UnifiedLink(
                    edge_id=f"{edge_source.lower()}:{edges[0].id}",
                    source_id=str(target_table_id),
                    target_id=str(source_table_id),
                    edge_source=edge_source,
                    status="ACTIVE",
                    confidence=min(
                        _DEFINITION_LINEAGE_CONFIDENCE.get(edge.confidence, 0.3)
                        for edge in edges
                    ),
                    source_columns=tuple(sorted({edge.target_column for edge in edges})),
                    target_columns=tuple(sorted({edge.source_column for edge in edges})),
                    evidence=evidence,
                )
            )

    if table_ids:
        view_rows = (
            await session.scalars(
                select(ViewLineageEdge)
                .where(
                    ViewLineageEdge.datasource_id == datasource.id,
                    ViewLineageEdge.source_table_id.in_(table_ids),
                    ViewLineageEdge.target_table_id.in_(table_ids),
                )
                .order_by(ViewLineageEdge.id)
                .limit(edge_limit)
            )
        ).all()
        if len(view_rows) >= edge_limit:
            truncation_reasons.append("EDGE_LIMIT")

        # AT-19: fetch only the three narrow columns needed to build
        # `transformation_reference`/`redaction_status` above -- never the
        # `definition_sql_redacted` text itself, so this stays a bounded
        # reference lookup (ADR-0010) and not a way to smuggle DDL text into
        # an already-bounded graph payload.
        view_target_ids = {row.target_table_id for row in view_rows if row.target_table_id}
        view_definitions_by_table_id: dict[UUID, tuple[str, str]] = {}
        if view_target_ids:
            definition_rows = (
                await session.execute(
                    select(
                        MetadataViewDefinition.table_id,
                        MetadataViewDefinition.redaction_status,
                        MetadataViewDefinition.availability,
                    ).where(
                        MetadataViewDefinition.datasource_id == datasource.id,
                        MetadataViewDefinition.table_id.in_(view_target_ids),
                    )
                )
            ).all()
            view_definitions_by_table_id = {
                table_id: (redaction_status, availability)
                for table_id, redaction_status, availability in definition_rows
            }
        register_definition_edges(
            view_rows, "VIEW_DEFINITION", view_definitions_by_table_id
        )

        procedure_rows = (
            await session.scalars(
                select(ProcedureLineageEdge)
                .where(
                    ProcedureLineageEdge.datasource_id == datasource.id,
                    ProcedureLineageEdge.source_table_id.in_(table_ids),
                    ProcedureLineageEdge.target_table_id.in_(table_ids),
                )
                .order_by(ProcedureLineageEdge.id)
                .limit(edge_limit)
            )
        ).all()
        if len(procedure_rows) >= edge_limit:
            truncation_reasons.append("EDGE_LIMIT")
        register_definition_edges(procedure_rows, "PROCEDURE_DEFINITION")

    # --- Suggested / approved column relationships ---
    candidates: Sequence[RelationshipCandidate] = []
    if table_ids:
        candidate_filters = [
            RelationshipCandidate.datasource_id == datasource.id,
            RelationshipCandidate.source_table_id.in_(table_ids),
            RelationshipCandidate.target_table_id.in_(table_ids),
        ]
        if suggestion_status != "ALL":
            candidate_filters.append(RelationshipCandidate.status == suggestion_status)
        candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*candidate_filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(edge_limit)
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
        register_link(
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
    if len(candidates) >= edge_limit:
        truncation_reasons.append("EDGE_LIMIT")

    # --- dbt manifest dependency edges (latest imported snapshot per project) ---
    dbt_projects = (
        await session.scalars(
            select(DbtProject).where(
                DbtProject.datasource_id == datasource.id, DbtProject.status == "ACTIVE"
            )
        )
    ).all()
    resource_node_id: dict[UUID, str] = {}
    dbt_edge_total = 0
    for project in dbt_projects:
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
        resources = (
            await session.scalars(
                select(DbtResource)
                .where(DbtResource.artifact_import_id == latest_import.id)
                .limit(node_limit + 1)
            )
        ).all()
        if len(resources) > node_limit:
            truncation_reasons.append("NODE_LIMIT")
        for resource in resources:
            if resource.matched_table_id is not None and resource.matched_table_id in table_ids:
                resource_node_id[resource.id] = str(resource.matched_table_id)
                continue
            node_kind = _DBT_NODE_KIND_BY_RESOURCE_TYPE.get(resource.resource_type)
            if node_kind is None:
                continue
            node_id = f"dbt:{resource.id}"
            info = _NodeInfo(
                id=node_id,
                node_kind=node_kind,
                label=resource.name,
                qualified_name=resource.relation_name or resource.unique_id,
                matched_table_id=None,
                resolved=False,
            )
            if register_node(info):
                resource_node_id[resource.id] = node_id
        edges = (
            await session.scalars(
                select(DbtLineageEdge)
                .where(
                    DbtLineageEdge.artifact_import_id == latest_import.id,
                    # Column-level (LN-5) edges are consumed via the dedicated
                    # dbt lineage read surface, not folded into this
                    # table/resource-level graph -- without this filter, one
                    # column edge per column pair would render as a redundant
                    # parallel link between the same two dbt-resource nodes.
                    DbtLineageEdge.edge_type == "DEPENDS_ON",
                )
                .limit(edge_limit + 1)
            )
        ).all()
        if len(edges) > edge_limit:
            truncation_reasons.append("EDGE_LIMIT")
        for edge in edges:
            source_node = resource_node_id.get(edge.source_resource_id)
            target_node = resource_node_id.get(edge.target_resource_id)
            if source_node is None or target_node is None or source_node == target_node:
                continue
            if register_link(
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
    if dbt_edge_total >= edge_limit:
        truncation_reasons.append("EDGE_LIMIT")

    # --- OpenLineage table edges ---
    ol_rows = (
        await session.scalars(
            select(OpenLineageTableEdge)
            .join(
                OpenLineageRunEvent,
                OpenLineageRunEvent.id == OpenLineageTableEdge.run_event_id,
            )
            .where(OpenLineageRunEvent.datasource_id == datasource.id)
            .order_by(OpenLineageTableEdge.created_at.desc())
            .limit(edge_limit)
        )
    ).all()
    ol_edge_total = 0
    for ol_edge in ol_rows:
        input_node_id = (
            str(ol_edge.input_table_id)
            if ol_edge.input_table_id is not None and ol_edge.input_table_id in table_ids
            else f"openlineage:{ol_edge.input_dataset_namespace}:{ol_edge.input_dataset_name}"
        )
        output_node_id = (
            str(ol_edge.output_table_id)
            if ol_edge.output_table_id is not None and ol_edge.output_table_id in table_ids
            else f"openlineage:{ol_edge.output_dataset_namespace}:{ol_edge.output_dataset_name}"
        )
        if input_node_id == output_node_id:
            continue
        input_registered = register_node(
            _NodeInfo(
                id=input_node_id,
                node_kind="UNRESOLVED_DATASET",
                label=ol_edge.input_dataset_name,
                qualified_name=f"{ol_edge.input_dataset_namespace}.{ol_edge.input_dataset_name}",
                matched_table_id=ol_edge.input_table_id,
                resolved=ol_edge.input_table_id is not None,
            )
        )
        output_registered = register_node(
            _NodeInfo(
                id=output_node_id,
                node_kind="UNRESOLVED_DATASET",
                label=ol_edge.output_dataset_name,
                qualified_name=f"{ol_edge.output_dataset_namespace}.{ol_edge.output_dataset_name}",
                matched_table_id=ol_edge.output_table_id,
                resolved=ol_edge.output_table_id is not None,
            )
        )
        if not input_registered or not output_registered:
            continue
        if register_link(
            UnifiedLink(
                edge_id=f"openlineage:{ol_edge.id}",
                source_id=output_node_id,
                target_id=input_node_id,
                edge_source="OPENLINEAGE_ETL",
                status="ACTIVE",
                confidence=1.0,
                evidence={"source": "OPENLINEAGE", "edge_kind": ol_edge.edge_kind},
            )
        ):
            ol_edge_total += 1
    if len(ol_rows) >= edge_limit:
        truncation_reasons.append("EDGE_LIMIT")

    return _UnifiedGraph(
        nodes=nodes,
        links=links,
        counts_by_source=counts_by_source,
        truncation_reasons=sorted(set(truncation_reasons)),
    )


async def _load_datasource(
    session: AsyncSession, context: SecurityContext, datasource_id: UUID
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    return datasource


async def _load_domain(
    session: AsyncSession, context: SecurityContext, domain_id: UUID
) -> DataDomain:
    domain = await session.get(DataDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, domain.organization_id)
    return domain


async def build_unified_lineage_graph_payload(
    session: AsyncSession,
    datasource: DataSource,
    *,
    node_limit: int = 300,
    edge_limit: int = 1_500,
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = "APPROVED",
    settings: Settings | None = None,
) -> UnifiedLineageGraphRead:
    """Build the merged FK + suggested + dbt + OpenLineage + view/procedure graph for one
    datasource.

    Pulled out of the REST route so the exact same graph can also be served as
    a native MCP tool (`atlas__get_lineage_graph` in `mcp_server.py`) without
    duplicating the merge logic. The caller is responsible for loading and
    authorizing `datasource` -- this function does no access control itself.
    """

    cache_key = (
        f"aida:lineage:graph:{datasource.organization_id}:{datasource.id}:"
        f"{node_limit}:{edge_limit}:{suggestion_status}"
    )
    if settings is not None and settings.lineage_cache_enabled:
        cached = await get_lineage_cache(settings.redis_url).get(cache_key)
        if cached is not None:
            return UnifiedLineageGraphRead.model_validate(cached)

    graph = await _build_unified_graph(
        session,
        datasource,
        node_limit=node_limit,
        edge_limit=edge_limit,
        suggestion_status=suggestion_status,
    )

    inbound = Counter(link.target_id for link in graph.links)
    outbound = Counter(link.source_id for link in graph.links)
    node_reads = sorted(
        (
            UnifiedLineageNodeRead(
                id=info.id,
                node_kind=info.node_kind,
                label=info.label,
                qualified_name=info.qualified_name,
                matched_table_id=info.matched_table_id,
                resolved=info.resolved,
                inbound_edge_count=inbound[info.id],
                outbound_edge_count=outbound[info.id],
            )
            for info in graph.nodes.values()
        ),
        key=lambda node: node.qualified_name,
    )
    edge_reads = [
        UnifiedLineageEdgeRead(
            id=link.edge_id,
            edge_source=link.edge_source,
            source_node_id=link.source_id,
            target_node_id=link.target_id,
            source_label=graph.nodes[link.source_id].qualified_name,
            target_label=graph.nodes[link.target_id].qualified_name,
            status=link.status,
            confidence=link.confidence,
            source_columns=list(link.source_columns),
            target_columns=list(link.target_columns),
            evidence=link.evidence,
        )
        for link in graph.links
        if link.source_id in graph.nodes and link.target_id in graph.nodes
    ]

    result = UnifiedLineageGraphRead(
        datasource_id=datasource.id,
        nodes=node_reads,
        edges=edge_reads,
        counts_by_source=graph.counts_by_source,
        returned_node_count=len(node_reads),
        returned_edge_count=len(edge_reads),
        node_limit=node_limit,
        edge_limit=edge_limit,
        truncated=bool(graph.truncation_reasons),
        truncation_reasons=graph.truncation_reasons,
    )
    if settings is not None and settings.lineage_cache_enabled:
        await get_lineage_cache(settings.redis_url).set(
            cache_key,
            result.model_dump(mode="json"),
            settings.lineage_cache_ttl_seconds,
        )
    return result


async def build_domain_unified_lineage_graph_payload(
    session: AsyncSession,
    domain: DataDomain,
    *,
    node_limit: int = 300,
    edge_limit: int = 1_500,
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = "APPROVED",
    settings: Settings | None = None,
) -> DomainLineageGraphRead:
    """Federate the per-datasource unified lineage graph (above) across every
    datasource in one data_domain (ADR-0017 SS3, SS6).

    Deliberately NOT a rewrite of _build_unified_graph to natively span
    multiple datasources in one query -- that would duplicate ~300 lines of
    tested traversal logic and widen its blast radius. Instead each
    datasource's already-bounded graph is built by the unchanged single-
    datasource path, then merged here under the domain's own combined
    node_limit/edge_limit, stopping as soon as the budget is spent -- lazy,
    same as ADR-0010 already requires at the single-datasource scope; this
    is a federated bounded view, not a global graph query. Node/edge ids are
    prefixed per-datasource before merging, since a synthetic OpenLineage
    node id (`openlineage:{namespace}:{name}`) is not guaranteed globally
    unique across two unrelated datasources that happen to share a
    namespace -- prefixing removes the false-merge risk rather than hoping
    it doesn't occur.
    """

    datasources = (
        await session.scalars(
            select(DataSource)
            .where(DataSource.data_domain_id == domain.id)
            .order_by(DataSource.name)
        )
    ).all()

    merged_nodes: list[UnifiedLineageNodeRead] = []
    merged_edges: list[UnifiedLineageEdgeRead] = []
    counts_by_source: dict[str, int] = {}
    truncation_reasons: list[str] = []
    contributing_datasource_ids: list[UUID] = []

    for datasource in datasources:
        if len(merged_nodes) >= node_limit or len(merged_edges) >= edge_limit:
            truncation_reasons.append("DOMAIN_DATASOURCE_LIMIT")
            break
        per_source_graph = await build_unified_lineage_graph_payload(
            session,
            datasource,
            node_limit=node_limit - len(merged_nodes),
            edge_limit=edge_limit - len(merged_edges),
            suggestion_status=suggestion_status,
            settings=settings,
        )
        contributing_datasource_ids.append(datasource.id)
        prefix = f"{datasource.id}:"
        node_id_map: dict[str, str] = {}
        for node in per_source_graph.nodes:
            prefixed_id = f"{prefix}{node.id}"
            node_id_map[node.id] = prefixed_id
            merged_nodes.append(node.model_copy(update={"id": prefixed_id}))
        for edge in per_source_graph.edges:
            merged_edges.append(
                edge.model_copy(
                    update={
                        "id": f"{prefix}{edge.id}",
                        "source_node_id": node_id_map.get(
                            edge.source_node_id, f"{prefix}{edge.source_node_id}"
                        ),
                        "target_node_id": node_id_map.get(
                            edge.target_node_id, f"{prefix}{edge.target_node_id}"
                        ),
                    }
                )
            )
        for key, value in per_source_graph.counts_by_source.items():
            counts_by_source[key] = counts_by_source.get(key, 0) + value
        if per_source_graph.truncated:
            truncation_reasons.extend(
                f"{datasource.name}:{reason}" for reason in per_source_graph.truncation_reasons
            )

    # --- Cross-source suggested relationships (ADR-0017 phase 5) ---
    # Candidates whose source and target tables live in two DIFFERENT datasources
    # of this domain -- same-source candidates were already merged per-datasource
    # above. Never crosses a data_domain boundary: both sides are always drawn
    # from contributing_datasource_ids, which only ever holds this one domain's
    # datasources (see discover_cross_source_relationship_candidates).
    remaining_edge_budget = edge_limit - len(merged_edges)
    if contributing_datasource_ids and remaining_edge_budget > 0:
        cross_source_filters = [
            RelationshipCandidate.datasource_id.in_(contributing_datasource_ids),
            RelationshipCandidate.target_datasource_id.in_(contributing_datasource_ids),
            RelationshipCandidate.datasource_id != RelationshipCandidate.target_datasource_id,
        ]
        if suggestion_status != "ALL":
            cross_source_filters.append(RelationshipCandidate.status == suggestion_status)
        cross_source_candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*cross_source_filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(remaining_edge_budget)
            )
        ).all()
        column_ids = {candidate.source_column_id for candidate in cross_source_candidates} | {
            candidate.target_column_id for candidate in cross_source_candidates
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
        nodes_by_id = {node.id: node for node in merged_nodes}
        for candidate in cross_source_candidates:
            source_node_id = f"{candidate.datasource_id}:{candidate.source_table_id}"
            target_node_id = f"{candidate.target_datasource_id}:{candidate.target_table_id}"
            source_node = nodes_by_id.get(source_node_id)
            target_node = nodes_by_id.get(target_node_id)
            if source_node is None or target_node is None:
                # One endpoint fell outside its own datasource's bounded graph
                # (truncated, or not a resolved node) -- skip rather than render
                # a dangling edge; this is itself a form of truncation.
                continue
            source_column = columns_by_id.get(candidate.source_column_id)
            target_column = columns_by_id.get(candidate.target_column_id)
            merged_edges.append(
                UnifiedLineageEdgeRead(
                    id=f"cross-source-candidate:{candidate.id}",
                    edge_source="SUGGESTED_RELATIONSHIP",
                    source_node_id=source_node_id,
                    target_node_id=target_node_id,
                    source_label=source_node.label,
                    target_label=target_node.label,
                    status=candidate.status,
                    confidence=candidate.confidence,
                    source_columns=[source_column] if source_column else [],
                    target_columns=[target_column] if target_column else [],
                    evidence=dict(candidate.evidence),
                )
            )
        if len(cross_source_candidates) >= remaining_edge_budget:
            truncation_reasons.append("DOMAIN_CROSS_SOURCE_EDGE_LIMIT")

    # --- Cross-boundary edges (ADR-0017 phase 4 enforcement) ---
    # A RelationshipCandidate whose two datasources sit in DIFFERENT data_domains
    # only ever renders as an edge here if an ACTIVE, unexpired CrossBoundaryGrant
    # lets *this* domain see across into the other one (INV-5: deny-by-default,
    # explicit audited grants, never inherited). A domain that has such a
    # candidate but no covering grant is named in withheld_cross_boundary_domain_ids
    # -- reported, never silently dropped, mirroring the withheld:"no_grant"
    # transparency ADR-0017 SS4 requires. Only the specific tables that
    # participate in a *permitted* cross-boundary edge are pulled in from the
    # other domain -- never that domain's whole internal graph -- so a grant
    # exposes exactly the boundary it names, nothing more (least privilege).
    withheld_cross_boundary_domain_ids: set[UUID] = set()
    remaining_edge_budget = edge_limit - len(merged_edges)
    if contributing_datasource_ids and remaining_edge_budget > 0:
        boundary_filters = [
            or_(
                and_(
                    RelationshipCandidate.datasource_id.in_(contributing_datasource_ids),
                    RelationshipCandidate.target_datasource_id.notin_(
                        contributing_datasource_ids
                    ),
                ),
                and_(
                    RelationshipCandidate.target_datasource_id.in_(contributing_datasource_ids),
                    RelationshipCandidate.datasource_id.notin_(contributing_datasource_ids),
                ),
            )
        ]
        if suggestion_status != "ALL":
            boundary_filters.append(RelationshipCandidate.status == suggestion_status)
        boundary_scan_limit = remaining_edge_budget * 4
        boundary_candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*boundary_filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(boundary_scan_limit)
            )
        ).all()
        if boundary_candidates:
            other_datasource_ids = {
                candidate.target_datasource_id
                if candidate.datasource_id in contributing_datasource_ids
                else candidate.datasource_id
                for candidate in boundary_candidates
            }
            other_datasources_by_id = {
                other_datasource.id: other_datasource
                for other_datasource in (
                    await session.scalars(
                        select(DataSource).where(DataSource.id.in_(other_datasource_ids))
                    )
                ).all()
            }
            grant_cache: dict[UUID, bool] = {}
            allowed_candidates: list[RelationshipCandidate] = []
            allowed_table_ids: set[UUID] = set()
            for candidate in boundary_candidates:
                if len(allowed_candidates) >= remaining_edge_budget:
                    break
                this_side_in = candidate.datasource_id in contributing_datasource_ids
                other_datasource_id = (
                    candidate.target_datasource_id if this_side_in else candidate.datasource_id
                )
                other_datasource = other_datasources_by_id.get(other_datasource_id)
                if other_datasource is None:
                    continue
                other_domain_id = other_datasource.data_domain_id
                if other_domain_id not in grant_cache:
                    grant_cache[other_domain_id] = await check_cross_boundary_grant(
                        session,
                        domain.organization_id,
                        other_domain_id,
                        domain.id,
                        edge_kind="SUGGESTED_RELATIONSHIP",
                    )
                if not grant_cache[other_domain_id]:
                    withheld_cross_boundary_domain_ids.add(other_domain_id)
                    continue
                allowed_candidates.append(candidate)
                allowed_table_ids.add(
                    candidate.target_table_id if this_side_in else candidate.source_table_id
                )

            if allowed_candidates:
                nodes_by_id = {node.id: node for node in merged_nodes}
                external_table_rows = (
                    await session.execute(
                        select(MetadataTable, MetadataSchema, MetadataCatalog)
                        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                        .where(MetadataTable.id.in_(allowed_table_ids))
                    )
                ).all()
                for table, table_schema, catalog in external_table_rows:
                    node_id = f"{table.datasource_id}:{table.id}"
                    if node_id in nodes_by_id:
                        continue
                    external_node = UnifiedLineageNodeRead(
                        id=node_id,
                        node_kind="TABLE",
                        label=table.name,
                        qualified_name=f"{catalog.name}.{table_schema.name}.{table.name}",
                        matched_table_id=table.id,
                        resolved=True,
                    )
                    merged_nodes.append(external_node)
                    nodes_by_id[node_id] = external_node

                boundary_column_ids = set()
                for candidate in allowed_candidates:
                    boundary_column_ids.add(candidate.source_column_id)
                    boundary_column_ids.add(candidate.target_column_id)
                boundary_columns_by_id = (
                    {
                        column.id: column.name
                        for column in (
                            await session.scalars(
                                select(MetadataColumn).where(
                                    MetadataColumn.id.in_(boundary_column_ids)
                                )
                            )
                        ).all()
                    }
                    if boundary_column_ids
                    else {}
                )
                for candidate in allowed_candidates:
                    source_node_id = f"{candidate.datasource_id}:{candidate.source_table_id}"
                    target_node_id = f"{candidate.target_datasource_id}:{candidate.target_table_id}"
                    source_node = nodes_by_id.get(source_node_id)
                    target_node = nodes_by_id.get(target_node_id)
                    if source_node is None or target_node is None:
                        # Other endpoint wasn't a resolved table -- skip rather
                        # than render a dangling edge (same policy as same-
                        # domain cross-source candidates above).
                        continue
                    source_column = boundary_columns_by_id.get(candidate.source_column_id)
                    target_column = boundary_columns_by_id.get(candidate.target_column_id)
                    merged_edges.append(
                        UnifiedLineageEdgeRead(
                            id=f"cross-boundary-candidate:{candidate.id}",
                            edge_source="SUGGESTED_RELATIONSHIP",
                            source_node_id=source_node_id,
                            target_node_id=target_node_id,
                            source_label=source_node.label,
                            target_label=target_node.label,
                            status=candidate.status,
                            confidence=candidate.confidence,
                            source_columns=[source_column] if source_column else [],
                            target_columns=[target_column] if target_column else [],
                            evidence=dict(candidate.evidence),
                        )
                    )
            if len(boundary_candidates) >= boundary_scan_limit:
                truncation_reasons.append("CROSS_BOUNDARY_EDGE_LIMIT")

    merged_nodes.sort(key=lambda node: node.qualified_name)

    return DomainLineageGraphRead(
        data_domain_id=domain.id,
        datasource_ids=contributing_datasource_ids,
        nodes=merged_nodes,
        edges=merged_edges,
        counts_by_source=counts_by_source,
        returned_node_count=len(merged_nodes),
        returned_edge_count=len(merged_edges),
        node_limit=node_limit,
        edge_limit=edge_limit,
        truncated=bool(truncation_reasons),
        truncation_reasons=truncation_reasons,
        withheld_cross_boundary_domain_ids=sorted(
            withheld_cross_boundary_domain_ids, key=str
        ),
    )


async def build_unified_lineage_impact_payload(
    session: AsyncSession,
    datasource: DataSource,
    node_id: str,
    *,
    depth: int = 5,
    node_limit: int = 200,
    settings: Settings | None = None,
) -> UnifiedLineageImpactRead:
    """Compute transitive upstream/downstream impact for one node.

    Shared by the REST route and the native MCP tool
    (`atlas__get_lineage_impact`); see `build_unified_lineage_graph_payload`
    for why this is split out. Raises `LineageNodeNotFoundError` -- not
    `HTTPException` -- so both callers can translate it into their own
    transport's error shape.

    Backend selection (C7 / ADR-0020 amendment): `aida.graph_store` is the
    port. `postgres` -- the default, and what runs when `settings` is `None`
    (e.g. `aida.lineage_evidence_export`'s offline callers) -- traverses the
    same relational lineage tables `_build_unified_graph` always has, via
    `PostgresGraphStore`. A `neo4j`-configured organization is tried first as
    an accelerated read and, on any miss or backend failure, falls through to
    the same Postgres traversal rather than surfacing an error -- Postgres
    remains the fallback authority regardless of what is configured (INV-1).
    A `disabled` organization is treated the same way: `DisabledGraphStore`
    refuses immediately, which is caught here and treated as a miss, so this
    function's six other call sites (`mcp_server`, `tool_impact`,
    `relationship_candidate_review`, `lineage_evidence_export`, ...) never
    have to learn a new exception -- `LineageNodeNotFoundError` remains the
    only one this function raises.
    """

    cache_key = (
        f"aida:lineage:impact:{datasource.organization_id}:{datasource.id}:"
        f"{node_id}:{depth}:{node_limit}"
    )
    if settings is not None and settings.lineage_cache_enabled:
        cached = await get_lineage_cache(settings.redis_url).get(cache_key)
        if cached is not None:
            return UnifiedLineageImpactRead.model_validate(cached)

    if settings is not None:
        backend = await resolve_graph_store_backend(session, datasource.organization_id, settings)
        if backend != "postgres":
            accelerated = build_graph_store(backend, settings)
            try:
                projected = await accelerated.lineage_impact(
                    session, datasource, node_id, depth=depth, node_limit=node_limit
                )
            except GraphStoreUnavailable:
                projected = None
            if projected is not None:
                if settings.lineage_cache_enabled:
                    await get_lineage_cache(settings.redis_url).set(
                        cache_key,
                        projected.model_dump(mode="json"),
                        settings.lineage_cache_ttl_seconds,
                    )
                return projected

    # `_build_unified_graph`'s return type (`_UnifiedGraph`) structurally satisfies
    # `graph_store.LineageGraphSnapshot` -- proven directly in
    # `tests/test_graph_store.py` -- but mypy does not resolve that through a
    # Callable-to-Protocol return-type check when the Protocol's own members are
    # themselves generic (`Mapping[str, GraphNodeInfo]`); a plain value of the same
    # type checks fine, only the *function type* comparison does not. The `cast` is
    # exactly that known gap, not a real type hole.
    postgres_store = PostgresGraphStore(
        build_snapshot=cast(SnapshotBuilder, _build_unified_graph)
    )
    result = await postgres_store.lineage_impact(
        session, datasource, node_id, depth=depth, node_limit=node_limit
    )
    if result is None:
        raise LineageNodeNotFoundError(
            f"lineage node '{node_id}' not found in this datasource's graph"
        )
    if settings is not None and settings.lineage_cache_enabled:
        await get_lineage_cache(settings.redis_url).set(
            cache_key,
            result.model_dump(mode="json"),
            settings.lineage_cache_ttl_seconds,
        )
    return result


@router.get(
    "/datasources/{datasource_id}/unified-lineage/graph",
    response_model=UnifiedLineageGraphRead,
)
async def get_unified_lineage_graph(
    datasource_id: UUID,
    node_limit: int = Query(default=300, ge=5, le=2_000),
    edge_limit: int = Query(default=1_500, ge=5, le=10_000),
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = Query(
        default="APPROVED"
    ),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UnifiedLineageGraphRead:
    """Return the merged FK + suggested + dbt + OpenLineage + view/procedure graph for one
    datasource.

    This is the canonical lineage graph called for in the Collibra-parity
    plan: one node/edge set spanning every lineage source instead of
    separate, unlinked workbenches. Also served as the native MCP tool
    `atlas__get_lineage_graph` (`mcp_server.py`).
    """

    datasource = await _load_datasource(session, context, datasource_id)
    return await build_unified_lineage_graph_payload(
        session,
        datasource,
        node_limit=node_limit,
        edge_limit=edge_limit,
        suggestion_status=suggestion_status,
        settings=settings,
    )


@router.get(
    "/datasources/{datasource_id}/unified-lineage/impact/{node_id}",
    response_model=UnifiedLineageImpactRead,
)
async def get_unified_lineage_impact(
    datasource_id: UUID,
    node_id: str,
    depth: int = Query(default=5, ge=1, le=8),
    node_limit: int = Query(default=200, ge=5, le=2_000),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UnifiedLineageImpactRead:
    """Transitive upstream/downstream impact across every merged lineage source.

    Replaces `GET /v1/metadata/tables/{table_id}/impact`'s direct-reference
    count with a bounded multi-hop traversal: "what would break, N hops out,
    if this node changed" -- the gap called out against Collibra's impact
    analysis view. Also served as the native MCP tool
    `atlas__get_lineage_impact` (`mcp_server.py`).
    """

    datasource = await _load_datasource(session, context, datasource_id)
    try:
        return await build_unified_lineage_impact_payload(
            session,
            datasource,
            node_id,
            depth=depth,
            node_limit=node_limit,
            settings=settings,
        )
    except LineageNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/data-domains/{domain_id}/unified-lineage/graph",
    response_model=DomainLineageGraphRead,
)
async def get_domain_unified_lineage_graph(
    domain_id: UUID,
    node_limit: int = Query(default=600, ge=5, le=4_000),
    edge_limit: int = Query(default=3_000, ge=5, le=20_000),
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = Query(
        default="APPROVED"
    ),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DomainLineageGraphRead:
    """Return the merged FK + suggested + dbt + OpenLineage + view/procedure graph across every
    datasource in one data_domain (ADR-0017 SS3) -- the domain-scoped
    traversal endpoint closing KG-2/RL-5's "cross-source traversal" gap for
    sources sharing a governance boundary, without opening an unbounded
    org-wide graph (ADR-0010's bounded/lazy/value-free contract still
    applies at this wider scope; see build_domain_unified_lineage_graph_payload).
    """

    domain = await _load_domain(session, context, domain_id)
    return await build_domain_unified_lineage_graph_payload(
        session,
        domain,
        node_limit=node_limit,
        edge_limit=edge_limit,
        suggestion_status=suggestion_status,
        settings=settings,
    )
