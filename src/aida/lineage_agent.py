"""ADR-0029: the lineage agent.

A task agent (`aida.task_agent`) -- `agent:lineage` by default -- that closes a
gap nothing else closed: the platform captures every view's definition and every
routine's body at ingestion (`MetadataViewDefinition`, `MetadataRoutine`) and
then never parses them. Lineage from either existed only when a person asked
for a parse.

Three capabilities:

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
* **TRIGGER_LINEAGE** (R11-FP01). Triggers whose stored lineage is older than the
  trigger row itself, which includes having none. `procedure_lineage.parse_trigger_lineage`
  parses the same kind of body through the same gate, with one thing the other two
  do not have: the firing table is bound to the body's firing-row names
  (`NEW`/`OLD`, `INSERTED`/`DELETED`) before any edge is built, so a trigger that
  writes an audit table states a path *from the table it fires on*. On PostgreSQL,
  where a trigger has no body at all, `routine_lineage_edges.trigger_body` follows
  `action_routine` to the function that does and records which one it read; a
  function it cannot reach becomes a marker naming why, never silence. What the
  parser cannot bind -- Oracle's `:NEW` -- is an UNPARSED marker too. Edges land in
  `trigger_lineage_edge`.

All three:

* **Proposal.** Each edge lands PROPOSED, always, with `created_by` the
  agent's identity -- whatever `lineage_parsed_edges_review_mode` or the
  high-confidence auto-activation threshold say. Those settings govern what a
  *person's* parse may activate. An agent's output is decided by a person in
  ADR-0026's per-edge queue, whose maker-checker refuses the agent as the
  reviewer of its own edge. An edge whose source the parser could not resolve
  is not proposed.
* **Negative knowledge.** An object with any lineage edge -- including one a
  reviewer rejected -- is not re-parsed by the agent, unless (R11-FP16) the source
  redefined it structurally or it returned after its newest edge was written: that
  lineage describes a definition that no longer exists. A literal-only change does
  not count, because lineage does not depend on literals. A definition or body it
  could not turn into lineage is recorded once, so the same dead end is not
  re-examined on every run until it changes.

**When a trigger's lineage is stale.** A trigger's own row carries no change
signal, so its `updated_at` stands in for its own definition: a parse older than
the last rewrite of the row may describe a trigger that is gone. That is the whole
story on SQL Server and Oracle, whose triggers carry their own bodies. It is not on
PostgreSQL, where the body is the *function* the trigger names: `CREATE OR REPLACE
FUNCTION` rewrites the routine row and leaves the trigger row exactly as it was.
The routine axis already records that change, once, as a `ROUTINE` change signal
against the object that actually changed -- so staleness is derived from it rather
than duplicated into a TRIGGER subject kind: a trigger whose parse read routine R
(`trigger_parse_coverage.routine_id`, or an edge's `routine_id`) is re-examined when
R has a structural redefinition or a return newer than that parse, under exactly the
R11-FP16 rule the routine axis uses, literal-only changes excluded. A function the
trigger names that could not be reached at all (not captured here, or ambiguous)
has no id to join through, so a trigger in that state is re-examined when a routine
of that name in the same source changes after its measurement -- which is what
makes "rescan once the function is captured" actually close that gap. Every proposing
examination records `trigger_parse_coverage` before any decline, so "parsed since the
change" is also "examined since the change".

Nothing here calls a model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import CHANGE_STRUCTURAL, SIGNAL_DEFINITION_CHANGED, SIGNAL_REACTIVATED
from aida.cost_metrics import Parser, classify_parse, parser_span, record_parser_spend
from aida.envelope_models import (
    AVAILABLE,
    MetadataRoutine,
    MetadataTrigger,
    MetadataViewDefinition,
)
from aida.ingest_screening import CLEAN
from aida.lineage_table_resolution import resolve_lineage_table_ids
from aida.models import AgentTask, DataSource, MetadataSchema, MetadataTable, ViewLineageEdge
from aida.parsed_lineage_review_service import edge_confidence_as_float
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    parse_procedure_lineage,
    parse_trigger_lineage,
)
from aida.procedure_lineage_models import (
    DeepProcedureLineageEdge,
    TriggerLineageEdge,
    TriggerParseCoverage,
)
from aida.routine_call_descent import descend_routine_calls
from aida.routine_lineage_edges import (
    SUPERSEDED,
    DecidedEdgeReconciliation,
    RoutineEdgeKey,
    persist_trigger_edges,
    persistable_table,
    reconcile_decided_edges,
    record_routine_parse_coverage,
    record_trigger_parse_coverage,
    require_eligible_routine_body,
    resolve_routine_table_ids,
    routine_edge_key,
    routine_edge_row,
    trigger_body,
    unreachable_body_marker,
)
from aida.security import SecurityContext
from aida.sql_lineage_parser import parse_view_lineage
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES
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
CAPABILITY_TRIGGER_LINEAGE: Final = "TRIGGER_LINEAGE"
#: What the queue decides: one parsed column-level edge, from a view ...
EDGE_OBJECT_TYPE: Final = "VIEW_LINEAGE_EDGE"
#: ... or from a routine.
PROCEDURE_EDGE_OBJECT_TYPE: Final = "PROCEDURE_LINEAGE_EDGE"
#: ... or from a trigger (R11-FP01).
TRIGGER_EDGE_OBJECT_TYPE: Final = "TRIGGER_LINEAGE_EDGE"
#: The unit of work the ledger links to: the definition, the routine, or the
#: trigger, parsed.
_PROPOSAL_REF_TYPE: Final = "VIEW_DEFINITION"
_ROUTINE_REF_TYPE: Final = "ROUTINE"
_TRIGGER_REF_TYPE: Final = "TRIGGER"

# Skips: an object the agent examined and deliberately left alone.
SKIP_UNSUPPORTED_DIALECT: Final = "unsupported_dialect"
SKIP_UNPARSEABLE: Final = "unparseable_definition"
SKIP_NO_LINEAGE: Final = "no_resolvable_lineage"
SKIP_LINEAGE_KNOWN: Final = "lineage_already_known"

#: Objects examined per run, as a multiple of the proposal limit.
_EXAMINE_FACTOR: Final = 4


def _reparse_signal() -> ColumnElement[bool]:
    """R11-FP16: the change signals after which existing lineage no longer describes the
    definition -- a structural redefinition, or the object returning. Literal-only changes do
    not count: lineage does not depend on literals."""
    return or_(
        and_(
            MetadataChangeSignal.signal_type == SIGNAL_DEFINITION_CHANGED,
            MetadataChangeSignal.change_class == CHANGE_STRUCTURAL,
        ),
        MetadataChangeSignal.signal_type == SIGNAL_REACTIVATED,
    )

#: The edge tables the agent writes, by the object type its outcomes name.
_EDGE_TABLES: Final[tuple[tuple[str, Any], ...]] = (
    (EDGE_OBJECT_TYPE, ViewLineageEdge),
    (PROCEDURE_EDGE_OBJECT_TYPE, DeepProcedureLineageEdge),
    (TRIGGER_EDGE_OBJECT_TYPE, TriggerLineageEdge),
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
        TaskAgentCapability(
            key=CAPABILITY_TRIGGER_LINEAGE,
            object_type=TRIGGER_EDGE_OBJECT_TYPE,
            intent="lineage.propose_trigger_lineage",
            producer=(
                "procedure_lineage: trigger bodies captured at ingestion, with the "
                "firing table bound as their implicit subject"
            ),
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
    # R11-FP16: ...unless the source redefined the view structurally, or it returned, after
    # its newest edge was written -- that lineage describes a definition that no longer exists.
    redefined_since_parsed = exists().where(
        MetadataChangeSignal.subject_kind == "VIEW",
        MetadataChangeSignal.subject_id == MetadataTable.id,
        _reparse_signal(),
        ~exists().where(
            ViewLineageEdge.target_table_id == MetadataTable.id,
            ViewLineageEdge.created_at >= MetadataChangeSignal.detected_at,
        ),
    )
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
        or_(~already_parsed, redefined_since_parsed),
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
    # R11-FP17: the per-source half of parser cost. `parse_view_lineage` times
    # itself and counts the parse fleet-wide by dialect, but it deliberately
    # does not know which source a definition came from (AT-D2 -- it is
    # catalog-free); this function does. Recorded before any decline below and
    # for the same reason F06.4's measurement is: an unsupported dialect or an
    # unreadable definition still cost a parse, and a cost record that only
    # counted the successes would understate exactly the sources that are
    # expensive because nothing about them works.
    await record_parser_spend(
        session,
        organization_id=run.organization_id,
        datasource_id=datasource_id,
        statements=1,
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
    # R11-FP16: ...unless the body was redefined structurally, or the routine returned, after
    # its newest edge was written.
    redefined_since_parsed = exists().where(
        MetadataChangeSignal.subject_kind == "ROUTINE",
        MetadataChangeSignal.subject_id == MetadataRoutine.id,
        _reparse_signal(),
        ~exists().where(
            DeepProcedureLineageEdge.routine_id == MetadataRoutine.id,
            DeepProcedureLineageEdge.created_at >= MetadataChangeSignal.detected_at,
        ),
    )
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
        MetadataRoutine.redaction_status.in_(sorted(VALUE_FREE_REDACTION_STATUSES)),
        MetadataRoutine.screening_status == CLEAN,
        or_(~already_parsed, redefined_since_parsed),
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

    # R11-FP07: a call to a routine captured here is read through, not left as a gap.
    #
    # R11-FP17: the root parse is wrapped in a `parser_span` because
    # `aida.procedure_lineage` -- unlike `sql_lineage_parser` -- is not
    # instrumented internally, so its cost is timed at the call sites that
    # invoke it. `statement_count` is the real figure here, not the 1 a view
    # definition contributes: a procedure body is many statements and its parse
    # cost scales with them, which is the whole reason a change burst over
    # routines is more expensive than one over views.
    body_sql = require_eligible_routine_body(routine)
    with parser_span(
        Parser.PROCEDURE_LINEAGE, dialect=datasource.dialect, sql=body_sql
    ) as parse_span:
        root = parse_procedure_lineage(body_sql, dialect=datasource.dialect)
        parse_span.observed(
            classify_parse(root.errors, has_edges=bool(root.edges)),
            statements=max(1, root.statement_count),
        )
    await record_parser_spend(
        session,
        organization_id=run.organization_id,
        datasource_id=datasource_id,
        statements=max(1, root.statement_count),
    )
    result = await descend_routine_calls(session, datasource, routine, root)
    # F06.4: the measurement is recorded before any decline below, because the
    # routines this agent declines -- an unparseable body, an unsupported
    # dialect, a body with no lineage to propose -- are exactly the ones whose
    # coverage a reader needs. It is a measurement, not a proposal, so it is
    # still only written on a proposing run: a dry run reports what it would do
    # and writes nothing.
    if run.proposing:
        await record_routine_parse_coverage(
            session,
            datasource=datasource,
            routine=routine,
            result=result,
            measured_by=run.principal_id,
        )
    if any(error.startswith("unsupported dialect") for error in result.errors):
        return await decline(SKIP_UNSUPPORTED_DIALECT)
    proposable = proposable_procedure_edges(result)
    reconciliation = DecidedEdgeReconciliation()
    fresh = proposable
    if run.proposing:
        # R11-FP16: a re-examination replaces this routine's undecided proposals and keeps what
        # a person decided. It used to insert every edge afresh, and an edge the redefined body
        # still wrote collided with its decided row on the unique natural key -- the item
        # failed with an IntegrityError, in exactly the case re-examination exists for.
        await session.execute(
            delete(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.organization_id == run.organization_id,
                DeepProcedureLineageEdge.datasource_id == datasource_id,
                DeepProcedureLineageEdge.routine_id == routine_id,
                DeepProcedureLineageEdge.review_status == "PROPOSED",
            )
        )
        reconciliation = await reconcile_decided_edges(
            session,
            model=DeepProcedureLineageEdge,
            datasource=datasource,
            owner_id=routine_id,
            result=result,
            produced=proposable,
        )
        decided = {
            routine_edge_key(row)
            for row in (
                await session.scalars(
                    select(DeepProcedureLineageEdge).where(
                        DeepProcedureLineageEdge.organization_id == run.organization_id,
                        DeepProcedureLineageEdge.datasource_id == datasource_id,
                        DeepProcedureLineageEdge.routine_id == routine_id,
                    )
                )
            ).all()
        }
        fresh = [edge for edge in proposable if routine_edge_key(edge) not in decided]
    if not proposable:
        return await decline(SKIP_UNPARSEABLE if result.errors else SKIP_NO_LINEAGE)
    if not fresh and not reconciliation.revived:
        # Everything the body writes is already decided. Any edge it stopped writing was
        # superseded above; the item is declined, not failed.
        return await decline(SKIP_LINEAGE_KNOWN)

    confidence = edge_confidence_as_float(result.confidence)
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=routine_id,
            subject_name=routine_name,
            confidence=confidence,
        )
    table_ids = await resolve_routine_table_ids(session, datasource_id, fresh)
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
        for edge in fresh
    ]
    session.add_all(rows)
    await session.flush()
    queued = [*rows, *reconciliation.revived]
    return await run.proposed_in_queue(
        capability,
        proposal_ref_type=_ROUTINE_REF_TYPE,
        proposal_ref_id=routine_id,
        subject_id=routine_id,
        subject_name=routine_name,
        inputs=inputs,
        pending_added=len(queued),
        evidence={
            "edge_ids": [str(row.id) for row in queued],
            "edge_count": len(queued),
            # Markers, plumbing, result sets and unresolved sources.
            "withheld_edges": len(result.edges) - len(proposable),
            # R11-FP16: what this re-examination did to lineage a person had decided.
            "superseded_edge_ids": [str(row.id) for row in reconciliation.superseded],
            "revived_edge_ids": [str(row.id) for row in reconciliation.revived],
            "statement_count": result.statement_count,
            "is_fully_parsed": result.is_fully_parsed,
            "sql_hash": result.sql_hash,
        },
        confidence=confidence,
    )


def _trigger_parsed_since(moment: Any) -> ColumnElement[bool]:
    """The (correlated) trigger was parsed at or after `moment`: its coverage was
    measured then, or -- for a trigger parsed before coverage existed -- an edge of
    it was written then. Both restate the trigger's organization (INV-5)."""
    return or_(
        exists().where(
            TriggerParseCoverage.organization_id == MetadataTrigger.organization_id,
            TriggerParseCoverage.trigger_id == MetadataTrigger.id,
            TriggerParseCoverage.parsed_at >= moment,
        ),
        exists().where(
            TriggerLineageEdge.organization_id == MetadataTrigger.organization_id,
            TriggerLineageEdge.trigger_id == MetadataTrigger.id,
            TriggerLineageEdge.created_at >= moment,
        ),
    )


def _trigger_body_redefined_since_parsed() -> ColumnElement[bool]:
    """Gap 4 of R11-FP01: the routine a trigger's body was read from was redefined
    structurally, or returned, after the trigger was last parsed.

    The signal is the routine axis's own -- `subject_kind = 'ROUTINE'`, written by
    ingestion when the function's text moved -- read through the join the parse
    left behind: `trigger_parse_coverage.routine_id`, or an edge's `routine_id` for
    a trigger parsed before coverage was recorded. `_reparse_signal` is the same
    predicate the routine axis applies, so a literal-only change re-examines a
    trigger exactly as often as it re-examines the routine: never.
    """
    read_this_routine = or_(
        exists().where(
            TriggerParseCoverage.organization_id == MetadataTrigger.organization_id,
            TriggerParseCoverage.trigger_id == MetadataTrigger.id,
            TriggerParseCoverage.routine_id == MetadataChangeSignal.subject_id,
        ),
        exists().where(
            TriggerLineageEdge.organization_id == MetadataTrigger.organization_id,
            TriggerLineageEdge.trigger_id == MetadataTrigger.id,
            TriggerLineageEdge.routine_id == MetadataChangeSignal.subject_id,
        ),
    )
    return exists().where(
        MetadataChangeSignal.organization_id == MetadataTrigger.organization_id,
        MetadataChangeSignal.datasource_id == MetadataTrigger.datasource_id,
        MetadataChangeSignal.subject_kind == "ROUTINE",
        _reparse_signal(),
        read_this_routine,
        ~_trigger_parsed_since(MetadataChangeSignal.detected_at),
    )


def _trigger_body_may_have_arrived() -> ColumnElement[bool]:
    """A trigger whose last parse could not reach the function it names -- not
    captured here, or ambiguous, so the measurement holds no `routine_id` to join a
    signal through -- when a routine of that name in the same source was written
    after that measurement.

    A newly captured routine is not a change signal (nothing can depend on an
    object that did not exist), so without this the marker saying "not captured"
    would outlive the capture forever, and the gap register's advice for it --
    widen the selection and rescan -- would not close it. The name test is a
    containment match, so it may over-include a similarly named routine; that costs
    one re-examination, which records a fresh measurement and stops. It can never
    under-include a routine the join in `routine_lineage_edges.trigger_body` would
    find.
    """
    return exists().where(
        TriggerParseCoverage.organization_id == MetadataTrigger.organization_id,
        TriggerParseCoverage.trigger_id == MetadataTrigger.id,
        TriggerParseCoverage.routine_id.is_(None),
        MetadataTrigger.availability != AVAILABLE,
        exists().where(
            MetadataRoutine.organization_id == MetadataTrigger.organization_id,
            MetadataRoutine.datasource_id == MetadataTrigger.datasource_id,
            MetadataRoutine.updated_at > TriggerParseCoverage.parsed_at,
            func.lower(MetadataTrigger.action_routine).contains(func.lower(MetadataRoutine.name)),
        ),
    )


async def _trigger_lineage(run: TaskAgentRun) -> None:
    session = run.session
    # R11-FP01: a trigger's code is either its own body (SQL Server, Oracle) or
    # the function `action_routine` names (PostgreSQL). Either makes it a
    # candidate; a trigger with neither is not one, because there is nothing to
    # hand the parser. `trigger_body` applies the real gate to whichever it is.
    has_own_body = and_(
        MetadataTrigger.availability == AVAILABLE,
        MetadataTrigger.redaction_status.in_(sorted(VALUE_FREE_REDACTION_STATUSES)),
        MetadataTrigger.screening_status == CLEAN,
    )
    names_a_routine = and_(
        MetadataTrigger.action_routine.is_not(None),
        MetadataTrigger.action_routine != "",
    )
    # 1. The trigger row itself changed -- or was never parsed: no parse newer
    # than the row's last rewrite. See the module docstring for why `updated_at`
    # stands in for a change signal on this half.
    row_unparsed_or_stale = ~_trigger_parsed_since(MetadataTrigger.updated_at)
    # A trigger examined since its row last changed -- proposed from, or declined.
    already_examined = exists().where(
        AgentTask.organization_id == run.organization_id,
        AgentTask.agent_principal_id == run.principal_id,
        AgentTask.proposal_ref_type == _TRIGGER_REF_TYPE,
        AgentTask.proposal_ref_id == MetadataTrigger.id,
        AgentTask.started_at >= MetadataTrigger.updated_at,
    )
    filters: list[Any] = [
        MetadataTrigger.organization_id == run.organization_id,
        MetadataTrigger.status == "ACTIVE",
        or_(has_own_body, names_a_routine),
        or_(
            and_(row_unparsed_or_stale, ~already_examined),
            # 2. The body changed while the row did not (PostgreSQL).
            _trigger_body_redefined_since_parsed(),
            # 3. The body could not be reached, and a routine that may be it has
            # changed since.
            and_(names_a_routine, _trigger_body_may_have_arrived()),
        ),
    ]
    if run.datasource_id is not None:
        filters.append(MetadataTrigger.datasource_id == run.datasource_id)
    rows = (
        await session.execute(
            select(MetadataTrigger, MetadataSchema, DataSource)
            .join(MetadataSchema, MetadataSchema.id == MetadataTrigger.schema_id)
            .join(DataSource, DataSource.id == MetadataTrigger.datasource_id)
            .where(*filters)
            .order_by(MetadataSchema.name, MetadataTrigger.name, MetadataTrigger.id)
            .limit(run.outcome.limit * _EXAMINE_FACTOR)
        )
    ).all()
    proposed = 0
    for trigger, schema, datasource in rows:
        if proposed >= run.outcome.limit:
            return
        if not await run.may_continue():
            return
        item = run.add(
            await run.guarded(
                CAPABILITY_TRIGGER_LINEAGE,
                subject_id=trigger.id,
                subject_name=f"{schema.name}.{trigger.name}",
                work=partial(_propose_trigger_lineage, run, trigger, schema, datasource),
            )
        )
        if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
            proposed += 1


async def _propose_trigger_lineage(
    run: TaskAgentRun,
    trigger: MetadataTrigger,
    schema: MetadataSchema,
    datasource: DataSource,
) -> TaskAgentItem:
    capability = CAPABILITY_TRIGGER_LINEAGE
    session = run.session
    trigger_id, trigger_name = trigger.id, f"{schema.name}.{trigger.name}"
    datasource_id = datasource.id
    # Value-free: which trigger, at which version of its definition.
    inputs = {
        "capability": capability,
        "trigger_id": str(trigger_id),
        "body_fingerprint": trigger.body_fingerprint or trigger.fingerprint,
    }

    async def decline(reason: str) -> TaskAgentItem:
        return await run.declined(
            capability,
            subject_id=trigger_id,
            subject_name=trigger_name,
            reason=reason,
            inputs=inputs,
            proposal_ref_type=_TRIGGER_REF_TYPE,
            proposal_ref_id=trigger_id,
        )

    body = await trigger_body(session, datasource, trigger)
    if body.sql is None:
        # PostgreSQL's action routine is not captured here, or its body is
        # withheld. Recorded as a marker rather than as nothing: a trigger whose
        # code cannot be read is a gap, and zero edges would read as a trigger
        # that touches nothing.
        result = unreachable_body_marker(
            body, dialect=datasource.dialect, sql_hash=trigger.body_fingerprint or ""
        )
    else:
        # R11-FP17: the same cost record a routine parse makes, at the same
        # granularity. The label is `procedure_lineage` because that is the parse
        # this runs -- `parse_trigger_lineage` binds the subject and delegates.
        with parser_span(
            Parser.PROCEDURE_LINEAGE, dialect=datasource.dialect, sql=body.sql
        ) as parse_span:
            result = parse_trigger_lineage(
                body.sql, dialect=datasource.dialect, firing_table=body.firing_table
            )
            parse_span.observed(
                classify_parse(result.errors, has_edges=bool(result.edges)),
                statements=max(1, result.statement_count),
            )
    await record_parser_spend(
        session,
        organization_id=run.organization_id,
        datasource_id=datasource_id,
        statements=max(1, result.statement_count),
    )
    # The measurement, before any decline -- the routine axis's F06.4 rule, for
    # the same reason: the triggers declined below (unsupported dialect, a body
    # with nothing to propose, a function nobody captured) are exactly the ones
    # whose coverage a reader needs. `routine_id` is the join a later change to
    # that function uses to find this trigger again. Written only on a proposing
    # run: a dry run reports and writes nothing.
    if run.proposing:
        await record_trigger_parse_coverage(
            session,
            datasource=datasource,
            trigger=trigger,
            result=result,
            routine_id=body.routine_id,
            measured_by=run.principal_id,
        )
    if any(error.startswith("unsupported dialect") for error in result.errors):
        return await decline(SKIP_UNSUPPORTED_DIALECT)

    proposable = proposable_procedure_edges(result)
    if not run.proposing:
        if not proposable:
            return await decline(SKIP_UNPARSEABLE if result.errors else SKIP_NO_LINEAGE)
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=trigger_id,
            subject_name=trigger_name,
            confidence=edge_confidence_as_float(result.confidence),
        )

    # One writer for both halves, so the trigger's rows are replaced rather than
    # duplicated on a re-parse and the natural key cannot be violated.
    # `agent_proposal` makes every real edge PROPOSED whatever the organization's
    # review mode says; a marker stays ACTIVE because it is a gap, not an edge.
    #
    # What reaches the writer is what a person can decide plus the gaps, and
    # nothing else -- the same table-to-table cut `_propose_procedure_lineage`
    # proposes. A hop into or out of a temp table, a `<RESULT>` set and an edge
    # whose source the parser could not resolve are the body's own plumbing: put
    # in the per-edge queue they are rows a reviewer can only rubber-stamp or
    # guess at, and approving one would change nothing the graph can use.
    gaps = [
        edge for edge in result.edges if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]
    written = await persist_trigger_edges(
        session,
        datasource=datasource,
        trigger=trigger,
        result=replace(result, edges=[*proposable, *gaps]),
        review_mode="require_review",
        threshold=1.0,
        created_by=run.principal_id,
        routine_id=body.routine_id,
        agent_proposal=True,
    )
    await session.flush()
    edges = [row for row in written if row.review_status == "PROPOSED"]
    markers = [
        row for row in written if row.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]
    superseded = [row for row in written if row.review_status == SUPERSEDED]
    if not edges:
        # The gap is still recorded above; only the *proposal* is declined. A body whose
        # every edge is already decided is known lineage, not unresolvable lineage.
        if proposable:
            return await decline(SKIP_LINEAGE_KNOWN)
        return await decline(SKIP_UNPARSEABLE if result.errors else SKIP_NO_LINEAGE)
    return await run.proposed_in_queue(
        capability,
        proposal_ref_type=_TRIGGER_REF_TYPE,
        proposal_ref_id=trigger_id,
        subject_id=trigger_id,
        subject_name=trigger_name,
        inputs=inputs,
        pending_added=len(edges),
        evidence={
            "edge_ids": [str(row.id) for row in edges],
            "edge_count": len(edges),
            # Markers, plumbing, result sets and unresolved sources.
            "withheld_edges": len(result.edges) - len(edges),
            "unparsed_statements": len(markers),
            # R11-FP01: approved lineage this complete re-parse no longer found.
            "superseded_edge_ids": [str(row.id) for row in superseded],
            "statement_count": result.statement_count,
            "is_fully_parsed": result.is_fully_parsed,
            "body_reached": body.sql is not None,
            "via_routine": body.via_routine,
            "sql_hash": result.sql_hash,
        },
        confidence=edge_confidence_as_float(result.confidence),
    )


LINEAGE_WORK: Final[Mapping[str, CapabilityWork]] = {
    CAPABILITY_VIEW_LINEAGE: _view_lineage,
    CAPABILITY_PROCEDURE_LINEAGE: _procedure_lineage,
    CAPABILITY_TRIGGER_LINEAGE: _trigger_lineage,
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
