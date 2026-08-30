"""Freshness watermark contracts (DQ-2).

Deterministic freshness evaluation based on watermark columns.
CRITICAL invariant (ADR-0016): scan age is NEVER presented as freshness.
Freshness is only activated for explicitly configured tables; unconfigured
tables return NOT_CONFIGURED. Configuration requires maker-checker approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class WatermarkConfig:
    """Configuration for a table's freshness watermark."""

    table_id: str
    watermark_column: str
    classification: str  # e.g. "CONFIDENTIAL", "INTERNAL", "PUBLIC"
    threshold_minutes: int
    retention_days: int
    approved_by: str | None = None
    approved_at: datetime | None = None
    status: str = "PENDING_APPROVAL"  # PENDING_APPROVAL, ACTIVE, DISABLED


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    """Deterministic freshness evaluation output."""

    status: str  # FRESH, STALE, NOT_CONFIGURED, AWAITING_APPROVAL
    last_watermark: datetime | None
    age_minutes: float | None
    threshold_minutes: int | None
    evidence: dict[str, Any] = field(default_factory=dict)


def evaluate_freshness(
    config: WatermarkConfig | None,
    latest_watermark: datetime | None,
    *,
    evaluation_time: datetime | None = None,
) -> FreshnessResult:
    """Evaluate freshness from watermark configuration and latest observed watermark.

    This function ONLY uses the actual data watermark timestamp, never the
    scan/observation time. ADR-0016: scan age is NEVER presented as freshness.
    """
    if config is None:
        return FreshnessResult(
            status="NOT_CONFIGURED",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=None,
            evidence={"reason": "no freshness configuration for this table"},
        )

    if config.status == "PENDING_APPROVAL":
        return FreshnessResult(
            status="AWAITING_APPROVAL",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=config.threshold_minutes,
            evidence={
                "reason": "watermark configuration awaiting maker-checker approval",
                "table_id": config.table_id,
            },
        )

    if config.status == "DISABLED":
        return FreshnessResult(
            status="NOT_CONFIGURED",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=None,
            evidence={"reason": "freshness monitoring is disabled for this table"},
        )

    now = evaluation_time or datetime.now(UTC)

    if latest_watermark is None:
        return FreshnessResult(
            status="STALE",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=config.threshold_minutes,
            evidence={
                "reason": "no watermark observation recorded",
                "watermark_column": config.watermark_column,
                "table_id": config.table_id,
            },
        )

    age_minutes = (now - latest_watermark).total_seconds() / 60.0
    is_fresh = age_minutes <= config.threshold_minutes

    return FreshnessResult(
        status="FRESH" if is_fresh else "STALE",
        last_watermark=latest_watermark,
        age_minutes=round(age_minutes, 2),
        threshold_minutes=config.threshold_minutes,
        evidence={
            "watermark_column": config.watermark_column,
            "table_id": config.table_id,
            "classification": config.classification,
            "evaluation_source": "data_watermark",
        },
    )
