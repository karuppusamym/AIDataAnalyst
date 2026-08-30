"""Trust-scored answers (EE.5).

Computes a composite trust score for assets used in AI-generated answers.
Every factor is explainable; the score never replaces evidence, it
summarizes it. Factors include quality posture, freshness, semantic
confidence, lineage depth, and tool approval status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TrustFactor:
    """One scored dimension contributing to the overall trust score."""

    name: str
    score: int  # 0-100
    weight: float  # 0.0-1.0
    evidence: dict[str, Any]
    explanation: str


@dataclass(frozen=True, slots=True)
class TrustScore:
    """Composite trust score for an asset or answer context."""

    overall_score: int  # 0-100
    grade: str  # A, B, C, D, F
    factors: list[TrustFactor] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AssetContext:
    """Input context for trust scoring: gathered evidence about assets."""

    quality_score: int | None = None  # 0-100 from quality observations
    freshness_status: str | None = None  # FRESH, STALE, NOT_CONFIGURED
    freshness_age_minutes: float | None = None
    freshness_threshold_minutes: int | None = None
    semantic_confidence: float | None = None  # 0.0-1.0
    lineage_depth: int | None = None
    tool_approval_status: str | None = None  # APPROVED, DRAFT, REJECTED


def _score_quality_posture(quality_score: int | None) -> TrustFactor:
    """Score quality posture from the latest observation."""
    if quality_score is None:
        return TrustFactor(
            name="quality_posture",
            score=50,
            weight=0.30,
            evidence={"quality_score": None},
            explanation="No quality observations available; score is neutral.",
        )
    return TrustFactor(
        name="quality_posture",
        score=quality_score,
        weight=0.30,
        evidence={"quality_score": quality_score},
        explanation=(
            f"Quality score is {quality_score}/100 from the latest observation."
        ),
    )


def _score_freshness(
    status: str | None,
    age_minutes: float | None,
    threshold_minutes: int | None,
) -> TrustFactor:
    """Score freshness from watermark evaluation."""
    if status is None or status == "NOT_CONFIGURED":
        return TrustFactor(
            name="freshness",
            score=50,
            weight=0.20,
            evidence={"freshness_status": status or "UNKNOWN"},
            explanation="Freshness monitoring is not configured; score is neutral.",
        )
    if status == "FRESH":
        score = 100
        explanation = "Data is fresh based on watermark evaluation."
    elif status == "STALE":
        if age_minutes is not None and threshold_minutes:
            staleness_ratio = age_minutes / threshold_minutes
            score = max(0, int(100 - (staleness_ratio - 1) * 50))
        else:
            score = 20
        explanation = (
            f"Data is stale: age is {age_minutes:.0f} minutes "
            f"(threshold: {threshold_minutes} minutes)."
            if age_minutes is not None and threshold_minutes
            else "Data is stale; no age details available."
        )
    else:
        score = 50
        explanation = f"Freshness status is {status}."

    return TrustFactor(
        name="freshness",
        score=score,
        weight=0.20,
        evidence={
            "freshness_status": status,
            "age_minutes": age_minutes,
            "threshold_minutes": threshold_minutes,
        },
        explanation=explanation,
    )


def _score_semantic_confidence(confidence: float | None) -> TrustFactor:
    """Score semantic confidence from retrieval or inference."""
    if confidence is None:
        return TrustFactor(
            name="semantic_confidence",
            score=50,
            weight=0.20,
            evidence={"semantic_confidence": None},
            explanation="No semantic confidence available; score is neutral.",
        )
    score = int(confidence * 100)
    return TrustFactor(
        name="semantic_confidence",
        score=score,
        weight=0.20,
        evidence={"semantic_confidence": confidence},
        explanation=f"Semantic confidence is {confidence:.2f}.",
    )


def _score_lineage_depth(depth: int | None) -> TrustFactor:
    """Score based on lineage availability and depth."""
    if depth is None:
        return TrustFactor(
            name="lineage_depth",
            score=40,
            weight=0.15,
            evidence={"lineage_depth": None},
            explanation="No lineage information available.",
        )
    if depth == 0:
        score = 60
        explanation = "Asset is a root node with no upstream lineage."
    elif depth <= 3:
        score = 90
        explanation = f"Well-traced lineage with depth {depth}."
    else:
        score = 70
        explanation = f"Deep lineage chain (depth {depth}); traceability may decrease."

    return TrustFactor(
        name="lineage_depth",
        score=score,
        weight=0.15,
        evidence={"lineage_depth": depth},
        explanation=explanation,
    )


def _score_tool_approval(status: str | None) -> TrustFactor:
    """Score based on governed tool approval status."""
    if status is None:
        return TrustFactor(
            name="tool_approval_status",
            score=50,
            weight=0.15,
            evidence={"tool_approval_status": None},
            explanation="No tool approval information available.",
        )
    score_map = {"APPROVED": 100, "DRAFT": 50, "REJECTED": 10}
    score = score_map.get(status, 50)
    return TrustFactor(
        name="tool_approval_status",
        score=score,
        weight=0.15,
        evidence={"tool_approval_status": status},
        explanation=f"Tool approval status is {status}.",
    )


def _compute_grade(score: int) -> str:
    """Map a numeric score to a letter grade."""
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def compute_trust_score(asset_context: AssetContext) -> TrustScore:
    """Compute a composite trust score from all available factors.

    Every factor is explainable; the score never replaces evidence, it
    summarizes it.
    """
    factors = [
        _score_quality_posture(asset_context.quality_score),
        _score_freshness(
            asset_context.freshness_status,
            asset_context.freshness_age_minutes,
            asset_context.freshness_threshold_minutes,
        ),
        _score_semantic_confidence(asset_context.semantic_confidence),
        _score_lineage_depth(asset_context.lineage_depth),
        _score_tool_approval(asset_context.tool_approval_status),
    ]

    total_weight = sum(f.weight for f in factors)
    if total_weight == 0:
        overall = 50
    else:
        overall = int(sum(f.score * f.weight for f in factors) / total_weight)

    overall = max(0, min(100, overall))
    grade = _compute_grade(overall)

    return TrustScore(overall_score=overall, grade=grade, factors=factors)
