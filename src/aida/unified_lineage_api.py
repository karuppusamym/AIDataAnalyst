"""Unified Lineage Explorer API.

Milestone 1 of the Collibra-parity lineage plan (see
`Docs/competitors/08-collibra-lineage-and-platform-analysis-2026-08.md` and
`Docs/20-modules/09-lineage.md`): one canonical graph that merges declared
foreign keys, human-approved/candidate column relationships, dbt manifest
dependency edges, and OpenLineage table edges, plus transitive
upstream/downstream impact traversal in place of direct-reference counting.

This intentionally does not yet cover: authoritative column-level mappings
(dbt UI still matches columns by name -- see `transformation-workbench.js`),
view/stored-procedure lineage, BI/report nodes, or export. Those remain
tracked as EA.9, EC.6+ in `Docs/60-delivery/02-epic-backlog.md`.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
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
    RelationshipCandidate,
)
from aida.schemas import (
    UnifiedLineageEdgeRead,
    UnifiedLineageGraphRead,
    UnifiedLineageImpactNodeRead,
    UnifiedLineageImpactRead,
    UnifiedLineageNodeRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.unified_lineage import TraversalResult, UnifiedLink, traverse

router = APIRouter(prefix="/v1", tags=["unified-lineage"])

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
    }

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
        nodes[node_id] = _NodeInfo(
            id=node_id,
            node_kind="TABLE",
            label=table.name,
            qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
            matched_table_id=table.id,
            resolved=True,
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
        links.append(
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
    counts_by_source["FOREIGN_KEY"] = len(constraints)
    if len(constraints) == edge_limit:
        truncation_reasons.append("EDGE_LIMIT")

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
        links.append(
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
    counts_by_source["SUGGESTED_RELATIONSHIP"] = len(candidates)
    if len(candidates) == edge_limit:
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
                select(DbtResource).where(DbtResource.artifact_import_id == latest_import.id)
            )
        ).all()
        for resource in resources:
            if resource.matched_table_id is not None and resource.matched_table_id in table_ids:
                resource_node_id[resource.id] = str(resource.matched_table_id)
                continue
            node_kind = _DBT_NODE_KIND_BY_RESOURCE_TYPE.get(resource.resource_type)
            if node_kind is None:
                continue
            node_id = f"dbt:{resource.id}"
            resource_node_id[resource.id] = node_id
            nodes.setdefault(
                node_id,
                _NodeInfo(
                    id=node_id,
                    node_kind=node_kind,
                    label=resource.name,
                    qualified_name=resource.relation_name or resource.unique_id,
                    matched_table_id=None,
                    resolved=False,
                ),
            )
        edges = (
            await session.scalars(
                select(DbtLineageEdge).where(DbtLineageEdge.artifact_import_id == latest_import.id)
            )
        ).all()
        for edge in edges:
            source_node = resource_node_id.get(edge.source_resource_id)
            target_node = resource_node_id.get(edge.target_resource_id)
            if source_node is None or target_node is None or source_node == target_node:
                continue
            links.append(
                UnifiedLink(
                    edge_id=f"dbt:{edge.id}",
                    source_id=source_node,
                    target_id=target_node,
                    edge_source="DBT_DEPENDENCY",
                    status="ACTIVE",
                    confidence=1.0,
                    evidence={"source": "DBT_MANIFEST", "edge_type": edge.edge_type},
                )
            )
            dbt_edge_total += 1
    counts_by_source["DBT_DEPENDENCY"] = dbt_edge_total
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
        nodes.setdefault(
            input_node_id,
            _NodeInfo(
                id=input_node_id,
                node_kind="UNRESOLVED_DATASET",
                label=ol_edge.input_dataset_name,
                qualified_name=f"{ol_edge.input_dataset_namespace}.{ol_edge.input_dataset_name}",
                matched_table_id=ol_edge.input_table_id,
                resolved=ol_edge.input_table_id is not None,
            ),
        )
        nodes.setdefault(
            output_node_id,
            _NodeInfo(
                id=output_node_id,
                node_kind="UNRESOLVED_DATASET",
                label=ol_edge.output_dataset_name,
                qualified_name=f"{ol_edge.output_dataset_namespace}.{ol_edge.output_dataset_name}",
                matched_table_id=ol_edge.output_table_id,
                resolved=ol_edge.output_table_id is not None,
            ),
        )
        links.append(
            UnifiedLink(
                edge_id=f"openlineage:{ol_edge.id}",
                source_id=output_node_id,
                target_id=input_node_id,
                edge_source="OPENLINEAGE_ETL",
                status="ACTIVE",
                confidence=1.0,
                evidence={"source": "OPENLINEAGE", "edge_kind": ol_edge.edge_kind},
            )
        )
        ol_edge_total += 1
    counts_by_source["OPENLINEAGE_ETL"] = ol_edge_total
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
) -> UnifiedLineageGraphRead:
    """Return the merged FK + suggested + dbt + OpenLineage graph for one datasource.

    This is the canonical lineage graph called for in the Collibra-parity
    plan: one node/edge set spanning every lineage source instead of
    separate, unlinked workbenches.
    """

    datasource = await _load_datasource(session, context, datasource_id)
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

    return UnifiedLineageGraphRead(
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
) -> UnifiedLineageImpactRead:
    """Transitive upstream/downstream impact across every merged lineage source.

    Replaces `GET /v1/metadata/tables/{table_id}/impact`'s direct-reference
    count with a bounded multi-hop traversal: "what would break, N hops out,
    if this node changed" -- the gap called out against Collibra's impact
    analysis view.
    """

    datasource = await _load_datasource(session, context, datasource_id)
    graph = await _build_unified_graph(
        session,
        datasource,
        node_limit=2_000,
        edge_limit=10_000,
        suggestion_status="APPROVED",
    )
    focus = graph.nodes.get(node_id)
    if focus is None:
        raise HTTPException(
            status_code=404, detail="lineage node not found in this datasource's graph"
        )

    upstream = traverse(
        seed=node_id,
        links=graph.links,
        direction="REFERENCES",
        max_depth=depth,
        node_limit=node_limit,
    )
    downstream = traverse(
        seed=node_id,
        links=graph.links,
        direction="REFERENCED_BY",
        max_depth=depth,
        node_limit=node_limit,
    )

    def _rows(result: TraversalResult) -> list[UnifiedLineageImpactNodeRead]:
        rows = []
        for candidate_id, candidate_depth in sorted(
            result.node_depths.items(), key=lambda item: (item[1], item[0])
        ):
            if candidate_id == node_id:
                continue
            info = graph.nodes.get(candidate_id)
            if info is None:
                continue
            rows.append(
                UnifiedLineageImpactNodeRead(
                    node_id=candidate_id,
                    node_kind=info.node_kind,
                    label=info.label,
                    qualified_name=info.qualified_name,
                    depth=candidate_depth,
                    contributing_edge_sources=sorted(
                        result.contributing_edge_sources.get(candidate_id, frozenset())
                    ),
                )
            )
        return rows

    return UnifiedLineageImpactRead(
        datasource_id=datasource.id,
        focus_node_id=node_id,
        focus_node_kind=focus.node_kind,
        focus_label=focus.qualified_name,
        upstream=_rows(upstream),
        downstream=_rows(downstream),
        requested_depth=depth,
        node_limit=node_limit,
        upstream_truncated=upstream.truncated,
        downstream_truncated=downstream.truncated,
    )
