"""F05 / T05: the one application service that decides a governance review.

The review found that the single-item endpoint claimed a review with
`SELECT ... FOR UPDATE` before checking `PENDING`, while the bulk endpoint
loaded ordinary ORM objects and checked their *in-memory* status. Two
checkers could therefore both act on the same pending review -- one
approving while the other rejected -- and both would proceed, each writing
its own audit row, outbox event and dependent version bumps.

This module is the single place a review is transitioned. Every surface goes
through it:

* `semantic_api.decide_governance_review` (one review, one checker),
* `semantic_api.bulk_decide_governance_reviews` (PG-3, up to 10,000),
* `asset_description_api` sample review (GL-9), via
  `semantic_api._apply_governance_review_decision`, and
* `reviewer_agent.auto_decide_tier0_tier1` (ADR-0027 automation).

**The invariant and how it is enforced.** A review is claimable exactly once,
out of `PENDING`. The claim is a single compare-and-set statement:

    UPDATE governance_review
       SET status = :terminal, decided_by = ..., decided_at = ...
     WHERE id = :id AND status = 'PENDING'

`status` *is* this row's state machine, so it is also its concurrency token:
there is no separate version column to keep in step, and the guard is the
same predicate on PostgreSQL and on the in-memory SQLite the tests use --
single-statement atomicity is enough, no row lock is required to make it
correct. Exactly one caller sees `rowcount == 1`; every other caller sees `0`
and is refused with `CONFLICT` plus the review's re-read state. Because the
claim precedes the target-specific effects, the loser never reaches them, so
a contended review yields one audit record, one outbox event and one set of
dependent version bumps.

`lock_reviews_for_decision` additionally takes `FOR UPDATE` row locks in a
deterministic (id) order for the bulk path. That is *not* what makes the
transition correct -- the compare-and-set is -- it is what stops two
overlapping bulk batches from deadlocking each other on PostgreSQL, and what
keeps a batch's reads from drifting under it mid-flight.

**Why the target-type adapters are registered rather than imported.** R03:
the service must not import a router. `semantic_api` owns the per-object-type
adapters (they reach into that layer's publish/reject helpers) and registers
them here at import time -- a router -> service edge, the direction that is
allowed. Nothing here imports `semantic_api`, so
`reviewer_agent -> governance_decision_service` closes no cycle, and the
four-module cycle the review recorded is gone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any, Final, Protocol, cast
from uuid import UUID

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.governance_decision_contracts import (
    CLAIMABLE_STATUS,
    TERMINAL_STATUS,
    AgentOversightOutcome,
    DecisionOutcome,
    GovernanceDecisionRefused,
    ReviewStateSnapshot,
    TargetEffect,
    normalize_verdict,
)
from aida.models import GovernanceReview
from aida.security_types import SecurityContext

logger = structlog.get_logger(__name__)

__all__ = [
    "DecisionOutcome",
    "GovernanceDecisionRefused",
    "ReviewStateSnapshot",
    "TargetEffect",
    "TargetEffectAdapter",
    "AgentDecisionGuard",
    "check_decision_permitted",
    "claim_review",
    "decide_review",
    "lock_reviews_for_decision",
    "record_decision_outbox",
    "register_agent_decision_guard",
    "register_target_adapters",
    "registered_object_types",
]


class TargetEffectAdapter(Protocol):
    """One governed object type's half of a decision.

    An adapter is handed a review that has **already been claimed** -- its
    status is terminal and its `decided_by`/`decided_at` are written -- and is
    responsible only for that object type's own state transition and for
    describing it as an outbox event. It must raise
    `fastapi.HTTPException` (409/422, the codes the routers already return)
    when its target is not in a state the decision can be applied to; the
    caller's savepoint then unwinds the claim with it.
    """

    async def __call__(
        self,
        session: AsyncSession,
        review: GovernanceReview,
        *,
        decision: str,
        reason: str | None,
        context: SecurityContext,
        now: datetime,
    ) -> TargetEffect: ...


_ADAPTERS: dict[str, TargetEffectAdapter] = {}


def register_target_adapters(adapters: Mapping[str, TargetEffectAdapter]) -> None:
    """Register the per-object-type adapters this service dispatches to.

    Called once, at import time, by the module that owns the adapters. The
    direction matters: the owner depends on the service, never the reverse
    (see this module's docstring on R03). Re-registering the same object type
    with a different adapter is a programming error and raises, so a second
    registrar cannot quietly take over an object type's decision.
    """
    for object_type, adapter in adapters.items():
        existing = _ADAPTERS.get(object_type)
        if existing is not None and existing is not adapter:
            raise RuntimeError(
                f"governance decision adapter for {object_type} is already registered"
            )
        _ADAPTERS[object_type] = adapter


class AgentDecisionGuard(Protocol):
    """The oversight regime a **non-human** principal's decision must pass.

    ADR-0027's safety case for automated review rests on four things: a risk
    tier ceiling, a sampled fraction that humans actually read, a suspension
    switch an operator can throw, and the backlog bounds that keep the sample
    honest. All four lived in `reviewer_agent.auto_decide_tier0_tier1` --
    *above* this service -- so they applied to the platform's own reviewer
    agent and to nothing else. An externally-supplied agent identity holding
    the `Reviewer` role reached the decision endpoints directly and got none
    of them: no ceiling, no sample, and an operator's suspension did not stop
    it. The switch was believed to have stopped automated review. It had
    stopped one implementation of it.

    Registered the same way the target adapters are, and for the same reason:
    the owner of the policy depends on this service, never the reverse. The
    difference is the default. An unregistered adapter means an object type
    nobody can decide, which is visible immediately; an unregistered guard
    would mean an agent nobody supervises, which is invisible -- so its
    absence refuses (INV-4).
    """

    async def __call__(
        self,
        session: AsyncSession,
        *,
        review: GovernanceReview,
        decision: str,
        context: SecurityContext,
    ) -> AgentOversightOutcome:
        """A blocking reason, and/or the audit sample this decision owes.

        Sampling is part of the regime rather than a separate concern, and
        this is the one place every agent decision passes through -- but the
        two halves land at different points. See `AgentOversightOutcome`.
        """


_AGENT_GUARD: list[AgentDecisionGuard] = []


def register_agent_decision_guard(guard: AgentDecisionGuard) -> None:
    """Register the oversight regime for non-human decisions. Called once, at
    import time, by the module that owns the policy.

    Re-registering a *different* guard raises rather than replacing, so a
    second registrar cannot quietly relax the regime -- the same rule
    `register_target_adapters` applies for the same reason.
    """
    if _AGENT_GUARD and _AGENT_GUARD[0] is not guard:
        raise RuntimeError("an agent decision guard is already registered")
    if not _AGENT_GUARD:
        _AGENT_GUARD.append(guard)


async def _enforce_agent_oversight(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    context: SecurityContext,
) -> AgentOversightOutcome:
    """Hold a non-human decider to ADR-0027, whatever surface it arrived on.

    Keyed on the *authenticated* principal type, not on a role or a name: an
    identity the provider asserts is an `AGENT` is one, and `Reviewer` is a
    role a human holds too. A human decision never reaches the guard.

    Refuses here, before the claim; returns the audit sample for the caller to
    write after the claim is won.
    """
    if context.principal_type != "AGENT":
        return AgentOversightOutcome()
    if not _AGENT_GUARD:
        raise GovernanceDecisionRefused(
            "NOT_PERMITTED",
            "agent review oversight is not available",
            http_status=403,
        )
    outcome = await _AGENT_GUARD[0](
        session, review=review, decision=decision, context=context
    )
    if outcome.reason is not None:
        raise GovernanceDecisionRefused(
            "NOT_PERMITTED", outcome.reason, http_status=403
        )
    return outcome


def registered_object_types() -> frozenset[str]:
    """Every object type this service can currently decide. Used by the test
    that keeps the registry in step with the object types the review queue
    actually raises."""
    return frozenset(_ADAPTERS)


def snapshot_review(review: GovernanceReview) -> ReviewStateSnapshot:
    """The review's state as a plain value, safe to hand to a caller whose
    own session may be about to roll back."""
    return ReviewStateSnapshot(
        review_id=review.id,
        status=review.status,
        decided_by=review.decided_by,
        decided_at=review.decided_at,
        decision_reason=review.decision_reason,
    )


# ---------------------------------------------------------------------------
# (a) shared preconditions
# ---------------------------------------------------------------------------


def check_decision_permitted(review: GovernanceReview, context: SecurityContext) -> None:
    """The rules about *who* may decide this review, independent of its state.

    Two of them, both INV-8 (maker != checker):

    1. the review must belong to the caller's organization (a PlatformAdmin
       may cross that boundary, matching `security.enforce_organization`);
    2. neither the caller nor -- when acting under a delegation grant (PG-4)
       -- the delegator that lent them the role may decide their own
       proposal. Without the second rule a delegate could rubber-stamp what
       the delegator proposed, which is self-approval by proxy.

    Raises `GovernanceDecisionRefused(NOT_PERMITTED)`. It is deliberately
    separate from `claim_review`: this answers "may this principal ever
    decide this review", which does not change when another checker wins the
    race, and is therefore never reported as a conflict.
    """
    if "PlatformAdmin" not in context.roles and context.organization_id != review.organization_id:
        raise GovernanceDecisionRefused(
            "NOT_PERMITTED", "cross-organization access denied", http_status=403
        )
    if review.requested_by == context.principal_id or (
        context.active_delegator_principal_id is not None
        and review.requested_by == context.active_delegator_principal_id
    ):
        raise GovernanceDecisionRefused(
            "NOT_PERMITTED", "maker-checker separation is required", http_status=409
        )


async def lock_reviews_for_decision(
    session: AsyncSession, review_ids: Sequence[UUID]
) -> dict[UUID, GovernanceReview]:
    """Load the reviews a batch is about to decide, ordered and row-locked.

    `ORDER BY id` before `FOR UPDATE` is the deadlock-avoidance rule: two
    overlapping bulk batches acquire their shared rows in the same order, so
    one waits rather than both dying. On SQLite `FOR UPDATE` compiles away --
    which is exactly why it is not what the correctness of a decision rests
    on. `claim_review` below is, and it behaves identically on both engines.
    """
    ordered = sorted(set(review_ids), key=lambda value: value.bytes)
    if not ordered:
        return {}
    rows = (
        await session.scalars(
            select(GovernanceReview)
            .where(GovernanceReview.id.in_(ordered))
            .order_by(GovernanceReview.id)
            .with_for_update()
        )
    ).all()
    return {row.id: row for row in rows}


# ---------------------------------------------------------------------------
# (b) the atomic claim
# ---------------------------------------------------------------------------

#: The columns the claim writes -- the decision itself, and nothing else.
CLAIMED_COLUMNS: Final = ("status", "decided_by", "decision_reason", "decided_at", "updated_at")


def claimable_columns(review: GovernanceReview) -> dict[str, Any]:
    """The review's current values for the columns a claim overwrites.

    Captured before the claim so an unwound claim can be undone in memory
    without a database round trip -- see `claim_review` on why the ORM object
    is written by hand rather than synchronized by the ORM.
    """
    return {column: getattr(review, column) for column in CLAIMED_COLUMNS}


async def claim_review(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> None:
    """Move one review PENDING -> terminal, or refuse with `CONFLICT`.

    The whole transition is one `UPDATE ... WHERE id = :id AND status =
    'PENDING'`. Whether the caller loses to another checker's commit, to a
    sibling item in the same batch, or to the reviewer agent, the losing
    side sees `rowcount == 0` and never runs the target's side effects.

    The in-memory ORM object is then brought in step by hand, with
    `set_committed_value` rather than by attribute assignment or by letting
    the ORM synchronize the session. That is a cost decision made against
    this endpoint's own 10,000-item bound (PG-3), and each alternative was
    measured: `synchronize_session="fetch"` adds a `SELECT` per claim, plain
    assignment marks the object dirty and so adds a second, redundant
    `UPDATE` at the next flush, and `"evaluate"` re-scans the whole identity
    map per claim -- which is quadratic in the size of the batch and pushed
    the existing scale test from seconds to minutes. Writing the committed
    values directly costs nothing, leaves the object clean, and is accurate
    because these are exactly the values the statement just wrote.

    A claim that is later unwound (the target adapter refuses, the caller's
    savepoint rolls back) would leave those hand-written values stale, since
    a clean object is not part of the snapshot a nested rollback restores.
    `decide_review` therefore puts them back itself, from the values it
    captured with `claimable_columns` before calling here.
    """
    verdict = normalize_verdict(decision)
    claimed = {
        "status": TERMINAL_STATUS[verdict],
        "decided_by": context.principal_id,
        "decision_reason": reason,
        "decided_at": now,
        "updated_at": now,
    }
    claim = (
        update(GovernanceReview)
        .where(
            GovernanceReview.id == review.id,
            GovernanceReview.status == CLAIMABLE_STATUS,
        )
        .values(**claimed)
        .execution_options(synchronize_session=False)
    )
    # `AsyncSession.execute` is typed as returning `Result`, which has no
    # `rowcount`; a DML statement always yields a `CursorResult`, which does,
    # and that count is the whole point of this statement.
    result = cast("CursorResult[Any]", await session.execute(claim))
    if result.rowcount == 1:
        for attribute, value in claimed.items():
            set_committed_value(review, attribute, value)
        return

    await session.refresh(review)
    state = snapshot_review(review)
    logger.info(
        "governance_decision_claim_lost",
        review_id=str(review.id),
        object_type=review.object_type,
        attempted_decision=verdict,
        observed_status=state.status,
        decided_by=state.decided_by,
        principal_id=context.principal_id,
    )
    detail = (
        f"governance review is already {state.status.lower()}"
        if state.status != CLAIMABLE_STATUS
        else "governance review could not be claimed"
    )
    raise GovernanceDecisionRefused("CONFLICT", detail, http_status=409, review_state=state)


# ---------------------------------------------------------------------------
# (c) the whole transition: preconditions -> claim -> target adapter
# ---------------------------------------------------------------------------


async def decide_review(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> TargetEffect:
    """Apply one governance decision, exactly once.

    Order is the point. Permissions first (they cannot be raced), then the
    compare-and-set claim, then -- only for the caller that won it -- the
    object type's own effects. Callers wrap this in their own savepoint when
    they want per-item partial success: a refusal or an adapter failure
    unwinds the claim with everything else that item wrote.

    Raises `GovernanceDecisionRefused` for anything this service can decide
    about (permission, conflict, unregistered object type); an adapter's own
    `HTTPException` propagates untouched, because the routers already
    translate those and the reasons are target-specific.
    """
    check_decision_permitted(review, context)
    oversight = await _enforce_agent_oversight(
        session, review, decision=decision, context=context
    )
    adapter = _ADAPTERS.get(review.object_type)
    if adapter is None:
        raise GovernanceDecisionRefused(
            "NOT_PERMITTED", "unsupported governance object type", http_status=422
        )
    unclaimed = claimable_columns(review)
    await claim_review(session, review, decision=decision, reason=reason, context=context, now=now)
    if oversight.sample is not None:
        # Only now. Before the claim, every loser of a contended race inserts
        # this row too and dies on its unique constraint -- see
        # `AgentOversightOutcome`.
        session.add(oversight.sample)
    try:
        return await adapter(
            session, review, decision=decision, reason=reason, context=context, now=now
        )
    except BaseException:
        # The caller's savepoint is about to unwind this item's writes,
        # including the claim -- but the claim was applied to the in-memory
        # review as committed state, which a nested rollback does not
        # restore (see `claim_review`). Put the pre-claim values back, so the
        # in-memory review agrees with the row: PENDING, undecided. Done
        # here rather than with `session.expire` deliberately -- an expired
        # object reloads on the *next attribute access*, which in async
        # SQLAlchemy fails outright if that access happens outside an await.
        for column, value in unclaimed.items():
            set_committed_value(review, column, value)
        raise


# ---------------------------------------------------------------------------
# (d) the common side-effect composition
# ---------------------------------------------------------------------------


def record_decision_outbox(
    session: AsyncSession, review: GovernanceReview, effect: TargetEffect
) -> None:
    """The one place a decided review's outbox event is written.

    Every caller uses it, so "one terminal decision produces one outbox
    event" is a property of this function's single call per won claim rather
    than of four call sites agreeing.
    """
    record_outbox(
        session,
        organization_id=review.organization_id,
        aggregate_type=effect.aggregate_type,
        aggregate_id=effect.aggregate_id,
        event_type=effect.event_type,
        payload=effect.payload,
    )


def record_decision_audit(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    context: SecurityContext,
    action: str,
    outcome: str = "SUCCESS",
    details: Mapping[str, Any] | None = None,
) -> None:
    """The audit row for one decided review, scoped to the review's own
    organization (a PlatformAdmin deciding across the boundary must not file
    the record under their own org)."""
    record_audit(
        session,
        replace(context, organization_id=review.organization_id),
        action=action,
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details=dict(details or {}),
    )


def delegation_details(context: SecurityContext) -> dict[str, Any]:
    """The two delegation fields every governance audit row carries."""
    return {
        "via_delegation_id": (
            str(context.active_delegation_id) if context.active_delegation_id else None
        ),
        "via_delegator_principal_id": context.active_delegator_principal_id,
    }


def unregistered_object_types(expected: Iterable[str]) -> frozenset[str]:
    """Which of `expected` this service could not decide today. Exists so a
    test can assert the registry is complete rather than discovering a gap as
    a 422 in production."""
    return frozenset(expected) - registered_object_types()
