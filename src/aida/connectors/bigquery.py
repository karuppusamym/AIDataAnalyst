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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from aida.connectors.base import (
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredViewDefinition,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    append_grouped_key_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
)
from aida.connectors.sql_execution import SqlExecutor

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


# --- Envelope 1.1 (gap/02 N1) ------------------------------------------------
#
# BigQuery answers three of the four envelope axes from region-qualified
# INFORMATION_SCHEMA. The fourth -- source grants -- it cannot answer, and that is a
# property of BigQuery rather than a gap in this adapter: see `_BIGQUERY_GRANTS_NOTE`.

# A definition longer than this is stored as a prefix with `truncated=True`, never
# silently whole-looking: view-DDL lineage (N2) has to be able to tell a short view
# from a clipped one.
_MAX_DEFINITION_CHARACTERS = 1_000_000

_VIEW_OBJECT_TYPES = frozenset({"VIEW", "MATERIALIZED_VIEW"})

# Why `capabilities.grants` stays False. BigQuery has no SQL GRANT: access is Cloud
# IAM policy bound at project, dataset, table, column and row level, inherited down
# the resource hierarchy and optionally conditional on a CEL expression.
# `INFORMATION_SCHEMA.OBJECT_PRIVILEGES` does expose those bindings, but its
# `privilege_type` is an IAM role name (`roles/bigquery.dataViewer`) -- a bundle of
# permissions -- where `DiscoveredGrant.privilege` everywhere else holds one SQL
# privilege (`SELECT`). Writing a role bundle into that field would make
# "who can already see this" answer differently on BigQuery than on Oracle or
# Snowflake while looking identical, which is worse than declining the axis.
# Closing it honestly needs an IAM-binding axis of its own, not `DiscoveredGrant`.
_BIGQUERY_GRANTS_NOTE = (
    "BigQuery has no SQL grant surface; access is Cloud IAM policy, which "
    "DiscoveredGrant does not model. capabilities.grants stays False."
)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unquote_option_value(value: object) -> str | None:
    """Unwrap a BigQuery `option_value` into plain text.

    `INFORMATION_SCHEMA.TABLE_OPTIONS`, `.SCHEMATA_OPTIONS` and `.ROUTINE_OPTIONS`
    return `option_value` as GoogleSQL *source text*, so a description arrives as a
    quoted, backslash-escaped string literal rather than as the description itself.
    Storing the literal verbatim would put a stray pair of quotes in front of every
    asset description in the catalogue.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    text = (
        text.replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )
    return text.strip() or None


def _build_view_definition(
    definition_text: object,
    *,
    object_label: str,
    is_materialized: bool = False,
    check_option: object = None,
    fallback_reason: str | None = None,
    max_characters: int = _MAX_DEFINITION_CHARACTERS,
) -> DiscoveredViewDefinition:
    """Turn one VIEWS / TABLES row into an honest view definition.

    A logical view's text comes from `INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION`. A
    materialized view has no row in that view at all, so its text comes from
    `INFORMATION_SCHEMA.TABLES.DDL` -- the whole `CREATE MATERIALIZED VIEW ...`
    statement rather than the bare query. When neither is present the object records
    why, because an absent definition and an empty one are different facts and
    view-DDL lineage would read a silent empty as a lineage gap in the estate.

    BigQuery exposes no updatability column, and its `CHECK_OPTION` is documented as
    always NULL, so `is_updatable` stays `None` and `check_option` is only set if the
    source ever starts populating it.
    """
    definition = DiscoveredViewDefinition(
        definition_sql=None,
        is_materialized=is_materialized,
        check_option=_optional_text(check_option),
    )
    if definition_text is None:
        return replace(
            definition,
            unavailable_reason=fallback_reason
            or (
                f"BigQuery INFORMATION_SCHEMA exposed no definition text for "
                f"{object_label} to this service account"
            ),
        )
    text = str(definition_text)
    if len(text) > max_characters:
        return replace(definition, definition_sql=text[:max_characters], truncated=True)
    return replace(definition, definition_sql=text)


def _build_routine(
    row: Mapping[str, Any],
    *,
    parameters: tuple[DiscoveredRoutineParameter, ...] = (),
    return_type: str | None = None,
    source_description: str | None = None,
    max_characters: int = _MAX_DEFINITION_CHARACTERS,
) -> DiscoveredRoutine:
    """Turn one `INFORMATION_SCHEMA.ROUTINES` row into a routine.

    `ROUTINE_DEFINITION` is the body for both SQL routines and JavaScript UDFs, and
    is NULL for a remote function, whose body lives in a Cloud Function rather than
    in BigQuery -- recorded as an `unavailable_reason`, not as an empty body.

    BigQuery documents `IS_DETERMINISTIC` and `SECURITY_TYPE` as always NULL, so both
    are read and both normally arrive as `None`; they are read rather than assumed so
    that the day BigQuery populates them the envelope carries them.
    """
    name = str(row["routine_name"])
    schema_name = str(row.get("routine_schema") or "")
    label = f"{schema_name}.{name}" if schema_name else name
    routine_body = _optional_text(row.get("routine_body"))
    external_language = _optional_text(row.get("external_language"))
    body = row.get("routine_definition")
    truncated = False
    unavailable_reason: str | None = None
    if body is None:
        body_sql: str | None = None
        unavailable_reason = (
            f"BigQuery returned no ROUTINE_DEFINITION for {label}; a remote function's "
            "body lives outside BigQuery and is not part of its metadata"
        )
    else:
        body_sql = str(body)
        if len(body_sql) > max_characters:
            body_sql = body_sql[:max_characters]
            truncated = True
    deterministic = _optional_text(row.get("is_deterministic"))
    attributes: dict[str, Any] = {}
    if routine_body is not None:
        attributes["routine_body"] = routine_body
    return DiscoveredRoutine(
        name=name,
        routine_type=str(row.get("routine_type") or "PROCEDURE"),
        language=external_language or ("SQL" if routine_body == "SQL" else None),
        body_sql=body_sql,
        parameters=parameters,
        return_type=_optional_text(row.get("data_type")) or return_type,
        is_deterministic=(None if deterministic is None else deterministic.upper() == "YES"),
        security_mode=_optional_text(row.get("security_type")),
        source_description=source_description,
        truncated=truncated,
        unavailable_reason=unavailable_reason,
        attributes=attributes,
    )


@dataclass(frozen=True, slots=True)
class _BigQueryEnvelopeRows:
    """Row sets behind envelope 1.1, plus why an axis is missing when it is.

    There is no `grants` field: BigQuery has no SQL grant surface at all (see
    `_BIGQUERY_GRANTS_NOTE`), and an always-empty field would read as "this project
    grants nothing" rather than "this source does not work that way".
    """

    tables: tuple[dict[str, Any], ...] = ()
    views: tuple[dict[str, Any], ...] = ()
    routines: tuple[dict[str, Any], ...] = ()
    parameters: tuple[dict[str, Any], ...] = ()
    routine_options: tuple[dict[str, Any], ...] = ()
    table_options: tuple[dict[str, Any], ...] = ()
    schema_options: tuple[dict[str, Any], ...] = ()
    column_field_paths: tuple[dict[str, Any], ...] = ()
    unavailable: tuple[tuple[str, str], ...] = ()

    def reason(self, axis: str) -> str | None:
        for name, message in self.unavailable:
            if name == axis:
                return message
        return None


def _description_by_key(
    rows: Sequence[Mapping[str, Any]], key_fields: Sequence[str]
) -> dict[tuple[str, ...], str | None]:
    descriptions: dict[tuple[str, ...], str | None] = {}
    for row in rows:
        if str(row.get("option_name") or "").lower() != "description":
            continue
        key = tuple(str(row[field]) for field in key_fields)
        descriptions[key] = _unquote_option_value(row.get("option_value"))
    return descriptions


def _envelope_routines(envelope: _BigQueryEnvelopeRows) -> dict[str, list[DiscoveredRoutine]]:
    """Group ROUTINES, PARAMETERS and ROUTINE_OPTIONS into per-schema routine lists.

    `PARAMETERS.ORDINAL_POSITION = 0` with `IS_RESULT = 'YES'` is a function's return
    value, not a parameter, and is lifted out as `return_type`.
    """
    parameters: dict[tuple[str, str], list[DiscoveredRoutineParameter]] = {}
    return_types: dict[tuple[str, str], str] = {}
    for row in envelope.parameters:
        key = (str(row["specific_schema"]), str(row["specific_name"]))
        position = int(row.get("ordinal_position") or 0)
        data_type = _optional_text(row.get("data_type"))
        if position == 0 or str(row.get("is_result") or "").upper() == "YES":
            if data_type is not None:
                return_types[key] = data_type
            continue
        parameters.setdefault(key, []).append(
            DiscoveredRoutineParameter(
                name=_optional_text(row.get("parameter_name")),
                ordinal_position=position,
                mode=str(row.get("parameter_mode") or "IN").upper(),
                physical_type=data_type or "",
                default_expression=_optional_text(row.get("parameter_default")),
            )
        )
    descriptions = _description_by_key(
        envelope.routine_options, ("routine_schema", "routine_name")
    )
    routines: dict[str, list[DiscoveredRoutine]] = {}
    for row in envelope.routines:
        schema_name = str(row["routine_schema"])
        key = (schema_name, str(row["routine_name"]))
        routines.setdefault(schema_name, []).append(
            _build_routine(
                row,
                parameters=tuple(parameters.get(key, ())),
                return_type=return_types.get(key),
                source_description=descriptions.get(key),
            )
        )
    return routines


def _apply_envelope(
    catalogs: tuple[DiscoveredCatalog, ...], envelope: _BigQueryEnvelopeRows
) -> tuple[DiscoveredCatalog, ...]:
    """Fold envelope 1.1 rows onto an already-assembled catalog.

    A rebuild rather than a change to `aida.connectors.discovery`, whose shared
    helpers are on the v1.0 contract and are used by connectors this workstream does
    not own.

    The catalog's own `source_description` stays `None`: a GCP project has no
    description in INFORMATION_SCHEMA.
    """
    view_definitions = {
        (str(row["table_schema"]), str(row["table_name"])): row for row in envelope.views
    }
    table_ddl = {
        (str(row["table_schema"]), str(row["table_name"])): row.get("ddl")
        for row in envelope.tables
    }
    table_descriptions = _description_by_key(
        envelope.table_options, ("table_schema", "table_name")
    )
    schema_descriptions = _description_by_key(envelope.schema_options, ("schema_name",))
    column_descriptions = {
        (
            str(row["table_schema"]),
            str(row["table_name"]),
            str(row["column_name"]),
        ): _optional_text(row.get("description"))
        for row in envelope.column_field_paths
        if str(row.get("field_path") or row.get("column_name")) == str(row["column_name"])
    }
    routines = _envelope_routines(envelope)
    views_reason = envelope.reason("views")

    rebuilt: list[DiscoveredCatalog] = []
    for catalog in catalogs:
        schemas = []
        for schema in catalog.schemas:
            tables = []
            for table in schema.tables:
                key = (schema.name, table.name)
                definition: DiscoveredViewDefinition | None = None
                if table.object_type in _VIEW_OBJECT_TYPES:
                    is_materialized = table.object_type == "MATERIALIZED_VIEW"
                    view_row: dict[str, Any] = view_definitions.get(key, {})
                    text = view_row.get("view_definition")
                    fallback_reason = None
                    if text is None and is_materialized:
                        # A materialized view has no INFORMATION_SCHEMA.VIEWS row;
                        # TABLES.DDL is the only place its statement appears.
                        text = table_ddl.get(key)
                        fallback_reason = envelope.reason("tables")
                    elif text is None:
                        fallback_reason = views_reason
                    definition = _build_view_definition(
                        text,
                        object_label=f"{schema.name}.{table.name}",
                        is_materialized=is_materialized,
                        check_option=view_row.get("check_option"),
                        fallback_reason=fallback_reason,
                    )
                columns = tuple(
                    replace(
                        column,
                        source_description=column_descriptions.get(
                            (schema.name, table.name, column.name)
                        ),
                    )
                    for column in table.columns
                )
                tables.append(
                    replace(
                        table,
                        columns=columns,
                        source_description=table_descriptions.get(key),
                        view_definition=definition,
                    )
                )
            schemas.append(
                replace(
                    schema,
                    tables=tuple(tables),
                    routines=tuple(routines.get(schema.name, ())),
                    source_description=schema_descriptions.get((schema.name,)),
                )
            )
        attributes = dict(catalog.attributes)
        attributes["grants"] = _BIGQUERY_GRANTS_NOTE
        if envelope.unavailable:
            attributes["envelope_v11_unavailable"] = dict(envelope.unavailable)
        rebuilt.append(replace(catalog, schemas=tuple(schemas), attributes=attributes))
    return tuple(rebuilt)


def _information_schema_query(prefix: str, columns: str, view: str, where: str = "") -> str:
    """Build one region-qualified INFORMATION_SCHEMA query.

    `prefix` is assembled from a `project_id` and `location` that
    `_parse_credential_payload` has already validated against a strict pattern, and
    `columns`, `view` and `where` are module constants. No caller-supplied value
    reaches this string, which is why the S608 suppression below is safe.
    """
    sql = f"SELECT {columns} FROM {prefix}.{view} {where}"  # noqa: S608
    return sql.strip()


def _query_optional_rows(client: Any, sql: str) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Run one supplementary metadata query, turning a refusal into a reason.

    A service account without `bigquery.routines.get` on a dataset gets an error from
    `INFORMATION_SCHEMA.ROUTINES`, and that denial must not read as "this project has
    no routines". It comes back as a reason string that lands on the catalog.
    """
    try:
        return tuple(dict(row.items()) for row in client.query(sql).result()), None
    except Exception as exc:
        return (), f"{type(exc).__name__}: {exc}"


_ROUTINE_COLUMNS = (
    "routine_schema, routine_name, routine_type, data_type, routine_body, "
    "routine_definition, external_language, is_deterministic, security_type"
)
_PARAMETER_COLUMNS = (
    "specific_schema, specific_name, ordinal_position, parameter_mode, is_result, "
    "parameter_name, data_type, parameter_default"
)
_FIELD_PATH_COLUMNS = "table_schema, table_name, column_name, field_path, description"


def _fetch_envelope_rows(client: Any, *, project_id: str, region: str) -> _BigQueryEnvelopeRows:
    """Read every envelope 1.1 axis BigQuery exposes, recording each refusal."""
    prefix = f"`{project_id}`.`{region}`.INFORMATION_SCHEMA"
    unavailable: list[tuple[str, str]] = []

    def _collect(axis: str, columns: str, view: str, where: str = "") -> tuple[dict[str, Any], ...]:
        rows, reason = _query_optional_rows(
            client, _information_schema_query(prefix, columns, view, where)
        )
        if reason is not None:
            unavailable.append((axis, reason))
        return rows

    description_filter = "WHERE option_name = 'description'"
    return _BigQueryEnvelopeRows(
        tables=_collect("tables", "table_schema, table_name, table_type, ddl", "TABLES"),
        views=_collect(
            "views", "table_schema, table_name, view_definition, check_option", "VIEWS"
        ),
        routines=_collect("routines", _ROUTINE_COLUMNS, "ROUTINES"),
        parameters=_collect("parameters", _PARAMETER_COLUMNS, "PARAMETERS"),
        routine_options=_collect(
            "routine_options",
            "routine_schema, routine_name, option_name, option_value",
            "ROUTINE_OPTIONS",
            description_filter,
        ),
        table_options=_collect(
            "table_options",
            "table_schema, table_name, option_name, option_value",
            "TABLE_OPTIONS",
            description_filter,
        ),
        schema_options=_collect(
            "schema_options",
            "schema_name, option_name, option_value",
            "SCHEMATA_OPTIONS",
            description_filter,
        ),
        column_field_paths=_collect(
            "column_field_paths", _FIELD_PATH_COLUMNS, "COLUMN_FIELD_PATHS"
        ),
        unavailable=tuple(unavailable),
    )


def _assemble_catalog(
    project_id: str,
    column_rows: list[dict[str, Any]],
    key_rows: list[dict[str, Any]],
    *,
    envelope: _BigQueryEnvelopeRows | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble a standard DiscoveredCatalog from discovered columns and primary/unique key rows."""
    tables = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(
        tables,
        key_rows,
        constraint_type_map={"PRIMARY KEY": "PRIMARY_KEY", "UNIQUE": "UNIQUE"},
    )
    catalogs = assemble_catalog(project_id, tables)
    if envelope is None:
        return catalogs
    return _apply_envelope(catalogs, envelope)


class BigQueryConnector(SqlExecutor):
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
        # Envelope 1.1 (gap/02 N1). Set from what `discover()` actually reads.
        views=True,  # INFORMATION_SCHEMA.VIEWS, plus TABLES.DDL for materialized views
        routines=True,  # INFORMATION_SCHEMA.ROUTINES / PARAMETERS / ROUTINE_OPTIONS
        object_comments=True,  # SCHEMATA_OPTIONS, TABLE_OPTIONS, COLUMN_FIELD_PATHS
        grants=False,  # see _BIGQUERY_GRANTS_NOTE -- BigQuery has no SQL grants
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

            # Envelope 1.1 (gap/02 N1): view text, routines with bodies and
            # descriptions. `INFORMATION_SCHEMA.COLUMNS` carries no table type, so the
            # real one comes from `TABLES`; if that query is refused, every object
            # keeps today's `BASE TABLE` default and the refusal is recorded.
            envelope = _fetch_envelope_rows(
                client, project_id=self._config.project_id, region=region
            )
            table_types = {
                (str(row["table_schema"]), str(row["table_name"])): str(row["table_type"])
                for row in envelope.tables
            }
            for row in column_rows:
                key = (str(row["table_schema"]), str(row["table_name"]))
                row["table_type"] = table_types.get(key, str(row.get("table_type") or "BASE TABLE"))

            return _assemble_catalog(
                self._config.project_id, column_rows, key_rows, envelope=envelope
            )

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
