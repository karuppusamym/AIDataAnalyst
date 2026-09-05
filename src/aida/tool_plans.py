"""
Multi-Step Tool Plans (Phase E - EE.6 / AG-4)
===============================================

Governed execution plans that compose multiple tools into a dependency-ordered
pipeline with budget enforcement, partial failure handling, and per-step
evidence recording.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    ToolPlanExecutionRecord,
    ToolPlanRecord,
    ToolPlanStepRecord,
)

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

PlanStatus = Literal[
    "DRAFT",
    "VALIDATED",
    "EXECUTING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]

StepStatus = Literal[
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "SKIPPED",
    "CANCELLED",
]


@dataclass(frozen=True, slots=True)
class PlanBudget:
    max_steps: int = 20
    max_time_seconds: int = 600
    max_tokens: int = 100_000
    max_cost_units: float = 100.0


@dataclass(frozen=True, slots=True)
class PlanStep:
    sequence: int
    tool_id: str
    tool_version: str
    parameters: dict[str, Any]
    dependencies: list[int] = field(default_factory=list)
    timeout_seconds: int = 300
    expected_cost: float = 0.0


@dataclass(slots=True)
class StepResult:
    sequence: int
    status: StepStatus
    evidence: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    step_sequence: int
    issue: str
    severity: Literal["ERROR", "WARNING"]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue]


@dataclass(frozen=True, slots=True)
class BudgetConsumed:
    steps_executed: int
    time_seconds: float
    tokens_used: int
    cost_units: float


@dataclass(slots=True)
class PlanResult:
    plan_id: UUID
    status: PlanStatus
    step_results: list[StepResult]
    budget_consumed: BudgetConsumed
    started_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ToolPlan:
    id: UUID | None
    name: str
    steps: list[PlanStep]
    budget: PlanBudget
    status: PlanStatus = "DRAFT"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_plan(plan: ToolPlan, available_tools: set[str] | None = None) -> ValidationResult:
    """Validate a tool plan for structural correctness and budget feasibility."""
    issues: list[ValidationIssue] = []

    if not plan.steps:
        issues.append(ValidationIssue(step_sequence=0, issue="plan has no steps", severity="ERROR"))
        return ValidationResult(valid=False, issues=issues)

    # Check step count budget
    if len(plan.steps) > plan.budget.max_steps:
        issues.append(
            ValidationIssue(
                step_sequence=0,
                issue=(
                    f"plan has {len(plan.steps)} steps, "
                    f"exceeds budget of {plan.budget.max_steps}"
                ),
                severity="ERROR",
            )
        )

    # Check total expected cost
    total_cost = sum(s.expected_cost for s in plan.steps)
    if total_cost > plan.budget.max_cost_units:
        issues.append(
            ValidationIssue(
                step_sequence=0,
                issue=(
                    f"total expected cost {total_cost} "
                    f"exceeds budget of {plan.budget.max_cost_units}"
                ),
                severity="ERROR",
            )
        )

    # Check total expected time
    total_time = sum(s.timeout_seconds for s in plan.steps)
    if total_time > plan.budget.max_time_seconds:
        issues.append(
            ValidationIssue(
                step_sequence=0,
                issue=(
                    f"total timeout {total_time}s "
                    f"exceeds budget of {plan.budget.max_time_seconds}s"
                ),
                severity="WARNING",
            )
        )

    sequences = {s.sequence for s in plan.steps}
    if len(sequences) != len(plan.steps):
        issues.append(ValidationIssue(0, "step sequences must be unique", "ERROR"))

    for step in plan.steps:
        # Check dependencies reference valid sequences
        for dep in step.dependencies:
            if dep not in sequences:
                issues.append(
                    ValidationIssue(
                        step_sequence=step.sequence,
                        issue=f"dependency on step {dep} which does not exist",
                        severity="ERROR",
                    )
                )
            if dep >= step.sequence:
                issues.append(
                    ValidationIssue(
                        step_sequence=step.sequence,
                        issue=(
                            f"dependency on step {dep} which is not a "
                            "predecessor (circular/forward)"
                        ),
                        severity="ERROR",
                    )
                )

        # Check tool availability
        if available_tools is not None and step.tool_id not in available_tools:
            issues.append(
                ValidationIssue(
                    step_sequence=step.sequence,
                    issue=f"tool '{step.tool_id}' is not available or approved",
                    severity="ERROR",
                )
            )

    has_errors = any(i.severity == "ERROR" for i in issues)
    return ValidationResult(valid=not has_errors, issues=issues)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _topological_order(steps: list[PlanStep]) -> list[PlanStep]:
    """Sort steps in dependency-respecting order (stable topological sort)."""
    by_seq = {s.sequence: s for s in steps}
    visited: set[int] = set()
    ordered: list[PlanStep] = []

    def visit(seq: int) -> None:
        if seq in visited:
            return
        step = by_seq[seq]
        for dep in step.dependencies:
            visit(dep)
        visited.add(seq)
        ordered.append(step)

    for s in sorted(by_seq.keys()):
        visit(s)

    return ordered


async def execute_plan(
    plan: ToolPlan,
    organization_id: UUID,
    session: AsyncSession,
    executor_principal: str,
    step_executor: Any | None = None,
) -> PlanResult:
    """Execute a validated plan in dependency order with budget enforcement.

    An executor is mandatory. Missing runtime wiring must never report success.
    """
    started_at = datetime.now(UTC)
    step_results: list[StepResult] = []
    completed_sequences: set[int] = set()
    failed = False

    budget_time = 0.0
    budget_tokens = 0
    budget_cost = 0.0

    validation = validate_plan(plan)
    if not validation.valid or step_executor is None:
        reason = "tool executor is unavailable" if step_executor is None else "; ".join(
            issue.issue for issue in validation.issues if issue.severity == "ERROR"
        )
        return PlanResult(
            plan.id or uuid4(), "FAILED",
            [StepResult(s.sequence, "FAILED", error_message=reason) for s in plan.steps],
            BudgetConsumed(0, 0, 0, 0), started_at, datetime.now(UTC),
        )

    ordered_steps = _topological_order(plan.steps)

    for step in ordered_steps:
        # Check if all dependencies completed
        deps_met = all(d in completed_sequences for d in step.dependencies)

        if not deps_met or failed:
            step_results.append(
                StepResult(
                    sequence=step.sequence,
                    status="SKIPPED",
                    evidence={
                        "reason": (
                            "dependency_not_met" if not deps_met else "earlier_failure"
                        )
                    },
                )
            )
            continue

        # Budget check before execution
        if budget_cost + step.expected_cost > plan.budget.max_cost_units:
            step_results.append(
                StepResult(
                    sequence=step.sequence,
                    status="CANCELLED",
                    evidence={"reason": "cost_budget_exceeded"},
                )
            )
            failed = True
            continue

        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        if elapsed > plan.budget.max_time_seconds:
            step_results.append(
                StepResult(
                    sequence=step.sequence,
                    status="CANCELLED",
                    evidence={"reason": "time_budget_exceeded"},
                )
            )
            failed = True
            continue

        # Execute step
        step_start = datetime.now(UTC)
        try:
            async with asyncio.timeout(min(
                step.timeout_seconds, max(0.001, plan.budget.max_time_seconds - elapsed)
            )):
                result = await step_executor(step, {"organization_id": str(organization_id)})
        except TimeoutError:
            result = StepResult(step.sequence, "FAILED", error_message="step timeout exceeded")
        except Exception:
            # Do not persist exception text: provider errors may include source values.
            result = StepResult(step.sequence, "FAILED", error_message="tool execution failed")

        result.started_at = result.started_at or step_start
        result.completed_at = result.completed_at or datetime.now(UTC)

        step_results.append(result)

        if result.status == "COMPLETED":
            completed_sequences.add(step.sequence)
            budget_cost += step.expected_cost
            if result.completed_at and result.started_at:
                budget_time += (result.completed_at - result.started_at).total_seconds()
        elif result.status != "COMPLETED":
            failed = True

    completed_at = datetime.now(UTC)
    all_completed = all(r.status == "COMPLETED" for r in step_results)

    return PlanResult(
        plan_id=plan.id or uuid4(),
        status="COMPLETED" if all_completed else "FAILED",
        step_results=step_results,
        budget_consumed=BudgetConsumed(
            steps_executed=len([r for r in step_results if r.status in ("COMPLETED", "FAILED")]),
            time_seconds=round(budget_time, 2),
            tokens_used=budget_tokens,
            cost_units=round(budget_cost, 2),
        ),
        started_at=started_at,
        completed_at=completed_at,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def persist_plan(
    session: AsyncSession,
    organization_id: UUID,
    plan: ToolPlan,
    created_by: str,
) -> ToolPlanRecord:
    """Persist a tool plan and its steps."""
    record = ToolPlanRecord(
        organization_id=organization_id,
        name=plan.name,
        budget={
            "max_steps": plan.budget.max_steps,
            "max_time_seconds": plan.budget.max_time_seconds,
            "max_tokens": plan.budget.max_tokens,
            "max_cost_units": plan.budget.max_cost_units,
        },
        status=plan.status,
        created_by=created_by,
    )
    session.add(record)
    await session.flush()

    for step in plan.steps:
        step_record = ToolPlanStepRecord(
            organization_id=organization_id,
            plan_id=record.id,
            sequence=step.sequence,
            tool_id=step.tool_id,
            tool_version=step.tool_version,
            parameters=step.parameters,
            dependencies=step.dependencies,
            timeout_seconds=step.timeout_seconds,
            expected_cost=step.expected_cost,
        )
        session.add(step_record)

    return record


async def persist_execution(
    session: AsyncSession,
    organization_id: UUID,
    plan_result: PlanResult,
    executed_by: str,
) -> ToolPlanExecutionRecord:
    """Persist execution results."""
    record = ToolPlanExecutionRecord(
        organization_id=organization_id,
        plan_id=plan_result.plan_id,
        started_at=plan_result.started_at,
        completed_at=plan_result.completed_at,
        budget_consumed={
            "steps_executed": plan_result.budget_consumed.steps_executed,
            "time_seconds": plan_result.budget_consumed.time_seconds,
            "tokens_used": plan_result.budget_consumed.tokens_used,
            "cost_units": plan_result.budget_consumed.cost_units,
        },
        status=plan_result.status,
        executed_by=executed_by,
    )
    session.add(record)
    return record
