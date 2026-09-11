"""API for view and procedure SQL lineage parsing and storage.

Extracts column-level lineage edges from SQL view definitions and stored
procedure bodies.  Definitions are parsed only -- never executed.
"""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.lineage_table_resolution import resolve_lineage_table_ids
from aida.models import (
    DataSource,
    ProcedureLineageEdge,
    ViewLineageEdge,
)
from aida.parsed_lineage_review_service import (
    resolve_review_status_for_new_edge,
)
from aida.resource_scope import load_datasource_in_scope
from aida.schemas import (
    LineageEdgeRead,
    ProcedureLineageEdgeRead,
    ViewLineageEdgeRead,
    ViewLineageParseRequest,
    ViewLineageParseResponse,
)
from aida.security import SecurityContext, require_roles
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
    *,
    context: SecurityContext | None = None,
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
    table_ids = await resolve_lineage_table_ids(session, datasource.id, table_names)

    settings = get_settings()
    review_mode = settings.lineage_parsed_edges_review_mode
    principal_id = context.principal_id if context is not None else None

    # P1-05: in require_review mode a re-parse must NOT clear an
    # ACTIVE edge that a human previously approved -- the re-parse is
    # untrusted, the approval is not. Only prior PROPOSED rows for the
    # same target table(s) are cleared. In auto_active mode (default,
    # backward-compatible with the pre-P1-05 delete-then-insert) we
    # continue to clear both ACTIVE and PROPOSED rows so a re-parse can
    # legitimately update an unreviewed edge in place.
    delete_stmt = delete(model).where(
        model.datasource_id == datasource.id,
        model.target_table.in_(target_tables),
    )
    if review_mode == "require_review":
        delete_stmt = delete_stmt.where(model.review_status == "PROPOSED")
    await session.execute(delete_stmt)

    # In require_review mode, fold the rows this re-parse would collide with
    # (same natural key) into a set so we can skip them -- inserting a
    # duplicate would raise on the natural-key unique constraint. Every row
    # the delete above kept is a decided one: ACTIVE, approved by a person or
    # activated by the threshold, or REJECTED, an answer a reviewer already
    # gave. Leaving it untouched is the explicit idempotency guarantee the
    # ADR calls for. Until 2026-09-11 only ACTIVE rows were folded, so a
    # re-parse after a rejection failed on the constraint.
    decided_keys: set[tuple[str, str, str, str, str]] = set()
    if review_mode == "require_review":
        existing_rows = (
            await session.scalars(
                select(model).where(
                    model.datasource_id == datasource.id,
                    model.target_table.in_(target_tables),
                    model.review_status != "PROPOSED",
                )
            )
        ).all()
        for row in cast("Sequence[Any]", existing_rows):
            decided_keys.add(
                (
                    row.source_table,
                    row.source_column,
                    row.target_table,
                    row.target_column,
                    row.transformation_type,
                )
            )

    inserted = 0
    for edge in result.edges:
        key = (
            edge.source_table,
            edge.source_column,
            edge.target_table,
            edge.target_column,
            edge.transformation_type,
        )
        if key in decided_keys:
            # A decided edge already covers this exact
            # source/target/column/transform triple -- leave it alone.
            continue
        source_name = _persistable_source_table(edge)
        target_name = _persistable_target_table(edge)
        review_status = resolve_review_status_for_new_edge(
            review_mode=review_mode,
            confidence=edge.confidence,
            threshold=settings.lineage_high_confidence_auto_active_threshold,
            source_trusted=None,  # SQL parses are never connector-pushed
        )
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
                review_status=review_status,
                created_by=principal_id,
            )
        )
        inserted += 1
    return inserted


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
    datasource = await load_datasource_in_scope(session, context, datasource_id)
    result = parse_view_lineage(body.sql, body.dialect)

    persisted = await _persist_edges(
        session, ViewLineageEdge, datasource, result, context=context
    )
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
    """Parse SQL text as a flat sequence of DML statements and extract
    column-level lineage. Not procedure-aware: identical parsing to
    `parse_view_lineage_endpoint` above, with no control-flow handling and
    no dynamic-SQL detection (AT-D5; real procedure-body parsing is tracker
    item N3, not started -- see `parse_procedure_lineage`'s docstring).

    The SQL is never executed.  Literal values are redacted.  Extracted edges
    are persisted for the datasource.
    """
    datasource = await load_datasource_in_scope(session, context, datasource_id)
    result = parse_procedure_lineage(body.sql, body.dialect)

    persisted = await _persist_edges(
        session, ProcedureLineageEdge, datasource, result, context=context
    )
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
    datasource = await load_datasource_in_scope(session, context, datasource_id)
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
    datasource = await load_datasource_in_scope(session, context, datasource_id)
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
