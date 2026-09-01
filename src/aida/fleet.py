from datetime import UTC, datetime
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
from aida.models import AnalysisRun, DataSource, Organization, ScanPolicy

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
    """Atomically enforce organization/source quotas and reserve a workflow run.

    Organization and datasource rows are locked in a stable order so separate API and
    scheduler replicas cannot collectively over-admit work.
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
    if (datasource_active or 0) >= 1:
        raise RunAdmissionRejected("datasource already has an active analysis run")

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
