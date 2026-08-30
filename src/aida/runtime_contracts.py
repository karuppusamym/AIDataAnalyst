"""
Runtime Data Contract Enforcement (Phase E - EE.1)
===================================================

Evaluates published data contracts at runtime against current state,
detecting schema drift, quality breaches, freshness violations, and
SLA breaches.  Composes existing quality observations, freshness checks,
and schema fingerprints into a unified enforcement surface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    ContractSlaRecord,
    ContractViolationRecord,
    DataContractVersion,
    DataQualityObservation,
    MetadataColumn,
    MetadataTable,
    TableProfile,
    utc_now,
)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

ViolationType = Literal[
    "SCHEMA_DRIFT",
    "QUALITY_BREACH",
    "FRESHNESS_BREACH",
    "SLA_BREACH",
]

Severity = Literal["INFO", "WARNING", "CRITICAL"]


@dataclass(frozen=True, slots=True)
class SchemaExpectation:
    column_name: str
    data_type: str
    required: bool = False
    classification: str | None = None


@dataclass(frozen=True, slots=True)
class QualityExpectation:
    max_null_rate: float = 1.0
    min_freshness_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    id: UUID
    product_id: UUID
    schema_contract: list[SchemaExpectation]
    quality_contract: QualityExpectation
    freshness_sla_minutes: int | None
    producer: str
    consumer: str
    version: int


@dataclass(frozen=True, slots=True)
class ContractViolation:
    contract_id: UUID
    violation_type: ViolationType
    severity: Severity
    evidence: dict[str, Any]
    detected_at: datetime


@dataclass(frozen=True, slots=True)
class EnforcementResult:
    allowed: bool
    violations: list[ContractViolation]
    enforcement_action: Literal["ALLOW", "WARN", "BLOCK"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SlaStatus:
    contract_id: UUID
    compliant: bool
    uptime_percent: float
    violations_in_period: int
    period_start: datetime
    period_end: datetime


# ---------------------------------------------------------------------------
# Contract from DB model
# ---------------------------------------------------------------------------


def contract_from_db(cv: DataContractVersion) -> RuntimeContract:
    """Build a RuntimeContract from a persisted DataContractVersion row."""
    schema_expectations = [
        SchemaExpectation(
            column_name=field_def["name"],
            data_type=field_def["data_type"],
            required=field_def.get("required", False),
            classification=field_def.get("classification"),
        )
        for field_def in (cv.schema_definition or [])
    ]

    max_null = 1.0
    for rule in (cv.quality_rules or []):
        if rule.get("rule_type") == "NOT_NULL" and rule.get("field_name"):
            max_null = min(max_null, 0.0)

    return RuntimeContract(
        id=cv.id,
        product_id=cv.product_id,
        schema_contract=schema_expectations,
        quality_contract=QualityExpectation(
            max_null_rate=max_null,
            min_freshness_minutes=cv.freshness_sla_minutes,
        ),
        freshness_sla_minutes=cv.freshness_sla_minutes,
        producer=cv.producer_principal,
        consumer=",".join(cv.consumer_roles) if cv.consumer_roles else "*",
        version=cv.version,
    )


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------


def _check_schema_drift(
    contract: RuntimeContract,
    current_columns: list[dict[str, Any]],
) -> list[ContractViolation]:
    """Compare expected schema against actual columns."""
    violations: list[ContractViolation] = []
    now = datetime.now(UTC)

    actual_by_name = {col["name"].lower(): col for col in current_columns}

    for expected in contract.schema_contract:
        actual = actual_by_name.get(expected.column_name.lower())
        if actual is None:
            if expected.required:
                violations.append(
                    ContractViolation(
                        contract_id=contract.id,
                        violation_type="SCHEMA_DRIFT",
                        severity="CRITICAL",
                        evidence={
                            "missing_column": expected.column_name,
                            "expected_type": expected.data_type,
                        },
                        detected_at=now,
                    )
                )
            else:
                violations.append(
                    ContractViolation(
                        contract_id=contract.id,
                        violation_type="SCHEMA_DRIFT",
                        severity="WARNING",
                        evidence={
                            "missing_column": expected.column_name,
                            "required": False,
                        },
                        detected_at=now,
                    )
                )
        elif actual.get("physical_type", "").upper() != expected.data_type.upper():
            violations.append(
                ContractViolation(
                    contract_id=contract.id,
                    violation_type="SCHEMA_DRIFT",
                    severity="CRITICAL",
                    evidence={
                        "column": expected.column_name,
                        "expected_type": expected.data_type,
                        "actual_type": actual.get("physical_type", "UNKNOWN"),
                    },
                    detected_at=now,
                )
            )

    return violations


def _check_quality(
    contract: RuntimeContract,
    quality_observations: list[dict[str, Any]],
) -> list[ContractViolation]:
    """Check quality thresholds from recent observations."""
    violations: list[ContractViolation] = []
    now = datetime.now(UTC)

    for obs in quality_observations:
        quality_score = obs.get("quality_score", 100)
        if quality_score < 50:
            violations.append(
                ContractViolation(
                    contract_id=contract.id,
                    violation_type="QUALITY_BREACH",
                    severity="CRITICAL",
                    evidence={
                        "quality_score": quality_score,
                        "threshold": 50,
                        "anomaly_types": obs.get("anomaly_types", []),
                    },
                    detected_at=now,
                )
            )
        elif quality_score < 75:
            violations.append(
                ContractViolation(
                    contract_id=contract.id,
                    violation_type="QUALITY_BREACH",
                    severity="WARNING",
                    evidence={
                        "quality_score": quality_score,
                        "threshold": 75,
                        "anomaly_types": obs.get("anomaly_types", []),
                    },
                    detected_at=now,
                )
            )

    return violations


def _check_freshness(
    contract: RuntimeContract,
    last_profile_at: datetime | None,
) -> list[ContractViolation]:
    """Check freshness SLA."""
    violations: list[ContractViolation] = []
    now = datetime.now(UTC)

    if contract.freshness_sla_minutes is None:
        return violations

    if last_profile_at is None:
        violations.append(
            ContractViolation(
                contract_id=contract.id,
                violation_type="FRESHNESS_BREACH",
                severity="CRITICAL",
                evidence={
                    "sla_minutes": contract.freshness_sla_minutes,
                    "last_profile_at": None,
                    "message": "no profile data available",
                },
                detected_at=now,
            )
        )
        return violations

    elapsed = now - last_profile_at
    elapsed_minutes = elapsed.total_seconds() / 60

    if elapsed_minutes > contract.freshness_sla_minutes:
        violations.append(
            ContractViolation(
                contract_id=contract.id,
                violation_type="FRESHNESS_BREACH",
                severity="CRITICAL" if elapsed_minutes > contract.freshness_sla_minutes * 2 else "WARNING",
                evidence={
                    "sla_minutes": contract.freshness_sla_minutes,
                    "elapsed_minutes": round(elapsed_minutes, 1),
                    "last_profile_at": last_profile_at.isoformat(),
                },
                detected_at=now,
            )
        )

    return violations


def evaluate_contract(
    contract: RuntimeContract,
    current_columns: list[dict[str, Any]],
    quality_observations: list[dict[str, Any]],
    last_profile_at: datetime | None,
) -> list[ContractViolation]:
    """Evaluate a contract against current state, returning all violations."""
    violations: list[ContractViolation] = []
    violations.extend(_check_schema_drift(contract, current_columns))
    violations.extend(_check_quality(contract, quality_observations))
    violations.extend(_check_freshness(contract, last_profile_at))
    return violations


def enforce_at_query_time(
    contract: RuntimeContract,
    violations: list[ContractViolation],
) -> EnforcementResult:
    """Decide enforcement action based on active violations."""
    if not violations:
        return EnforcementResult(
            allowed=True,
            violations=[],
            enforcement_action="ALLOW",
        )

    critical = [v for v in violations if v.severity == "CRITICAL"]
    if critical:
        return EnforcementResult(
            allowed=False,
            violations=violations,
            enforcement_action="BLOCK",
            reason=f"{len(critical)} critical violation(s) detected",
        )

    return EnforcementResult(
        allowed=True,
        violations=violations,
        enforcement_action="WARN",
        reason=f"{len(violations)} non-critical violation(s) detected",
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def persist_violations(
    session: AsyncSession,
    organization_id: UUID,
    violations: list[ContractViolation],
) -> list[ContractViolationRecord]:
    """Persist violation records and return them."""
    records = []
    for v in violations:
        record = ContractViolationRecord(
            organization_id=organization_id,
            contract_id=v.contract_id,
            violation_type=v.violation_type,
            severity=v.severity,
            evidence=v.evidence,
            detected_at=v.detected_at,
        )
        session.add(record)
        records.append(record)
    return records


async def record_sla_status(
    session: AsyncSession,
    organization_id: UUID,
    contract_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> ContractSlaRecord:
    """Compute and persist SLA status for a contract over a period."""
    stmt = select(func.count()).select_from(ContractViolationRecord).where(
        and_(
            ContractViolationRecord.contract_id == contract_id,
            ContractViolationRecord.organization_id == organization_id,
            ContractViolationRecord.detected_at >= period_start,
            ContractViolationRecord.detected_at <= period_end,
        )
    )
    result = await session.execute(stmt)
    violation_count = result.scalar() or 0

    # Simple SLA: percentage of time without violations
    total_minutes = max((period_end - period_start).total_seconds() / 60, 1)
    breach_minutes = violation_count * 5  # Assume 5 min per violation
    uptime = max(0.0, 100.0 * (1 - breach_minutes / total_minutes))

    record = ContractSlaRecord(
        organization_id=organization_id,
        contract_id=contract_id,
        period_start=period_start,
        period_end=period_end,
        uptime_percent=round(uptime, 2),
        violations_count=violation_count,
        breach_minutes=breach_minutes,
    )
    session.add(record)
    return record
