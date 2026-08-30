import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from aida import openlineage
from aida.openlineage import (
    OpenLineageError,
    parse_openlineage_run_event,
)


def valid_event_fixture() -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-01-15T12:30:00Z",
        "producer": "https://github.com/apache/airflow/tree/providers-openlineage",
        "schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/definitions/RunEvent",
        "job": {
            "namespace": "airflow",
            "name": "orders_etl.build_order_summary",
            "facets": {
                "documentation": {"description": "builds the order summary table"},
            },
        },
        "run": {
            "runId": "d46e465b-d358-4d32-83d4-df660ff614dd",
            "facets": {
                "nominalTime": {"nominalStartTime": "2026-01-15T12:00:00Z"},
            },
        },
        "inputs": [
            {
                "namespace": "snowflake://acct",
                "name": "raw.orders",
                "facets": {
                    "schema": {
                        "fields": [
                            {"name": "order_id", "type": "NUMBER"},
                            {"name": "amount", "type": "NUMBER"},
                        ]
                    }
                },
            },
            {
                "namespace": "snowflake://acct",
                "name": "raw.customers",
                "facets": {
                    "schema": {"fields": [{"name": "customer_id", "type": "NUMBER"}]}
                },
            },
        ],
        "outputs": [
            {
                "namespace": "snowflake://acct",
                "name": "analytics.order_summary",
                "facets": {
                    "schema": {
                        "fields": [
                            {"name": "customer_id", "type": "NUMBER"},
                            {"name": "total_amount", "type": "NUMBER"},
                        ]
                    },
                    "columnLineage": {
                        "fields": {
                            "total_amount": {
                                "inputFields": [
                                    {
                                        "namespace": "snowflake://acct",
                                        "name": "raw.orders",
                                        "field": "amount",
                                        "transformations": [
                                            {"type": "AGGREGATION", "subtype": "SUM"},
                                            {"type": "DIRECT", "subtype": "IDENTITY"},
                                        ],
                                    }
                                ]
                            },
                            "customer_id": {
                                "inputFields": [
                                    {
                                        "namespace": "snowflake://acct",
                                        "name": "raw.customers",
                                        "field": "customer_id",
                                    }
                                ]
                            },
                        }
                    },
                },
            }
        ],
    }


def canonical_json(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ---------------------------------------------------------------------------
# Happy path: full shape, fingerprint, table edges, column edges
# ---------------------------------------------------------------------------


def test_valid_event_parses_expected_shape() -> None:
    parsed = parse_openlineage_run_event(valid_event_fixture())

    assert parsed.event_type == "COMPLETE"
    assert parsed.event_time == datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC)
    assert parsed.producer == "https://github.com/apache/airflow/tree/providers-openlineage"
    assert parsed.schema_url == (
        "https://openlineage.io/spec/1-0-5/OpenLineage.json#/definitions/RunEvent"
    )
    assert parsed.job_namespace == "airflow"
    assert parsed.job_name == "orders_etl.build_order_summary"
    assert parsed.run_id == "d46e465b-d358-4d32-83d4-df660ff614dd"
    assert len(parsed.inputs) == 2
    assert len(parsed.outputs) == 1
    assert parsed.inputs[0].schema_fields == ["order_id", "amount"]
    assert parsed.outputs[0].schema_fields == ["customer_id", "total_amount"]


def test_fingerprint_is_deterministic_sha256_of_canonical_json() -> None:
    event = valid_event_fixture()
    expected = hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()

    parsed = parse_openlineage_run_event(event)

    assert parsed.fingerprint == expected
    assert len(parsed.fingerprint) == 64
    int(parsed.fingerprint, 16)  # must be valid hex


def test_fingerprint_is_stable_across_key_order_and_repeated_calls() -> None:
    event_a = valid_event_fixture()
    # Same content, keys inserted in a different order -- canonicalization
    # (sort_keys=True) must make the fingerprint independent of dict order.
    event_b = {
        "producer": event_a["producer"],
        "outputs": deepcopy(event_a["outputs"]),
        "job": deepcopy(event_a["job"]),
        "eventTime": event_a["eventTime"],
        "inputs": deepcopy(event_a["inputs"]),
        "run": deepcopy(event_a["run"]),
        "eventType": event_a["eventType"],
        "schemaURL": event_a["schemaURL"],
    }

    first = parse_openlineage_run_event(deepcopy(event_a))
    second = parse_openlineage_run_event(deepcopy(event_a))
    third = parse_openlineage_run_event(event_b)

    assert first.fingerprint == second.fingerprint == third.fingerprint


def test_table_edges_are_full_cross_product_of_inputs_and_outputs() -> None:
    event = valid_event_fixture()
    event["outputs"].append(
        {"namespace": "snowflake://acct", "name": "analytics.order_summary_v2"}
    )

    parsed = parse_openlineage_run_event(event)

    assert len(parsed.table_edges) == len(parsed.inputs) * len(parsed.outputs) == 4
    pairs = {
        (edge.input_dataset_name, edge.output_dataset_name) for edge in parsed.table_edges
    }
    assert pairs == {
        ("raw.orders", "analytics.order_summary"),
        ("raw.orders", "analytics.order_summary_v2"),
        ("raw.customers", "analytics.order_summary"),
        ("raw.customers", "analytics.order_summary_v2"),
    }
    for edge in parsed.table_edges:
        assert edge.input_dataset_namespace == "snowflake://acct"
        assert edge.output_dataset_namespace == "snowflake://acct"


def test_table_edges_empty_when_no_inputs_or_no_outputs() -> None:
    event = valid_event_fixture()
    event["inputs"] = []

    parsed = parse_openlineage_run_event(event)

    assert parsed.table_edges == []
    # Column edges are derived purely from the outputs' columnLineage facet,
    # independent of whether any inputs were present.
    assert len(parsed.column_edges) == 2
    assert len(parsed.outputs) == 1


def test_column_edges_derived_from_column_lineage_facet() -> None:
    parsed = parse_openlineage_run_event(valid_event_fixture())

    assert len(parsed.column_edges) == 2
    by_output_column = {edge.output_column_name: edge for edge in parsed.column_edges}

    total_amount_edge = by_output_column["total_amount"]
    assert total_amount_edge.input_dataset_namespace == "snowflake://acct"
    assert total_amount_edge.input_dataset_name == "raw.orders"
    assert total_amount_edge.input_column_name == "amount"
    assert total_amount_edge.output_dataset_namespace == "snowflake://acct"
    assert total_amount_edge.output_dataset_name == "analytics.order_summary"

    customer_id_edge = by_output_column["customer_id"]
    assert customer_id_edge.input_dataset_name == "raw.customers"
    assert customer_id_edge.input_column_name == "customer_id"


def test_column_edge_transformation_taken_from_first_transformation_only() -> None:
    parsed = parse_openlineage_run_event(valid_event_fixture())
    by_output_column = {edge.output_column_name: edge for edge in parsed.column_edges}

    # The "total_amount" input field lists AGGREGATION/SUM before DIRECT/IDENTITY;
    # only the first entry should be used.
    total_amount_edge = by_output_column["total_amount"]
    assert total_amount_edge.transformation_type == "AGGREGATION"
    assert total_amount_edge.transformation_subtype == "SUM"

    # "customer_id" input field has no transformations at all.
    customer_id_edge = by_output_column["customer_id"]
    assert customer_id_edge.transformation_type is None
    assert customer_id_edge.transformation_subtype is None


def test_column_edge_skips_malformed_input_fields_without_raising() -> None:
    event = valid_event_fixture()
    event["outputs"][0]["facets"]["columnLineage"] = {
        "fields": {
            "good_column": {
                "inputFields": [
                    {"namespace": "ns", "name": "t", "field": "c"},
                    {"namespace": "ns", "name": "t"},  # missing field -> skipped
                    "not-a-dict",  # skipped
                    {"namespace": "ns"},  # missing name/field -> skipped
                ]
            },
            "ignored_column": {"inputFields": "not-a-list"},
        }
    }

    parsed = parse_openlineage_run_event(event)

    assert len(parsed.column_edges) == 1
    assert parsed.column_edges[0].output_column_name == "good_column"
    assert parsed.column_edges[0].input_column_name == "c"


# ---------------------------------------------------------------------------
# Structural rejections: job / run / eventTime / inputs / outputs
# ---------------------------------------------------------------------------


def test_rejects_non_dict_event() -> None:
    with pytest.raises(OpenLineageError, match="JSON object"):
        parse_openlineage_run_event("not-a-dict")  # type: ignore[arg-type]


def test_rejects_event_that_is_not_json_serializable() -> None:
    event = valid_event_fixture()
    event["job"]["facets"]["not_json_serializable"] = {1, 2, 3}

    with pytest.raises(OpenLineageError, match="valid JSON data"):
        parse_openlineage_run_event(event)


def test_rejects_missing_job() -> None:
    event = valid_event_fixture()
    del event["job"]
    with pytest.raises(OpenLineageError, match="job is required"):
        parse_openlineage_run_event(event)


def test_rejects_non_dict_job() -> None:
    event = valid_event_fixture()
    event["job"] = "orders_etl"
    with pytest.raises(OpenLineageError, match="job is required"):
        parse_openlineage_run_event(event)


def test_rejects_missing_run() -> None:
    event = valid_event_fixture()
    del event["run"]
    with pytest.raises(OpenLineageError, match="run is required"):
        parse_openlineage_run_event(event)


def test_rejects_non_dict_run() -> None:
    event = valid_event_fixture()
    event["run"] = "not-a-run"
    with pytest.raises(OpenLineageError, match="run is required"):
        parse_openlineage_run_event(event)


def test_rejects_missing_job_namespace_or_name() -> None:
    event = valid_event_fixture()
    del event["job"]["namespace"]
    with pytest.raises(OpenLineageError, match="job.namespace"):
        parse_openlineage_run_event(event)

    event = valid_event_fixture()
    del event["job"]["name"]
    with pytest.raises(OpenLineageError, match="job.name"):
        parse_openlineage_run_event(event)


def test_rejects_missing_run_id() -> None:
    event = valid_event_fixture()
    del event["run"]["runId"]
    with pytest.raises(OpenLineageError, match="run.runId"):
        parse_openlineage_run_event(event)


def test_rejects_missing_event_time() -> None:
    event = valid_event_fixture()
    del event["eventTime"]
    with pytest.raises(OpenLineageError, match="eventTime is required"):
        parse_openlineage_run_event(event)


def test_rejects_unparseable_event_time() -> None:
    event = valid_event_fixture()
    event["eventTime"] = "not-a-timestamp"
    with pytest.raises(OpenLineageError, match="ISO-8601"):
        parse_openlineage_run_event(event)


def test_rejects_non_array_inputs() -> None:
    event = valid_event_fixture()
    event["inputs"] = {"namespace": "x", "name": "y"}
    with pytest.raises(OpenLineageError, match="must be arrays"):
        parse_openlineage_run_event(event)


def test_rejects_non_array_outputs() -> None:
    event = valid_event_fixture()
    event["outputs"] = "not-a-list"
    with pytest.raises(OpenLineageError, match="must be arrays"):
        parse_openlineage_run_event(event)


def test_non_dict_items_in_inputs_or_outputs_are_silently_skipped() -> None:
    event = valid_event_fixture()
    event["inputs"].append("not-a-dataset")
    event["outputs"].append(123)

    parsed = parse_openlineage_run_event(event)

    assert len(parsed.inputs) == 2
    assert len(parsed.outputs) == 1


def test_missing_inputs_and_outputs_default_to_empty() -> None:
    event = valid_event_fixture()
    del event["inputs"]
    del event["outputs"]

    parsed = parse_openlineage_run_event(event)

    assert parsed.inputs == []
    assert parsed.outputs == []
    assert parsed.table_edges == []
    assert parsed.column_edges == []


def test_rejects_dataset_missing_namespace() -> None:
    event = valid_event_fixture()
    del event["inputs"][0]["namespace"]
    with pytest.raises(OpenLineageError, match="dataset.namespace"):
        parse_openlineage_run_event(event)


def test_rejects_dataset_missing_name() -> None:
    event = valid_event_fixture()
    del event["outputs"][0]["name"]
    with pytest.raises(OpenLineageError, match="dataset.name"):
        parse_openlineage_run_event(event)


# ---------------------------------------------------------------------------
# eventType normalization / rejection
# ---------------------------------------------------------------------------


def test_event_type_is_upper_cased() -> None:
    event = valid_event_fixture()
    event["eventType"] = "complete"
    parsed = parse_openlineage_run_event(event)
    assert parsed.event_type == "COMPLETE"


def test_rejects_missing_event_type() -> None:
    event = valid_event_fixture()
    del event["eventType"]
    with pytest.raises(OpenLineageError, match="eventType is required"):
        parse_openlineage_run_event(event)


def test_rejects_blank_event_type() -> None:
    event = valid_event_fixture()
    event["eventType"] = "   "
    with pytest.raises(OpenLineageError, match="eventType is required"):
        parse_openlineage_run_event(event)


# ---------------------------------------------------------------------------
# eventTime timezone handling
# ---------------------------------------------------------------------------


def test_event_time_z_suffix_and_explicit_utc_offset_match() -> None:
    event_z = valid_event_fixture()
    event_z["eventTime"] = "2026-01-15T12:30:00Z"
    event_offset = valid_event_fixture()
    event_offset["eventTime"] = "2026-01-15T12:30:00+00:00"

    parsed_z = parse_openlineage_run_event(event_z)
    parsed_offset = parse_openlineage_run_event(event_offset)

    assert parsed_z.event_time == parsed_offset.event_time
    assert parsed_z.event_time.utcoffset().total_seconds() == 0


def test_event_time_non_utc_offset_normalizes_to_same_instant() -> None:
    event = valid_event_fixture()
    event["eventTime"] = "2026-01-15T17:30:00+05:00"

    parsed = parse_openlineage_run_event(event)

    assert parsed.event_time == datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC)


def test_naive_event_time_gets_utc_attached() -> None:
    event = valid_event_fixture()
    event["eventTime"] = "2026-01-15T12:30:00"

    parsed = parse_openlineage_run_event(event)

    assert parsed.event_time.tzinfo is UTC
    assert parsed.event_time == datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Facet validation: disallowed dataset facets, forbidden fragments, nesting,
# oversized lists/strings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "facet_key",
    ["dataQualityAssertions", "DATAQUALITYMETRICS", "OutputStatistics"],
)
def test_rejects_disallowed_dataset_facets_case_insensitively(facet_key: str) -> None:
    event = valid_event_fixture()
    event["outputs"][0]["facets"][facet_key] = {"anything": True}

    with pytest.raises(OpenLineageError, match="value-free contract"):
        parse_openlineage_run_event(event)


@pytest.mark.parametrize(
    "fragment", ["sample", "payload", "password", "secret", "token", "credential"]
)
def test_rejects_forbidden_facet_key_fragment_anywhere_in_nested_facets(fragment: str) -> None:
    event = valid_event_fixture()
    event["outputs"][0]["facets"]["custom"] = {"outer": {f"inner{fragment.title()}Value": "x"}}

    with pytest.raises(OpenLineageError, match="not permitted"):
        parse_openlineage_run_event(event)


def test_rejects_forbidden_facet_key_fragment_in_job_facets() -> None:
    event = valid_event_fixture()
    event["job"]["facets"]["ownership"] = {"apiTokenRef": "x"}

    with pytest.raises(OpenLineageError, match="not permitted"):
        parse_openlineage_run_event(event)


def test_rejects_forbidden_facet_key_fragment_in_run_facets() -> None:
    event = valid_event_fixture()
    event["run"]["facets"]["custom"] = {"secretValue": "x"}

    with pytest.raises(OpenLineageError, match="not permitted"):
        parse_openlineage_run_event(event)


def test_rejects_facet_nesting_deeper_than_max_depth() -> None:
    event = valid_event_fixture()
    nested: dict[str, Any] = {"leaf": "value"}
    for _ in range(14):
        nested = {"level": nested}
    event["outputs"][0]["facets"]["custom"] = nested

    with pytest.raises(OpenLineageError, match="maximum nesting depth"):
        parse_openlineage_run_event(event)


def test_rejects_oversized_facet_list() -> None:
    event = valid_event_fixture()
    event["outputs"][0]["facets"]["custom"] = {"items": list(range(10_001))}

    with pytest.raises(OpenLineageError, match="maximum list size"):
        parse_openlineage_run_event(event)


def test_rejects_oversized_facet_string() -> None:
    event = valid_event_fixture()
    event["outputs"][0]["facets"]["custom"] = {"note": "x" * 10_001}

    with pytest.raises(OpenLineageError, match="maximum size"):
        parse_openlineage_run_event(event)


def test_allows_facet_list_and_string_at_the_boundary() -> None:
    event = valid_event_fixture()
    event["outputs"][0]["facets"]["custom"] = {
        "items": list(range(10_000)),
        "note": "x" * 10_000,
    }

    parsed = parse_openlineage_run_event(event)
    assert parsed.outputs[0].facets["custom"]["note"] == "x" * 10_000


# ---------------------------------------------------------------------------
# Safety boundaries: overall event size, dataset/edge counts.
# ---------------------------------------------------------------------------


def test_rejects_event_exceeding_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openlineage, "MAX_OPENLINEAGE_EVENT_BYTES", 200)
    event = valid_event_fixture()

    with pytest.raises(OpenLineageError, match="1 MiB ingestion limit"):
        parse_openlineage_run_event(event)


def test_rejects_dataset_count_exceeding_safety_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openlineage, "MAX_OPENLINEAGE_DATASETS", 2)
    event = valid_event_fixture()  # 2 inputs + 1 output == 3 datasets > 2

    with pytest.raises(OpenLineageError, match="dataset safety boundary"):
        parse_openlineage_run_event(event)


def test_rejects_table_edge_count_exceeding_safety_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openlineage, "MAX_OPENLINEAGE_TABLE_EDGES", 1)
    event = valid_event_fixture()  # 2 inputs x 1 output == 2 table edges > 1

    with pytest.raises(OpenLineageError, match="table-edge safety boundary"):
        parse_openlineage_run_event(event)


def test_rejects_column_edge_count_exceeding_safety_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openlineage, "MAX_OPENLINEAGE_COLUMN_EDGES", 1)
    event = valid_event_fixture()  # 2 column edges (total_amount, customer_id) > 1

    with pytest.raises(OpenLineageError, match="column-edge safety boundary"):
        parse_openlineage_run_event(event)
