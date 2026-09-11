"""ADR-0029: the shared runtime of contracted task agents.

A *task agent* is a registered agent whose unit of work is a proposal: it reads
the platform's own backlog, drafts something with an existing producer, and
opens a `GovernanceReview` as its own workload identity. The steward agent is
the first. The analyst orchestrator is not one -- its unit of work is an
`AgentRun` -- and neither is the ADR-0027 reviewer agent, which decides rather
than proposes.

Everything that makes such an agent governable is the same for every one of
them, so it lives here and an agent module cannot implement it differently:

1. **Authority.** The agent acts only as its configured principal
   (`<key>_agent_principal_id`), only under the one contract naming that
   principal that hangs off an APPROVED `AGENT`-kind AI asset version in this
   organization. No contract, no approved version, two approved candidates, or
   a principal another agent is configured with is a refusal -- never an
   unconstrained run (AG-10's fail-closed rule). Reserving every other agent's
   principal is what keeps two agents from sharing an identity, and a maker from
   sharing one with the reviewer agent.
2. **The kill switch** -- the agent's own, its tier's, the organization's and
   the model gateway's (`agent_contracts.agent_kill_blocking_reason`) -- is
   checked before the run and re-read from the database before every item.
3. **The autonomy tier is a ceiling.** T0 observes: the run reports what it
   would propose and opens nothing. T1 proposes. T2 and T3 propose exactly what
   T1 does, because no task agent has a branch that applies its own output; the
   tier can narrow an agent and cannot widen it.
4. **Budgets.** A per-run proposal limit clamped by configuration, a bound on
   the agent's own undecided proposals, and the contract's wall-clock cap.
5. **One way to write.** `TaskAgentRun.open_review` is the only path by which an
   agent's work reaches anyone, and it refuses any object type above
   `review_risk_tiers.HARD_MAX_AGENT_TIER` -- so an agent that tried to propose
   published meaning or a trust-boundary change would fail at its first
   proposal rather than ship.
6. **A ledger and an outcome measure.** One `AgentTask` per proposal, linked to
   its review and carrying ids, hashes and scores only (INV-6); and an
   acceptance rate per object type that is `None`, never 0, until a reviewer has
   decided something.

**Two ways a run ends early, deliberately different.** Authority withdrawn
mid-run -- a kill switch engaged, the tier lowered to T0, the contract or its
approved version gone -- raises `TaskAgentRefused`, and the caller rolls the
whole run back: nothing produced by a run whose licence was withdrawn survives
it, the same way a mid-batch suspension discards the reviewer agent's batch. A
*budget* reached mid-run ends the run and keeps what it did, reported as
`stopped_reason`.

An agent module contributes a `TaskAgentSpec` -- its key, the capabilities it
has and the object types they propose -- and one coroutine per capability that
finds work and hands each item to `TaskAgentRun.guarded`. Nothing here calls a
model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_budget import wall_clock_violation
from aida.agent_contracts import REASON_CONTRACT_MISSING, agent_kill_blocking_reason
from aida.agent_tasks import finish_agent_task, record_agent_task
from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.models import AgentContract, AiAsset, AiAssetVersion, GovernanceReview
from aida.review_risk_tiers import HARD_MAX_AGENT_TIER, risk_tier_for, tier_at_or_below
from aida.security import SecurityContext
from atlas.platform.config import Settings

Mode = Literal["OBSERVE", "PROPOSE"]

#: How every task agent reaches its output today. Reported on every surface
#: that describes an agent; there is no model route to name.
METHOD_DETERMINISTIC: Final = "DETERMINISTIC"

ACTION_PROPOSED: Final = "PROPOSED"
ACTION_WOULD_PROPOSE: Final = "WOULD_PROPOSE"
ACTION_SKIPPED: Final = "SKIPPED"
ACTION_FAILED: Final = "FAILED"

# Refusals: the agent may not act, and a refused run keeps nothing.
REASON_CONTRACT_AMBIGUOUS: Final = "agent_contract_unresolved"
REASON_VERSION_NOT_APPROVED: Final = "agent_version_not_approved"
REASON_PRINCIPAL_RESERVED: Final = "agent_principal_reserved"
REASON_AUTONOMY_WITHDRAWN: Final = "agent_autonomy_withdrawn"
REASON_OBJECT_TYPE_ABOVE_CEILING: Final = "agent_object_type_above_ceiling"
REASON_QUEUE_NOT_PERMITTED: Final = "agent_review_queue_not_permitted"
# Stops: a budget ran out, and the run keeps what it already did. The contract's
# wall-clock cap reports `agent_budget.REASON_WALL_CLOCK_CAP`.
STOP_REVIEW_BACKLOG: Final = "agent_review_backlog_full"

#: Where a capability's proposals are decided. Most go to the shared
#: `GovernanceReview` queue, where the ADR-0027 tier table bounds what an agent
#: may ask for. A few object kinds have a dedicated queue of their own, and an
#: agent may write into one only if it is on `_HUMAN_ONLY_QUEUES` -- a queue no
#: agent can decide from, so maker != checker holds there by construction.
QUEUE_GOVERNANCE_REVIEW: Final = "GOVERNANCE_REVIEW"
#: ADR-0026's per-edge review of parsed lineage. Only human reviewer roles decide
#: it, and its maker-checker compares the edge's `created_by` with the reviewer.
QUEUE_PARSED_LINEAGE: Final = "PARSED_LINEAGE_REVIEW"
_HUMAN_ONLY_QUEUES: Final = frozenset({QUEUE_PARSED_LINEAGE})

#: Contracts read when resolving authority. More than one APPROVED candidate is
#: refused as ambiguous; this only bounds the read.
_CONTRACT_SCAN_LIMIT: Final = 20
#: Every setting with this suffix is an agent's workload identity.
_PRINCIPAL_SETTING_SUFFIX: Final = "_agent_principal_id"


class TaskAgentRefused(RuntimeError):
    """The agent may not act, or its authority was withdrawn mid-run.

    Raised before the first write, or -- for a withdrawal noticed mid-run --
    after some. The caller rolls the transaction back in both cases, so a
    refused run leaves no proposal and no task behind. `reason_code` is stable
    and operator-facing.
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def mode_for(autonomy_tier: str) -> Mode:
    """T0 observes; T1 and above propose, and propose identically.

    An unrecognised tier observes: a tier the platform cannot read is not a
    licence to write (INV-4).
    """
    return "PROPOSE" if autonomy_tier in ("T1", "T2", "T3") else "OBSERVE"


# ---------------------------------------------------------------------------
# What an agent is
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskAgentCapability:
    """One kind of proposal an agent makes."""

    key: str
    #: The `GovernanceReview.object_type` its proposals are decided as.
    object_type: str
    #: `AgentTask.intent` for each proposal.
    intent: str
    #: Where the proposal's content comes from, reported verbatim by the state
    #: endpoint so "how does this agent write" is answerable without the code.
    producer: str
    #: Where its proposals are decided (`QUEUE_*`).
    queue: str = QUEUE_GOVERNANCE_REVIEW


#: Proposals an agent has waiting in a dedicated queue: (session, organization
#: id, agent principal) -> count.
PendingCounter = Callable[[AsyncSession, UUID, str], Awaitable[int]]
#: How an agent's proposals in a dedicated queue have been decided.
OutcomeReader = Callable[[AsyncSession, UUID, str], Awaitable[list["TaskAgentOutcomeRow"]]]


@dataclass(frozen=True, slots=True)
class TaskAgentSpec:
    """An agent's identity and capabilities. Its settings are named by `key`:
    `<key>_agent_principal_id`, `<key>_agent_max_proposals_per_run` and
    `<key>_agent_max_pending_proposals`."""

    key: str
    #: Roles on the audit context of the agent's own writes.
    audit_roles: frozenset[str]
    #: In the order a run performs them.
    capabilities: tuple[TaskAgentCapability, ...]
    method: str = METHOD_DETERMINISTIC
    #: For an agent whose proposals are decided outside `GovernanceReview`: its
    #: waiting proposals there, summed into the backlog bound ...
    pending_counter: PendingCounter | None = None
    #: ... and how they have been decided, merged into its outcome measure.
    outcome_reader: OutcomeReader | None = None

    @property
    def principal_setting(self) -> str:
        return f"{self.key}{_PRINCIPAL_SETTING_SUFFIX}"

    def principal(self, settings: Settings) -> str:
        return str(getattr(settings, self.principal_setting)).strip()

    def max_proposals_per_run(self, settings: Settings) -> int:
        return int(getattr(settings, f"{self.key}_agent_max_proposals_per_run"))

    def max_pending_proposals(self, settings: Settings) -> int:
        return int(getattr(settings, f"{self.key}_agent_max_pending_proposals"))

    @property
    def capability_keys(self) -> tuple[str, ...]:
        return tuple(capability.key for capability in self.capabilities)

    def capability(self, key: str) -> TaskAgentCapability:
        for capability in self.capabilities:
            if capability.key == key:
                return capability
        raise KeyError(key)


def reserved_principals(settings: Settings, spec: TaskAgentSpec) -> frozenset[str]:
    """Every *other* agent's configured workload identity.

    Read from the settings model itself rather than from a list kept here, so an
    agent added later is reserved against every existing one the moment its
    principal setting exists -- including the reviewer agent's, which is what
    keeps a maker from sharing an identity with its checker.
    """
    return frozenset(
        str(getattr(settings, name)).strip()
        for name in type(settings).model_fields
        if name.endswith(_PRINCIPAL_SETTING_SUFFIX) and name != spec.principal_setting
    )


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskAgentAuthority:
    """The contract, asset and version an agent acts under.

    `principal_id` is captured at resolution rather than read off the contract
    later: the contract row is re-read before every item, and a principal
    renamed mid-run must refuse the run, not quietly re-attribute its remaining
    proposals.
    """

    contract: AgentContract
    asset: AiAsset
    version: AiAssetVersion
    principal_id: str


async def resolve_task_agent_authority(
    session: AsyncSession, organization_id: UUID, *, spec: TaskAgentSpec, settings: Settings
) -> TaskAgentAuthority:
    """The contract this agent acts under in this organization, or a refusal.

    Contracts on superseded, draft or retired versions are ignored -- an
    asset's next version can be contracted ahead of its approval without making
    the current one ambiguous -- but two approved candidates are refused,
    because picking one would let row order decide which authority an agent
    runs under.
    """
    principal = spec.principal(settings)
    if principal in reserved_principals(settings, spec):
        raise TaskAgentRefused(REASON_PRINCIPAL_RESERVED)
    rows = (
        await session.execute(
            select(AgentContract, AiAssetVersion, AiAsset)
            .join(AiAssetVersion, AiAssetVersion.id == AgentContract.ai_asset_version_id)
            .join(AiAsset, AiAsset.id == AiAssetVersion.asset_id)
            .where(
                AgentContract.organization_id == organization_id,
                AgentContract.agent_principal_id == principal,
                AiAssetVersion.organization_id == organization_id,
                AiAsset.organization_id == organization_id,
                AiAsset.asset_kind == "AGENT",
            )
            .limit(_CONTRACT_SCAN_LIMIT)
        )
    ).all()
    if not rows:
        raise TaskAgentRefused(REASON_CONTRACT_MISSING)
    approved = [
        (contract, version, asset)
        for contract, version, asset in rows
        if version.status == "APPROVED"
    ]
    if not approved:
        raise TaskAgentRefused(REASON_VERSION_NOT_APPROVED)
    if len(approved) > 1:
        raise TaskAgentRefused(REASON_CONTRACT_AMBIGUOUS)
    contract, version, asset = approved[0]
    return TaskAgentAuthority(
        contract=contract, asset=asset, version=version, principal_id=principal
    )


async def authority_withdrawn(
    session: AsyncSession, authority: TaskAgentAuthority, *, mode: Mode
) -> str | None:
    """Why the agent may no longer act, re-read now, or `None`.

    `agent_kill_blocking_reason` reads `kill_engaged` off the contract object it
    is handed, and within one session that object is the identity-map copy
    loaded when the run began. Without `populate_existing` a switch engaged by
    another transaction mid-run would be invisible to every check after the
    first -- the trap `reviewer_agent.organization_suspended` avoids the same
    way. The version's status is read as a column, which never comes from the
    identity map.
    """
    contract = await session.scalar(
        select(AgentContract)
        .where(AgentContract.id == authority.contract.id)
        .execution_options(populate_existing=True)
    )
    if contract is None or contract.agent_principal_id != authority.principal_id:
        return REASON_CONTRACT_MISSING
    version_status = await session.scalar(
        select(AiAssetVersion.status).where(AiAssetVersion.id == authority.version.id)
    )
    if version_status != "APPROVED":
        return REASON_VERSION_NOT_APPROVED
    blocking = await agent_kill_blocking_reason(session, contract)
    if blocking is not None:
        return blocking
    if mode == "PROPOSE" and mode_for(contract.autonomy_tier) != "PROPOSE":
        return REASON_AUTONOMY_WITHDRAWN
    return None


async def pending_proposal_count(
    session: AsyncSession, organization_id: UUID, *, agent_principal_id: str
) -> int:
    """The agent's own proposals still waiting for a decision."""
    count = await session.scalar(
        select(func.count())
        .select_from(GovernanceReview)
        .where(
            GovernanceReview.organization_id == organization_id,
            GovernanceReview.requested_by == agent_principal_id,
            GovernanceReview.status == "PENDING",
        )
    )
    return int(count or 0)


async def agent_pending_count(
    session: AsyncSession, organization_id: UUID, *, spec: TaskAgentSpec, agent_principal_id: str
) -> int:
    """Everything the agent has waiting for a person: its pending reviews, plus
    its proposals in any dedicated queue it writes to."""
    count = await pending_proposal_count(
        session, organization_id, agent_principal_id=agent_principal_id
    )
    if spec.pending_counter is not None:
        count += await spec.pending_counter(session, organization_id, agent_principal_id)
    return count


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskAgentRunRequest:
    #: `None` runs every capability the agent has, in its own order.
    capabilities: tuple[str, ...] | None = None
    #: Proposals per capability, clamped to `<key>_agent_max_proposals_per_run`.
    limit: int = 10
    datasource_id: UUID | None = None
    #: Observe under a proposing contract: report, open nothing.
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class TaskAgentItem:
    """One thing a run looked at, and what it did about it.

    `subject` is what the proposal is about (a table, a view); `related` is the
    other end when there is one (a glossary term, an upstream table).
    """

    capability: str
    subject_id: UUID
    subject_name: str
    action: str
    reason: str | None = None
    object_type: str | None = None
    object_id: UUID | None = None
    review_id: UUID | None = None
    task_id: UUID | None = None
    confidence: float | None = None
    rank: int | None = None
    related_id: UUID | None = None
    related_name: str | None = None


@dataclass(slots=True)
class TaskAgentOutcome:
    run_id: str
    agent_key: str
    organization_id: UUID
    agent_principal_id: str
    ai_asset_version_id: UUID
    autonomy_tier: str
    mode: Mode
    dry_run: bool
    limit: int
    capabilities: tuple[str, ...]
    started_at: datetime
    finished_at: datetime | None = None
    stopped_reason: str | None = None
    items: list[TaskAgentItem] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for item in self.items if item.action == action)

    def skipped_by_reason(self) -> dict[str, int]:
        reasons = Counter(
            item.reason or "unspecified" for item in self.items if item.action == ACTION_SKIPPED
        )
        return dict(sorted(reasons.items()))


def _agent_context(
    organization_id: UUID, principal_id: str, roles: frozenset[str]
) -> SecurityContext:
    """The agent's own identity on the audit rows its proposals write --
    `principal_type=AGENT`, as the reviewer agent's are (PG-2)."""
    return SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT",
        organization_id=organization_id,
        roles=roles,
    )


class TaskAgentRun:
    """One run's state and the only operations an agent may perform in it."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        spec: TaskAgentSpec,
        authority: TaskAgentAuthority,
        settings: Settings,
        outcome: TaskAgentOutcome,
        datasource_id: UUID | None,
        pending: int,
    ) -> None:
        self.session = session
        self.spec = spec
        self.authority = authority
        self.settings = settings
        self.outcome = outcome
        self.datasource_id = datasource_id
        self.organization_id = outcome.organization_id
        self.agent_context = _agent_context(
            outcome.organization_id, authority.principal_id, spec.audit_roles
        )
        self.pending = pending

    @property
    def principal_id(self) -> str:
        return self.authority.principal_id

    @property
    def proposing(self) -> bool:
        return self.outcome.mode == "PROPOSE"

    def add(self, item: TaskAgentItem) -> TaskAgentItem:
        self.outcome.items.append(item)
        return item

    def item(
        self,
        capability: str,
        *,
        action: str,
        subject_id: UUID,
        subject_name: str,
        reason: str | None = None,
        object_id: UUID | None = None,
        review_id: UUID | None = None,
        task_id: UUID | None = None,
        confidence: float | None = None,
        rank: int | None = None,
        related_id: UUID | None = None,
        related_name: str | None = None,
    ) -> TaskAgentItem:
        """An item, with the object type filled in for anything proposed."""
        proposing = action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE)
        return TaskAgentItem(
            capability=capability,
            subject_id=subject_id,
            subject_name=subject_name,
            action=action,
            reason=reason,
            object_type=self.spec.capability(capability).object_type if proposing else None,
            object_id=object_id,
            review_id=review_id,
            task_id=task_id,
            confidence=confidence,
            rank=rank,
            related_id=related_id,
            related_name=related_name,
        )

    async def may_continue(self) -> bool:
        """Before each item: authority first -- a withdrawal refuses the whole
        run -- then budgets, which end it and keep what it did."""
        withdrawn = await authority_withdrawn(self.session, self.authority, mode=self.outcome.mode)
        if withdrawn is not None:
            raise TaskAgentRefused(withdrawn)
        wall_clock = wall_clock_violation(
            self.authority.contract, started_at=self.outcome.started_at, now=datetime.now(UTC)
        )
        if wall_clock is not None:
            self.outcome.stopped_reason = wall_clock
            return False
        backlog_limit = self.spec.max_pending_proposals(self.settings)
        if self.proposing and backlog_limit and self.pending >= backlog_limit:
            self.outcome.stopped_reason = STOP_REVIEW_BACKLOG
            return False
        return True

    async def guarded(
        self,
        capability: str,
        *,
        subject_id: UUID,
        subject_name: str,
        work: Callable[[], Awaitable[TaskAgentItem]],
        rank: int | None = None,
        related_id: UUID | None = None,
        related_name: str | None = None,
    ) -> TaskAgentItem:
        """One item's work, in its own savepoint.

        A refusal propagates -- it ends the run and the run is rolled back.
        Anything else is one item's failure: its savepoint unwinds, it is
        recorded, and the run goes on. `subject_*`/`related_*` are passed in
        already evaluated because a rolled-back savepoint can expire ORM state,
        and an expired attribute cannot be lazy-loaded outside an await.
        """
        try:
            async with self.session.begin_nested():
                return await work()
        except TaskAgentRefused:
            raise
        except Exception as exc:  # noqa: BLE001 -- one item's failure must not end the run
            return await self.failed(
                capability,
                subject_id=subject_id,
                subject_name=subject_name,
                exc=exc,
                rank=rank,
                related_id=related_id,
                related_name=related_name,
            )

    async def open_review(
        self,
        capability: str,
        *,
        object_id: UUID,
        requested_action: str,
        details: dict[str, Any],
    ) -> GovernanceReview:
        """Put one proposal in the review queue as the agent's own request.

        The tier check is structural, not configuration: an agent may only ever
        ask for decisions an agent could in principle be trusted near
        (ADR-0027's T0/T1).
        """
        spec_capability = self.spec.capability(capability)
        if spec_capability.queue != QUEUE_GOVERNANCE_REVIEW:
            raise ValueError(f"{capability} is decided in {spec_capability.queue}, not here")
        object_type = spec_capability.object_type
        tier = risk_tier_for(object_type)
        if not tier_at_or_below(tier, HARD_MAX_AGENT_TIER):
            raise TaskAgentRefused(REASON_OBJECT_TYPE_ABOVE_CEILING)
        review = GovernanceReview(
            organization_id=self.organization_id,
            object_type=object_type,
            object_id=str(object_id),
            requested_action=requested_action,
            requested_by=self.principal_id,
        )
        self.session.add(review)
        await self.session.flush()
        record_audit(
            self.session,
            self.agent_context,
            action=f"{self.spec.key}_agent.propose",
            resource_type="governance_review",
            resource_id=str(review.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={
                "object_type": object_type,
                "object_id": str(object_id),
                "risk_tier": tier,
                "run_id": self.outcome.run_id,
                **details,
            },
        )
        record_outbox(
            self.session,
            organization_id=self.organization_id,
            aggregate_type="governance_review",
            aggregate_id=str(review.id),
            event_type="governance.review_requested.v1",
            payload={
                "review_id": str(review.id),
                "object_type": object_type,
                "object_id": str(object_id),
                "requested_action": requested_action,
            },
        )
        return review

    async def proposed(
        self,
        capability: str,
        *,
        review: GovernanceReview,
        object_id: UUID,
        subject_id: UUID,
        subject_name: str,
        inputs: dict[str, Any],
        confidence: float | None = None,
        rank: int | None = None,
        related_id: UUID | None = None,
        related_name: str | None = None,
    ) -> TaskAgentItem:
        """Ledger one opened proposal and report it.

        The task stays `PROPOSED`: the decision lives on the review, and
        `task_agent_outcomes` reads it from there rather than copying it.
        `inputs` must be value-free -- ids, hashes, names of things, never text
        the agent drafted (INV-6).
        """
        spec_capability = self.spec.capability(capability)
        task = await record_agent_task(
            self.session,
            organization_id=self.organization_id,
            agent_principal_id=self.principal_id,
            intent=spec_capability.intent,
            inputs=inputs,
            ai_asset_version_id=self.authority.version.id,
            proposal_ref_type="GOVERNANCE_REVIEW",
            proposal_ref_id=review.id,
            sampling_rate=self.authority.contract.sampling_rate,
        )
        evidence: dict[str, Any] = {
            "run_id": self.outcome.run_id,
            "review_id": str(review.id),
            "object_type": spec_capability.object_type,
            "object_id": str(object_id),
        }
        if confidence is not None:
            evidence["confidence"] = confidence
        if rank is not None:
            evidence["rank"] = rank
        task.evidence = evidence
        self.pending += 1
        return self.item(
            capability,
            action=ACTION_PROPOSED,
            subject_id=subject_id,
            subject_name=subject_name,
            object_id=object_id,
            review_id=review.id,
            task_id=task.id,
            confidence=confidence,
            rank=rank,
            related_id=related_id,
            related_name=related_name,
        )

    async def proposed_in_queue(
        self,
        capability: str,
        *,
        proposal_ref_type: str,
        proposal_ref_id: UUID,
        subject_id: UUID,
        subject_name: str,
        inputs: dict[str, Any],
        pending_added: int,
        evidence: dict[str, Any],
        confidence: float | None = None,
        rank: int | None = None,
    ) -> TaskAgentItem:
        """Ledger a proposal whose rows the agent wrote into a dedicated queue.

        The second write path, and deliberately narrow: only a queue on
        `_HUMAN_ONLY_QUEUES` is accepted, because the maker-checker guarantee
        there is the queue's own. The agent module wrote the rows; this records
        what it wrote -- ids and counts in `evidence`, never content -- audits it
        as the agent, and counts `pending_added` against the backlog bound.
        """
        spec_capability = self.spec.capability(capability)
        if spec_capability.queue not in _HUMAN_ONLY_QUEUES:
            raise TaskAgentRefused(REASON_QUEUE_NOT_PERMITTED)
        task = await record_agent_task(
            self.session,
            organization_id=self.organization_id,
            agent_principal_id=self.principal_id,
            intent=spec_capability.intent,
            inputs=inputs,
            ai_asset_version_id=self.authority.version.id,
            proposal_ref_type=proposal_ref_type,
            proposal_ref_id=proposal_ref_id,
            sampling_rate=self.authority.contract.sampling_rate,
        )
        task_evidence: dict[str, Any] = {
            "run_id": self.outcome.run_id,
            "queue": spec_capability.queue,
            "object_type": spec_capability.object_type,
            **evidence,
        }
        if confidence is not None:
            task_evidence["confidence"] = confidence
        task.evidence = task_evidence
        record_audit(
            self.session,
            self.agent_context,
            action=f"{self.spec.key}_agent.propose",
            resource_type=proposal_ref_type.lower(),
            resource_id=str(proposal_ref_id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={
                "queue": spec_capability.queue,
                "object_type": spec_capability.object_type,
                "proposed_count": pending_added,
                "run_id": self.outcome.run_id,
            },
        )
        self.pending += pending_added
        return self.item(
            capability,
            action=ACTION_PROPOSED,
            subject_id=subject_id,
            subject_name=subject_name,
            object_id=proposal_ref_id,
            task_id=task.id,
            confidence=confidence,
            rank=rank,
        )

    async def declined(
        self,
        capability: str,
        *,
        subject_id: UUID,
        subject_name: str,
        reason: str,
        inputs: dict[str, Any],
        proposal_ref_type: str | None = None,
        proposal_ref_id: UUID | None = None,
        rank: int | None = None,
    ) -> TaskAgentItem:
        """Something the agent examined and deliberately did not propose.

        A proposing run ledgers it, so an agent can recognise work it has already
        looked at -- by `inputs`, or by `proposal_ref_*` -- and not spend every
        run re-examining the same dead end. The ledger's closed status set has no
        "declined", so it is recorded as FAILED with the reason in `evidence`:
        the unit of work could not produce a proposal. An observing run writes
        nothing.
        """
        task_id: UUID | None = None
        if self.proposing:
            task = await record_agent_task(
                self.session,
                organization_id=self.organization_id,
                agent_principal_id=self.principal_id,
                intent=self.spec.capability(capability).intent,
                inputs=inputs,
                ai_asset_version_id=self.authority.version.id,
                proposal_ref_type=proposal_ref_type,
                proposal_ref_id=proposal_ref_id,
                sampling_rate=self.authority.contract.sampling_rate,
            )
            finish_agent_task(
                task, status="FAILED", evidence={"run_id": self.outcome.run_id, "declined": reason}
            )
            task_id = task.id
        return self.item(
            capability,
            action=ACTION_SKIPPED,
            subject_id=subject_id,
            subject_name=subject_name,
            reason=reason,
            task_id=task_id,
            rank=rank,
        )

    async def failed(
        self,
        capability: str,
        *,
        subject_id: UUID,
        subject_name: str,
        exc: Exception,
        rank: int | None = None,
        related_id: UUID | None = None,
        related_name: str | None = None,
    ) -> TaskAgentItem:
        """One item's failure, after its savepoint has unwound.

        Only the exception's class is kept -- never its message, which can carry
        a value (the same INV-6 rule the ingest drafter's handler follows). A
        proposing run records the failure in the ledger; an observing run writes
        nothing, failures included.
        """
        error_class = type(exc).__name__
        task_id: UUID | None = None
        if self.proposing:
            task = await record_agent_task(
                self.session,
                organization_id=self.organization_id,
                agent_principal_id=self.principal_id,
                intent=self.spec.capability(capability).intent,
                inputs={
                    "capability": capability,
                    "subject_id": str(subject_id),
                    "related_id": str(related_id) if related_id else None,
                    "run_id": self.outcome.run_id,
                },
                ai_asset_version_id=self.authority.version.id,
                sampling_rate=self.authority.contract.sampling_rate,
            )
            finish_agent_task(
                task,
                status="FAILED",
                evidence={"run_id": self.outcome.run_id, "error_class": error_class},
            )
            task_id = task.id
        return self.item(
            capability,
            action=ACTION_FAILED,
            subject_id=subject_id,
            subject_name=subject_name,
            reason=f"error:{error_class}",
            task_id=task_id,
            rank=rank,
            related_id=related_id,
            related_name=related_name,
        )


#: One capability's work: find items, and hand each to `TaskAgentRun.guarded`.
CapabilityWork = Callable[[TaskAgentRun], Awaitable[None]]


async def run_task_agent(
    session: AsyncSession,
    organization_id: UUID,
    *,
    spec: TaskAgentSpec,
    work: Mapping[str, CapabilityWork],
    request: TaskAgentRunRequest,
    settings: Settings,
    triggered_by: SecurityContext,
) -> TaskAgentOutcome:
    """One bounded run. Commits nothing: the caller owns the transaction,
    commits a returned outcome, and rolls back on `TaskAgentRefused`."""
    requested = request.capabilities if request.capabilities is not None else spec.capability_keys
    if not requested or set(requested) - set(spec.capability_keys):
        raise ValueError(f"capabilities must be a non-empty subset of {spec.capability_keys}")
    authority = await resolve_task_agent_authority(
        session, organization_id, spec=spec, settings=settings
    )
    blocking = await agent_kill_blocking_reason(session, authority.contract)
    if blocking is not None:
        raise TaskAgentRefused(blocking)

    outcome = TaskAgentOutcome(
        run_id=str(uuid4()),
        agent_key=spec.key,
        organization_id=organization_id,
        agent_principal_id=authority.principal_id,
        ai_asset_version_id=authority.version.id,
        autonomy_tier=authority.contract.autonomy_tier,
        mode="OBSERVE" if request.dry_run else mode_for(authority.contract.autonomy_tier),
        dry_run=request.dry_run,
        limit=max(1, min(request.limit, spec.max_proposals_per_run(settings))),
        capabilities=tuple(key for key in spec.capability_keys if key in requested),
        started_at=datetime.now(UTC),
    )
    run = TaskAgentRun(
        session,
        spec=spec,
        authority=authority,
        settings=settings,
        outcome=outcome,
        datasource_id=request.datasource_id,
        pending=await agent_pending_count(
            session, organization_id, spec=spec, agent_principal_id=authority.principal_id
        ),
    )
    for key in outcome.capabilities:
        if outcome.stopped_reason is not None:
            break
        await work[key](run)
    outcome.finished_at = datetime.now(UTC)

    # The run itself is attributed to whoever started it; each proposal's own
    # audit row (`<key>_agent.propose`) is attributed to the agent.
    record_audit(
        session,
        replace(triggered_by, organization_id=organization_id),
        action=f"{spec.key}_agent.run",
        resource_type="agent_contract",
        resource_id=str(authority.version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "run_id": outcome.run_id,
            "agent_principal_id": outcome.agent_principal_id,
            "autonomy_tier": outcome.autonomy_tier,
            "mode": outcome.mode,
            "dry_run": outcome.dry_run,
            "capabilities": list(outcome.capabilities),
            "limit": outcome.limit,
            "datasource_id": str(request.datasource_id) if request.datasource_id else None,
            "proposed": outcome.count(ACTION_PROPOSED),
            "would_propose": outcome.count(ACTION_WOULD_PROPOSE),
            "failed": outcome.count(ACTION_FAILED),
            "skipped": outcome.skipped_by_reason(),
            "stopped_reason": outcome.stopped_reason,
        },
    )
    return outcome


def record_task_agent_refusal(
    session: AsyncSession,
    organization_id: UUID,
    *,
    spec: TaskAgentSpec,
    triggered_by: SecurityContext,
    reason_code: str,
) -> None:
    """The DENIED audit row for a refused run. Written by the caller in a fresh
    transaction, after the refused run's own writes were rolled back."""
    record_audit(
        session,
        replace(triggered_by, organization_id=organization_id),
        action=f"{spec.key}_agent.run",
        resource_type="agent_contract",
        resource_id=None,
        outcome="DENIED",
        correlation_id=get_correlation_id(),
        details={"reason": reason_code},
    )


# ---------------------------------------------------------------------------
# What an agent is here, and how its proposals have fared
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskAgentOutcomeRow:
    """How one kind of an agent's proposals has been decided."""

    object_type: str
    pending: int
    approved: int
    rejected: int
    other: int

    @property
    def acceptance_rate(self) -> float | None:
        """Approved over decided. `None`, never 0, when nothing is decided --
        a rate over an empty set is not a measurement."""
        decided = self.approved + self.rejected
        return round(self.approved / decided, 4) if decided else None


async def task_agent_outcomes(
    session: AsyncSession, organization_id: UUID, *, agent_principal_id: str
) -> list[TaskAgentOutcomeRow]:
    """Every review the agent requested, by object type and status.

    Read from the reviews rather than the task ledger: the review is where the
    decision is recorded, and the agent's identity on `requested_by` is the
    attribution. This is the outcome measure the 2026-09-09 architecture review
    asks for in place of counting agents -- how often an agent's work survives
    an independent reviewer.
    """
    rows = (
        await session.execute(
            select(GovernanceReview.object_type, GovernanceReview.status, func.count())
            .where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.requested_by == agent_principal_id,
            )
            .group_by(GovernanceReview.object_type, GovernanceReview.status)
        )
    ).all()
    counts: dict[str, Counter[str]] = {}
    for object_type, status, count in rows:
        counts.setdefault(str(object_type), Counter())[str(status)] += int(count)
    result: list[TaskAgentOutcomeRow] = []
    for object_type in sorted(counts):
        by_status = counts[object_type]
        pending = by_status.pop("PENDING", 0)
        approved = by_status.pop("APPROVED", 0)
        rejected = by_status.pop("REJECTED", 0)
        result.append(
            TaskAgentOutcomeRow(
                object_type=object_type,
                pending=pending,
                approved=approved,
                rejected=rejected,
                other=sum(by_status.values()),
            )
        )
    return result


async def agent_outcomes(
    session: AsyncSession, organization_id: UUID, *, spec: TaskAgentSpec, agent_principal_id: str
) -> list[TaskAgentOutcomeRow]:
    """Its reviews' outcomes, plus those of any dedicated queue it writes to."""
    rows = await task_agent_outcomes(
        session, organization_id, agent_principal_id=agent_principal_id
    )
    if spec.outcome_reader is not None:
        rows.extend(await spec.outcome_reader(session, organization_id, agent_principal_id))
    return sorted(rows, key=lambda row: row.object_type)


@dataclass(frozen=True, slots=True)
class TaskAgentStatus:
    agent_principal_id: str
    authority: TaskAgentAuthority | None
    refusal_reason: str | None
    blocking_reason: str | None
    pending_proposals: int
    outcomes: list[TaskAgentOutcomeRow]


async def task_agent_status(
    session: AsyncSession, organization_id: UUID, *, spec: TaskAgentSpec, settings: Settings
) -> TaskAgentStatus:
    """Whether the agent could run here now, and if not, why not.

    A status read, not a run: it resolves authority and the kill switch the
    same way a run would, and reports the refusal a run would get instead of
    raising it.
    """
    principal = spec.principal(settings)
    authority: TaskAgentAuthority | None = None
    refusal: str | None = None
    try:
        authority = await resolve_task_agent_authority(
            session, organization_id, spec=spec, settings=settings
        )
    except TaskAgentRefused as exc:
        refusal = exc.reason_code
    blocking = (
        await agent_kill_blocking_reason(session, authority.contract)
        if authority is not None
        else None
    )
    return TaskAgentStatus(
        agent_principal_id=principal,
        authority=authority,
        refusal_reason=refusal,
        blocking_reason=blocking,
        pending_proposals=await agent_pending_count(
            session, organization_id, spec=spec, agent_principal_id=principal
        ),
        outcomes=await agent_outcomes(
            session, organization_id, spec=spec, agent_principal_id=principal
        ),
    )
