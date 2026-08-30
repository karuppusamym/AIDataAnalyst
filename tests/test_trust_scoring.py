from aida.trust_scoring import (
    AssetContext,
    TrustScore,
    _compute_grade,
    _score_freshness,
    _score_lineage_depth,
    _score_quality_posture,
    _score_semantic_confidence,
    _score_tool_approval,
    compute_trust_score,
)
from aida.schemas import TrustFactorRead, TrustScoreRead


# --- _compute_grade ---


def test_grade_a() -> None:
    assert _compute_grade(90) == "A"
    assert _compute_grade(100) == "A"


def test_grade_b() -> None:
    assert _compute_grade(80) == "B"
    assert _compute_grade(89) == "B"


def test_grade_c() -> None:
    assert _compute_grade(70) == "C"
    assert _compute_grade(79) == "C"


def test_grade_d() -> None:
    assert _compute_grade(60) == "D"
    assert _compute_grade(69) == "D"


def test_grade_f() -> None:
    assert _compute_grade(59) == "F"
    assert _compute_grade(0) == "F"


# --- individual factor scoring ---


def test_quality_posture_none_is_neutral() -> None:
    factor = _score_quality_posture(None)
    assert factor.score == 50
    assert factor.weight == 0.30


def test_quality_posture_passes_through() -> None:
    factor = _score_quality_posture(85)
    assert factor.score == 85
    assert factor.evidence["quality_score"] == 85


def test_freshness_not_configured_is_neutral() -> None:
    factor = _score_freshness(None, None, None)
    assert factor.score == 50
    factor2 = _score_freshness("NOT_CONFIGURED", None, None)
    assert factor2.score == 50


def test_freshness_fresh_is_100() -> None:
    factor = _score_freshness("FRESH", 10.0, 60)
    assert factor.score == 100


def test_freshness_stale_degrades() -> None:
    factor = _score_freshness("STALE", 120.0, 60)
    assert factor.score < 100
    assert factor.score >= 0


def test_freshness_stale_no_details() -> None:
    factor = _score_freshness("STALE", None, None)
    assert factor.score == 20


def test_semantic_confidence_none_is_neutral() -> None:
    factor = _score_semantic_confidence(None)
    assert factor.score == 50
    assert factor.weight == 0.20


def test_semantic_confidence_scales() -> None:
    factor = _score_semantic_confidence(0.95)
    assert factor.score == 95
    factor_low = _score_semantic_confidence(0.3)
    assert factor_low.score == 30


def test_lineage_depth_none_is_low() -> None:
    factor = _score_lineage_depth(None)
    assert factor.score == 40


def test_lineage_depth_root() -> None:
    factor = _score_lineage_depth(0)
    assert factor.score == 60


def test_lineage_depth_well_traced() -> None:
    factor = _score_lineage_depth(2)
    assert factor.score == 90


def test_lineage_depth_deep() -> None:
    factor = _score_lineage_depth(5)
    assert factor.score == 70


def test_tool_approval_none_is_neutral() -> None:
    factor = _score_tool_approval(None)
    assert factor.score == 50


def test_tool_approval_approved() -> None:
    factor = _score_tool_approval("APPROVED")
    assert factor.score == 100


def test_tool_approval_rejected() -> None:
    factor = _score_tool_approval("REJECTED")
    assert factor.score == 10


# --- composite scoring ---


def test_all_defaults_produce_neutral_score() -> None:
    ctx = AssetContext()
    result = compute_trust_score(ctx)
    assert result.overall_score == 48  # weighted average of all neutral/none values
    assert result.grade == "F"
    assert len(result.factors) == 5


def test_perfect_context_scores_high() -> None:
    ctx = AssetContext(
        quality_score=100,
        freshness_status="FRESH",
        freshness_age_minutes=5.0,
        freshness_threshold_minutes=60,
        semantic_confidence=0.99,
        lineage_depth=2,
        tool_approval_status="APPROVED",
    )
    result = compute_trust_score(ctx)
    assert result.overall_score >= 90
    assert result.grade == "A"


def test_poor_context_scores_low() -> None:
    ctx = AssetContext(
        quality_score=20,
        freshness_status="STALE",
        freshness_age_minutes=500.0,
        freshness_threshold_minutes=60,
        semantic_confidence=0.1,
        lineage_depth=None,
        tool_approval_status="REJECTED",
    )
    result = compute_trust_score(ctx)
    assert result.overall_score < 30
    assert result.grade == "F"


def test_score_is_deterministic() -> None:
    ctx = AssetContext(
        quality_score=75,
        freshness_status="FRESH",
        semantic_confidence=0.85,
        lineage_depth=3,
        tool_approval_status="APPROVED",
    )
    r1 = compute_trust_score(ctx)
    r2 = compute_trust_score(ctx)
    assert r1.overall_score == r2.overall_score
    assert r1.grade == r2.grade


def test_score_clamped_to_0_100() -> None:
    ctx = AssetContext(quality_score=100)
    result = compute_trust_score(ctx)
    assert 0 <= result.overall_score <= 100


def test_all_factors_are_explainable() -> None:
    ctx = AssetContext(quality_score=80)
    result = compute_trust_score(ctx)
    for factor in result.factors:
        assert factor.name != ""
        assert factor.explanation != ""
        assert 0 <= factor.score <= 100
        assert 0.0 < factor.weight <= 1.0


def test_weights_sum_to_one() -> None:
    ctx = AssetContext()
    result = compute_trust_score(ctx)
    total = sum(f.weight for f in result.factors)
    assert abs(total - 1.0) < 0.001


# --- schema contracts ---


def test_trust_factor_read_schema() -> None:
    factor = TrustFactorRead(
        name="quality_posture",
        score=85,
        weight=0.30,
        evidence={"quality_score": 85},
        explanation="Quality score is 85/100.",
    )
    assert factor.name == "quality_posture"


def test_trust_score_read_schema() -> None:
    score = TrustScoreRead(
        overall_score=85,
        grade="B",
        factors=[
            TrustFactorRead(
                name="quality_posture",
                score=85,
                weight=0.30,
                evidence={},
                explanation="test",
            ),
        ],
    )
    assert score.grade == "B"
