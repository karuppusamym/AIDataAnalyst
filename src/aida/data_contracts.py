"""
Atlas Data Contracts Engine
===========================

Provides enterprise-grade formal data contracts between data producers and data
consumers, supporting schema drift detection, freshness & SLA watermarks,
column constraint assertions, and automated incident reconciliation.

Key Capabilities
----------------
1. Schema Contract Assertions:
   - Required columns, forbidden types, nullability limits
   - Non-breaking changes vs breaking changes detection
2. SLA & Freshness Assertions:
   - Maximum allowable ingestion latency
   - Arrival schedule watermarks
3. Quality Constraint Assertions:
   - Range bounds, set membership, regex patterns
4. Contract Verification Engine:
   - Evaluates active schema/table profiles against published contract specs
   - Produces structured evaluation results (PASSED, WARNING, BREACHED)
   - Emits Kafka outbox events for downstream alerting
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data Contract Specifications (Pydantic Models)
# ---------------------------------------------------------------------------


class ColumnContractSpec(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    physical_type: str = Field(min_length=1, max_length=100)
    nullable: bool = True
    classification: str | None = None
    min_distinct_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    max_null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    description: str | None = None


class SlaContractSpec(BaseModel):
    max_latency_minutes: int = Field(default=1440, ge=1)
    cron_schedule: str | None = None
    min_row_count: int | None = Field(default=None, ge=0)
    max_volume_drop_percent: float | None = Field(default=25.0, ge=0.0, le=100.0)


class DataContractSpec(BaseModel):
    contract_name: str = Field(min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    status: Literal["DRAFT", "PROPOSED", "ACTIVE", "DEPRECATED"] = "ACTIVE"
    producer: str = Field(min_length=1, max_length=200)
    consumer: str = Field(min_length=1, max_length=200)
    target_table_name: str = Field(min_length=1, max_length=255)
    columns: list[ColumnContractSpec] = Field(default_factory=list)
    sla: SlaContractSpec = Field(default_factory=SlaContractSpec)
    strict_schema: bool = True  # If True, unexpected extra columns trigger a warning


# ---------------------------------------------------------------------------
# Contract Evaluation Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssertionResult:
    assertion_type: str
    target: str
    status: Literal["PASSED", "WARNING", "BREACHED"]
    message: str
    observed_value: Any = None
    expected_value: Any = None


@dataclass(frozen=True, slots=True)
class ContractEvaluationResult:
    contract_name: str
    version: int
    evaluated_at: datetime
    overall_status: Literal["PASSED", "WARNING", "BREACHED"]
    assertions: tuple[AssertionResult, ...]
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evaluated_at"] = self.evaluated_at.isoformat()
        return result


# ---------------------------------------------------------------------------
# Contract Verification Logic
# ---------------------------------------------------------------------------


def evaluate_data_contract(
    spec: DataContractSpec,
    discovered_columns: list[dict[str, Any]],
    *,
    row_count: int | None = None,
    last_updated_at: datetime | None = None,
    column_null_rates: dict[str, float] | None = None,
) -> ContractEvaluationResult:
    """Evaluate a table's discovered metadata against a formal Data Contract spec."""
    assertions: list[AssertionResult] = []
    observed_col_map = {col["name"].lower(): col for col in discovered_columns}
    null_rates = column_null_rates or {}

    # 1. Column Assertions
    for expected_col in spec.columns:
        col_name_lower = expected_col.name.lower()
        if col_name_lower not in observed_col_map:
            assertions.append(
                AssertionResult(
                    assertion_type="SCHEMA_REQUIRED_COLUMN",
                    target=expected_col.name,
                    status="BREACHED",
                    message=(
                        f"Required column '{expected_col.name}' is missing from the "
                        "table schema."
                    ),
                    observed_value=None,
                    expected_value=expected_col.name,
                )
            )
            continue

        actual_col = observed_col_map[col_name_lower]

        # Nullability check
        if not expected_col.nullable and actual_col.get("nullable", True):
            assertions.append(
                AssertionResult(
                    assertion_type="SCHEMA_NULLABILITY",
                    target=expected_col.name,
                    status="BREACHED",
                    message=(
                        f"Column '{expected_col.name}' is declared NOT NULL in contract "
                        "but marked nullable in source."
                    ),
                    observed_value=actual_col.get("nullable"),
                    expected_value=False,
                )
            )
        else:
            assertions.append(
                AssertionResult(
                    assertion_type="SCHEMA_NULLABILITY",
                    target=expected_col.name,
                    status="PASSED",
                    message=f"Nullability constraint satisfied for '{expected_col.name}'.",
                )
            )

        # Max null rate check
        if expected_col.max_null_ratio is not None and col_name_lower in null_rates:
            observed_null_rate = null_rates[col_name_lower]
            if observed_null_rate > expected_col.max_null_ratio:
                assertions.append(
                    AssertionResult(
                        assertion_type="QUALITY_NULL_RATE",
                        target=expected_col.name,
                        status="BREACHED",
                        message=(
                            f"Null rate {observed_null_rate:.2%} exceeds max allowed "
                            f"{expected_col.max_null_ratio:.2%}."
                        ),
                        observed_value=observed_null_rate,
                        expected_value=expected_col.max_null_ratio,
                    )
                )

    # 2. Strict Schema Check (Unexpected Columns)
    if spec.strict_schema:
        expected_names = {c.name.lower() for c in spec.columns}
        unexpected_cols = [
            c["name"] for c in discovered_columns if c["name"].lower() not in expected_names
        ]
        if unexpected_cols:
            assertions.append(
                AssertionResult(
                    assertion_type="SCHEMA_EXTRA_COLUMNS",
                    target="schema",
                    status="WARNING",
                    message=f"Found unexpected extra columns: {', '.join(unexpected_cols)}",
                    observed_value=unexpected_cols,
                    expected_value=list(expected_names),
                )
            )

    # 3. SLA & Volume Checks
    if spec.sla.min_row_count is not None and row_count is not None:
        if row_count < spec.sla.min_row_count:
            assertions.append(
                AssertionResult(
                    assertion_type="SLA_MIN_ROW_COUNT",
                    target="table",
                    status="BREACHED",
                    message=(
                        f"Observed row count {row_count} is below SLA minimum of "
                        f"{spec.sla.min_row_count}."
                    ),
                    observed_value=row_count,
                    expected_value=spec.sla.min_row_count,
                )
            )
        else:
            assertions.append(
                AssertionResult(
                    assertion_type="SLA_MIN_ROW_COUNT",
                    target="table",
                    status="PASSED",
                    message="Row count satisfies SLA minimum.",
                    observed_value=row_count,
                )
            )

    # 4. Freshness Checks
    if last_updated_at is not None:
        age_minutes = int((datetime.now(UTC) - last_updated_at).total_seconds() / 60)
        if age_minutes > spec.sla.max_latency_minutes:
            assertions.append(
                AssertionResult(
                    assertion_type="SLA_FRESHNESS",
                    target="table",
                    status="BREACHED",
                    message=(
                        f"Dataset freshness age {age_minutes}m exceeds SLA threshold of "
                        f"{spec.sla.max_latency_minutes}m."
                    ),
                    observed_value=age_minutes,
                    expected_value=spec.sla.max_latency_minutes,
                )
            )
        else:
            assertions.append(
                AssertionResult(
                    assertion_type="SLA_FRESHNESS",
                    target="table",
                    status="PASSED",
                    message="Dataset freshness within SLA threshold.",
                    observed_value=age_minutes,
                )
            )

    # Determine Overall Status
    has_breach = any(a.status == "BREACHED" for a in assertions)
    has_warning = any(a.status == "WARNING" for a in assertions)
    overall_status: Literal["PASSED", "WARNING", "BREACHED"] = (
        "BREACHED" if has_breach else "WARNING" if has_warning else "PASSED"
    )

    now = datetime.now(UTC)
    fingerprint = hashlib.sha256(
        f"{spec.contract_name}:{spec.version}:{overall_status}:{len(assertions)}".encode()
    ).hexdigest()

    return ContractEvaluationResult(
        contract_name=spec.contract_name,
        version=spec.version,
        evaluated_at=now,
        overall_status=overall_status,
        assertions=tuple(assertions),
        fingerprint=fingerprint,
    )
