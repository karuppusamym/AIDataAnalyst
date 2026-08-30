import json
from pathlib import Path
from uuid import uuid4

import pytest

from aida.bi_api import _matched_table_id_for_column_ref
from aida.bi_lineage import (
    IMPLEMENTED_BI_TOOLS,
    BiLineageError,
    ParsedBiColumnRef,
    parse_bi_artifact,
)
from aida.main import app
from aida.schemas import BiMetricNodeRead

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tableau_metadata_sample.json"


def tableau_metadata_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text())


def test_tableau_parser_builds_report_metric_column_edges() -> None:
    parsed = parse_bi_artifact("tableau", tableau_metadata_fixture())

    assert parsed.bi_tool == "TABLEAU"
    assert parsed.generated_at is not None

    reports_by_id = {report.external_id: report for report in parsed.reports}
    assert set(reports_by_id) == {
        "wb-quarterly-revenue",
        "sheet-revenue-by-region",
        "dash-executive-summary",
    }
    assert reports_by_id["wb-quarterly-revenue"].report_type == "WORKBOOK"
    assert reports_by_id["wb-quarterly-revenue"].parent_external_id is None
    assert reports_by_id["sheet-revenue-by-region"].report_type == "SHEET"
    assert reports_by_id["sheet-revenue-by-region"].parent_external_id == "wb-quarterly-revenue"
    assert reports_by_id["dash-executive-summary"].report_type == "DASHBOARD"
    assert reports_by_id["dash-executive-summary"].parent_external_id == "wb-quarterly-revenue"

    metrics_by_id = {metric.external_id: metric for metric in parsed.metrics}
    assert set(metrics_by_id) == {"field-profit-ratio", "field-region"}
    calculated = metrics_by_id["field-profit-ratio"]
    assert calculated.field_type == "CalculatedField"
    assert calculated.formula_present is True
    assert calculated.formula_hash is not None
    column_field = metrics_by_id["field-region"]
    assert column_field.field_type == "ColumnField"
    assert column_field.formula_present is False
    assert column_field.formula_hash is None

    # Report -> metric edges: the workbook itself carries none (only its
    # sheets/dashboards reference fields directly); each is deduplicated
    # across repeated field instances (Profit Ratio appears in both the
    # sheet and the dashboard, but the edge is recorded once per report).
    report_metric_pairs = {
        (edge.report_external_id, edge.metric_external_id) for edge in parsed.report_metric_edges
    }
    assert report_metric_pairs == {
        ("sheet-revenue-by-region", "field-profit-ratio"),
        ("sheet-revenue-by-region", "field-region"),
        ("dash-executive-summary", "field-profit-ratio"),
        ("dash-executive-summary", "field-region"),
    }
    assert "wb-quarterly-revenue" not in {r for r, _ in report_metric_pairs}

    # Metric -> column edges: Profit Ratio derives from two physical columns,
    # Region from one — deduplicated even though it is used by two reports.
    metric_column_pairs = {
        (edge.metric_external_id, edge.column.table_name, edge.column.column_name)
        for edge in parsed.metric_column_edges
    }
    assert metric_column_pairs == {
        ("field-profit-ratio", "orders", "profit"),
        ("field-profit-ratio", "orders", "sales"),
        ("field-region", "customer", "region"),
    }
    region_edge = next(
        edge for edge in parsed.metric_column_edges if edge.metric_external_id == "field-region"
    )
    assert region_edge.column == ParsedBiColumnRef(
        database_name="bank",
        schema_name="raw",
        table_name="customer",
        column_name="region",
    )


def test_tableau_parser_is_deterministic() -> None:
    first = parse_bi_artifact("tableau", tableau_metadata_fixture())
    second = parse_bi_artifact("tableau", tableau_metadata_fixture())

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_raw_formula_text_is_never_part_of_the_read_contract() -> None:
    schema = BiMetricNodeRead.model_json_schema()["properties"]
    assert "formula" not in schema
    assert "formula_hash" in schema
    assert "formula_present" in schema


def test_parser_rejects_non_object_artifact() -> None:
    with pytest.raises(BiLineageError, match="must be a JSON object"):
        parse_bi_artifact("tableau", ["not", "an", "object"])  # type: ignore[arg-type]


def test_parser_rejects_missing_workbooks() -> None:
    with pytest.raises(BiLineageError, match="workbooks array"):
        parse_bi_artifact("tableau", {"data": {}})


def test_tableau_is_the_only_implemented_bi_tool_today() -> None:
    assert IMPLEMENTED_BI_TOOLS == frozenset({"TABLEAU"})


def test_power_bi_and_looker_are_accepted_names_but_not_yet_implemented() -> None:
    for tool in ("POWER_BI", "LOOKER"):
        with pytest.raises(BiLineageError, match="not yet implemented"):
            parse_bi_artifact(tool, {"data": {"workbooks": []}})


def test_unknown_bi_tool_is_rejected() -> None:
    with pytest.raises(BiLineageError, match="unsupported bi tool"):
        parse_bi_artifact("crystal_reports", {"data": {"workbooks": []}})


def test_column_matching_prefers_exact_database_schema_table() -> None:
    exact_table_id = uuid4()
    fallback_table_id = uuid4()
    column_ref = ParsedBiColumnRef(
        database_name="BANK",
        schema_name="RAW",
        table_name="customer",
        column_name="region",
    )

    assert (
        _matched_table_id_for_column_ref(
            column_ref,
            {("bank", "raw", "customer"): exact_table_id},
            {("raw", "customer"): fallback_table_id},
        )
        == exact_table_id
    )


def test_column_matching_falls_back_to_unambiguous_schema_table_without_database() -> None:
    fallback_table_id = uuid4()
    column_ref = ParsedBiColumnRef(
        database_name=None,
        schema_name="public",
        table_name="orders",
        column_name="profit",
    )

    assert (
        _matched_table_id_for_column_ref(column_ref, {}, {("public", "orders"): fallback_table_id})
        == fallback_table_id
    )


def test_column_matching_returns_none_when_nothing_matches() -> None:
    column_ref = ParsedBiColumnRef(
        database_name="bank",
        schema_name="raw",
        table_name="unknown_table",
        column_name="unknown_column",
    )

    assert _matched_table_id_for_column_ref(column_ref, {}, {}) is None


def test_bi_lineage_paths_are_published() -> None:
    paths = app.openapi()["paths"]

    assert "/v1/projects/{project_id}/bi-connections" in paths
    assert "/v1/bi-connections/{connection_id}/artifact-imports" in paths
    assert "/v1/bi-artifact-imports/{artifact_id}/reports" in paths
    assert "/v1/bi-artifact-imports/{artifact_id}/lineage" in paths


def test_bi_integration_toggle_is_present_in_the_shared_integration_catalog() -> None:
    from aida.integration_catalog import default_transformation_metadata_integrations

    assert default_transformation_metadata_integrations()["bi"] is False
