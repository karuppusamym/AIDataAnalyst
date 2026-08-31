"""SM-4: metric suggestions from approved annotations.

Module 07 (semantic layer) already versions metrics with maker-checker
publication (`SemanticMetricVersion` / `semantic_api.create_metric_version`
+ `submit_semantic_model_for_review`), but nothing proposes a metric on its
own. This module closes that gap the same way GL-8 (`GlossaryLinkProposal`,
`stewardship_api.generate_glossary_link_proposals`) infers term links from
approved `MetadataBusinessAnnotation` evidence, and the same way GL-9
(`asset_description_service.score_evidence` / `ensure_reviewable`)
evidence-scores a draft before it may ever reach the shared
`governance_review` queue:

1. Evidence is real and already in this database: an *approved* business
   annotation on a table (proof a human steward signed off on that table's
   business meaning) plus a numeric column on that table whose name matches
   a fixed, deterministic measure-keyword vocabulary (`MEASURE_KEYWORDS`) --
   metadata only, no source data is ever read (ADR-0014).
2. `score_evidence` is a pure, deterministic function of that evidence --
   more/stronger corroboration (an exact keyword match, a fact-shaped table,
   a monetary column type, an already-approved glossary-term binding on the
   table, the annotation's own description naming the measure) always scores
   at or above weaker evidence for the same dimension.
3. `ensure_reviewable` blocks a below-threshold proposal with 422 before any
   `GovernanceReview` row is ever constructed -- see
   `metric_suggestion_api.submit_metric_suggestion_proposal`.
4. `apply_metric_suggestion_proposal` is the only code path that publishes a
   proposal: called exclusively from `semantic_api.decide_governance_review`
   (via `_apply_governance_review_decision`), after that function's shared
   maker-checker guard (self-approval denied) has already run. It creates a
   real `SemanticMetric` + `SemanticMetricVersion`, published immediately.

Why a dedicated `SemanticModelVersion` per approval, not a shared draft:
`SemanticMetricVersion.semantic_model_version_id` is a mandatory FK with a
`(semantic_model_version_id, metric_id)` uniqueness constraint, so every
metric version must live inside *some* model version. Consumption
(`retrieval.hybrid_retrieve`, `intelligence_api`) filters purely on
`SemanticMetricVersion.status == "PUBLISHED"` -- it never inspects the
parent model version's own status -- so bundling an inferred metric into an
unrelated in-progress model draft would either block on that draft's own
review or silently couple two unrelated approvals. Instead, approval creates
a small model version scoped to exactly this one metric, publishes it
alongside the metric version in the same decision, and records the
provenance on `SemanticMetricProposal.published_metric_version_id` --
mirroring how `apply_asset_description_draft` creates an
`AssetDocumentation` container on demand rather than requiring one to
pre-exist.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    SemanticMetric,
    SemanticMetricProposal,
    SemanticMetricVersion,
    SemanticModelVersion,
)

Aggregation = Literal["SUM", "COUNT", "AVG", "MIN", "MAX"]

# Deterministic, bounded measure-keyword vocabulary. Ordered longest-first so
# a more specific keyword (e.g. "quantity") is preferred over a shorter one
# that might also match (e.g. none here overlap, but new entries must keep
# this ordering property -- see `_match_measure_keyword`).
MEASURE_KEYWORDS: tuple[tuple[str, Aggregation], ...] = (
    ("quantity", "SUM"),
    ("revenue", "SUM"),
    ("balance", "SUM"),
    ("amount", "SUM"),
    ("volume", "SUM"),
    ("total", "SUM"),
    ("price", "AVG"),
    ("count", "COUNT"),
    ("value", "SUM"),
    ("cost", "SUM"),
    ("fee", "SUM"),
    ("qty", "SUM"),
)
_MEASURE_KEYWORDS_BY_LENGTH = tuple(
    sorted(MEASURE_KEYWORDS, key=lambda pair: len(pair[0]), reverse=True)
)

MatchKind = Literal["EXACT", "SUFFIX", "CONTAINS"]

# Table roles module 07's own deterministic inference (`semantic_inference.
# _table_role`) assigns to fact-shaped tables -- where a measure naturally
# lives. Used only as corroborating evidence, never a hard filter.
_FACT_LIKE_TABLE_ROLES = frozenset({"FACT", "TRANSACTION", "EVENT", "SNAPSHOT"})

# Physical-type prefixes treated as numeric (candidate for a measure at all)
# and, within that, the stricter monetary-shaped subset used as corroborating
# style/accuracy evidence. Matched case-insensitively against the start of
# `MetadataColumn.physical_type` (which may carry precision, e.g. "NUMERIC(18,2)").
_NUMERIC_TYPE_PREFIXES = (
    "NUMERIC",
    "DECIMAL",
    "INTEGER",
    "INT",
    "BIGINT",
    "SMALLINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "MONEY",
)
_MONETARY_TYPE_PREFIXES = ("NUMERIC", "DECIMAL", "MONEY")

# Below this overall score a proposal carries too little evidence to be worth
# an independent reviewer's time -- mirrors GL-9's
# `asset_description_service.MINIMUM_EVIDENCE_FOR_REVIEW`.
MINIMUM_EVIDENCE_FOR_METRIC_REVIEW = 0.4


def is_numeric_physical_type(physical_type: str) -> bool:
    normalized = physical_type.strip().upper()
    return normalized.startswith(_NUMERIC_TYPE_PREFIXES)


def is_monetary_physical_type(physical_type: str) -> bool:
    normalized = physical_type.strip().upper()
    return normalized.startswith(_MONETARY_TYPE_PREFIXES)


def match_measure_keyword(column_name: str) -> tuple[str, Aggregation, MatchKind] | None:
    """Deterministically match a column name against `MEASURE_KEYWORDS`.

    Returns the most specific (longest) matching keyword, its suggested
    aggregation, and how strongly the name matched it -- `EXACT` (the whole,
    normalized column name), `SUFFIX` (a `_keyword`/`keyword` word boundary),
    or `CONTAINS` (the keyword appears anywhere else in the name). Callers
    that only want strong evidence should treat `CONTAINS` as too weak to
    act on -- see `metric_suggestion_api`'s generation pass, which never
    creates a proposal for a bare `CONTAINS` match.
    """
    normalized = column_name.strip().casefold()
    for keyword, aggregation in _MEASURE_KEYWORDS_BY_LENGTH:
        if normalized == keyword:
            return keyword, aggregation, "EXACT"
        if normalized.endswith(f"_{keyword}") or normalized.startswith(f"{keyword}_"):
            return keyword, aggregation, "SUFFIX"
        if keyword in normalized:
            return keyword, aggregation, "CONTAINS"
    return None


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    """Value-free, DB-derived signals about one candidate (table, column)
    pair. No source row values -- metadata only (ADR-0014)."""

    table_id: UUID
    table_name: str
    project_id: UUID
    business_annotation_id: UUID
    business_name: str
    business_description: str
    table_role: str
    grain_statement: str
    column_id: UUID
    column_name: str
    physical_type: str
    nullable: bool
    matched_keyword: str
    suggested_aggregation: Aggregation
    match_kind: MatchKind
    bound_term_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetricConfidenceBreakdown:
    """A composite score with an explainable per-dimension breakdown.

    Every sub-score is a deterministic function of `MetricEvidence` -- more
    corroborating evidence always scores at or above less evidence for the
    same dimension. There is no learned weight and no external call.
    """

    accuracy: float
    clarity: float
    style: float
    completeness: float
    overall: float

    def as_dict(self) -> dict[str, float]:
        return {
            "accuracy": self.accuracy,
            "clarity": self.clarity,
            "style": self.style,
            "completeness": self.completeness,
            "overall": self.overall,
        }


def _description_mentions(evidence: MetricEvidence) -> bool:
    description = evidence.business_description.casefold()
    return evidence.matched_keyword in description or (
        evidence.column_name.casefold() in description
    )


def score_evidence(evidence: MetricEvidence) -> MetricConfidenceBreakdown:
    """Score a candidate metric's evidence on four dimensions, deterministically.

    - accuracy: how strongly the column name itself signals a measure (an
      exact keyword match beats a suffix match) plus whether the table shape
      (FACT/TRANSACTION/EVENT/SNAPSHOT) and the annotation's own prose
      corroborate it.
    - clarity: how much human-authored, readable context (an approved
      glossary term already bound to the table, the annotation's
      description naming the measure, a non-nullable column) backs the
      candidate.
    - style: how well-formed the proposed metric would be -- an exact-match
      column of a monetary-shaped type with corroborating glossary grounding.
    - completeness: the fraction of the evidence categories this module
      tracks that are actually present.
    """
    is_exact = evidence.match_kind == "EXACT"
    is_suffix = evidence.match_kind == "SUFFIX"
    is_fact_like = evidence.table_role in _FACT_LIKE_TABLE_ROLES
    is_monetary = is_monetary_physical_type(evidence.physical_type)
    description_mentions = _description_mentions(evidence)

    categories = (
        is_exact,
        is_fact_like,
        is_monetary,
        not evidence.nullable,
        bool(evidence.bound_term_names),
        description_mentions,
    )
    completeness = sum(1 for present in categories if present) / len(categories)

    accuracy = 0.4
    accuracy += 0.3 if is_exact else (0.15 if is_suffix else 0.0)
    if is_fact_like:
        accuracy += 0.15
    if description_mentions:
        accuracy += 0.15
    accuracy = min(accuracy, 1.0)

    clarity = 0.3
    if evidence.bound_term_names:
        clarity += 0.3
    if description_mentions:
        clarity += 0.2
    if not evidence.nullable:
        clarity += 0.2
    clarity = min(clarity, 1.0)

    style = 0.3
    if is_monetary:
        style += 0.25
    if is_exact:
        style += 0.25
    if evidence.bound_term_names:
        style += 0.2
    style = min(style, 1.0)

    overall = round((accuracy + clarity + style + completeness) / 4, 4)
    return MetricConfidenceBreakdown(
        accuracy=round(accuracy, 4),
        clarity=round(clarity, 4),
        style=round(style, 4),
        completeness=round(completeness, 4),
        overall=overall,
    )


def _slugify(table_name: str, column_name: str) -> str:
    raw = f"{table_name}_{column_name}".strip().casefold()
    cleaned = "".join(char if char.isalnum() else "_" for char in raw)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("_")[:99] or "metric"
    if not cleaned[0].isalpha():
        cleaned = f"m_{cleaned}"[:99]
    return cleaned


def compose_metric_definition(evidence: MetricEvidence) -> tuple[str, str, str]:
    """Assemble a deterministic (slug, name, description) entirely from
    evidence fields. No model call."""
    slug = _slugify(evidence.table_name, evidence.column_name)
    readable_column = evidence.column_name.replace("_", " ").strip().title()
    name = f"{evidence.business_name} {readable_column}"[:200]

    sentences = [
        f"{evidence.suggested_aggregation.title()} of "
        f"{evidence.table_name}.{evidence.column_name}, proposed from the approved "
        f"business annotation on {evidence.table_name} ({evidence.business_name})."
    ]
    match_word = {
        "EXACT": "exactly matches",
        "SUFFIX": "matches as a suffix",
        "CONTAINS": "appears within",
    }[evidence.match_kind]
    sentences.append(
        f"The column name {match_word} the measure keyword '{evidence.matched_keyword}'."
    )
    if evidence.table_role in _FACT_LIKE_TABLE_ROLES:
        sentences.append(f"{evidence.table_name} is inferred as a {evidence.table_role} table.")
    if evidence.bound_term_names:
        term_word = "term" if len(evidence.bound_term_names) == 1 else "terms"
        sentences.append(
            f"{evidence.table_name} is linked to the glossary {term_word} "
            + ", ".join(evidence.bound_term_names)
            + "."
        )
    sentences.append(f"Approved business context: {evidence.business_description.strip()}")
    return slug, name, " ".join(sentences)


def evidence_payload(evidence: MetricEvidence) -> dict[str, Any]:
    """JSON-safe evidence record: the raw signals a proposal was built from."""
    return {
        "table_role": evidence.table_role,
        "column_id": str(evidence.column_id),
        "column_name": evidence.column_name,
        "physical_type": evidence.physical_type,
        "nullable": evidence.nullable,
        "matched_keyword": evidence.matched_keyword,
        "suggested_aggregation": evidence.suggested_aggregation,
        "match_kind": evidence.match_kind,
        "is_monetary_type": is_monetary_physical_type(evidence.physical_type),
        "business_annotation_id": str(evidence.business_annotation_id),
        "bound_term_names": list(evidence.bound_term_names),
    }


def ensure_reviewable(overall_score: float) -> None:
    """Gate submission to review on a minimum evidence bar.

    This is the only place a proposal's route to `governance_review` can be
    blocked, and it is enforced purely on the deterministic score computed
    above -- never bypassed by role, urgency, or any other signal. Mirrors
    `asset_description_service.ensure_reviewable` (GL-9) exactly.
    """
    if overall_score < MINIMUM_EVIDENCE_FOR_METRIC_REVIEW:
        raise HTTPException(
            status_code=422,
            detail="metric proposal carries too little evidence for independent review",
        )


def _metric_version_fingerprint(proposal: SemanticMetricProposal) -> str:
    payload = json.dumps(
        {
            "table_id": str(proposal.table_id),
            "measure_column_id": str(proposal.measure_column_id),
            "aggregation": proposal.proposed_aggregation,
            "grain": proposal.proposed_grain,
            "source_annotation_id": str(proposal.source_annotation_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def apply_metric_suggestion_proposal(
    session: AsyncSession,
    proposal: SemanticMetricProposal,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, SemanticMetricVersion]:
    """Publish an approved proposal as a real, published `SemanticMetric` +
    `SemanticMetricVersion`.

    Called exclusively from `semantic_api.decide_governance_review` after its
    shared maker-checker guard (status must be PENDING, requester independent
    of the approver) has already passed. There is no other call site that
    can move a proposal to APPROVED. See the module docstring for why this
    creates a dedicated, immediately-published `SemanticModelVersion` to host
    the metric version rather than reusing an unrelated draft.
    """
    if proposal.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="proposal is no longer pending review")

    metric = await session.scalar(
        select(SemanticMetric).where(
            SemanticMetric.project_id == proposal.project_id,
            SemanticMetric.slug == proposal.proposed_slug,
        )
    )
    if metric is None:
        metric = SemanticMetric(
            organization_id=proposal.organization_id,
            project_id=proposal.project_id,
            slug=proposal.proposed_slug,
        )
        session.add(metric)
        await session.flush()

    latest_model_version = await session.scalar(
        select(func.max(SemanticModelVersion.version)).where(
            SemanticModelVersion.project_id == proposal.project_id
        )
    )
    model_version = SemanticModelVersion(
        organization_id=proposal.organization_id,
        project_id=proposal.project_id,
        version=(latest_model_version or 0) + 1,
        name=f"Inferred metric: {proposal.proposed_slug}",
        change_summary=(
            "Auto-created to host a metric proposed from approved-annotation evidence "
            f"(SM-4); see SemanticMetricProposal {proposal.id} for provenance."
        ),
        status="PUBLISHED",
        created_by="system:sm4-metric-suggestion",
        approved_by=reviewer,
        approved_at=now,
        published_at=now,
    )
    session.add(model_version)
    await session.flush()

    await session.execute(
        update(SemanticMetricVersion)
        .where(
            SemanticMetricVersion.metric_id == metric.id,
            SemanticMetricVersion.status == "PUBLISHED",
        )
        .values(status="SUPERSEDED", updated_at=now)
    )
    latest_metric_version = await session.scalar(
        select(func.max(SemanticMetricVersion.version)).where(
            SemanticMetricVersion.metric_id == metric.id
        )
    )
    version = SemanticMetricVersion(
        organization_id=proposal.organization_id,
        semantic_model_version_id=model_version.id,
        metric_id=metric.id,
        version=(latest_metric_version or 0) + 1,
        status="PUBLISHED",
        name=proposal.proposed_name,
        description=proposal.proposed_description,
        aggregation=proposal.proposed_aggregation,
        grain=proposal.proposed_grain,
        source_table_id=proposal.table_id,
        measure_column_id=proposal.measure_column_id,
        allowed_dimension_column_ids=[],
        fingerprint=_metric_version_fingerprint(proposal),
        created_by=proposal.created_by,
    )
    session.add(version)
    await session.flush()

    proposal.status = "APPROVED"
    proposal.reviewed_by = reviewer
    proposal.reviewed_at = now
    proposal.published_metric_version_id = version.id
    return "metric_suggestion.approved.v1", version


async def reject_metric_suggestion_proposal(
    proposal: SemanticMetricProposal,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a proposal. Retained (not deleted) as negative knowledge, the
    same as `asset_description_service.reject_asset_description_draft` (GL-9)
    and `stewardship_service.reject_link_proposal` (GL-8) -- an identical
    low-value proposal is not silently regenerated on the next `generate`
    call (the evidence-tuple unique constraint would refuse it anyway, but
    the row staying REJECTED makes that refusal legible in the review UI)."""
    if proposal.status != "PENDING_APPROVAL":
        raise HTTPException(status_code=409, detail="proposal is no longer pending review")
    proposal.status = "REJECTED"
    proposal.reviewed_by = reviewer
    proposal.reviewed_at = now
    return "metric_suggestion.rejected.v1"
