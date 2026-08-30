"""Tests for multi-step tool plans."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aida.tool_plans import (
    BudgetConsumed,
    PlanBudget,
    PlanResult,
    PlanStep,
    StepResult,
    ToolPlan,
    ValidationIssue,
    _topological_order,
    validate_plan,
)


def _make_plan(**overrides: object) -> ToolPlan:
    defaults = dict(
        id=uuid4(),
        name="Test Plan",
        steps=[
            PlanStep(sequence=1, tool_id="tool-a", tool_version="1.0", parameters={"x": 1}),
            PlanStep(
                sequence=2,
                tool_id="tool-b",
                tool_version="1.0",
                parameters={"y": 2},
                dependencies=[1],
            ),
        ],
        budget=PlanBudget(),
    )
    defaults.update(overrides)
    return ToolPlan(**defaults)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_plan_passes() -> None:
    plan = _make_plan()
    result = validate_plan(plan)
    assert result.valid is True
    assert len(result.issues) == 0


def test_empty_steps_fails_validation() -> None:
    plan = _make_plan(steps=[])
    result = validate_plan(plan)
    assert result.valid is False
    assert any("no steps" in i.issue for i in result.issues)


def test_too_many_steps_fails_validation() -> None:
    steps = [
        PlanStep(sequence=i, tool_id=f"tool-{i}", tool_version="1.0", parameters={})
        for i in range(1, 25)
    ]
    plan = _make_plan(steps=steps, budget=PlanBudget(max_steps=10))
    result = validate_plan(plan)
    assert result.valid is False
    assert any("exceeds budget" in i.issue for i in result.issues)


def test_cost_budget_exceeded_fails() -> None:
    steps = [
        PlanStep(sequence=1, tool_id="expensive", tool_version="1.0", parameters={}, expected_cost=200.0),
    ]
    plan = _make_plan(steps=steps, budget=PlanBudget(max_cost_units=100.0))
    result = validate_plan(plan)
    assert result.valid is False
    assert any("cost" in i.issue.lower() for i in result.issues)


def test_invalid_dependency_reference_fails() -> None:
    steps = [
        PlanStep(sequence=1, tool_id="tool-a", tool_version="1.0", parameters={}),
        PlanStep(
            sequence=2,
            tool_id="tool-b",
            tool_version="1.0",
            parameters={},
            dependencies=[99],  # does not exist
        ),
    ]
    plan = _make_plan(steps=steps)
    result = validate_plan(plan)
    assert result.valid is False
    assert any("does not exist" in i.issue for i in result.issues)


def test_forward_dependency_fails() -> None:
    steps = [
        PlanStep(
            sequence=1,
            tool_id="tool-a",
            tool_version="1.0",
            parameters={},
            dependencies=[2],  # forward reference
        ),
        PlanStep(sequence=2, tool_id="tool-b", tool_version="1.0", parameters={}),
    ]
    plan = _make_plan(steps=steps)
    result = validate_plan(plan)
    assert result.valid is False
    assert any("circular" in i.issue.lower() or "forward" in i.issue.lower() for i in result.issues)


def test_tool_availability_check() -> None:
    plan = _make_plan()
    available = {"tool-a"}  # "tool-b" not available
    result = validate_plan(plan, available_tools=available)
    assert result.valid is False
    assert any("not available" in i.issue for i in result.issues)


def test_tool_availability_all_present() -> None:
    plan = _make_plan()
    available = {"tool-a", "tool-b"}
    result = validate_plan(plan, available_tools=available)
    assert result.valid is True


def test_time_budget_warning() -> None:
    """Total timeout exceeding budget is a warning, not error."""
    steps = [
        PlanStep(sequence=1, tool_id="slow", tool_version="1.0", parameters={}, timeout_seconds=500),
        PlanStep(sequence=2, tool_id="also-slow", tool_version="1.0", parameters={}, timeout_seconds=500, dependencies=[1]),
    ]
    plan = _make_plan(steps=steps, budget=PlanBudget(max_time_seconds=600))
    result = validate_plan(plan)
    # Time budget is a warning, not an error, so plan is still valid
    assert result.valid is True
    warnings = [i for i in result.issues if i.severity == "WARNING"]
    assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def test_topological_order_respects_dependencies() -> None:
    steps = [
        PlanStep(sequence=3, tool_id="c", tool_version="1.0", parameters={}, dependencies=[1, 2]),
        PlanStep(sequence=1, tool_id="a", tool_version="1.0", parameters={}),
        PlanStep(sequence=2, tool_id="b", tool_version="1.0", parameters={}, dependencies=[1]),
    ]
    ordered = _topological_order(steps)
    seqs = [s.sequence for s in ordered]
    assert seqs.index(1) < seqs.index(2)
    assert seqs.index(1) < seqs.index(3)
    assert seqs.index(2) < seqs.index(3)


def test_topological_order_independent_steps() -> None:
    steps = [
        PlanStep(sequence=2, tool_id="b", tool_version="1.0", parameters={}),
        PlanStep(sequence=1, tool_id="a", tool_version="1.0", parameters={}),
        PlanStep(sequence=3, tool_id="c", tool_version="1.0", parameters={}),
    ]
    ordered = _topological_order(steps)
    seqs = [s.sequence for s in ordered]
    # With no dependencies, should be in sequence order
    assert seqs == [1, 2, 3]


# ---------------------------------------------------------------------------
# Budget enforcement in results
# ---------------------------------------------------------------------------


def test_budget_consumed_dataclass() -> None:
    bc = BudgetConsumed(steps_executed=3, time_seconds=15.5, tokens_used=5000, cost_units=3.5)
    assert bc.steps_executed == 3
    assert bc.time_seconds == 15.5
    assert bc.tokens_used == 5000
    assert bc.cost_units == 3.5


# ---------------------------------------------------------------------------
# Partial failure handling
# ---------------------------------------------------------------------------


def test_step_result_tracks_status() -> None:
    sr = StepResult(
        sequence=1,
        status="FAILED",
        evidence={"error": "timeout"},
        error_message="step timed out",
    )
    assert sr.status == "FAILED"
    assert sr.error_message == "step timed out"


def test_plan_result_completed() -> None:
    result = PlanResult(
        plan_id=uuid4(),
        status="COMPLETED",
        step_results=[
            StepResult(sequence=1, status="COMPLETED"),
            StepResult(sequence=2, status="COMPLETED"),
        ],
        budget_consumed=BudgetConsumed(steps_executed=2, time_seconds=10.0, tokens_used=0, cost_units=0),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    assert result.status == "COMPLETED"
    assert len(result.step_results) == 2


def test_plan_result_partial_failure() -> None:
    result = PlanResult(
        plan_id=uuid4(),
        status="FAILED",
        step_results=[
            StepResult(sequence=1, status="COMPLETED"),
            StepResult(sequence=2, status="FAILED", error_message="tool error"),
            StepResult(sequence=3, status="SKIPPED", evidence={"reason": "earlier_failure"}),
        ],
        budget_consumed=BudgetConsumed(steps_executed=2, time_seconds=5.0, tokens_used=0, cost_units=0),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    assert result.status == "FAILED"
    completed = [s for s in result.step_results if s.status == "COMPLETED"]
    assert len(completed) == 1  # Step 1 completed, retained
    skipped = [s for s in result.step_results if s.status == "SKIPPED"]
    assert len(skipped) == 1


# ---------------------------------------------------------------------------
# Dependency ordering in results
# ---------------------------------------------------------------------------


def test_plan_status_values() -> None:
    """All plan status values are valid literals."""
    statuses = ["DRAFT", "VALIDATED", "EXECUTING", "COMPLETED", "FAILED", "CANCELLED"]
    for s in statuses:
        plan = _make_plan(status=s)
        assert plan.status == s


def test_step_status_values() -> None:
    """All step status values are valid."""
    statuses = ["PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED", "CANCELLED"]
    for s in statuses:
        sr = StepResult(sequence=1, status=s)
        assert sr.status == s
