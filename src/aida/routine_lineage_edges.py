"""The routine-aware procedure lineage table: its gate and its writes.

`deep_procedure_lineage_edge` holds what `procedure_lineage.parse_procedure_lineage`
finds in one captured `MetadataRoutine` body. Two callers write it -- a person's
parse (`procedure_lineage_api`) and the lineage agent (ADR-0029) -- and they share
what lives here, moved out of that router on 2026-09-11 so the agent reaches it
without importing one:

* `require_eligible_routine_body`, the gate every use of a captured body passes
  first (`procedure_tool_blueprint` uses it too);
* `routine_edge_row`, one parsed edge as a row, its table ids resolved by
  `lineage_table_resolution` -- the router's identical private copy of that
  resolver is gone;
* `persist_routine_edges`, a person's parse, written under ADR-0026's review
  mode the way every other parser's already is.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import AVAILABLE, MetadataRoutine
from aida.ingest_screening import is_eligible_for_model_context
from aida.lineage_table_resolution import resolve_lineage_table_ids
from aida.models import DataSource
from aida.parsed_lineage_review_service import resolve_review_status_for_new_edge
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET

#: The table's natural key within one routine: statement ordinal, source,
#: target, transformation, and the temp table a transitive edge runs through.
RoutineEdgeKey = tuple[int, str, str, str, str, str, str | None]


class RoutineNotEligibleError(ValueError):
    """The routine's own captured body is missing, withheld, unparsed or
    quarantined -- refused, never guessed. Mirrors `view_tool_blueprint.py`'s
    `ViewNotEligibleError` gate exactly."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"routine is not eligible for lineage parsing: {reason}")


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


def persistable_table(name: str, resolved: bool) -> str | None:
    """`name`, if it can be a catalog table: resolved, and not the parser's
    placeholder for a statement's result set."""
    if not resolved or name == PROCEDURE_RESULT_TARGET:
        return None
    return name


def routine_edge_key(
    edge: ProcedureLineageEdgeRecord | DeepProcedureLineageEdge,
) -> RoutineEdgeKey:
    return (
        edge.statement_ordinal,
        edge.source_table,
        edge.source_column,
        edge.target_table,
        edge.target_column,
        edge.transformation_type,
        edge.via_temp_table,
    )


async def resolve_routine_table_ids(
    session: AsyncSession, datasource_id: UUID, edges: Iterable[ProcedureLineageEdgeRecord]
) -> dict[str, UUID]:
    """Catalog ids for every name in `edges` that can be a table."""
    names = {
        name
        for edge in edges
        for name in (
            persistable_table(edge.source_table, edge.source_resolved),
            persistable_table(edge.target_table, True),
        )
        if name is not None
    }
    return await resolve_lineage_table_ids(session, datasource_id, names)


def routine_edge_row(
    edge: ProcedureLineageEdgeRecord,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    routine_id: UUID,
    sql_hash: str,
    table_ids: dict[str, UUID],
    review_status: str,
    created_by: str | None,
) -> DeepProcedureLineageEdge:
    source_name = persistable_table(edge.source_table, edge.source_resolved)
    target_name = persistable_table(edge.target_table, True)
    return DeepProcedureLineageEdge(
        organization_id=organization_id,
        datasource_id=datasource_id,
        routine_id=routine_id,
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
        sql_hash=sql_hash,
        review_status=review_status,
        created_by=created_by,
    )


async def persist_routine_edges(
    session: AsyncSession,
    *,
    datasource: DataSource,
    routine: MetadataRoutine,
    result: ProcedureParseResult,
    review_mode: str,
    threshold: float,
    created_by: str | None,
) -> int:
    """A person's parse of one routine, written under ADR-0026's review mode.
    Returns the rows written.

    `auto_active`, the default, keeps what this table always did: the routine's
    rows are replaced by its re-parse -- AT-D2's delete-then-insert, scoped to
    this routine and never touching another's -- and every one is ACTIVE.

    `require_review` lands each edge as every other parser's are
    (`resolve_review_status_for_new_edge`), and a re-parse replaces only what
    is undecided: PROPOSED rows and UNPARSED markers. A key already present in
    any other state -- approved, activated by the confidence threshold, or
    rejected by a reviewer -- is left as it is and not written again.

    An UNPARSED marker records a gap in the parse, not an edge, so it is never
    put in front of a reviewer: it is ACTIVE in either mode.
    """
    clear = delete(DeepProcedureLineageEdge).where(
        DeepProcedureLineageEdge.datasource_id == datasource.id,
        DeepProcedureLineageEdge.routine_id == routine.id,
    )
    if review_mode == "require_review":
        clear = clear.where(
            or_(
                DeepProcedureLineageEdge.review_status == "PROPOSED",
                DeepProcedureLineageEdge.transformation_type == UNPARSED_TRANSFORMATION_TYPE,
            )
        )
    await session.execute(clear)
    if not result.edges:
        return 0

    kept: set[RoutineEdgeKey] = set()
    if review_mode == "require_review":
        kept = {
            routine_edge_key(row)
            for row in (
                await session.scalars(
                    select(DeepProcedureLineageEdge).where(
                        DeepProcedureLineageEdge.datasource_id == datasource.id,
                        DeepProcedureLineageEdge.routine_id == routine.id,
                    )
                )
            ).all()
        }
    table_ids = await resolve_routine_table_ids(session, datasource.id, result.edges)
    written = 0
    for edge in result.edges:
        key = routine_edge_key(edge)
        if key in kept:
            continue
        kept.add(key)
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE:
            review_status = "ACTIVE"
        else:
            review_status = resolve_review_status_for_new_edge(
                review_mode=review_mode,
                confidence=edge.confidence,
                threshold=threshold,
                source_trusted=None,  # a captured body is parsed here, never pushed
            )
        session.add(
            routine_edge_row(
                edge,
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                routine_id=routine.id,
                sql_hash=result.sql_hash,
                table_ids=table_ids,
                review_status=review_status,
                created_by=created_by,
            )
        )
        written += 1
    return written
