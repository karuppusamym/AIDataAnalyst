"""R11-AUD02: the review gate for the two access changes INV-8 claimed and did not cover.

INV-8 says the identity that proposes a governed change can never be the identity that
approves it, for any object type. `ACCESS_POLICY` and `WORKSPACE_MEMBERSHIP` were classed
risk tier T3 in `review_risk_tiers` -- "the platform's trust boundary moved", never
agent-decidable -- and had no review adapter at all. So nothing ever opened a review for
them: `create_access_policy` inserted a policy with whatever `status` the caller asked for
(`ACTIVE` included), and `add_member` seated any workspace role, `workspace_owner`
included, and each did it in one call by one principal. The tier was a label on a queue
row that could never exist.

**What now happens, and to which object.** Both changes are *proposals*:

* An access policy is created `DRAFT` -- the state `business_graph.load_policies` has
  always excluded, so a draft is visible to a reviewer and enforces nothing -- and an
  `ACCESS_POLICY` review is opened for it. APPROVE moves it to `ACTIVE`, the only state
  the policy engine loads. REJECT moves it to `REJECTED`, a state nothing loads either.
* A workspace membership is created `PENDING_APPROVAL` and a `WORKSPACE_MEMBERSHIP` review
  is opened for it. `workspace_service.membership_roles` and the entitlement report both
  read `status == "ACTIVE"` only, so a pending membership grants nothing and shows in no
  entitlement. APPROVE moves it to `ACTIVE`; REJECT to `REJECTED`.

Neither needed a migration: both tables already carry a free-text `status` column, and the
readers that decide what is enforced already filter on it. That is also why the states are
these strings rather than new ones -- a new state would be a state some reader forgot to
exclude, and for an access grant "some reader forgot" is the failure to design against.

**Why every membership, not only `workspace_owner`.** `review_risk_tiers` classifies the
*type*: `WORKSPACE_MEMBERSHIP` is T3 whatever the role, and `risk_tier_for` only ever
escalates a tier by payload, never lowers it. A direct path for the "lesser" roles would be
a second, unreviewed way to grant the same thing -- `analyst` and `steward` already carry
`READ_DATA`, `EXECUTE_TOOL` and `PROPOSE` -- so the gate is on the type, and the tier table
stays the single statement of what is trust-boundary-moving.

**Who decides.** The same route every other review goes through
(`POST /v1/governance/reviews/{id}/decision`, roles `PlatformAdmin`, `DataSteward`,
`Reviewer`), with the same checks: `check_decision_permitted` refuses the proposer (and a
delegate acting for the proposer) with 409, and `agent_decision_oversight` refuses a
non-human decider for a T3 type with 403, whatever the reviewer-agent ceiling says. Those
live in `governance_decision_service` and are not repeated here. What *is* here is the one
rule that service cannot know: a membership's beneficiary may not approve their own grant.
The maker of a proposal is not the only person with an interest in it.

Registered by `semantic_api` beside every other adapter, for the same reason
`decide_quality_rule_proposal` is: the registry has one home, and a type whose adapter is
absent is a type nobody can decide.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.governance_decision_contracts import TargetEffect
from aida.models import AccessPolicy, GovernanceReview, WorkspaceMembership
from aida.security_types import SecurityContext
from aida.timeutil import is_expired

ACCESS_POLICY_REVIEW_TYPE: Final = "ACCESS_POLICY"
WORKSPACE_MEMBERSHIP_REVIEW_TYPE: Final = "WORKSPACE_MEMBERSHIP"

# `DRAFT` and `ACTIVE` are the two statuses `AccessPolicyCreate` has always allowed and
# `load_policies` has always distinguished; `REJECTED` is the terminal state of a proposal a
# reviewer refused. Not `RETIRED`: that word belongs to a policy that *was* in force, and a
# rejected proposal never was.
POLICY_DRAFT: Final = "DRAFT"
POLICY_ACTIVE: Final = "ACTIVE"
POLICY_REJECTED: Final = "REJECTED"

# `ACTIVE` is what `membership_roles` and the entitlement report read; `PENDING_APPROVAL` is
# the word a source binding and a cross-boundary grant already use for the same situation.
MEMBERSHIP_PENDING: Final = "PENDING_APPROVAL"
MEMBERSHIP_ACTIVE: Final = "ACTIVE"
MEMBERSHIP_REJECTED: Final = "REJECTED"

_TARGET_UNAVAILABLE: Final = "review target is unavailable"


def open_access_review(
    session: AsyncSession,
    *,
    organization_id: UUID,
    object_type: str,
    object_id: UUID,
    requested_action: str,
    requested_by: str,
) -> GovernanceReview:
    """File one access change into the governance review queue, in the caller's transaction.

    Writes the review and the `governance.review_requested.v1` event every other creator
    of a review writes, and nothing else: the change's own audit row stays with the caller,
    which knows what it changed. The review's id is assigned here rather than by a flush, so
    the caller can report it -- a proposer who is told "submitted" needs to be told *where* --
    without forcing the proposal to the database ahead of the commit. That matters because the
    caller turns a unique-constraint clash into a 409 at commit, and a flush before it would
    raise the same clash somewhere nothing translates it.

    Committing is the caller's job. The proposal and its review must land together or not at
    all: a `DRAFT` policy with no review is one nobody can ever activate, and a review with
    no policy is one nobody can decide.
    """
    review = GovernanceReview(
        id=uuid4(),
        organization_id=organization_id,
        object_type=object_type,
        object_id=str(object_id),
        requested_action=requested_action,
        requested_by=requested_by,
    )
    session.add(review)
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
            "requested_action": review.requested_action,
        },
    )
    return review


def _target_id(review: GovernanceReview) -> UUID:
    try:
        return UUID(review.object_id)
    except ValueError:
        # Only this module opens these reviews, and it always writes a UUID -- so a value
        # that is not one is a review that was not opened here. Refuse it the way a missing
        # target is refused; the claim unwinds with the 409 and the review stays pending.
        raise HTTPException(status_code=409, detail=_TARGET_UNAVAILABLE) from None


async def decide_access_policy(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> TargetEffect:
    """The governance adapter for `ACCESS_POLICY`: activate the draft, or reject it.

    Handed a review already claimed, like every adapter, so it never asks whether the review
    is still pending or whether the decider may decide it. It asks about its own target: a
    policy that is gone, another organization's, or no longer a `DRAFT` is a 409, and the
    claim unwinds with it -- the review stays pending rather than spending its one decision
    on a policy that has already moved on.

    Approving activates exactly this row. It does not retire an earlier `ACTIVE` version of
    the same `code`, and nothing did before: versions are immutable and each is evaluated on
    its own, so replacing a policy's meaning is a separate change this gate does not make.
    """
    policy = await session.get(AccessPolicy, _target_id(review))
    if policy is None or policy.organization_id != review.organization_id:
        raise HTTPException(status_code=409, detail=_TARGET_UNAVAILABLE)
    if policy.status != POLICY_DRAFT:
        raise HTTPException(status_code=409, detail="access policy is no longer a draft")
    approved = decision == "APPROVE"
    policy.status = POLICY_ACTIVE if approved else POLICY_REJECTED
    record_audit(
        session,
        replace(context, organization_id=review.organization_id),
        action="ACCESS_POLICY_ACTIVATED" if approved else "ACCESS_POLICY_REJECTED",
        resource_type="ACCESS_POLICY",
        resource_id=policy.code,
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "version": policy.version,
            "effect": policy.effect,
            "governance_review_id": str(review.id),
            "proposed_by": review.requested_by,
            "reason": reason,
        },
    )
    return TargetEffect(
        "access_policy.activated.v1" if approved else "access_policy.rejected.v1",
        "access_policy",
        str(policy.id),
        {
            "access_policy_id": str(policy.id),
            "code": policy.code,
            "version": policy.version,
            "effect": policy.effect,
            "review_id": str(review.id),
        },
    )


async def decide_workspace_membership(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> TargetEffect:
    """The governance adapter for `WORKSPACE_MEMBERSHIP`: seat the member, or reject them.

    Same contract as `decide_access_policy`: a membership that is gone, another
    organization's, or no longer `PENDING_APPROVAL` is a 409.

    Two refusals are specific to an approval. The beneficiary may not approve their own
    grant -- directly or by acting under a delegation from them -- because
    `check_decision_permitted` compares the decider only with the *proposer*, and a member
    proposed by someone else could otherwise sign off on their own access. And a membership
    whose expiry has already passed is not approved: it would be recorded as granted while
    granting nothing (`membership_roles` treats an expired row as absent), which is a
    misleading entry in exactly the ledger an access review reads. Rejecting it is always
    allowed.

    `granted_by` is the last principal to act on the grant: the proposer while it is
    pending, the approver once it is active -- the person who switched the access on is the
    one accountable for it, the same rule `decide_quality_rule_proposal` follows for the
    rule it creates. The proposer stays on the review (`requested_by`) and in the audit row.
    """
    membership = await session.get(WorkspaceMembership, _target_id(review))
    if membership is None or membership.organization_id != review.organization_id:
        raise HTTPException(status_code=409, detail=_TARGET_UNAVAILABLE)
    if membership.status != MEMBERSHIP_PENDING:
        raise HTTPException(
            status_code=409, detail="workspace membership is no longer pending approval"
        )
    approved = decision == "APPROVE"
    if approved:
        if membership.principal_id in (
            context.principal_id,
            context.active_delegator_principal_id,
        ):
            raise HTTPException(
                status_code=409,
                detail="a principal cannot approve their own workspace membership",
            )
        if is_expired(membership.expires_at, now):
            raise HTTPException(
                status_code=409,
                detail=(
                    "workspace membership has already expired; reject it and propose "
                    "a new one"
                ),
            )
        membership.granted_by = context.principal_id
    membership.status = MEMBERSHIP_ACTIVE if approved else MEMBERSHIP_REJECTED
    record_audit(
        session,
        replace(context, organization_id=review.organization_id),
        # `WORKSPACE_MEMBER_ADDED` keeps its name and now means what it always read as: the
        # member was added. It used to be written when a request was accepted, which was the
        # same instant only because nothing stood between the two.
        action="WORKSPACE_MEMBER_ADDED" if approved else "WORKSPACE_MEMBER_REJECTED",
        resource_type="WORKSPACE",
        resource_id=str(membership.workspace_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "role": membership.role,
            "principal_kind": membership.principal_kind,
            "member_principal_id": membership.principal_id,
            "governance_review_id": str(review.id),
            "proposed_by": review.requested_by,
            "reason": reason,
        },
    )
    return TargetEffect(
        "workspace_membership.approved.v1" if approved else "workspace_membership.rejected.v1",
        "workspace_membership",
        str(membership.id),
        {
            "membership_id": str(membership.id),
            "workspace_id": str(membership.workspace_id),
            "role": membership.role,
            "principal_kind": membership.principal_kind,
            "review_id": str(review.id),
        },
    )
