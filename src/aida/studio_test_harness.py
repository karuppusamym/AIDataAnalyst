"""Studio test harness for validating change set items.

Provides dry-run testing of governed object changes against synthetic
fixtures.  Tests validate parameter contracts for tools, expected outputs
for metrics, and structural integrity for terms and context products.

Only tested changes can be submitted for governance review (test gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aida.studio import (
    ChangeItem,
    ChangeSet,
    TestResult,
    validate_context_product_contract,
    validate_parameter_contract,
)


@dataclass
class TestFixture:
    """Synthetic data matching the schema of a governed object."""

    object_type: str
    object_id: str
    synthetic_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestSuiteResult:
    """Aggregated result of running all tests for a change set."""

    change_set_id: UUID
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    passed: bool = False
    item_results: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _validate_metric_item(
    item: ChangeItem,
    fixture: TestFixture | None,
) -> TestResult:
    """Validate a metric change item."""
    failures: list[str] = []
    evidence: dict[str, Any] = {"object_type": "METRIC", "object_id": item.object_id}

    snapshot = item.after_snapshot if item.operation != "DELETE" else item.before_snapshot
    if snapshot is None and item.operation != "DELETE":
        failures.append("metric definition missing: no after_snapshot provided")
        return TestResult(passed=False, failures=failures, evidence=evidence)

    if item.operation == "DELETE":
        evidence["validation"] = "delete_accepted"
        return TestResult(passed=True, failures=[], evidence=evidence)

    if snapshot is not None:
        # Validate required metric fields
        required_fields = ["name", "aggregation", "grain"]
        for f in required_fields:
            if f not in snapshot:
                failures.append(f"missing required metric field: {f}")

        valid_aggregations = {"SUM", "COUNT", "AVG", "MIN", "MAX"}
        if snapshot.get("aggregation") and snapshot["aggregation"] not in valid_aggregations:
            failures.append(f"invalid aggregation: {snapshot['aggregation']}")

    evidence["validation"] = "passed" if not failures else "failed"
    return TestResult(passed=len(failures) == 0, failures=failures, evidence=evidence)


def _validate_tool_item(
    item: ChangeItem,
    fixture: TestFixture | None,
) -> TestResult:
    """Validate a governed tool change item against its typed, enum-bound parameter
    contract (ST-A4), reusing the real ``ToolParameterDefinition`` schema and SQL
    renderer instead of a loose dict-shape check.
    """
    failures: list[str] = []
    evidence: dict[str, Any] = {"object_type": "TOOL", "object_id": item.object_id}

    if item.operation == "DELETE":
        evidence["validation"] = "delete_accepted"
        return TestResult(passed=True, failures=[], evidence=evidence)

    snapshot = item.after_snapshot
    if snapshot is None:
        failures.append("tool definition missing: no after_snapshot provided")
        return TestResult(passed=False, failures=failures, evidence=evidence)

    # Validate required tool fields
    required_fields = ["name", "sql_template", "allowed_roles"]
    for f in required_fields:
        if f not in snapshot:
            failures.append(f"missing required tool field: {f}")

    parameters = snapshot.get("parameters", [])
    if not isinstance(parameters, list):
        failures.append("parameters must be a list")
        parameters = []

    if failures:
        evidence["validation"] = "failed"
        evidence["parameter_count"] = len(parameters)
        return TestResult(passed=False, failures=failures, evidence=evidence)

    contract = validate_parameter_contract(
        sql_template=snapshot["sql_template"],
        raw_definitions=parameters,
        dialect=snapshot.get("dialect", "postgres"),
    )
    evidence["parameter_count"] = len(parameters)
    evidence["parameter_contract_errors"] = contract.errors
    if contract.sample_rendered_sql is not None:
        evidence["sample_rendered_sql"] = contract.sample_rendered_sql
    if not contract.valid:
        failures.extend(contract.errors)

    # Validate against fixture if provided
    if fixture and fixture.synthetic_data:
        test_params = fixture.synthetic_data.get("test_parameters", {})
        for param in contract.definitions:
            if param.get("required"):
                name = param.get("name", "")
                if name not in test_params:
                    evidence.setdefault("missing_test_params", []).append(name)

    evidence["validation"] = "passed" if not failures else "failed"
    return TestResult(passed=len(failures) == 0, failures=failures, evidence=evidence)


def _validate_term_item(
    item: ChangeItem,
    fixture: TestFixture | None,
) -> TestResult:
    """Validate a glossary term change item."""
    failures: list[str] = []
    evidence: dict[str, Any] = {"object_type": "TERM", "object_id": item.object_id}

    if item.operation == "DELETE":
        evidence["validation"] = "delete_accepted"
        return TestResult(passed=True, failures=[], evidence=evidence)

    snapshot = item.after_snapshot
    if snapshot is None:
        failures.append("term definition missing: no after_snapshot provided")
        return TestResult(passed=False, failures=failures, evidence=evidence)

    required_fields = ["display_name", "definition"]
    for f in required_fields:
        if f not in snapshot:
            failures.append(f"missing required term field: {f}")

    definition = snapshot.get("definition", "")
    if isinstance(definition, str) and len(definition) < 10:
        failures.append("term definition must be at least 10 characters")

    evidence["validation"] = "passed" if not failures else "failed"
    return TestResult(passed=len(failures) == 0, failures=failures, evidence=evidence)


def _validate_context_product_item(
    item: ChangeItem,
    fixture: TestFixture | None,
) -> TestResult:
    """Validate a context product change item (ST-A7).

    Reuses ``validate_context_product_contract``, which parses the snapshot as
    a real ``ContextProductDefinition`` (module 19's own pydantic contract)
    instead of a hand-rolled dict-shape check -- the same "reuse the real
    domain schema" pattern ST-A4's ``_validate_tool_item`` established for
    TOOL items via ``ToolParameterDefinition``.
    """
    evidence: dict[str, Any] = {"object_type": "CONTEXT_PRODUCT", "object_id": item.object_id}
    contract = validate_context_product_contract(
        operation=item.operation,
        object_id=item.object_id,
        snapshot=item.after_snapshot,
    )
    evidence["validation"] = "passed" if contract.valid else "failed"
    if contract.definition is not None:
        evidence["definition"] = contract.definition
    return TestResult(passed=contract.valid, failures=contract.errors, evidence=evidence)


_VALIDATORS = {
    "METRIC": _validate_metric_item,
    "TOOL": _validate_tool_item,
    "TERM": _validate_term_item,
    "CONTEXT_PRODUCT": _validate_context_product_item,
}


def run_test(
    item: ChangeItem,
    fixture: TestFixture | None = None,
) -> TestResult:
    """Run validation tests on a single change item.

    Dispatches to the appropriate validator based on object_type.
    """
    validator = _VALIDATORS.get(item.object_type)
    if validator is None:
        return TestResult(
            passed=False,
            failures=[f"unknown object type: {item.object_type}"],
            evidence={"object_type": item.object_type},
        )
    return validator(item, fixture)


def run_test_suite(
    change_set: ChangeSet,
    fixtures: dict[str, TestFixture] | None = None,
) -> TestSuiteResult:
    """Run the full test suite for all items in a change set.

    Returns aggregated results.  All items must pass for the suite to pass.
    """
    started_at = datetime.now(UTC)
    fixtures = fixtures or {}
    item_results: list[dict[str, Any]] = []
    all_passed = True

    for item in change_set.items:
        fixture_key = f"{item.object_type}:{item.object_id}"
        fixture = fixtures.get(fixture_key)
        result = run_test(item, fixture)

        item.test_status = "PASSED" if result.passed else "FAILED"
        if not result.passed:
            all_passed = False

        item_results.append(
            {
                "item_id": str(item.id),
                "object_type": item.object_type,
                "object_id": item.object_id,
                "operation": item.operation,
                "passed": result.passed,
                "failures": result.failures,
                "evidence": result.evidence,
            }
        )

    completed_at = datetime.now(UTC)

    return TestSuiteResult(
        change_set_id=change_set.id,
        started_at=started_at,
        completed_at=completed_at,
        passed=all_passed,
        item_results=item_results,
        evidence={
            "total_items": len(change_set.items),
            "passed_items": sum(1 for r in item_results if r["passed"]),
            "failed_items": sum(1 for r in item_results if not r["passed"]),
        },
    )
