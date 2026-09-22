"""ING-4 / P0-01: auto-enqueue AI drafts on new-table ingest.

**Problem.** The 2026-08-30 end-to-end audit found that a newly-ingested
table sits with no asset-description draft, no business-annotation
proposal, and no glossary-link candidate until a steward manually POSTs
each drafter endpoint (see
`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` finding P0-01 and
`Docs/60-delivery/10-session-2026-09-04-auto-enqueue.md`). The ingest
activity itself finishes at `_get_or_create_table`, returning
`created_table_ids` to the caller, but nothing consumes that list to
enqueue the drafters.

**Fix.** `persist_discovery_snapshot` now emits a
`catalog.table.newly_created.v1` outbox event for every table it
actually created (see `_emit_newly_created_table_events` in
`aida.workflows.activities`). This module owns the receive-side: it
consumes that event from the shared `aida.platform.events.v1` Kafka
topic (same topic the graph projector reads from) and, for each event,
calls the *service* functions of the two drafters directly -- never
their HTTP endpoints -- so no security context or bearer token is
required, only a DB session and the worker principal.

**Idempotency.** The handler is idempotent by construction:

- The asset-description drafter is skipped when the table already has
  an APPROVED `AssetDocumentationVersion` (a stewarded description is
  the source of truth and must never be overwritten by an AI draft) or
  when it already has an open (`DRAFT` / `PENDING_APPROVAL`) draft (the
  handler must not stack redundant drafts on top of one another).
- The semantic-inference drafter is skipped when no `AnalysisRun` has
  reached `COMPLETED` for the datasource yet (business inference reads
  profile summaries the profiling phase writes, so it *has* to wait --
  the same 409 gate `create_semantic_inference_run` enforces on the
  HTTP path); this is a defer, not a failure, and a later completion
  event picks the table back up.

**Reachability.** `supervise_newly_created_table_drafter()` is
imported and started as a background asyncio task from
`aida.workflows.worker.run_worker` (the `aida.workflows.worker`
process, already an `ENTRY_POINTS` row in
`tests/test_reachability_gate.py`) so this module is reachable through
the existing worker entry point rather than becoming a new deployable.
It runs `run_newly_created_table_drafter_consumer()` -- one
connection's worth of work -- and runs it again when it fails.

**Failure handling (R11-AUD03).** The consumer needs a broker and the
default ten-service stack has none. Started bare, `consumer.start()`
raised inside a task nobody awaited until the worker shut down: the
task ended, nothing was logged, and automatic drafting for newly
created tables quietly never happened -- and when Redpanda came up
later, nothing noticed. The supervisor is the fix. It re-runs the
consumer whenever the consumer ends without having been asked to stop,
waiting 2 s and doubling to a 60 s cap between attempts, and it logs
every failed attempt as `newly_created_table_drafter_unavailable`
(`attempt`, `next_retry_seconds`, `bootstrap_servers`, `error_type`):
at ERROR for the first failure of an incident and every tenth attempt
after it, at WARNING in between, so a broker that stays away is loud
without becoming an error line per retry. A consumer that had been up
for a minute before it failed starts a new incident rather than
continuing a crash loop, and the backoff starts over. Nothing the
consumer raises can end the supervisor, so nothing it raises can take
the Temporal worker down with it; cancellation and the consumer's own
stopping state (the SIGINT / SIGTERM flag) are the only ways out.

**What a monitor can scrape (R11-AUD03).** The log lines above tell a person who
is reading them; two series tell an alert.
`aida_newly_created_table_drafter_consumer_up` is 1 while the consumer has
started and is consuming and 0 while it is not -- before its first start,
while it is being retried, after it ends. `..._failures_total` counts the
attempts that ended without a stop having been asked for, the same events the
`newly_created_table_drafter_unavailable` line reports. Both live in the
Temporal worker's registry and reach a scrape only when the worker opens its
metrics listener (`aida.worker_metrics`, `worker_metrics_port`, off by default).

The gauge carries one label, `consumer_group`, and that is not decoration. This
module is imported by the worker whether or not `auto_enqueue_on_ingest` is on,
and a gauge with no label is exported at 0 from the moment it is created: a
worker that had switched the feature off would publish "consumer down" forever,
and an alert on it would page for a decision somebody made. A labelled gauge has
no series until `.labels()` is first called, which the supervisor does when it
starts -- so the series exists exactly where the consumer is supposed to be
running, and an alert on `== 0` cannot fire anywhere else. Where it does fire is
the default stack: `auto_enqueue_on_ingest` is on, there is no broker, and 0 is
the truth.

What the gauge cannot see is a consumer that dies on the same message again and
again. It is 1 for the moment each attempt's `start()` has succeeded and 0 for
the rest of every backoff, so a scrape occasionally lands on the 1. The failures
counter is the reading that loop cannot hide from.

**Per-message semantics are unchanged, on purpose.** An exception
while handling a message leaves the `async for` before
`consumer.commit()`, so the offset stays where it was and the group
redelivers the message once the consumer is started again:
at-least-once, which `handle_newly_created_table` is written for
(idempotent; `enqueue_semantics_in_batches` resumes). What the
supervisor adds is that the restart now happens -- before it, the
exception ended the task and nothing ever redelivered. A message that
fails every time therefore holds its partition in a loud restart loop
at the capped backoff, where it used to stop the consumer silently for
good. Skipping or dead-lettering such a message means deciding which
failures are terminal and where the message goes, and it changes the
delivery guarantee, so it is a change of its own and not made here.

**Never do.** Never call `record_outbox()` from inside the handler
itself for the same event id -- that would put a downstream projector
into an emit-and-re-consume loop. Never call the HTTP drafter
endpoints; they require a user-scoped `SecurityContext` and would
force a bearer token onto a worker path that has none.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from prometheus_client import Counter, Gauge
from sqlalchemy import select

from aida.agent_contracts import REASON_CONTRACT_MISSING, agent_kill_blocking_reason
from aida.agent_tasks import record_agent_task
from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    compose_draft_text,
    evidence_payload,
    gather_evidence,
    score_evidence,
    text_fingerprint,
)
from aida.config import get_settings
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.models import (
    AnalysisRun,
    AssetDescriptionDraft,
    AssetDocumentation,
    AssetDocumentationVersion,
    DataSource,
    GovernanceReview,
    MetadataEnrichmentProposal,
    MetadataTable,
    SemanticInferenceRun,
)
from aida.schemas import SemanticInferenceRequest
from aida.security import SecurityContext
from aida.semantic_inference_service import generate_semantic_inference
from aida.steward_agent import STEWARD_AGENT
from aida.task_agent import (
    TaskAgentAuthority,
    TaskAgentRefused,
    mode_for,
    resolve_task_agent_authority,
)

logger = structlog.get_logger(__name__)


# Kept as a module-level constant so tests and the emitter side (in
# `aida.workflows.activities`) can key on the same string rather than
# each carrying its own copy.
NEWLY_CREATED_TABLE_EVENT_TYPE = "catalog.table.newly_created.v1"
_DRAFT_ENQUEUED_EVENT_TYPE = "asset_description.draft.auto_enqueued.v1"
_SEMANTIC_INFERENCE_DEFERRED_EVENT_TYPE = "business_semantics.inference.auto_enqueue_deferred.v1"
_AUTO_ENQUEUE_PRINCIPAL = "auto-enqueue-drafter"
# The two open-draft statuses `generate_asset_description_drafts` also
# treats as "already in flight; do not stack another one on top of this".
_OPEN_DRAFT_STATUSES = ("DRAFT", "PENDING_APPROVAL")
_APPROVED_DOC_STATUS = "APPROVED"

#: The Kafka consumer group this side-car reads the shared topic as. A constant because two
#: things must name the same group: the consumer that joins it and the label on the gauge that
#: says whether that consumer is up -- a label that drifted from the group would report on a
#: consumer that does not exist.
DRAFTER_CONSUMER_GROUP = "aida-newly-created-table-drafter-v1"

DRAFTER_CONSUMER_UP = Gauge(
    "aida_newly_created_table_drafter_consumer_up",
    (
        "1 while the newly-created-table drafter's Kafka consumer has started and is consuming, "
        "0 while it is not (before its first start, while it is being retried). Absent when the "
        "supervisor never ran, e.g. auto_enqueue_on_ingest is off. 0 is expected on the default "
        "stack, which has no broker."
    ),
    labelnames=("consumer_group",),
)
DRAFTER_CONSUMER_FAILURES = Counter(
    "aida_newly_created_table_drafter_failures_total",
    (
        "Attempts by the newly-created-table drafter's consumer that ended without a stop having "
        "been asked for: the broker was unreachable or dropped, or a message could not be "
        "handled. The supervisor restarts the consumer after each. A message that fails on every "
        "delivery shows here even when the up gauge is caught at 1."
    ),
)


def _set_consumer_up(value: int) -> None:
    """The one place the gauge is written, so its label is written the same way everywhere.

    The first call creates the series (see the module docstring on why that must not be at
    import): the supervisor makes it at start, the consumer moves it after that.
    """
    DRAFTER_CONSUMER_UP.labels(consumer_group=DRAFTER_CONSUMER_GROUP).set(value)


#: ADR-0029: the ledger intent of a draft made on ingest by an organization's
#: registered steward agent, and the stop reason when its contract is T0.
INTENT_DRAFT_ON_INGEST = "steward.draft_table_description_on_ingest"
REASON_STEWARD_OBSERVES_ONLY = "steward_agent_observes_only"


@dataclass(slots=True)
class DrafterConsumerState:
    """What the consumer and whoever supervises it share.

    `stopping` is flipped by the SIGINT / SIGTERM handler and read by both: the
    consumer stops after the message in hand, and the supervisor does not start
    another attempt. `started_at` is `time.monotonic()` at the moment the
    consumer's `start()` last succeeded; the supervisor clears it before each
    attempt and reads it afterwards, so "was it up for a while before it failed"
    is measured from a live connection and not from the attempt beginning (a
    `start()` that hangs for its own timeout must not count as uptime).
    """

    stopping: bool = False
    started_at: float | None = None


@dataclass(frozen=True, slots=True)
class StewardGovernance:
    """Whether this side-car drafts under the steward agent's contract (ADR-0029).

    An organization that registered the steward agent -- a contract for its
    principal on an approved version -- has put automated table drafting under
    that contract, and the side-car honours it: it drafts as the agent, stops
    when the agent's kill switch is engaged or its contract only observes (T0),
    and puts a reviewable draft in the queue as the agent's own request, the way
    the agent's own runs do. `authority` is then set, and `stopped_reason` when
    the contract forbids drafting now -- including a registration the runtime
    refuses (an unapproved version, two contracts).

    An organization with no contract at all has not opted in, and both stay
    unset: the side-car behaves exactly as it did before ADR-0029. That is what
    keeps this from being the behaviour change the ADR declined -- failing
    closed in every organization that never registered the agent.
    """

    authority: TaskAgentAuthority | None = None
    stopped_reason: str | None = None


async def steward_governance(session: Any, organization_id: UUID) -> StewardGovernance:
    try:
        authority = await resolve_task_agent_authority(
            session, organization_id, spec=STEWARD_AGENT, settings=get_settings()
        )
    except TaskAgentRefused as exc:
        if exc.reason_code == REASON_CONTRACT_MISSING:
            return StewardGovernance()
        return StewardGovernance(stopped_reason=exc.reason_code)
    blocking = await agent_kill_blocking_reason(session, authority.contract)
    if blocking is not None:
        return StewardGovernance(authority=authority, stopped_reason=blocking)
    if mode_for(authority.contract.autonomy_tier) != "PROPOSE":
        return StewardGovernance(authority=authority, stopped_reason=REASON_STEWARD_OBSERVES_ONLY)
    return StewardGovernance(authority=authority)


def _steward_context(organization_id: UUID, authority: TaskAgentAuthority) -> SecurityContext:
    return SecurityContext(
        principal_id=authority.principal_id,
        principal_type="AGENT",
        organization_id=organization_id,
        roles=STEWARD_AGENT.audit_roles,
    )


def _worker_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=_AUTO_ENQUEUE_PRINCIPAL,
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"MetadataWorker"}),
    )


async def _table_has_approved_description(session: Any, table_id: UUID) -> bool:
    """True when `table_id` already has an APPROVED, stewarded description.

    An approved description is the source of truth (see ADR-0018 / GL-9)
    and must never be silently overwritten by an AI draft; the handler
    skips such tables entirely rather than emit a lower-confidence
    proposal on top of them.
    """
    approved_id = await session.scalar(
        select(AssetDocumentationVersion.id)
        .join(
            AssetDocumentation,
            AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
        )
        .where(
            AssetDocumentation.table_id == table_id,
            AssetDocumentationVersion.status == _APPROVED_DOC_STATUS,
        )
        .limit(1)
    )
    return approved_id is not None


async def _table_has_open_draft(session: Any, table_id: UUID) -> bool:
    """True when an `AssetDescriptionDraft` for this table is still in
    review (`DRAFT` or `PENDING_APPROVAL`). Mirrors
    `generate_asset_description_drafts`'s `open_table_ids` gate so the
    auto-enqueue path never stacks a second draft on top of one that a
    steward is still working through.
    """
    open_id = await session.scalar(
        select(AssetDescriptionDraft.id)
        .where(
            AssetDescriptionDraft.table_id == table_id,
            AssetDescriptionDraft.status.in_(_OPEN_DRAFT_STATUSES),
        )
        .limit(1)
    )
    return open_id is not None


async def _table_has_any_draft(session: Any, table_id: UUID) -> bool:
    """Idempotency guard for the handler itself: True when an
    `AssetDescriptionDraft` of ANY status already exists for this table.

    Distinct from `_table_has_open_draft` (which mirrors the HTTP
    endpoint's skip contract and is retained separately so a rejected
    draft can still be regenerated on the next ingest, matching the
    endpoint's `skipped_duplicate_rejected` shape); this second check
    is what stops delivering the same
    `catalog.table.newly_created.v1` event twice from producing two
    identical `DRAFT` rows.
    """
    existing_id = await session.scalar(
        select(AssetDescriptionDraft.id).where(AssetDescriptionDraft.table_id == table_id).limit(1)
    )
    return existing_id is not None


async def _completed_analysis_run_id(session: Any, datasource_id: UUID) -> UUID | None:
    """Return the id of the most recently `COMPLETED` `AnalysisRun` for
    `datasource_id`, or None if none has completed yet. Mirrors the
    exact gate `create_semantic_inference_run` uses on the HTTP path
    (409 when absent); on the auto-enqueue path we defer instead of
    failing so a later completion event can pick this table back up.
    """
    run_id: UUID | None = await session.scalar(
        select(AnalysisRun.id)
        .where(
            AnalysisRun.datasource_id == datasource_id,
            AnalysisRun.status == "COMPLETED",
        )
        .order_by(AnalysisRun.updated_at.desc())
        .limit(1)
    )
    return run_id


async def enqueue_description_draft_for_table(
    session: Any,
    *,
    organization_id: UUID,
    table: MetadataTable,
) -> AssetDescriptionDraft | None:
    """Create one `AssetDescriptionDraft` for `table` by calling the same
    service functions the HTTP `generate_asset_description_drafts`
    endpoint calls (`gather_evidence`, `compose_draft_text`,
    `score_evidence`, `text_fingerprint`, `evidence_payload`). Returns
    the persisted draft, or `None` when it was intentionally skipped
    (APPROVED description already exists, open draft in review already
    exists, handler already produced one for this table, or the
    organization's steward agent contract stops it -- see
    `StewardGovernance`).
    """
    if await _table_has_approved_description(session, table.id):
        logger.info(
            "auto_enqueue_skipped_approved_description",
            table_id=str(table.id),
        )
        return None
    if await _table_has_open_draft(session, table.id):
        logger.info(
            "auto_enqueue_skipped_open_draft",
            table_id=str(table.id),
        )
        return None
    if await _table_has_any_draft(session, table.id):
        logger.info(
            "auto_enqueue_skipped_existing_draft",
            table_id=str(table.id),
        )
        return None
    governance = await steward_governance(session, organization_id)
    if governance.stopped_reason is not None:
        logger.info(
            "auto_enqueue_stopped_by_steward_agent",
            table_id=str(table.id),
            reason=governance.stopped_reason,
        )
        record_audit(
            session,
            _worker_context(organization_id),
            action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
            resource_type="TABLE",
            resource_id=str(table.id),
            outcome="SKIPPED",
            correlation_id=str(table.id),
            details={"reason": governance.stopped_reason, "steward_agent_contract": True},
        )
        return None
    authority = governance.authority
    evidence = await gather_evidence(session, table)
    drafted_text = compose_draft_text(evidence)
    fingerprint = text_fingerprint(drafted_text)
    scores = score_evidence(evidence)
    # Under the steward agent's contract a reviewable draft is its request for
    # review, exactly as in the agent's own runs; a thin one stays a DRAFT, as
    # it always did, because it could never be submitted.
    submitting = authority is not None and scores.overall >= MINIMUM_EVIDENCE_FOR_REVIEW
    draft = AssetDescriptionDraft(
        organization_id=organization_id,
        table_id=table.id,
        drafted_text=drafted_text,
        text_fingerprint=fingerprint,
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall,
        evidence=evidence_payload(evidence),
        status="PENDING_APPROVAL" if submitting else "DRAFT",
        created_by=authority.principal_id if authority is not None else _AUTO_ENQUEUE_PRINCIPAL,
    )
    session.add(draft)
    await session.flush()
    if authority is not None:
        await _record_under_steward_contract(
            session,
            organization_id=organization_id,
            authority=authority,
            draft=draft,
            submitting=submitting,
        )
    return draft


async def _record_under_steward_contract(
    session: Any,
    *,
    organization_id: UUID,
    authority: TaskAgentAuthority,
    draft: AssetDescriptionDraft,
    submitting: bool,
) -> None:
    """The review request and the ledger row the steward agent's own runs write,
    so a draft made on its behalf at ingest is decided, counted and sampled
    like one it proposed. `ASSET_DESCRIPTION_DRAFT` is T0, inside every agent's
    proposal ceiling."""
    review: GovernanceReview | None = None
    if submitting:
        review = GovernanceReview(
            organization_id=organization_id,
            object_type="ASSET_DESCRIPTION_DRAFT",
            object_id=str(draft.id),
            requested_action="PUBLISH",
            requested_by=authority.principal_id,
        )
        session.add(review)
        await session.flush()
        draft.governance_review_id = review.id
        record_audit(
            session,
            _steward_context(organization_id, authority),
            action=f"{STEWARD_AGENT.key}_agent.propose",
            resource_type="governance_review",
            resource_id=str(review.id),
            outcome="SUCCESS",
            correlation_id=str(draft.table_id),
            details={
                "object_type": "ASSET_DESCRIPTION_DRAFT",
                "object_id": str(draft.id),
                "trigger": NEWLY_CREATED_TABLE_EVENT_TYPE,
            },
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="governance_review",
            aggregate_id=str(review.id),
            event_type="governance.review_requested.v1",
            payload={
                "review_id": str(review.id),
                "object_type": "ASSET_DESCRIPTION_DRAFT",
                "object_id": str(draft.id),
                "requested_action": "PUBLISH",
            },
        )
    await record_agent_task(
        session,
        organization_id=organization_id,
        agent_principal_id=authority.principal_id,
        intent=INTENT_DRAFT_ON_INGEST,
        inputs={
            "table_id": str(draft.table_id),
            "text_fingerprint": draft.text_fingerprint,
            "trigger": NEWLY_CREATED_TABLE_EVENT_TYPE,
        },
        ai_asset_version_id=authority.version.id,
        proposal_ref_type="GOVERNANCE_REVIEW" if review is not None else "ASSET_DESCRIPTION_DRAFT",
        proposal_ref_id=review.id if review is not None else draft.id,
        sampling_rate=authority.contract.sampling_rate,
    )


async def handle_newly_created_table(session: Any, payload: dict[str, Any]) -> None:
    """Handle one decoded `catalog.table.newly_created.v1` event.

    Runs inside the caller's `AsyncSession` -- the caller commits (the
    Kafka-consumer loop below commits after `handle_newly_created_table`
    returns, then commits the Kafka offset; tests drive this function
    directly and commit themselves). Never raises for
    business-as-usual skips (already-described table, in-flight draft,
    profiling not yet complete) -- those are ordinary control-flow
    outcomes and are logged + audited, not thrown.

    An unexpected exception (DB write refused, evidence gathering hit
    a bad row, etc.) IS re-raised so the Kafka consumer records the
    failure and does not commit the offset; the event will be re-tried
    on the next batch. Per the P0-01 fix contract we never swallow --
    a failure must produce a DENIED audit row AND propagate so the
    outbox / Kafka delivery guarantees stay intact.
    """
    organization_id = UUID(payload["organization_id"])
    datasource_id = UUID(payload["datasource_id"])
    table_id = UUID(payload["table_id"])
    correlation_id = payload.get("analysis_run_id") or str(table_id)
    context = _worker_context(organization_id)
    table = await session.get(MetadataTable, table_id)
    if table is None or table.organization_id != organization_id:
        # A newly-created event whose table has since been deleted (or
        # whose payload's organization_id doesn't match the row) is a
        # legitimate no-op -- the deletion has retired the drafters'
        # subject. Audited so a spike of these is visible to ops.
        logger.info(
            "auto_enqueue_table_missing",
            table_id=str(table_id),
            organization_id=str(organization_id),
        )
        record_audit(
            session,
            context,
            action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
            resource_type="TABLE",
            resource_id=str(table_id),
            outcome="SKIPPED",
            correlation_id=correlation_id,
            details={"reason": "table_missing_or_tenant_mismatch"},
        )
        return
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        logger.info(
            "auto_enqueue_datasource_missing",
            datasource_id=str(datasource_id),
        )
        record_audit(
            session,
            context,
            action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
            resource_type="TABLE",
            resource_id=str(table_id),
            outcome="SKIPPED",
            correlation_id=correlation_id,
            details={"reason": "datasource_missing"},
        )
        return

    try:
        draft = await enqueue_description_draft_for_table(
            session,
            organization_id=organization_id,
            table=table,
        )
    except Exception as exc:
        # INV-6 shape: never log `str(exc)` into audit `details` or the
        # outbox payload -- keep to the exception type only; the same
        # pattern `discover_datasource`'s exception handler uses.
        logger.exception(
            "auto_enqueue_description_draft_failed",
            table_id=str(table_id),
            error_type=type(exc).__name__,
        )
        record_audit(
            session,
            context,
            action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
            resource_type="TABLE",
            resource_id=str(table_id),
            outcome="FAILED",
            correlation_id=correlation_id,
            details={
                "reason": "description_draft_error",
                "error_class": type(exc).__name__,
            },
        )
        raise
    if draft is not None:
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="asset_description_draft",
            aggregate_id=str(draft.id),
            event_type=_DRAFT_ENQUEUED_EVENT_TYPE,
            payload={
                "asset_description_draft_id": str(draft.id),
                "table_id": str(table_id),
                "datasource_id": str(datasource_id),
                "overall_score": draft.overall_score,
            },
        )

    # Semantic inference needs a COMPLETED AnalysisRun (mirrors the
    # HTTP endpoint's 409 gate). When absent, DEFER -- do not FAIL --
    # so a later profiling-complete event can pick the table back up.
    analysis_run_id = await _completed_analysis_run_id(session, datasource_id)
    if analysis_run_id is None:
        logger.info(
            "auto_enqueue_semantic_inference_deferred",
            table_id=str(table_id),
            datasource_id=str(datasource_id),
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="metadata_table",
            aggregate_id=str(table_id),
            event_type=_SEMANTIC_INFERENCE_DEFERRED_EVENT_TYPE,
            payload={
                "table_id": str(table_id),
                "datasource_id": str(datasource_id),
                "reason": "analysis_run_not_yet_completed",
            },
        )
        record_audit(
            session,
            context,
            action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
            resource_type="TABLE",
            resource_id=str(table_id),
            outcome="DEFERRED",
            correlation_id=correlation_id,
            details={
                "reason": "analysis_run_not_yet_completed",
                "description_draft_enqueued": draft is not None,
            },
        )
        return

    record_audit(
        session,
        context,
        action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
        resource_type="TABLE",
        resource_id=str(table_id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "description_draft_enqueued": draft is not None,
            "description_draft_id": str(draft.id) if draft is not None else None,
            "analysis_run_id": str(analysis_run_id),
            "semantic_inference_ready": True,
        },
    )
    await enqueue_semantics_for_source(session, datasource_id, [table_id])


#: Tables per semantic-inference batch. On the consumer's path each batch is
#: its own transaction (`enqueue_semantics_in_batches`).
SEMANTICS_BATCH_SIZE = 100


async def enqueue_semantics_batch(
    session: Any,
    datasource_id: UUID,
    *,
    table_ids: list[UUID] | None = None,
    after: UUID | None = None,
) -> UUID | None:
    """Propose semantics for the next batch of a source's unproposed tables.

    Returns the last table id the batch covered, which is the cursor for the
    next one, or None when nothing is left or the source is not ready (no
    completed scan, or auto-enqueue switched off). It takes the source's row
    lock, so two batches for one source never run side by side.

    The cursor is what makes the pass end. A table inference writes no proposal
    for stays unproposed, so selecting "whatever is still unproposed" picked it
    again on every pass, forever. In the dev stack that loop held one
    transaction for the worker's whole uptime; every migration queued behind
    it, and every write behind the migration.
    """
    datasource = await session.scalar(
        select(DataSource).where(DataSource.id == datasource_id).with_for_update()
    )
    if datasource is None or not get_settings().auto_enqueue_on_ingest:
        return None
    run_id = await _completed_analysis_run_id(session, datasource_id)
    if run_id is None:
        return None
    proposed = (
        select(MetadataEnrichmentProposal.table_id)
        .join(
            SemanticInferenceRun,
            SemanticInferenceRun.id == MetadataEnrichmentProposal.inference_run_id,
        )
        .where(SemanticInferenceRun.analysis_run_id == run_id)
    )
    filters = [
        MetadataTable.datasource_id == datasource_id,
        MetadataTable.status == "ACTIVE",
        MetadataTable.id.not_in(proposed),
    ]
    if table_ids is not None:
        filters.append(MetadataTable.id.in_(table_ids))
    if after is not None:
        filters.append(MetadataTable.id > after)
    pending: list[UUID] = list(
        await session.scalars(
            select(MetadataTable.id)
            .where(*filters)
            .order_by(MetadataTable.id)
            .limit(SEMANTICS_BATCH_SIZE)
        )
    )
    if not pending:
        return None
    await generate_semantic_inference(
        datasource_id,
        SemanticInferenceRequest(use_model=False, max_tables=SEMANTICS_BATCH_SIZE),
        _worker_context(datasource.organization_id),
        session,
        get_settings(),
        table_ids=pending,
    )
    await session.flush()
    return pending[-1]


async def enqueue_semantics_for_source(
    session: Any,
    datasource_id: UUID,
    table_ids: list[UUID] | None = None,
) -> None:
    """Every batch, inside the caller's transaction: the single-table path from
    `handle_newly_created_table`, and any caller that owns the transaction."""
    after: UUID | None = None
    while True:
        after = await enqueue_semantics_batch(
            session, datasource_id, table_ids=table_ids, after=after
        )
        if after is None:
            return


async def enqueue_semantics_in_batches(datasource_id: UUID) -> int:
    """The consumer's path for a completed scan: one transaction per batch.

    A whole source in one transaction held its row lock and its snapshot for
    the entire pass, so on a large source any migration touching those tables
    waited for all of it. Committed batch by batch, nothing outlives a batch.
    A failure keeps the batches before it, and since the message is not
    acknowledged, a replay resumes there: tables already proposed are skipped.
    Returns how many batches ran.
    """
    after: UUID | None = None
    batches = 0
    while True:
        async with session_factory() as session, session.begin():
            after = await enqueue_semantics_batch(session, datasource_id, after=after)
        if after is None:
            return batches
        batches += 1


def _decode_event(raw: bytes) -> dict[str, Any]:
    """Same envelope shape `outbox_publisher.serialize_event` writes."""
    decoded: dict[str, Any] = json.loads(raw)
    return decoded


def _stop_on_signals(state: DrafterConsumerState) -> None:
    """SIGINT / SIGTERM set `state.stopping` and nothing else.

    Whoever creates the state owns this, exactly once: the supervisor, or a
    caller that runs the consumer bare. A supervised consumer is handed the
    supervisor's state and must not register the handlers again on every retry.
    """
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, setattr, state, "stopping", True)


async def run_newly_created_table_drafter_consumer(
    state: DrafterConsumerState | None = None,
) -> None:
    """Consume `catalog.table.newly_created.v1` from the shared
    `aida.platform.events.v1` Kafka topic and dispatch each event to
    `handle_newly_created_table`.

    One connection's worth of work, and it never retries: it returns when
    `state.stopping` is set and raises when the connection or the handling of a
    message fails. Retrying is `supervise_newly_created_table_drafter`'s job --
    that is what `aida.workflows.worker.run_worker` starts as a background task
    when `settings.auto_enqueue_on_ingest` is True, keeping this file
    reachable through the existing worker entry point rather than
    becoming a new deployable -- and it passes the `state` it shares with this
    coroutine. Called with no state, this creates one and installs the signal
    handlers, as it always did.

    Deliberately imports `aiokafka` locally (rather than at module
    top) so tests that only exercise `handle_newly_created_table`
    against an in-memory SQLite session do not require the Kafka
    driver on the import path.
    """
    from aiokafka import AIOKafkaConsumer  # noqa: PLC0415 -- see docstring

    settings = get_settings()
    if state is None:
        state = DrafterConsumerState()
        _stop_on_signals(state)
    consumer = AIOKafkaConsumer(
        "aida.platform.events.v1",
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=DRAFTER_CONSUMER_GROUP,
        client_id="aida-newly-created-table-drafter",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    started = False
    try:
        # Inside the `try` so that a `start()` that fails still reaches the
        # `finally`: it can die after the client has opened connections and its
        # metadata-refresh task, and a supervised retry every minute must not
        # leave those behind for each attempt (aiokafka's `__del__` warns
        # "Unclosed AIOKafkaConsumer" about exactly that).
        await consumer.start()
        started = True
        state.started_at = time.monotonic()
        _set_consumer_up(1)
        logger.info(
            "newly_created_table_drafter_started",
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        async for message in consumer:
            envelope = _decode_event(message.value)
            if envelope.get("event_type") not in (
                NEWLY_CREATED_TABLE_EVENT_TYPE,
                "metadata.analysis.completed.v1",
            ):
                await consumer.commit()
                if state.stopping:
                    break
                continue
            if envelope["event_type"] == NEWLY_CREATED_TABLE_EVENT_TYPE:
                async with session_factory() as session, session.begin():
                    await handle_newly_created_table(session, envelope["payload"])
            else:
                await enqueue_semantics_in_batches(UUID(envelope["payload"]["datasource_id"]))
            await consumer.commit()
            logger.info(
                "newly_created_table_drafter_processed",
                event_id=envelope.get("event_id"),
                table_id=envelope["payload"].get("table_id"),
            )
            if state.stopping:
                break
    finally:
        # First, before the awaits below: from here on nothing is consuming -- whether the loop
        # ended, a message failed, `start()` never succeeded or this was cancelled -- and
        # `stop()` against a broker that is gone can take a while to give up.
        _set_consumer_up(0)
        try:
            await consumer.stop()
        except Exception as exc:
            # Cleanup must not replace the error that brought us here: the
            # supervisor reports that one, and a `stop()` that cannot reach a
            # broker that is already gone is the expected companion of it.
            logger.warning("newly_created_table_drafter_stop_failed", error_type=type(exc).__name__)
        if started:
            logger.info("newly_created_table_drafter_stopped")


#: Wait after the first failed attempt; doubles per consecutive failure up to
#: the cap. Two seconds is long enough not to hammer a broker that is starting
#: and short enough that a Redpanda which comes up a moment after the worker is
#: picked up almost at once; a minute is the longest a healthy stack is left
#: without drafting after the broker returns.
DRAFTER_RETRY_INITIAL_SECONDS = 2.0
DRAFTER_RETRY_MAX_SECONDS = 60.0
#: A consumer that stayed up this long before it failed is a new incident, not
#: a continuing crash loop: the backoff starts over and the failure is logged
#: as a first one. It equals the cap on purpose -- staying up as long as the
#: longest wait the supervisor would ever impose is what "healthy" has to mean.
DRAFTER_HEALTHY_AFTER_SECONDS = 60.0
#: `newly_created_table_drafter_unavailable` is an ERROR for the first failed
#: attempt of an incident and for every Nth after it, a WARNING otherwise: with
#: the cap above, a broker that stays away is an error about every ten minutes
#: and a warning each minute in between.
DRAFTER_ERROR_EVERY_N_ATTEMPTS = 10

DrafterRunner = Callable[[DrafterConsumerState], Awaitable[None]]
DrafterSleep = Callable[[float], Awaitable[None]]


def drafter_retry_delay_seconds(attempt: int) -> float:
    """Seconds to wait after the `attempt`-th consecutive failure (1 is the first)."""
    # The exponent is clamped so a very long outage cannot overflow the float
    # long after the delay has stopped growing.
    exponent = min(max(attempt, 1) - 1, 16)
    return min(DRAFTER_RETRY_INITIAL_SECONDS * 2.0**exponent, DRAFTER_RETRY_MAX_SECONDS)


async def supervise_newly_created_table_drafter(
    run_consumer: DrafterRunner = run_newly_created_table_drafter_consumer,
    *,
    sleep: DrafterSleep = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    state: DrafterConsumerState | None = None,
) -> None:
    """Keep the newly-created-table consumer running, and say so when it is not.

    The worker starts this as a background task and nobody awaits that task until
    shutdown, so it is written not to end: a failure of the consumer -- the broker
    is absent or drops, a message cannot be handled -- is logged as
    `newly_created_table_drafter_unavailable` and the consumer is started again
    after a capped exponential backoff, for as long as the process lives. The
    consumer's success is logged by the consumer itself
    (`newly_created_table_drafter_started`).

    It ends only by cancellation (the worker's shutdown) or when the shared
    `state.stopping` is set. A consumer that returns without being asked to stop
    is treated as a failure, and waited out like one, so that it cannot spin.

    `run_consumer`, `sleep` and `monotonic` are parameters so the loop can be
    driven by a fake consumer and a fake clock; the defaults are the real ones.
    When it creates the state it also installs the SIGINT / SIGTERM handlers that
    set it, as the bare consumer used to.

    It also owns the two series in the module docstring: it creates the gauge, at 0, when
    it starts (the consumer moves it to 1 once `start()` has succeeded and back to 0 when it
    ends), and it counts each failed attempt into `DRAFTER_CONSUMER_FAILURES` where it logs it.
    """
    bootstrap_servers = get_settings().kafka_bootstrap_servers
    if state is None:
        state = DrafterConsumerState()
        _stop_on_signals(state)
    # Not consuming yet, and the moment the series comes to exist: see the module docstring.
    _set_consumer_up(0)
    failed_attempts = 0
    while not state.stopping:
        state.started_at = None
        failure: Exception | None = None
        try:
            await run_consumer(state)
        except Exception as exc:
            # `Exception` and not `BaseException`: cancellation and interpreter
            # exit must pass through. Everything else is what this loop exists
            # to survive -- the consumer runs in a task no one is awaiting.
            failure = exc
        error_type = type(failure).__name__ if failure is not None else None
        if state.stopping:
            if failure is not None:
                logger.warning(
                    "newly_created_table_drafter_failed_while_stopping", error_type=error_type
                )
            return
        if (
            state.started_at is not None
            and monotonic() - state.started_at >= DRAFTER_HEALTHY_AFTER_SECONDS
        ):
            failed_attempts = 0
        failed_attempts += 1
        # Every failed attempt, not only the ones the log escalates: the counter is what a
        # rate() over a window reads, and it must agree with the WARNING lines between the ERRORs.
        DRAFTER_CONSUMER_FAILURES.inc()
        delay = drafter_retry_delay_seconds(failed_attempts)
        fields: dict[str, Any] = {
            "attempt": failed_attempts,
            "next_retry_seconds": delay,
            "bootstrap_servers": bootstrap_servers,
            # None: the consumer returned without an error and without being told to stop.
            "error_type": error_type,
        }
        if failed_attempts == 1 or failed_attempts % DRAFTER_ERROR_EVERY_N_ATTEMPTS == 0:
            # `exc_info` carries the traceback where the log pipeline keeps it
            # (the platform's redaction processor drops the `exception` field, so
            # `error_type` above is what is always readable).
            logger.error("newly_created_table_drafter_unavailable", exc_info=failure, **fields)
        else:
            logger.warning("newly_created_table_drafter_unavailable", **fields)
        await sleep(delay)


if __name__ == "__main__":
    asyncio.run(supervise_newly_created_table_drafter())
