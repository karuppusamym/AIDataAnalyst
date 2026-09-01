"""API for view and procedure SQL lineage parsing and storage.

Extracts column-level lineage edges from SQL view definitions and stored
procedure bodies.  Definitions are parsed only -- never executed.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    ProcedureLineageEdge,
    ViewLineageEdge,
)
from aida.schemas import (
    LineageEdgeRead,
    ProcedureLineageEdgeRead,
    ViewLineageEdgeRead,
    ViewLineageParseRequest,
    ViewLineageParseResponse,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_lineage_parser import (
    PROCEDURE_RESULT_TARGET,
    LineageEdge,
    ParseResult,
    parse_procedure_lineage,
    parse_view_lineage,
)

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


async def _resolve_table_ids(
    session: AsyncSession, datasource_id: UUID, table_names: set[str]
) -> dict[str, UUID]:
    """Resolve raw table-name strings the parser extracted to `MetadataTable.id`.

    AT-D2: `source_table_id`/`target_table_id` were never populated, so a
    parsed edge could never be traversed even once the unified lineage graph
    (LN-7/AT-10) was ready to fold it in -- `_build_unified_graph` already
    filters both columns to non-NULL and simply got nothing.

    Matched case-insensitively against every active table's fully-qualified
    (`catalog.schema.table`), schema-qualified (`schema.table`), and bare
    name -- the parser's own resolution may return any of those three forms
    depending on how the SQL qualified the reference. On a same-name
    collision across schemas the first table loaded wins; that ambiguity is
    inherent to a free-text name with no schema context, not something this
    lookup can resolve on its own.
    """
    if not table_names:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataTable.name, MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    by_key: dict[str, UUID] = {}
    for table_id, table_name, schema_name, catalog_name in rows:
        for key in (
            f"{catalog_name}.{schema_name}.{table_name}",
            f"{schema_name}.{table_name}",
            table_name,
        ):
            by_key.setdefault(key.lower(), table_id)
    return {name: by_key[name.lower()] for name in table_names if name.lower() in by_key}


def _persistable_source_table(edge: LineageEdge) -> str | None:
    """The raw source-table name to resolve, or None if the parser marked it
    unresolved -- an unresolved reference is never looked up by name (the raw
    text could coincidentally match an unrelated real table)."""
    return edge.source_table if edge.source_resolved else None


def _persistable_target_table(edge: LineageEdge) -> str | None:
    """The raw target-table name to resolve, or None for the parser's own
    internal `PROCEDURE_RESULT_TARGET` sentinel (a standalone SELECT with no
    real destination table -- not customer data, never a name to look up)."""
    return edge.target_table if edge.target_table != PROCEDURE_RESULT_TARGET else None


async def _persist_edges(
    session: AsyncSession,
    model: type[ViewLineageEdge] | type[ProcedureLineageEdge],
    datasource: DataSource,
    result: ParseResult,
) -> int:
    """Replace this parse's edges for the target table(s) it actually
    produced, then insert the fresh set with `source_table_id`/
    `target_table_id` resolved wherever the underlying table exists in the
    catalog.

    AT-D2: previously a blind `session.add` on every parse, with no unique
    constraint backing it up, doubled the graph on every re-parse. Scoping
    the delete to just the target table(s) this parse produced edges for
    (not the whole datasource) means an unrelated view's edges are
    untouched, and an empty/failed parse (no edges) never wipes the last
    known-good lineage for anything.

    Known limitation, pre-existing and not introduced here: this endpoint
    takes only raw SQL, with no procedure-identity field, so a standalone
    SELECT inside a procedure body is bucketed under the parser's shared
    `PROCEDURE_RESULT_TARGET` sentinel rather than a real target table.  Two
    different procedures that both produce an identical standalone-SELECT
    edge are indistinguishable under that shared bucket -- re-parsing one
    can replace the other's `PROCEDURE_RESULT_TARGET` rows. The unique
    constraint requires deleting by every target_table a parse touches,
    `PROCEDURE_RESULT_TARGET` included, or a re-parse containing a
    standalone SELECT would fail outright with a constraint violation.
    """
    if not result.edges:
        return 0

    target_tables = {edge.target_table for edge in result.edges}
    table_names = {
        name
        for edge in result.edges
        for name in (_persistable_source_table(edge), _persistable_target_table(edge))
        if name is not None
    }
    table_ids = await _resolve_table_ids(session, datasource.id, table_names)

    await session.execute(
        delete(model).where(
            model.datasource_id == datasource.id,
            model.target_table.in_(target_tables),
        )
    )
    for edge in result.edges:
        source_name = _persistable_source_table(edge)
        target_name = _persistable_target_table(edge)
        session.add(
            model(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                source_table=edge.source_table,
                source_column=edge.source_column,
                target_table=edge.target_table,
                target_column=edge.target_column,
                source_table_id=table_ids.get(source_name) if source_name else None,
                target_table_id=table_ids.get(target_name) if target_name else None,
                transformation_type=edge.transformation_type,
                confidence=edge.confidence,
                dialect=edge.dialect,
                sql_hash=result.sql_hash,
            )
        )
    return len(result.edges)


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

    persisted = await _persist_edges(session, ViewLineageEdge, datasource, result)
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

    persisted = await _persist_edges(session, ProcedureLineageEdge, datasource, result)
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
