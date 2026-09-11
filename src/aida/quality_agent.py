"""ADR-0029: the quality agent.

A task agent (`aida.task_agent`) -- `agent:quality` by default -- that works a
backlog nothing else worked: tables and columns the profiler has measured again
and again with no quality rule watching them. DQ-4 gave stewards threshold rules
of their own; nothing ever suggested one.

Two capabilities, each one of DQ-4's value-free rule types, derived from recent
profile history by `aida.quality_rule_proposals`:

* ROW_COUNT_FLOOR -- `TABLE_ROW_COUNT_MIN`, at half the smallest row count the
  table has recently had.
* NULL_RATE_CEILING -- `COLUMN_NULL_RATE_MAX`, just above the worst null rate of
  a column that is normally complete.

A proposal is a `QualityRuleProposal` behind a `QUALITY_RULE_PROPOSAL` review.
That object type is T2 -- a failing rule gates governed tools -- so this is the
one task agent whose spec raises its proposal ceiling, to
`task_agent.HARD_MAX_PROPOSAL_TIER`, and every proposal it makes is decided by a
person: the reviewer agent's decision ceiling is T1 and cannot be raised.

Nothing here calls a model, reads a source value, or creates a `QualityRule`.
Only a person's approval does (`quality_rule_proposals.decide_quality_rule_proposal`).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any, Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.quality_rule_proposals import (
    QUALITY_RULE_PROPOSAL_OBJECT_TYPE,
    RuleCandidate,
    build_quality_rule_proposal,
    null_rate_ceiling_candidates,
    row_count_floor_candidates,
    rule_is_covered,
)
from aida.review_risk_tiers import TIER_T2
from aida.security import SecurityContext
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    CapabilityWork,
    TaskAgentCapability,
    TaskAgentItem,
    TaskAgentOutcome,
    TaskAgentRun,
    TaskAgentRunRequest,
    TaskAgentSpec,
    run_task_agent,
)
from atlas.platform.config import Settings

CAPABILITY_ROW_COUNT_FLOOR: Final = "ROW_COUNT_FLOOR"
CAPABILITY_NULL_RATE_CEILING: Final = "NULL_RATE_CEILING"

#: A rule or a proposal for the same (table, column, rule type) appeared after
#: the candidates were selected -- another run, or a person, got there first.
SKIP_ALREADY_COVERED: Final = "rule_or_proposal_exists"

#: Candidates examined per run, as a multiple of the proposal limit. Selection
#: already excludes every covered rule key, so this only bounds the read.
_EXAMINE_FACTOR: Final = 4
_REQUESTED_ACTION: Final = "APPROVE_QUALITY_RULE"

QUALITY_AGENT: Final = TaskAgentSpec(
    key="quality",
    audit_roles=frozenset({"DataSteward"}),
    capabilities=(
        TaskAgentCapability(
            key=CAPABILITY_ROW_COUNT_FLOOR,
            object_type=QUALITY_RULE_PROPOSAL_OBJECT_TYPE,
            intent="quality.propose_row_count_floor",
            producer="quality_rule_proposals: half the smallest row count in recent profiles",
        ),
        TaskAgentCapability(
            key=CAPABILITY_NULL_RATE_CEILING,
            object_type=QUALITY_RULE_PROPOSAL_OBJECT_TYPE,
            intent="quality.propose_null_rate_ceiling",
            producer=(
                "quality_rule_proposals: worst recent null rate of a normally "
                "complete column, plus headroom"
            ),
        ),
    ),
    # T2: see the module docstring. A person decides every proposal.
    max_proposal_tier=TIER_T2,
)


def _inputs(candidate: RuleCandidate) -> dict[str, Any]:
    """Value-free: which rule key, from how much history."""
    return {
        "rule_type": candidate.rule_type,
        "table_id": str(candidate.table_id),
        "column_id": str(candidate.column_id) if candidate.column_id else None,
        "profiles_used": candidate.derived.profiles_used,
    }


async def _row_count_floors(run: TaskAgentRun) -> None:
    candidates = await row_count_floor_candidates(
        run.session,
        run.organization_id,
        datasource_id=run.datasource_id,
        limit=run.outcome.limit * _EXAMINE_FACTOR,
    )
    await _propose_each(run, CAPABILITY_ROW_COUNT_FLOOR, candidates)


async def _null_rate_ceilings(run: TaskAgentRun) -> None:
    candidates = await null_rate_ceiling_candidates(
        run.session,
        run.organization_id,
        datasource_id=run.datasource_id,
        limit=run.outcome.limit * _EXAMINE_FACTOR,
    )
    await _propose_each(run, CAPABILITY_NULL_RATE_CEILING, candidates)


async def _propose_each(
    run: TaskAgentRun, capability: str, candidates: list[RuleCandidate]
) -> None:
    proposed = 0
    for candidate in candidates:
        if proposed >= run.outcome.limit:
            return
        if not await run.may_continue():
            return
        item = run.add(
            await run.guarded(
                capability,
                subject_id=candidate.subject_id,
                subject_name=candidate.subject_name,
                work=partial(_propose, run, capability, candidate),
            )
        )
        if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
            proposed += 1


async def _propose(
    run: TaskAgentRun, capability: str, candidate: RuleCandidate
) -> TaskAgentItem:
    session = run.session
    derived = candidate.derived
    if await rule_is_covered(
        session,
        table_id=candidate.table_id,
        column_id=candidate.column_id,
        rule_type=candidate.rule_type,
    ):
        return run.item(
            capability,
            action=ACTION_SKIPPED,
            subject_id=candidate.subject_id,
            subject_name=candidate.subject_name,
            reason=SKIP_ALREADY_COVERED,
        )
    if not run.proposing:
        return run.item(
            capability,
            action=ACTION_WOULD_PROPOSE,
            subject_id=candidate.subject_id,
            subject_name=candidate.subject_name,
            confidence=derived.confidence,
        )
    proposal = build_quality_rule_proposal(
        candidate,
        organization_id=run.organization_id,
        created_by=run.principal_id,
        agent_run=run.outcome.run_id,
    )
    session.add(proposal)
    await session.flush()
    review = await run.open_review(
        capability,
        object_id=proposal.id,
        requested_action=_REQUESTED_ACTION,
        details={"rule_type": candidate.rule_type, "threshold": derived.threshold},
    )
    proposal.governance_review_id = review.id
    return await run.proposed(
        capability,
        review=review,
        object_id=proposal.id,
        subject_id=candidate.subject_id,
        subject_name=candidate.subject_name,
        inputs=_inputs(candidate),
        confidence=derived.confidence,
    )


QUALITY_WORK: Final[Mapping[str, CapabilityWork]] = {
    CAPABILITY_ROW_COUNT_FLOOR: _row_count_floors,
    CAPABILITY_NULL_RATE_CEILING: _null_rate_ceilings,
}


async def run_quality_agent(
    session: AsyncSession,
    organization_id: UUID,
    *,
    request: TaskAgentRunRequest,
    settings: Settings,
    triggered_by: SecurityContext,
) -> TaskAgentOutcome:
    """One bounded quality run; see `task_agent.run_task_agent`."""
    return await run_task_agent(
        session,
        organization_id,
        spec=QUALITY_AGENT,
        work=QUALITY_WORK,
        request=request,
        settings=settings,
        triggered_by=triggered_by,
    )
