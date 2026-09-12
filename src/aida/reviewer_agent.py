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

Guards, in order, on every auto-decision:

1. The object type's tier must be at or below the ceiling *in force*, AND
   the type must appear in the allowlist derived from the tier table. The
   ceiling in force is the configured one clamped to
   `review_risk_tiers.HARD_MAX_AGENT_TIER`, so config narrows and can never
   widen (ADR-0027 condition (a)).
2. The tier is recomputed from authoritative evidence at decision time, not
   read back from the row. For the two bulk types this means loading the real
   change count; a count that cannot be resolved escalates to T2 and the item
   is left for a human.
3. Positive, object-specific evidence must support an APPROVE. An object type
   with no evidence resolver, or a proposal whose own confidence is absent or
   below the threshold, is abstained on rather than approved.
4. The evidence must be fresh: a recommendation older than
   `reviewer_agent_evidence_max_age_minutes` is re-derived, never consumed.
5. The agent never decides an item it proposed -- `maker != checker` is
   enforced by the shared decision path, and re-checked here so a
   misconfiguration fails loudly rather than at the database.
6. Suspension, process-wide or per-organization, stops everything
   (condition (c)) -- re-read before *each* commit, not only at batch entry.
   That re-read bounds the stop at one item per worker only at READ
   COMMITTED, so the batch refuses to start at any other isolation level
   rather than running without the bound (`refuse_unsupported_isolation`).
7. The unresolved audit-sample backlog must be inside its bound. Condition
   (b)'s safety argument is that humans read a 5% sample; an unread queue is
   not oversight, so the agent stops deciding rather than adding to it.
8. And the sample must be read *in time*: the oldest unread one must be
   younger than `reviewer_agent_max_sample_age_hours`. Guard 7 alone does not
   imply this -- a queue that stays just under the count bound while nobody
   opens its oldest item satisfies it forever, which is the shape unattended
   oversight actually decays into. Added 2026-09-12 for AR-11, whose exit
   condition recorded the time-to-verdict targets as reported but not
   enforced; `reviewer_agent_metrics.oldest_pending_hours` was already the
   number, and this is that number asked as a precondition.

Guards 2, 3, 4 and the per-item half of 6 were added on 2026-09-09 closing
AR-01 through AR-04 of `Docs/10-architecture/15-agent-architecture-critical-
review.md`. Before them: an elevated configured ceiling admitted T2/T3 types,
bulk items were tiered without their size, a *missing* confidence was the
most permissive input the rule had (it returned APPROVE), and a suspension
raised mid-batch did not stop the batch it was raised during.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aida.column_description_service import ORIGIN_MODEL_INFERRED
from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.governance_decision_contracts import TERMINAL_STATUS
from aida.governance_decision_service import (
    GovernanceDecisionRefused,
    decide_review,
    record_decision_audit,
    record_decision_outbox,
)
from aida.models import (
    AssetDescriptionDraft,
    BulkStewardshipOperation,
    ColumnDescriptionDraft,
    DataQualityIncident,
    GovernanceReview,
    MetadataEnrichmentProposal,
    MetadataTable,
    ModelImportBatch,
    QueryHistoryMetricCandidate,
    ReviewAuditSample,
    ReviewerAgentState,
)
from aida.review_risk_tiers import (
    TIER_T2,
    agent_decidable_object_types,
    effective_agent_ceiling,
    requires_size_evidence,
    risk_tier_for,
    tier_at_or_below,
)
from aida.security import SecurityContext

_log = structlog.get_logger(__name__)

_SAMPLING_FLOOR = 0.05
_FINGERPRINT_BUCKETS = float(2**32)

REASON_TIER_EXCEEDED = "reviewer_agent_tier_exceeded"
REASON_SUSPENDED = "reviewer_agent_suspended"
REASON_DISABLED = "reviewer_agent_disabled"
REASON_SELF_PROPOSED = "reviewer_agent_cannot_decide_own_proposal"
#: AR-11: the sample backlog has outgrown what humans are resolving, so the
#: oversight ADR-0027 condition (b) claims is not actually happening.
REASON_AUDIT_BACKLOG = "reviewer_agent_audit_backlog_exceeded"
#: AR-04: the batch is running at a transaction isolation level under which
#: the per-item suspension re-read cannot see the kill switch being thrown.
REASON_UNSUPPORTED_ISOLATION = "reviewer_agent_unsupported_isolation"

#: AR-11: the *oldest* unread sample has sat past its deadline. Distinct from
#: the count bound above because the two fail differently: a small queue that
#: nobody ever drains passes the count check forever, and is the shape
#: unattended oversight actually rots into.
REASON_SAMPLE_AGE = "reviewer_agent_sample_age_exceeded"

#: AR-04: the only transaction isolation level at which
#: `auto_decide_tier0_tier1`'s per-item suspension re-read can observe a
#: suspension committed *after* the batch's transaction began, and therefore
#: the only one at which the stop bound of one item per worker holds.
#:
#: PostgreSQL's REPEATABLE READ and SERIALIZABLE both serve every statement in
#: a transaction from the snapshot taken at its first statement, so a plain
#: re-read of `ReviewerAgentState` returns the pre-suspension row for the life
#: of the batch however often it is repeated. That is a property of the
#: isolation level, so it is enforced here rather than checked for.
SUPPORTED_ISOLATION_LEVEL = "READ COMMITTED"


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


#: Why a proposal carries no usable positive evidence. Stable codes, written
#: into `pre_review_evidence.evidence_reason` so an operator can tell "the
#: agent has no way to judge this type" from "this particular proposal is
#: missing its own confidence".
EVIDENCE_OK = "resolved"
EVIDENCE_NO_RESOLVER = "no_evidence_resolver_for_object_type"
EVIDENCE_SUBJECT_MISSING = "proposal_object_not_found"
EVIDENCE_VALUE_MISSING = "proposal_carries_no_confidence"
EVIDENCE_SIZE_UNRESOLVED = "bulk_change_count_unresolvable"
EVIDENCE_MODEL_INFERRED = "model_inferred_proposal_needs_a_human"


@dataclass(frozen=True, slots=True)
class ProposalEvidence:
    """Positive, object-specific evidence for one proposal -- or the reason
    there is none (AR-03).

    `confidence` is read from the *proposal's own row*, never from the review
    row the agent itself writes. That distinction is the defect this type
    exists to close: the previous implementation read `proposal_confidence`
    out of `GovernanceReview.pre_review_evidence`, which is written by
    pre-review and by nothing else, so on the only pass that ever read it the
    value was always `None` -- and `None` was the rule's most permissive
    input. `reviewer_agent_approve_confidence` was, in effect, dead
    configuration.
    """

    resolved: bool
    reason: str
    confidence: float | None = None
    source: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "evidence_reason": self.reason,
            "proposal_confidence": self.confidence,
            "evidence_source": self.source,
            **({"evidence_details": self.details} if self.details else {}),
        }


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _by_object_id(model: Any, attribute: str) -> Any:
    """A resolver for the common shape: `GovernanceReview.object_id` holds the
    proposal's own primary key, and the proposal carries a confidence."""

    async def resolve(session: AsyncSession, review: GovernanceReview) -> ProposalEvidence:
        try:
            subject_id = UUID(review.object_id)
        except (ValueError, AttributeError, TypeError):
            return ProposalEvidence(resolved=False, reason=EVIDENCE_SUBJECT_MISSING)
        row = await session.scalar(
            select(model).where(
                model.id == subject_id,
                model.organization_id == review.organization_id,
            )
        )
        if row is None:
            return ProposalEvidence(resolved=False, reason=EVIDENCE_SUBJECT_MISSING)
        return _confidence_evidence(row, model, attribute)

    return resolve


def _confidence_evidence(row: Any, model: Any, attribute: str) -> ProposalEvidence:
    value = _as_float(getattr(row, attribute, None))
    source = f"{model.__tablename__}.{attribute}"
    if value is None:
        return ProposalEvidence(
            resolved=False, reason=EVIDENCE_VALUE_MISSING, source=source
        )
    return ProposalEvidence(
        resolved=True, reason=EVIDENCE_OK, confidence=value, source=source
    )


async def _bulk_size_evidence(
    session: AsyncSession, review: GovernanceReview
) -> ProposalEvidence:
    """Positive evidence for the two bulk types is the *verified* size.

    A bulk operation carries no confidence -- a human chose its subjects
    explicitly. What makes it safe for an agent to decide is that the platform
    can still see how many subjects there are and confirm the number is inside
    the threshold that decided the item needed review at all. An unresolvable
    count is not evidence of smallness (the same rule `risk_tier_for` applies
    to an unparseable one), so it abstains here and escalates the tier in
    `_sized_risk_tier`.
    """
    count = await _authoritative_change_count(session, review)
    if count is None:
        return ProposalEvidence(resolved=False, reason=EVIDENCE_SIZE_UNRESOLVED)
    return ProposalEvidence(
        resolved=True,
        reason=EVIDENCE_OK,
        # A verified count is a categorical fact, not a graded one. 1.0 says
        # "this evidence is present and checked", and the tier -- not this
        # number -- is what bounds the size.
        confidence=1.0,
        source=f"{review.object_type.lower()}.change_count",
        details={"change_count": count},
    )


async def _column_description_draft_evidence(
    session: AsyncSession, review: GovernanceReview
) -> ProposalEvidence:
    """Evidence for a column draft -- unless a model wrote it.

    An evidence draft's score measures catalog facts, so agreeing with it is at
    least a check against something. A model draft's score is the model's
    capped confidence in its own guess; an agent agreeing with that adds nothing
    a person has not been asked to supply. So the agent abstains on every
    MODEL_INFERRED draft, edited or not, whatever `reviewer_agent_approve_confidence`
    says -- the cap already sits below the default, and this does not rely on it.
    """
    try:
        subject_id = UUID(review.object_id)
    except (ValueError, AttributeError, TypeError):
        return ProposalEvidence(resolved=False, reason=EVIDENCE_SUBJECT_MISSING)
    row = await session.scalar(
        select(ColumnDescriptionDraft).where(
            ColumnDescriptionDraft.id == subject_id,
            ColumnDescriptionDraft.organization_id == review.organization_id,
        )
    )
    if row is None:
        return ProposalEvidence(resolved=False, reason=EVIDENCE_SUBJECT_MISSING)
    origin = str((row.evidence or {}).get("origin") or "")
    if origin.startswith(ORIGIN_MODEL_INFERRED):
        return ProposalEvidence(
            resolved=False,
            reason=EVIDENCE_MODEL_INFERRED,
            source="column_description_draft.evidence.origin",
            details={"origin": origin},
        )
    return _confidence_evidence(row, ColumnDescriptionDraft, "overall_score")


#: `semantic_inference.enrich_with_optional_model` marks a proposal the model
#: wrote with this engine type; the rules engine's are `RULES`.
_MODEL_ENRICHMENT_ENGINE = "LLM_ASSISTED"


async def _metadata_enrichment_evidence(
    session: AsyncSession, review: GovernanceReview
) -> ProposalEvidence:
    """Evidence for an enrichment proposal -- unless a model inferred it.

    The rule `_column_description_draft_evidence` applies to a model-written
    column draft, for the same reason. On the rules path `confidence` is a fixed
    function of the table's structure (0.82 with a primary key and a domain
    keyword, else 0.66). On the model path it is what the model said about its
    own answer, bounded to [0, 1] and checked by nothing: the false-approval
    benchmark's model twin filed a customer table under Payments, claimed 0.95,
    and was approved (AR-03).
    """
    try:
        subject_id = UUID(review.object_id)
    except (ValueError, AttributeError, TypeError):
        return ProposalEvidence(resolved=False, reason=EVIDENCE_SUBJECT_MISSING)
    row = await session.scalar(
        select(MetadataEnrichmentProposal).where(
            MetadataEnrichmentProposal.id == subject_id,
            MetadataEnrichmentProposal.organization_id == review.organization_id,
        )
    )
    if row is None:
        return ProposalEvidence(resolved=False, reason=EVIDENCE_SUBJECT_MISSING)
    if row.engine_type == _MODEL_ENRICHMENT_ENGINE:
        return ProposalEvidence(
            resolved=False,
            reason=EVIDENCE_MODEL_INFERRED,
            source="metadata_enrichment_proposal.engine_type",
            details={"engine_type": row.engine_type},
        )
    return _confidence_evidence(row, MetadataEnrichmentProposal, "confidence")


#: Object type -> the resolver that produces positive evidence for it.
#:
#: A type absent from this table is one the agent has no object-specific way
#: to judge, so it abstains (`EVIDENCE_NO_RESOLVER`) rather than falling back
#: to "nothing argued against it". `TERM_SEMANTIC_BINDING` and
#: `ASSET_DOCUMENTATION_VERSION` are deliberately absent: both are human
#: steward assertions with no computed score, and an agent agreeing with a
#: human's unscored assertion adds no independent check.
#:
#: `DOCUMENT_CLAIM` is absent for a related reason. Its `confidence` is the
#: certainty of the structural *name match* -- 1.0 for any data-dictionary row
#: whose table and column names matched -- which says the claim is about the
#: right column and nothing about whether its description is true. Nothing in
#: the platform scores that. Read as evidence, it approved every matched row of
#: any uploaded dictionary, wrong descriptions included (AR-03).
#:
#: `GLOSSARY_LINK_PROPOSAL` is absent for the same reason. Its producer emits
#: exactly two confidences -- 1.0 when an annotation's business name equals a
#: term's display name, 0.92 for any other label pair -- and both clear the
#: approve threshold, so the number cannot separate a right link from a wrong
#: one: a staging table whose name stem is "revenue" links to the Revenue term
#: at 1.0. `tests/test_ar03_false_approval_benchmark.py` measures both.
_EVIDENCE_RESOLVERS: dict[str, Any] = {
    "ASSET_DESCRIPTION_DRAFT": _by_object_id(AssetDescriptionDraft, "overall_score"),
    "COLUMN_DESCRIPTION_DRAFT": _column_description_draft_evidence,
    "METADATA_ENRICHMENT_PROPOSAL": _metadata_enrichment_evidence,
    "QUERY_HISTORY_METRIC_CANDIDATE": _by_object_id(QueryHistoryMetricCandidate, "confidence"),
    "BULK_STEWARDSHIP_OPERATION": _bulk_size_evidence,
    "MODEL_IMPORT_BATCH": _bulk_size_evidence,
}


async def _resolve_evidence(
    session: AsyncSession, review: GovernanceReview
) -> ProposalEvidence:
    resolver = _EVIDENCE_RESOLVERS.get(review.object_type)
    if resolver is None:
        return ProposalEvidence(resolved=False, reason=EVIDENCE_NO_RESOLVER)
    evidence: ProposalEvidence = await resolver(session, review)
    return evidence


async def _authoritative_change_count(
    session: AsyncSession, review: GovernanceReview
) -> int | None:
    """How many things this bulk review would actually change (AR-02).

    Read from the row the platform itself wrote when the operation was
    submitted, not from anything the proposal supplied. `None` means the
    count could not be established -- the operation row is missing, or its
    subject list is not a list -- which callers must treat as "too big to
    wave through", never as zero.
    """
    if review.object_type == "MODEL_IMPORT_BATCH":
        batch = await session.scalar(
            select(ModelImportBatch).where(
                ModelImportBatch.governance_review_id == review.id,
                ModelImportBatch.organization_id == review.organization_id,
            )
        )
        if batch is None:
            return None
        return int(batch.change_count)
    if review.object_type == "BULK_STEWARDSHIP_OPERATION":
        operation = await session.scalar(
            select(BulkStewardshipOperation).where(
                BulkStewardshipOperation.governance_review_id == review.id,
                BulkStewardshipOperation.organization_id == review.organization_id,
            )
        )
        if operation is None or not isinstance(operation.subject_ids, list):
            return None
        return len(operation.subject_ids)
    return None


async def _reverses_operation_id(
    session: AsyncSession, review: GovernanceReview
) -> UUID | None:
    """The operation this review's bulk operation undoes, if it undoes one.

    Read from the row rather than from the review's payload for the same
    reason `_authoritative_change_count` is (AR-02): the tier must be
    recomputed from authoritative evidence at decision time, and a flag that
    decides whether an agent may act on an item is exactly the flag a caller
    must not be able to supply.
    """
    if review.object_type != "BULK_STEWARDSHIP_OPERATION":
        return None
    return await session.scalar(
        select(BulkStewardshipOperation.reverses_operation_id).where(
            BulkStewardshipOperation.governance_review_id == review.id,
            BulkStewardshipOperation.organization_id == review.organization_id,
        )
    )


async def _sized_risk_tier(
    session: AsyncSession, review: GovernanceReview, *, governance_threshold: int
) -> tuple[str, dict[str, Any]]:
    """The tier for one item, with size resolved when size is what decides it.

    `risk_tier_for(object_type)` with no payload answers T1 for both bulk
    types, which reads as "small" when it actually means "not measured" --
    AR-02. This resolves the authoritative count first and escalates to T2
    when it cannot, so a 900-change workbook can no longer sit at T1 because
    nobody looked.
    """
    if not requires_size_evidence(review.object_type):
        return risk_tier_for(review.object_type), {}
    count = await _authoritative_change_count(session, review)
    if count is None:
        return TIER_T2, {"size_evidence": EVIDENCE_SIZE_UNRESOLVED}
    # AR-11: a bulk operation raised to undo an applied one is T2 whatever its
    # size, so the flag has to reach `risk_tier_for` alongside the count --
    # otherwise a three-table reversal tiers as T1 on size alone and lands
    # back inside the agent's ceiling.
    reverses = await _reverses_operation_id(session, review)
    tier = risk_tier_for(
        review.object_type,
        {
            "item_count": count,
            "governance_threshold": governance_threshold,
            "reverses_operation_id": reverses,
        },
    )
    evidence: dict[str, Any] = {
        "size_evidence": EVIDENCE_OK,
        "change_count": count,
        "governance_threshold": governance_threshold,
    }
    if reverses is not None:
        evidence["reverses_operation_id"] = str(reverses)
    return tier, evidence


def _recommendation(
    *,
    risk_tier: str,
    max_tier: str,
    negative_hits: int,
    open_incidents: int,
    evidence: ProposalEvidence,
    approve_confidence: float,
) -> str:
    """The deterministic rule, stated once so it can be quoted in an audit.

    * A prior rejection of the identical proposal -> REJECT. The platform has
      already been told this is wrong; re-proposing it does not make it right.
    * APPROVE requires all of: the item inside the tier ceiling in force, no
      open quality incident on the asset, **and positive object-specific
      evidence at or above the approve threshold**.
    * Everything else -> NONE, which means "a human should look".

    The third clause is the AR-03 fix. The rule used to read "nothing argues
    against it", and absence of contrary evidence is not a reason to believe
    something is right -- least of all for the types where the platform holds
    no computed judgement at all. NONE is now the default for those, which is
    what "deliberately reluctant" was always supposed to mean.
    """
    if negative_hits > 0:
        return "REJECT"
    if not tier_at_or_below(risk_tier, max_tier):
        return "NONE"
    if open_incidents > 0:
        return "NONE"
    if not evidence.resolved or evidence.confidence is None:
        return "NONE"
    return "APPROVE" if evidence.confidence >= approve_confidence else "NONE"


@dataclass(frozen=True, slots=True)
class _Assessment:
    """One full evaluation of one review item, from live evidence."""

    risk_tier: str
    recommendation: str
    confidence: float | None
    evidence: dict[str, Any]


async def _assess(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    settings: Settings,
    ceiling: str,
) -> _Assessment:
    """Evaluate one item against evidence read *now*.

    Deliberately shared by `pre_review_pending` and `auto_decide_tier0_tier1`
    rather than computed once and stored (AR-04). The stored recommendation
    is what makes an item *eligible* for a decision; what the decision rests
    on is this function's answer at commit time. A rejection recorded, an
    incident opened, or a workbook grown between the two passes therefore
    changes the outcome instead of being invisible to it.
    """
    tier, size_evidence = await _sized_risk_tier(
        session, review, governance_threshold=settings.bulk_governance_threshold
    )
    negative_hits = await _negative_knowledge_hits(session, review)
    open_incidents = await _open_incident_count(session, review)
    proposal_evidence = await _resolve_evidence(session, review)
    recommendation = _recommendation(
        risk_tier=tier,
        max_tier=ceiling,
        negative_hits=negative_hits,
        open_incidents=open_incidents,
        evidence=proposal_evidence,
        approve_confidence=settings.reviewer_agent_approve_confidence,
    )
    evidence: dict[str, Any] = {
        "risk_tier": tier,
        "negative_knowledge_hits": negative_hits,
        "open_quality_incidents": open_incidents,
        "max_tier": ceiling,
        "configured_max_tier": settings.reviewer_agent_max_tier,
        # True when configuration asked for a ceiling the hard limit refused.
        # Surfaced rather than swallowed so a misconfiguration is visible in
        # the audit trail instead of merely being ineffective (AR-01).
        "max_tier_clamped": ceiling != settings.reviewer_agent_max_tier,
        "approve_confidence": settings.reviewer_agent_approve_confidence,
        "rule_version": 2,
        **proposal_evidence.as_json(),
        **size_evidence,
    }
    return _Assessment(
        risk_tier=tier,
        recommendation=recommendation,
        confidence=proposal_evidence.confidence,
        evidence=evidence,
    )


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
    ceiling = effective_agent_ceiling(settings.reviewer_agent_max_tier)
    outcomes: list[PreReviewOutcome] = []
    for review in rows:
        assessment = await _assess(session, review, settings=settings, ceiling=ceiling)
        review.risk_tier = assessment.risk_tier
        review.pre_review_recommendation = assessment.recommendation
        review.pre_review_confidence = assessment.confidence
        review.pre_review_evidence = assessment.evidence
        review.pre_reviewed_at = moment
        review.pre_reviewed_by = settings.reviewer_agent_principal_id
        outcomes.append(
            PreReviewOutcome(
                review_id=review.id,
                object_type=review.object_type,
                risk_tier=assessment.risk_tier,
                recommendation=assessment.recommendation,
                confidence=assessment.confidence,
                evidence=assessment.evidence,
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


async def unresolved_audit_samples(session: AsyncSession, organization_id: UUID) -> int:
    """How many of this agent's decisions are sampled and still unread (AR-11).

    ADR-0027 condition (b) is the argument that a 5% sample makes unattended
    decisions acceptable. That argument is about humans *reading* the sample,
    and nothing checked that they were: the ledger could grow without bound
    while the agent kept deciding, and the safety case would degrade with no
    signal. This is the number that makes the degradation visible, and
    `auto_decide_tier0_tier1` refuses on it.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(ReviewAuditSample)
        .where(
            ReviewAuditSample.organization_id == organization_id,
            ReviewAuditSample.human_outcome == "PENDING",
        )
    )
    return int(count or 0)


async def oldest_unresolved_sample_age_hours(
    session: AsyncSession, organization_id: UUID, *, now: datetime
) -> float | None:
    """How long the longest-unread sample has been waiting, or `None` (AR-11).

    The companion to `unresolved_audit_samples`, and not derivable from it:
    the count answers "how much is outstanding", this answers "for how long",
    and only the second one notices a small queue that never drains. Forty-nine
    samples untouched since February sit inside the default count bound
    indefinitely while ADR-0027 condition (b)'s claim that humans read the
    sample has been false for months.

    `reviewer_agent_metrics` already reports this number as
    `oldest_pending_hours`; this is the same quantity asked as a precondition
    rather than a statistic. Computed in Python from `min(sampled_at)` rather
    than as a SQL interval because SQLite and PostgreSQL disagree about
    date arithmetic, and because the stored value comes back naive on SQLite
    -- the same `.replace(tzinfo=UTC)` normalisation the metrics module does.
    """
    oldest = await session.scalar(
        select(func.min(ReviewAuditSample.sampled_at)).where(
            ReviewAuditSample.organization_id == organization_id,
            ReviewAuditSample.human_outcome == "PENDING",
        )
    )
    if oldest is None:
        return None
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return round((now - oldest).total_seconds() / 3600, 2)


def record_audit_backlog_refusal(
    session: AsyncSession,
    organization_id: UUID,
    *,
    context: SecurityContext,
    unresolved: int,
    limit: int,
) -> None:
    """Make the backlog refusal an event, not only a 409 (AR-11).

    `auto_decide_tier0_tier1` raises before it writes anything and its caller
    rolls back, so the agent stopping for want of human attention used to
    leave no trace but a response code. The caller records it after the
    rollback: an audit row naming who asked, and an outbox event that a
    notification or a dashboard can act on.
    """
    details = {"unresolved_samples": unresolved, "max_unresolved_samples": limit}
    _record_oversight_denial(
        session, organization_id, context=context, reason=REASON_AUDIT_BACKLOG, details=details
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="reviewer_agent_state",
        aggregate_id=str(organization_id),
        event_type="reviewer_agent.audit_backlog_exceeded.v1",
        payload=details,
    )


async def transaction_isolation_level(session: AsyncSession) -> str | None:
    """This transaction's isolation level, upper-cased, or `None` off PostgreSQL.

    `SHOW transaction_isolation` reports the level actually in force for the
    current transaction, which is what matters: the level can come from the
    engine, from the connection, or from a `SET TRANSACTION` the caller issued,
    and reading configuration back would only re-state what was asked for
    rather than what was granted.

    Returns `None` on any other dialect. The question is about PostgreSQL's
    MVCC snapshots; SQLite, where the rest of the suite runs, has no
    equivalent and serialises writers outright.
    """
    connection = await session.connection()
    if connection.dialect.name != "postgresql":
        return None
    level = await session.scalar(text("SHOW transaction_isolation"))
    return str(level).strip().upper() if level is not None else None


async def refuse_unsupported_isolation(session: AsyncSession) -> None:
    """AR-04: enforce the isolation level the stop bound depends on.

    The per-item suspension re-read in `auto_decide_tier0_tier1` bounds the
    stop at one item per worker *only* at READ COMMITTED, where a statement
    sees rows committed since the transaction began. Under REPEATABLE READ or
    SERIALIZABLE the re-read is served from the batch's own snapshot, the
    suspension stays invisible for the life of the transaction, and the batch
    runs to its `limit` -- the kill switch does not bind.

    Until 2026-09-12 that was a comment in Guard 0 saying the code should be
    run at READ COMMITTED. Nothing checked, so a deployment that set a
    stricter default -- the direction an operator reaches for when they want
    *more* safety -- silently bought an unbounded agent. The precondition is
    now a refusal: the agent declines to run rather than run without its stop
    bound, with a named reason code the API surfaces as a 409.

    Why enforcement rather than making the bound hold at REPEATABLE READ:
    no check *inside* the batch's transaction can defeat its snapshot. The
    two mechanisms that could are both worse than refusing. Re-reading the
    state on a separate connection turns one batch transaction into 1+N, and
    the agent is handed a session rather than an engine. A locking re-read
    (`SELECT ... FOR SHARE`/`FOR UPDATE`) makes every worker hold a lock on
    the state row that the suspending `UPDATE` must then wait for -- the kill
    switch would queue behind the batches it exists to stop, and with a
    `lock_timeout` set it would fail outright. A kill switch delayed by its
    own target is the wrong trade.
    """
    level = await transaction_isolation_level(session)
    if level is None or level == SUPPORTED_ISOLATION_LEVEL:
        return
    _log.error(
        "reviewer_agent_unsupported_isolation",
        reason=REASON_UNSUPPORTED_ISOLATION,
        isolation_level=level,
        supported_isolation_level=SUPPORTED_ISOLATION_LEVEL,
    )
    raise ReviewerAgentUnavailable(REASON_UNSUPPORTED_ISOLATION)
def record_sample_age_refusal(
    session: AsyncSession,
    organization_id: UUID,
    *,
    context: SecurityContext,
    oldest_hours: float,
    limit_hours: int,
) -> None:
    """The age half of the same refusal (AR-11).

    Deliberately a *different* outbox event from the count bound rather than
    the same event carrying a reason field. A consumer's correct response
    differs: a count breach says samples arrive faster than they are read and
    wants more reviewers on the queue, an age breach says one item has been
    skipped and wants someone to open that item. Collapsing them would make
    the two indistinguishable to exactly the dashboard that has to tell them
    apart.
    """
    details = {"oldest_pending_hours": oldest_hours, "max_sample_age_hours": limit_hours}
    _record_oversight_denial(
        session, organization_id, context=context, reason=REASON_SAMPLE_AGE, details=details
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="reviewer_agent_state",
        aggregate_id=str(organization_id),
        event_type="reviewer_agent.sample_age_exceeded.v1",
        payload=details,
    )


def _record_oversight_denial(
    session: AsyncSession,
    organization_id: UUID,
    *,
    context: SecurityContext,
    reason: str,
    details: dict[str, Any],
) -> None:
    """The DENIED audit row shared by both oversight bounds.

    One funnel deliberately: both refusals are the same governance fact --
    the agent's licence to decide lapsed because condition (b)'s oversight
    stopped happening -- so an auditor reconstructing why the agent went
    quiet finds them under one `action` and one `resource_type`, with
    `details["reason"]` naming which bound tripped.

    The *outbox* call stays at each caller with its event name written as a
    literal, rather than being folded in here too. `tests/
    test_event_catalog_gate.py` resolves `event_type=` statically, and an
    event handed in as a parameter is invisible to it -- the catalog row
    would stop being checked against a real emitter, which is the one thing
    that gate exists to prevent.
    """
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="reviewer_agent.run",
        resource_type="reviewer_agent_state",
        resource_id=str(organization_id),
        outcome="DENIED",
        correlation_id=get_correlation_id(),
        details={"reason": reason, **details},
    )


async def organization_suspended(session: AsyncSession, organization_id: UUID) -> bool:
    state = await session.scalar(
        select(ReviewerAgentState)
        .where(ReviewerAgentState.organization_id == organization_id)
        .execution_options(populate_existing=True)
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

    Decisions go through `governance_decision_service.decide_review`, the
    single application service the human endpoint, the bulk endpoint and the
    sample-review endpoint all call, so every object type's side effects,
    audit row and outbox event are identical to a human decision. Nothing
    about the decision path is special-cased for the agent except who is
    recorded as deciding it.

    F05 applies here too, and it is the reason the batch below does not
    abort on a contended item: the service claims each review out of PENDING
    with a compare-and-set, so an item a human decided between this batch's
    read and its claim is refused (`GovernanceDecisionRefused`), rolled back
    with its own savepoint, and left out of the returned outcomes -- the
    human's decision stands, and the agent records neither a second audit row
    nor a second outbox event for it. That also means this pass deliberately
    takes no `FOR UPDATE` locks: a lock order different from the bulk
    endpoint's would create the deadlock the compare-and-set makes
    unnecessary.
    """
    if not settings.reviewer_agent_enabled:
        raise ReviewerAgentUnavailable(REASON_DISABLED)
    if settings.reviewer_agent_suspended or await organization_suspended(session, organization_id):
        raise ReviewerAgentUnavailable(REASON_SUSPENDED)
    # AR-04: refuse to run at all at an isolation level where Guard 0 below
    # cannot see the kill switch being thrown. Checked after the suspension
    # checks so an agent that is *already* suspended reports that, which is
    # the more useful answer and is correct at every isolation level -- the
    # snapshot a transaction opens with already contains an earlier commit.
    await refuse_unsupported_isolation(session)
    # AR-11: the agent's licence to decide is contingent on humans keeping up
    # with the sample of what it already decided. A backlog past the configured
    # bound stops new decisions rather than adding to it -- the alternative is
    # an oversight claim that quietly stops being true.
    backlog_limit = settings.reviewer_agent_max_unresolved_samples
    if backlog_limit and await unresolved_audit_samples(session, organization_id) >= backlog_limit:
        raise ReviewerAgentUnavailable(REASON_AUDIT_BACKLOG)

    moment = now or datetime.now(UTC)
    # AR-11: and the sample must be read *in time*. The count bound above is
    # necessary but not sufficient -- a queue that stays just under it while
    # nobody opens the oldest item passes it forever, which is the shape
    # unattended oversight actually decays into. Checked after `moment` is
    # fixed so the age is measured against the same clock the run uses.
    age_limit = settings.reviewer_agent_max_sample_age_hours
    if age_limit:
        oldest_hours = await oldest_unresolved_sample_age_hours(
            session, organization_id, now=moment
        )
        if oldest_hours is not None and oldest_hours >= age_limit:
            raise ReviewerAgentUnavailable(REASON_SAMPLE_AGE)

    # AR-01: the configured value is clamped before anything derives from it,
    # so a T2/T3 ceiling narrows nothing and widens nothing.
    ceiling = effective_agent_ceiling(settings.reviewer_agent_max_tier)
    allowlist = agent_decidable_object_types(ceiling)
    agent_principal = settings.reviewer_agent_principal_id
    context = _agent_context(organization_id, agent_principal)
    staleness = timedelta(minutes=settings.reviewer_agent_evidence_max_age_minutes)

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
        # Guard 0 (AR-04): a suspension raised while this batch is running
        # stops the batch it was raised during, not merely the next one.
        #
        # Bound on the stop: one item per worker, and therefore N items for N
        # workers drawing on one organization's queue -- each worker can be
        # inside `decide_review` when the suspension commits, and none can
        # begin a second item after it. The bound is per worker because
        # workers share no transaction; it does not degrade with queue depth
        # or with how many of them lose the claim race for a given row.
        #
        # It holds because the re-read below sees rows committed since this
        # transaction began, which is true at READ COMMITTED and false at
        # REPEATABLE READ and SERIALIZABLE. That is a property of the
        # isolation level rather than of this check, so it is a precondition
        # enforced at entry (`refuse_unsupported_isolation`) rather than a
        # hope recorded here. Measured on real PostgreSQL, at both levels, by
        # `tests/test_reviewer_agent_postgres_suspension.py`.
        if await organization_suspended(session, organization_id):
            raise ReviewerAgentUnavailable(REASON_SUSPENDED)
        # Guard 1 (AR-04): stale evidence is not evidence. An item pre-reviewed
        # long enough ago that the world may have moved is left for the next
        # pre-review pass to re-derive rather than decided on what it said then.
        if review.pre_reviewed_at is None:
            continue
        pre_reviewed_at = review.pre_reviewed_at
        if pre_reviewed_at.tzinfo is None:
            pre_reviewed_at = pre_reviewed_at.replace(tzinfo=UTC)
        if moment - pre_reviewed_at > staleness:
            continue
        # Guard 2 (AR-02/AR-03/AR-04): re-derive from live evidence. The
        # stored recommendation selected this row; it does not decide it. A
        # bulk item is re-sized here, so a workbook that grew past the
        # threshold escalates to T2 and drops out below.
        assessment = await _assess(session, review, settings=settings, ceiling=ceiling)
        if assessment.recommendation not in ("APPROVE", "REJECT"):
            continue
        # A fresh verdict that contradicts the stored one is a signal that the
        # evidence moved, and a contradiction is exactly the case a human
        # should see. Abstain rather than act on either version.
        if assessment.recommendation != review.pre_review_recommendation:
            continue
        tier = assessment.risk_tier
        # Guard 3: the allowlist is derived from the tier table and the
        # clamped ceiling, so an object type classified T2/T3 is refused
        # whatever configuration claimed.
        if review.object_type not in allowlist or not tier_at_or_below(tier, ceiling):
            # Named, not silent: an operator asking why the agent passed over an
            # item gets the same vocabulary the refusal paths above raise, rather
            # than an absence they have to reconstruct from the tier table.
            _log.info(
                "reviewer_agent_item_skipped",
                reason=REASON_TIER_EXCEEDED,
                review_id=str(review.id),
                object_type=review.object_type,
                risk_tier=tier,
                ceiling=ceiling,
            )
            continue
        # Guard 4: never decide our own proposal. The shared decision path
        # enforces this too; re-checking here means a misconfigured
        # principal fails as a skip rather than as a 409 mid-batch.
        if review.requested_by == agent_principal:
            _log.info(
                "reviewer_agent_item_skipped",
                reason=REASON_SELF_PROPOSED,
                review_id=str(review.id),
                object_type=review.object_type,
            )
            continue

        # The *verdict* ("APPROVE"/"REJECT") is what the decision service
        # takes; `decision` below is the terminal status it writes, which is
        # what this function has always reported and what `ReviewAuditSample`
        # stores. Deriving one from the other through `TERMINAL_STATUS`
        # rather than restating both is deliberate: passing the past-tense
        # form where a verdict was expected used to be silently read as a
        # rejection.
        verdict = assessment.recommendation
        decision = TERMINAL_STATUS[verdict]
        reason = (
            f"reviewer agent ({agent_principal}), rule v2, tier {tier}: {verdict}"
        )
        try:
            async with session.begin_nested():
                effect = await decide_review(
                    session,
                    review,
                    decision=verdict,
                    reason=reason,
                    context=context,
                    now=moment,
                )
                record_decision_outbox(session, review, effect)
        except GovernanceDecisionRefused:
            # Another checker (human or a parallel agent pass) reached this
            # review first, or it is not the agent's to decide. Its savepoint
            # has unwound; leave the winner's decision alone and move on.
            continue
        except HTTPException:
            # The target object refused this decision (it moved on, a gate
            # did not pass). Same treatment: this item's writes are rolled
            # back with its savepoint and the batch continues.
            continue
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
        record_decision_audit(
            session,
            review,
            context=context,
            action="reviewer_agent.decide",
            details={
                "decision": decision,
                "object_type": review.object_type,
                "risk_tier": tier,
                "sampled_for_audit": is_sampled,
                "rule_version": 2,
                "evidence_reason": assessment.evidence.get("evidence_reason"),
                "proposal_confidence": assessment.confidence,
                "revalidated_at_decision": True,
            },
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
