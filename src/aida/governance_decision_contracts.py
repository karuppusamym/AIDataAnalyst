"""Dependency-light contracts for the one governance decision path (F05/R03).

`aida.governance_decision_service` owns the invariant "a governance review
reaches exactly one terminal decision, and that decision produces exactly one
set of side effects". Three surfaces call it -- the single-item endpoint, the
bulk endpoint and the sample-review endpoint in the routers, plus the
reviewer agent's automation -- and *none* of them may be imported by the
service itself: the review (R03) records a four-module cycle
`semantic_api -> agent_contract_request_api -> agent_contract_api ->
reviewer_agent -> semantic_api` created precisely by automation reaching back
into a router for the decision core.

This module is the shared vocabulary that lets the service stay on the
service side of that boundary. It imports **nothing from `aida`** (stdlib
only) on purpose: every caller can depend on it without acquiring a
dependency on any of the others.

The four outcomes are deliberately distinct rather than a success/failure
boolean, because a bulk batch has to be able to say *why* an item did not
apply:

* ``APPLIED``       -- this caller won the claim and the transition happened.
* ``CONFLICT``      -- somebody else reached the terminal decision first.
                       Not an error in this caller's request; the review's
                       refreshed state is attached so the caller can show
                       what actually happened.
* ``NOT_PERMITTED`` -- the caller may not decide this review at all
                       (cross-organization, maker == checker, or an object
                       type no adapter claims).
* ``FAILED``        -- the transition was permitted and claimed but its
                       target-specific effects could not be applied (a
                       missing or already-moved target object, a gate that
                       did not pass). The claim is rolled back with the
                       item's own savepoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

#: The two verdicts a checker (human or agent) may record. This is the wire
#: vocabulary `GovernanceDecisionRequest.decision` already uses; the terminal
#: `GovernanceReview.status` values are the past-tense forms below.
DecisionVerdict = Literal["APPROVE", "REJECT"]

DecisionOutcome = Literal["APPLIED", "CONFLICT", "NOT_PERMITTED", "FAILED"]

#: verdict -> the terminal `GovernanceReview.status` it writes. Declared once
#: here so no caller re-derives it with its own `if decision == "APPROVE"`,
#: which is how `reviewer_agent` came to pass the *status* ("APPROVED") where
#: the verdict ("APPROVE") was expected.
TERMINAL_STATUS: Final[dict[str, str]] = {"APPROVE": "APPROVED", "REJECT": "REJECTED"}

#: The only status a review may be claimed from. A review is claimable once,
#: ever: this is the compare-and-set predicate, not a preference.
CLAIMABLE_STATUS: Final = "PENDING"


def normalize_verdict(decision: str) -> DecisionVerdict:
    """Accept a verdict, refuse anything else -- including a terminal status.

    Raises `ValueError` rather than silently treating an unrecognised value
    as a rejection, which is what the previous
    `"APPROVED" if decision == "APPROVE" else "REJECTED"` expression did: a
    caller passing the past-tense form got a rejection and no complaint.
    """
    verdict = decision.upper()
    if verdict not in TERMINAL_STATUS:
        raise ValueError(f"unsupported governance decision verdict: {decision!r}")
    return "APPROVE" if verdict == "APPROVE" else "REJECT"


@dataclass(frozen=True, slots=True)
class TargetEffect:
    """What one target-type adapter did, as the outbox event describing it.

    Adapters return this instead of a bare 4-tuple so the composition step
    (which records the event exactly once) cannot be handed the members in
    the wrong order.
    """

    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def as_tuple(self) -> tuple[str, str, str, dict[str, Any]]:
        """The legacy `(event_type, aggregate_type, aggregate_id, payload)`
        shape, for the call sites that still destructure it."""
        return self.event_type, self.aggregate_type, self.aggregate_id, self.payload


@dataclass(frozen=True, slots=True)
class ReviewStateSnapshot:
    """The authoritative state of a review, re-read from the database.

    Handed to the *losing* caller of a contended decision so it can report
    what actually happened rather than a bare "conflict" -- the review's
    acceptance criterion for F05 is "the losing caller receives conflict *and*
    refreshed state".
    """

    review_id: UUID
    status: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {
            "review_id": str(self.review_id),
            "status": self.status,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision_reason": self.decision_reason,
        }


class GovernanceDecisionRefused(Exception):
    """One review could not be decided by this caller, and why.

    `outcome` is the machine-readable reason (see the module docstring),
    `detail` is the operator-facing sentence the HTTP layer already uses, and
    `http_status` is the status code the existing endpoints already return
    for that class of refusal -- 403 for a cross-organization attempt, 409 for
    a lost claim or an unusable target, 422 for an object type nothing
    handles. The mapping is kept here so all three routers and the agent
    agree, rather than each choosing its own code.
    """

    def __init__(
        self,
        outcome: DecisionOutcome,
        detail: str,
        *,
        http_status: int = 409,
        review_state: ReviewStateSnapshot | None = None,
    ) -> None:
        super().__init__(detail)
        self.outcome: DecisionOutcome = outcome
        self.detail = detail
        self.http_status = http_status
        self.review_state = review_state
