import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from aida.config import Settings, get_settings
from aida.custom_quality_rules import run_due_rule_packs
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, reserve_analysis_run
from aida.glossary_owner_routing import DEFAULT_ESCALATE_AFTER, sync_unowned_asset_backlog
from aida.graph_reconciliation import run_graph_reconciliation_pass
from aida.logging import configure_logging
from aida.models import (
    AnalysisRun,
    MetadataTable,
    NotificationRuleRecord,
    OwnershipRule,
    QueryExecution,
    ScanPolicy,
    UnownedAssetEscalation,
)
from aida.profiling_exceptions import purge_expired_value_profile_artifacts
from aida.security import SecurityContext
from aida.stewardship_api import (
    UNOWNED_BACKLOG_ROUTE_LIMIT,
    _owned_table_ids,
    _scope_table_ids,
    _unowned_asset_table_facts,
)
from aida.workflows.discovery import DatasourceDiscoveryWorkflow

logger = structlog.get_logger(__name__)


def maintenance_window_allows(policy: ScanPolicy, now: datetime) -> bool:
    start = policy.maintenance_start_hour_utc
    end = policy.maintenance_end_hour_utc
    if start is None or end is None:
        return True
    hour = now.astimezone(UTC).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def next_maintenance_window(policy: ScanPolicy, now: datetime) -> datetime:
    start = policy.maintenance_start_hour_utc
    if start is None:
        return now
    now_utc = now.astimezone(UTC)
    candidate = now_utc.replace(hour=start, minute=0, second=0, microsecond=0)
    if candidate <= now_utc:
        candidate += timedelta(days=1)
    return candidate


def next_interval_at(policy: ScanPolicy, now: datetime) -> datetime:
    candidate = policy.next_run_at + timedelta(minutes=policy.interval_minutes)
    if candidate <= now:
        return now + timedelta(minutes=policy.interval_minutes)
    return candidate


def usage_weighted_priority_boost(recent_query_count: int, max_boost: int) -> int:
    """Turn recent query volume into a small, capped boost on top of
    base_priority (ADR-0017 SS8). Deliberately coarse -- one boost point per
    five recent queries -- because this nudges the admin-set base priority, it
    does not replace it: a heavily-queried datasource should get scanned
    sooner than an idle one with the same base priority, never leapfrog a
    genuinely higher-priority one by an unbounded amount.
    """
    return min(max_boost, max(0, recent_query_count) // 5)


def stale_usage_boost_policies_statement(settings: Settings, now: datetime) -> Select[tuple[UUID]]:
    """Usage-boost-enabled policies whose computed_usage_boost has not been
    refreshed recently, oldest-refreshed first. Bounded by usage_boost_batch_size
    for the same reason due_scan_policies_statement is bounded by
    scheduler_batch_size -- recomputing a usage signal for every enabled policy
    on every tick does not scale to a large estate any better than scanning it
    would.
    """
    stale_before = now - timedelta(minutes=settings.usage_boost_refresh_minutes)
    return (
        select(ScanPolicy.id)
        .where(
            ScanPolicy.usage_boost_enabled.is_(True),
            (ScanPolicy.usage_boost_updated_at.is_(None))
            | (ScanPolicy.usage_boost_updated_at <= stale_before),
        )
        .order_by(ScanPolicy.usage_boost_updated_at.asc().nulls_first())
        .limit(settings.usage_boost_batch_size)
    )


async def rebalance_usage_weighted_priorities(
    settings: Settings, *, now: datetime | None = None
) -> int:
    """Recompute computed_usage_boost for a bounded batch of stale, opted-in
    scan policies from recent query volume on their datasource, then set
    `priority = base_priority + computed_usage_boost` (clamped to 0-100).
    due_scan_policies_statement and reserve_analysis_run keep reading plain
    `priority` completely unchanged -- only this function ever writes it once
    usage boosting is enabled, and always relative to `base_priority` (the
    admin's last explicit value, set on every scan-policy upsert), never
    compounding on a previous boost.
    """
    effective_now = now or datetime.now(UTC)
    updated = 0
    async with session_factory() as session:
        policy_ids = (
            await session.scalars(stale_usage_boost_policies_statement(settings, effective_now))
        ).all()
        for policy_id in policy_ids:
            policy = await session.get(ScanPolicy, policy_id)
            if policy is None or not policy.usage_boost_enabled:
                continue
            window_start = effective_now - timedelta(days=settings.usage_boost_window_days)
            recent_query_count = await session.scalar(
                select(func.count())
                .select_from(QueryExecution)
                .where(
                    QueryExecution.datasource_id == policy.datasource_id,
                    QueryExecution.created_at >= window_start,
                )
            )
            policy.computed_usage_boost = usage_weighted_priority_boost(
                recent_query_count or 0, settings.usage_boost_max
            )
            policy.priority = max(0, min(100, policy.base_priority + policy.computed_usage_boost))
            policy.usage_boost_updated_at = effective_now
            updated += 1
        if updated:
            await session.commit()
    return updated


# --- GL-6: unowned-asset backlog owner routing -----------------------------
#
# glossary_owner_routing.sync_unowned_asset_backlog is a pure reconciliation
# step (route aged-unowned entries, escalate stale-routed ones, resolve
# now-owned ones) that until now nothing called except the on-demand
# `POST .../unowned-backlog/route` endpoint -- so routing was inert unless an
# org or a cron outside this codebase called it. This closes that gap by
# giving it the same periodic home scan policies already have here, on an
# aged-backlog cadence (owner_routing_interval_minutes, default daily) rather
# than the scheduler's own short poll cadence: the backlog's own thresholds
# (DEFAULT_ROUTE_AFTER/DEFAULT_ESCALATE_AFTER = 7/14 days) mean nothing about
# a given table's routing state can change between two sub-daily sweeps, so a
# tighter cadence would only add per-tick, per-organization DB scans for no
# earlier routing or escalation.
#
# Unlike ScanPolicy (one row per datasource, carrying its own next_run_at),
# there is no existing per-organization row to persist a next-due-at on
# without a new model/migration column -- out of scope here (see the
# collision-avoidance note against touching models.py). So due-ness is
# tracked in this process's memory instead, keyed by organization_id. A
# scheduler restart just forgets it swept recently and re-sweeps once more
# than strictly necessary; sync_unowned_asset_backlog is idempotent/safe to
# re-run (it reconciles from the current unowned set every time), so that
# costs one redundant bounded pass, never a correctness problem.
_owner_routing_last_run_at: dict[UUID, datetime] = {}

DEFAULT_UNOWNED_BACKLOG_RULE_NAME = "Unowned asset backlog (default)"


def owner_routing_due(last_run_at: datetime | None, now: datetime, interval: timedelta) -> bool:
    """Whether an owner-routing sweep is due for an organization.

    ``None`` (never swept) is always due; otherwise due once ``interval`` has
    elapsed since the last successful sweep.
    """
    if last_run_at is None:
        return True
    return now - last_run_at >= interval


async def ensure_default_unowned_backlog_notification_rule(
    session: AsyncSession, organization_id: UUID
) -> NotificationRuleRecord:
    """Return the organization's catch-all unowned-backlog routing rule,
    creating it if missing.

    Mirrors ``aida.domain_service.ensure_default_domain``'s lazily-created
    default row rather than a migration seed: an organization is created long
    before it has any unowned-asset backlog worth routing, so there is
    nothing to seed a migration-time row *for* at organization-creation time,
    and this codebase already has a "lazy default on first use" convention
    for exactly that situation.

    Without *some* enabled rule, sync_unowned_asset_backlog still tracks and
    ages backlog entries but never routes them anywhere -- the tracker's
    "no default catch-all notification rule ships" gap. The shape of this
    rule is deliberate, not a guess:

    - ``conditions={}``: ``notification_routing._matches_conditions`` only
      narrows on keys actually *present* in ``conditions`` (severity,
      source_id, domain, owner) -- an empty dict matches every incident it is
      asked to match. Matching narrower than that would risk silently
      matching nothing: unowned-asset incidents' ``domain`` is ``None``
      whenever the table carries no business-domain annotation (the common
      case for a freshly-discovered, unowned table -- domain assignment is
      itself often a stewardship task blocked on having an owner), so a rule
      keyed on ``domain`` would miss the very backlog entries most in need of
      routing.
    - ``channel="EMAIL"``: the one channel among this engine's
      EMAIL/WEBHOOK/ITSM that needs no external delivery infrastructure
      (a webhook endpoint, an ITSM integration) configured to exist as a
      sane default.
    - ``recipients=[]``: this codebase has no stored "organization admin" or
      steward-lead roster to look up a default recipient from --
      ``SecurityContext`` carries a per-request bearer principal, not a
      queryable roster -- so fabricating one would be inventing a lookup
      that does not exist. An empty-recipient rule is inert as far as actual
      delivery goes, but it is *present and enabled*, which is what unblocks
      routing/escalation status transitions (and their audit/outbox events)
      from sitting inert in PENDING forever; an org fills in real recipients
      (or a channel) once it has somewhere to send to.
    - ``escalation_after_minutes`` mirrors ``DEFAULT_ESCALATE_AFTER`` so a
      ROUTED entry escalates on the same 14-day horizon this rule's absence
      would otherwise fall back to (see sync_unowned_asset_backlog's
      internal fallback rule for the no-rule-matched case) -- this rule
      matching should not tighten or loosen that horizon by default.
    """
    existing = await session.scalar(
        select(NotificationRuleRecord).where(
            NotificationRuleRecord.organization_id == organization_id,
            NotificationRuleRecord.name == DEFAULT_UNOWNED_BACKLOG_RULE_NAME,
        )
    )
    if existing is not None:
        return existing
    rule = NotificationRuleRecord(
        organization_id=organization_id,
        name=DEFAULT_UNOWNED_BACKLOG_RULE_NAME,
        conditions={},
        channel="EMAIL",
        recipients=[],
        escalation_after_minutes=int(DEFAULT_ESCALATE_AFTER.total_seconds() // 60),
        enabled=True,
        created_by="fleet-scheduler",
    )
    session.add(rule)
    await session.flush()
    return rule


async def _sync_owner_routing_for_organization(organization_id: UUID, *, now: datetime) -> None:
    """One backlog reconciliation pass for a single organization, mirroring
    ``stewardship_api.route_unowned_asset_backlog``'s full-organization-scope
    call (no datasource/domain/line-of-business narrowing) and its
    persistence of the result -- this is that same endpoint run automatically
    instead of only on demand.
    """
    async with session_factory() as session:
        table_ids = await _scope_table_ids(
            session,
            organization_id=organization_id,
            datasource_id=None,
            domain_id=None,
            line_of_business_id=None,
        )
        owned = await _owned_table_ids(
            session, organization_id=organization_id, table_ids=table_ids
        )
        unowned_table_ids = table_ids - owned

        existing_rows = (
            await session.scalars(
                select(UnownedAssetEscalation).where(
                    UnownedAssetEscalation.organization_id == organization_id,
                    UnownedAssetEscalation.status != "RESOLVED",
                )
            )
        ).all()
        existing_entries = {row.table_id: row for row in existing_rows}

        ownership_rules = list(
            await session.scalars(
                select(OwnershipRule).where(
                    OwnershipRule.organization_id == organization_id,
                    OwnershipRule.status == "ACTIVE",
                )
            )
        )
        await ensure_default_unowned_backlog_notification_rule(session, organization_id)
        notification_rules = list(
            await session.scalars(
                select(NotificationRuleRecord).where(
                    NotificationRuleRecord.organization_id == organization_id,
                    NotificationRuleRecord.enabled.is_(True),
                )
            )
        )

        route_candidates = sorted(unowned_table_ids, key=str)[:UNOWNED_BACKLOG_ROUTE_LIMIT]
        table_facts = await _unowned_asset_table_facts(
            session, organization_id=organization_id, table_ids=route_candidates
        )

        result = sync_unowned_asset_backlog(
            organization_id=organization_id,
            unowned_table_ids=unowned_table_ids,
            existing_entries=existing_entries,
            table_facts=table_facts,
            ownership_rules=ownership_rules,
            notification_rules=notification_rules,
            now=now,
            route_limit=UNOWNED_BACKLOG_ROUTE_LIMIT,
        )
        for entry in result.created:
            session.add(entry)
        await session.flush()

        worker_context = SecurityContext(
            principal_id="fleet-scheduler",
            principal_type="WORKER",
            organization_id=organization_id,
            roles=frozenset({"SchedulerWorker"}),
        )
        for entry in result.routed:
            record_audit(
                session,
                worker_context,
                action="stewardship.unowned_asset.routed",
                resource_type="unowned_asset_escalation",
                resource_id=str(entry.id),
                outcome="SUCCESS",
                correlation_id=str(entry.id),
                details={"table_id": str(entry.table_id), "candidate_owner": entry.candidate_owner},
            )
            record_outbox(
                session,
                organization_id=organization_id,
                aggregate_type="unowned_asset_escalation",
                aggregate_id=str(entry.id),
                event_type="stewardship.unowned_asset_routed.v1",
                payload={"table_id": str(entry.table_id), "candidate_owner": entry.candidate_owner},
            )
        for entry in result.escalated:
            record_audit(
                session,
                worker_context,
                action="stewardship.unowned_asset.escalated",
                resource_type="unowned_asset_escalation",
                resource_id=str(entry.id),
                outcome="SUCCESS",
                correlation_id=str(entry.id),
                details={"table_id": str(entry.table_id)},
            )
            record_outbox(
                session,
                organization_id=organization_id,
                aggregate_type="unowned_asset_escalation",
                aggregate_id=str(entry.id),
                event_type="stewardship.unowned_asset_escalated.v1",
                payload={"table_id": str(entry.table_id)},
            )
        for entry in result.resolved:
            record_outbox(
                session,
                organization_id=organization_id,
                aggregate_type="unowned_asset_escalation",
                aggregate_id=str(entry.id),
                event_type="stewardship.unowned_asset_resolved.v1",
                payload={"table_id": str(entry.table_id)},
            )
        await session.commit()


async def run_owner_routing_pass(
    settings: Settings,
    *,
    now: datetime | None = None,
    organization_ids: Sequence[UUID] | None = None,
) -> int:
    """Sweep every organization due for a GL-6 owner-routing pass (see
    ``owner_routing_due``), returning how many were actually swept.

    One organization's failure (a bad rule, a transient DB error) is logged
    and skipped -- exactly the fault-isolation style ``run_scheduler_iteration``
    already uses around ``process_scan_policy`` -- so it never aborts every
    other organization's sweep in the same iteration. A failed organization's
    last-run time is deliberately left unchanged, so it is retried on the very
    next scheduler iteration rather than waiting a full interval.

    ``organization_ids`` overrides the DB-derived candidate list (every
    organization with at least one active table); production call sites leave
    it unset. It exists so tests can drive the due-check and fault-isolation
    behavior above without a live Postgres/Docker, matching this codebase's
    fake-session/pure-function test convention (see test_glossary_owner_routing.py) --
    it is not a scoping/filtering knob for real callers.
    """
    effective_now = now or datetime.now(UTC)
    interval = timedelta(minutes=settings.owner_routing_interval_minutes)
    if organization_ids is None:
        async with session_factory() as session:
            organization_ids = (
                await session.scalars(select(MetadataTable.organization_id).distinct())
            ).all()
    swept = 0
    for organization_id in organization_ids:
        if not owner_routing_due(
            _owner_routing_last_run_at.get(organization_id), effective_now, interval
        ):
            continue
        try:
            await _sync_owner_routing_for_organization(organization_id, now=effective_now)
        except Exception:
            logger.exception("owner_routing_pass_failed", organization_id=str(organization_id))
            continue
        _owner_routing_last_run_at[organization_id] = effective_now
        swept += 1
    return swept


# --- DQ-4: custom quality rule packs -----------------------------------------
#
# Each QualityRulePack carries its own interval_minutes (unlike GL-6's single
# org-wide cadence), so due-ness is tracked per rule-pack id rather than per
# organization. Same in-process-memory, restart-just-resweeps-once-more
# tradeoff as _owner_routing_last_run_at, for the same reason: no existing
# per-pack "next due at" column, and a sweep is idempotent/safe to repeat
# (custom_quality_rules.evaluate_rule_pack reconciles from the latest stored
# profile every time).
_custom_rule_pack_last_run_at: dict[UUID, datetime] = {}


async def run_custom_rule_pack_pass(*, now: datetime | None = None) -> int:
    """Sweep every enabled ``QualityRulePack`` due per its own
    ``interval_minutes``, independent of the profiling scan cadence above --
    DQ-4's exit condition ("rules run outside scans"). Delegates to
    ``custom_quality_rules.run_due_rule_packs``, which already isolates one
    rule pack's failure from the rest, matching ``run_owner_routing_pass``.
    """
    return await run_due_rule_packs(now=now, last_run_at=_custom_rule_pack_last_run_at)


# --- KG-7: scheduled knowledge-graph reconciliation + drift alerting -------
#
# graph_reconciliation.run_graph_reconciliation_pass is a read-only diff pass
# (Postgres's current projection selection vs. what Neo4j actually holds)
# that, like GL-6's owner-routing pass and DQ-4's rule packs above, needs a
# periodic home outside the event-driven projector. Same in-process-memory
# due-tracking tradeoff as those two: no per-datasource "next reconciled at"
# column exists to persist to without a new model/migration, and the pass is
# safe to repeat, so a scheduler restart costs at most one redundant sweep
# per datasource.
_graph_reconciliation_last_run_at: dict[UUID, datetime] = {}


async def run_graph_reconciliation_scheduler_pass(
    settings: Settings, *, now: datetime | None = None
) -> int:
    """Sweep every datasource due for a KG-7 reconciliation pass (see
    ``graph_reconciliation.graph_reconciliation_due``). Delegates entirely to
    ``graph_reconciliation.run_graph_reconciliation_pass``, which already
    isolates one datasource's failure (Neo4j unreachable, a bad rule) from
    the rest, matching ``run_owner_routing_pass``/``run_custom_rule_pack_pass``.
    """
    return await run_graph_reconciliation_pass(
        settings, now=now, last_run_at=_graph_reconciliation_last_run_at
    )


async def _start_workflow(client: Client, settings: Settings, run: AnalysisRun) -> None:
    try:
        await client.start_workflow(
            DatasourceDiscoveryWorkflow.run,
            str(run.id),
            id=run.temporal_workflow_id or f"discovery-{run.datasource_id}-{run.id}",
            task_queue=settings.temporal_task_queue,
        )
    except WorkflowAlreadyStartedError:
        return
    except Exception as exc:
        async with session_factory() as session:
            persisted = await session.get(AnalysisRun, run.id)
            if persisted is not None and persisted.status == "QUEUED":
                persisted.status = "SUBMISSION_FAILED"
                persisted.error_class = type(exc).__name__
                persisted.error_message = "workflow submission failed"
                await session.commit()
        logger.exception("scheduled_workflow_submission_failed", run_id=str(run.id))


async def process_scan_policy(
    policy_id: UUID,
    client: Client,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    effective_now = now or datetime.now(UTC)
    admitted_run: AnalysisRun | None = None
    async with session_factory() as session:
        policy = await session.scalar(
            select(ScanPolicy).where(ScanPolicy.id == policy_id).with_for_update()
        )
        if policy is None or not policy.enabled or policy.next_run_at > effective_now:
            return False
        if not maintenance_window_allows(policy, effective_now):
            policy.next_run_at = next_maintenance_window(policy, effective_now)
            await session.commit()
            return False
        try:
            admitted_run = await reserve_analysis_run(
                session,
                settings,
                datasource_id=policy.datasource_id,
                mode=policy.mode,
                trigger_type="SCHEDULED",
                priority=policy.priority,
            )
        except RunAdmissionRejected as exc:
            retry_minutes = min(5, policy.interval_minutes)
            policy.next_run_at = effective_now + timedelta(minutes=retry_minutes)
            await session.commit()
            logger.info(
                "scheduled_scan_deferred",
                policy_id=str(policy.id),
                reason=str(exc),
                retry_minutes=retry_minutes,
            )
            return False

        policy.last_triggered_at = effective_now
        policy.next_run_at = next_interval_at(policy, effective_now)
        worker_context = SecurityContext(
            principal_id="fleet-scheduler",
            principal_type="WORKER",
            organization_id=policy.organization_id,
            roles=frozenset({"SchedulerWorker"}),
        )
        record_audit(
            session,
            worker_context,
            action="analysis_run.schedule",
            resource_type="analysis_run",
            resource_id=str(admitted_run.id),
            outcome="SUCCESS",
            correlation_id=str(admitted_run.id),
            details={"scan_policy_id": str(policy.id), "priority": policy.priority},
        )
        record_outbox(
            session,
            organization_id=policy.organization_id,
            aggregate_type="analysis_run",
            aggregate_id=str(admitted_run.id),
            event_type="analysis_run.scheduled.v1",
            payload={
                "run_id": str(admitted_run.id),
                "datasource_id": str(policy.datasource_id),
                "scan_policy_id": str(policy.id),
            },
        )
        await session.commit()

    await _start_workflow(client, settings, admitted_run)
    return True


def due_scan_policies_statement(settings: Settings, now: datetime) -> Select[tuple[UUID]]:
    """Due, enabled policies ordered highest-priority-first, oldest-due as tiebreaker.

    Higher ``priority`` policies are admitted ahead of lower-priority ones whenever more
    policies are due than a single scheduler batch can process -- this is the platform's
    priority claim for fleet scheduling. ``next_run_at`` only breaks ties within a priority
    tier, so it never lets an older, lower-priority scan jump ahead of a newer, higher one.
    """
    return (
        select(ScanPolicy.id)
        .where(ScanPolicy.enabled.is_(True), ScanPolicy.next_run_at <= now)
        .order_by(ScanPolicy.priority.desc(), ScanPolicy.next_run_at)
        .limit(settings.scheduler_batch_size)
    )


async def run_scheduler_iteration(client: Client, settings: Settings) -> int:
    await reconcile_cancellation_requests(client, settings)
    now = datetime.now(UTC)
    await rebalance_usage_weighted_priorities(settings, now=now)
    await run_owner_routing_pass(settings, now=now)
    await run_custom_rule_pack_pass(now=now)
    await run_graph_reconciliation_scheduler_pass(settings, now=now)
    # PR-2's retention contract: expired value-bearing profiling artifacts are
    # purged every iteration, bounded by profiling_exception_purge_batch_size,
    # the same "bounded pass every iteration" shape as the two calls above.
    await purge_expired_value_profile_artifacts(settings, now=now)
    async with session_factory() as session:
        policy_ids = (await session.scalars(due_scan_policies_statement(settings, now))).all()
    admitted = 0
    for policy_id in policy_ids:
        try:
            admitted += int(await process_scan_policy(policy_id, client, settings, now=now))
        except Exception:
            logger.exception("scan_policy_processing_failed", policy_id=str(policy_id))
    return admitted


async def reconcile_cancellation_requests(client: Client, settings: Settings) -> int:
    async with session_factory() as session:
        run_ids = (
            await session.scalars(
                select(AnalysisRun.id)
                .where(
                    AnalysisRun.status == "CANCELLATION_REQUESTED",
                    AnalysisRun.temporal_workflow_id.is_not(None),
                )
                .order_by(AnalysisRun.updated_at)
                .limit(settings.scheduler_batch_size)
            )
        ).all()
    reconciled = 0
    for run_id in run_ids:
        async with session_factory() as session:
            run = await session.get(AnalysisRun, run_id)
            if (
                run is None
                or run.status != "CANCELLATION_REQUESTED"
                or not run.temporal_workflow_id
            ):
                continue
            try:
                description = await client.get_workflow_handle(run.temporal_workflow_id).describe()
            except Exception:
                logger.exception("workflow_cancellation_reconciliation_failed", run_id=str(run.id))
                continue
            if description.status in {
                WorkflowExecutionStatus.CANCELED,
                WorkflowExecutionStatus.TERMINATED,
            }:
                run.status = "CANCELLED"
                event_type = "metadata.analysis.cancelled.v1"
            elif description.status == WorkflowExecutionStatus.COMPLETED:
                run.status = "COMPLETED"
                event_type = "metadata.analysis.cancellation_race_completed.v1"
            elif description.status in {
                WorkflowExecutionStatus.FAILED,
                WorkflowExecutionStatus.TIMED_OUT,
            }:
                run.status = "FAILED"
                run.error_class = f"TEMPORAL_{description.status.name}"
                run.error_message = "workflow reached a terminal state during cancellation"
                event_type = "metadata.analysis.failed.v1"
            else:
                continue
            worker_context = SecurityContext(
                principal_id="fleet-scheduler",
                principal_type="WORKER",
                organization_id=run.organization_id,
                roles=frozenset({"SchedulerWorker"}),
            )
            record_audit(
                session,
                worker_context,
                action="analysis_run.cancellation.reconcile",
                resource_type="analysis_run",
                resource_id=str(run.id),
                outcome="SUCCESS",
                correlation_id=str(run.id),
                details={"temporal_status": description.status.name},
            )
            record_outbox(
                session,
                organization_id=run.organization_id,
                aggregate_type="analysis_run",
                aggregate_id=str(run.id),
                event_type=event_type,
                payload={"run_id": str(run.id), "status": run.status},
            )
            await session.commit()
            reconciled += 1
    return reconciled


async def run_scheduler() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    logger.info(
        "fleet_scheduler_started",
        poll_seconds=settings.scheduler_poll_seconds,
        batch_size=settings.scheduler_batch_size,
    )
    while True:
        admitted = await run_scheduler_iteration(client, settings)
        logger.info("fleet_scheduler_iteration", admitted_runs=admitted)
        await asyncio.sleep(settings.scheduler_poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_scheduler())
