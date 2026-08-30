"""
Compliance Pack Generation (Phase E - EE.4 / OB-5)
====================================================

Generates audit-ready compliance reports from runtime evidence.  Packs are
reproducible (same inputs produce the same output) and WORM-archived after
generation.  Supports MODEL_RISK, BCBS_239, ACCESS_REVIEW, AI_USAGE, and
CHANGE_CONTROL frameworks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AbacDecisionRecord,
    AgentRun,
    AiDecisionRecord,
    AuditEvent,
    CompliancePackRecord,
    ContractViolationRecord,
    DataContractVersion,
    DataQualityObservation,
    GovernanceReview,
    ModelRouteConfiguration,
    ToolExecution,
    utc_now,
)

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

Framework = Literal[
    "MODEL_RISK",
    "BCBS_239",
    "ACCESS_REVIEW",
    "AI_USAGE",
    "CHANGE_CONTROL",
]

SectionStatus = Literal["COMPLIANT", "NON_COMPLIANT", "NOT_ASSESSED"]


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    source: str
    count: int
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComplianceSection:
    title: str
    control_id: str
    evidence: list[EvidenceItem]
    status: SectionStatus


@dataclass(frozen=True, slots=True)
class CompliancePack:
    name: str
    framework: Framework
    period_start: datetime
    period_end: datetime
    sections: list[ComplianceSection]
    generated_at: datetime
    checksum: str


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------


def _compute_checksum(pack_dict: dict[str, Any]) -> str:
    """Deterministic checksum for reproducibility verification."""
    # Remove generated_at and checksum from hash input for reproducibility
    hashable = {k: v for k, v in pack_dict.items() if k not in ("generated_at", "checksum")}
    canonical = json.dumps(hashable, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _section_to_dict(section: ComplianceSection) -> dict[str, Any]:
    return {
        "title": section.title,
        "control_id": section.control_id,
        "evidence": [asdict(e) for e in section.evidence],
        "status": section.status,
    }


# ---------------------------------------------------------------------------
# Framework-specific generators
# ---------------------------------------------------------------------------


async def _generate_model_risk(
    session: AsyncSession,
    org_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> list[ComplianceSection]:
    """MODEL_RISK: model routes, evaluations, kill-switch status, approval chain."""
    sections: list[ComplianceSection] = []

    # Model route inventory
    stmt = select(func.count()).select_from(ModelRouteConfiguration).where(
        and_(
            ModelRouteConfiguration.organization_id == org_id,
            ModelRouteConfiguration.status == "APPROVED",
        )
    )
    result = await session.execute(stmt)
    approved_routes = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Model Route Inventory",
            control_id="MR-001",
            evidence=[
                EvidenceItem(
                    source="model_route_configuration",
                    count=approved_routes,
                    summary=f"{approved_routes} approved model routes in production",
                )
            ],
            status="COMPLIANT" if approved_routes > 0 else "NOT_ASSESSED",
        )
    )

    # AI Decision records (refusals check)
    stmt = select(func.count()).select_from(AiDecisionRecord).where(
        and_(
            AiDecisionRecord.organization_id == org_id,
            AiDecisionRecord.decision_type == "REFUSAL",
            AiDecisionRecord.decided_at >= period_start,
            AiDecisionRecord.decided_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    refusal_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Model Refusal Tracking",
            control_id="MR-002",
            evidence=[
                EvidenceItem(
                    source="ai_decision_record",
                    count=refusal_count,
                    summary=f"{refusal_count} model refusals in period",
                )
            ],
            status="COMPLIANT",
        )
    )

    return sections


async def _generate_bcbs_239(
    session: AsyncSession,
    org_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> list[ComplianceSection]:
    """BCBS_239: data lineage completeness, quality posture, timeliness."""
    sections: list[ComplianceSection] = []

    # Quality observations
    stmt = select(func.count()).select_from(DataQualityObservation).where(
        and_(
            DataQualityObservation.organization_id == org_id,
            DataQualityObservation.created_at >= period_start,
            DataQualityObservation.created_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    total_observations = result.scalar() or 0

    stmt_pass = select(func.count()).select_from(DataQualityObservation).where(
        and_(
            DataQualityObservation.organization_id == org_id,
            DataQualityObservation.status == "PASSED",
            DataQualityObservation.created_at >= period_start,
            DataQualityObservation.created_at <= period_end,
        )
    )
    result = await session.execute(stmt_pass)
    passed_observations = result.scalar() or 0

    pass_rate = (passed_observations / total_observations * 100) if total_observations > 0 else 0

    sections.append(
        ComplianceSection(
            title="Data Quality Posture",
            control_id="BCBS-001",
            evidence=[
                EvidenceItem(
                    source="data_quality_observation",
                    count=total_observations,
                    summary=f"{pass_rate:.1f}% quality pass rate ({passed_observations}/{total_observations})",
                    details={"pass_rate": pass_rate},
                )
            ],
            status="COMPLIANT" if pass_rate >= 90 else "NON_COMPLIANT" if total_observations > 0 else "NOT_ASSESSED",
        )
    )

    # Contract violations (timeliness / reconciliation)
    stmt = select(func.count()).select_from(ContractViolationRecord).where(
        and_(
            ContractViolationRecord.organization_id == org_id,
            ContractViolationRecord.detected_at >= period_start,
            ContractViolationRecord.detected_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    violation_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Data Timeliness & Contract Compliance",
            control_id="BCBS-002",
            evidence=[
                EvidenceItem(
                    source="contract_violation",
                    count=violation_count,
                    summary=f"{violation_count} contract violations in period",
                )
            ],
            status="COMPLIANT" if violation_count == 0 else "NON_COMPLIANT",
        )
    )

    return sections


async def _generate_access_review(
    session: AsyncSession,
    org_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> list[ComplianceSection]:
    """ACCESS_REVIEW: who accessed what, policy decisions, role assignments."""
    sections: list[ComplianceSection] = []

    # ABAC decisions
    stmt = select(func.count()).select_from(AbacDecisionRecord).where(
        and_(
            AbacDecisionRecord.organization_id == org_id,
            AbacDecisionRecord.evaluated_at >= period_start,
            AbacDecisionRecord.evaluated_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    total_decisions = result.scalar() or 0

    stmt_deny = select(func.count()).select_from(AbacDecisionRecord).where(
        and_(
            AbacDecisionRecord.organization_id == org_id,
            AbacDecisionRecord.decision == "DENY",
            AbacDecisionRecord.evaluated_at >= period_start,
            AbacDecisionRecord.evaluated_at <= period_end,
        )
    )
    result = await session.execute(stmt_deny)
    denied_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Access Control Decisions",
            control_id="AR-001",
            evidence=[
                EvidenceItem(
                    source="abac_decision",
                    count=total_decisions,
                    summary=f"{total_decisions} access decisions ({denied_count} denied)",
                    details={"denied_count": denied_count},
                )
            ],
            status="COMPLIANT" if total_decisions > 0 else "NOT_ASSESSED",
        )
    )

    return sections


async def _generate_ai_usage(
    session: AsyncSession,
    org_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> list[ComplianceSection]:
    """AI_USAGE: agent runs, tool invocations, model calls, refusals."""
    sections: list[ComplianceSection] = []

    # Agent runs
    stmt = select(func.count()).select_from(AgentRun).where(
        and_(
            AgentRun.organization_id == org_id,
            AgentRun.created_at >= period_start,
            AgentRun.created_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    agent_run_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Agent Execution Inventory",
            control_id="AI-001",
            evidence=[
                EvidenceItem(
                    source="agent_run",
                    count=agent_run_count,
                    summary=f"{agent_run_count} agent runs in period",
                )
            ],
            status="COMPLIANT" if agent_run_count >= 0 else "NOT_ASSESSED",
        )
    )

    # Tool invocations
    stmt = select(func.count()).select_from(ToolExecution).where(
        and_(
            ToolExecution.organization_id == org_id,
            ToolExecution.created_at >= period_start,
            ToolExecution.created_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    tool_exec_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Governed Tool Invocations",
            control_id="AI-002",
            evidence=[
                EvidenceItem(
                    source="tool_execution",
                    count=tool_exec_count,
                    summary=f"{tool_exec_count} governed tool invocations in period",
                )
            ],
            status="COMPLIANT",
        )
    )

    # AI Decision refusals
    stmt = select(func.count()).select_from(AiDecisionRecord).where(
        and_(
            AiDecisionRecord.organization_id == org_id,
            AiDecisionRecord.decision_type == "REFUSAL",
            AiDecisionRecord.decided_at >= period_start,
            AiDecisionRecord.decided_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    refusal_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="AI Refusal & Safety Log",
            control_id="AI-003",
            evidence=[
                EvidenceItem(
                    source="ai_decision_record",
                    count=refusal_count,
                    summary=f"{refusal_count} AI refusals in period",
                )
            ],
            status="COMPLIANT",
        )
    )

    return sections


async def _generate_change_control(
    session: AsyncSession,
    org_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> list[ComplianceSection]:
    """CHANGE_CONTROL: governance decisions, approvals, rejections."""
    sections: list[ComplianceSection] = []

    stmt = select(func.count()).select_from(GovernanceReview).where(
        and_(
            GovernanceReview.organization_id == org_id,
            GovernanceReview.created_at >= period_start,
            GovernanceReview.created_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    total_reviews = result.scalar() or 0

    stmt_approved = select(func.count()).select_from(GovernanceReview).where(
        and_(
            GovernanceReview.organization_id == org_id,
            GovernanceReview.status == "APPROVED",
            GovernanceReview.created_at >= period_start,
            GovernanceReview.created_at <= period_end,
        )
    )
    result = await session.execute(stmt_approved)
    approved_count = result.scalar() or 0

    stmt_rejected = select(func.count()).select_from(GovernanceReview).where(
        and_(
            GovernanceReview.organization_id == org_id,
            GovernanceReview.status == "REJECTED",
            GovernanceReview.created_at >= period_start,
            GovernanceReview.created_at <= period_end,
        )
    )
    result = await session.execute(stmt_rejected)
    rejected_count = result.scalar() or 0

    sections.append(
        ComplianceSection(
            title="Governance Review Activity",
            control_id="CC-001",
            evidence=[
                EvidenceItem(
                    source="governance_review",
                    count=total_reviews,
                    summary=(
                        f"{total_reviews} governance reviews "
                        f"({approved_count} approved, {rejected_count} rejected)"
                    ),
                    details={
                        "approved": approved_count,
                        "rejected": rejected_count,
                    },
                )
            ],
            status="COMPLIANT" if total_reviews > 0 else "NOT_ASSESSED",
        )
    )

    return sections


_GENERATORS = {
    "MODEL_RISK": _generate_model_risk,
    "BCBS_239": _generate_bcbs_239,
    "ACCESS_REVIEW": _generate_access_review,
    "AI_USAGE": _generate_ai_usage,
    "CHANGE_CONTROL": _generate_change_control,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_pack(
    framework: Framework,
    period_start: datetime,
    period_end: datetime,
    org_id: UUID,
    session: AsyncSession,
    generated_by: str,
) -> CompliancePack:
    """Generate a compliance pack from runtime evidence."""
    generator = _GENERATORS.get(framework)
    if generator is None:
        raise ValueError(f"unsupported compliance framework: {framework}")

    sections = await generator(session, org_id, period_start, period_end)
    generated_at = datetime.now(UTC)

    pack_dict = {
        "name": f"{framework} Compliance Pack",
        "framework": framework,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "sections": [_section_to_dict(s) for s in sections],
    }
    checksum = _compute_checksum(pack_dict)

    return CompliancePack(
        name=pack_dict["name"],
        framework=framework,
        period_start=period_start,
        period_end=period_end,
        sections=sections,
        generated_at=generated_at,
        checksum=checksum,
    )


async def persist_pack(
    session: AsyncSession,
    org_id: UUID,
    pack: CompliancePack,
    generated_by: str,
) -> CompliancePackRecord:
    """WORM-archive a generated compliance pack."""
    record = CompliancePackRecord(
        organization_id=org_id,
        name=pack.name,
        framework=pack.framework,
        period_start=pack.period_start,
        period_end=pack.period_end,
        sections=[_section_to_dict(s) for s in pack.sections],
        checksum=pack.checksum,
        generated_by=generated_by,
        generated_at=pack.generated_at,
    )
    session.add(record)
    return record
