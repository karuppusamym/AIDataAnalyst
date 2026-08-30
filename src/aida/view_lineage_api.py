"""API for view and procedure SQL lineage parsing and storage.

Extracts column-level lineage edges from SQL view definitions and stored
procedure bodies.  Definitions are parsed only -- never executed.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import DataSource, ProcedureLineageEdge, ViewLineageEdge
from aida.schemas import (
    LineageEdgeRead,
    ProcedureLineageEdgeRead,
    ViewLineageEdgeRead,
    ViewLineageParseRequest,
    ViewLineageParseResponse,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_lineage_parser import parse_procedure_lineage, parse_view_lineage

router = APIRouter(prefix="/v1", tags=["view-lineage"])

_LINEAGE_WRITER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
)

_LINEAGE_READER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "MetadataReviewer",
    "Analyst",
    "Auditor",
    "Viewer",
)


async def _load_datasource(
    session: AsyncSession, context: SecurityContext, datasource_id: UUID
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    return datasource


@router.post(
    "/datasources/{datasource_id}/view-lineage/parse",
    response_model=ViewLineageParseResponse,
)
async def parse_view_lineage_endpoint(
    datasource_id: UUID,
    body: ViewLineageParseRequest,
    context: SecurityContext = Depends(require_roles(*_LINEAGE_WRITER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ViewLineageParseResponse:
    """Parse a SQL view definition and extract column-level lineage.

    The SQL is never executed.  Literal values are redacted.  Extracted edges
    are persisted for the datasource.
    """
    datasource = await _load_datasource(session, context, datasource_id)
    result = parse_view_lineage(body.sql, body.dialect)

    persisted = 0
    for edge in result.edges:
        session.add(
            ViewLineageEdge(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                source_table=edge.source_table,
                source_column=edge.source_column,
                target_table=edge.target_table,
                target_column=edge.target_column,
                transformation_type=edge.transformation_type,
                confidence=edge.confidence,
                dialect=edge.dialect,
                sql_hash=result.sql_hash,
            )
        )
        persisted += 1
    record_audit(
        session,
        context,
        action="view_lineage.parse",
        resource_type="datasource",
        resource_id=str(datasource_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"persisted_edges": persisted, "dialect": body.dialect},
    )
    await session.flush()

    return ViewLineageParseResponse(
        edges=[
            LineageEdgeRead(
                source_table=e.source_table,
                source_column=e.source_column,
                target_table=e.target_table,
                target_column=e.target_column,
                transformation_type=e.transformation_type,
                confidence=e.confidence,
                dialect=e.dialect,
            )
            for e in result.edges
        ],
        confidence=result.confidence,
        dialect=result.dialect,
        sql_hash=result.sql_hash,
        errors=result.errors,
        persisted_edge_count=persisted,
    )


@router.post(
    "/datasources/{datasource_id}/procedure-lineage/parse",
    response_model=ViewLineageParseResponse,
)
async def parse_procedure_lineage_endpoint(
    datasource_id: UUID,
    body: ViewLineageParseRequest,
    context: SecurityContext = Depends(require_roles(*_LINEAGE_WRITER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ViewLineageParseResponse:
    """Parse a stored procedure body and extract column-level lineage.

    The SQL is never executed.  Literal values are redacted.  Extracted edges
    are persisted for the datasource.
    """
    datasource = await _load_datasource(session, context, datasource_id)
    result = parse_procedure_lineage(body.sql, body.dialect)

    persisted = 0
    for edge in result.edges:
        session.add(
            ProcedureLineageEdge(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                source_table=edge.source_table,
                source_column=edge.source_column,
                target_table=edge.target_table,
                target_column=edge.target_column,
                transformation_type=edge.transformation_type,
                confidence=edge.confidence,
                dialect=edge.dialect,
                sql_hash=result.sql_hash,
            )
        )
        persisted += 1
    record_audit(
        session,
        context,
        action="procedure_lineage.parse",
        resource_type="datasource",
        resource_id=str(datasource_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"persisted_edges": persisted, "dialect": body.dialect},
    )
    await session.flush()

    return ViewLineageParseResponse(
        edges=[
            LineageEdgeRead(
                source_table=e.source_table,
                source_column=e.source_column,
                target_table=e.target_table,
                target_column=e.target_column,
                transformation_type=e.transformation_type,
                confidence=e.confidence,
                dialect=e.dialect,
            )
            for e in result.edges
        ],
        confidence=result.confidence,
        dialect=result.dialect,
        sql_hash=result.sql_hash,
        errors=result.errors,
        persisted_edge_count=persisted,
    )


@router.get(
    "/datasources/{datasource_id}/view-lineage",
    response_model=list[ViewLineageEdgeRead],
)
async def list_view_lineage(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[ViewLineageEdgeRead]:
    """List all view lineage edges for a datasource."""
    datasource = await _load_datasource(session, context, datasource_id)
    rows = (
        await session.scalars(
            select(ViewLineageEdge)
            .where(ViewLineageEdge.datasource_id == datasource.id)
            .order_by(ViewLineageEdge.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [ViewLineageEdgeRead.model_validate(row) for row in rows]


@router.get(
    "/datasources/{datasource_id}/procedure-lineage",
    response_model=list[ProcedureLineageEdgeRead],
)
async def list_procedure_lineage(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[ProcedureLineageEdgeRead]:
    """List all procedure lineage edges for a datasource."""
    datasource = await _load_datasource(session, context, datasource_id)
    rows = (
        await session.scalars(
            select(ProcedureLineageEdge)
            .where(ProcedureLineageEdge.datasource_id == datasource.id)
            .order_by(ProcedureLineageEdge.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [ProcedureLineageEdgeRead.model_validate(row) for row in rows]
