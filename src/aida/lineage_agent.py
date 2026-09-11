"""ADR-0029: the lineage agent.

A task agent (`aida.task_agent`) -- `agent:lineage` by default -- that closes a
gap nothing else closed: the platform captures every view's definition at
ingestion (`MetadataViewDefinition`) and then never parses it. Lineage from a
view existed only when a person pasted its SQL into the parse endpoint.

One capability, VIEW_LINEAGE:

* **Selection.** Views whose captured definition is eligible -- ACTIVE,
  AVAILABLE, literal-redacted and screened CLEAN, the gate
  `view_tool_blueprint` applies before any view text is used -- that have no
  parsed lineage edge targeting them yet, in any review state, and whose
  current definition the agent has not already examined.
* **Parsing.** `sql_lineage_parser.parse_view_lineage` on the redacted
  definition, wrapped as `CREATE VIEW <schema>.<view> AS ...` when the connector
  captured only the body, so every edge targets the view itself rather than the
  parser's `<RESULT>` placeholder. An edge whose source the parser could not
  resolve is not proposed.
* **Proposal.** Each edge lands in `view_lineage_edge` as PROPOSED, always, with
  `created_by` the agent's identity -- whatever `lineage_parsed_edges_review_mode`
  or the high-confidence auto-activation threshold say. Those settings govern
  what a *person's* parse may activate. An agent's output is decided by a
  person in ADR-0026's per-edge queue, whose maker-checker refuses the agent as
  the reviewer of its own edge.
* **Negative knowledge.** A view with any edge targeting it -- including one a
  reviewer rejected -- is not re-parsed by the agent. A definition it could not
  turn into lineage is recorded once, so the same dead end is not re-examined on
  every run until the definition changes.

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

from aida.envelope_models import AVAILABLE, MetadataViewDefinition
from aida.ingest_screening import CLEAN
from aida.lineage_table_resolution import resolve_lineage_table_ids
from aida.models import AgentTask, DataSource, MetadataSchema, MetadataTable, ViewLineageEdge
from aida.parsed_lineage_review_service import edge_confidence_as_float
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
#: What the queue decides: one parsed column-level edge.
EDGE_OBJECT_TYPE: Final = "VIEW_LINEAGE_EDGE"
#: The unit of work the ledger links to: the definition that was parsed.
_PROPOSAL_REF_TYPE: Final = "VIEW_DEFINITION"

# Skips: a view the agent examined and deliberately left alone.
SKIP_UNSUPPORTED_DIALECT: Final = "unsupported_dialect"
SKIP_UNPARSEABLE: Final = "unparseable_definition"
SKIP_NO_LINEAGE: Final = "no_resolvable_lineage"
SKIP_LINEAGE_KNOWN: Final = "lineage_already_known"

#: Views examined per run, as a multiple of the proposal limit.
_EXAMINE_FACTOR: Final = 4


async def _pending_edges(session: AsyncSession, organization_id: UUID, principal: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(ViewLineageEdge)
        .where(
            ViewLineageEdge.organization_id == organization_id,
            ViewLineageEdge.created_by == principal,
            ViewLineageEdge.review_status == "PROPOSED",
        )
    )
    return int(count or 0)


async def _edge_outcomes(
    session: AsyncSession, organization_id: UUID, principal: str
) -> list[TaskAgentOutcomeRow]:
    """Its edges by review state: PROPOSED is pending, ACTIVE approved,
    REJECTED rejected."""
    rows = (
        await session.execute(
            select(ViewLineageEdge.review_status, func.count())
            .where(
                ViewLineageEdge.organization_id == organization_id,
                ViewLineageEdge.created_by == principal,
            )
            .group_by(ViewLineageEdge.review_status)
        )
    ).all()
    if not rows:
        return []
    by_status: Counter[str] = Counter({str(status): int(count) for status, count in rows})
    pending = by_status.pop("PROPOSED", 0)
    approved = by_status.pop("ACTIVE", 0)
    rejected = by_status.pop("REJECTED", 0)
    return [
        TaskAgentOutcomeRow(
            object_type=EDGE_OBJECT_TYPE,
            pending=pending,
            approved=approved,
            rejected=rejected,
            other=sum(by_status.values()),
        )
    ]


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


LINEAGE_WORK: Final[Mapping[str, CapabilityWork]] = {
    CAPABILITY_VIEW_LINEAGE: _view_lineage,
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
