from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aida.connectors.registry import connector_registry
from aida.ingestion import (
    chunk_fingerprint,
    connector_certification_evidence,
    envelope_counts,
    envelope_fingerprint,
    envelope_to_discovery,
    validate_batch_chunks,
)
from aida.models import DataSource
from aida.schemas import MetadataIngestionChunkCreate, MetadataIngestionCreate
from aida.workflows.activities import SnapshotScope


def _envelope(**overrides: object) -> MetadataIngestionCreate:
    payload: dict[str, object] = {
        "idempotency_key": "inventory:2026-08-27:001",
        "producer": "bank-metadata-bridge",
        "transport": "PUSH",
        "snapshot_type": "FULL",
        "emitted_at": datetime.now(UTC),
        "catalogs": [
            {
                "name": "bank",
                "attributes": {"region": "us-east"},
                "schemas": [
                    {
                        "name": "customer",
                        "tables": [
                            {
                                "name": "account",
                                "object_type": "BASE_TABLE",
                                "columns": [
                                    {
                                        "name": "account_id",
                                        "ordinal_position": 1,
                                        "physical_type": "bigint",
                                        "nullable": False,
                                    },
                                    {
                                        "name": "customer_id",
                                        "ordinal_position": 2,
                                        "physical_type": "bigint",
                                        "nullable": False,
                                    },
                                ],
                                "constraints": [
                                    {
                                        "name": "account_pk",
                                        "constraint_type": "PRIMARY_KEY",
                                        "columns": ["account_id"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return MetadataIngestionCreate.model_validate(payload)


def test_metadata_envelope_is_canonical_and_counted() -> None:
    emitted_at = datetime(2026, 8, 27, tzinfo=UTC)
    envelope = _envelope(emitted_at=emitted_at)

    assert envelope_counts(envelope) == {
        "catalogs": 1,
        "schemas": 1,
        "tables": 1,
        "columns": 2,
        "constraints": 1,
        # Envelope 1.1 axes. Always reported, zero for a 1.0 payload, so a
        # consumer never has to tell "sent none" from "not counted".
        "views": 0,
        "routines": 0,
        "routine_parameters": 0,
        "grants": 0,
    }
    assert envelope_fingerprint(envelope) == envelope_fingerprint(_envelope(emitted_at=emitted_at))
    discovery = envelope_to_discovery(envelope)
    assert discovery[0].schemas[0].tables[0].columns[1].name == "customer_id"


def test_metadata_envelope_rejects_raw_value_attributes() -> None:
    with pytest.raises(ValidationError, match="value-free contract"):
        _envelope(
            catalogs=[
                {
                    "name": "bank",
                    "attributes": {"sample_rows": "must never be retained"},
                    "schemas": [],
                }
            ]
        )


def test_metadata_envelope_rejects_invalid_constraint_column() -> None:
    payload = _envelope().model_dump(mode="json")
    payload["catalogs"][0]["schemas"][0]["tables"][0]["constraints"][0]["columns"] = ["missing_id"]
    with pytest.raises(ValidationError, match="unknown local column"):
        MetadataIngestionCreate.model_validate(payload)


def test_connector_certification_is_deterministic() -> None:
    definition = connector_registry.definition("postgres")
    datasource = DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="Consumer warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="restricted-east",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
        max_concurrency=4,
        capabilities={"catalogs": True, "schemas": True},
    )

    status, score, checks = connector_certification_evidence(
        datasource, definition, active_catalogs=1, active_tables=10
    )

    assert status == "CERTIFIED"
    assert score == 100
    assert all(check["status"] == "PASS" for check in checks)


def test_registry_exposes_planned_connectors_without_claiming_implementation() -> None:
    teradata = connector_registry.definition("teradata")

    assert teradata.implementation_status == "PLANNED"
    assert "teradata" not in connector_registry.supported_types
    assert teradata.transports == ("PUSH",)


def test_registry_exposes_snowflake_as_implemented() -> None:
    snowflake = connector_registry.definition("snowflake")

    assert snowflake.implementation_status == "IMPLEMENTED"
    assert "snowflake" in connector_registry.supported_types
    assert "PULL" in snowflake.transports
    assert "PUSH" in snowflake.transports


def test_registry_exposes_databricks_as_implemented() -> None:
    """CN-2b: Databricks moved from `declare_planned` to a real pull adapter."""
    databricks = connector_registry.definition("databricks")

    assert databricks.implementation_status == "IMPLEMENTED"
    assert "databricks" in connector_registry.supported_types
    assert "PULL" in databricks.transports
    assert "PUSH" in databricks.transports


def test_registry_exposes_bigquery_as_implemented() -> None:
    bigquery = connector_registry.definition("bigquery")

    assert bigquery.implementation_status == "IMPLEMENTED"
    assert "bigquery" in connector_registry.supported_types
    assert "PULL" in bigquery.transports
    assert "PUSH" in bigquery.transports


def _chunk(number: int, key: str, *, table_name: str) -> MetadataIngestionChunkCreate:
    payload = _envelope().model_dump(mode="json")
    payload["catalogs"][0]["schemas"][0]["tables"][0]["name"] = table_name
    return MetadataIngestionChunkCreate.model_validate(
        {
            "chunk_number": number,
            "chunk_key": key,
            "emitted_at": payload["emitted_at"],
            "catalogs": payload["catalogs"],
        }
    )


def test_chunked_ingestion_contract_is_order_independent_and_checksum_safe() -> None:
    first = _chunk(1, "estate:chunk:0001", table_name="account")
    second = _chunk(2, "estate:chunk:0002", table_name="customer")

    assert validate_batch_chunks([second, first], expected_chunks=2) == {
        "catalogs": 2,
        "schemas": 2,
        "tables": 2,
        "columns": 4,
        "constraints": 2,
        "views": 0,
        "routines": 0,
        "routine_parameters": 0,
        "grants": 0,
    }
    assert chunk_fingerprint(first) == chunk_fingerprint(
        MetadataIngestionChunkCreate.model_validate(first.model_dump(mode="json"))
    )


def test_chunked_ingestion_rejects_missing_sequence_and_duplicate_tables() -> None:
    first = _chunk(1, "estate:chunk:0001", table_name="account")
    duplicate = _chunk(2, "estate:chunk:0002", table_name="account")

    with pytest.raises(ValueError, match="consecutively"):
        validate_batch_chunks([first], expected_chunks=2)
    with pytest.raises(ValueError, match="only one chunk"):
        validate_batch_chunks([first, duplicate], expected_chunks=2)


def test_snapshot_scope_reports_cross_chunk_inventory() -> None:
    scope = SnapshotScope(
        catalog_ids={uuid4()},
        schema_ids={uuid4(), uuid4()},
        table_ids={uuid4(), uuid4(), uuid4()},
        column_ids={uuid4()},
        constraint_ids=set(),
    )

    assert scope.object_counts() == {
        "catalogs": 1,
        "schemas": 2,
        "tables": 3,
        "columns": 1,
        "constraints": 0,
        "indexes": 0,
        "partitions": 0,
    }
