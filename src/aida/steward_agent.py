"""ADR-0029: the steward agent.

A workload identity (`agent:steward` by default) that works the stewardship
backlog under its own `AgentContract`: it drafts table descriptions and
glossary links and submits each one to the shared `GovernanceReview` queue,
where a human -- or, for these T0/T1 types, the ADR-0027 reviewer agent --
decides it. `Docs/10-architecture/adr/ADR-0029-steward-agent.md` records why it
exists; this module is the mechanism.

One run, in order:

1. **Resolve authority.** The agent acts only as
   `Settings.steward_agent_principal_id`, only under that principal's contract,
   and only when the contract hangs off an APPROVED `AGENT`-kind AI asset
   version in this organization. No contract, no approved version, two
   approved candidates, or a principal equal to the reviewer agent's is a
   refusal -- never an unconstrained run (AG-10's fail-closed rule).
2. **Check the kill switch** -- the agent's own, its tier's, the
   organization's, and the organization-wide model switch
   (`agent_contracts.agent_kill_blocking_reason`) -- before the run and again
   before every item, re-read from the database each time.
3. **Read the autonomy tier as a ceiling.** T0 observes: the run reports what
   it would propose and opens no proposal. T1 proposes. T2 and T3 propose
   exactly what T1 does, because this agent has no branch that applies its own
   output; the tier can narrow it and cannot widen it. This is the first
   runtime consumer of `AgentContract.autonomy_tier`.
4. **Select from the steward's own queue.** Descriptions come from the AT-5
   documentation worklist in its default priority order, so the agent works the
   backlog in the order a human steward is shown it rather than in one of its
   own. Links come from GL-8's label matcher. Both are bounded per run.
5. **Propose through the one review queue**, as its own principal, so
   maker != checker needs no special case: any human reviewer may decide the
   proposal -- the steward who supervises the agent included -- and the agent
   may decide none.
6. **Leave a ledger.** One `AgentTask` per proposal, linked to its review. The
   task stays `PROPOSED`: the decision lives on the review, and
   `steward_agent_outcomes` reads it from there rather than copying it.

**Two ways a run ends early, deliberately different.** Authority withdrawn
mid-run -- a kill switch engaged, the tier lowered to T0, the contract or its
approved version gone -- raises `StewardAgentRefused`, and the API rolls the
whole run back: nothing produced by a run whose licence was withdrawn
survives it, the same way a mid-batch suspension discards the reviewer agent's
batch. A *budget* reached mid-run -- the contract's wall-clock cap, the
proposal limit, the review-backlog bound -- ends the run and keeps what it did,
reported as `stopped_reason`.

**Method.** Nothing here calls a model. Descriptions are GL-9's
evidence-composed drafts and links are GL-8's exact label matches:
deterministic functions of catalog rows, reported as `METHOD` wherever this
agent is described. The contract's token caps therefore have nothing to bound;
its wall-clock cap does.

**Never.** It never decides a review, publishes, edits someone else's draft,
stacks a second draft on a table that already has one open, or re-proposes text
or a link a human already rejected.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_budget import wall_clock_violation
from aida.agent_contracts import REASON_CONTRACT_MISSING, agent_kill_blocking_reason
from aida.agent_tasks import finish_agent_task, record_agent_task
from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    compose_draft_text,
    evidence_payload,
    gather_evidence,
    score_evidence,
    text_fingerprint,
)
from aida.context import get_correlation_id
from aida.documentation_worklist import rank_documentation_worklist
from aida.documentation_worklist_signals import gather_documentation_worklist_signals
from aida.events import record_audit, record_outbox
from aida.glossary_link_candidates import (
    GlossaryLinkCandidate,
    build_glossary_link_proposal,
    find_glossary_link_candidates,
)
from aida.models import (
    AgentContract,
    AgentTask,
    AiAsset,
    AiAssetVersion,
    AssetDescriptionDraft,
    GovernanceReview,
    MetadataTable,
)
from aida.review_risk_tiers import HARD_MAX_AGENT_TIER, risk_tier_for, tier_at_or_below
from aida.security import SecurityContext
from atlas.platform.config import Settings

Mode = Literal["OBSERVE", "PROPOSE"]

CAPABILITY_TABLE_DESCRIPTION: Final = "TABLE_DESCRIPTION"
CAPABILITY_GLOSSARY_LINK: Final = "GLOSSARY_LINK"
#: In the order a run performs them.
CAPABILITIES: Final[tuple[str, ...]] = (CAPABILITY_TABLE_DESCRIPTION, CAPABILITY_GLOSSARY_LINK)

#: What each capability's proposals are reviewed as. Every one must sit at or
#: below `review_risk_tiers.HARD_MAX_AGENT_TIER`, and `_Run.open_review` refuses
#: anything else -- so a future capability that proposed published meaning or a
#: trust-boundary change would fail at its first proposal, not ship.
PROPOSAL_OBJECT_TYPES: Final[dict[str, str]] = {
    CAPABILITY_TABLE_DESCRIPTION: "ASSET_DESCRIPTION_DRAFT",
    CAPABILITY_GLOSSARY_LINK: "GLOSSARY_LINK_PROPOSAL",
}

#: Where each capability's output comes from, reported verbatim by the state
#: endpoint so "how does this agent write" is answerable without reading code.
CAPABILITY_PRODUCERS: Final[dict[str, str]] = {
    CAPABILITY_TABLE_DESCRIPTION: (
        "asset_description_service: GL-9 draft composed from catalog evidence"
    ),
    CAPABILITY_GLOSSARY_LINK: "glossary_link_candidates: GL-8 approved-label exact match",
}

_INTENTS: Final[dict[str, str]] = {
    CAPABILITY_TABLE_DESCRIPTION: "steward.propose_table_description",
    CAPABILITY_GLOSSARY_LINK: "steward.propose_glossary_link",
}

#: How this agent reaches its output, on every surface that reports it. There
#: is no model route to name because nothing here calls one.
METHOD: Final = "DETERMINISTIC"

ACTION_PROPOSED: Final = "PROPOSED"
ACTION_WOULD_PROPOSE: Final = "WOULD_PROPOSE"
ACTION_SKIPPED: Final = "SKIPPED"
ACTION_FAILED: Final = "FAILED"

# Refusals: the agent may not act, and a refused run keeps nothing.
REASON_CONTRACT_AMBIGUOUS: Final = "agent_contract_unresolved"
REASON_VERSION_NOT_APPROVED: Final = "steward_agent_version_not_approved"
REASON_PRINCIPAL_IS_REVIEWER: Final = "steward_agent_principal_is_reviewer"
REASON_AUTONOMY_WITHDRAWN: Final = "steward_agent_autonomy_withdrawn"
REASON_OBJECT_TYPE_ABOVE_CEILING: Final = "steward_agent_object_type_above_ceiling"
# Stops: a budget ran out, and the run keeps what it already did. The contract's
# wall-clock cap reports `agent_budget.REASON_WALL_CLOCK_CAP`.
STOP_REVIEW_BACKLOG: Final = "steward_agent_review_backlog_full"
# Skips: one table or link the agent looked at and deliberately left alone.
SKIP_OPEN_DRAFT: Final = "open_draft_exists"
SKIP_REJECTED_BEFORE: Final = "identical_text_rejected"
SKIP_BELOW_EVIDENCE_BAR: Final = "below_evidence_bar"

#: Tables examined per run, as a multiple of the proposal limit. Bounds the work
#: of a run whose worklist is mostly already in review: with a limit of 10 it
#: reads at most 40 tables' evidence, however long the backlog is.
_EXAMINE_FACTOR: Final = 4

#: The same default a steward's own request uses
#: (`schemas.GlossaryLinkProposalGenerate.minimum_confidence`). Every GL-8 match
#: scores 1.0 or 0.92, so this admits every exact label match and nothing else.
_LINK_MINIMUM_CONFIDENCE: Final = 0.75

#: A table with a draft in either state is already somebody's work in progress.
#: The same pair `asset_description_api.generate_asset_description_drafts` uses.
_OPEN_DRAFT_STATUSES: Final = ("DRAFT", "PENDING_APPROVAL")

#: Contracts read when resolving authority. More than one APPROVED candidate is
#: refused as ambiguous; this only bounds the read.
_CONTRACT_SCAN_LIMIT: Final = 20


class StewardAgentRefused(RuntimeError):
    """The agent may not act, or its authority was withdrawn mid-run.

    Raised before the first write, or -- for a withdrawal noticed mid-run --
    after some. The API rolls the transaction back in both cases, so a refused
    run leaves no proposal and no task behind. `reason_code` is stable and
    operator-facing.
    """

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def mode_for(autonomy_tier: str) -> Mode:
    """T0 observes; T1 and above propose, and propose identically.

    The ceiling is the point. This agent has no branch that applies its own
    output, so a T2 or T3 contract buys it nothing a T1 contract does not. An
    unrecognised tier observes: a tier the platform cannot read is not a
    licence to write (INV-4).
    """
    return "PROPOSE" if autonomy_tier in ("T1", "T2", "T3") else "OBSERVE"


def _agent_context(organization_id: UUID, principal_id: str) -> SecurityContext:
    """The agent's own identity on the audit rows its proposals write --
    `principal_type=AGENT`, as the reviewer agent's are (PG-2)."""
    return SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StewardAuthority:
    """The contract, asset and version the agent acts under.

    `principal_id` is captured at resolution rather than read off the contract
    later: the contract row is re-read before every item, and a principal
    renamed mid-run must refuse the run, not quietly re-attribute its
    remaining proposals.
    """

    contract: AgentContract
    asset: AiAsset
    version: AiAssetVersion
    principal_id: str


async def resolve_steward_authority(
    session: AsyncSession, organization_id: UUID, *, settings: Settings
) -> StewardAuthority:
    """The contract this agent acts under in this organization, or a refusal.

    Exactly one `AgentContract` naming the principal must hang off an APPROVED
    version of an `AGENT`-kind asset here. Contracts on superseded, draft or
    retired versions are ignored -- an asset's next version can be contracted
    ahead of its approval without making the current one ambiguous -- but two
    approved candidates are refused, because picking one would let row order
    decide which authority an agent runs under.
    """
    principal = settings.steward_agent_principal_id.strip()
    if principal == settings.reviewer_agent_principal_id.strip():
        # The agent that drafts would be the agent that checks. The reviewer
        # agent would skip every one of these proposals as self-proposed, which
        # is safe -- but a configuration that collapses maker and checker into
        # one identity is refused loudly rather than left to be discovered.
        raise StewardAgentRefused(REASON_PRINCIPAL_IS_REVIEWER)
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
        raise StewardAgentRefused(REASON_CONTRACT_MISSING)
    approved = [
        (contract, version, asset)
        for contract, version, asset in rows
        if version.status == "APPROVED"
    ]
    if not approved:
        raise StewardAgentRefused(REASON_VERSION_NOT_APPROVED)
    if len(approved) > 1:
        raise StewardAgentRefused(REASON_CONTRACT_AMBIGUOUS)
    contract, version, asset = approved[0]
    return StewardAuthority(contract=contract, asset=asset, version=version, principal_id=principal)


async def _authority_withdrawn(
    session: AsyncSession, authority: StewardAuthority, *, mode: Mode
) -> str | None:
    """Why the agent may no longer act, re-read now, or `None`.

    `agent_kill_blocking_reason` reads `kill_engaged` off the contract object it
    is handed, and within one session that object is the identity-map copy
    loaded when the run began. Without `populate_existing` a switch engaged by
    another transaction mid-run would be invisible to every check after the
    first -- the same trap `reviewer_agent.organization_suspended` avoids the
    same way. The version's status is read as a column, which never comes from
    the identity map.
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


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StewardRunRequest:
    capabilities: tuple[str, ...] = CAPABILITIES
    #: Proposals per capability. Clamped to
    #: `Settings.steward_agent_max_proposals_per_run`.
    limit: int = 10
    datasource_id: UUID | None = None
    #: Observe under a proposing contract: report, write no proposal.
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class StewardRunItem:
    """One table or link the run looked at, and what it did about it."""

    capability: str
    table_id: UUID
    table_name: str
    action: str
    reason: str | None = None
    object_type: str | None = None
    object_id: UUID | None = None
    review_id: UUID | None = None
    task_id: UUID | None = None
    confidence: float | None = None
    worklist_rank: int | None = None
    term_id: UUID | None = None
    term_name: str | None = None


@dataclass(slots=True)
class StewardRunOutcome:
    run_id: str
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
    items: list[StewardRunItem] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for item in self.items if item.action == action)

    def skipped_by_reason(self) -> dict[str, int]:
        reasons = Counter(
            item.reason or "unspecified" for item in self.items if item.action == ACTION_SKIPPED
        )
        return dict(sorted(reasons.items()))


class _Run:
    """One run's mutable state. `run_steward_agent` is the interface."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        authority: StewardAuthority,
        settings: Settings,
        outcome: StewardRunOutcome,
        pending: int,
    ) -> None:
        self.session = session
        self.authority = authority
        self.settings = settings
        self.outcome = outcome
        self.organization_id = outcome.organization_id
        self.agent_context = _agent_context(outcome.organization_id, authority.principal_id)
        self.pending = pending

    async def may_continue(self) -> bool:
        """Before each item: authority first -- a withdrawal refuses the whole
        run -- then budgets, which end it and keep what it did."""
        withdrawn = await _authority_withdrawn(
            self.session, self.authority, mode=self.outcome.mode
        )
        if withdrawn is not None:
            raise StewardAgentRefused(withdrawn)
        wall_clock = wall_clock_violation(
            self.authority.contract, started_at=self.outcome.started_at, now=datetime.now(UTC)
        )
        if wall_clock is not None:
            self.outcome.stopped_reason = wall_clock
            return False
        backlog_limit = self.settings.steward_agent_max_pending_proposals
        if self.outcome.mode == "PROPOSE" and backlog_limit and self.pending >= backlog_limit:
            self.outcome.stopped_reason = STOP_REVIEW_BACKLOG
            return False
        return True

    # --- table descriptions -------------------------------------------------

    async def table_descriptions(self, *, datasource_id: UUID | None) -> None:
        signals = await gather_documentation_worklist_signals(
            self.session,
            organization_id=self.organization_id,
            scan_limit=self.settings.agent_retrieval_scan_limit,
            include_zero_volume=True,
        )
        # The steward's own order: `priority` ranking, real usage first and
        # zero-volume tables after it, documented tables already excluded.
        entries, _total = rank_documentation_worklist(
            signals, limit=len(signals), include_zero_volume=True
        )
        if not entries:
            return
        table_filters: list[Any] = [
            MetadataTable.organization_id == self.organization_id,
            MetadataTable.id.in_([entry.table_id for entry in entries]),
            MetadataTable.status == "ACTIVE",
        ]
        if datasource_id is not None:
            table_filters.append(MetadataTable.datasource_id == datasource_id)
        tables = {
            table.id: table
            for table in (
                await self.session.scalars(select(MetadataTable).where(*table_filters))
            ).all()
        }
        open_drafts = set(
            (
                await self.session.scalars(
                    select(AssetDescriptionDraft.table_id).where(
                        AssetDescriptionDraft.organization_id == self.organization_id,
                        AssetDescriptionDraft.table_id.in_(list(tables)),
                        AssetDescriptionDraft.status.in_(_OPEN_DRAFT_STATUSES),
                    )
                )
            ).all()
        )
        proposed = 0
        examined = 0
        examine_budget = self.outcome.limit * _EXAMINE_FACTOR
        for entry in entries:
            table = tables.get(entry.table_id)
            if table is None:
                continue
            if proposed >= self.outcome.limit or examined >= examine_budget:
                return
            examined += 1
            if table.id in open_drafts:
                self.outcome.items.append(
                    StewardRunItem(
                        capability=CAPABILITY_TABLE_DESCRIPTION,
                        table_id=table.id,
                        table_name=table.name,
                        action=ACTION_SKIPPED,
                        reason=SKIP_OPEN_DRAFT,
                        worklist_rank=entry.rank,
                    )
                )
                continue
            if not await self.may_continue():
                return
            item = await self._table_description(table, worklist_rank=entry.rank)
            self.outcome.items.append(item)
            if item.action in (ACTION_PROPOSED, ACTION_WOULD_PROPOSE):
                proposed += 1

    async def _table_description(
        self, table: MetadataTable, *, worklist_rank: int
    ) -> StewardRunItem:
        capability = CAPABILITY_TABLE_DESCRIPTION
        object_type = PROPOSAL_OBJECT_TYPES[capability]
        # Captured up front: a rolled-back savepoint can expire ORM state, and an
        # expired attribute cannot be lazy-loaded outside an await.
        table_id, table_name = table.id, table.name
        try:
            async with self.session.begin_nested():
                evidence = await gather_evidence(self.session, table)
                drafted_text = compose_draft_text(evidence)
                fingerprint = text_fingerprint(drafted_text)
                # Negative knowledge: these words, for this table, were already
                # put in front of a human, who said no.
                rejected_before = await self.session.scalar(
                    select(AssetDescriptionDraft.id)
                    .where(
                        AssetDescriptionDraft.table_id == table_id,
                        AssetDescriptionDraft.status == "REJECTED",
                        AssetDescriptionDraft.text_fingerprint == fingerprint,
                    )
                    .limit(1)
                )
                if rejected_before is not None:
                    return StewardRunItem(
                        capability=capability,
                        table_id=table_id,
                        table_name=table_name,
                        action=ACTION_SKIPPED,
                        reason=SKIP_REJECTED_BEFORE,
                        worklist_rank=worklist_rank,
                    )
                scores = score_evidence(evidence)
                # GL-9's own submission bar (`ensure_reviewable`). A draft under
                # it could never be submitted, so the agent does not create one.
                if scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
                    return StewardRunItem(
                        capability=capability,
                        table_id=table_id,
                        table_name=table_name,
                        action=ACTION_SKIPPED,
                        reason=SKIP_BELOW_EVIDENCE_BAR,
                        confidence=scores.overall,
                        worklist_rank=worklist_rank,
                    )
                if self.outcome.mode == "OBSERVE":
                    return StewardRunItem(
                        capability=capability,
                        table_id=table_id,
                        table_name=table_name,
                        action=ACTION_WOULD_PROPOSE,
                        object_type=object_type,
                        confidence=scores.overall,
                        worklist_rank=worklist_rank,
                    )
                draft = AssetDescriptionDraft(
                    organization_id=self.organization_id,
                    table_id=table_id,
                    drafted_text=drafted_text,
                    text_fingerprint=fingerprint,
                    accuracy_score=scores.accuracy,
                    clarity_score=scores.clarity,
                    style_score=scores.style,
                    completeness_score=scores.completeness,
                    overall_score=scores.overall,
                    evidence={
                        **evidence_payload(evidence),
                        "origin": "METADATA",
                        "worklist_rank": worklist_rank,
                        "steward_agent_run": self.outcome.run_id,
                    },
                    # Submitted as it is created: the agent's draft *is* its
                    # request for review, the transition a steward makes with
                    # `submit_asset_description_draft`.
                    status="PENDING_APPROVAL",
                    created_by=self.authority.principal_id,
                )
                self.session.add(draft)
                await self.session.flush()
                review = await self.open_review(
                    object_type=object_type,
                    object_id=draft.id,
                    requested_action="PUBLISH",
                    details={
                        "table_id": str(table_id),
                        "overall_score": scores.overall,
                        "worklist_rank": worklist_rank,
                    },
                )
                draft.governance_review_id = review.id
                task = await self.record_task(
                    capability,
                    review=review,
                    inputs={
                        "capability": capability,
                        "table_id": str(table_id),
                        "text_fingerprint": fingerprint,
                    },
                    evidence={
                        "object_type": object_type,
                        "object_id": str(draft.id),
                        "confidence": scores.overall,
                        "worklist_rank": worklist_rank,
                    },
                )
                self.pending += 1
                return StewardRunItem(
                    capability=capability,
                    table_id=table_id,
                    table_name=table_name,
                    action=ACTION_PROPOSED,
                    object_type=object_type,
                    object_id=draft.id,
                    review_id=review.id,
                    task_id=task.id,
                    confidence=scores.overall,
                    worklist_rank=worklist_rank,
                )
        except StewardAgentRefused:
            raise
        except Exception as exc:  # noqa: BLE001 -- one table's failure must not end the run
            return await self.failed(
                capability,
                table_id=table_id,
                table_name=table_name,
                exc=exc,
                worklist_rank=worklist_rank,
            )

    # --- glossary links -----------------------------------------------------

    async def glossary_links(self, *, datasource_id: UUID | None) -> None:
        scan = await find_glossary_link_candidates(
            self.session,
            organization_id=self.organization_id,
            minimum_confidence=_LINK_MINIMUM_CONFIDENCE,
            limit=self.outcome.limit,
            datasource_id=datasource_id,
            active_tables_only=True,
        )
        for candidate in scan.candidates:
            if not await self.may_continue():
                return
            self.outcome.items.append(await self._glossary_link(candidate))

    async def _glossary_link(self, candidate: GlossaryLinkCandidate) -> StewardRunItem:
        capability = CAPABILITY_GLOSSARY_LINK
        object_type = PROPOSAL_OBJECT_TYPES[capability]
        table_id, table_name = candidate.table.id, candidate.table.name
        term_id, term_name = candidate.term.id, candidate.term_version.display_name
        if self.outcome.mode == "OBSERVE":
            return StewardRunItem(
                capability=capability,
                table_id=table_id,
                table_name=table_name,
                action=ACTION_WOULD_PROPOSE,
                object_type=object_type,
                confidence=candidate.confidence,
                term_id=term_id,
                term_name=term_name,
            )
        try:
            async with self.session.begin_nested():
                proposal = build_glossary_link_proposal(
                    candidate,
                    organization_id=self.organization_id,
                    created_by=self.authority.principal_id,
                )
                # Submitted as it is created, the transition a steward makes
                # with `submit_glossary_link_proposal`.
                proposal.status = "REVIEW_REQUIRED"
                self.session.add(proposal)
                await self.session.flush()
                review = await self.open_review(
                    object_type=object_type,
                    object_id=proposal.id,
                    requested_action="APPROVE_LINK",
                    details={
                        "table_id": str(table_id),
                        "term_id": str(term_id),
                        "confidence": candidate.confidence,
                    },
                )
                proposal.governance_review_id = review.id
                task = await self.record_task(
                    capability,
                    review=review,
                    inputs={
                        "capability": capability,
                        "table_id": str(table_id),
                        "term_id": str(term_id),
                        "source_annotation_id": str(candidate.source_annotation_id),
                    },
                    evidence={
                        "object_type": object_type,
                        "object_id": str(proposal.id),
                        "confidence": candidate.confidence,
                    },
                )
                self.pending += 1
                return StewardRunItem(
                    capability=capability,
                    table_id=table_id,
                    table_name=table_name,
                    action=ACTION_PROPOSED,
                    object_type=object_type,
                    object_id=proposal.id,
                    review_id=review.id,
                    task_id=task.id,
                    confidence=candidate.confidence,
                    term_id=term_id,
                    term_name=term_name,
                )
        except StewardAgentRefused:
            raise
        except Exception as exc:  # noqa: BLE001 -- one link's failure must not end the run
            return await self.failed(
                capability,
                table_id=table_id,
                table_name=table_name,
                exc=exc,
                term_id=term_id,
                term_name=term_name,
            )

    # --- shared writes ------------------------------------------------------

    async def open_review(
        self,
        *,
        object_type: str,
        object_id: UUID,
        requested_action: str,
        details: dict[str, Any],
    ) -> GovernanceReview:
        """Put one proposal in the review queue as the agent's own request.

        The tier check is structural, not configuration: this agent may only
        ever ask for decisions an agent could in principle be trusted near
        (ADR-0027's T0/T1). Every capability today proposes inside it; the
        check is what stops a future one proposing outside it.
        """
        tier = risk_tier_for(object_type)
        if not tier_at_or_below(tier, HARD_MAX_AGENT_TIER):
            raise StewardAgentRefused(REASON_OBJECT_TYPE_ABOVE_CEILING)
        review = GovernanceReview(
            organization_id=self.organization_id,
            object_type=object_type,
            object_id=str(object_id),
            requested_action=requested_action,
            requested_by=self.authority.principal_id,
        )
        self.session.add(review)
        await self.session.flush()
        record_audit(
            self.session,
            self.agent_context,
            action="steward_agent.propose",
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

    async def record_task(
        self,
        capability: str,
        *,
        review: GovernanceReview,
        inputs: dict[str, Any],
        evidence: dict[str, Any],
    ) -> AgentTask:
        """The ledger row for one proposal. `inputs` and `evidence` carry ids,
        hashes and scores only -- never drafted text (INV-6)."""
        task = await record_agent_task(
            self.session,
            organization_id=self.organization_id,
            agent_principal_id=self.authority.principal_id,
            intent=_INTENTS[capability],
            inputs=inputs,
            ai_asset_version_id=self.authority.version.id,
            proposal_ref_type="GOVERNANCE_REVIEW",
            proposal_ref_id=review.id,
            sampling_rate=self.authority.contract.sampling_rate,
        )
        task.evidence = {"run_id": self.outcome.run_id, "review_id": str(review.id), **evidence}
        return task

    async def failed(
        self,
        capability: str,
        *,
        table_id: UUID,
        table_name: str,
        exc: Exception,
        worklist_rank: int | None = None,
        term_id: UUID | None = None,
        term_name: str | None = None,
    ) -> StewardRunItem:
        """One item's failure, after its savepoint has unwound.

        Only the exception's class is kept -- never its message, which can carry
        a value (the same INV-6 rule the ingest drafter's handler follows). A
        proposing run records the failure in the ledger; an observing run writes
        nothing, failures included.
        """
        error_class = type(exc).__name__
        task_id: UUID | None = None
        if self.outcome.mode == "PROPOSE":
            task = await record_agent_task(
                self.session,
                organization_id=self.organization_id,
                agent_principal_id=self.authority.principal_id,
                intent=_INTENTS[capability],
                inputs={
                    "capability": capability,
                    "table_id": str(table_id),
                    "term_id": str(term_id) if term_id else None,
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
        return StewardRunItem(
            capability=capability,
            table_id=table_id,
            table_name=table_name,
            action=ACTION_FAILED,
            reason=f"error:{error_class}",
            task_id=task_id,
            worklist_rank=worklist_rank,
            term_id=term_id,
            term_name=term_name,
        )


async def run_steward_agent(
    session: AsyncSession,
    organization_id: UUID,
    *,
    request: StewardRunRequest,
    settings: Settings,
    triggered_by: SecurityContext,
) -> StewardRunOutcome:
    """One bounded run. Writes nothing it commits: the caller owns the
    transaction, commits a returned outcome, and rolls back on
    `StewardAgentRefused` (see the module docstring on why)."""
    unknown = set(request.capabilities) - set(CAPABILITIES)
    if unknown or not request.capabilities:
        raise ValueError("steward agent capabilities must be a non-empty subset of CAPABILITIES")
    authority = await resolve_steward_authority(session, organization_id, settings=settings)
    blocking = await agent_kill_blocking_reason(session, authority.contract)
    if blocking is not None:
        raise StewardAgentRefused(blocking)

    outcome = StewardRunOutcome(
        run_id=str(uuid4()),
        organization_id=organization_id,
        agent_principal_id=authority.principal_id,
        ai_asset_version_id=authority.version.id,
        autonomy_tier=authority.contract.autonomy_tier,
        mode="OBSERVE" if request.dry_run else mode_for(authority.contract.autonomy_tier),
        dry_run=request.dry_run,
        limit=max(1, min(request.limit, settings.steward_agent_max_proposals_per_run)),
        capabilities=tuple(c for c in CAPABILITIES if c in request.capabilities),
        started_at=datetime.now(UTC),
    )
    run = _Run(
        session,
        authority=authority,
        settings=settings,
        outcome=outcome,
        pending=await pending_proposal_count(
            session, organization_id, agent_principal_id=authority.principal_id
        ),
    )
    if CAPABILITY_TABLE_DESCRIPTION in outcome.capabilities:
        await run.table_descriptions(datasource_id=request.datasource_id)
    if CAPABILITY_GLOSSARY_LINK in outcome.capabilities and outcome.stopped_reason is None:
        await run.glossary_links(datasource_id=request.datasource_id)
    outcome.finished_at = datetime.now(UTC)

    # The run itself is attributed to the human who started it; each proposal's
    # own audit row (`steward_agent.propose`) is attributed to the agent.
    record_audit(
        session,
        replace(triggered_by, organization_id=organization_id),
        action="steward_agent.run",
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
            "proposed": outcome.count(ACTION_PROPOSED),
            "would_propose": outcome.count(ACTION_WOULD_PROPOSE),
            "failed": outcome.count(ACTION_FAILED),
            "skipped": outcome.skipped_by_reason(),
            "stopped_reason": outcome.stopped_reason,
        },
    )
    return outcome


def record_steward_refusal(
    session: AsyncSession,
    organization_id: UUID,
    *,
    triggered_by: SecurityContext,
    reason_code: str,
) -> None:
    """The DENIED audit row for a refused run. Written by the caller in a fresh
    transaction, after the refused run's own writes were rolled back."""
    record_audit(
        session,
        replace(triggered_by, organization_id=organization_id),
        action="steward_agent.run",
        resource_type="agent_contract",
        resource_id=None,
        outcome="DENIED",
        correlation_id=get_correlation_id(),
        details={"reason": reason_code},
    )


# ---------------------------------------------------------------------------
# What the agent is, and how its proposals have fared
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StewardOutcomeRow:
    """How one kind of the agent's proposals has been decided."""

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


async def steward_agent_outcomes(
    session: AsyncSession, organization_id: UUID, *, agent_principal_id: str
) -> list[StewardOutcomeRow]:
    """Every review the agent requested, by object type and status.

    Read from the reviews rather than the task ledger: the review is where the
    decision is recorded, and the agent's identity on `requested_by` is the
    attribution. This is the outcome measure the 2026-09-09 architecture review
    asks for in place of counting agents -- how often the agent's work survives
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
    result: list[StewardOutcomeRow] = []
    for object_type in sorted(counts):
        by_status = counts[object_type]
        pending = by_status.pop("PENDING", 0)
        approved = by_status.pop("APPROVED", 0)
        rejected = by_status.pop("REJECTED", 0)
        result.append(
            StewardOutcomeRow(
                object_type=object_type,
                pending=pending,
                approved=approved,
                rejected=rejected,
                other=sum(by_status.values()),
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class StewardAgentStatus:
    agent_principal_id: str
    authority: StewardAuthority | None
    refusal_reason: str | None
    blocking_reason: str | None
    pending_proposals: int
    outcomes: list[StewardOutcomeRow]


async def steward_agent_status(
    session: AsyncSession, organization_id: UUID, *, settings: Settings
) -> StewardAgentStatus:
    """Whether the agent could run here now, and if not, why not.

    A status read, not a run: it resolves authority and the kill switch the
    same way a run would, and reports the refusal a run would get instead of
    raising it.
    """
    principal = settings.steward_agent_principal_id.strip()
    authority: StewardAuthority | None = None
    refusal: str | None = None
    try:
        authority = await resolve_steward_authority(session, organization_id, settings=settings)
    except StewardAgentRefused as exc:
        refusal = exc.reason_code
    blocking = (
        await agent_kill_blocking_reason(session, authority.contract)
        if authority is not None
        else None
    )
    return StewardAgentStatus(
        agent_principal_id=principal,
        authority=authority,
        refusal_reason=refusal,
        blocking_reason=blocking,
        pending_proposals=await pending_proposal_count(
            session, organization_id, agent_principal_id=principal
        ),
        outcomes=await steward_agent_outcomes(
            session, organization_id, agent_principal_id=principal
        ),
    )
