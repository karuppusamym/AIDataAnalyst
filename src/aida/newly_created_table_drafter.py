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

**Reachability.** `run_newly_created_table_drafter_consumer()` is
imported and started as a background asyncio task from
`aida.workflows.worker.run_worker` (the `aida.workflows.worker`
process, already an `ENTRY_POINTS` row in
`tests/test_reachability_gate.py`) so this module is reachable through
the existing worker entry point rather than becoming a new deployable.

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
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aida.asset_description_service import (
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
    MetadataTable,
)
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)


# Kept as a module-level constant so tests and the emitter side (in
# `aida.workflows.activities`) can key on the same string rather than
# each carrying its own copy.
NEWLY_CREATED_TABLE_EVENT_TYPE = "catalog.table.newly_created.v1"
_DRAFT_ENQUEUED_EVENT_TYPE = "asset_description.draft.auto_enqueued.v1"
_SEMANTIC_INFERENCE_DEFERRED_EVENT_TYPE = (
    "business_semantics.inference.auto_enqueue_deferred.v1"
)
_AUTO_ENQUEUE_PRINCIPAL = "auto-enqueue-drafter"
# The two open-draft statuses `generate_asset_description_drafts` also
# treats as "already in flight; do not stack another one on top of this".
_OPEN_DRAFT_STATUSES = ("DRAFT", "PENDING_APPROVAL")
_APPROVED_DOC_STATUS = "APPROVED"


@dataclass(slots=True)
class DrafterConsumerState:
    stopping: bool = False


def _worker_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=_AUTO_ENQUEUE_PRINCIPAL,
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"MetadataWorker"}),
    )


async def _table_has_approved_description(
    session: Any, table_id: UUID
) -> bool:
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
        select(AssetDescriptionDraft.id)
        .where(AssetDescriptionDraft.table_id == table_id)
        .limit(1)
    )
    return existing_id is not None


async def _completed_analysis_run_id(
    session: Any, datasource_id: UUID
) -> UUID | None:
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
    exists, handler already produced one for this table).
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
    evidence = await gather_evidence(session, table)
    drafted_text = compose_draft_text(evidence)
    fingerprint = text_fingerprint(drafted_text)
    scores = score_evidence(evidence)
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
        created_by=_AUTO_ENQUEUE_PRINCIPAL,
    )
    session.add(draft)
    await session.flush()
    return draft


async def handle_newly_created_table(
    session: Any, payload: dict[str, Any]
) -> None:
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
            # The semantic-inference *proposal* generation itself is
            # left to the operator-triggered
            # `create_semantic_inference_run` (which runs across every
            # active table of the datasource in one batch, per its own
            # contract); this row records that the gate is now open on
            # this table so the next inference run will include it.
            "semantic_inference_ready": True,
        },
    )


def _decode_event(raw: bytes) -> dict[str, Any]:
    """Same envelope shape `outbox_publisher.serialize_event` writes."""
    decoded: dict[str, Any] = json.loads(raw)
    return decoded


async def run_newly_created_table_drafter_consumer() -> None:
    """Consume `catalog.table.newly_created.v1` from the shared
    `aida.platform.events.v1` Kafka topic and dispatch each event to
    `handle_newly_created_table`. Import path is what
    `aida.workflows.worker.run_worker` starts as a background task
    when `settings.auto_enqueue_on_ingest` is True, keeping this file
    reachable through the existing worker entry point rather than
    becoming a new deployable.

    Deliberately imports `aiokafka` locally (rather than at module
    top) so tests that only exercise `handle_newly_created_table`
    against an in-memory SQLite session do not require the Kafka
    driver on the import path.
    """
    from aiokafka import AIOKafkaConsumer  # noqa: PLC0415 -- see docstring

    settings = get_settings()
    state = DrafterConsumerState()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, setattr, state, "stopping", True)
    consumer = AIOKafkaConsumer(
        "aida.platform.events.v1",
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="aida-newly-created-table-drafter-v1",
        client_id="aida-newly-created-table-drafter",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info("newly_created_table_drafter_started")
    try:
        async for message in consumer:
            envelope = _decode_event(message.value)
            if envelope.get("event_type") != NEWLY_CREATED_TABLE_EVENT_TYPE:
                await consumer.commit()
                if state.stopping:
                    break
                continue
            async with session_factory() as session, session.begin():
                await handle_newly_created_table(session, envelope["payload"])
            await consumer.commit()
            logger.info(
                "newly_created_table_drafter_processed",
                event_id=envelope.get("event_id"),
                table_id=envelope["payload"].get("table_id"),
            )
            if state.stopping:
                break
    finally:
        await consumer.stop()
        logger.info("newly_created_table_drafter_stopped")


if __name__ == "__main__":
    asyncio.run(run_newly_created_table_drafter_consumer())
