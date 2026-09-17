from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.config import Settings
from aida.connector_health import (
    RUN_HISTORY_WINDOW,
    ConnectorHealthScore,
    ConnectorRunSample,
    compute_connector_health,
)
from aida.models import AgentRun, AnalysisRun, DataSource, Organization, ScanPolicy
from aida.tool_first_rate import DEFAULT_WINDOW_DAYS, ToolFirstRate, compute_tool_first_rate
from aida.usage_quotas import QuotaRefused, UsageDimension, consume_quota

ACTIVE_ANALYSIS_STATUSES = frozenset({"QUEUED", "RUNNING", "PROFILING", "CANCELLATION_REQUESTED"})


class RunAdmissionRejected(RuntimeError):
    """The requested run cannot be admitted without violating a fleet policy."""


def ensure_datasource_enabled(datasource: DataSource) -> None:
    if datasource.status == "DISABLED":
        raise RunAdmissionRejected("datasource is disabled")


async def reserve_analysis_run(
    session: AsyncSession,
    settings: Settings,
    *,
    datasource_id: UUID,
    mode: str,
    trigger_type: str,
    priority: int = 50,
    resumed_from_run_id: UUID | None = None,
) -> AnalysisRun:
    """Atomically enforce admission concurrency and daily quotas, then reserve a run.

    Organization and datasource rows are locked in a stable order so separate API and
    scheduler replicas cannot collectively over-admit work.

    Two different limits, and the distinction was worth making explicit (R11-FP17):
    `max_active_runs_per_organization` and `max_active_runs_per_datasource` are
    *concurrency* -- how much may be in flight at once, enforced by counting active
    runs under the locks taken above. The `analysis_run_daily_quota_per_*` settings
    are *quotas* -- how much may be consumed in a UTC day, enforced through
    `aida.usage_quotas`' accumulated windows. This function previously had only the
    first, with the per-source figure a hard-coded `1`.
    """
    datasource_snapshot = await session.get(DataSource, datasource_id)
    if datasource_snapshot is None:
        raise RunAdmissionRejected("datasource not found")

    organization = await session.scalar(
        select(Organization)
        .where(Organization.id == datasource_snapshot.organization_id)
        .with_for_update()
    )
    if organization is None or organization.status != "ACTIVE":
        raise RunAdmissionRejected("organization is not active")
    datasource = await session.scalar(
        select(DataSource).where(DataSource.id == datasource_id).with_for_update()
    )
    if datasource is None:
        raise RunAdmissionRejected("datasource not found")
    ensure_datasource_enabled(datasource)

    organization_active = await session.scalar(
        select(func.count())
        .select_from(AnalysisRun)
        .where(
            AnalysisRun.organization_id == datasource.organization_id,
            AnalysisRun.status.in_(ACTIVE_ANALYSIS_STATUSES),
        )
    )
    if (organization_active or 0) >= settings.max_active_runs_per_organization:
        raise RunAdmissionRejected("organization analysis-run quota is exhausted")

    datasource_active = await session.scalar(
        select(func.count())
        .select_from(AnalysisRun)
        .where(
            AnalysisRun.datasource_id == datasource.id,
            AnalysisRun.status.in_(ACTIVE_ANALYSIS_STATUSES),
        )
    )
    if (datasource_active or 0) >= settings.max_active_runs_per_datasource:
        raise RunAdmissionRejected("datasource already has an active analysis run")

    # R11-FP17: the two checks above are *concurrency*, which is all this
    # function had. They bound how much work is in flight and say nothing about
    # how much work a tenant or a source may consume in a day -- a source
    # admitted one run at a time, a thousand times, is a thousand runs. The
    # daily quota is the missing half, and it is enforced through
    # `aida.usage_quotas` so there is one accounting mechanism in the platform
    # rather than two: the same conditional-UPDATE-carrying-its-own-cap shape
    # `aida.agent_budget` already uses for an agent contract's token cap.
    #
    # With no quota declared (the shipped default) this issues no statement at
    # all, so admission is byte-for-byte what it was.
    try:
        await consume_quota(
            session,
            settings,
            datasource_id=datasource.id,
            organization_id=datasource.organization_id,
            dimension=UsageDimension.ANALYSIS_RUNS,
            amount=1,
        )
    except QuotaRefused as refused:
        # Translated into this module's own rejection type rather than allowed
        # to escape: every caller of `reserve_analysis_run` already handles
        # `RunAdmissionRejected`, and a second exception type from the same
        # call would be an unhandled 500 on the two API paths and an unlogged
        # crash in the scheduler loop. The reason code survives the
        # translation, so the refusal is still attributable to which window
        # refused.
        raise RunAdmissionRejected(
            f"analysis-run quota is exhausted ({refused.reason_code})"
        ) from refused

    run_id = uuid4()
    run = AnalysisRun(
        id=run_id,
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        resumed_from_run_id=resumed_from_run_id,
        mode=mode,
        trigger_type=trigger_type,
        priority=priority,
        temporal_workflow_id=f"discovery-{datasource.id}-{run_id}",
    )
    session.add(run)
    await session.flush()
    return run


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC-aware.

    Production runs on PostgreSQL, whose `TIMESTAMPTZ` round-trips a
    `DateTime(timezone=True)` column tz-aware. SQLite (used in this repo's
    test suite -- see `test_catalog_pagination.py`, `test_asset_evidence.py`
    -- because PostgreSQL is unreachable in this sandbox) hands the same
    column back naive, which would otherwise make a naive/aware comparison
    raise instead of answering a question. Same helper as
    `aida.catalog_read_model._as_aware` / `aida.security._as_aware`.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _run_sample(run: AnalysisRun) -> ConnectorRunSample:
    return ConnectorRunSample(
        status=run.status,
        finished_at=_as_aware(run.updated_at),
        error_class=run.error_class,
        discovered_tables=run.discovered_tables,
        profiled_tables=run.profiled_tables,
    )


async def datasource_health(
    session: AsyncSession,
    datasource_id: UUID,
    *,
    now: datetime | None = None,
) -> ConnectorHealthScore | None:
    """Per-connector health score (CN-7) for one datasource.

    Read-only aggregation over existing `AnalysisRun`/`ScanPolicy` rows --
    see `aida.connector_health` for the scoring itself, which is pure and
    unit-tested without a database. Returns `None` when the datasource does
    not exist so the caller can 404.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        return None
    scan_interval_minutes = await session.scalar(
        select(ScanPolicy.interval_minutes).where(ScanPolicy.datasource_id == datasource_id)
    )
    runs = (
        await session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.datasource_id == datasource_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(RUN_HISTORY_WINDOW)
        )
    ).all()
    return compute_connector_health(
        datasource_id=datasource_id,
        datasource_status=datasource.status,
        runs=[_run_sample(run) for run in runs],
        scan_interval_minutes=scan_interval_minutes,
        now=now or datetime.now(UTC),
    )


async def fleet_health(
    session: AsyncSession,
    organization_id: UUID,
    *,
    now: datetime | None = None,
) -> list[ConnectorHealthScore]:
    """Per-connector health scores (CN-7) for every datasource in an org.

    One `row_number() OVER (PARTITION BY datasource_id ...)` query brings
    back the most recent `RUN_HISTORY_WINDOW` runs per datasource (the same
    ranked-window idiom `aida.catalog_read_model` already uses for
    latest-profile/latest-certification lookups) instead of one query per
    datasource, so this stays cheap for a large fleet console.
    """
    resolved_now = now or datetime.now(UTC)
    datasources = (
        await session.scalars(
            select(DataSource)
            .where(DataSource.organization_id == organization_id)
            .order_by(DataSource.name, DataSource.id)
        )
    ).all()
    if not datasources:
        return []

    policy_rows = (
        await session.execute(
            select(ScanPolicy.datasource_id, ScanPolicy.interval_minutes).where(
                ScanPolicy.organization_id == organization_id
            )
        )
    ).all()
    intervals: dict[UUID, int] = {row.datasource_id: row.interval_minutes for row in policy_rows}

    ranked = (
        select(
            AnalysisRun,
            func.row_number()
            .over(
                partition_by=AnalysisRun.datasource_id,
                order_by=AnalysisRun.created_at.desc(),
            )
            .label("rn"),
        )
        .where(AnalysisRun.organization_id == organization_id)
        .subquery()
    )
    ranked_run = aliased(AnalysisRun, ranked)
    run_rows = (
        await session.scalars(select(ranked_run).where(ranked.c.rn <= RUN_HISTORY_WINDOW))
    ).all()
    runs_by_datasource: dict[UUID, list[AnalysisRun]] = {}
    for run in run_rows:
        runs_by_datasource.setdefault(run.datasource_id, []).append(run)

    scores: list[ConnectorHealthScore] = []
    for datasource in datasources:
        runs = sorted(
            runs_by_datasource.get(datasource.id, []),
            key=lambda run: run.created_at,
            reverse=True,
        )
        scores.append(
            compute_connector_health(
                datasource_id=datasource.id,
                datasource_status=datasource.status,
                runs=[_run_sample(run) for run in runs],
                scan_interval_minutes=intervals.get(datasource.id),
                now=resolved_now,
            )
        )
    return scores


async def tool_first_execution_rate(
    session: AsyncSession,
    organization_id: UUID,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> ToolFirstRate:
    """Tool-first execution rate (TL-6) for one organization's rolling window.

    Read-only aggregation over existing `AgentRun` rows -- no new persisted
    state. Only `COMPLETED` runs are counted (mirrors TL-4's
    `tool_usage.get_tool_usage_counts`: a rejected or failed attempt is not
    evidence of tool-first *or* freeform execution, since it never finished
    either way). See `aida.tool_first_rate` for the ratio itself, which is
    pure and unit-tested without a database.
    """
    resolved_now = now or datetime.now(UTC)
    since = resolved_now - timedelta(days=window_days)
    rows = (
        await session.execute(
            select(AgentRun.generation_source, func.count())
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status == "COMPLETED",
                AgentRun.created_at >= since,
            )
            .group_by(AgentRun.generation_source)
        )
    ).all()
    counts = {str(source): int(count) for source, count in rows}
    return compute_tool_first_rate(
        organization_id=organization_id,
        window_days=window_days,
        generation_source_counts=counts,
        now=resolved_now,
    )
