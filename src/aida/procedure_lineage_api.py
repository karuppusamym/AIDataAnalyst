"""N3: API for procedure-aware SQL lineage parsing and storage.

Keeps the parse-and-persist shape the removed `view_lineage_api.py` had
(`_load_datasource`, delete-then-insert scoped to what a parse actually
produced) but is routine-identity-aware from the start: rather than taking
raw SQL text with no identity (the gap AT-19 documents for the former
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

from aida.authorization_gate import gate_read
from aida.capability_states import parse_coverage_state
from aida.context import get_correlation_id
from aida.db import get_session
from aida.envelope_models import MetadataRoutine, MetadataTrigger
from aida.events import record_audit
from aida.models import DataSource
from aida.procedure_capability_matrix import build_capability_matrix
from aida.procedure_lineage import ProcedureLineageEdgeRecord, parse_procedure_lineage
from aida.procedure_lineage_models import (
    DeepProcedureLineageEdge,
    RoutineParseCoverage,
    TriggerParseCoverage,
)
from aida.procedure_token_ranges import TokenRange
from aida.resource_scope import load_datasource_in_scope
from aida.routine_call_descent import descend_routine_calls
from aida.routine_lineage_edges import (
    RoutineNotEligibleError,
    persist_routine_edges,
    record_routine_parse_coverage,
    require_eligible_routine_body,
)
from aida.schemas import (
    DeepProcedureLineageEdgeRead,
    DeepProcedureLineageParseResponse,
    ProcedureCapabilityConstructRead,
    ProcedureCapabilityMatrixRead,
    RoutineParseCoverageRead,
    StatementRangeRead,
    TokenRangeRead,
    TriggerParseCoverageRead,
)
from aida.security import SecurityContext, require_roles
from atlas.platform.config import get_settings

router = APIRouter(prefix="/v1", tags=["procedure-lineage"])

_LINEAGE_WRITER_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
_LINEAGE_READER_ROLES = (
    "PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward",
    "MetadataReviewer", "Analyst", "Auditor", "Viewer",
)


async def _load_readable_datasource(
    session: AsyncSession, context: SecurityContext, datasource_id: UUID
) -> DataSource:
    """The datasource, once the caller's organization owns it **and** its workspace gate admits
    `READ_METADATA` -- the decision its tables route takes.

    R11-D30: these routes checked roles and tenant only (`load_datasource_in_scope`), so in an
    organization with an enforcing workspace a caller refused a datasource's tables could still
    read its routines' and triggers' lineage -- the tables they read and write -- and their parse
    coverage. The same gap R11-D28 closed for the unified-lineage surfaces. Asked before any
    routine or trigger is looked up, so a refusal says nothing about what exists. Under the
    default SHADOW posture the gate allows and records, so nothing changes until a workspace
    enforces. Settings are read here, as the parse route already read them, so the handlers'
    signatures -- which tests call directly -- are unchanged.
    """
    datasource = await load_datasource_in_scope(session, context, datasource_id)
    await gate_read(
        session,
        context,
        get_settings(),
        action="READ_METADATA",
        resource_type="datasource",
        resource_id=str(datasource.id),
        datasource_id=datasource.id,
    )
    return datasource


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
    where = edge.statement_range
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
        via_routine=edge.via_routine,
        statement_range=(
            StatementRangeRead(
                start_offset=where.start_offset,
                end_offset=where.end_offset,
                start_line=where.start_line,
                start_column=where.start_column,
                end_line=where.end_line,
                end_column=where.end_column,
            )
            if where is not None
            else None
        ),
        statement_range_status=edge.statement_range_status,
        statement_text_digest=edge.statement_text_digest,
        source_token_range=_token_read(edge.source_token_range),
        target_token_range=_token_read(edge.target_token_range),
        package_member=edge.package_member,
        member_attribution=edge.member_attribution,
    )


def _token_read(token: TokenRange | None) -> TokenRangeRead | None:
    if token is None:
        return None
    return TokenRangeRead(
        kind=token.kind, start_offset=token.start_offset, end_offset=token.end_offset
    )


def _row_token(
    kind: str | None, start_offset: int | None, end_offset: int | None
) -> TokenRangeRead | None:
    """A stored row's token range for one end of the edge, or None -- all three
    columns or none (R11-FP07 token grain)."""
    if kind is None or start_offset is None or end_offset is None:
        return None
    return TokenRangeRead(kind=kind, start_offset=start_offset, end_offset=end_offset)


def _row_range(row: DeepProcedureLineageEdge) -> StatementRangeRead | None:
    """A stored row's range, or None -- all six positions or none (R11-FP07)."""
    positions = (
        row.statement_start_offset,
        row.statement_end_offset,
        row.statement_start_line,
        row.statement_start_column,
        row.statement_end_line,
        row.statement_end_column,
    )
    if any(position is None for position in positions):
        return None
    start_offset, end_offset, start_line, start_column, end_line, end_column = (
        int(position) for position in positions if position is not None
    )
    return StatementRangeRead(
        start_offset=start_offset,
        end_offset=end_offset,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
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
    datasource = await _load_readable_datasource(session, context, datasource_id)
    routine = await _load_routine(session, datasource, routine_id)
    try:
        body = require_eligible_routine_body(routine)
    except RoutineNotEligibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # R11-FP07: calls to routines captured here are read through.
    result = await descend_routine_calls(
        session, datasource, routine, parse_procedure_lineage(body, dialect=datasource.dialect)
    )
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
    # F06.4: record how completely this body was understood, per object, in the
    # same transaction as the edges it produced. Without it, the only trace of
    # `is_fully_parsed` is this response and the audit entry below -- and
    # "which routines are not fully understood?" would have to be re-derived
    # from UNPARSED edges, which answers a different question once a re-parse
    # under review mode has replaced them.
    await record_routine_parse_coverage(
        session,
        datasource=datasource,
        routine=routine,
        result=result,
        measured_by=context.principal_id,
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
            # R11-FP03: codes only -- the grain a package was attributed at.
            "member_attribution": result.member_attribution,
            "member_fallback_reason": result.member_fallback_reason,
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
        statement_text_digest=result.statement_text_digest,
        member_attribution=result.member_attribution,
        member_fallback_reason=result.member_fallback_reason,
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
    datasource = await _load_readable_datasource(session, context, datasource_id)
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
            via_routine=row.via_routine,
            review_status=row.review_status,
            statement_range=_row_range(row),
            statement_range_status=row.statement_range_status,
            statement_text_digest=row.statement_text_digest,
            source_token_range=_row_token(
                row.source_token_kind,
                row.source_token_start_offset,
                row.source_token_end_offset,
            ),
            target_token_range=_row_token(
                row.target_token_kind,
                row.target_token_start_offset,
                row.target_token_end_offset,
            ),
            package_member=row.package_member,
            member_attribution=row.member_attribution,
            member_routine_id=row.member_routine_id,
        )
        for row in rows
    ]


@router.get(
    "/datasources/{datasource_id}/procedures/{routine_id}/parse-coverage",
    response_model=RoutineParseCoverageRead,
)
async def get_routine_parse_coverage(
    datasource_id: UUID,
    routine_id: UUID,
    context: SecurityContext = Depends(require_roles(*_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> RoutineParseCoverageRead:
    """How completely this routine's body was understood, as last measured.

    Finding F06.4: an object being inventoried is not the same as every path
    through it being understood, and this is the stored answer rather than one
    re-derived by scanning for `UNPARSED` edges. 404 when no parse has
    measured this routine yet -- "not measured" is a different answer from
    "fully understood", and returning a zeroed row would collapse them.

    `state` is the reporting-boundary rendering of the two stored booleans
    (`aida.capability_states.parse_coverage_state`); the row itself keeps them
    as booleans, so no sentinel string ever stands where a real value would.
    """
    datasource = await _load_readable_datasource(session, context, datasource_id)
    await _load_routine(session, datasource, routine_id)
    coverage = (
        await session.scalars(
            select(RoutineParseCoverage).where(
                RoutineParseCoverage.datasource_id == datasource.id,
                RoutineParseCoverage.routine_id == routine_id,
            )
        )
    ).first()
    if coverage is None:
        raise HTTPException(
            status_code=404, detail="no parse has measured this routine's coverage yet"
        )
    return RoutineParseCoverageRead(
        routine_id=coverage.routine_id,
        state=parse_coverage_state(
            parse_completed=coverage.parse_completed,
            statement_count=coverage.statement_count,
        ).value,
        parse_completed=coverage.parse_completed,
        is_read_only=coverage.is_read_only,
        statement_count=coverage.statement_count,
        unparsed_statement_count=coverage.unparsed_statement_count,
        unparsed_reason_codes=(
            coverage.unparsed_reason_codes.split(",")
            if coverage.unparsed_reason_codes
            else []
        ),
        dialect=coverage.dialect,
        confidence=coverage.confidence,
        source_mapping_granularity=coverage.source_mapping_granularity,
        parsed_at=coverage.parsed_at,
        member_attribution=coverage.member_attribution,
        member_fallback_reason=coverage.member_fallback_reason,
    )


@router.get(
    "/datasources/{datasource_id}/triggers/{trigger_id}/parse-coverage",
    response_model=TriggerParseCoverageRead,
)
async def get_trigger_parse_coverage(
    datasource_id: UUID,
    trigger_id: UUID,
    context: SecurityContext = Depends(require_roles(*_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> TriggerParseCoverageRead:
    """How completely this trigger's body was understood, as last measured (R11-FP01).

    The trigger axis of `get_routine_parse_coverage`, with the same contract: the datasource is
    resolved in the caller's scope first, a trigger of another datasource or organization is not
    found, and 404 means no parse has measured it yet -- "not measured" is a different answer
    from "fully understood". The coverage row restates the organization and the datasource
    (INV-5).
    """
    datasource = await _load_readable_datasource(session, context, datasource_id)
    trigger = await session.get(MetadataTrigger, trigger_id)
    if (
        trigger is None
        or trigger.datasource_id != datasource.id
        or trigger.organization_id != datasource.organization_id
    ):
        raise HTTPException(status_code=404, detail="trigger not found for this datasource")
    coverage = (
        await session.scalars(
            select(TriggerParseCoverage).where(
                TriggerParseCoverage.organization_id == datasource.organization_id,
                TriggerParseCoverage.datasource_id == datasource.id,
                TriggerParseCoverage.trigger_id == trigger_id,
            )
        )
    ).first()
    if coverage is None:
        raise HTTPException(
            status_code=404, detail="no parse has measured this trigger's coverage yet"
        )
    return TriggerParseCoverageRead(
        trigger_id=coverage.trigger_id,
        routine_id=coverage.routine_id,
        state=parse_coverage_state(
            parse_completed=coverage.parse_completed,
            statement_count=coverage.statement_count,
        ).value,
        parse_completed=coverage.parse_completed,
        is_read_only=coverage.is_read_only,
        statement_count=coverage.statement_count,
        unparsed_statement_count=coverage.unparsed_statement_count,
        unparsed_reason_codes=(
            coverage.unparsed_reason_codes.split(",")
            if coverage.unparsed_reason_codes
            else []
        ),
        dialect=coverage.dialect,
        confidence=coverage.confidence,
        source_mapping_granularity=coverage.source_mapping_granularity,
        parsed_at=coverage.parsed_at,
    )


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
