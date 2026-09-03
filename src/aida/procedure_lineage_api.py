"""N3: API for procedure-aware SQL lineage parsing and storage.

Mirrors `view_lineage_api.py`'s parse-and-persist shape (`_load_datasource`,
delete-then-insert scoped to what a parse actually produced, `_resolve_table_ids`)
but is routine-identity-aware from the start: rather than taking raw SQL text
with no identity (the gap AT-19 documents for the existing
`ProcedureLineageEdge`/`.../procedure-lineage/parse` path), this endpoint
takes a `MetadataRoutine.id` and parses that routine's own captured,
redacted body -- gated the same way `view_tool_blueprint.py` gates a view's
`MetadataViewDefinition` (missing/`UNAVAILABLE`/unparsed/quarantined all
refuse outright, never guess).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.envelope_models import AVAILABLE, MetadataRoutine
from aida.events import record_audit
from aida.ingest_screening import is_eligible_for_model_context
from aida.models import DataSource, MetadataCatalog, MetadataSchema, MetadataTable
from aida.procedure_capability_matrix import build_capability_matrix
from aida.procedure_lineage import (
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    parse_procedure_lineage,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.schemas import (
    DeepProcedureLineageEdgeRead,
    DeepProcedureLineageParseResponse,
    ProcedureCapabilityConstructRead,
    ProcedureCapabilityMatrixRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET

router = APIRouter(prefix="/v1", tags=["procedure-lineage"])

_LINEAGE_WRITER_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
_LINEAGE_READER_ROLES = (
    "PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward",
    "MetadataReviewer", "Analyst", "Auditor", "Viewer",
)


class RoutineNotEligibleError(ValueError):
    """The routine's own captured body is missing, withheld, unparsed or
    quarantined -- refused, never guessed. Mirrors `view_tool_blueprint.py`'s
    `ViewNotEligibleError` gate exactly."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"routine is not eligible for lineage parsing: {reason}")


async def _load_datasource(
    session: AsyncSession, context: SecurityContext, datasource_id: UUID
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    return datasource


def require_eligible_routine_body(routine: MetadataRoutine | None) -> str:
    """Return the routine's own redacted body text, or raise
    `RoutineNotEligibleError` naming exactly why it cannot be parsed."""
    if routine is None:
        raise RoutineNotEligibleError("no captured routine for this id")
    if routine.status != "ACTIVE":
        raise RoutineNotEligibleError(f"routine status is {routine.status}, not ACTIVE")
    if routine.availability != AVAILABLE:
        raise RoutineNotEligibleError(
            f"routine body is UNAVAILABLE ({routine.unavailable_reason or 'no reason recorded'})"
        )
    if routine.redaction_status != "PARSED":
        raise RoutineNotEligibleError(
            f"routine body redaction status is {routine.redaction_status}, not PARSED"
        )
    if not is_eligible_for_model_context(routine.screening_status):
        raise RoutineNotEligibleError(
            f"routine body is quarantined by prompt-risk screening "
            f"(screening_status={routine.screening_status})"
        )
    if routine.body_sql_redacted is None:
        raise RoutineNotEligibleError("routine has no body text despite AVAILABLE status")
    return routine.body_sql_redacted


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


async def _resolve_table_ids(
    session: AsyncSession, datasource_id: UUID, table_names: set[str]
) -> dict[str, UUID]:
    """Identical technique to `view_lineage_api._resolve_table_ids` (AT-D2
    defect 6) -- duplicated rather than imported because that function is
    private to a module this pass does not touch, and the two tables it
    resolves against (`ProcedureLineageEdge` vs `DeepProcedureLineageEdge`)
    are otherwise unrelated."""
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


def _persistable_table(name: str, resolved: bool) -> str | None:
    if not resolved or name == PROCEDURE_RESULT_TARGET:
        return None
    return name


async def _persist_edges(
    session: AsyncSession,
    datasource: DataSource,
    routine: MetadataRoutine,
    result: ProcedureParseResult,
) -> int:
    """Delete-then-insert scoped to this routine (AT-D2's pattern applied to
    routine identity rather than target-table identity: a routine's edges
    are always fully replaced by its own re-parse, and never touch any other
    routine's rows -- unlike the legacy raw-SQL endpoint, there is no shared
    `PROCEDURE_RESULT_TARGET` bucket collision risk here at all, because
    every row carries this routine's own `routine_id`)."""
    await session.execute(
        delete(DeepProcedureLineageEdge).where(
            DeepProcedureLineageEdge.datasource_id == datasource.id,
            DeepProcedureLineageEdge.routine_id == routine.id,
        )
    )
    if not result.edges:
        return 0

    table_names = {
        name
        for edge in result.edges
        for name in (
            _persistable_table(edge.source_table, edge.source_resolved),
            _persistable_table(edge.target_table, True),
        )
        if name is not None
    }
    table_ids = await _resolve_table_ids(session, datasource.id, table_names)

    for edge in result.edges:
        source_name = _persistable_table(edge.source_table, edge.source_resolved)
        target_name = _persistable_table(edge.target_table, True)
        session.add(
            DeepProcedureLineageEdge(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                routine_id=routine.id,
                statement_ordinal=edge.statement_ordinal,
                source_table=edge.source_table,
                source_column=edge.source_column,
                target_table=edge.target_table,
                target_column=edge.target_column,
                source_resolved=edge.source_resolved,
                source_table_id=table_ids.get(source_name) if source_name else None,
                target_table_id=table_ids.get(target_name) if target_name else None,
                transformation_type=edge.transformation_type,
                confidence=edge.confidence,
                dialect=edge.dialect,
                is_write=edge.is_write,
                is_intermediate=edge.is_intermediate,
                control_flow_context=edge.control_flow_context,
                unparsed_reason=edge.unparsed_reason,
                via_temp_table=edge.via_temp_table,
                sql_hash=result.sql_hash,
            )
        )
    return len(result.edges)


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
    `require_eligible_routine_body`. The SQL is never executed.
    """
    datasource = await _load_datasource(session, context, datasource_id)
    routine = await _load_routine(session, datasource, routine_id)
    try:
        body = require_eligible_routine_body(routine)
    except RoutineNotEligibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = parse_procedure_lineage(body, dialect=datasource.dialect)
    persisted = await _persist_edges(session, datasource, routine, result)
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
    datasource = await _load_datasource(session, context, datasource_id)
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
