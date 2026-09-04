"""AG-10: the agent task ledger (`AgentTask`).

One row per unit of agent work: what the agent intended, a value-free
fingerprint of what it was given, the proposal it produced (if any), how it
ended, and -- for auto-applied work -- whether the deterministic sampler
picked it for a human audit and what that audit concluded.

`record_agent_task` opens the row; `finish_agent_task` closes it. The
orchestrator (`GovernedAgentOrchestrator.run`) calls both so every governed
run leaves exactly one task behind, whatever path it exits through.

**Sampling is deterministic (INV-3).** Whether an applied task is sampled
for audit is a pure function of its `inputs_fingerprint` and the contract's
`sampling_rate` (`sampled_for_audit`): the first eight hex digits of the
fingerprint, read as a fraction of 2**32, fall below the rate. No random
number generator, no clock -- the same task under the same contract is
always sampled or always not, so the decision replays. The rate is floored
at `AGENT_SAMPLING_RATE_FLOOR` (ADR-0027) at the point of use too, never
only at contract-validation time.

**Value-freedom (INV-6).** `inputs_fingerprint` hashes a canonical JSON
payload the *caller* assembles from identifiers, hashes and parameter
names. `canonical_inputs_fingerprint` does not inspect the payload for
values -- that discipline belongs to the caller -- but the orchestrator's
own payload is built from `question_hash` (already an HMAC), ids, and the
sorted parameter *keys*, never a parameter value.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AGENT_SAMPLING_RATE_FLOOR,
    AGENT_TASK_AUDIT_OUTCOMES,
    AGENT_TASK_STATUSES,
    AgentTask,
)

_FINGERPRINT_BUCKETS = float(2**32)


def canonical_inputs_fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 hex of the canonical (sorted-key, compact) JSON form of a
    value-free payload. Non-JSON scalars (UUIDs, datetimes) are rendered
    through `str`, so callers may pass ids directly.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sampled_for_audit(inputs_fingerprint: str, sampling_rate: float) -> bool:
    """Deterministic sampling decision -- see the module docstring."""
    effective_rate = max(float(sampling_rate), AGENT_SAMPLING_RATE_FLOOR)
    bucket = int(inputs_fingerprint[:8], 16) / _FINGERPRINT_BUCKETS
    return bucket < effective_rate


async def record_agent_task(
    session: AsyncSession,
    *,
    organization_id: UUID,
    agent_principal_id: str,
    intent: str,
    inputs: dict[str, Any],
    ai_asset_version_id: UUID | None = None,
    agent_run_id: UUID | None = None,
    proposal_ref_type: str | None = None,
    proposal_ref_id: UUID | None = None,
    sampling_rate: float = AGENT_SAMPLING_RATE_FLOOR,
    started_at: datetime | None = None,
) -> AgentTask:
    """Open one `AgentTask` in `PROPOSED` status and flush it so its id is
    available to the caller. `inputs` must already be value-free.
    """
    fingerprint = canonical_inputs_fingerprint(inputs)
    task = AgentTask(
        organization_id=organization_id,
        ai_asset_version_id=ai_asset_version_id,
        agent_run_id=agent_run_id,
        agent_principal_id=agent_principal_id,
        intent=intent[:100],
        inputs_fingerprint=fingerprint,
        proposal_ref_type=proposal_ref_type,
        proposal_ref_id=proposal_ref_id,
        status="PROPOSED",
        sampled_for_audit=sampled_for_audit(fingerprint, sampling_rate),
        started_at=started_at or datetime.now(UTC),
    )
    session.add(task)
    await session.flush()
    return task


def finish_agent_task(
    task: AgentTask,
    *,
    status: str,
    evidence: dict[str, Any] | None = None,
    finished_at: datetime | None = None,
) -> AgentTask:
    """Close a task. `status` must be one of `AGENT_TASK_STATUSES` other
    than the opening `PROPOSED`. An `APPLIED` task the sampler picked is
    stored as `SAMPLED` with `audit_outcome="PENDING"` so the human audit
    it is waiting for is visible in the ledger and the inbox; every other
    terminal status leaves `audit_outcome` unset.
    """
    if status not in AGENT_TASK_STATUSES or status == "PROPOSED":
        raise ValueError("agent task terminal status is invalid")
    final_status = status
    if status == "APPLIED" and task.sampled_for_audit:
        final_status = "SAMPLED"
        task.audit_outcome = "PENDING"
    task.status = final_status
    task.finished_at = finished_at or datetime.now(UTC)
    if evidence is not None:
        task.evidence = evidence
    return task


def record_audit_outcome(task: AgentTask, *, outcome: str) -> AgentTask:
    """A human auditor's verdict on a sampled task."""
    if outcome not in AGENT_TASK_AUDIT_OUTCOMES:
        raise ValueError("agent task audit outcome is invalid")
    if not task.sampled_for_audit:
        raise ValueError("agent task was not sampled for audit")
    task.audit_outcome = outcome
    return task


async def task_for_agent_run(session: AsyncSession, *, agent_run_id: UUID) -> AgentTask | None:
    """The single task the orchestrator opened for one `AgentRun`."""
    result: AgentTask | None = await session.scalar(
        select(AgentTask).where(AgentTask.agent_run_id == agent_run_id).limit(1)
    )
    return result
