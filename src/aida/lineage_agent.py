"""ADR-0029: the lineage agent.

A task agent (`aida.task_agent`) -- `agent:lineage` by default -- that closes a
gap nothing else closed: the platform captures every view's definition and every
routine's body at ingestion (`MetadataViewDefinition`, `MetadataRoutine`) and
then never parses them. Lineage from either existed only when a person asked
for a parse.

Two capabilities:

* **VIEW_LINEAGE.** Views whose captured definition is eligible -- ACTIVE,
  AVAILABLE, literal-redacted and screened CLEAN, the gate
  `view_tool_blueprint` applies before any view text is used -- that have no
  parsed lineage edge targeting them yet, in any review state.
  `sql_lineage_parser.parse_view_lineage` parses the redacted definition, wrapped
  as `CREATE VIEW <schema>.<view> AS ...` when the connector captured only the
  body, so every edge targets the view itself rather than the parser's
  `<RESULT>` placeholder. Edges land in `view_lineage_edge`.
* **PROCEDURE_LINEAGE.** Routines whose captured body passes the same gate
  (`routine_lineage_edges.require_eligible_routine_body`, a person's parse's
  own) and that have no row in the routine-aware `deep_procedure_lineage_edge`
  table, in any review state. `procedure_lineage.parse_procedure_lineage` parses
  the redacted body. Only table-to-table lineage is proposed: an edge into or
  out of a temp table or table variable is the procedure's own plumbing -- the
  parser's transitive edge through it is proposed instead -- an edge into
  `<RESULT>` is a result set, and an UNPARSED marker is a gap, not an edge.

Both:

* **Proposal.** Each edge lands PROPOSED, always, with `created_by` the
  agent's identity -- whatever `lineage_parsed_edges_review_mode` or the
  high-confidence auto-activation threshold say. Those settings govern what a
  *person's* parse may activate. An agent's output is decided by a person in
  ADR-0026's per-edge queue, whose maker-checker refuses the agent as the
  reviewer of its own edge. An edge whose source the parser could not resolve
  is not proposed.
* **Negative knowledge.** An object with any lineage edge -- including one a
  reviewer rejected -- is not re-parsed by the agent. A definition or body it
  could not turn into lineage is recorded once, so the same dead end is not
  re-examined on every run until it changes.

Nothing here calls a model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from functools import partial
from typing import Any, Final
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import AVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.ingest_screening import CLEAN
from aida.lineage_table_resolution import resolve_lineage_table_ids
from aida.models import AgentTask, DataSource, MetadataSchema, MetadataTable, ViewLineageEdge
from aida.parsed_lineage_review_service import edge_confidence_as_float
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    parse_procedure_lineage,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_lineage_edges import (
    RoutineEdgeKey,
    persistable_table,
    require_eligible_routine_body,
    resolve_routine_table_ids,
    routine_edge_key,
    routine_edge_row,
)
from aida.security import SecurityContext
from aida.sql_lineage_parser import parse_view_lineage
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_WOULD_PROPOSE,
    QUEUE_PARSED_LINEAGE,
    CapabilityWork,
    TaskAgentCapability,
    TaskAgentItem,
    TaskAgentOutcome,
    TaskAgentOutcomeRow,
    TaskAgentRun,
    TaskAgentRunRequest,
    TaskAgentSpec,
    run_task_agent,
)
from atlas.platform.config import Settings

CAPABILITY_VIEW_LINEAGE: Final = "VIEW_LINEAGE"
CAPABILITY_PROCEDURE_LINEAGE: Final = "PROCEDURE_LINEAGE"
#: What the queue decides: one parsed column-level edge, from a view ...
EDGE_OBJECT_TYPE: Final = "VIEW_LINEAGE_EDGE"
#: ... or from a routine.
PROCEDURE_EDGE_OBJECT_TYPE: Final = "PROCEDURE_LINEAGE_EDGE"
#: The unit of work the ledger links to: the definition, or the routine, parsed.
_PROPOSAL_REF_TYPE: Final = "VIEW_DEFINITION"
_ROUTINE_REF_TYPE: Final = "ROUTINE"

# Skips: an object the agent examined and deliberately left alone.
SKIP_UNSUPPORTED_DIALECT: Final = "unsupported_dialect"
SKIP_UNPARSEABLE: Final = "unparseable_definition"
SKIP_NO_LINEAGE: Final = "no_resolvable_lineage"
SKIP_LINEAGE_KNOWN: Final = "lineage_already_known"

#: Objects examined per run, as a multiple of the proposal limit.
_EXAMINE_FACTOR: Final = 4

#: The edge tables the agent writes, by the object type its outcomes name.
_EDGE_TABLES: Final[tuple[tuple[str, Any], ...]] = (
    (EDGE_OBJECT_TYPE, ViewLineageEdge),
    (PROCEDURE_EDGE_OBJECT_TYPE, DeepProcedureLineageEdge),
)


async def _pending_edges(session: AsyncSession, organization_id: UUID, principal: str) -> int:
    pending = 0
    for _object_type, model in _EDGE_TABLES:
        count = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.organization_id == organization_id,
                model.created_by == principal,
                model.review_status == "PROPOSED",
            )
        )
        pending += int(count or 0)
    return pending


async def _edge_outcomes(
    session: AsyncSession, organization_id: UUID, principal: str
) -> list[TaskAgentOutcomeRow]:
    """Its edges by review state, a row for each table it has written:
    PROPOSED is pending, ACTIVE approved, REJECTED rejected."""
    outcomes: list[TaskAgentOutcomeRow] = []
    for object_type, model in _EDGE_TABLES:
        rows = (
            await session.execute(
                select(model.review_status, func.count())
                .where(
                    model.organization_id == organization_id,
                    model.created_by == principal,
                )
                .group_by(model.review_status)
            )
        ).all()
        if not rows:
            continue
        by_status: Counter[str] = Counter({str(status): int(count) for status, count in rows})
        pending = by_status.pop("PROPOSED", 0)
        approved = by_status.pop("ACTIVE", 0)
        rejected = by_status.pop("REJECTED", 0)
        outcomes.append(
            TaskAgentOutcomeRow(
                object_type=object_type,
                pending=pending,
                approved=approved,
                rejected=rejected,
                other=sum(by_status.values()),
            )
        )
    return outcomes


LINEAGE_AGENT: Final = TaskAgentSpec(
    key="lineage",
    audit_roles=frozenset({"MetadataAdmin"}),
    capabilities=(
        TaskAgentCapability(
            key=CAPABILITY_VIEW_LINEAGE,
            object_type=EDGE_OBJECT_TYPE,
            intent="lineage.propose_view_lineage",
            producer="sql_lineage_parser: view definitions captured at ingestion",
            queue=QUEUE_PARSED_LINEAGE,
        ),
        TaskAgentCapability(
            key=CAPABILITY_PROCEDURE_LINEAGE,
            object_type=PROCEDURE_EDGE_OBJECT_TYPE,
            intent="lineage.propose_procedure_lineage",
            producer="procedure_lineage: routine bodies captured at ingestion",
            queue=QUEUE_PARSED_LINEAGE,
        ),
    ),
    pending_counter=_pending_edges,
    outcome_reader=_edge_outcomes,
)


def as_create_view(view_name: str, definition: str) -> str:
    """The definition as a statement the parser can attribute to the view.

    Connectors usually capture only a view's body (`SELECT ...`); parsed alone,
    its edges would target the parser's `<RESULT>` placeholder rather than the
    view. A definition that is already a CREATE statement is left as it is.
    """
    body = definition.strip().rstrip(";").strip()
    if body[:6].lower() == "create":
        return body
    return f"CREATE VIEW {view_name} AS {body}"


def _inputs(definition: MetadataViewDefinition) -> dict[str, Any]:
    """Value-free: which definition, at which version."""
    return {
        "capability": CAPABILITY_VIEW_LINEAGE,
        "view_definition_id": str(definition.id),
        "definition_fingerprint": definition.definition_fingerprint or definition.fingerprint,
    }


async def _view_lineage(run: TaskAgentRun) -> None:
    session = run.session
    already_parsed = exists().where(ViewLineageEdge.target_table_id == MetadataTable.id)
    # A definition examined since it last changed -- proposed from, or declined.
    already_examined = exists().where(
        AgentTask.organization_id == run.organization_id,
        AgentTask.agent_principal_id == run.principal_id,
        AgentTask.proposal_ref_type == _PROPOSAL_REF_TYPE,
        AgentTask.proposal_ref_id == MetadataViewDefinition.id,
        AgentTask.started_at >= MetadataViewDefinition.updated_at,
    )
    filters: list[Any] = [
        MetadataViewDefinition.organization_id == run.organization_id,
        MetadataViewDefinition.status == "ACTIVE",
        MetadataViewDefinition.availability == AVAILABLE,
        MetadataViewDefinition.redaction_status == "PARSED",
        # `ingest_screening.is_eligible_for_model_context`, as a predicate.
        MetadataViewDefinition.screening_status == CLEAN,
        MetadataTable.status == "ACTIVE",
        ~already_parsed,
        ~already_examined,
    ]
    if run.datasource_id is not None:
        filters.append(MetadataViewDefinition.datasource_id == run.datasource_id)
    rows = (
        await session.execute(
            select(MetadataViewDefinition, MetadataTable, MetadataSchema, DataSource)
            .join(MetadataTable, MetadataTable.id == MetadataViewDefinition.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(DataSource, DataSource.id == MetadataViewDefinition.datasource_id)
            .where(*filters)
            .order_by(MetadataTable.name, MetadataTable.id)
            .limit(run.outcome.limit * _EXAMINE_FACTOR)
        )
    ).all()
    proposed = 0
    for definition, view, schema, datasource in rows:
        if proposed >= run.outcome.limit:
            return
        if not await run.may_continue():
            return
        item = run.add(
            await run.guarded(
                CAPABILITY_VIEW_LINEAGE,
                subject_id=view.id,
                subject_name=f"{schema.name}.{view.name}",
                work=partial(_propose_view_lineage, run, definition, view, schema, datasource),
            )
        )
        if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
            proposed += 1


async def _propose_view_lineage(
    run: TaskAgentRun,
    definition: MetadataViewDefinition,
    view: MetadataTable,
    schema: MetadataSchema,
    datasource: DataSource,
) -> TaskAgentItem:
    capability = CAPABILITY_VIEW_LINEAGE
    session = run.session
    view_id, view_name = view.id, f"{schema.name}.{view.name}"
    definition_id, datasource_id = definition.id, datasource.id
    inputs = _inputs(definition)

    async def decline(reason: str) -> TaskAgentItem:
        return await run.declined(
            capability,
            subject_id=view_id,
            subject_name=view_name,
            reason=reason,
            inputs=inputs,
            proposal_ref_type=_PROPOSAL_REF_TYPE,
            proposal_ref_id=definition_id,
        )

    result = parse_view_lineage(
        as_create_view(view_name, definition.definition_sql_redacted or ""),
        dialect=datasource.dialect,
    )
    if any(error.startswith("unsupported dialect") for error in result.errors):
        return await decline(SKIP_UNSUPPORTED_DIALECT)
    resolved = [edge for edge in result.edges if edge.source_resolved]
    if not result.edges:
        return await decline(SKIP_UNPARSEABLE if result.errors else SKIP_NO_LINEAGE)
    if not resolved:
        return await decline(SKIP_NO_LINEAGE)

    # The natural key the table enforces. A key already present in any state --
    # ACTIVE, PROPOSED, or REJECTED by a reviewer -- is not proposed again.
    existing = {
        tuple(row)
        for row in (
            await session.execute(
                select(
                    ViewLineageEdge.source_table,
                    ViewLineageEdge.source_column,
                    ViewLineageEdge.target_table,
                    ViewLineageEdge.target_column,
                    ViewLineageEdge.transformation_type,
                ).where(
                    ViewLineageEdge.datasource_id == datasource_id,
                    ViewLineageEdge.target_table.in_({edge.target_table for edge in resolved}),
                )
            )
        ).all()
    }
    fresh = []
    for edge in resolved:
        key = (
            edge.source_table,
            edge.source_column,
            edge.target_table,
            edge.target_column,
            edge.transformation_type,
        )
        if key not in existing:
            existing.add(key)
            fresh.append(edge)
    if not fresh:
        return await decline(SKIP_LINEAGE_KNOWN)

    confidence = edge_confidence_as_float(result.confidence)
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=view_id,
            subject_name=view_name,
            confidence=confidence,
        )
    source_ids = await resolve_lineage_table_ids(
        session, datasource_id, {edge.source_table for edge in fresh}
    )
    rows = [
        ViewLineageEdge(
            organization_id=run.organization_id,
            datasource_id=datasource_id,
            source_table=edge.source_table,
            source_column=edge.source_column,
            target_table=edge.target_table,
            target_column=edge.target_column,
            source_table_id=source_ids.get(edge.source_table),
            target_table_id=view_id,
            transformation_type=edge.transformation_type,
            confidence=edge.confidence,
            dialect=edge.dialect,
            sql_hash=result.sql_hash,
            # Never ACTIVE, whatever the review mode or the confidence: an
            # agent's edge is decided by a person. See the module docstring.
            review_status="PROPOSED",
            created_by=run.principal_id,
        )
        for edge in fresh
    ]
    session.add_all(rows)
    await session.flush()
    return await run.proposed_in_queue(
        capability,
        proposal_ref_type=_PROPOSAL_REF_TYPE,
        proposal_ref_id=definition_id,
        subject_id=view_id,
        subject_name=view_name,
        inputs=inputs,
        pending_added=len(rows),
        evidence={
            "edge_ids": [str(row.id) for row in rows],
            "edge_count": len(rows),
            "unresolved_sources": len(result.edges) - len(resolved),
            "sql_hash": result.sql_hash,
        },
        confidence=confidence,
    )


def proposable_procedure_edges(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    """A routine parse's table-to-table lineage, once per natural key.

    Withheld: an UNPARSED marker, which is a gap, not an edge; an edge whose
    source the parser could not resolve; an edge into a temp table or table
    variable, or out of one -- the procedure's own plumbing, whose end-to-end
    lineage the parser states as a transitive edge through it (`via_temp_table`)
    -- and an edge into `<RESULT>`, which is a result set, not a table.
    """
    intermediates = {edge.target_table.lower() for edge in result.edges if edge.is_intermediate}
    seen: set[RoutineEdgeKey] = set()
    proposable: list[ProcedureLineageEdgeRecord] = []
    for edge in result.edges:
        if (
            edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
            or edge.is_intermediate
            or edge.source_table.lower() in intermediates
            or persistable_table(edge.source_table, edge.source_resolved) is None
            or persistable_table(edge.target_table, True) is None
        ):
            continue
        key = routine_edge_key(edge)
        if key not in seen:
            seen.add(key)
            proposable.append(edge)
    return proposable


async def _procedure_lineage(run: TaskAgentRun) -> None:
    session = run.session
    already_parsed = exists().where(DeepProcedureLineageEdge.routine_id == MetadataRoutine.id)
    # A body examined since it last changed -- proposed from, or declined.
    already_examined = exists().where(
        AgentTask.organization_id == run.organization_id,
        AgentTask.agent_principal_id == run.principal_id,
        AgentTask.proposal_ref_type == _ROUTINE_REF_TYPE,
        AgentTask.proposal_ref_id == MetadataRoutine.id,
        AgentTask.started_at >= MetadataRoutine.updated_at,
    )
    filters: list[Any] = [
        MetadataRoutine.organization_id == run.organization_id,
        # `require_eligible_routine_body`, as a predicate; it is applied again
        # to each routine before its body is read.
        MetadataRoutine.status == "ACTIVE",
        MetadataRoutine.availability == AVAILABLE,
        MetadataRoutine.redaction_status == "PARSED",
        MetadataRoutine.screening_status == CLEAN,
        ~already_parsed,
        ~already_examined,
    ]
    if run.datasource_id is not None:
        filters.append(MetadataRoutine.datasource_id == run.datasource_id)
    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema, DataSource)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
            .where(*filters)
            .order_by(MetadataSchema.name, MetadataRoutine.name, MetadataRoutine.id)
            .limit(run.outcome.limit * _EXAMINE_FACTOR)
        )
    ).all()
    proposed = 0
    for routine, schema, datasource in rows:
        if proposed >= run.outcome.limit:
            return
        if not await run.may_continue():
            return
        item = run.add(
            await run.guarded(
                CAPABILITY_PROCEDURE_LINEAGE,
                subject_id=routine.id,
                subject_name=f"{schema.name}.{routine.name}",
                work=partial(_propose_procedure_lineage, run, routine, schema, datasource),
            )
        )
        if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
            proposed += 1


async def _propose_procedure_lineage(
    run: TaskAgentRun,
    routine: MetadataRoutine,
    schema: MetadataSchema,
    datasource: DataSource,
) -> TaskAgentItem:
    capability = CAPABILITY_PROCEDURE_LINEAGE
    session = run.session
    routine_id, routine_name = routine.id, f"{schema.name}.{routine.name}"
    datasource_id = datasource.id
    # Value-free: which routine, at which version of its body.
    inputs = {
        "capability": capability,
        "routine_id": str(routine_id),
        "body_fingerprint": routine.body_fingerprint or routine.fingerprint,
    }

    async def decline(reason: str) -> TaskAgentItem:
        return await run.declined(
            capability,
            subject_id=routine_id,
            subject_name=routine_name,
            reason=reason,
            inputs=inputs,
            proposal_ref_type=_ROUTINE_REF_TYPE,
            proposal_ref_id=routine_id,
        )

    result = parse_procedure_lineage(
        require_eligible_routine_body(routine), dialect=datasource.dialect
    )
    if any(error.startswith("unsupported dialect") for error in result.errors):
        return await decline(SKIP_UNSUPPORTED_DIALECT)
    proposable = proposable_procedure_edges(result)
    if not proposable:
        return await decline(SKIP_UNPARSEABLE if result.errors else SKIP_NO_LINEAGE)

    confidence = edge_confidence_as_float(result.confidence)
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=routine_id,
            subject_name=routine_name,
            confidence=confidence,
        )
    table_ids = await resolve_routine_table_ids(session, datasource_id, proposable)
    rows = [
        routine_edge_row(
            edge,
            organization_id=run.organization_id,
            datasource_id=datasource_id,
            routine_id=routine_id,
            sql_hash=result.sql_hash,
            table_ids=table_ids,
            # Never ACTIVE, whatever the review mode or the confidence: an
            # agent's edge is decided by a person. See the module docstring.
            review_status="PROPOSED",
            created_by=run.principal_id,
        )
        for edge in proposable
    ]
    session.add_all(rows)
    await session.flush()
    return await run.proposed_in_queue(
        capability,
        proposal_ref_type=_ROUTINE_REF_TYPE,
        proposal_ref_id=routine_id,
        subject_id=routine_id,
        subject_name=routine_name,
        inputs=inputs,
        pending_added=len(rows),
        evidence={
            "edge_ids": [str(row.id) for row in rows],
            "edge_count": len(rows),
            # Markers, plumbing, result sets and unresolved sources.
            "withheld_edges": len(result.edges) - len(rows),
            "statement_count": result.statement_count,
            "is_fully_parsed": result.is_fully_parsed,
            "sql_hash": result.sql_hash,
        },
        confidence=confidence,
    )


LINEAGE_WORK: Final[Mapping[str, CapabilityWork]] = {
    CAPABILITY_VIEW_LINEAGE: _view_lineage,
    CAPABILITY_PROCEDURE_LINEAGE: _procedure_lineage,
}


async def run_lineage_agent(
    session: AsyncSession,
    organization_id: UUID,
    *,
    request: TaskAgentRunRequest,
    settings: Settings,
    triggered_by: SecurityContext,
) -> TaskAgentOutcome:
    """One bounded lineage run; see `task_agent.run_task_agent`."""
    return await run_task_agent(
        session,
        organization_id,
        spec=LINEAGE_AGENT,
        work=LINEAGE_WORK,
        request=request,
        settings=settings,
        triggered_by=triggered_by,
    )
