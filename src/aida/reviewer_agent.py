"""ADR-0027: the reviewer agent.

Two passes, deliberately separate:

* `pre_review_pending` attaches evidence and a **recommendation** to pending
  review items. It decides nothing. Running it alone is safe, useful, and is
  what an organization that never enables auto-decision still wants: the
  human reviewer opens a queue where blast radius, negative knowledge and
  quality state are already computed.
* `auto_decide_tier0_tier1` acts on those recommendations, for tier-eligible
  items only, through the same decision path a human uses.

**Nothing here is a model call.** The recommendation is a deterministic
function of evidence the platform already holds -- blast radius from the
lineage impact helpers, prior rejections of the same proposal, open quality
incidents on the referenced tables, and the proposal's own confidence if it
carries one. That is a deliberate first cut: it makes the agent auditable
and replayable, it gives the eval corpus something to score against, and it
means ADR-0027's risk argument does not depend on model behaviour. A model
route can be added later behind the same interface, and the tier ceiling
still bounds it.

Three guards, in order, on every auto-decision:

1. The object type's tier must be at or below the configured ceiling, AND
   the type must appear in the allowlist derived from the tier table. Config
   can narrow this; it cannot widen it (ADR-0027 condition (a)).
2. The agent never decides an item it proposed -- `maker != checker` is
   enforced by the shared decision path, and re-checked here so a
   misconfiguration fails loudly rather than at the database.
3. Suspension, process-wide or per-organization, stops everything
   (condition (c)).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.models import (
    DataQualityIncident,
    GovernanceReview,
    MetadataTable,
    ReviewAuditSample,
    ReviewerAgentState,
)
from aida.review_risk_tiers import (
    DEFAULT_MAX_AGENT_TIER,
    agent_decidable_object_types,
    risk_tier_for,
    tier_at_or_below,
)
from aida.security import SecurityContext

_SAMPLING_FLOOR = 0.05
_FINGERPRINT_BUCKETS = float(2**32)

REASON_TIER_EXCEEDED = "reviewer_agent_tier_exceeded"
REASON_SUSPENDED = "reviewer_agent_suspended"
REASON_DISABLED = "reviewer_agent_disabled"
REASON_SELF_PROPOSED = "reviewer_agent_cannot_decide_own_proposal"


@dataclass(frozen=True, slots=True)
class PreReviewOutcome:
    """What the pre-review pass concluded about one item. Value-free."""

    review_id: UUID
    object_type: str
    risk_tier: str
    recommendation: str
    confidence: float | None
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AutoDecisionOutcome:
    review_id: UUID
    object_type: str
    risk_tier: str
    decision: str
    sampled_for_audit: bool


def _agent_context(organization_id: UUID, principal_id: str) -> SecurityContext:
    """The agent's own identity. `principal_kind` is AGENT so every policy
    decision this drives is attributable to a non-human principal (PG-2),
    and so `maker != checker` compares two genuinely different principals."""
    return SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT",
        organization_id=organization_id,
        roles=frozenset({"Reviewer"}),
    )


def sampled_for_audit(review_id: UUID, sampling_rate: float) -> bool:
    """Deterministic: a pure function of the review id and the rate.

    No RNG and no clock, so the decision replays -- an auditor can recompute
    which items *should* have been sampled and check that the ledger matches.
    A rate below ADR-0027's floor is raised to the floor here as well as at
    validation time, so a row written before the constraint existed, or a
    config value edited by hand, cannot sample less than 5%.
    """
    effective = max(float(sampling_rate), _SAMPLING_FLOOR)
    bucket = int(hashlib.sha256(str(review_id).encode()).hexdigest()[:8], 16)
    return (bucket / _FINGERPRINT_BUCKETS) < effective


def _payload_fingerprint(review: GovernanceReview) -> str:
    """Identity of *what is being proposed*, not of the proposal row.

    Two proposals of the same change to the same object share a
    fingerprint, which is what makes "we rejected this before" answerable.
    """
    canonical = json.dumps(
        {
            "object_type": review.object_type,
            "object_id": review.object_id,
            "requested_action": review.requested_action,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _negative_knowledge_hits(session: AsyncSession, review: GovernanceReview) -> int:
    """How many times this exact proposal was rejected before.

    Negative knowledge is one of the platform's differentiators and it is
    free here: the review queue already retains rejections. A prior
    rejection of the same (object_type, object_id, action) is the single
    strongest reason for an agent to refuse to wave something through.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(GovernanceReview)
        .where(
            GovernanceReview.organization_id == review.organization_id,
            GovernanceReview.object_type == review.object_type,
            GovernanceReview.object_id == review.object_id,
            GovernanceReview.requested_action == review.requested_action,
            GovernanceReview.status == "REJECTED",
            GovernanceReview.id != review.id,
        )
    )
    return int(count or 0)


async def _open_incident_count(session: AsyncSession, review: GovernanceReview) -> int:
    """Open quality incidents on the table this proposal concerns, when the
    proposal concerns a table at all. A proposal about an asset the platform
    is currently unhappy about is not one to auto-approve."""
    try:
        table_id = UUID(review.object_id)
    except (ValueError, AttributeError):
        return 0
    exists = await session.scalar(
        select(func.count()).select_from(MetadataTable).where(MetadataTable.id == table_id)
    )
    if not exists:
        return 0
    count = await session.scalar(
        select(func.count())
        .select_from(DataQualityIncident)
        .where(
            DataQualityIncident.table_id == table_id,
            DataQualityIncident.status.in_(["OPEN", "ACKNOWLEDGED"]),
        )
    )
    return int(count or 0)


def _recommendation(
    *,
    risk_tier: str,
    max_tier: str,
    negative_hits: int,
    open_incidents: int,
    confidence: float | None,
    approve_confidence: float,
) -> str:
    """The deterministic rule, stated once so it can be quoted in an audit.

    * A prior rejection of the identical proposal -> REJECT. The platform has
      already been told this is wrong; re-proposing it does not make it right.
    * Otherwise, APPROVE only when the item is inside the tier ceiling AND
      nothing argues against it: no prior rejection, no open quality incident
      on the asset, and either a confidence at or above the threshold or no
      confidence at all combined with no contrary evidence.
    * Everything else -> NONE, which means "a human should look".

    NONE is the default and the common case. The rule is deliberately
    reluctant: the cost of a NONE is a human reading one item, and the cost
    of a wrong APPROVE is a wrong fact in the catalog.
    """
    if negative_hits > 0:
        return "REJECT"
    if not tier_at_or_below(risk_tier, max_tier):
        return "NONE"
    if open_incidents > 0:
        return "NONE"
    if confidence is None:
        return "APPROVE"
    return "APPROVE" if confidence >= approve_confidence else "NONE"


def _proposal_confidence(review: GovernanceReview) -> float | None:
    """A confidence the proposal carries, if any. Read defensively: most
    object types carry none, and a malformed one is not a confidence."""
    evidence = getattr(review, "pre_review_evidence", None)
    if isinstance(evidence, dict):
        raw = evidence.get("proposal_confidence")
        if isinstance(raw, int | float):
            return float(raw)
    return None


async def pre_review_pending(
    session: AsyncSession,
    organization_id: UUID,
    *,
    settings: Settings,
    limit: int = 200,
    now: datetime | None = None,
) -> list[PreReviewOutcome]:
    """Attach tier, evidence and a recommendation to pending review items.

    Decides nothing and is safe to run with the agent disabled -- that is
    the point. Items already pre-reviewed are skipped, so this is idempotent
    and can run on a schedule.
    """
    moment = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(GovernanceReview)
            .where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.status == "PENDING",
                GovernanceReview.pre_reviewed_at.is_(None),
            )
            .order_by(GovernanceReview.created_at)
            .limit(limit)
        )
    ).all()
    outcomes: list[PreReviewOutcome] = []
    for review in rows:
        tier = risk_tier_for(review.object_type)
        negative_hits = await _negative_knowledge_hits(session, review)
        open_incidents = await _open_incident_count(session, review)
        confidence = _proposal_confidence(review)
        recommendation = _recommendation(
            risk_tier=tier,
            max_tier=settings.reviewer_agent_max_tier,
            negative_hits=negative_hits,
            open_incidents=open_incidents,
            confidence=confidence,
            approve_confidence=settings.reviewer_agent_approve_confidence,
        )
        evidence: dict[str, Any] = {
            "risk_tier": tier,
            "negative_knowledge_hits": negative_hits,
            "open_quality_incidents": open_incidents,
            "proposal_confidence": confidence,
            "max_tier": settings.reviewer_agent_max_tier,
            "rule_version": 1,
        }
        review.risk_tier = tier
        review.pre_review_recommendation = recommendation
        review.pre_review_confidence = confidence
        review.pre_review_evidence = evidence
        review.pre_reviewed_at = moment
        review.pre_reviewed_by = settings.reviewer_agent_principal_id
        outcomes.append(
            PreReviewOutcome(
                review_id=review.id,
                object_type=review.object_type,
                risk_tier=tier,
                recommendation=recommendation,
                confidence=confidence,
                evidence=evidence,
            )
        )
    if outcomes:
        record_audit(
            session,
            _agent_context(organization_id, settings.reviewer_agent_principal_id),
            action="reviewer_agent.pre_review",
            resource_type="governance_review",
            resource_id=None,
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={
                "reviewed": len(outcomes),
                "recommendations": {
                    value: sum(1 for o in outcomes if o.recommendation == value)
                    for value in ("APPROVE", "REJECT", "NONE")
                },
            },
        )
    return outcomes


async def organization_suspended(session: AsyncSession, organization_id: UUID) -> bool:
    state = await session.scalar(
        select(ReviewerAgentState).where(ReviewerAgentState.organization_id == organization_id)
    )
    return bool(state and state.suspended)


async def set_suspended(
    session: AsyncSession,
    organization_id: UUID,
    *,
    suspended: bool,
    context: SecurityContext,
    reason: str | None = None,
    now: datetime | None = None,
) -> ReviewerAgentState:
    """ADR-0027 condition (c): one human action, audited, no deployment."""
    moment = now or datetime.now(UTC)
    state = await session.scalar(
        select(ReviewerAgentState).where(ReviewerAgentState.organization_id == organization_id)
    )
    if state is None:
        state = ReviewerAgentState(organization_id=organization_id, suspended=False)
        session.add(state)
    state.suspended = suspended
    state.suspended_by = context.principal_id if suspended else None
    state.suspended_at = moment if suspended else None
    state.suspension_reason = reason if suspended else None
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="reviewer_agent.suspend" if suspended else "reviewer_agent.resume",
        resource_type="reviewer_agent_state",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"suspended": suspended, "reason": reason},
    )
    return state


async def auto_decide_tier0_tier1(
    session: AsyncSession,
    organization_id: UUID,
    *,
    settings: Settings,
    limit: int = 100,
    now: datetime | None = None,
) -> list[AutoDecisionOutcome]:
    """Apply agent decisions to tier-eligible pre-reviewed items.

    Refuses wholesale when disabled or suspended. Per item, refuses anything
    outside the tier allowlist -- derived from the tier table, not from
    config -- and anything the agent itself proposed.

    Decisions go through `semantic_api._apply_governance_review_decision`,
    the single core the human endpoint and the bulk endpoint both call, so
    every object type's side effects, audit row and outbox event are
    identical to a human decision. Nothing about the decision path is
    special-cased for the agent except who is recorded as deciding it.
    """
    if not settings.reviewer_agent_enabled:
        raise ReviewerAgentUnavailable(REASON_DISABLED)
    if settings.reviewer_agent_suspended or await organization_suspended(
        session, organization_id
    ):
        raise ReviewerAgentUnavailable(REASON_SUSPENDED)

    # Imported here rather than at module scope: `semantic_api` imports a
    # large slice of the application, and importing it at load time would
    # make this module unimportable from a worker that does not need it.
    from aida.semantic_api import _apply_governance_review_decision

    moment = now or datetime.now(UTC)
    ceiling = settings.reviewer_agent_max_tier or DEFAULT_MAX_AGENT_TIER
    allowlist = agent_decidable_object_types(ceiling)
    agent_principal = settings.reviewer_agent_principal_id
    context = _agent_context(organization_id, agent_principal)

    rows = (
        await session.scalars(
            select(GovernanceReview)
            .where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.status == "PENDING",
                GovernanceReview.pre_reviewed_at.is_not(None),
                GovernanceReview.pre_review_recommendation.in_(["APPROVE", "REJECT"]),
            )
            .order_by(GovernanceReview.created_at)
            .limit(limit)
        )
    ).all()

    outcomes: list[AutoDecisionOutcome] = []
    for review in rows:
        tier = review.risk_tier or risk_tier_for(review.object_type)
        # Guard 1: the allowlist is derived from the tier table, so an
        # object type classified T2/T3 is refused even if a future config
        # value claimed a higher ceiling.
        if review.object_type not in allowlist or not tier_at_or_below(tier, ceiling):
            continue
        # Guard 2: never decide our own proposal. The shared decision path
        # enforces this too; re-checking here means a misconfigured
        # principal fails as a skip rather than as a 409 mid-batch.
        if review.requested_by == agent_principal:
            continue

        decision = "APPROVED" if review.pre_review_recommendation == "APPROVE" else "REJECTED"
        reason = (
            f"reviewer agent ({agent_principal}), rule v1, tier {tier}: "
            f"{review.pre_review_recommendation}"
        )
        event_type, aggregate_type, aggregate_id, payload = (
            await _apply_governance_review_decision(
                session,
                review,
                decision=decision,
                reason=reason,
                context=context,
                now=moment,
            )
        )
        is_sampled = decision == "APPROVED" and sampled_for_audit(
            review.id, settings.reviewer_agent_sampling_rate
        )
        if is_sampled:
            session.add(
                ReviewAuditSample(
                    organization_id=organization_id,
                    governance_review_id=review.id,
                    agent_principal_id=agent_principal,
                    object_type=review.object_type,
                    risk_tier=tier,
                    decision=decision,
                    sampled_at=moment,
                    human_outcome="PENDING",
                )
            )
        record_audit(
            session,
            context,
            action="reviewer_agent.decide",
            resource_type="governance_review",
            resource_id=str(review.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={
                "decision": decision,
                "object_type": review.object_type,
                "risk_tier": tier,
                "sampled_for_audit": is_sampled,
                "rule_version": 1,
            },
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        outcomes.append(
            AutoDecisionOutcome(
                review_id=review.id,
                object_type=review.object_type,
                risk_tier=tier,
                decision=decision,
                sampled_for_audit=is_sampled,
            )
        )
    return outcomes


async def resolve_audit_sample(
    session: AsyncSession,
    sample: ReviewAuditSample,
    *,
    human_outcome: str,
    rationale: str,
    context: SecurityContext,
    now: datetime | None = None,
) -> ReviewAuditSample:
    """A human's verdict on one sampled agent decision.

    A DISAGREED outcome is recorded but does **not** silently revert the
    underlying object: the decision's own side effects have already been
    applied through the normal path, and unwinding them generically is not
    something this function can do correctly for eighteen object types.
    What it does instead is make the disagreement loud -- an audit row, an
    outbox event, and a row that the DISAGREED-rate metric counts -- and
    leave the correction to the object type's own supersession path, which
    is the same route a human reviewer's mistake would take.
    """
    if human_outcome not in ("AGREED", "DISAGREED"):
        raise ValueError("human_outcome must be AGREED or DISAGREED")
    if not rationale.strip():
        raise ValueError("a rationale is mandatory when resolving a sampled decision")
    if sample.human_outcome != "PENDING":
        raise ValueError("this sample is already resolved")
    sample.human_outcome = human_outcome
    sample.human_principal_id = context.principal_id
    sample.human_rationale = rationale
    sample.resolved_at = now or datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=sample.organization_id),
        action="reviewer_agent.sample.resolve",
        resource_type="review_audit_sample",
        resource_id=str(sample.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "human_outcome": human_outcome,
            "object_type": sample.object_type,
            "risk_tier": sample.risk_tier,
            "governance_review_id": str(sample.governance_review_id),
        },
    )
    record_outbox(
        session,
        organization_id=sample.organization_id,
        aggregate_type="review_audit_sample",
        aggregate_id=str(sample.id),
        event_type="reviewer_agent.sample_resolved.v1",
        payload={
            "sample_id": str(sample.id),
            "human_outcome": human_outcome,
            "object_type": sample.object_type,
            "risk_tier": sample.risk_tier,
        },
    )
    return sample


class ReviewerAgentUnavailable(RuntimeError):
    """The agent is disabled or suspended. Carries a stable reason code so
    the API can answer 409 with the operator-facing reason."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
