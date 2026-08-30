"""PR-2: policy-approved range/top-value profiling by classification.

Module 05 §8 (ADR-0014, value-free control plane): value-free statistics
(row estimates, null rates, distinct estimates, lengths, schema fingerprints)
are the only thing `profile_table_task` computes by default. "Ranges and top
values require a policy-approved classification-specific exception with its
own retention contract" -- `ProfilingExceptionPolicy` is that policy object,
this module is the live gate `profile_table_task` calls before it is ever
allowed to invoke a connector's `profile_column_values`, and
`purge_expired_value_profile_artifacts` is the retention contract enforced:
a background sweep that hard-deletes every `ColumnValueProfileArtifact` past
its pinned `expires_at`. `api.py` wires request/list/decide/revoke REST
endpoints onto the functions here, following the same maker-checker shape
`GovernanceReview` uses elsewhere in this codebase (see
`ProfilingExceptionPolicy`'s docstring in `models.py` for why this policy
keeps its own status fields rather than filing into that shared queue).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.classification import SENSITIVE_CLASSES
from aida.config import Settings
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.models import ColumnValueProfileArtifact, ProfilingExceptionPolicy
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

WORKER_PRINCIPAL_ID = "profiling-retention-worker"

#: Classifications that are ever eligible for the value-bearing exception --
#: the same "sensitive" tier `catalog_bulk_actions.ALLOWED_CLASSIFICATIONS`
#: already carves out of the full classification vocabulary. `UNCLASSIFIED`/
#: `PUBLIC`/`INTERNAL` columns have nothing sensitive to gate -- value capture
#: for them would be pointless ADR-0014 exposure for no governance reason, so
#: `profile_table_task` never even looks up a policy for those classifications,
#: and a request for one is rejected by the API before it reaches this module.
GATED_CLASSIFICATIONS = SENSITIVE_CLASSES


async def approved_policy_for(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    classification: str,
) -> ProfilingExceptionPolicy | None:
    """The live gate `profile_table_task` calls before capturing any value.

    Returns the policy only when it is currently `APPROVED` -- a revoked
    policy moves to `REVOKED` (see `revoke_policy` in `api.py`), so this single
    status check is both "was it approved" and "is it still in force" at once.
    `organization_id`/`datasource_id` are restated explicitly in the predicate
    (INV-5) rather than trusted transitively through a joined row.
    """
    result = await session.scalar(
        select(ProfilingExceptionPolicy).where(
            ProfilingExceptionPolicy.organization_id == organization_id,
            ProfilingExceptionPolicy.datasource_id == datasource_id,
            ProfilingExceptionPolicy.classification == classification,
            ProfilingExceptionPolicy.status == "APPROVED",
        )
    )
    return result


async def purge_expired_value_profile_artifacts(
    settings: Settings, *, now: datetime | None = None
) -> int:
    """PR-2's retention contract, enforced.

    Hard-deletes every `ColumnValueProfileArtifact` whose `expires_at` -- fixed
    at capture time from the authorizing policy's `retention_days`, never
    recomputed from the policy's *current* `retention_days` -- has passed.
    Bounded to `settings.profiling_exception_purge_batch_size` per call so an
    arbitrarily large backlog is worked off over successive scheduler
    iterations (see `run_scheduler_iteration`) rather than in one unbounded
    sweep, the same shape every other bounded housekeeping pass in this
    codebase uses.
    """
    effective_now = now or datetime.now(UTC)
    purged = 0
    async with session_factory() as session:
        expired = (
            await session.scalars(
                select(ColumnValueProfileArtifact)
                .where(ColumnValueProfileArtifact.expires_at <= effective_now)
                .order_by(ColumnValueProfileArtifact.expires_at)
                .limit(settings.profiling_exception_purge_batch_size)
            )
        ).all()
        for artifact in expired:
            worker_context = SecurityContext(
                principal_id=WORKER_PRINCIPAL_ID,
                principal_type="WORKER",
                organization_id=artifact.organization_id,
                roles=frozenset({"MetadataWorker"}),
            )
            record_audit(
                session,
                worker_context,
                action="profiling_exception.artifact_purged",
                resource_type="column_value_profile_artifact",
                resource_id=str(artifact.id),
                outcome="SUCCESS",
                correlation_id=str(artifact.id),
                details={
                    "column_id": str(artifact.column_id),
                    "policy_id": str(artifact.policy_id),
                    "expired_at": artifact.expires_at.isoformat(),
                },
            )
            record_outbox(
                session,
                organization_id=artifact.organization_id,
                aggregate_type="column_value_profile_artifact",
                aggregate_id=str(artifact.id),
                event_type="profiling.value_artifact_purged.v1",
                payload={
                    "artifact_id": str(artifact.id),
                    "column_id": str(artifact.column_id),
                    "table_id": str(artifact.table_id),
                    "policy_id": str(artifact.policy_id),
                },
            )
            await session.delete(artifact)
            purged += 1
        if purged:
            await session.commit()
    if purged:
        logger.info("profiling_value_artifacts_purged", count=purged)
    return purged
