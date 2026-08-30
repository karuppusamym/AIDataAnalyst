import hashlib
import hmac
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.domain_service import check_cross_boundary_grant
from aida.events import record_audit, record_outbox
from aida.key_inference import ColumnStat, infer_key_candidates, key_fingerprint
from aida.knowledge_graph import GraphDirection, GraphLink, expand_frontier
from aida.models import (
    AgentRun,
    ColumnProfile,
    DataDomain,
    DataSource,
    DbtResource,
    GovernedToolVersion,
    KeyInferenceCandidate,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    QueryExecution,
    QueryFeedback,
    QueryMemoryEvidence,
    RelationshipCandidate,
    SemanticMetricVersion,
    TableProfile,
)
from aida.schemas import (
    CrossSourceRelationshipCandidateDiscoveryRequest,
    GraphEdgeRead,
    GraphNodeRead,
    GraphSearchRead,
    ImpactAnalysisRead,
    KeyInferenceCandidateDecision,
    KeyInferenceCandidateRead,
    KeyInferenceDiscoveryRequest,
    KnowledgeGraphRead,
    Page,
    QueryFeedbackRead,
    QueryFeedbackUpsert,
    QueryMemoryEvidenceRead,
    RelationshipCandidateDecision,
    RelationshipCandidateDiscoveryRequest,
    RelationshipCandidateRead,
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
        columns_by_name: dict[str, list[MetadataColumn]] = {}
        for column in columns:
            columns_by_name.setdefault(column.name.lower(), []).append(column)
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
                for source in other_columns_by_name.get(target.name.lower(), []):
                    if source.physical_type.lower() != target.physical_type.lower():
                        continue
                    pair = (source.id, target.id)
                    if pair in existing_candidate_pairs:
                        continue
                    candidate = RelationshipCandidate(
                        organization_id=domain.organization_id,
                        datasource_id=other.id,
                        target_datasource_id=pk_owner.id,
                        source_table_id=source.table_id,
                        source_column_id=source.id,
                        target_table_id=target.table_id,
                        target_column_id=target.id,
                        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_CROSS_SOURCE_V1",
                        confidence=0.75,
                        evidence={
                            "column_name_match": "EXACT",
                            "physical_type_match": "EXACT",
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
        event_type="relationship_candidate.decided.v1",
        payload={"candidate_id": str(candidate.id), "status": candidate.status},
    )
    await session.commit()
    return candidate


@router.post(
    "/datasources/{datasource_id}/key-candidates/discover",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_key_candidates(
    datasource_id: UUID,
    body: KeyInferenceDiscoveryRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Propose single-column and bounded 2-4 column composite keys (module 05
    sec 13, PR-1) from already-computed, value-free ``table_profile`` /
    ``column_profile`` statistics -- see ``aida.key_inference`` for the
    independence-upper-bound method behind composite candidates.

    Candidates land ``PENDING`` and require a maker-checker decision through
    ``POST /v1/key-candidates/{id}/decision`` before anything treats a column
    (set) as a key -- this endpoint only ever proposes, it never accepts.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)

    profile_filters = [
        TableProfile.datasource_id == datasource.id,
        TableProfile.status == "COMPLETED",
    ]
    if body.table_id is not None:
        profile_filters.append(TableProfile.table_id == body.table_id)
    profile_rows = (
        await session.scalars(
            select(TableProfile)
            .where(*profile_filters)
            .order_by(TableProfile.table_id, TableProfile.created_at.desc())
        )
    ).all()
    latest_profile_by_table: dict[UUID, TableProfile] = {}
    for profile in profile_rows:
        latest_profile_by_table.setdefault(profile.table_id, profile)
    if not latest_profile_by_table:
        raise HTTPException(status_code=409, detail="no completed table profile available")

    created: list[KeyInferenceCandidate] = []
    for table_id, table_profile in latest_profile_by_table.items():
        if len(created) >= body.max_candidates:
            break
        column_rows = (
            await session.execute(
                select(ColumnProfile, MetadataColumn)
                .join(MetadataColumn, MetadataColumn.id == ColumnProfile.column_id)
                .where(ColumnProfile.table_profile_id == table_profile.id)
            )
        ).all()
        if not column_rows:
            continue
        stats = [
            ColumnStat(
                column_id=column.id,
                column_name=column.name,
                null_count=column_profile.null_count,
                non_null_count=column_profile.non_null_count,
                approximate_distinct_count=column_profile.approximate_distinct_count,
            )
            for column_profile, column in column_rows
        ]
        columns_by_lower_name = {column.name.lower(): column for _, column in column_rows}
        constraints = (
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.table_id == table_id,
                    MetadataConstraint.constraint_type == "PRIMARY_KEY",
                    MetadataConstraint.status == "ACTIVE",
                )
            )
        ).all()
        declared_primary_keys: set[frozenset[UUID]] = set()
        for constraint in constraints:
            declared_columns = frozenset(
                columns_by_lower_name[name.lower()].id
                for name in constraint.columns
                if name.lower() in columns_by_lower_name
            )
            if declared_columns:
                declared_primary_keys.add(declared_columns)
        candidates = infer_key_candidates(
            stats,
            table_profile.row_count_estimate or 0,
            min_ratio=body.min_ratio,
            declared_primary_key_column_ids=frozenset(declared_primary_keys),
            max_candidates=body.max_candidates - len(created),
        )
        if not candidates:
            continue
        existing_fingerprints = set(
            await session.scalars(
                select(KeyInferenceCandidate.key_fingerprint).where(
                    KeyInferenceCandidate.table_id == table_id
                )
            )
        )
        for candidate in candidates:
            fp = key_fingerprint(candidate.column_ids)
            if fp in existing_fingerprints:
                continue
            row = KeyInferenceCandidate(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                table_id=table_id,
                table_profile_id=table_profile.id,
                column_ids=[str(column_id) for column_id in candidate.column_ids],
                column_names=list(candidate.column_names),
                column_count=candidate.column_count,
                key_fingerprint=fp,
                detection_rule=candidate.detection_rule,
                confidence=candidate.confidence,
                estimated_distinctness_ratio=candidate.estimated_distinctness_ratio,
                evidence=candidate.evidence,
                created_by=context.principal_id,
            )
            session.add(row)
            created.append(row)
            existing_fingerprints.add(fp)
            if len(created) >= body.max_candidates:
                break
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="key_candidates.discover",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_candidates": len(created),
            "tables_scanned": len(latest_profile_by_table),
            "value_inspection": False,
        },
    )
    await session.commit()
    return Page(
        items=[KeyInferenceCandidateRead.model_validate(item) for item in created],
        limit=body.max_candidates,
        offset=0,
        total=len(created),
    )


@router.get("/datasources/{datasource_id}/key-candidates", response_model=Page)
async def list_key_candidates(
    datasource_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
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
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [KeyInferenceCandidate.datasource_id == datasource.id]
    if candidate_status:
        filters.append(KeyInferenceCandidate.status == candidate_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(KeyInferenceCandidate).where(*filters)
    )
    rows = (
        await session.scalars(
            select(KeyInferenceCandidate)
            .where(*filters)
            .order_by(KeyInferenceCandidate.confidence.desc(), KeyInferenceCandidate.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[KeyInferenceCandidateRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/key-candidates/{candidate_id}/decision",
    response_model=KeyInferenceCandidateRead,
)
async def decide_key_candidate(
    candidate_id: UUID,
    body: KeyInferenceCandidateDecision,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> KeyInferenceCandidate:
    candidate = await session.get(KeyInferenceCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="key candidate not found")
    enforce_organization(context, candidate.organization_id)
    if candidate.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if candidate.status != "PENDING":
        raise HTTPException(status_code=409, detail="key candidate is already decided")
    candidate.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    candidate.reviewed_by = context.principal_id
    candidate.review_reason = body.reason
    candidate.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=candidate.organization_id),
        action="key_candidate.decide",
        resource_type="key_inference_candidate",
        resource_id=str(candidate.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    await session.commit()
    return candidate
