"""Unit tests for the AI trust-score algorithm and assessment scoring.

Pure-function tests only (no database), mirroring the existing convention in
tests/test_unified_lineage.py and tests/test_mcp_server.py.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aida.ai_registry import compute_ai_trust_score, score_assessment_controls
from aida.models import AiAssessment, AiAssetVersion


def _version(**overrides: object) -> AiAssetVersion:
    values: dict[str, object] = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "asset_id": uuid4(),
        "version": 1,
        "status": "APPROVED",
        "name": "Fraud triage agent",
        "description": "Summarizes suspicious transactions for human review.",
        "intended_use": "Assist fraud analysts; never auto-decides.",
        "owner_principal": "fraud-ml-team",
        "provider_type": "INTERNAL",
        "risk_tier": "MEDIUM",
        "documentation_url": "https://internal.example/docs/fraud-agent",
        "context_product_version_ids": [],
        "model_route_ids": [],
        "policy_control_ids": ["pii-masking", "cost-cap"],
        "evaluation_evidence": {"pass_rate": 0.9, "evidence_id": "eval-1"},
        "runtime_evidence": {"success_rate": 0.95, "open_critical_incidents": 0},
        "fingerprint": "abc123",
        "created_by": "someone",
        "approved_by": "reviewer-1",
    }
    values.update(overrides)
    return AiAssetVersion(**values)


def _assessment(**overrides: object) -> AiAssessment:
    values: dict[str, object] = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "ai_asset_version_id": uuid4(),
        "framework": "NIST_AI_RMF",
        "framework_version": "1.0",
        "status": "PASS",
        "score": 90,
        "control_results": [],
        "findings": [],
        "assessed_by": "reviewer-1",
    }
    values.update(overrides)
    return AiAssessment(**values)


def test_fully_documented_approved_version_with_passing_assessment_is_trusted() -> None:
    version = _version()
    assessment = _assessment()
    score = compute_ai_trust_score(version, assessment, computed_at=datetime.now(UTC))
    assert score.blockers == []
    assert score.grade == "TRUSTED"
    assert score.score >= 85
    # every factor must carry a human-readable reason and machine-checkable evidence
    for factor in score.factors:
        assert factor.reason
        assert isinstance(factor.evidence, dict)


def test_missing_assessment_is_blocked_and_capped() -> None:
    version = _version()
    score = compute_ai_trust_score(version, None, computed_at=datetime.now(UTC))
    assert "ASSESSMENT_MISSING" in score.blockers
    assert score.grade == "BLOCKED"
    assert score.score <= 59


def test_prohibited_risk_tier_is_always_blocked() -> None:
    version = _version(risk_tier="PROHIBITED", policy_control_ids=["a", "b", "c", "d"])
    assessment = _assessment()
    score = compute_ai_trust_score(version, assessment, computed_at=datetime.now(UTC))
    assert "PROHIBITED_RISK_TIER" in score.blockers
    assert score.grade == "BLOCKED"


def test_high_risk_below_evaluation_threshold_is_blocked() -> None:
    version = _version(
        risk_tier="HIGH",
        policy_control_ids=["a", "b", "c", "d"],
        evaluation_evidence={"pass_rate": 0.5},
    )
    assessment = _assessment()
    score = compute_ai_trust_score(version, assessment, computed_at=datetime.now(UTC))
    assert "HIGH_RISK_EVALUATION_BELOW_THRESHOLD" in score.blockers


def test_open_critical_incident_zeroes_runtime_factor_and_blocks() -> None:
    version = _version(
        runtime_evidence={"success_rate": 1.0, "open_critical_incidents": 2},
    )
    assessment = _assessment()
    score = compute_ai_trust_score(version, assessment, computed_at=datetime.now(UTC))
    assert "OPEN_CRITICAL_RUNTIME_INCIDENT" in score.blockers
    runtime_factor = next(f for f in score.factors if f.factor == "RUNTIME_POSTURE")
    assert runtime_factor.score == 0.0


def test_failed_assessment_is_blocked() -> None:
    version = _version()
    assessment = _assessment(status="FAIL", score=20)
    score = compute_ai_trust_score(version, assessment, computed_at=datetime.now(UTC))
    assert "ASSESSMENT_FAILED" in score.blockers


@pytest.mark.parametrize(
    ("results", "expected_status"),
    [
        ([{"control_key": "a", "title": "A", "weight": 1, "outcome": "PASS"}], "PASS"),
        ([{"control_key": "a", "title": "A", "weight": 1, "outcome": "FAIL"}], "FAIL"),
        (
            [
                {"control_key": "a", "title": "A", "weight": 3, "outcome": "PASS"},
                {"control_key": "b", "title": "B", "weight": 1, "outcome": "FAIL"},
            ],
            "NEEDS_REMEDIATION",
        ),
    ],
)
def test_score_assessment_controls_status_thresholds(
    results: list[dict[str, object]], expected_status: str
) -> None:
    score, status, findings = score_assessment_controls(results)
    assert status == expected_status
    if expected_status != "PASS":
        assert findings


def test_score_assessment_controls_ignores_not_applicable() -> None:
    results = [
        {"control_key": "a", "title": "A", "weight": 1, "outcome": "PASS"},
        {"control_key": "b", "title": "B", "weight": 5, "outcome": "NOT_APPLICABLE"},
    ]
    score, status, findings = score_assessment_controls(results)
    assert score == 100
    assert status == "PASS"
    assert findings == []


def test_score_assessment_controls_empty_is_zero_score() -> None:
    score, status, findings = score_assessment_controls([])
    assert score == 0
    assert status == "FAIL"
    assert findings == []
