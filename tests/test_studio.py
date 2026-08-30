"""Tests for the Studio authoring environment.

Pure unit tests -- no database required.  Covers change set lifecycle,
conflict detection, diff computation, impact preview, and the test harness.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from aida.studio import (
    ChangeItem,
    ChangeSet,
    Conflict,
    ImpactPreview,
    TestResult,
    add_item,
    compute_diff,
    compute_impact,
    create_change_set,
    detect_conflicts,
    remove_item,
)
from aida.studio_test_harness import (
    TestFixture,
    TestSuiteResult,
    run_test,
    run_test_suite,
)


def _make_item(
    *,
    object_type: str = "METRIC",
    object_id: str = "revenue",
    operation: str = "CREATE",
    before_snapshot: dict | None = None,
    after_snapshot: dict | None = None,
) -> ChangeItem:
    return ChangeItem(
        id=uuid4(),
        object_type=object_type,
        object_id=object_id,
        operation=operation,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
    )


# ---------------------------------------------------------------------------
# Change set lifecycle
# ---------------------------------------------------------------------------


class TestChangeSetLifecycle:
    def test_create_change_set_returns_draft(self) -> None:
        cs = create_change_set("test-set", "user@example.com")
        assert cs.status == "DRAFT"
        assert cs.conflict_status == "CLEAN"
        assert cs.name == "test-set"
        assert cs.author == "user@example.com"
        assert isinstance(cs.id, UUID)

    def test_add_item_to_draft(self) -> None:
        cs = create_change_set("test-set", "user@example.com")
        item = _make_item()
        cs = add_item(cs, item)
        assert len(cs.items) == 1
        assert cs.items[0].id == item.id

    def test_add_item_to_non_draft_raises(self) -> None:
        cs = create_change_set("test-set", "user@example.com")
        cs.status = "SUBMITTED"
        with pytest.raises(ValueError, match="DRAFT"):
            add_item(cs, _make_item())

    def test_remove_item(self) -> None:
        cs = create_change_set("test-set", "user@example.com")
        item = _make_item()
        cs = add_item(cs, item)
        assert len(cs.items) == 1
        cs = remove_item(cs, item.id)
        assert len(cs.items) == 0

    def test_remove_item_from_non_draft_raises(self) -> None:
        cs = create_change_set("test-set", "user@example.com")
        item = _make_item()
        cs = add_item(cs, item)
        cs.status = "TESTING"
        with pytest.raises(ValueError, match="DRAFT"):
            remove_item(cs, item.id)

    def test_remove_nonexistent_item_is_noop(self) -> None:
        cs = create_change_set("test-set", "user@example.com")
        item = _make_item()
        cs = add_item(cs, item)
        cs = remove_item(cs, uuid4())  # non-existent
        assert len(cs.items) == 1


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


class TestDiffComputation:
    def test_identical_snapshots_produce_empty_diff(self) -> None:
        snap = {"name": "revenue", "aggregation": "SUM"}
        diff = compute_diff(snap, snap)
        assert diff == {}

    def test_changed_field_appears_in_diff(self) -> None:
        before = {"name": "revenue", "aggregation": "SUM"}
        after = {"name": "revenue", "aggregation": "AVG"}
        diff = compute_diff(before, after)
        assert "aggregation" in diff
        assert diff["aggregation"]["before"] == "SUM"
        assert diff["aggregation"]["after"] == "AVG"

    def test_added_field_appears_in_diff(self) -> None:
        before = {"name": "revenue"}
        after = {"name": "revenue", "grain": "daily"}
        diff = compute_diff(before, after)
        assert "grain" in diff
        assert diff["grain"]["before"] is None
        assert diff["grain"]["after"] == "daily"

    def test_removed_field_appears_in_diff(self) -> None:
        before = {"name": "revenue", "grain": "daily"}
        after = {"name": "revenue"}
        diff = compute_diff(before, after)
        assert "grain" in diff
        assert diff["grain"]["before"] == "daily"
        assert diff["grain"]["after"] is None

    def test_diff_computed_on_add_item_when_both_snapshots_present(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(
            operation="UPDATE",
            before_snapshot={"name": "r", "aggregation": "SUM"},
            after_snapshot={"name": "r", "aggregation": "AVG"},
        )
        cs = add_item(cs, item)
        assert cs.items[0].diff is not None
        assert "aggregation" in cs.items[0].diff


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_no_conflicts_when_state_unchanged(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(
            operation="UPDATE",
            before_snapshot={"name": "revenue", "aggregation": "SUM"},
            after_snapshot={"name": "revenue", "aggregation": "AVG"},
        )
        cs = add_item(cs, item)
        # Current state matches the before_snapshot
        state = {"METRIC:revenue": {"name": "revenue", "aggregation": "SUM"}}
        conflicts = detect_conflicts(cs, state)
        assert conflicts == []

    def test_field_level_conflict_detected(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(
            operation="UPDATE",
            before_snapshot={"name": "revenue", "aggregation": "SUM"},
            after_snapshot={"name": "revenue", "aggregation": "AVG"},
        )
        cs = add_item(cs, item)
        # Published state has diverged: aggregation changed to COUNT
        state = {"METRIC:revenue": {"name": "revenue", "aggregation": "COUNT"}}
        conflicts = detect_conflicts(cs, state)
        assert len(conflicts) == 1
        assert conflicts[0].field_name == "aggregation"
        assert conflicts[0].current_value == "COUNT"

    def test_create_conflict_when_object_already_exists(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(operation="CREATE", after_snapshot={"name": "revenue"})
        cs = add_item(cs, item)
        state = {"METRIC:revenue": {"name": "revenue"}}
        conflicts = detect_conflicts(cs, state)
        assert len(conflicts) == 1
        assert conflicts[0].current_value == "ALREADY_EXISTS"

    def test_delete_conflict_when_object_already_gone(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(operation="DELETE", before_snapshot={"name": "revenue"})
        cs = add_item(cs, item)
        state = {}  # already deleted
        conflicts = detect_conflicts(cs, state)
        assert len(conflicts) == 1
        assert conflicts[0].current_value == "ALREADY_DELETED"

    def test_update_conflict_when_object_not_found(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(
            operation="UPDATE",
            before_snapshot={"name": "revenue"},
            after_snapshot={"name": "revenue_v2"},
        )
        cs = add_item(cs, item)
        state = {}  # not found
        conflicts = detect_conflicts(cs, state)
        assert len(conflicts) == 1
        assert conflicts[0].current_value == "NOT_FOUND"

    def test_clean_when_no_state_provided(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(operation="CREATE", after_snapshot={"name": "new"})
        cs = add_item(cs, item)
        conflicts = detect_conflicts(cs, {})
        assert conflicts == []


# ---------------------------------------------------------------------------
# Impact preview
# ---------------------------------------------------------------------------


class TestImpactPreview:
    def test_impact_lists_all_items(self) -> None:
        cs = create_change_set("test", "user@example.com")
        cs = add_item(cs, _make_item(object_id="a", operation="CREATE"))
        cs = add_item(cs, _make_item(object_id="b", operation="DELETE"))
        cs = add_item(cs, _make_item(object_id="c", operation="UPDATE"))

        impact = compute_impact(cs)
        assert isinstance(impact, ImpactPreview)
        assert impact.affected_object_count == 3
        assert len(impact.affected_objects) == 3

        ops = {o["operation"] for o in impact.affected_objects}
        assert ops == {"CREATE", "DELETE", "UPDATE"}

    def test_impact_includes_changed_fields_from_diff(self) -> None:
        cs = create_change_set("test", "user@example.com")
        item = _make_item(
            operation="UPDATE",
            before_snapshot={"name": "r", "agg": "SUM"},
            after_snapshot={"name": "r", "agg": "AVG"},
        )
        cs = add_item(cs, item)
        impact = compute_impact(cs)

        changed = impact.affected_objects[0].get("changed_fields", [])
        assert "agg" in changed

    def test_empty_change_set_has_zero_impact(self) -> None:
        cs = create_change_set("test", "user@example.com")
        impact = compute_impact(cs)
        assert impact.affected_object_count == 0
        assert impact.affected_objects == []


# ---------------------------------------------------------------------------
# Test harness -- run_test for individual item types
# ---------------------------------------------------------------------------


class TestHarnessMetric:
    def test_valid_metric_passes(self) -> None:
        item = _make_item(
            object_type="METRIC",
            operation="CREATE",
            after_snapshot={"name": "revenue", "aggregation": "SUM", "grain": "daily"},
        )
        result = run_test(item)
        assert result.passed is True
        assert result.failures == []

    def test_missing_required_field_fails(self) -> None:
        item = _make_item(
            object_type="METRIC",
            operation="CREATE",
            after_snapshot={"name": "revenue"},  # missing aggregation, grain
        )
        result = run_test(item)
        assert result.passed is False
        assert any("aggregation" in f or "grain" in f for f in result.failures)

    def test_invalid_aggregation_fails(self) -> None:
        item = _make_item(
            object_type="METRIC",
            operation="CREATE",
            after_snapshot={"name": "revenue", "aggregation": "MEDIAN", "grain": "daily"},
        )
        result = run_test(item)
        assert result.passed is False
        assert any("MEDIAN" in f for f in result.failures)

    def test_delete_metric_passes(self) -> None:
        item = _make_item(object_type="METRIC", operation="DELETE")
        result = run_test(item)
        assert result.passed is True

    def test_no_snapshot_fails(self) -> None:
        item = _make_item(object_type="METRIC", operation="CREATE")
        result = run_test(item)
        assert result.passed is False


class TestHarnessTool:
    def test_valid_tool_passes(self) -> None:
        item = _make_item(
            object_type="TOOL",
            operation="CREATE",
            after_snapshot={
                "name": "lookup",
                "sql_template": "SELECT * FROM t WHERE id = :id",
                "allowed_roles": ["Analyst"],
                "parameters": [
                    {"name": "id", "parameter_type": "INTEGER", "required": True},
                ],
            },
        )
        result = run_test(item)
        assert result.passed is True
        assert result.evidence.get("parameter_count") == 1

    def test_duplicate_parameter_name_fails(self) -> None:
        item = _make_item(
            object_type="TOOL",
            operation="CREATE",
            after_snapshot={
                "name": "lookup",
                "sql_template": "SELECT 1",
                "allowed_roles": ["Analyst"],
                "parameters": [
                    {"name": "id", "parameter_type": "INTEGER"},
                    {"name": "id", "parameter_type": "STRING"},
                ],
            },
        )
        result = run_test(item)
        assert result.passed is False
        assert any("duplicate" in f for f in result.failures)

    def test_invalid_parameter_type_fails(self) -> None:
        item = _make_item(
            object_type="TOOL",
            operation="CREATE",
            after_snapshot={
                "name": "lookup",
                "sql_template": "SELECT 1",
                "allowed_roles": ["Analyst"],
                "parameters": [
                    {"name": "x", "parameter_type": "BLOB"},
                ],
            },
        )
        result = run_test(item)
        assert result.passed is False
        assert any("BLOB" in f for f in result.failures)

    def test_missing_tool_fields_fails(self) -> None:
        item = _make_item(
            object_type="TOOL",
            operation="CREATE",
            after_snapshot={"name": "lookup"},
        )
        result = run_test(item)
        assert result.passed is False
        assert any("sql_template" in f for f in result.failures)

    def test_delete_tool_passes(self) -> None:
        item = _make_item(object_type="TOOL", operation="DELETE")
        result = run_test(item)
        assert result.passed is True


class TestHarnessTerm:
    def test_valid_term_passes(self) -> None:
        item = _make_item(
            object_type="TERM",
            operation="CREATE",
            after_snapshot={
                "display_name": "Revenue",
                "definition": "Total income from all product sales before deductions.",
            },
        )
        result = run_test(item)
        assert result.passed is True

    def test_short_definition_fails(self) -> None:
        item = _make_item(
            object_type="TERM",
            operation="CREATE",
            after_snapshot={
                "display_name": "Revenue",
                "definition": "Money",  # too short
            },
        )
        result = run_test(item)
        assert result.passed is False
        assert any("10 characters" in f for f in result.failures)

    def test_missing_display_name_fails(self) -> None:
        item = _make_item(
            object_type="TERM",
            operation="CREATE",
            after_snapshot={
                "definition": "A full and detailed definition of the term.",
            },
        )
        result = run_test(item)
        assert result.passed is False

    def test_delete_term_passes(self) -> None:
        item = _make_item(object_type="TERM", operation="DELETE")
        result = run_test(item)
        assert result.passed is True


class TestHarnessContextProduct:
    def test_valid_context_product_passes(self) -> None:
        item = _make_item(
            object_type="CONTEXT_PRODUCT",
            operation="CREATE",
            after_snapshot={
                "name": "Sales Dashboard",
                "description": "Provides sales metrics",
                "purpose": "Enable real-time sales monitoring",
                "allowed_consumer_roles": ["Analyst", "Manager"],
            },
        )
        result = run_test(item)
        assert result.passed is True

    def test_empty_consumer_roles_fails(self) -> None:
        item = _make_item(
            object_type="CONTEXT_PRODUCT",
            operation="CREATE",
            after_snapshot={
                "name": "Sales Dashboard",
                "description": "desc",
                "purpose": "purpose",
                "allowed_consumer_roles": [],
            },
        )
        result = run_test(item)
        assert result.passed is False
        assert any("consumer role" in f for f in result.failures)

    def test_missing_fields_fails(self) -> None:
        item = _make_item(
            object_type="CONTEXT_PRODUCT",
            operation="CREATE",
            after_snapshot={"name": "Sales Dashboard"},
        )
        result = run_test(item)
        assert result.passed is False

    def test_delete_context_product_passes(self) -> None:
        item = _make_item(object_type="CONTEXT_PRODUCT", operation="DELETE")
        result = run_test(item)
        assert result.passed is True


class TestHarnessUnknownType:
    def test_unknown_object_type_fails(self) -> None:
        item = _make_item(object_type="WIDGET", operation="CREATE")
        result = run_test(item)
        assert result.passed is False
        assert any("unknown" in f for f in result.failures)


# ---------------------------------------------------------------------------
# Test suite (aggregated)
# ---------------------------------------------------------------------------


class TestSuite:
    def test_all_passing_suite(self) -> None:
        cs = create_change_set("test", "user@example.com")
        cs = add_item(
            cs,
            _make_item(
                object_type="METRIC",
                object_id="revenue",
                operation="CREATE",
                after_snapshot={"name": "revenue", "aggregation": "SUM", "grain": "daily"},
            ),
        )
        cs = add_item(
            cs,
            _make_item(
                object_type="TERM",
                object_id="revenue-term",
                operation="CREATE",
                after_snapshot={
                    "display_name": "Revenue",
                    "definition": "Total income from all product sales.",
                },
            ),
        )

        result = run_test_suite(cs)
        assert isinstance(result, TestSuiteResult)
        assert result.passed is True
        assert result.evidence["total_items"] == 2
        assert result.evidence["passed_items"] == 2
        assert result.evidence["failed_items"] == 0
        assert result.completed_at is not None

        # Items should be marked PASSED
        for item in cs.items:
            assert item.test_status == "PASSED"

    def test_mixed_pass_fail_suite(self) -> None:
        cs = create_change_set("test", "user@example.com")
        cs = add_item(
            cs,
            _make_item(
                object_type="METRIC",
                object_id="good",
                operation="CREATE",
                after_snapshot={"name": "good", "aggregation": "SUM", "grain": "daily"},
            ),
        )
        cs = add_item(
            cs,
            _make_item(
                object_type="METRIC",
                object_id="bad",
                operation="CREATE",
                after_snapshot={"name": "bad"},  # missing required fields
            ),
        )

        result = run_test_suite(cs)
        assert result.passed is False
        assert result.evidence["passed_items"] == 1
        assert result.evidence["failed_items"] == 1

    def test_empty_change_set_suite_passes(self) -> None:
        cs = create_change_set("empty", "user@example.com")
        result = run_test_suite(cs)
        assert result.passed is True
        assert result.evidence["total_items"] == 0

    def test_suite_with_fixture(self) -> None:
        cs = create_change_set("test", "user@example.com")
        cs = add_item(
            cs,
            _make_item(
                object_type="TOOL",
                object_id="lookup",
                operation="CREATE",
                after_snapshot={
                    "name": "lookup",
                    "sql_template": "SELECT * FROM t WHERE id = :id",
                    "allowed_roles": ["Analyst"],
                    "parameters": [
                        {"name": "id", "parameter_type": "INTEGER", "required": True},
                    ],
                },
            ),
        )
        fixture = TestFixture(
            object_type="TOOL",
            object_id="lookup",
            synthetic_data={"test_parameters": {"id": 42}},
        )
        result = run_test_suite(cs, fixtures={"TOOL:lookup": fixture})
        assert result.passed is True

    def test_suite_records_item_results(self) -> None:
        cs = create_change_set("test", "user@example.com")
        cs = add_item(
            cs,
            _make_item(
                object_type="METRIC",
                object_id="m1",
                operation="CREATE",
                after_snapshot={"name": "m1", "aggregation": "SUM", "grain": "daily"},
            ),
        )
        result = run_test_suite(cs)
        assert len(result.item_results) == 1
        assert result.item_results[0]["passed"] is True
        assert result.item_results[0]["object_type"] == "METRIC"


# ---------------------------------------------------------------------------
# Schema / API route presence
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Verify Studio API routes are registered on the FastAPI app."""

    def test_studio_routes_present_in_openapi(self) -> None:
        try:
            from aida.main import app
        except ImportError as exc:
            pytest.skip(f"cannot import aida.main: {exc}")

        schema = app.openapi()
        paths = schema.get("paths", {})
        assert "/v1/studio/change-sets" in paths
        assert "/v1/studio/change-sets/{change_set_id}" in paths
        assert "/v1/studio/change-sets/{change_set_id}/items" in paths
        assert "/v1/studio/change-sets/{change_set_id}/test" in paths
        assert "/v1/studio/change-sets/{change_set_id}/submit" in paths
        assert "/v1/studio/change-sets/{change_set_id}/diff" in paths
        assert "/v1/studio/change-sets/{change_set_id}/impact" in paths
        assert "/v1/studio/change-sets/{change_set_id}/detect-conflicts" in paths

    def test_view_lineage_routes_present_in_openapi(self) -> None:
        try:
            from aida.main import app
        except ImportError as exc:
            pytest.skip(f"cannot import aida.main: {exc}")

        schema = app.openapi()
        paths = schema.get("paths", {})
        assert "/v1/datasources/{datasource_id}/view-lineage/parse" in paths
        assert "/v1/datasources/{datasource_id}/procedure-lineage/parse" in paths
        assert "/v1/datasources/{datasource_id}/view-lineage" in paths
        assert "/v1/datasources/{datasource_id}/procedure-lineage" in paths
