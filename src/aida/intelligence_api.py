import hashlib
import hmac
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.knowledge_graph import GraphDirection, GraphLink, expand_frontier
from aida.models import (
    AgentRun,
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
    SemanticMetricVersion,
)
from aida.schemas import (
    GraphEdgeRead,
    GraphNodeRead,
    GraphSearchRead,
    ImpactAnalysisRead,
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
