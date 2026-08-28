import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredConstraint,
    DiscoveredSchema,
    DiscoveredTable,
)
from aida.connectors.registry import ConnectorDefinition
from aida.models import DataSource
from aida.schemas import (
    MetadataCatalogEnvelope,
    MetadataIngestionChunkCreate,
    MetadataIngestionCreate,
)

INGESTION_CONTRACT_VERSION = "1.0"
CERTIFICATION_SUITE_VERSION = "connector-contract-v1"


def envelope_fingerprint(envelope: MetadataIngestionCreate) -> str:
    canonical = json.dumps(envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def envelope_counts(envelope: MetadataIngestionCreate) -> dict[str, int]:
    return catalog_counts(envelope.catalogs)


def catalog_counts(catalogs: list[MetadataCatalogEnvelope]) -> dict[str, int]:
    schemas = tables = columns = constraints = 0
    for catalog in catalogs:
        schemas += len(catalog.schemas)
        for schema in catalog.schemas:
            tables += len(schema.tables)
            for table in schema.tables:
                columns += len(table.columns)
                constraints += len(table.constraints)
    return {
        "catalogs": len(catalogs),
        "schemas": schemas,
        "tables": tables,
        "columns": columns,
        "constraints": constraints,
    }


def envelope_to_discovery(
    envelope: MetadataIngestionCreate,
) -> tuple[DiscoveredCatalog, ...]:
    return catalogs_to_discovery(envelope.catalogs)


def catalogs_to_discovery(
    catalogs: list[MetadataCatalogEnvelope],
) -> tuple[DiscoveredCatalog, ...]:
    return tuple(
        DiscoveredCatalog(
            name=catalog.name,
            attributes=dict(catalog.attributes),
            schemas=tuple(
                DiscoveredSchema(
                    name=schema.name,
                    attributes=dict(schema.attributes),
                    tables=tuple(
                        DiscoveredTable(
                            name=table.name,
                            object_type=table.object_type,
                            source_description=table.source_description,
                            attributes=dict(table.attributes),
                            columns=tuple(
                                DiscoveredColumn(
                                    name=column.name,
                                    ordinal_position=column.ordinal_position,
                                    physical_type=column.physical_type,
                                    nullable=column.nullable,
                                    default_expression=column.default_expression,
                                    attributes=dict(column.attributes),
                                )
                                for column in table.columns
                            ),
                            constraints=tuple(
                                DiscoveredConstraint(
                                    name=constraint.name,
                                    constraint_type=constraint.constraint_type,
                                    columns=tuple(constraint.columns),
                                    referenced_schema=constraint.referenced_schema,
                                    referenced_table=constraint.referenced_table,
                                    referenced_columns=tuple(constraint.referenced_columns),
                                )
                                for constraint in table.constraints
                            ),
                        )
                        for table in schema.tables
                    ),
                )
                for schema in catalog.schemas
            ),
        )
        for catalog in catalogs
    )


def chunk_fingerprint(chunk: MetadataIngestionChunkCreate) -> str:
    canonical = json.dumps(chunk.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_batch_chunks(
    chunks: list[MetadataIngestionChunkCreate], expected_chunks: int
) -> dict[str, int]:
    numbers = [chunk.chunk_number for chunk in chunks]
    if sorted(numbers) != list(range(1, expected_chunks + 1)):
        raise ValueError("batch chunks must be complete and numbered consecutively from one")
    table_keys: set[tuple[str, str, str]] = set()
    totals = {key: 0 for key in ("catalogs", "schemas", "tables", "columns", "constraints")}
    for chunk in chunks:
        counts = catalog_counts(chunk.catalogs)
        for key, value in counts.items():
            totals[key] += value
        for catalog in chunk.catalogs:
            for schema in catalog.schemas:
                for table in schema.tables:
                    table_key = (catalog.name, schema.name, table.name)
                    if table_key in table_keys:
                        raise ValueError(
                            "a table may appear in only one chunk: " + ".".join(table_key)
                        )
                    table_keys.add(table_key)
    return totals


def connector_certification_evidence(
    datasource: DataSource,
    definition: ConnectorDefinition,
    *,
    active_catalogs: int,
    active_tables: int,
) -> tuple[str, int, list[dict[str, Any]]]:
    capabilities = datasource.capabilities or {}
    checks = [
        _check(
            "implementation",
            definition.implementation_status == "IMPLEMENTED",
            definition.version,
        ),
        _check(
            "opaque_secret_reference",
            "://" in datasource.credential_reference,
            "reference only",
        ),
        _check(
            "connection_evidence",
            datasource.status in {"CONNECTION_VERIFIED", "ACTIVE"},
            datasource.status,
        ),
        _check(
            "hierarchy_contract",
            bool(capabilities.get("catalogs")) and bool(capabilities.get("schemas")),
            "catalog and schema capabilities required",
        ),
        _check(
            "inventory_evidence",
            active_catalogs > 0 and active_tables > 0,
            f"{active_catalogs} catalogs / {active_tables} tables",
        ),
        _check(
            "canonical_push_contract",
            "PUSH" in definition.transports,
            INGESTION_CONTRACT_VERSION,
        ),
    ]
    passed = sum(1 for check in checks if check["status"] == "PASS")
    score = round(passed * 100 / len(checks))
    if passed == len(checks):
        status = "CERTIFIED"
    elif score >= 67:
        status = "CONDITIONAL"
    else:
        status = "FAILED"
    return status, score, checks


def connector_definition_payload(
    definition: ConnectorDefinition, capabilities: dict[str, bool] | None = None
) -> dict[str, Any]:
    return definition.as_dict(capabilities=capabilities)


def _check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "evidence": evidence,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def default_capabilities(definition: ConnectorDefinition) -> dict[str, bool]:
    if definition.implementation_status != "IMPLEMENTED":
        return {}
    return dict(definition.capabilities)
