import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import DataSource
from aida.prompt_risk import PromptRiskAssessment
from aida.retrieval import hybrid_retrieve_enhanced

STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "by",
        "for",
        "from",
        "in",
        "is",
        "latest",
        "list",
        "of",
        "on",
        "show",
        "the",
        "to",
        "with",
    }
)


def normalized_terms(text: str) -> tuple[str, ...]:
    terms = re.findall(r"[a-z0-9]+", text.lower())
    return tuple(dict.fromkeys(term for term in terms if len(term) > 1 and term not in STOP_WORDS))


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    object_type: str
    object_id: str
    display_name: str
    score: float
    reason_codes: list[str]
    metadata: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentPlan:
    strategy: Literal[
        "GOVERNED_TOOL",
        "DEVELOPMENT_SQL",
        "MODEL_GENERATION",
        "CLARIFICATION",
        "BLOCKED",
    ]
    confidence: float
    reason_codes: list[str]
    selected_tool_version_id: str | None
    required_parameters: list[str]
    retrieval_object_ids: list[str]
    prompt_risk: dict[str, object] = field(default_factory=dict)
    tool_decisions: list[dict[str, str]] = field(default_factory=list)

    def evidence(self) -> dict[str, Any]:
        return asdict(self)


class GovernedRetriever:
    """Organization-scoped governed metadata retrieval.

    Delegates to `aida.retrieval.hybrid_retrieve_enhanced` -- lexical BM25 +
    vector similarity + graph expansion + fusion ranking (RT-1, RT-2, RT-3,
    RT-9, SM-2) -- rather than the narrower hand-rolled lexical scan this class
    used to run itself. That scan and this class had drifted into two parallel
    retrieval implementations: `retrieval.py` was fully built, independently
    tested, and never called from here, the exact gap
    `Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` §2 found. Only the
    result shape is translated back to `RetrievalHit` here; every org/status
    scoping filter and the business-annotation/glossary-binding reading this
    class already did correctly now lives in `retrieval.py` (SM-2's own
    hand-off comment), unchanged.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        question: str,
        preferred_tool_version_id: UUID | None = None,
    ) -> list[RetrievalHit]:
        """The bounded, read-only retrieval path: every scored candidate, capped
        at ``agent_retrieval_limit``. Never writes -- used by the retrieval-preview
        endpoint and the control-evaluation suite as well as the live orchestrator.
        """
        hits = await hybrid_retrieve_enhanced(
            session,
            datasource=datasource,
            question=question,
            settings=self.settings,
            preferred_tool_version_id=preferred_tool_version_id,
        )
        return [
            RetrievalHit(
                object_type=hit.object_type,
                object_id=hit.object_id,
                display_name=hit.display_name,
                score=hit.score,
                reason_codes=hit.reason_codes,
                metadata=hit.metadata,
            )
            for hit in hits
        ]

    async def score_candidates(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        question: str,
        preferred_tool_version_id: UUID | None = None,
    ) -> list[RetrievalHit]:
        """Every candidate the fusion stage ranked, unbounded -- also read-only.

        AU-5: the live orchestrator calls this directly (rather than ``retrieve``)
        so it can see the candidates ``retrieve``'s ``agent_retrieval_limit`` cap
        would otherwise discard, and record them as ``RETRIEVAL_REJECTED``
        decision edges itself. Recording lives in the orchestrator, not here, so
        this module -- also used by the read-only retrieval-preview endpoint --
        stays free of any write the INV-7 read-only-route gate would trip on.

        `hybrid_retrieve_enhanced` takes no result-limit override of its own --
        every stage (the lexical scan, the fusion cut) reads
        ``settings.agent_retrieval_limit`` directly -- so rather than adding one
        to `retrieval.py` (owned by RT-1/RT-2/RT-3/RT-9/SM-2, landed the same day
        as this change), a `Settings` copy with `agent_retrieval_limit` widened to
        `agent_retrieval_scan_limit` (the same bound `hybrid_retrieve`'s own
        per-object-type candidate fetch already uses) is passed in its place, so
        nothing the fusion stage ranked is silently dropped before the
        orchestrator gets a chance to record it as rejected. Every other setting
        (embedding provider, secrets, fusion weights) is untouched.
        """
        widened_settings = self.settings.model_copy(
            update={"agent_retrieval_limit": self.settings.agent_retrieval_scan_limit}
        )
        hits = await hybrid_retrieve_enhanced(
            session,
            datasource=datasource,
            question=question,
            settings=widened_settings,
            preferred_tool_version_id=preferred_tool_version_id,
        )
        return [
            RetrievalHit(
                object_type=hit.object_type,
                object_id=hit.object_id,
                display_name=hit.display_name,
                score=hit.score,
                reason_codes=hit.reason_codes,
                metadata=hit.metadata,
            )
            for hit in hits
        ]


class GovernedPlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def plan(
        self,
        *,
        retrieval_hits: list[RetrievalHit],
        roles: frozenset[str],
        candidate_sql_available: bool,
        tool_parameters: dict[str, Any],
        preferred_tool_version_id: UUID | None = None,
        prompt_risk: PromptRiskAssessment | None = None,
    ) -> AgentPlan:
        risk_evidence = prompt_risk.evidence() if prompt_risk else {}
        if prompt_risk and prompt_risk.decision == "BLOCK":
            return AgentPlan(
                "BLOCKED",
                prompt_risk.score,
                ["PROMPT_POLICY_DENIED", *prompt_risk.reason_codes],
                None,
                [],
                [],
                risk_evidence,
            )
        tools = [hit for hit in retrieval_hits if hit.object_type == "GOVERNED_TOOL"]
        eligible: list[RetrievalHit] = []
        tool_decisions: list[dict[str, str]] = []
        for hit in tools:
            allowed = set(hit.metadata["allowed_roles"])
            role_allowed = "PlatformAdmin" in roles or not roles.isdisjoint(allowed)
            explicitly_selected = str(preferred_tool_version_id) == hit.object_id
            meets_threshold = (
                explicitly_selected or hit.score >= self.settings.agent_tool_match_threshold
            )
            if role_allowed and meets_threshold:
                eligible.append(hit)
            else:
                reason = (
                    "role not in tool allowed_roles"
                    if not role_allowed
                    else "score below the governed-tool match threshold"
                )
                tool_decisions.append(
                    {"tool_version_id": hit.object_id, "decision": "REJECTED", "reason": reason}
                )
        selected = eligible[0] if eligible else None
        for hit in eligible:
            if selected is not None and hit.object_id == selected.object_id:
                tool_decisions.append(
                    {
                        "tool_version_id": hit.object_id,
                        "decision": "SELECTED",
                        "reason": "highest-ranked eligible governed tool for this question",
                    }
                )
            else:
                tool_decisions.append(
                    {
                        "tool_version_id": hit.object_id,
                        "decision": "REJECTED",
                        "reason": "eligible but ranked below the selected governed tool",
                    }
                )
        if selected:
            required = list(selected.metadata["required_parameters"])
            missing = sorted(set(required) - set(tool_parameters))
            if not missing:
                return AgentPlan(
                    "GOVERNED_TOOL",
                    selected.score,
                    ["APPROVED_TOOL_FIRST", "ROLE_BINDING_SATISFIED"],
                    selected.object_id,
                    [],
                    [hit.object_id for hit in retrieval_hits],
                    risk_evidence,
                    tool_decisions,
                )
            if not candidate_sql_available or preferred_tool_version_id:
                return AgentPlan(
                    "CLARIFICATION",
                    selected.score,
                    ["APPROVED_TOOL_REQUIRES_PARAMETERS"],
                    selected.object_id,
                    missing,
                    [hit.object_id for hit in retrieval_hits],
                    risk_evidence,
                    tool_decisions,
                )
        if candidate_sql_available:
            return AgentPlan(
                "DEVELOPMENT_SQL",
                1.0,
                ["CONTROLLED_DEVELOPMENT_OVERRIDE"],
                None,
                [],
                [hit.object_id for hit in retrieval_hits],
                risk_evidence,
                tool_decisions,
            )
        return AgentPlan(
            "MODEL_GENERATION",
            max((hit.score for hit in retrieval_hits), default=0.0),
            ["NO_EXECUTABLE_APPROVED_TOOL", "MODEL_ROUTE_REQUIRED"],
            None,
            [],
            [hit.object_id for hit in retrieval_hits],
            risk_evidence,
            tool_decisions,
        )
