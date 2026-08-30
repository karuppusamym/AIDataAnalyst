from datetime import UTC, datetime
from typing import Any

from aida.models import AiAssessment, AiAssetVersion
from aida.platform_schemas import AiTrustFactorRead, AiTrustScoreRead


def _normalized_rate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    rate = float(value)
    if rate > 1:
        rate /= 100
    return max(0.0, min(rate, 1.0))


def compute_ai_trust_score(
    version: AiAssetVersion,
    assessment: AiAssessment | None,
    *,
    computed_at: datetime | None = None,
) -> AiTrustScoreRead:
    factors: list[AiTrustFactorRead] = []
    blockers: list[str] = []

    documentation_parts = [
        bool(version.description.strip()),
        bool(version.intended_use.strip()),
        bool(version.documentation_url),
    ]
    documentation_score = round((10.0 / 3.0) * sum(documentation_parts), 2)
    factors.append(
        AiTrustFactorRead(
            factor="DOCUMENTATION",
            score=documentation_score,
            maximum=10,
            reason=f"{sum(documentation_parts)} of 3 required documentation elements are present.",
            evidence={"documentation_url_present": bool(version.documentation_url)},
        )
    )

    ownership_score = 10.0 if version.owner_principal.strip() else 0.0
    factors.append(
        AiTrustFactorRead(
            factor="ACCOUNTABILITY",
            score=ownership_score,
            maximum=10,
            reason="An accountable owner is assigned."
            if ownership_score
            else "No owner is assigned.",
            evidence={"owner_principal": version.owner_principal},
        )
    )

    lifecycle_scores = {
        "APPROVED": 15.0,
        "REVIEW_REQUIRED": 8.0,
        "DRAFT": 3.0,
        "SUPERSEDED": 5.0,
        "REJECTED": 0.0,
        "RETIRED": 0.0,
    }
    lifecycle_score = lifecycle_scores.get(version.status, 0.0)
    factors.append(
        AiTrustFactorRead(
            factor="GOVERNANCE_LIFECYCLE",
            score=lifecycle_score,
            maximum=15,
            reason=f"The governed version is {version.status}.",
            evidence={"status": version.status, "approved_by": version.approved_by},
        )
    )

    required_controls = {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "PROHIBITED": 4}[version.risk_tier]
    control_count = len(version.policy_control_ids)
    policy_score = 15.0 * min(control_count / required_controls, 1.0)
    factors.append(
        AiTrustFactorRead(
            factor="POLICY_COVERAGE",
            score=round(policy_score, 2),
            maximum=15,
            reason=f"{control_count} of {required_controls} risk-tier policy controls are linked.",
            evidence={"risk_tier": version.risk_tier, "control_count": control_count},
        )
    )

    evaluation_rate = _normalized_rate(version.evaluation_evidence.get("pass_rate"))
    evaluation_score = round(15.0 * evaluation_rate, 2)
    factors.append(
        AiTrustFactorRead(
            factor="EVALUATION_POSTURE",
            score=evaluation_score,
            maximum=15,
            reason=f"Recorded evaluation pass rate is {evaluation_rate:.1%}.",
            evidence={
                "pass_rate": evaluation_rate,
                "evidence_id": version.evaluation_evidence.get("evidence_id"),
            },
        )
    )

    runtime_rate = _normalized_rate(version.runtime_evidence.get("success_rate"))
    critical_incidents = int(version.runtime_evidence.get("open_critical_incidents") or 0)
    runtime_score = round(15.0 * runtime_rate, 2)
    if critical_incidents:
        runtime_score = 0.0
        blockers.append("OPEN_CRITICAL_RUNTIME_INCIDENT")
    factors.append(
        AiTrustFactorRead(
            factor="RUNTIME_POSTURE",
            score=runtime_score,
            maximum=15,
            reason=(
                f"{critical_incidents} critical incidents are open."
                if critical_incidents
                else f"Recorded runtime success rate is {runtime_rate:.1%}."
            ),
            evidence={
                "success_rate": runtime_rate,
                "open_critical_incidents": critical_incidents,
                "evidence_id": version.runtime_evidence.get("evidence_id"),
            },
        )
    )

    assessment_score = float(assessment.score) / 5 if assessment is not None else 0.0
    factors.append(
        AiTrustFactorRead(
            factor="INDEPENDENT_ASSESSMENT",
            score=round(assessment_score, 2),
            maximum=20,
            reason=(
                f"Latest {assessment.framework} assessment scored {assessment.score}/100."
                if assessment is not None
                else "No independent assessment has been recorded."
            ),
            evidence={
                "assessment_id": str(assessment.id) if assessment is not None else None,
                "framework": assessment.framework if assessment is not None else None,
                "status": assessment.status if assessment is not None else None,
            },
        )
    )

    if version.risk_tier == "PROHIBITED":
        blockers.append("PROHIBITED_RISK_TIER")
    if version.risk_tier == "HIGH" and evaluation_rate < 0.8:
        blockers.append("HIGH_RISK_EVALUATION_BELOW_THRESHOLD")
    if assessment is None:
        blockers.append("ASSESSMENT_MISSING")
    elif assessment.status == "FAIL":
        blockers.append("ASSESSMENT_FAILED")

    raw_score = round(sum(factor.score for factor in factors))
    score = min(raw_score, 59) if blockers else raw_score
    if blockers:
        grade = "BLOCKED"
    elif score >= 85:
        grade = "TRUSTED"
    elif score >= 70:
        grade = "CONDITIONAL"
    else:
        grade = "UNTRUSTED"
    return AiTrustScoreRead(
        ai_asset_version_id=version.id,
        score=score,
        grade=grade,
        factors=factors,
        blockers=sorted(set(blockers)),
        computed_at=computed_at or datetime.now(UTC),
    )


def score_assessment_controls(
    control_results: list[dict[str, Any]],
) -> tuple[int, str, list[dict[str, Any]]]:
    applicable = [item for item in control_results if item["outcome"] != "NOT_APPLICABLE"]
    total_weight = sum(int(item["weight"]) for item in applicable)
    passed_weight = sum(int(item["weight"]) for item in applicable if item["outcome"] == "PASS")
    score = round((passed_weight / total_weight) * 100) if total_weight else 0
    failed = [
        {
            "control_key": item["control_key"],
            "title": item["title"],
            "finding": item.get("finding") or "Control did not pass.",
        }
        for item in applicable
        if item["outcome"] == "FAIL"
    ]
    status = (
        "PASS" if score >= 80 and not failed else "NEEDS_REMEDIATION" if score >= 60 else "FAIL"
    )
    return score, status, failed
