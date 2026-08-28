"""
Google BigQuery Connector
=========================

Implements the Atlas ``Connector`` ABC for Google BigQuery using the official
``google-cloud-bigquery`` Python client library with strict governance,
fail-closed validation, dry-run byte estimation, and value-free metadata discovery.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from aida.connectors.base import (
    ColumnProfileSnapshot,
    Connector,
    ConnectorCapabilities,
    DiscoveredCatalog,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    append_grouped_key_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
)

_COMPLEX_SCALAR_TYPES = frozenset({"JSON", "STRUCT", "RECORD", "GEOGRAPHY", "BYTES"})
_VALID_AUTH_METHODS = frozenset({"service_account", "workload_identity"})
_REQUIRED_SERVICE_ACCOUNT_KEYS = ("type", "client_email", "private_key", "token_uri")
_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
_LOCATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")


@dataclass(frozen=True, slots=True)
class _CredentialConfig:
    project_id: str
    location: str
    auth_method: str
    service_account_info: dict[str, Any] | None = None


def _quote_identifier(identifier: str) -> str:
    """BigQuery backtick-quote an identifier."""
    return "`" + identifier.replace("`", "\\`") + "`"


def _qualified_table(project: str, dataset: str, table: str) -> str:
    """Format a fully-qualified 3-part BigQuery table identifier."""
    return f"{_quote_identifier(project)}.{_quote_identifier(dataset)}.{_quote_identifier(table)}"


def _region_dataset(location: str) -> str:
    """Normalize a BigQuery region/location string into an INFORMATION_SCHEMA dataset prefix."""
    normalized = location.strip().lower()
    return f"region-{normalized}"


def _parse_credential_payload(payload: str) -> _CredentialConfig:
    """Strictly validate and parse the resolved secret credential JSON.

    Rejects ambiguous, extra, or incomplete fields to maintain fail-closed security.
    """
    try:
        data = json.loads(payload)
    except Exception as exc:
        raise ValueError(f"BigQuery credential payload must be valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("BigQuery credential payload must be a JSON object")

    allowed_keys = {"project_id", "location", "auth_method", "service_account_info"}
    extra_keys = set(data.keys()) - allowed_keys
    if extra_keys:
        raise ValueError(f"Unexpected fields in BigQuery credential payload: {extra_keys}")

    project_id = data.get("project_id")
    location = data.get("location")
    auth_method = data.get("auth_method")

    if not project_id or not isinstance(project_id, str):
        raise ValueError("BigQuery credential payload requires a non-empty string 'project_id'")
    if not location or not isinstance(location, str):
        raise ValueError("BigQuery credential payload requires a non-empty string 'location'")
    if _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ValueError("BigQuery project_id has an invalid format")
    if _LOCATION_PATTERN.fullmatch(location) is None:
        raise ValueError("BigQuery location has an invalid format")
    if auth_method not in _VALID_AUTH_METHODS:
        raise ValueError(
            f"Invalid auth_method '{auth_method}'; expected one of {_VALID_AUTH_METHODS}"
        )

    sa_info = data.get("service_account_info")
    if auth_method == "service_account":
        if not isinstance(sa_info, dict):
            raise ValueError(
                "auth_method 'service_account' requires a 'service_account_info' object"
            )
        if sa_info.get("type") != "service_account":
            raise ValueError("service_account_info 'type' field must equal 'service_account'")
        for req_key in _REQUIRED_SERVICE_ACCOUNT_KEYS:
            if not sa_info.get(req_key):
                raise ValueError(f"service_account_info missing required key: {req_key}")
    elif auth_method == "workload_identity":
        if sa_info is not None:
            raise ValueError(
                "auth_method 'workload_identity' must not include 'service_account_info'"
            )

    return _CredentialConfig(
        project_id=project_id,
        location=location,
        auth_method=auth_method,
        service_account_info=sa_info,
    )


def _profile_expressions(
    quoted_column: str, position: int, data_type: str, mode: str
) -> list[str]:
    """Generate per-column aggregate expressions for bounded BigQuery profiling."""
    if mode == "REPEATED":
        return [
            f"CAST(NULL AS INT64) AS n_{position}",
            f"CAST(NULL AS INT64) AS nn_{position}",
            f"CAST(0 AS INT64) AS d_{position}",
            f"CAST(NULL AS INT64) AS minl_{position}",
            f"CAST(NULL AS INT64) AS maxl_{position}",
        ]

    upper_type = data_type.upper()
    if upper_type in _COMPLEX_SCALAR_TYPES:
        return [
            f"SUM(CASE WHEN {quoted_column} IS NULL THEN 1 ELSE 0 END) AS n_{position}",
            f"COUNT({quoted_column}) AS nn_{position}",
            f"CAST(0 AS INT64) AS d_{position}",
            f"CAST(NULL AS INT64) AS minl_{position}",
            f"CAST(NULL AS INT64) AS maxl_{position}",
        ]

    return [
        f"SUM(CASE WHEN {quoted_column} IS NULL THEN 1 ELSE 0 END) AS n_{position}",
        f"COUNT({quoted_column}) AS nn_{position}",
        f"APPROX_COUNT_DISTINCT({quoted_column}) AS d_{position}",
        f"MIN(LENGTH(CAST({quoted_column} AS STRING))) AS minl_{position}",
        f"MAX(LENGTH(CAST({quoted_column} AS STRING))) AS maxl_{position}",
    ]


def _assemble_catalog(
    project_id: str,
    column_rows: list[dict[str, Any]],
    key_rows: list[dict[str, Any]],
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble a standard DiscoveredCatalog from discovered columns and primary/unique key rows."""
    tables = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(
        tables,
        key_rows,
        constraint_type_map={"PRIMARY KEY": "PRIMARY_KEY", "UNIQUE": "UNIQUE"},
    )
    return assemble_catalog(project_id, tables)


class BigQueryConnector(Connector):
    """Governed Google BigQuery connector with dry-run byte estimation and bounded profiling."""

    connector_type = "bigquery"
    dialect = "bigquery"

    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        catalogs=True,
        schemas=True,
        constraints=True,
        indexes=False,
        partitions=False,
        explain=True,
        query_history=False,
        delegated_identity=False,
        approximate_statistics=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._config = _parse_credential_payload(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    def _get_client(self) -> Any:
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise RuntimeError(
                "google-cloud-bigquery is required for BigQuery operations. "
                "Install with: pip install 'google-cloud-bigquery>=3.28'"
            ) from exc

        if self._config.auth_method == "service_account" and self._config.service_account_info:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                self._config.service_account_info
            )
            return bigquery.Client(
                project=self._config.project_id,
                location=self._config.location,
                credentials=credentials,
            )

        return bigquery.Client(
            project=self._config.project_id,
            location=self._config.location,
        )

    async def test_connection(self) -> None:
        def _test() -> None:
            client = self._get_client()
            next(iter(client.list_datasets(project=self._config.project_id, max_results=1)), None)

        await asyncio.to_thread(_test)

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        def _discover() -> tuple[DiscoveredCatalog, ...]:
            client = self._get_client()
            region = _region_dataset(self._config.location)
            cols_query = f"""
                SELECT
                    table_schema,
                    table_name,
                    'BASE TABLE' AS table_type,
                    column_name,
                    ordinal_position,
                    data_type,
                    is_nullable,
                    column_default
                FROM `{self._config.project_id}`.`{region}`.INFORMATION_SCHEMA.COLUMNS
                ORDER BY table_schema, table_name, ordinal_position
            """  # noqa: S608 -- credential identifiers are strictly validated
            column_rows = [dict(row.items()) for row in client.query(cols_query).result()]

            keys_query = f"""
                SELECT
                    table_schema,
                    table_name,
                    constraint_name,
                    constraint_type,
                    column_name
                FROM `{self._config.project_id}`.`{region}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                ORDER BY table_schema, table_name, constraint_name, ordinal_position
            """  # noqa: S608 -- credential identifiers are strictly validated
            try:
                key_rows = [dict(row.items()) for row in client.query(keys_query).result()]
            except Exception:
                key_rows = []

            return _assemble_catalog(self._config.project_id, column_rows, key_rows)

        return await asyncio.to_thread(_discover)

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        def _estimate() -> QueryEstimate:
            from google.cloud import bigquery

            client = self._get_client()
            job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
            job = client.query(sql, job_config=job_config, timeout=timeout_seconds)
            bytes_processed = int(job.total_bytes_processed or 0)
            return QueryEstimate(
                score=float(bytes_processed),
                kind="BIGQUERY_DRY_RUN_BYTES",
                estimated_bytes=bytes_processed,
                evidence={
                    "project_id": self._config.project_id,
                    "location": self._config.location,
                    "bytes_processed": bytes_processed,
                },
            )

        return await asyncio.to_thread(_estimate)

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        def _execute() -> QueryResult:
            from google.cloud import bigquery

            client = self._get_client()
            job_config = bigquery.QueryJobConfig(use_query_cache=False)
            job = client.query(sql, job_config=job_config, timeout=timeout_seconds)
            rows = tuple(dict(row.items()) for row in job.result(timeout=timeout_seconds))
            return QueryResult(
                rows=rows,
                warehouse_query_id=str(job.job_id),
            )

        return await asyncio.to_thread(_execute)

    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        if not column_names:
            return TableProfileSnapshot(None, 0, ())
        if sample_rows < 1 or column_batch_size < 1:
            raise ValueError("profiling limits must be positive")

        def _profile() -> TableProfileSnapshot:
            from google.cloud import bigquery

            client = self._get_client()
            table_ref = _qualified_table(self._config.project_id, schema_name, table_name)
            region = _region_dataset(self._config.location)

            schema_query = f"""
                SELECT column_name, data_type, is_nullable
                FROM `{self._config.project_id}`.`{region}`.INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = @schema_name AND table_name = @table_name
            """  # noqa: S608 -- credential identifiers are strictly validated
            schema_job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("schema_name", "STRING", schema_name),
                    bigquery.ScalarQueryParameter("table_name", "STRING", table_name),
                ]
            )
            schema_rows = client.query(
                schema_query,
                job_config=schema_job_config,
                timeout=timeout_seconds,
            ).result()
            schema_info = {
                row["column_name"]: (
                    row["data_type"],
                    "REPEATED" if "ARRAY" in row["data_type"].upper() else "NULLABLE",
                )
                for row in [dict(result.items()) for result in schema_rows]
            }

            snapshots: list[ColumnProfileSnapshot] = []
            sampled_row_count = 0

            for start in range(0, len(column_names), column_batch_size):
                batch = column_names[start : start + column_batch_size]
                selected = ", ".join(_quote_identifier(n) for n in batch)
                expressions = ["COUNT(*) AS sampled_row_count"]

                for position, name in enumerate(batch):
                    quoted = _quote_identifier(name)
                    data_type, mode = schema_info.get(name, ("STRING", "NULLABLE"))
                    expressions.extend(_profile_expressions(quoted, position, data_type, mode))

                profile_sql = f"""
                    WITH bounded_sample AS (
                        SELECT {selected} FROM {table_ref} LIMIT {int(sample_rows)}
                    )
                    SELECT {', '.join(expressions)} FROM bounded_sample
                """  # noqa: S608 -- identifiers are quoted; limits are validated integers
                profile_rows = client.query(profile_sql, timeout=timeout_seconds).result()
                row_dict = dict(next(iter(profile_rows)).items())
                sampled_row_count = max(
                    sampled_row_count,
                    int(row_dict.get("sampled_row_count", 0)),
                )

                for position, name in enumerate(batch):
                    snapshots.append(
                        ColumnProfileSnapshot(
                            name=name,
                            null_count=int(row_dict.get(f"n_{position}") or 0),
                            non_null_count=int(row_dict.get(f"nn_{position}") or 0),
                            approximate_distinct_count=int(row_dict.get(f"d_{position}") or 0),
                            min_length=row_dict.get(f"minl_{position}"),
                            max_length=row_dict.get(f"maxl_{position}"),
                        )
                    )

            return TableProfileSnapshot(
                row_count_estimate=sampled_row_count,
                sampled_row_count=sampled_row_count,
                columns=tuple(snapshots),
            )

        return await asyncio.to_thread(_profile)
