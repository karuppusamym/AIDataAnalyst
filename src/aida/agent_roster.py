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
   see `agent_intelligence.AgentPlan`), aggregated across an organization's
   recent runs rather than a static, hand-written description -- reusing
   TL-6's own `aida.fleet.tool_first_execution_rate` /
   `aida.tool_first_rate.compute_tool_first_rate` verbatim for the
   tool-first/freeform `generation_source` split, exactly as this row's own
   tracker note directs ("reusing TL-6's tool_first_rate/generation_source
   composition ... don't re-derive from scratch"). The `strategy` and
   `confidence` breakdown alongside it comes from the same `plan_evidence`
   blob TL-6 does not touch.

3. **Live results** -- a bounded, paginated window over the same
   organization's most recent `AgentRun` rows (status, strategy, confidence,
   generation_source, failure reason), the "recent live results" half of
   this row's exit condition.

**The organization-wide correlation gap, stated honestly.** `AgentRun`
(`aida/models.py`) has no foreign key back to `AiAsset`/`AiAssetVersion` --
checked directly against the model, not assumed. This platform's own
runtime produces `AgentRun` rows from exactly one operational code path
(`GovernedAgentOrchestrator.run`), scoped only by `organization_id`/
`datasource_id`, never by "which registered agent". The AI registry, by
contrast, can hold any number of `AGENT`-kind entries per organization --
including ones that describe an agent this platform does not itself execute
(see `tests/test_ai_registry.py`'s own "Fraud triage agent" fixture, a
governance dossier for something no `agent_orchestrator.py` code path runs).
There is therefore no persisted way to attribute a specific `AgentRun` to a
specific registered `AiAsset` today, and manufacturing one here (e.g. a
name-matching heuristic) would be exactly the kind of fabrication the
tracker row forbids. Instead, every registered `AGENT`-kind asset in an
organization is shown alongside that *organization's* real, aggregate
governed-run activity -- clearly labelled `scope="ORGANIZATION_WIDE"` with
an explanatory `note`, never claimed to be that one registered entity's own
private execution history. A future row that adds an `AgentRun -> AiAsset`
link (a real schema change, out of scope here -- `models.py` is read-only
for this row) can narrow this from organization-wide to per-asset without
changing the response shape.

**Auto-apply threshold, stated honestly.** This row's exit condition:
"plans that end in an auto-apply branch state the threshold that governs
them." Checked directly, not assumed: every AI-authored proposal pathway in
this codebase --  glossary-link proposals (`stewardship_api.
generate_glossary_link_proposals`), asset-description drafts
(`asset_description_service.py`, whose own module docstring states outright
that it "rejects the no-review auto-apply" pattern used by comparable
products), metric suggestions (`metric_suggestion_service.py`,
`apply_metric_suggestion_proposal` callable *only* from
`semantic_api.decide_governance_review` after its maker-checker guard), and
every bulk stewardship operation (GL-2/GL-5/GL-7, `stewardship_api.py`
`_create_bulk_operation` -> `GovernanceReview`) -- is submitted to the
shared `GovernanceReview` maker-checker queue. None has a confidence-gated
branch that applies an AI-authored action without a human decision; the two
"confidence"-named values that do exist in this codebase (glossary
label-match confidence in `generate_glossary_link_proposals`, and
`Settings.agent_tool_match_threshold`, which `agent_intelligence.
GovernedPlanner.plan` uses to gate which governed tool is *eligible* to
answer a question) each gate something other than a governance action
bypassing review -- the first
still lands as a review-queue proposal, the second only chooses between two
ways of *answering a read-only question*, not applying a change. AT-1's own
tracker row proposes introducing a real confidence-threshold auto-apply
branch "mirroring GL-2/GL-5" -- but as of this row, AT-1 is `TODO` and GL-2/
GL-5 do not actually have one either. So every agent in the roster is
reported with `has_auto_apply_branch=False`; if a future row adds a genuine
one, this module's `_AUTO_APPLY_EVIDENCE` is the single place to change.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import computed_field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.fleet import tool_first_execution_rate
from aida.models import AgentRun, AiAsset, AiAssetVersion
from aida.schemas import ApiModel
from aida.tool_first_rate import DEFAULT_WINDOW_DAYS

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
#: Kept as one named constant so a future row that adds a real auto-apply
#: branch has exactly one place to change this module's answer.
_AUTO_APPLY_EVIDENCE = (
    "No agent plan in this codebase reaches a branch that applies an "
    "AI-authored action without a human decision. Every proposal-shaped "
    "output routes through the shared GovernanceReview maker-checker queue: "
    "glossary-link proposals (stewardship_api.generate_glossary_link_proposals), "
    "asset-description drafts (asset_description_service.py -- its own module "
    "docstring states it \"rejects the no-review auto-apply\" pattern), metric "
    "suggestions (metric_suggestion_service.apply_metric_suggestion_proposal, "
    "callable only from semantic_api.decide_governance_review after its "
    "maker-checker guard), and bulk stewardship operations (GL-2/GL-5/GL-7, "
    "stewardship_api.py). AT-1's tracker row proposes a real confidence-"
    "threshold auto-apply branch \"mirroring GL-2/GL-5\", but AT-1 is not yet "
    "built and GL-2/GL-5 do not have one today."
)


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
    hand-written description, but what this organization's governed agent
    has actually been doing lately, aggregated from `AgentRun.plan_evidence`
    (`agent_intelligence.AgentPlan.evidence()`) and `generation_source`.
    """

    scope: Literal["ORGANIZATION_WIDE"]
    note: str
    window_days: int
    sampled_runs: int
    by_strategy: dict[str, int]
    average_confidence: float | None
    tool_first: ToolFirstRateSummaryRead


class AgentRunOutcomeRead(ApiModel):
    """One recent `AgentRun`'s outcome -- the "live results" half of this
    row's exit condition.
    """

    run_id: UUID
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_agents(self) -> int:
        return len(self.agents)


def _auto_apply_for(_asset: AiAsset, _version: AiAssetVersion) -> AgentAutoApplyRead:
    """See this module's docstring, "Auto-apply threshold, stated honestly."

    Takes the asset/version so a future row that adds a genuine per-agent
    auto-apply configuration (e.g. AT-1) has an obvious place to start
    reading it -- today no such configuration exists anywhere in this
    codebase, so every agent gets the same honest, evidence-carrying
    negative answer rather than a per-agent guess.
    """
    return AgentAutoApplyRead(
        has_auto_apply_branch=False,
        threshold=None,
        threshold_source=None,
        evidence=_AUTO_APPLY_EVIDENCE,
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


async def _compose_method_summary(
    session: AsyncSession,
    *,
    organization_id: UUID,
    window_days: int,
    now: datetime,
    sample_limit: int,
) -> AgentMethodSummaryRead:
    """Aggregate `AgentRun.plan_evidence`/`generation_source` for one
    organization's rolling window. `tool_first_execution_rate` (TL-6) is
    reused verbatim for the tool-first/freeform split; the `strategy`/
    `confidence` breakdown here is new aggregation over the same
    `plan_evidence` blob TL-6 does not read.
    """
    tool_first = await tool_first_execution_rate(
        session, organization_id, window_days=window_days, now=now
    )
    since = now - timedelta(days=window_days)
    rows = (
        await session.execute(
            select(AgentRun.plan_evidence)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status == "COMPLETED",
                AgentRun.created_at >= since,
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
        scope="ORGANIZATION_WIDE",
        note=(
            "AgentRun carries no per-registered-agent identity today (see this "
            "module's docstring) -- this summarizes this organization's actual "
            "governed-agent run activity as a whole, not this specific "
            "registered entity's own isolated execution history."
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
    window_days: int,
    now: datetime,
    limit: int,
) -> tuple[list[AgentRunOutcomeRead], int]:
    since = now - timedelta(days=window_days)
    filters = (
        AgentRun.organization_id == organization_id,
        AgentRun.created_at >= since,
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


async def compose_agent_roster(
    session: AsyncSession,
    *,
    organization_id: UUID,
    window_days: int = DEFAULT_WINDOW_DAYS,
    recent_results_limit: int = DEFAULT_RECENT_RESULTS_LIMIT,
    method_sample_limit: int = DEFAULT_METHOD_SAMPLE_LIMIT,
    now: datetime | None = None,
) -> AgentRosterRead:
    """Compose the agent roster for one organization.

    For every `AiAsset` of `asset_kind == "AGENT"` in the organization, at
    its latest `AiAssetVersion` (same "latest version" query shape as
    `ai_registry_api.list_ai_assets`): published purpose, an aggregated
    method summary, a bounded recent-results window, and an honest
    auto-apply determination. Returns an empty roster (not an error) when
    the organization has registered no `AGENT`-kind asset -- that is itself
    an honest, informative answer, not a defect.
    """
    moment = now or datetime.now(UTC)
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

    if not rows:
        return AgentRosterRead(
            organization_id=organization_id,
            generated_at=moment,
            window_days=window_days,
            agents=[],
        )

    # Org-wide method summary and recent-results window are the same for
    # every entry (see this module's docstring on the correlation gap) --
    # computed once, not once per registered agent.
    method = await _compose_method_summary(
        session,
        organization_id=organization_id,
        window_days=window_days,
        now=moment,
        sample_limit=method_sample_limit,
    )
    recent_results, recent_results_total = await _recent_results(
        session,
        organization_id=organization_id,
        window_days=window_days,
        now=moment,
        limit=recent_results_limit,
    )

    agents = [
        AgentRosterEntryRead(
            purpose=_purpose_read(asset, version),
            method=method,
            recent_results=recent_results,
            recent_results_total=recent_results_total,
            auto_apply=_auto_apply_for(asset, version),
        )
        for asset, version in rows
    ]
    return AgentRosterRead(
        organization_id=organization_id,
        generated_at=moment,
        window_days=window_days,
        agents=agents,
    )
