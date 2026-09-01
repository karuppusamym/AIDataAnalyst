import hashlib
import hmac
import math
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.domain_service import check_cross_boundary_grant
from aida.events import record_audit, record_outbox
from aida.identity_merge import merge_table_identity
from aida.identity_resolution import IdentityMatch, score_cross_source_match
from aida.knowledge_graph import GraphDirection, GraphLink, expand_frontier
from aida.models import (
    AgentRun,
    CanonicalTableMapping,
    ColumnProfile,
    CrossSourceResolutionCandidate,
    DataDomain,
    DataSource,
    DbtResource,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    QueryExecution,
    QueryFeedback,
    QueryMemoryEvidence,
    RelationshipCandidate,
    RelationshipCandidateGroundTruthLabel,
    RelationshipCandidateGroup,
    RelationshipCandidateGroupMember,
    RenameCandidate,
    SemanticMetricVersion,
    TableFamilyCandidate,
    TableProfile,
)
from aida.relationship_intelligence import (
    ColumnMeta,
    generate_composite_relationship_candidates,
    resolve_canonical_table_id,
)
from aida.relationship_naming import canonical_column_name, physical_type_family
from aida.schemas import (
    RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS,
    CanonicalTableMappingRead,
    CanonicalTableOverrideRequest,
    CompositeRelationshipCandidateDiscoveryRequest,
    CompositeRelationshipCandidateMemberRead,
    CompositeRelationshipCandidateRead,
    CrossSourceObjectResolutionDiscoveryRequest,
    CrossSourceRelationshipCandidateDiscoveryRequest,
    CrossSourceResolutionCandidateDecision,
    CrossSourceResolutionCandidateRead,
    GraphEdgeRead,
    GraphNodeRead,
    GraphSearchRead,
    ImpactAnalysisRead,
    KnowledgeGraphRead,
    Page,
    QueryFeedbackRead,
    QueryFeedbackUpsert,
    QueryMemoryEvidenceRead,
    RelationshipCandidateBulkDecisionItemRead,
    RelationshipCandidateBulkDecisionRequest,
    RelationshipCandidateBulkDecisionResultRead,
    RelationshipCandidateBulkSelectionFilter,
    RelationshipCandidateCalibrationBucketRead,
    RelationshipCandidateCalibrationRead,
    RelationshipCandidateDecision,
    RelationshipCandidateDiscoveryRequest,
    RelationshipCandidateRead,
    RenameCandidateDecision,
    RenameCandidateRead,
    TableRef,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["intelligence-governance"])

GRAPH_READER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "MetadataReviewer",
    "Analyst",
    "Auditor",
    "Viewer",
)
SENSITIVE_CLASSIFICATIONS = {"PII", "PCI", "PHI", "SECRET", "CONFIDENTIAL"}


def _is_positive(rating: str) -> bool:
    return rating == "HELPFUL"


@router.get(
    "/datasources/{datasource_id}/knowledge-graph",
    response_model=KnowledgeGraphRead,
)
async def get_knowledge_graph(
    datasource_id: UUID,
    limit: int = Query(default=250, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "DataSteward",
            "MetadataReviewer",
            "Analyst",
            "Auditor",
            "Viewer",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeGraphRead:
    """Return a bounded authoritative graph slice with declared and suggested edges."""
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    table_filter = (
        MetadataTable.datasource_id == datasource.id,
        MetadataTable.status == "ACTIVE",
    )
    total_tables = int(
        await session.scalar(select(func.count()).select_from(MetadataTable).where(*table_filter))
        or 0
    )
    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(*table_filter)
            .order_by(MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    table_ids = {table.id for table, _, _ in table_rows}
    columns = (
        (
            await session.scalars(
                select(MetadataColumn).where(
                    MetadataColumn.table_id.in_(table_ids), MetadataColumn.status == "ACTIVE"
                )
            )
        ).all()
        if table_ids
        else []
    )
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    columns_by_id: dict[UUID, MetadataColumn] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
        columns_by_id[column.id] = column
    table_labels = {
        table.id: f"{catalog.name}.{schema.name}.{table.name}"
        for table, schema, catalog in table_rows
    }
    nodes = [
        GraphNodeRead(
            id=table.id,
            node_type="TABLE",
            label=table.name,
            qualified_name=table_labels[table.id],
            object_type=table.object_type,
            status=table.status,
            column_count=len(columns_by_table.get(table.id, [])),
            sensitive_column_count=sum(
                column.classification in SENSITIVE_CLASSIFICATIONS
                for column in columns_by_table.get(table.id, [])
            ),
        )
        for table, _, _ in table_rows
    ]
    declared_total = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataConstraint)
            .where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
            )
        )
        or 0
    )
    constraints = (
        (
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.datasource_id == datasource.id,
                    MetadataConstraint.status == "ACTIVE",
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.table_id.in_(table_ids),
                    MetadataConstraint.referenced_table_id.in_(table_ids),
                )
            )
        ).all()
        if table_ids
        else []
    )
    candidate_filters = (RelationshipCandidate.datasource_id == datasource.id,)
    suggested_total = int(
        await session.scalar(
            select(func.count()).select_from(RelationshipCandidate).where(*candidate_filters)
        )
        or 0
    )
    pending_suggestions = int(
        await session.scalar(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(*candidate_filters, RelationshipCandidate.status == "PENDING")
        )
        or 0
    )
    candidates = (
        (
            await session.scalars(
                select(RelationshipCandidate)
                .where(
                    *candidate_filters,
                    RelationshipCandidate.source_table_id.in_(table_ids),
                    RelationshipCandidate.target_table_id.in_(table_ids),
                )
                .order_by(
                    RelationshipCandidate.status,
                    RelationshipCandidate.confidence.desc(),
                )
                .limit(2000)
            )
        ).all()
        if table_ids
        else []
    )
    edges = [
        GraphEdgeRead(
            id=f"constraint:{constraint.id}",
            edge_type="DECLARED_FOREIGN_KEY",
            source_node_id=constraint.table_id,
            target_node_id=constraint.referenced_table_id,
            source_label=table_labels[constraint.table_id],
            target_label=table_labels[constraint.referenced_table_id],
            source_columns=constraint.columns,
            target_columns=constraint.referenced_columns,
            status="DECLARED",
            confidence=1.0,
            evidence={"source": "DATABASE_CONSTRAINT", "source_values_inspected": False},
        )
        for constraint in constraints
        if constraint.referenced_table_id is not None
    ]
    edges.extend(
        GraphEdgeRead(
            id=f"candidate:{candidate.id}",
            edge_type="SUGGESTED_RELATIONSHIP",
            source_node_id=candidate.source_table_id,
            target_node_id=candidate.target_table_id,
            source_label=table_labels[candidate.source_table_id],
            target_label=table_labels[candidate.target_table_id],
            source_columns=[columns_by_id[candidate.source_column_id].name],
            target_columns=[columns_by_id[candidate.target_column_id].name],
            status=candidate.status,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            candidate_id=candidate.id,
        )
        for candidate in candidates
        if candidate.source_column_id in columns_by_id
        and candidate.target_column_id in columns_by_id
    )
    inbound = Counter(edge.target_node_id for edge in edges)
    outbound = Counter(edge.source_node_id for edge in edges)
    nodes = [
        node.model_copy(
            update={
                "inbound_edge_count": inbound[node.id],
                "outbound_edge_count": outbound[node.id],
            }
        )
        for node in nodes
    ]
    truncation_reasons = []
    if offset + len(nodes) < total_tables:
        truncation_reasons.append("TABLE_PAGE")
    if len(candidates) == 2000:
        truncation_reasons.append("EDGE_LIMIT")
    return KnowledgeGraphRead(
        datasource_id=datasource.id,
        nodes=nodes,
        edges=edges,
        total_tables=total_tables,
        total_declared_edges=declared_total,
        total_suggested_edges=suggested_total,
        pending_suggestions=pending_suggestions,
        truncated=bool(truncation_reasons),
        returned_node_count=len(nodes),
        returned_edge_count=len(edges),
        node_limit=limit,
        edge_limit=2000,
        truncation_reasons=truncation_reasons,
    )


@router.get(
    "/datasources/{datasource_id}/knowledge-graph/search",
    response_model=GraphSearchRead,
)
async def search_knowledge_graph(
    datasource_id: UUID,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GraphSearchRead:
    """Search active table nodes without exposing source data values."""

    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)

    normalized_query = q.strip()
    if len(normalized_query) < 2:
        raise HTTPException(status_code=400, detail="graph search requires two visible characters")
    escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    search_filter = or_(
        MetadataTable.name.ilike(pattern, escape="\\"),
        MetadataSchema.name.ilike(pattern, escape="\\"),
        MetadataCatalog.name.ilike(pattern, escape="\\"),
    )
    base_filters = (
        MetadataTable.datasource_id == datasource.id,
        MetadataTable.status == "ACTIVE",
        search_filter,
    )
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataTable)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(*base_filters)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(*base_filters)
            .order_by(MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .limit(limit)
        )
    ).all()
    table_ids = {table.id for table, _, _ in rows}
    columns = (
        (
            await session.scalars(
                select(MetadataColumn).where(
                    MetadataColumn.table_id.in_(table_ids), MetadataColumn.status == "ACTIVE"
                )
            )
        ).all()
        if table_ids
        else []
    )
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
    items = [
        GraphNodeRead(
            id=table.id,
            node_type="TABLE",
            label=table.name,
            qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
            object_type=table.object_type,
            status=table.status,
            column_count=len(columns_by_table.get(table.id, [])),
            sensitive_column_count=sum(
                column.classification in SENSITIVE_CLASSIFICATIONS
                for column in columns_by_table.get(table.id, [])
            ),
        )
        for table, schema, catalog in rows
    ]
    return GraphSearchRead(
        datasource_id=datasource.id,
        query=normalized_query,
        items=items,
        total=total,
        truncated=len(items) < total,
    )


@router.get(
    "/datasources/{datasource_id}/knowledge-graph/neighborhood",
    response_model=KnowledgeGraphRead,
)
async def get_knowledge_graph_neighborhood(
    datasource_id: UUID,
    focus_table_id: UUID,
    depth: int = Query(default=1, ge=1, le=8),
    direction: GraphDirection = Query(default="BOTH"),
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = Query(default="ALL"),
    node_limit: int = Query(default=100, ge=5, le=2_000),
    edge_limit: int = Query(default=500, ge=1, le=10_000),
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> KnowledgeGraphRead:
    """Expand a bounded, value-free table neighborhood from one authorized node."""

    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    if depth > settings.knowledge_graph_max_depth:
        raise HTTPException(status_code=400, detail="requested graph depth exceeds policy")
    if node_limit > settings.knowledge_graph_max_nodes:
        raise HTTPException(status_code=400, detail="requested graph node limit exceeds policy")
    if edge_limit > settings.knowledge_graph_max_edges:
        raise HTTPException(status_code=400, detail="requested graph edge limit exceeds policy")

    focus = await session.get(MetadataTable, focus_table_id)
    if focus is None or focus.datasource_id != datasource.id or focus.status != "ACTIVE":
        raise HTTPException(status_code=404, detail="active graph focus table not found")

    visited: set[UUID] = {focus.id}
    frontier: set[UUID] = {focus.id}
    node_depths: dict[UUID, int] = {focus.id: 0}
    truncation_reasons: set[str] = set()
    encountered_links: dict[str, GraphLink] = {}

    for current_depth in range(1, depth + 1):
        if not frontier or len(visited) >= node_limit or len(encountered_links) >= edge_limit:
            if frontier and len(visited) >= node_limit:
                truncation_reasons.add("NODE_LIMIT")
            if frontier and len(encountered_links) >= edge_limit:
                truncation_reasons.add("EDGE_LIMIT")
            break

        constraint_frontier: ColumnElement[bool]
        candidate_frontier: ColumnElement[bool]
        if direction == "REFERENCES":
            constraint_frontier = MetadataConstraint.table_id.in_(frontier)
            candidate_frontier = RelationshipCandidate.source_table_id.in_(frontier)
        elif direction == "REFERENCED_BY":
            constraint_frontier = MetadataConstraint.referenced_table_id.in_(frontier)
            candidate_frontier = RelationshipCandidate.target_table_id.in_(frontier)
        else:
            constraint_frontier = or_(
                MetadataConstraint.table_id.in_(frontier),
                MetadataConstraint.referenced_table_id.in_(frontier),
            )
            candidate_frontier = or_(
                RelationshipCandidate.source_table_id.in_(frontier),
                RelationshipCandidate.target_table_id.in_(frontier),
            )

        probe_limit = edge_limit - len(encountered_links) + 1
        constraints = (
            await session.scalars(
                select(MetadataConstraint)
                .where(
                    MetadataConstraint.datasource_id == datasource.id,
                    MetadataConstraint.status == "ACTIVE",
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.referenced_table_id.is_not(None),
                    constraint_frontier,
                )
                .order_by(MetadataConstraint.id)
                .limit(probe_limit)
            )
        ).all()
        candidate_filters = [
            RelationshipCandidate.datasource_id == datasource.id,
            candidate_frontier,
        ]
        if suggestion_status != "ALL":
            candidate_filters.append(RelationshipCandidate.status == suggestion_status)
        candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*candidate_filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(probe_limit)
            )
        ).all()
        if len(constraints) == probe_limit or len(candidates) == probe_limit:
            truncation_reasons.add("EDGE_SCAN_LIMIT")

        links = [
            GraphLink(
                edge_id=f"constraint:{constraint.id}",
                source_node_id=constraint.table_id,
                target_node_id=constraint.referenced_table_id,
            )
            for constraint in constraints
            if constraint.referenced_table_id is not None
        ]
        links.extend(
            GraphLink(
                edge_id=f"candidate:{candidate.id}",
                source_node_id=candidate.source_table_id,
                target_node_id=candidate.target_table_id,
            )
            for candidate in candidates
        )
        remaining_edge_capacity = edge_limit - len(encountered_links)
        for link in sorted(links, key=lambda item: item.edge_id)[:remaining_edge_capacity]:
            encountered_links.setdefault(link.edge_id, link)
        if len(links) > remaining_edge_capacity:
            truncation_reasons.add("EDGE_LIMIT")

        expansion = expand_frontier(
            frontier=frontier,
            visited=visited,
            links=list(encountered_links.values()),
            direction=direction,
            depth=current_depth,
            node_limit=node_limit,
        )
        if expansion.truncated:
            truncation_reasons.add("NODE_LIMIT")
        frontier = set(expansion.node_ids)
        visited.update(frontier)
        node_depths.update(expansion.node_depths)

    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.id.in_(visited),
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    active_table_ids = {table.id for table, _, _ in table_rows}
    table_labels = {
        table.id: f"{catalog.name}.{schema.name}.{table.name}"
        for table, schema, catalog in table_rows
    }
    columns = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id.in_(active_table_ids),
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    columns_by_id: dict[UUID, MetadataColumn] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
        columns_by_id[column.id] = column

    final_constraints = (
        await session.scalars(
            select(MetadataConstraint)
            .where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
                MetadataConstraint.table_id.in_(active_table_ids),
                MetadataConstraint.referenced_table_id.in_(active_table_ids),
            )
            .order_by(MetadataConstraint.id)
            .limit(edge_limit + 1)
        )
    ).all()
    final_candidate_filters = [
        RelationshipCandidate.datasource_id == datasource.id,
        RelationshipCandidate.source_table_id.in_(active_table_ids),
        RelationshipCandidate.target_table_id.in_(active_table_ids),
    ]
    if suggestion_status != "ALL":
        final_candidate_filters.append(RelationshipCandidate.status == suggestion_status)
    final_candidates = (
        await session.scalars(
            select(RelationshipCandidate)
            .where(*final_candidate_filters)
            .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
            .limit(edge_limit + 1)
        )
    ).all()
    edge_records: list[GraphEdgeRead] = [
        GraphEdgeRead(
            id=f"constraint:{constraint.id}",
            edge_type="DECLARED_FOREIGN_KEY",
            source_node_id=constraint.table_id,
            target_node_id=constraint.referenced_table_id,
            source_label=table_labels[constraint.table_id],
            target_label=table_labels[constraint.referenced_table_id],
            source_columns=constraint.columns,
            target_columns=constraint.referenced_columns,
            status="DECLARED",
            confidence=1.0,
            evidence={"source": "DATABASE_CONSTRAINT", "source_values_inspected": False},
        )
        for constraint in final_constraints
        if constraint.referenced_table_id is not None
    ]
    edge_records.extend(
        GraphEdgeRead(
            id=f"candidate:{candidate.id}",
            edge_type="SUGGESTED_RELATIONSHIP",
            source_node_id=candidate.source_table_id,
            target_node_id=candidate.target_table_id,
            source_label=table_labels[candidate.source_table_id],
            target_label=table_labels[candidate.target_table_id],
            source_columns=[columns_by_id[candidate.source_column_id].name],
            target_columns=[columns_by_id[candidate.target_column_id].name],
            status=candidate.status,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            candidate_id=candidate.id,
        )
        for candidate in final_candidates
        if candidate.source_column_id in columns_by_id
        and candidate.target_column_id in columns_by_id
    )
    if len(edge_records) > edge_limit:
        truncation_reasons.add("EDGE_LIMIT")
        edge_records = edge_records[:edge_limit]

    inbound = Counter(edge.target_node_id for edge in edge_records)
    outbound = Counter(edge.source_node_id for edge in edge_records)
    nodes = sorted(
        (
            GraphNodeRead(
                id=table.id,
                node_type="TABLE",
                label=table.name,
                qualified_name=table_labels[table.id],
                object_type=table.object_type,
                status=table.status,
                column_count=len(columns_by_table.get(table.id, [])),
                sensitive_column_count=sum(
                    column.classification in SENSITIVE_CLASSIFICATIONS
                    for column in columns_by_table.get(table.id, [])
                ),
                depth=node_depths.get(table.id, depth),
                inbound_edge_count=inbound[table.id],
                outbound_edge_count=outbound[table.id],
            )
            for table, _, _ in table_rows
        ),
        key=lambda node: (node.depth, node.qualified_name),
    )

    total_tables = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataTable)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
        )
        or 0
    )
    declared_total = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataConstraint)
            .where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
            )
        )
        or 0
    )
    suggested_total = int(
        await session.scalar(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(RelationshipCandidate.datasource_id == datasource.id)
        )
        or 0
    )
    pending_suggestions = int(
        await session.scalar(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(
                RelationshipCandidate.datasource_id == datasource.id,
                RelationshipCandidate.status == "PENDING",
            )
        )
        or 0
    )
    return KnowledgeGraphRead(
        datasource_id=datasource.id,
        nodes=nodes,
        edges=edge_records,
        total_tables=total_tables,
        total_declared_edges=declared_total,
        total_suggested_edges=suggested_total,
        pending_suggestions=pending_suggestions,
        truncated=bool(truncation_reasons),
        focus_node_id=focus.id,
        direction=direction,
        requested_depth=depth,
        returned_node_count=len(nodes),
        returned_edge_count=len(edge_records),
        node_limit=node_limit,
        edge_limit=edge_limit,
        truncation_reasons=sorted(truncation_reasons),
    )


@router.put("/agent-runs/{agent_run_id}/feedback", response_model=QueryFeedbackRead)
async def upsert_query_feedback(
    agent_run_id: UUID,
    body: QueryFeedbackUpsert,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "Analyst", "AgentDeveloper")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QueryFeedback:
    agent_run = await session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    enforce_organization(context, agent_run.organization_id)
    if "PlatformAdmin" not in context.roles and agent_run.principal_id != context.principal_id:
        raise HTTPException(status_code=403, detail="feedback is limited to the run owner")
    if agent_run.status != "COMPLETED" or agent_run.query_execution_id is None:
        raise HTTPException(status_code=409, detail="only completed agent runs accept feedback")
    execution = await session.get(QueryExecution, agent_run.query_execution_id)
    if execution is None:
        raise HTTPException(status_code=409, detail="query execution evidence is unavailable")

    memory = await session.scalar(
        select(QueryMemoryEvidence).where(QueryMemoryEvidence.agent_run_id == agent_run.id)
    )
    if memory is None:
        memory = QueryMemoryEvidence(
            organization_id=agent_run.organization_id,
            datasource_id=agent_run.datasource_id,
            agent_run_id=agent_run.id,
            query_execution_id=execution.id,
            question_hash=agent_run.question_hash,
            sql_hash=execution.sql_hash,
            semantic_version=agent_run.semantic_version,
            status="OBSERVED",
        )
        session.add(memory)
        await session.flush()
    feedback = await session.scalar(
        select(QueryFeedback).where(
            QueryFeedback.agent_run_id == agent_run.id,
            QueryFeedback.principal_id == context.principal_id,
        )
    )
    if feedback is not None:
        if _is_positive(feedback.rating):
            memory.positive_feedback_count = max(0, memory.positive_feedback_count - 1)
        else:
            memory.negative_feedback_count = max(0, memory.negative_feedback_count - 1)
    comment_hash = (
        hmac.new(
            settings.audit_hmac_key.encode(),
            body.comment.encode(),
            hashlib.sha256,
        ).hexdigest()
        if body.comment
        else None
    )
    if feedback is None:
        feedback = QueryFeedback(
            organization_id=agent_run.organization_id,
            agent_run_id=agent_run.id,
            principal_id=context.principal_id,
            rating=body.rating,
            comment_hash=comment_hash,
        )
        session.add(feedback)
    else:
        feedback.rating = body.rating
        feedback.comment_hash = comment_hash
    if _is_positive(body.rating):
        memory.positive_feedback_count += 1
    else:
        memory.negative_feedback_count += 1
    memory.status = (
        "SUPPRESSED"
        if memory.negative_feedback_count > 0
        else "ELIGIBLE"
        if memory.positive_feedback_count > 0
        else "OBSERVED"
    )
    await session.flush()
    execution_context = replace(context, organization_id=agent_run.organization_id)
    record_audit(
        session,
        execution_context,
        action="agent.feedback.upsert",
        resource_type="agent_run",
        resource_id=str(agent_run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"rating": body.rating, "memory_status": memory.status},
    )
    record_outbox(
        session,
        organization_id=agent_run.organization_id,
        aggregate_type="query_memory_evidence",
        aggregate_id=str(memory.id),
        event_type="query.feedback.updated.v1",
        payload={
            "memory_evidence_id": str(memory.id),
            "agent_run_id": str(agent_run.id),
            "rating": body.rating,
            "status": memory.status,
        },
    )
    await session.commit()
    return feedback


@router.get("/datasources/{datasource_id}/query-memory", response_model=Page)
async def list_query_memory(
    datasource_id: UUID,
    memory_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "AgentDeveloper", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [QueryMemoryEvidence.datasource_id == datasource.id]
    if memory_status:
        filters.append(QueryMemoryEvidence.status == memory_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(QueryMemoryEvidence).where(*filters)
    )
    rows = (
        await session.scalars(
            select(QueryMemoryEvidence)
            .where(*filters)
            .order_by(QueryMemoryEvidence.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[QueryMemoryEvidenceRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/metadata/tables/{table_id}/impact", response_model=ImpactAnalysisRead)
async def table_impact_analysis(
    table_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "MetadataAdmin", "DataAdmin", "SemanticAdmin", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> ImpactAnalysisRead:
    row = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(MetadataTable.id == table_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="metadata table not found")
    table, schema, catalog = row
    enforce_organization(context, table.organization_id)
    metric_ids = list(
        await session.scalars(
            select(SemanticMetricVersion.id).where(
                SemanticMetricVersion.source_table_id == table.id,
                SemanticMetricVersion.status.in_({"PUBLISHED", "REVIEW_REQUIRED"}),
            )
        )
    )
    name_variants = {
        table.name.lower(),
        f"{schema.name}.{table.name}".lower(),
        f"{catalog.name}.{schema.name}.{table.name}".lower(),
    }
    tool_rows = (
        await session.scalars(
            select(GovernedToolVersion).where(
                GovernedToolVersion.datasource_id == table.datasource_id,
                GovernedToolVersion.status.in_({"PUBLISHED", "REVIEW_REQUIRED"}),
            )
        )
    ).all()
    tool_ids = [
        tool.id
        for tool in tool_rows
        if name_variants.intersection(name.lower() for name in tool.referenced_tables)
    ]
    relationship_ids = list(
        await session.scalars(
            select(RelationshipCandidate.id).where(
                RelationshipCandidate.status == "APPROVED",
                (
                    (RelationshipCandidate.source_table_id == table.id)
                    | (RelationshipCandidate.target_table_id == table.id)
                ),
            )
        )
    )
    dbt_resource_ids = list(
        await session.scalars(
            select(DbtResource.id).where(DbtResource.matched_table_id == table.id)
        )
    )
    return ImpactAnalysisRead(
        table_id=table.id,
        table_name=f"{catalog.name}.{schema.name}.{table.name}",
        semantic_metric_version_ids=metric_ids,
        governed_tool_version_ids=tool_ids,
        approved_relationship_candidate_ids=relationship_ids,
        dbt_resource_ids=dbt_resource_ids,
        downstream_object_count=(
            len(metric_ids) + len(tool_ids) + len(relationship_ids) + len(dbt_resource_ids)
        ),
    )


@router.post(
    "/datasources/{datasource_id}/relationship-candidates/discover",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_relationship_candidates(
    datasource_id: UUID,
    body: RelationshipCandidateDiscoveryRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    tables = (
        await session.scalars(
            select(MetadataTable).where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    table_ids = {table.id for table in tables}
    columns = (
        await session.scalars(
            select(MetadataColumn)
            .where(
                MetadataColumn.table_id.in_(table_ids),
                MetadataColumn.status == "ACTIVE",
            )
            .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
            .limit(settings.relationship_candidate_scan_max_columns)
        )
    ).all()
    columns_by_table_name = {(column.table_id, column.name.lower()): column for column in columns}
    columns_by_name: dict[str, list[MetadataColumn]] = {}
    for column in columns:
        columns_by_name.setdefault(column.name.lower(), []).append(column)
    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.status == "ACTIVE",
            )
        )
    ).all()
    existing_fk_pairs: set[tuple[UUID, UUID]] = set()
    primary_keys: list[MetadataColumn] = []
    for constraint in constraints:
        if constraint.constraint_type == "PRIMARY_KEY":
            for name in constraint.columns:
                primary_key_column = columns_by_table_name.get((constraint.table_id, name.lower()))
                if primary_key_column is not None:
                    primary_keys.append(primary_key_column)
        elif constraint.constraint_type == "FOREIGN_KEY" and constraint.referenced_table_id:
            for source_name, target_name in zip(
                constraint.columns, constraint.referenced_columns, strict=False
            ):
                source = columns_by_table_name.get((constraint.table_id, source_name.lower()))
                target = columns_by_table_name.get(
                    (constraint.referenced_table_id, target_name.lower())
                )
                if source and target:
                    existing_fk_pairs.add((source.id, target.id))
    existing_candidate_pairs = {
        (source_column_id, target_column_id)
        for source_column_id, target_column_id in (
            await session.execute(
                select(
                    RelationshipCandidate.source_column_id,
                    RelationshipCandidate.target_column_id,
                ).where(RelationshipCandidate.datasource_id == datasource.id)
            )
        ).all()
    }
    created: list[RelationshipCandidate] = []
    for target in primary_keys:
        for source in columns_by_name.get(target.name.lower(), []):
            pair = (source.id, target.id)
            if (
                source.table_id == target.table_id
                or source.physical_type.lower() != target.physical_type.lower()
                or pair in existing_fk_pairs
                or pair in existing_candidate_pairs
            ):
                continue
            candidate = RelationshipCandidate(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                target_datasource_id=datasource.id,
                source_table_id=source.table_id,
                source_column_id=source.id,
                target_table_id=target.table_id,
                target_column_id=target.id,
                detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
                confidence=0.90,
                evidence={
                    "column_name_match": "EXACT",
                    "physical_type_match": "EXACT",
                    "target_is_primary_key": True,
                    "source_values_inspected": False,
                },
                created_by=context.principal_id,
            )
            session.add(candidate)
            created.append(candidate)
            existing_candidate_pairs.add(pair)
            if len(created) >= body.max_candidates:
                break
        if len(created) >= body.max_candidates:
            break
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="relationship_candidates.discover",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_candidates": len(created),
            "columns_scanned": len(columns),
            "column_scan_limit": settings.relationship_candidate_scan_max_columns,
            "value_inspection": False,
        },
    )
    await session.commit()
    return Page(
        items=[RelationshipCandidateRead.model_validate(item) for item in created],
        limit=body.max_candidates,
        offset=0,
        total=len(created),
    )


@router.post(
    "/data-domains/{domain_id}/relationship-candidates/discover-cross-source",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_cross_source_relationship_candidates(
    domain_id: UUID,
    body: CrossSourceRelationshipCandidateDiscoveryRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    """Infer column relationships ACROSS datasources within one data_domain --
    or, when `body.target_data_domain_id` is set, across the boundary into a
    second domain, gated by an ACTIVE cross_boundary_grant.

    Relationship inference is free to cross project/datasource boundaries within
    a domain (ADR-0017 SS4/SS8) -- pairing two datasources that both already
    belong to `domain_id` never touches a grant. Pairing against a
    `target_data_domain_id` is different: that crosses a data_domain boundary,
    so it is refused with 403 unless domain_service.check_cross_boundary_grant
    confirms an ACTIVE, unexpired grant lets `domain_id` see into the target
    domain for SUGGESTED_RELATIONSHIP edges -- deny-by-default, never inherited
    (INV-5), exactly like every other cross-boundary read in this platform.

    Bounded like every other discovery path in this platform (ADR-0017 SS8: an
    estate cannot be scanned all at once) -- datasource pairs are capped at
    max_datasource_pairs and candidates at max_candidates, so a domain with many
    datasources is triaged over repeated calls rather than in one unbounded pass.
    """
    domain = await session.get(DataDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, domain.organization_id)
    datasources = list(
        (
            await session.scalars(
                select(DataSource)
                .where(DataSource.data_domain_id == domain.id)
                .order_by(DataSource.name)
            )
        ).all()
    )

    target_domain: DataDomain | None = None
    target_datasources: list[DataSource] = []
    if body.target_data_domain_id is not None:
        if body.target_data_domain_id == domain.id:
            raise HTTPException(
                status_code=422, detail="target_data_domain_id must differ from domain_id"
            )
        target_domain = await session.get(DataDomain, body.target_data_domain_id)
        if target_domain is None or target_domain.organization_id != domain.organization_id:
            raise HTTPException(status_code=422, detail="target_data_domain_id not found")
        allowed = await check_cross_boundary_grant(
            session,
            domain.organization_id,
            target_domain.id,
            domain.id,
            edge_kind="SUGGESTED_RELATIONSHIP",
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "cross-domain access denied: no ACTIVE cross_boundary_grant permits "
                    f"{domain.id} to see into {target_domain.id}"
                ),
            )
        target_datasources = list(
            (
                await session.scalars(
                    select(DataSource)
                    .where(DataSource.data_domain_id == target_domain.id)
                    .order_by(DataSource.name)
                )
            ).all()
        )

    if target_domain is not None:
        # Only cross-boundary pairs -- same-domain pairs on either side stay the
        # job of a same-domain call against that domain's own id.
        pairs = [
            (source_datasource, target_datasource)
            for source_datasource in datasources
            for target_datasource in target_datasources
        ]
    else:
        pairs = list(combinations(datasources, 2))
    pairs_available = len(pairs)
    pairs = pairs[: body.max_datasource_pairs]
    profile_datasources = datasources + target_datasources

    async def _load_source_profile(datasource: DataSource) -> dict[str, Any]:
        tables = (
            await session.scalars(
                select(MetadataTable).where(
                    MetadataTable.datasource_id == datasource.id,
                    MetadataTable.status == "ACTIVE",
                )
            )
        ).all()
        table_ids = {table.id for table in tables}
        columns = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
                .limit(settings.relationship_candidate_scan_max_columns)
            )
        ).all()
        columns_by_table_name = {
            (column.table_id, column.name.lower()): column for column in columns
        }
        # RL-5: keyed by canonical_column_name (case- AND naming-convention-
        # normalized: snake_case, camelCase, PascalCase, SCREAMING_CASE all
        # collapse to the same key), not a bare `.lower()`, so a heterogeneous
        # estate's real naming differences don't hide an otherwise-matching
        # candidate. See aida.relationship_naming.
        columns_by_name: dict[str, list[MetadataColumn]] = {}
        for column in columns:
            columns_by_name.setdefault(canonical_column_name(column.name), []).append(column)
        constraints = (
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.datasource_id == datasource.id,
                    MetadataConstraint.status == "ACTIVE",
                    MetadataConstraint.constraint_type == "PRIMARY_KEY",
                )
            )
        ).all()
        primary_keys: list[MetadataColumn] = []
        for constraint in constraints:
            for name in constraint.columns:
                primary_key_column = columns_by_table_name.get((constraint.table_id, name.lower()))
                if primary_key_column is not None:
                    primary_keys.append(primary_key_column)
        return {
            "columns_by_name": columns_by_name,
            "primary_keys": primary_keys,
            "column_count": len(columns),
        }

    profiles = {
        datasource.id: await _load_source_profile(datasource) for datasource in profile_datasources
    }
    existing_candidate_pairs = {
        (source_column_id, target_column_id)
        for source_column_id, target_column_id in (
            await session.execute(
                select(
                    RelationshipCandidate.source_column_id,
                    RelationshipCandidate.target_column_id,
                )
            )
        ).all()
    }
    created: list[RelationshipCandidate] = []
    columns_scanned = 0
    for ds_a, ds_b in pairs:
        if len(created) >= body.max_candidates:
            break
        profile_a = profiles[ds_a.id]
        profile_b = profiles[ds_b.id]
        columns_scanned += profile_a["column_count"] + profile_b["column_count"]
        # Both directions: PKs in ds_a matched from ds_b's columns, and vice versa.
        for pk_owner, other, other_columns_by_name in (
            (ds_a, ds_b, profile_b["columns_by_name"]),
            (ds_b, ds_a, profile_a["columns_by_name"]),
        ):
            targets = profiles[pk_owner.id]["primary_keys"]
            for target in targets:
                # RL-5: candidates are looked up by canonical name (naming-
                # convention-normalized), not a bare `.lower()`, so
                # `customer_id` / `customerId` / `CustomerId` / `CUSTOMER_ID`
                # across two datasources are recognized as the same key.
                for source in other_columns_by_name.get(canonical_column_name(target.name), []):
                    # RL-5: type compatibility is by family (NUMERIC/STRING/
                    # DATE_TIME/BOOLEAN/BINARY), not literal dialect spelling,
                    # so e.g. Oracle NUMBER(38,0) matches BigQuery INT64.
                    source_type_family = physical_type_family(source.physical_type)
                    target_type_family = physical_type_family(target.physical_type)
                    if source_type_family != target_type_family:
                        continue
                    pair = (source.id, target.id)
                    if pair in existing_candidate_pairs:
                        continue
                    # A literal, dialect-identical match is still the strongest
                    # signal; a canonical/family-only match is real evidence but
                    # weaker, and the reviewer sees exactly which is which
                    # (module 06 SS7 -- evidence must be inspectable, not just a
                    # confidence number) rather than both being reported as
                    # "EXACT" the way the pre-RL-5 code would have.
                    name_is_literal_exact = source.name.lower() == target.name.lower()
                    type_is_literal_exact = (
                        source.physical_type.lower() == target.physical_type.lower()
                    )
                    if name_is_literal_exact and type_is_literal_exact:
                        detection_rule = "EXACT_NAME_TYPE_TO_PRIMARY_KEY_CROSS_SOURCE_V1"
                        confidence = 0.75
                    elif name_is_literal_exact or type_is_literal_exact:
                        detection_rule = "CANONICAL_NAME_TYPE_FAMILY_TO_PRIMARY_KEY_CROSS_SOURCE_V1"
                        confidence = 0.65
                    else:
                        detection_rule = "CANONICAL_NAME_TYPE_FAMILY_TO_PRIMARY_KEY_CROSS_SOURCE_V1"
                        confidence = 0.55
                    candidate = RelationshipCandidate(
                        organization_id=domain.organization_id,
                        datasource_id=other.id,
                        target_datasource_id=pk_owner.id,
                        source_table_id=source.table_id,
                        source_column_id=source.id,
                        target_table_id=target.table_id,
                        target_column_id=target.id,
                        detection_rule=detection_rule,
                        confidence=confidence,
                        evidence={
                            "column_name_match": "EXACT" if name_is_literal_exact else "CANONICAL",
                            "physical_type_match": (
                                "EXACT" if type_is_literal_exact else "FAMILY"
                            ),
                            "physical_type_family": target_type_family,
                            "target_is_primary_key": True,
                            "source_values_inspected": False,
                            "source_datasource": other.name,
                            "target_datasource": pk_owner.name,
                        },
                        created_by=context.principal_id,
                    )
                    session.add(candidate)
                    created.append(candidate)
                    existing_candidate_pairs.add(pair)
                    if len(created) >= body.max_candidates:
                        break
                if len(created) >= body.max_candidates:
                    break
            if len(created) >= body.max_candidates:
                break
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=domain.organization_id),
        action="relationship_candidates.discover_cross_source",
        resource_type="data_domain",
        resource_id=str(domain.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_candidates": len(created),
            "datasource_pairs_scanned": len(pairs),
            "datasource_pairs_available": pairs_available,
            "columns_scanned": columns_scanned,
            "value_inspection": False,
            "target_data_domain_id": str(target_domain.id) if target_domain else None,
        },
    )
    await session.commit()
    return Page(
        items=[RelationshipCandidateRead.model_validate(item) for item in created],
        limit=body.max_candidates,
        offset=0,
        total=len(created),
    )


@router.get("/datasources/{datasource_id}/relationship-candidates", response_model=Page)
async def list_relationship_candidates(
    datasource_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [RelationshipCandidate.datasource_id == datasource.id]
    if candidate_status:
        filters.append(RelationshipCandidate.status == candidate_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(RelationshipCandidate).where(*filters)
    )
    rows = (
        await session.scalars(
            select(RelationshipCandidate)
            .where(*filters)
            .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[RelationshipCandidateRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


def _relationship_candidate_decision_event_type(status: str) -> str:
    """RL-4: `graph_projector.run_projector` listens for exactly these two
    literal event types -- not the old, single consolidated
    `relationship_candidate.decided.v1` -- to trigger `project_unified_lineage`
    (src/aida/projectors/graph_projector.py). Before this fix the emitted name
    and the projector's expected names never matched, so an approved or
    rejected candidate never actually reached Neo4j projection; see
    Docs/30-contracts/04-event-catalog.md's KNOWN_ST14_DRIFT-adjacent note in
    tests/test_event_catalog_gate.py for the rename this replaces.
    """
    return (
        "relationship_candidate.approved.v1"
        if status == "APPROVED"
        else "relationship_candidate.rejected.v1"
    )


def _relationship_candidate_decision_payload(candidate: RelationshipCandidate) -> dict[str, Any]:
    return {
        "candidate_id": str(candidate.id),
        "status": candidate.status,
        # RL-4: `datasource_id` is what
        # graph_projector._event_datasource_id keys on to know which
        # datasource's unified-lineage graph to rebuild; without it,
        # project_unified_lineage silently no-ops even once event_type
        # matches. Same-source candidates project immediately on this event;
        # a cross-source candidate (datasource_id != target_datasource_id)
        # still is not projected per-datasource today -- see the module 06
        # RL-4 report for why that is a separate, larger gap
        # (build_unified_lineage_graph_payload's single-datasource query only
        # ever matches same-datasource candidates; the federated
        # domain-wide graph that does include cross-source edges has no
        # Neo4j projection path at all yet).
        "datasource_id": str(candidate.datasource_id),
        "target_datasource_id": str(candidate.target_datasource_id),
    }


@router.post(
    "/relationship-candidates/{candidate_id}/decision",
    response_model=RelationshipCandidateRead,
)
async def decide_relationship_candidate(
    candidate_id: UUID,
    body: RelationshipCandidateDecision,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> RelationshipCandidate:
    candidate = await session.get(RelationshipCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="relationship candidate not found")
    enforce_organization(context, candidate.organization_id)
    if candidate.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if candidate.status != "PENDING":
        raise HTTPException(status_code=409, detail="relationship candidate is already decided")
    candidate.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    candidate.reviewed_by = context.principal_id
    candidate.review_reason = body.reason
    candidate.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=candidate.organization_id),
        action="relationship_candidate.decide",
        resource_type="relationship_candidate",
        resource_id=str(candidate.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=candidate.organization_id,
        aggregate_type="relationship_candidate",
        aggregate_id=str(candidate.id),
        event_type=_relationship_candidate_decision_event_type(candidate.status),
        payload=_relationship_candidate_decision_payload(candidate),
    )
    await session.commit()
    return candidate


# --------------------------------------------------------------------------------------------
# CT-4: rename detection with steward confirmation
#
# Detection itself is automatic and lives in `aida.workflows.activities.detect_rename_candidates`
# (run inside the same scan that tombstones the old object and creates the new one) -- there is
# no discovery endpoint here because there is nothing for a caller to trigger. What lives here is
# the review surface: list what was proposed, and let a steward confirm or reject it. Approval is
# the ONLY path that reassigns the old object's downstream links (`aida.identity_merge`); this
# heuristic never merges identity on its own (module 04 SS6).
# --------------------------------------------------------------------------------------------


@router.get("/datasources/{datasource_id}/rename-candidates", response_model=Page)
async def list_rename_candidates(
    datasource_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [RenameCandidate.datasource_id == datasource.id]
    if candidate_status:
        filters.append(RenameCandidate.status == candidate_status.upper())
    total = await session.scalar(select(func.count()).select_from(RenameCandidate).where(*filters))
    rows = (
        await session.scalars(
            select(RenameCandidate)
            .where(*filters)
            .order_by(RenameCandidate.confidence.desc(), RenameCandidate.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[RenameCandidateRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/rename-candidates/{candidate_id}/decision",
    response_model=RenameCandidateRead,
)
async def decide_rename_candidate(
    candidate_id: UUID,
    body: RenameCandidateDecision,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> RenameCandidate:
    candidate = await session.get(RenameCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="rename candidate not found")
    enforce_organization(context, candidate.organization_id)
    if candidate.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if candidate.status != "PENDING":
        raise HTTPException(status_code=409, detail="rename candidate is already decided")

    reassigned_links: dict[str, int] | None = None
    if body.decision == "APPROVE":
        # The only place identity actually merges -- explicit steward approval,
        # never automatic (module 04 SS6). Reassigns every downstream reference
        # to the old table's stable ID onto the new one, then marks the old
        # (still-tombstoned) row as superseded so anyone still holding its ID
        # can resolve forward to what it became.
        reassigned_links = await merge_table_identity(
            session, old_table_id=candidate.old_table_id, new_table_id=candidate.new_table_id
        )
        old_table = await session.get(MetadataTable, candidate.old_table_id)
        if old_table is not None:
            old_table.superseded_by_table_id = candidate.new_table_id
        candidate.status = "APPROVED"
        candidate.merged_at = datetime.now(UTC)
    else:
        candidate.status = "REJECTED"
    candidate.reviewed_by = context.principal_id
    candidate.review_reason = body.reason
    candidate.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=candidate.organization_id),
        action="rename_candidate.decide",
        resource_type="rename_candidate",
        resource_id=str(candidate.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision, "reassigned_links": reassigned_links},
    )
    record_outbox(
        session,
        organization_id=candidate.organization_id,
        aggregate_type="rename_candidate",
        aggregate_id=str(candidate.id),
        event_type="rename_candidate.decided.v1",
        payload={"candidate_id": str(candidate.id), "status": candidate.status},
    )
    await session.commit()
    return candidate


# --------------------------------------------------------------------------------------------
# CT-6: cross-source object resolution
#
# The catalog-identity analogue of `discover_cross_source_relationship_candidates` above: same
# domain scoping and cross-boundary grant gate (ADR-0017 SS4), same datasource-pair and candidate
# bounds (ADR-0017 SS8). The question here is "is this table, structurally, the same logical
# asset as that one?" rather than "does this column reference that primary key?", so matching is
# whole-table (`aida.identity_resolution.score_cross_source_match`) rather than column-to-PK.
# Approval only confirms the link -- unlike a rename candidate it never reassigns either table's
# downstream references, because both tables remain distinct catalog objects in distinct estates.
# --------------------------------------------------------------------------------------------


@router.post(
    "/data-domains/{domain_id}/cross-source-object-resolution-candidates/discover",
    response_model=Page,
)
async def discover_cross_source_object_resolution_candidates(
    domain_id: UUID,
    body: CrossSourceObjectResolutionDiscoveryRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    """Propose that a table in one datasource is the same logical asset -- possibly
    renamed or differently cased -- as a table in another datasource, via
    deterministic, metadata-only structural matching. Never sampled or live row
    values (ADR-0014).

    Bounded like every other discovery path in this platform: at most
    `settings.object_resolution_scan_max_tables_per_datasource` ACTIVE tables are
    loaded per datasource, datasource pairs are capped at `max_datasource_pairs`,
    and candidates at `max_candidates`.
    """
    domain = await session.get(DataDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, domain.organization_id)
    datasources = list(
        (
            await session.scalars(
                select(DataSource)
                .where(DataSource.data_domain_id == domain.id)
                .order_by(DataSource.name)
            )
        ).all()
    )

    target_domain: DataDomain | None = None
    target_datasources: list[DataSource] = []
    if body.target_data_domain_id is not None:
        if body.target_data_domain_id == domain.id:
            raise HTTPException(
                status_code=422, detail="target_data_domain_id must differ from domain_id"
            )
        target_domain = await session.get(DataDomain, body.target_data_domain_id)
        if target_domain is None or target_domain.organization_id != domain.organization_id:
            raise HTTPException(status_code=422, detail="target_data_domain_id not found")
        allowed = await check_cross_boundary_grant(
            session,
            domain.organization_id,
            target_domain.id,
            domain.id,
            edge_kind="SUGGESTED_RELATIONSHIP",
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "cross-domain access denied: no ACTIVE cross_boundary_grant permits "
                    f"{domain.id} to see into {target_domain.id}"
                ),
            )
        target_datasources = list(
            (
                await session.scalars(
                    select(DataSource)
                    .where(DataSource.data_domain_id == target_domain.id)
                    .order_by(DataSource.name)
                )
            ).all()
        )

    if target_domain is not None:
        pairs = [
            (source_datasource, target_datasource)
            for source_datasource in datasources
            for target_datasource in target_datasources
        ]
    else:
        pairs = list(combinations(datasources, 2))
    pairs_available = len(pairs)
    pairs = pairs[: body.max_datasource_pairs]
    profile_datasources = datasources + target_datasources

    async def _load_table_profiles(datasource: DataSource) -> list[dict[str, Any]]:
        tables = (
            await session.scalars(
                select(MetadataTable)
                .where(
                    MetadataTable.datasource_id == datasource.id,
                    MetadataTable.status == "ACTIVE",
                )
                .order_by(MetadataTable.name)
                .limit(settings.object_resolution_scan_max_tables_per_datasource)
            )
        ).all()
        if not tables:
            return []
        table_ids = {table.id for table in tables}
        columns = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
            )
        ).all()
        columns_by_table: dict[UUID, list[MetadataColumn]] = {}
        for column in columns:
            columns_by_table.setdefault(column.table_id, []).append(column)
        schemas_by_id = {
            schema.id: schema
            for schema in (
                await session.scalars(
                    select(MetadataSchema).where(
                        MetadataSchema.id.in_({table.schema_id for table in tables})
                    )
                )
            ).all()
        }
        return [
            {
                "table": table,
                "schema_name": schemas_by_id[table.schema_id].name
                if table.schema_id in schemas_by_id
                else "",
                "columns": columns_by_table.get(table.id, []),
            }
            for table in tables
        ]

    profiles = {
        datasource.id: await _load_table_profiles(datasource)
        for datasource in profile_datasources
    }
    existing_candidate_pairs = {
        (source_table_id, target_table_id)
        for source_table_id, target_table_id in (
            await session.execute(
                select(
                    CrossSourceResolutionCandidate.source_table_id,
                    CrossSourceResolutionCandidate.target_table_id,
                )
            )
        ).all()
    }
    created: list[CrossSourceResolutionCandidate] = []
    tables_scanned = 0
    for ds_a, ds_b in pairs:
        if len(created) >= body.max_candidates:
            break
        entries_a = profiles[ds_a.id]
        entries_b = profiles[ds_b.id]
        tables_scanned += len(entries_a) + len(entries_b)
        for entry_a in entries_a:
            for entry_b in entries_b:
                pair = (entry_a["table"].id, entry_b["table"].id)
                if pair in existing_candidate_pairs:
                    continue
                match: IdentityMatch | None = score_cross_source_match(
                    source_schema_name=entry_a["schema_name"],
                    source_table_name=entry_a["table"].name,
                    source_columns=entry_a["columns"],
                    target_schema_name=entry_b["schema_name"],
                    target_table_name=entry_b["table"].name,
                    target_columns=entry_b["columns"],
                    min_confidence=settings.object_resolution_min_confidence,
                )
                if match is None:
                    continue
                candidate = CrossSourceResolutionCandidate(
                    organization_id=domain.organization_id,
                    source_datasource_id=ds_a.id,
                    source_table_id=entry_a["table"].id,
                    target_datasource_id=ds_b.id,
                    target_table_id=entry_b["table"].id,
                    detection_rule=match.detection_rule,
                    confidence=match.confidence,
                    evidence=match.evidence,
                    created_by=context.principal_id,
                )
                session.add(candidate)
                created.append(candidate)
                existing_candidate_pairs.add(pair)
                if len(created) >= body.max_candidates:
                    break
            if len(created) >= body.max_candidates:
                break
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=domain.organization_id),
        action="cross_source_resolution_candidates.discover",
        resource_type="data_domain",
        resource_id=str(domain.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_candidates": len(created),
            "datasource_pairs_scanned": len(pairs),
            "datasource_pairs_available": pairs_available,
            "tables_scanned": tables_scanned,
            "value_inspection": False,
            "target_data_domain_id": str(target_domain.id) if target_domain else None,
        },
    )
    await session.commit()
    return Page(
        items=[CrossSourceResolutionCandidateRead.model_validate(item) for item in created],
        limit=body.max_candidates,
        offset=0,
        total=len(created),
    )


@router.get(
    "/datasources/{datasource_id}/cross-source-object-resolution-candidates",
    response_model=Page,
)
async def list_cross_source_object_resolution_candidates(
    datasource_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [
        or_(
            CrossSourceResolutionCandidate.source_datasource_id == datasource.id,
            CrossSourceResolutionCandidate.target_datasource_id == datasource.id,
        )
    ]
    if candidate_status:
        filters.append(CrossSourceResolutionCandidate.status == candidate_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(CrossSourceResolutionCandidate).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CrossSourceResolutionCandidate)
            .where(*filters)
            .order_by(
                CrossSourceResolutionCandidate.confidence.desc(),
                CrossSourceResolutionCandidate.created_at,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CrossSourceResolutionCandidateRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/cross-source-object-resolution-candidates/{candidate_id}/decision",
    response_model=CrossSourceResolutionCandidateRead,
)
async def decide_cross_source_object_resolution_candidate(
    candidate_id: UUID,
    body: CrossSourceResolutionCandidateDecision,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> CrossSourceResolutionCandidate:
    candidate = await session.get(CrossSourceResolutionCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="cross-source resolution candidate not found")
    enforce_organization(context, candidate.organization_id)
    if candidate.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if candidate.status != "PENDING":
        raise HTTPException(
            status_code=409, detail="cross-source resolution candidate is already decided"
        )
    candidate.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    candidate.reviewed_by = context.principal_id
    candidate.review_reason = body.reason
    candidate.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=candidate.organization_id),
        action="cross_source_resolution_candidate.decide",
        resource_type="cross_source_resolution_candidate",
        resource_id=str(candidate.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=candidate.organization_id,
        aggregate_type="cross_source_resolution_candidate",
        aggregate_id=str(candidate.id),
        event_type="cross_source_resolution_candidate.decided.v1",
        payload={"candidate_id": str(candidate.id), "status": candidate.status},
    )
    await session.commit()
    return candidate


# ---------------------------------------------------------------------------
# RL-6: bulk review for large candidate sets
# ---------------------------------------------------------------------------


async def _resolve_relationship_candidate_bulk_subjects(
    session: AsyncSession,
    *,
    context: SecurityContext,
    candidate_ids: list[UUID] | None,
    selection_filter: RelationshipCandidateBulkSelectionFilter | None,
) -> tuple[list[UUID], Literal["EXPLICIT", "FILTER"], bool]:
    """Resolve the bounded set of candidate ids a bulk decision applies to.

    Mirrors `catalog_bulk_actions`'s explicit-ids-or-filter shape (CT-1): an
    explicit list is deduped and returned as-is (already bounded to
    RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS by the request schema); a
    filter is scoped to one datasource -- validated against the caller's
    organization the same way `discover_relationship_candidates` is -- and
    only ever matches PENDING candidates (an already-decided candidate has no
    business being swept into a bulk decision), ordered by confidence
    descending and capped at the same limit, reporting whether the cap
    actually truncated the match set.
    """
    if candidate_ids is not None:
        return list(dict.fromkeys(candidate_ids)), "EXPLICIT", False
    assert selection_filter is not None
    datasource = await session.get(DataSource, selection_filter.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters: list[ColumnElement[bool]] = [
        RelationshipCandidate.datasource_id == datasource.id,
        RelationshipCandidate.status == "PENDING",
    ]
    if selection_filter.min_confidence is not None:
        filters.append(RelationshipCandidate.confidence >= selection_filter.min_confidence)
    if selection_filter.max_confidence is not None:
        filters.append(RelationshipCandidate.confidence <= selection_filter.max_confidence)
    if selection_filter.detection_rule is not None:
        filters.append(RelationshipCandidate.detection_rule == selection_filter.detection_rule)
    rows = list(
        (
            await session.scalars(
                select(RelationshipCandidate.id)
                .where(*filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.created_at)
                .limit(RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS + 1)
            )
        ).all()
    )
    truncated = len(rows) > RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS
    ids = rows[:RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS]
    if not ids:
        raise HTTPException(
            status_code=409, detail="filter matched no PENDING relationship candidates"
        )
    return ids, "FILTER", truncated


@router.post(
    "/relationship-candidates/bulk-decision",
    response_model=RelationshipCandidateBulkDecisionResultRead,
)
async def bulk_decide_relationship_candidates(
    body: RelationshipCandidateBulkDecisionRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> RelationshipCandidateBulkDecisionResultRead:
    """RL-6: decide up to RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS PENDING
    relationship candidates in one call, by explicit id list or by a
    single-datasource filter.

    Exactly the same maker-checker and PENDING-only rules as
    `decide_relationship_candidate` apply per candidate -- this is not a
    looser bulk path, just a batched one -- but a rule violation on one
    candidate marks that candidate FAILED and continues rather than aborting
    the whole request (CT-1's partial-success precedent), since a reviewer
    triaging hundreds of candidates should not have the entire batch rejected
    for one candidate that happens to be their own or already decided.
    """
    subject_ids, selection_mode, truncated = await _resolve_relationship_candidate_bulk_subjects(
        session,
        context=context,
        candidate_ids=body.candidate_ids,
        selection_filter=body.filter,
    )
    candidates = {
        row.id: row
        for row in (
            await session.scalars(
                select(RelationshipCandidate).where(RelationshipCandidate.id.in_(subject_ids))
            )
        ).all()
    }
    new_status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    now = datetime.now(UTC)
    results: list[RelationshipCandidateBulkDecisionItemRead] = []
    succeeded = 0
    for candidate_id in subject_ids:
        candidate = candidates.get(candidate_id)
        if candidate is None:
            results.append(
                RelationshipCandidateBulkDecisionItemRead(
                    candidate_id=str(candidate_id),
                    status="FAILED",
                    reason="relationship candidate not found",
                )
            )
            continue
        try:
            enforce_organization(context, candidate.organization_id)
        except HTTPException:
            results.append(
                RelationshipCandidateBulkDecisionItemRead(
                    candidate_id=str(candidate_id),
                    status="FAILED",
                    reason="cross-organization access denied",
                )
            )
            continue
        if candidate.created_by == context.principal_id:
            results.append(
                RelationshipCandidateBulkDecisionItemRead(
                    candidate_id=str(candidate_id),
                    status="FAILED",
                    reason="maker cannot review their own candidate",
                )
            )
            continue
        if candidate.status != "PENDING":
            results.append(
                RelationshipCandidateBulkDecisionItemRead(
                    candidate_id=str(candidate_id),
                    status="FAILED",
                    reason=f"relationship candidate is already {candidate.status.lower()}",
                )
            )
            continue
        candidate.status = new_status
        candidate.reviewed_by = context.principal_id
        candidate.review_reason = body.reason
        candidate.reviewed_at = now
        record_outbox(
            session,
            organization_id=candidate.organization_id,
            aggregate_type="relationship_candidate",
            aggregate_id=str(candidate.id),
            event_type=_relationship_candidate_decision_event_type(candidate.status),
            payload=_relationship_candidate_decision_payload(candidate),
        )
        results.append(
            RelationshipCandidateBulkDecisionItemRead(
                candidate_id=str(candidate_id), status="SUCCEEDED", reason=None
            )
        )
        succeeded += 1
    failed = len(results) - succeeded
    outcome = "SUCCESS" if not failed else "PARTIAL_SUCCESS" if succeeded else "FAILURE"
    record_audit(
        session,
        context,
        action="relationship_candidate.bulk_decide",
        resource_type="relationship_candidate",
        resource_id=None,
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "decision": body.decision,
            "selection_mode": selection_mode,
            "requested_count": len(results),
            "succeeded_count": succeeded,
            "failed_count": failed,
            "truncated": truncated,
        },
    )
    await session.commit()
    return RelationshipCandidateBulkDecisionResultRead(
        decision=body.decision,
        selection_mode=selection_mode,
        requested_count=len(results),
        succeeded_count=succeeded,
        failed_count=failed,
        truncated=truncated,
        results=results,
    )


# ---------------------------------------------------------------------------
# RL-7: confidence calibration against real decision history
# ---------------------------------------------------------------------------

_CALIBRATION_METHODOLOGY_NOTE = (
    "Buckets are the observed steward approval rate from THIS organization's own "
    "decision history (or a RelationshipCandidateGroundTruthLabel override where one "
    "exists), not a published calibration curve against a labelled banking corpus -- "
    "no such corpus exists in this environment (module 06 RL-7). A steward's "
    "APPROVE/REJECT is legitimate first-form ground truth, but it is this deployment's "
    "own history, generated by this deployment's own detection rules; treat the rates "
    "below as a diagnostic on whether confidence tracks reviewer agreement here, not as "
    "an externally-validated accuracy claim."
)


@router.get(
    "/relationship-candidates/confidence-calibration",
    response_model=RelationshipCandidateCalibrationRead,
)
async def get_relationship_candidate_confidence_calibration(
    datasource_id: UUID | None = Query(default=None),
    bucket_width: float = Query(default=0.1, gt=0.0, le=1.0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> RelationshipCandidateCalibrationRead:
    """RL-7: bucket DECIDED relationship candidates by confidence and report the
    observed approval rate per bucket, from this organization's real
    maker-checker decision history.

    This is explicitly NOT a published calibration curve against a labelled
    banking corpus -- see `_CALIBRATION_METHODOLOGY_NOTE`, echoed verbatim in
    the response so a caller cannot mistake this for one. Where a
    `RelationshipCandidateGroundTruthLabel` exists for a decided candidate
    (RL-7's optional additive extra -- a later, stronger signal than the
    original steward call, e.g. a labelled corpus or a usage confirmation),
    that label supersedes the candidate's own `status` for this calculation
    only; `RelationshipCandidate.status` itself, and the steward decision it
    records, is never touched.
    """
    if datasource_id is not None:
        datasource = await session.get(DataSource, datasource_id)
        if datasource is None:
            raise HTTPException(status_code=404, detail="datasource not found")
        enforce_organization(context, datasource.organization_id)
        base_filters: list[ColumnElement[bool]] = [
            RelationshipCandidate.organization_id == context.organization_id,
            RelationshipCandidate.datasource_id == datasource_id,
        ]
    else:
        base_filters = [RelationshipCandidate.organization_id == context.organization_id]

    decided = (
        await session.scalars(
            select(RelationshipCandidate).where(
                *base_filters,
                RelationshipCandidate.status.in_(("APPROVED", "REJECTED")),
            )
        )
    ).all()

    ground_truth_by_candidate: dict[UUID, str] = {}
    if decided:
        candidate_ids = [candidate.id for candidate in decided]
        ground_truth_rows = (
            await session.scalars(
                select(RelationshipCandidateGroundTruthLabel).where(
                    RelationshipCandidateGroundTruthLabel.candidate_id.in_(candidate_ids)
                )
            )
        ).all()
        ground_truth_by_candidate = {
            row.candidate_id: row.label for row in ground_truth_rows
        }

    bucket_count = max(1, math.ceil(1.0 / bucket_width))
    bucket_totals = [0] * bucket_count
    bucket_approved = [0] * bucket_count

    def _bucket_index(confidence: float) -> int:
        clamped = min(max(confidence, 0.0), 1.0)
        index = int(clamped / bucket_width)
        return min(index, bucket_count - 1)

    for candidate in decided:
        effective_label = ground_truth_by_candidate.get(candidate.id, candidate.status)
        index = _bucket_index(candidate.confidence)
        bucket_totals[index] += 1
        if effective_label == "APPROVED":
            bucket_approved[index] += 1

    buckets = [
        RelationshipCandidateCalibrationBucketRead(
            confidence_low=round(index * bucket_width, 10),
            confidence_high=round(min((index + 1) * bucket_width, 1.0), 10),
            decided_count=bucket_totals[index],
            approved_count=bucket_approved[index],
            rejected_count=bucket_totals[index] - bucket_approved[index],
            observed_approval_rate=(
                bucket_approved[index] / bucket_totals[index]
                if bucket_totals[index]
                else None
            ),
        )
        for index in range(bucket_count)
    ]
    return RelationshipCandidateCalibrationRead(
        datasource_id=datasource_id,
        bucket_width=bucket_width,
        total_decided=len(decided),
        ground_truth_overrides_applied=sum(
            1
            for candidate in decided
            if candidate.id in ground_truth_by_candidate
            and ground_truth_by_candidate[candidate.id] != candidate.status
        ),
        buckets=buckets,
        methodology_note=_CALIBRATION_METHODOLOGY_NOTE,
    )


# --------------------------------------------------------------------------
# RL-2 / RL-3 shared helpers
#
# These fetch rows and hand plain, value-free data to ``aida.relationship_intelligence``
# (pure, unit-tested resolution/generation logic), then persist and shape the
# result. No source values are read at any point (ADR-0014). RL-1 (table
# family detection) is not here -- see ``aida.table_family_api`` and
# ``aida.table_family_intelligence``.
# --------------------------------------------------------------------------


async def _qualified_names(session: AsyncSession, table_ids: set[UUID]) -> dict[UUID, str]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataTable.name, MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(MetadataTable.id.in_(table_ids))
        )
    ).all()
    return {
        table_id: f"{catalog_name}.{schema_name}.{table_name}"
        for table_id, table_name, schema_name, catalog_name in rows
    }


async def _latest_table_profiles(
    session: AsyncSession, datasource_id: UUID, table_ids: set[UUID]
) -> dict[UUID, TableProfile]:
    if not table_ids:
        return {}
    ranked = (
        select(
            TableProfile,
            func.row_number()
            .over(partition_by=TableProfile.table_id, order_by=TableProfile.created_at.desc())
            .label("rn"),
        )
        .where(TableProfile.datasource_id == datasource_id, TableProfile.table_id.in_(table_ids))
        .subquery()
    )
    alias = aliased(TableProfile, ranked)
    rows = (await session.scalars(select(alias).where(ranked.c.rn == 1))).all()
    return {profile.table_id: profile for profile in rows}


async def _column_profile_stats(
    session: AsyncSession, table_profile_ids: list[UUID]
) -> dict[UUID, ColumnProfile]:
    if not table_profile_ids:
        return {}
    rows = (
        await session.scalars(
            select(ColumnProfile).where(ColumnProfile.table_profile_id.in_(table_profile_ids))
        )
    ).all()
    return {row.column_id: row for row in rows}


def _composite_keys_from_constraints(
    constraints: list[MetadataConstraint], table_ids: set[UUID]
) -> tuple[
    dict[UUID, tuple[str, ...]],
    frozenset[tuple[UUID, tuple[str, ...], UUID, tuple[str, ...]]],
]:
    composite_primary_keys: dict[UUID, tuple[str, ...]] = {}
    declared_composite_fks: set[tuple[UUID, tuple[str, ...], UUID, tuple[str, ...]]] = set()
    for constraint in constraints:
        if (
            constraint.constraint_type == "PRIMARY_KEY"
            and len(constraint.columns) >= 2
            and constraint.table_id in table_ids
        ):
            composite_primary_keys[constraint.table_id] = tuple(constraint.columns)
        elif (
            constraint.constraint_type == "FOREIGN_KEY"
            and constraint.referenced_table_id is not None
            and len(constraint.columns) >= 2
        ):
            declared_composite_fks.add(
                (
                    constraint.table_id,
                    tuple(name.lower() for name in constraint.columns),
                    constraint.referenced_table_id,
                    tuple(name.lower() for name in constraint.referenced_columns),
                )
            )
    return composite_primary_keys, frozenset(declared_composite_fks)


async def _build_canonical_read(
    session: AsyncSession, mapping: CanonicalTableMapping
) -> CanonicalTableMappingRead:
    qualified = await _qualified_names(session, {mapping.canonical_table_id})
    return CanonicalTableMappingRead(
        id=mapping.id,
        organization_id=mapping.organization_id,
        family_candidate_id=mapping.family_candidate_id,
        canonical_table_id=mapping.canonical_table_id,
        canonical_qualified_name=qualified.get(mapping.canonical_table_id, ""),
        resolved_by=mapping.resolved_by,
        rationale=mapping.rationale,
        is_steward_override=mapping.is_steward_override,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


async def _build_composite_read(
    session: AsyncSession, group: RelationshipCandidateGroup
) -> CompositeRelationshipCandidateRead:
    members = (
        await session.scalars(
            select(RelationshipCandidateGroupMember)
            .where(RelationshipCandidateGroupMember.group_id == group.id)
            .order_by(RelationshipCandidateGroupMember.ordinal)
        )
    ).all()
    column_ids = {member.source_column_id for member in members} | {
        member.target_column_id for member in members
    }
    columns = (
        (await session.scalars(select(MetadataColumn).where(MetadataColumn.id.in_(column_ids))))
        .all()
        if column_ids
        else []
    )
    names = {column.id: column.name for column in columns}
    return CompositeRelationshipCandidateRead(
        id=group.id,
        organization_id=group.organization_id,
        datasource_id=group.datasource_id,
        source_table_id=group.source_table_id,
        target_table_id=group.target_table_id,
        detection_rule=group.detection_rule,
        confidence=group.confidence,
        evidence=group.evidence,
        status=group.status,
        created_by=group.created_by,
        reviewed_by=group.reviewed_by,
        review_reason=group.review_reason,
        reviewed_at=group.reviewed_at,
        members=[
            CompositeRelationshipCandidateMemberRead(
                ordinal=member.ordinal,
                source_column_id=member.source_column_id,
                target_column_id=member.target_column_id,
                source_column_name=names.get(member.source_column_id, ""),
                target_column_name=names.get(member.target_column_id, ""),
            )
            for member in members
        ],
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


# --------------------------------------------------------------------------
# RL-2 -- canonical table resolution with steward override
#
# Table family detection/review (RL-1) lives entirely in
# ``aida.table_family_api`` / ``aida.table_family_intelligence``, backed by
# ``TableFamilyCandidate``. What follows only resolves, given an APPROVED
# family, which member is canonical -- filling the one gap that model leaves
# open (no steward override mechanism, and ``base_table_id`` is explicitly
# never set for a SNAPSHOT family).
# --------------------------------------------------------------------------

_CANONICAL_OVERRIDE_ROLES = ("PlatformAdmin", "MetadataReviewer", "DataSteward")


async def _find_approved_family_for_table(
    session: AsyncSession, datasource_id: UUID, table_id: UUID
) -> TableFamilyCandidate | None:
    """The APPROVED ``TableFamilyCandidate`` (if any) that ``table_id`` belongs to.

    ``member_table_ids`` is a plain JSON list of stringified ids (not a JSONB
    column with a containment operator to lean on), so this scans the
    (small, per-datasource) set of APPROVED candidates and matches in Python
    -- the same approach ``aida.table_family_api._existing_member_keys``
    already uses for the analogous re-detection dedupe check.
    """
    candidates = (
        await session.scalars(
            select(TableFamilyCandidate).where(
                TableFamilyCandidate.datasource_id == datasource_id,
                TableFamilyCandidate.status == "APPROVED",
            )
        )
    ).all()
    target = str(table_id)
    for candidate in candidates:
        if target in candidate.member_table_ids:
            return candidate
    return None


@router.get(
    "/datasources/{datasource_id}/canonical-table/resolve",
    response_model=TableRef | None,
)
async def resolve_canonical_table(
    datasource_id: UUID,
    table_id: UUID = Query(...),
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> TableRef | None:
    """``resolve_canonical(scope, entity_ref)`` from the module 06 public interface.

    ``entity_ref`` is any table belonging to the family. Resolution order:
    an explicit steward override (``CanonicalTableMapping``), else the
    family's own ``base_table_id``, else ``None`` -- e.g. an un-overridden
    SNAPSHOT family, where the algorithm never names a "current" member.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None or table.datasource_id != datasource_id:
        raise HTTPException(status_code=404, detail="metadata table not found")
    enforce_organization(context, table.organization_id)
    family = await _find_approved_family_for_table(session, datasource_id, table_id)
    if family is None:
        return None
    mapping = await session.scalar(
        select(CanonicalTableMapping).where(
            CanonicalTableMapping.family_candidate_id == family.id
        )
    )
    effective_id = resolve_canonical_table_id(
        base_table_id=family.base_table_id,
        steward_override_table_id=mapping.canonical_table_id if mapping else None,
    )
    if effective_id is None:
        return None
    qualified = await _qualified_names(session, {effective_id})
    return TableRef(table_id=effective_id, qualified_name=qualified.get(effective_id, ""))


@router.get(
    "/table-family-candidates/{family_candidate_id}/canonical",
    response_model=CanonicalTableMappingRead | None,
)
async def get_canonical_mapping(
    family_candidate_id: UUID,
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CanonicalTableMappingRead | None:
    family = await session.get(TableFamilyCandidate, family_candidate_id)
    if family is None:
        raise HTTPException(status_code=404, detail="table family candidate not found")
    enforce_organization(context, family.organization_id)
    mapping = await session.scalar(
        select(CanonicalTableMapping).where(
            CanonicalTableMapping.family_candidate_id == family_candidate_id
        )
    )
    if mapping is None:
        return None
    return await _build_canonical_read(session, mapping)


@router.post(
    "/table-family-candidates/{family_candidate_id}/canonical/override",
    response_model=CanonicalTableMappingRead | None,
)
async def override_canonical_table(
    family_candidate_id: UUID,
    body: CanonicalTableOverrideRequest,
    context: SecurityContext = Depends(require_roles(*_CANONICAL_OVERRIDE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CanonicalTableMappingRead | None:
    """Steward decision naming (or clearing) the canonical member (maker-checker).

    Only accepted against an APPROVED ``TableFamilyCandidate`` -- a family
    still under review, or rejected, has no canonical member to name yet.
    ``table_id=None`` clears an existing override (deleting the mapping row)
    and reverts resolution to the family's own ``base_table_id``; a
    rationale is always required, including to clear, since either is an
    auditable decision.
    """
    family = await session.get(TableFamilyCandidate, family_candidate_id)
    if family is None:
        raise HTTPException(status_code=404, detail="table family candidate not found")
    enforce_organization(context, family.organization_id)
    if family.status != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail="canonical table can only be set for an APPROVED table family candidate",
        )

    mapping = await session.scalar(
        select(CanonicalTableMapping).where(
            CanonicalTableMapping.family_candidate_id == family_candidate_id
        )
    )

    if body.table_id is None:
        if mapping is not None:
            await session.delete(mapping)
            await session.flush()
        record_audit(
            session,
            replace(context, organization_id=family.organization_id),
            action="canonical_table.override_cleared",
            resource_type="table_family_candidate",
            resource_id=str(family.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"rationale": body.rationale},
        )
        record_outbox(
            session,
            organization_id=family.organization_id,
            aggregate_type="table_family_candidate",
            aggregate_id=str(family.id),
            event_type="canonical_table.resolved.v1",
            payload={
                "family_candidate_id": str(family.id),
                "canonical_table_id": (
                    str(family.base_table_id) if family.base_table_id else None
                ),
                "steward_override": False,
            },
        )
        await session.commit()
        return None

    if str(body.table_id) not in family.member_table_ids:
        raise HTTPException(
            status_code=409,
            detail="override target is not a member of this table family",
        )

    if mapping is None:
        mapping = CanonicalTableMapping(
            organization_id=family.organization_id,
            family_candidate_id=family.id,
            canonical_table_id=body.table_id,
            resolved_by=context.principal_id,
            rationale=body.rationale,
            is_steward_override=True,
        )
        session.add(mapping)
    else:
        mapping.canonical_table_id = body.table_id
        mapping.resolved_by = context.principal_id
        mapping.rationale = body.rationale
        mapping.is_steward_override = True
    await session.flush()

    record_audit(
        session,
        replace(context, organization_id=family.organization_id),
        action="canonical_table.override",
        resource_type="canonical_table_mapping",
        resource_id=str(mapping.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"canonical_table_id": str(body.table_id)},
    )
    record_outbox(
        session,
        organization_id=family.organization_id,
        aggregate_type="canonical_table_mapping",
        aggregate_id=str(mapping.id),
        event_type="canonical_table.resolved.v1",
        payload={
            "family_candidate_id": str(family.id),
            "canonical_table_id": str(mapping.canonical_table_id),
            "steward_override": True,
        },
    )
    result = await _build_canonical_read(session, mapping)
    await session.commit()
    return result

# --------------------------------------------------------------------------
# RL-3 -- composite (multi-column) relationship candidates
# --------------------------------------------------------------------------


@router.post(
    "/datasources/{datasource_id}/relationship-candidates/discover-composite",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_composite_relationship_candidates(
    datasource_id: UUID,
    body: CompositeRelationshipCandidateDiscoveryRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    """Propose bounded, evidence-backed composite (multi-column) FK-like candidates.

    Single-column candidates keep using ``RelationshipCandidate`` unchanged;
    this extends generation with the ordered-column-set shape a composite key
    needs, following the same declared-constraints-are-facts pruning order.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)

    tables = (
        await session.scalars(
            select(MetadataTable).where(
                MetadataTable.datasource_id == datasource.id, MetadataTable.status == "ACTIVE"
            )
        )
    ).all()
    table_ids = {table.id for table in tables}
    columns = (
        await session.scalars(
            select(MetadataColumn)
            .where(MetadataColumn.table_id.in_(table_ids), MetadataColumn.status == "ACTIVE")
            .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
            .limit(settings.relationship_candidate_scan_max_columns)
        )
    ).all()
    columns_by_table_id: dict[UUID, list[MetadataColumn]] = {}
    for column in columns:
        columns_by_table_id.setdefault(column.table_id, []).append(column)

    profiles_by_table = await _latest_table_profiles(session, datasource.id, table_ids)
    column_profile_by_column = await _column_profile_stats(
        session, [profile.id for profile in profiles_by_table.values()]
    )
    columns_meta_by_table: dict[UUID, tuple[ColumnMeta, ...]] = {
        table_id: tuple(
            ColumnMeta(
                id=col.id,
                table_id=col.table_id,
                name=col.name,
                physical_type=col.physical_type,
                nullable=col.nullable,
                ordinal_position=col.ordinal_position,
                null_count=(
                    column_profile_by_column[col.id].null_count
                    if col.id in column_profile_by_column
                    else None
                ),
                non_null_count=(
                    column_profile_by_column[col.id].non_null_count
                    if col.id in column_profile_by_column
                    else None
                ),
                approximate_distinct_count=(
                    column_profile_by_column[col.id].approximate_distinct_count
                    if col.id in column_profile_by_column
                    else None
                ),
            )
            for col in cols
        )
        for table_id, cols in columns_by_table_id.items()
    }

    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.status == "ACTIVE",
            )
        )
    ).all()
    composite_primary_keys, declared_composite_fks = _composite_keys_from_constraints(
        list(constraints), table_ids
    )
    existing_fingerprints = frozenset(
        await session.scalars(
            select(RelationshipCandidateGroup.member_fingerprint).where(
                RelationshipCandidateGroup.datasource_id == datasource.id
            )
        )
    )

    generated = generate_composite_relationship_candidates(
        columns_by_table=columns_meta_by_table,
        composite_primary_keys=composite_primary_keys,
        declared_composite_foreign_keys=declared_composite_fks,
        existing_fingerprints=existing_fingerprints,
        max_group_columns=settings.relationship_candidate_composite_max_columns,
        max_candidates_per_table=settings.relationship_candidate_composite_max_per_table,
    )[: body.max_candidates]

    created_groups: list[RelationshipCandidateGroup] = []
    for candidate in generated:
        group = RelationshipCandidateGroup(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table_id=candidate.source_table_id,
            target_table_id=candidate.target_table_id,
            member_fingerprint=candidate.fingerprint,
            member_count=len(candidate.members),
            detection_rule=candidate.detection_rule,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            created_by=context.principal_id,
        )
        session.add(group)
        await session.flush()
        for pair in candidate.members:
            session.add(
                RelationshipCandidateGroupMember(
                    group_id=group.id,
                    ordinal=pair.ordinal,
                    source_column_id=pair.source_column_id,
                    target_column_id=pair.target_column_id,
                )
            )
        created_groups.append(group)
    await session.flush()

    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="composite_relationship_candidates.discover",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_candidates": len(created_groups),
            "columns_scanned": len(columns),
            "column_scan_limit": settings.relationship_candidate_scan_max_columns,
            "max_group_columns": settings.relationship_candidate_composite_max_columns,
            "value_inspection": False,
        },
    )
    await session.commit()

    items = [await _build_composite_read(session, group) for group in created_groups]
    return Page(items=items, limit=body.max_candidates, offset=0, total=len(items))


@router.get(
    "/datasources/{datasource_id}/relationship-candidates/composite",
    response_model=Page,
)
async def list_composite_relationship_candidates(
    datasource_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [RelationshipCandidateGroup.datasource_id == datasource.id]
    if candidate_status:
        filters.append(RelationshipCandidateGroup.status == candidate_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(RelationshipCandidateGroup).where(*filters)
    )
    rows = (
        await session.scalars(
            select(RelationshipCandidateGroup)
            .where(*filters)
            .order_by(
                RelationshipCandidateGroup.confidence.desc(),
                RelationshipCandidateGroup.created_at,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[await _build_composite_read(session, row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/composite-relationship-candidates/{group_id}/decision",
    response_model=CompositeRelationshipCandidateRead,
)
async def decide_composite_relationship_candidate(
    group_id: UUID,
    body: RelationshipCandidateDecision,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> CompositeRelationshipCandidateRead:
    group = await session.get(RelationshipCandidateGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="composite relationship candidate not found")
    enforce_organization(context, group.organization_id)
    if group.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if group.status != "PENDING":
        raise HTTPException(
            status_code=409, detail="composite relationship candidate is already decided"
        )
    group.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    group.reviewed_by = context.principal_id
    group.review_reason = body.reason
    group.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=group.organization_id),
        action="composite_relationship_candidate.decide",
        resource_type="relationship_candidate_group",
        resource_id=str(group.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=group.organization_id,
        aggregate_type="relationship_candidate_group",
        aggregate_id=str(group.id),
        event_type="composite_relationship_candidate.decided.v1",
        payload={"group_id": str(group.id), "status": group.status},
    )
    await session.flush()
    result = await _build_composite_read(session, group)
    await session.commit()
    return result
