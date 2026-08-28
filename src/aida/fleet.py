from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import AnalysisRun, DataSource, Organization

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
