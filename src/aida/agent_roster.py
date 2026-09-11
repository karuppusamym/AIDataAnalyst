"""UX-19: agent roster with published purpose, task plan and live results.

A steward-facing surface -- "each agent's method is inspectable before its
output is trusted" (tracker UX-19) -- composed entirely from data that
already exists, in three parts:

1. **Purpose** -- EA.10c's AI registry (`aida.ai_registry_api`). Every
   `AiAsset` of `asset_kind == "AGENT"` carries a governed `AiAssetVersion`
   with real, steward-authored `name`/`description`/`intended_use`/
   `owner_principal`/`risk_tier` fields (`ai_asset_fingerprint` even hashes
   them, so they cannot silently drift without a new version) -- this is a
   genuine published purpose, not invented here.

2. **Method** -- AT-6/AT-16's `AgentRun.plan_evidence` (the exact JSON the
   real `GovernedPlanner.plan(...).evidence()` call in
   `agent_orchestrator.GovernedAgentOrchestrator.run` persists on every run,
   see `agent_intelligence.AgentPlan`), aggregated across *this agent
   version's* recent runs rather than a static, hand-written description --
   reusing TL-6's own `aida.tool_first_rate.compute_tool_first_rate` verbatim
   for the tool-first/freeform `generation_source` split, exactly as this
   row's own tracker note directs ("reusing TL-6's tool_first_rate/
   generation_source composition ... don't re-derive from scratch"). The pure
   TL-6 function is called directly rather than through
   `fleet.tool_first_execution_rate`, whose own `WHERE` cannot scope narrower
   than an organization. The `strategy` and `confidence` breakdown alongside
   it comes from the same `plan_evidence` blob TL-6 does not touch.

3. **Live results** -- a bounded, paginated window over that same agent
   version's most recent `AgentRun` rows (status, strategy, confidence,
   generation_source, failure reason), the "recent live results" half of
   this row's exit condition.

**Attribution, and what is still not attributable (AR-07).** The link this
module was written without now exists: `AgentRun.ai_asset_version_id`, a real
column, populated by `GovernedAgentOrchestrator._stage_screen` whenever a run
executes *as* a registered agent version under an `AgentContract`. Each roster
entry is therefore scoped to its own version's runs (`scope="AGENT_VERSION"`),
which is what a steward reading "this agent's recent results" reasonably
assumes they are being shown. Until 2026-09-09 this module still reported the
organization's *total* governed-run activity against every registered agent
row, and said in its own docstring that no link existed -- a statement that
had become false.

Two honesty properties survive the change, because the link being present
does not make it universal:

* A run with no `ai_asset_version_id` -- the ordinary case, an analyst asking
  a question through the runtime rather than a registered agent acting -- is
  attributed to nobody. Those runs are reported once, at the top level, as
  `unattributed`. They are never folded into an agent's own numbers.
* A registered `AGENT`-kind asset may describe an agent this platform does not
  execute at all (see `tests/test_ai_registry.py`'s "Fraud triage agent"
  fixture, a governance dossier for something no `agent_orchestrator.py` path
  runs). Such an entry now honestly reports zero runs rather than borrowing
  the organization's.

Name-matching or any other heuristic bridge between a run and a registration
remains out of the question: the foreign key is the only evidence used.

**Auto-apply threshold, stated honestly (AR-07).** This row's exit condition:
"plans that end in an auto-apply branch state the threshold that governs
them." The *proposal-authoring* pathways still have none -- glossary-link
proposals (`stewardship_api.generate_glossary_link_proposals`),
asset-description drafts (`asset_description_service.py`, whose own module
docstring states outright that it "rejects the no-review auto-apply" pattern
used by comparable products), metric suggestions
(`metric_suggestion_service.apply_metric_suggestion_proposal`, callable only
from `semantic_api.decide_governance_review` after its maker-checker guard)
and bulk stewardship operations all submit to the shared `GovernanceReview`
queue and stop there.

What changed is on the *checking* side. ADR-0027's
`reviewer_agent.auto_decide_tier0_tier1` is a genuine unattended-decision
branch, governed by `reviewer_agent_approve_confidence` and bounded by the
T0/T1 tier ceiling; it is off by default. This module therefore no longer
emits a blanket "no agent in this codebase auto-applies" for every row. An
asset whose contract carries `Settings.reviewer_agent_principal_id` reports
that branch and its real threshold; every other registered agent reports the
honest negative, worded so it describes *that agent*, not the platform.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import computed_field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.models import AgentContract, AgentRun, AiAsset, AiAssetVersion, AuditEvent
from aida.schemas import ApiModel
from aida.task_agent_registry import task_agent_for_principal
from aida.tool_first_rate import DEFAULT_WINDOW_DAYS, compute_tool_first_rate

#: Steward-facing recent-results window default -- a roster entry is a
#: summary, not a full audit export (`GET /v1/agent-runs/{id}` already
#: exists for one run's full detail); mirrors `consumer_footer.py`'s
#: `DEFAULT_CONSUMER_FOOTER_LIMIT` bounded-window idiom.
DEFAULT_RECENT_RESULTS_LIMIT = 20
MAX_RECENT_RESULTS_LIMIT = 200

#: Bound on how many recent `AgentRun` rows are pulled into the plan/method
#: aggregation itself (separate from `recent_results`, which is what is
#: actually returned). 500 mirrors `review_queue_api.py`'s own
#: `_MAX_QUEUE_ROWS` bounded-batch idiom -- large enough for a meaningful
#: method summary, never an unbounded scan.
DEFAULT_METHOD_SAMPLE_LIMIT = 500

#: See this module's docstring, "Auto-apply threshold, stated honestly."
#: The wording is about *this agent*, not about the platform: the platform
#: does have one unattended-decision branch (the ADR-0027 reviewer agent), and
#: a blanket denial here would be false since it shipped.
_NO_AUTO_APPLY_EVIDENCE = (
    "This agent has no branch that applies its own output without a human "
    "decision. Its proposal-shaped output routes through the shared "
    "GovernanceReview maker-checker queue, alongside glossary-link proposals "
    "(stewardship_api.generate_glossary_link_proposals), asset-description "
    "drafts (asset_description_service.py -- its own module docstring states "
    "it \"rejects the no-review auto-apply\" pattern), metric suggestions "
    "(metric_suggestion_service.apply_metric_suggestion_proposal, callable "
    "only from semantic_api.decide_governance_review after its maker-checker "
    "guard) and bulk stewardship operations. The platform's one unattended-"
    "decision branch belongs to the ADR-0027 reviewer agent, which is a "
    "different registered identity and is reported on its own row."
)

#: The reviewer agent's real branch (ADR-0027, `reviewer_agent.
#: auto_decide_tier0_tier1`), stated with the guards that bound it.
_REVIEWER_AUTO_APPLY_EVIDENCE = (
    "reviewer_agent.auto_decide_tier0_tier1 decides tier-eligible items "
    "without a human, through the same governance_decision_service.decide_review "
    "path a human checker uses. Bounded by: an immutable T0/T1 tier ceiling "
    "(review_risk_tiers.HARD_MAX_AGENT_TIER, which configuration cannot "
    "raise); positive object-specific evidence at or above the threshold, "
    "with abstention when a proposal carries none; a re-derivation of that "
    "evidence at decision time rather than at pre-review time; maker != "
    "checker; a 5%-floor audit sample of every approval; and a per-"
    "organization suspension re-read before each decision."
)

#: Scope of the run evidence attached to a roster entry.
#: `AGENT_VERSION` -- runs linked to this version by
#: `AgentRun.ai_asset_version_id`. `ORGANIZATION_WIDE` -- every run in the
#: organization, used only for the top-level `unattributed` block.
RunScope = Literal["AGENT_VERSION", "ORGANIZATION_WIDE"]


class AgentPurposeRead(ApiModel):
    """EA.10c AI registry data for one agent's latest version -- the
    "published purpose" half of this row's exit condition, verbatim from
    `AiAssetVersion` (`aida.ai_registry_api._apply_definition`), never
    re-worded here.
    """

    asset_id: UUID
    asset_key: str
    version: int
    status: str
    name: str
    description: str
    intended_use: str
    owner_principal: str
    provider_type: str
    risk_tier: str
    documentation_url: str | None


class AgentAutoApplyRead(ApiModel):
    """Whether this agent's plan has a real, code-backed auto-apply branch
    and, only when one genuinely exists, the threshold that governs it. See
    this module's docstring for how this is determined -- never a value
    invented for an agent that does not actually have one.
    """

    has_auto_apply_branch: bool
    threshold: float | None
    threshold_source: str | None
    #: Whether that branch is switched on right now. A branch that exists in
    #: code and is disabled by configuration is a materially different answer
    #: from one that does not exist, and collapsing the two would misreport
    #: both. `None` when there is no branch to enable.
    enabled: bool | None
    evidence: str


class ToolFirstRateSummaryRead(ApiModel):
    """TL-6's `aida.tool_first_rate.ToolFirstRate`, embedded verbatim --
    the same locally-scoped-`ApiModel` idiom `operational_api.
    ToolFirstRateRead.from_rate` already uses for this exact dataclass.
    """

    tool_first_executions: int
    freeform_executions: int
    total_executions: int
    rate: float | None
    by_source: dict[str, int]
    target_rate: float
    meets_target: bool | None


class AgentMethodSummaryRead(ApiModel):
    """The "task plan" half of this row's exit condition: not a static,
    hand-written description, but what this agent version has actually been
    doing lately, aggregated from `AgentRun.plan_evidence`
    (`agent_intelligence.AgentPlan.evidence()`) and `generation_source`.
    """

    scope: RunScope
    note: str
    window_days: int
    sampled_runs: int
    by_strategy: dict[str, int]
    average_confidence: float | None
    tool_first: ToolFirstRateSummaryRead


class UnattributedRunsRead(ApiModel):
    """Governed runs in this organization that no registered agent owns.

    `AgentRun.ai_asset_version_id` is NULL for these -- the ordinary case of a
    person asking the runtime a question, rather than a registered agent
    acting under a contract. Reported once at the top level so the numbers
    remain visible without being credited to an agent that did not produce
    them (AR-07).
    """

    method: AgentMethodSummaryRead
    recent_results: list[AgentRunOutcomeRead]
    recent_results_total: int


class AgentRunOutcomeRead(ApiModel):
    """One recent `AgentRun`'s outcome -- the "live results" half of this
    row's exit condition.
    """

    #: `None` for a refused task-agent run (ADR-0029): it never started, so it
    #: has no run of its own, and the row carries why it was stopped instead.
    run_id: UUID | None
    status: str
    strategy: str | None
    confidence: float | None
    generation_source: str
    created_at: datetime
    failure_reason: str | None


class AgentRosterEntryRead(ApiModel):
    purpose: AgentPurposeRead
    method: AgentMethodSummaryRead
    recent_results: list[AgentRunOutcomeRead]
    recent_results_total: int
    auto_apply: AgentAutoApplyRead


class AgentRosterRead(ApiModel):
    organization_id: UUID
    generated_at: datetime
    window_days: int
    agents: list[AgentRosterEntryRead]
    unattributed: UnattributedRunsRead

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_agents(self) -> int:
        return len(self.agents)


def _auto_apply_for(
    _asset: AiAsset,
    _version: AiAssetVersion,
    *,
    agent_principal_id: str | None,
    settings: Settings,
) -> AgentAutoApplyRead:
    """See this module's docstring, "Auto-apply threshold, stated honestly."

    `agent_principal_id` comes from this version's `AgentContract`, which is
    the only place a registered agent declares the workload identity it runs
    as. Matching it against `Settings.reviewer_agent_principal_id` is an
    identity comparison against declared configuration, not a name-matching
    heuristic: if no contract exists, or the identity differs, the answer is
    the negative one.
    """
    if agent_principal_id is not None and agent_principal_id == (
        settings.reviewer_agent_principal_id
    ):
        return AgentAutoApplyRead(
            has_auto_apply_branch=True,
            threshold=settings.reviewer_agent_approve_confidence,
            threshold_source="Settings.reviewer_agent_approve_confidence",
            enabled=(
                settings.reviewer_agent_enabled and not settings.reviewer_agent_suspended
            ),
            evidence=_REVIEWER_AUTO_APPLY_EVIDENCE,
        )
    return AgentAutoApplyRead(
        has_auto_apply_branch=False,
        threshold=None,
        threshold_source=None,
        enabled=None,
        evidence=_NO_AUTO_APPLY_EVIDENCE,
    )


def _purpose_read(asset: AiAsset, version: AiAssetVersion) -> AgentPurposeRead:
    return AgentPurposeRead(
        asset_id=asset.id,
        asset_key=asset.asset_key,
        version=version.version,
        status=version.status,
        name=version.name,
        description=version.description,
        intended_use=version.intended_use,
        owner_principal=version.owner_principal,
        provider_type=version.provider_type,
        risk_tier=version.risk_tier,
        documentation_url=version.documentation_url,
    )


def _scope_filter(ai_asset_version_id: UUID | None) -> Any:
    """The one place the attribution rule is expressed (AR-07).

    A version id scopes to that version's own runs; `None` scopes to the runs
    no registered agent owns. Nothing in this module ever scopes an agent
    entry to "every run in the organization" -- that shape was the defect.
    """
    if ai_asset_version_id is None:
        return AgentRun.ai_asset_version_id.is_(None)
    return AgentRun.ai_asset_version_id == ai_asset_version_id


async def _compose_method_summary(
    session: AsyncSession,
    *,
    organization_id: UUID,
    ai_asset_version_id: UUID | None,
    window_days: int,
    now: datetime,
    sample_limit: int,
) -> AgentMethodSummaryRead:
    """Aggregate `AgentRun.plan_evidence`/`generation_source` for one scope's
    rolling window.

    TL-6's ratio is still reused verbatim -- `compute_tool_first_rate` is the
    pure function `fleet.tool_first_execution_rate` itself calls, and it is
    handed the same `{generation_source: count}` shape. What changed is only
    the `WHERE`: `tool_first_execution_rate` can scope to an organization and
    nothing narrower, and an agent entry needs its own version's runs.
    """
    since = now - timedelta(days=window_days)
    scope = _scope_filter(ai_asset_version_id)
    source_rows = (
        await session.execute(
            select(AgentRun.generation_source, func.count())
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status == "COMPLETED",
                AgentRun.created_at >= since,
                scope,
            )
            .group_by(AgentRun.generation_source)
        )
    ).all()
    tool_first = compute_tool_first_rate(
        organization_id=organization_id,
        window_days=window_days,
        generation_source_counts={str(source): int(count) for source, count in source_rows},
        now=now,
    )
    rows = (
        await session.execute(
            select(AgentRun.plan_evidence)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status == "COMPLETED",
                AgentRun.created_at >= since,
                scope,
            )
            .order_by(AgentRun.created_at.desc())
            .limit(sample_limit)
        )
    ).all()
    strategies: Counter[str] = Counter()
    confidences: list[float] = []
    for (plan_evidence,) in rows:
        evidence = plan_evidence or {}
        strategy = evidence.get("strategy")
        if isinstance(strategy, str):
            strategies[strategy] += 1
        confidence = evidence.get("confidence")
        if isinstance(confidence, int | float) and not isinstance(confidence, bool):
            confidences.append(float(confidence))
    average_confidence = round(sum(confidences) / len(confidences), 4) if confidences else None
    return AgentMethodSummaryRead(
        scope="AGENT_VERSION" if ai_asset_version_id is not None else "ORGANIZATION_WIDE",
        note=(
            "Runs linked to this agent version by AgentRun.ai_asset_version_id, "
            "which the orchestrator sets when a run executes under this "
            "version's contract. A registered agent this platform does not "
            "execute reports zero runs rather than the organization's."
            if ai_asset_version_id is not None
            else (
                "Governed runs in this organization that carry no registered-"
                "agent identity -- typically a person asking the runtime a "
                "question. Attributed to no agent."
            )
        ),
        window_days=window_days,
        sampled_runs=sum(strategies.values()),
        by_strategy=dict(sorted(strategies.items())),
        average_confidence=average_confidence,
        tool_first=ToolFirstRateSummaryRead(
            tool_first_executions=tool_first.tool_first_executions,
            freeform_executions=tool_first.freeform_executions,
            total_executions=tool_first.total_executions,
            rate=tool_first.rate,
            by_source=tool_first.by_source,
            target_rate=tool_first.target_rate,
            meets_target=tool_first.meets_target,
        ),
    )


async def _recent_results(
    session: AsyncSession,
    *,
    organization_id: UUID,
    ai_asset_version_id: UUID | None,
    window_days: int,
    now: datetime,
    limit: int,
) -> tuple[list[AgentRunOutcomeRead], int]:
    since = now - timedelta(days=window_days)
    filters = (
        AgentRun.organization_id == organization_id,
        AgentRun.created_at >= since,
        _scope_filter(ai_asset_version_id),
    )
    total = await session.scalar(select(func.count()).select_from(AgentRun).where(*filters))
    rows = (
        await session.execute(
            select(AgentRun)
            .where(*filters)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    results = [
        AgentRunOutcomeRead(
            run_id=run.id,
            status=run.status,
            strategy=(run.plan_evidence or {}).get("strategy"),
            confidence=(run.plan_evidence or {}).get("confidence"),
            generation_source=run.generation_source,
            created_at=run.created_at,
            failure_reason=run.failure_reason,
        )
        for run in rows
    ]
    return results, int(total or 0)


async def _contract_principals(
    session: AsyncSession, *, organization_id: UUID, version_ids: list[UUID]
) -> dict[UUID, str]:
    """`ai_asset_version_id -> agent_principal_id` for the versions that have
    a contract. One query, not one per row."""
    if not version_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentContract.ai_asset_version_id, AgentContract.agent_principal_id).where(
                AgentContract.organization_id == organization_id,
                AgentContract.ai_asset_version_id.in_(version_ids),
            )
        )
    ).all()
    return {version_id: principal for version_id, principal in rows}


def _task_agent_note(run_action: str) -> str:
    """ADR-0029: a task agent runs, but writes no `AgentRun`. Its recent results
    come from the audit row each run leaves, completed or refused, and the
    method figures, which describe planned runs, stay empty by construction --
    not because it did nothing. Saying so is the AR-07 rule applied the other
    way: a number that looks like evidence of inactivity must not be left to
    read as one."""
    return (
        "A task agent (ADR-0029). It writes no AgentRun rows: each run is a "
        f"{run_action} audit event, listed here as a recent result -- a refused "
        "one with the reason it was stopped -- and each proposal a ledger task. "
        "Its method is deterministic, so the strategy and tool-first figures do "
        "not apply."
    )


async def _task_agent_recent_results(
    session: AsyncSession,
    *,
    organization_id: UUID,
    ai_asset_version_id: UUID,
    run_action: str,
    method: str,
    window_days: int,
    now: datetime,
    limit: int,
) -> tuple[list[AgentRunOutcomeRead], int]:
    """A task agent's runs, completed and refused, from the audit row each one
    leaves.

    Scoped the AR-07 way: the row's resource is this version, and nothing is
    inferred from a name. A refused run never started, so it has no run id. It
    is listed with the reason its refusal recorded -- the kill switch, an
    autonomy tier withdrawn, a version no longer approved -- which is what a
    supervisor looking at an agent that is not working needs to see. (A full
    review backlog is not a refusal: it stops a run, which still completes.)
    Only a
    refusal after authority resolved names a version, so one refused before
    that, with no approved contract at all, belongs to no roster entry.
    """
    since = now - timedelta(days=window_days)
    filters = (
        AuditEvent.organization_id == organization_id,
        AuditEvent.action == run_action,
        AuditEvent.resource_type == "agent_contract",
        AuditEvent.resource_id == str(ai_asset_version_id),
        AuditEvent.outcome.in_(("SUCCESS", "DENIED")),
        AuditEvent.occurred_at >= since,
    )
    total = await session.scalar(select(func.count()).select_from(AuditEvent).where(*filters))
    events = (
        await session.scalars(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(limit)
        )
    ).all()
    results: list[AgentRunOutcomeRead] = []
    for event in events:
        details = event.details or {}
        if event.outcome == "DENIED":
            results.append(
                AgentRunOutcomeRead(
                    run_id=None,
                    status="REFUSED",
                    strategy=None,
                    confidence=None,
                    generation_source=method,
                    created_at=event.occurred_at,
                    failure_reason=str(details.get("reason") or "unspecified"),
                )
            )
            continue
        try:
            run_id = UUID(str(details.get("run_id")))
        except ValueError:
            continue
        results.append(
            AgentRunOutcomeRead(
                run_id=run_id,
                status="COMPLETED",
                strategy=None,
                confidence=None,
                generation_source=method,
                created_at=event.occurred_at,
                failure_reason=None,
            )
        )
    return results, int(total or 0)


async def compose_agent_roster(
    session: AsyncSession,
    *,
    organization_id: UUID,
    settings: Settings | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    recent_results_limit: int = DEFAULT_RECENT_RESULTS_LIMIT,
    method_sample_limit: int = DEFAULT_METHOD_SAMPLE_LIMIT,
    now: datetime | None = None,
) -> AgentRosterRead:
    """Compose the agent roster for one organization.

    For every `AiAsset` of `asset_kind == "AGENT"` in the organization, at
    its latest `AiAssetVersion` (same "latest version" query shape as
    `ai_registry_api.list_ai_assets`): published purpose, a method summary
    aggregated over *that version's own* runs, a bounded recent-results
    window over the same, and an auto-apply determination read from the
    version's contract. Runs no registered agent owns are reported once, at
    the top level, as `unattributed`.

    Returns an empty agent list (not an error) when the organization has
    registered no `AGENT`-kind asset -- that is itself an honest, informative
    answer, not a defect. The `unattributed` block is still computed, because
    an organization with no registered agents can still have governed runs.
    """
    moment = now or datetime.now(UTC)
    resolved_settings = settings or get_settings()
    latest_version = (
        select(func.max(AiAssetVersion.version))
        .where(AiAssetVersion.asset_id == AiAsset.id)
        .correlate(AiAsset)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(AiAsset, AiAssetVersion)
            .join(AiAssetVersion, AiAssetVersion.asset_id == AiAsset.id)
            .where(
                AiAsset.organization_id == organization_id,
                AiAsset.asset_kind == "AGENT",
                AiAssetVersion.version == latest_version,
            )
            .order_by(AiAsset.asset_key)
        )
    ).all()

    unattributed_method = await _compose_method_summary(
        session,
        organization_id=organization_id,
        ai_asset_version_id=None,
        window_days=window_days,
        now=moment,
        sample_limit=method_sample_limit,
    )
    unattributed_results, unattributed_total = await _recent_results(
        session,
        organization_id=organization_id,
        ai_asset_version_id=None,
        window_days=window_days,
        now=moment,
        limit=recent_results_limit,
    )
    unattributed = UnattributedRunsRead(
        method=unattributed_method,
        recent_results=unattributed_results,
        recent_results_total=unattributed_total,
    )

    principals = await _contract_principals(
        session,
        organization_id=organization_id,
        version_ids=[version.id for _asset, version in rows],
    )

    agents: list[AgentRosterEntryRead] = []
    for asset, version in rows:
        method = await _compose_method_summary(
            session,
            organization_id=organization_id,
            ai_asset_version_id=version.id,
            window_days=window_days,
            now=moment,
            sample_limit=method_sample_limit,
        )
        task_agent = task_agent_for_principal(resolved_settings, principals.get(version.id))
        if task_agent is not None:
            method = method.model_copy(
                update={"note": _task_agent_note(task_agent.spec.run_action)}
            )
            recent_results, recent_results_total = await _task_agent_recent_results(
                session,
                organization_id=organization_id,
                ai_asset_version_id=version.id,
                run_action=task_agent.spec.run_action,
                method=task_agent.spec.method,
                window_days=window_days,
                now=moment,
                limit=recent_results_limit,
            )
        else:
            recent_results, recent_results_total = await _recent_results(
                session,
                organization_id=organization_id,
                ai_asset_version_id=version.id,
                window_days=window_days,
                now=moment,
                limit=recent_results_limit,
            )
        agents.append(
            AgentRosterEntryRead(
                purpose=_purpose_read(asset, version),
                method=method,
                recent_results=recent_results,
                recent_results_total=recent_results_total,
                auto_apply=_auto_apply_for(
                    asset,
                    version,
                    agent_principal_id=principals.get(version.id),
                    settings=resolved_settings,
                ),
            )
        )
    return AgentRosterRead(
        organization_id=organization_id,
        generated_at=moment,
        window_days=window_days,
        agents=agents,
        unattributed=unattributed,
    )
