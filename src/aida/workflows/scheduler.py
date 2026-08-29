import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import Select, select
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from aida.config import Settings, get_settings
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, reserve_analysis_run
from aida.logging import configure_logging
from aida.models import AnalysisRun, ScanPolicy
from aida.security import SecurityContext
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
