"""N3: API for procedure-aware SQL lineage parsing and storage.

Mirrors `view_lineage_api.py`'s parse-and-persist shape (`_load_datasource`,
delete-then-insert scoped to what a parse actually produced) but is
routine-identity-aware from the start: rather than taking raw SQL text
with no identity (the gap AT-19 documents for the existing
`ProcedureLineageEdge`/`.../procedure-lineage/parse` path), this endpoint
takes a `MetadataRoutine.id` and parses that routine's own captured,
redacted body -- gated the same way `view_tool_blueprint.py` gates a view's
`MetadataViewDefinition` (missing/`UNAVAILABLE`/unparsed/quarantined all
refuse outright, never guess).

Since 2026-09-11 the table has ADR-0026's review state, and a parse here is
written under `lineage_parsed_edges_review_mode` like every other parser's.
The gate and the write live in `aida.routine_lineage_edges`, which the lineage
agent (ADR-0029) shares; this module owns the paths and the roles.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.envelope_models import MetadataRoutine
from aida.events import record_audit
from aida.models import DataSource
from aida.procedure_capability_matrix import build_capability_matrix
from aida.procedure_lineage import ProcedureLineageEdgeRecord, parse_procedure_lineage
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.resource_scope import load_datasource_in_scope
from aida.routine_lineage_edges import (
    RoutineNotEligibleError,
    persist_routine_edges,
    require_eligible_routine_body,
)
from aida.schemas import (
    DeepProcedureLineageEdgeRead,
    DeepProcedureLineageParseResponse,
    ProcedureCapabilityConstructRead,
    ProcedureCapabilityMatrixRead,
)
from aida.security import SecurityContext, require_roles
from atlas.platform.config import get_settings

router = APIRouter(prefix="/v1", tags=["procedure-lineage"])

_LINEAGE_WRITER_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
_LINEAGE_READER_ROLES = (
    "PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward",
    "MetadataReviewer", "Analyst", "Auditor", "Viewer",
)


async def _load_routine(
    session: AsyncSession, datasource: DataSource, routine_id: UUID
) -> MetadataRoutine:
    routine = await session.get(MetadataRoutine, routine_id)
    if (
        routine is None
        or routine.datasource_id != datasource.id
        or routine.organization_id != datasource.organization_id
    ):
        raise HTTPException(status_code=404, detail="routine not found for this datasource")
    return routine


def _edge_read(edge: ProcedureLineageEdgeRecord) -> DeepProcedureLineageEdgeRead:
    return DeepProcedureLineageEdgeRead(
        source_table=edge.source_table,
        source_column=edge.source_column,
        target_table=edge.target_table,
        target_column=edge.target_column,
        transformation_type=edge.transformation_type,
        confidence=edge.confidence,
        dialect=edge.dialect,
        source_resolved=edge.source_resolved,
        statement_ordinal=edge.statement_ordinal,
        is_write=edge.is_write,
        is_intermediate=edge.is_intermediate,
        control_flow_context=edge.control_flow_context,
        unparsed_reason=edge.unparsed_reason,
        via_temp_table=edge.via_temp_table,
    )


@router.post(
    "/datasources/{datasource_id}/procedures/{routine_id}/lineage/parse",
    response_model=DeepProcedureLineageParseResponse,
)
async def parse_deep_procedure_lineage_endpoint(
    datasource_id: UUID,
    routine_id: UUID,
    context: SecurityContext = Depends(require_roles(*_LINEAGE_WRITER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> DeepProcedureLineageParseResponse:
    """N3: procedure-aware column-level lineage extraction for one captured
    `MetadataRoutine`. Refuses (422) rather than guesses when the routine's
    own body is missing, withheld, unparsed, or quarantined -- see
    `require_eligible_routine_body`. The SQL is never executed. The edges are
    stored under the deployment's review mode (`persist_routine_edges`).
    """
    datasource = await load_datasource_in_scope(session, context, datasource_id)
    routine = await _load_routine(session, datasource, routine_id)
    try:
        body = require_eligible_routine_body(routine)
    except RoutineNotEligibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = parse_procedure_lineage(body, dialect=datasource.dialect)
    settings = get_settings()
    persisted = await persist_routine_edges(
        session,
        datasource=datasource,
        routine=routine,
        result=result,
        review_mode=settings.lineage_parsed_edges_review_mode,
        threshold=settings.lineage_high_confidence_auto_active_threshold,
        created_by=context.principal_id,
    )
    record_audit(
        session,
        context,
        action="procedure_lineage.deep_parse",
        resource_type="metadata_routine",
        resource_id=str(routine_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "persisted_edges": persisted,
            "dialect": datasource.dialect,
            "is_fully_parsed": result.is_fully_parsed,
            "is_read_only": result.is_read_only,
            "statement_count": result.statement_count,
            "review_mode": settings.lineage_parsed_edges_review_mode,
        },
    )
    await session.flush()

    return DeepProcedureLineageParseResponse(
        edges=[_edge_read(edge) for edge in result.edges],
        statement_count=result.statement_count,
        confidence=result.confidence,
        dialect=result.dialect,
        sql_hash=result.sql_hash,
        errors=result.errors,
        is_fully_parsed=result.is_fully_parsed,
        is_read_only=result.is_read_only,
        persisted_edge_count=persisted,
    )


@router.get(
    "/datasources/{datasource_id}/procedures/{routine_id}/lineage",
    response_model=list[DeepProcedureLineageEdgeRead],
)
async def list_deep_procedure_lineage(
    datasource_id: UUID,
    routine_id: UUID,
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[DeepProcedureLineageEdgeRead]:
    datasource = await load_datasource_in_scope(session, context, datasource_id)
    rows = (
        await session.scalars(
            select(DeepProcedureLineageEdge)
            .where(
                DeepProcedureLineageEdge.datasource_id == datasource.id,
                DeepProcedureLineageEdge.routine_id == routine_id,
            )
            .order_by(DeepProcedureLineageEdge.statement_ordinal)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [
        DeepProcedureLineageEdgeRead(
            source_table=row.source_table,
            source_column=row.source_column,
            target_table=row.target_table,
            target_column=row.target_column,
            transformation_type=row.transformation_type,
            confidence=row.confidence,
            dialect=row.dialect,
            source_resolved=row.source_resolved,
            statement_ordinal=row.statement_ordinal,
            is_write=row.is_write,
            is_intermediate=row.is_intermediate,
            control_flow_context=row.control_flow_context,
            unparsed_reason=row.unparsed_reason,
            via_temp_table=row.via_temp_table,
            review_status=row.review_status,
        )
        for row in rows
    ]


@router.get(
    "/procedure-lineage/capability-matrix",
    response_model=ProcedureCapabilityMatrixRead,
)
async def get_procedure_lineage_capability_matrix(
    context: SecurityContext = Depends(require_roles(*_LINEAGE_READER_ROLES)),
) -> ProcedureCapabilityMatrixRead:
    """AT-22: serve the parser capability matrix live, generated from
    `sql_lineage_parser.py`'s and `procedure_lineage.py`'s own dispatch code
    at request time (`aida.procedure_capability_matrix.build_capability_matrix`)
    -- the same source `scripts/generate_procedure_capability_matrix.py`
    uses to publish `Docs/90-reference/procedure-lineage-capability-matrix.md`,
    so that published page is verifiably backed by a live, callable source,
    not only a one-off script. Not datasource-scoped (dialect/construct
    support is a property of the parser code itself, not of any one
    customer's data), so no `datasource_id`/tenancy check applies here --
    `context` is still required so an unauthenticated caller cannot reach it.
    """
    matrix = build_capability_matrix()
    return ProcedureCapabilityMatrixRead(
        generated_at=matrix.generated_at,
        dialects=list(matrix.dialects),
        constructs=[
            ProcedureCapabilityConstructRead(
                construct_name=row.construct,
                view_parser_status=row.view_parser_status,
                procedure_parser_status=row.procedure_parser_status,
                evidence=row.evidence,
            )
            for row in matrix.constructs
        ],
        unparsed_reasons=list(matrix.unparsed_reasons),
    )
