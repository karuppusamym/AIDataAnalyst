"""
Snowflake Native Warehouse Connector
====================================

Implements the Atlas ``Connector`` ABC for Snowflake Data Cloud with strict governance,
fail-closed validation, partition-pruned EXPLAIN cost estimation, and value-free metadata discovery.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from aida.connectors.base import (
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredViewDefinition,
    QueryEstimate,
    QueryLogEntry,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    TableMap,
    append_grouped_foreign_key_rows,
    append_grouped_key_rows,
    apply_column_descriptions,
    apply_table_descriptions,
    apply_view_definitions,
    assemble_catalog,
    build_grants,
    build_table_map_from_column_rows,
    normalize_object_type,
    view_definition_row,
)
from aida.connectors.sql_execution import SqlExecutor

_COMPLEX_SCALAR_TYPES = frozenset({"VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY"})
_EXCLUDED_SCHEMAS = frozenset({"INFORMATION_SCHEMA", "ACCOUNT_USAGE", "READER_ACCOUNT_USAGE"})


def _quote_identifier(identifier: str) -> str:
    """Snowflake double-quote an identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_table(database: str, schema: str, table: str) -> str:
    """Format a fully-qualified 3-part Snowflake table identifier."""
    return f"{_quote_identifier(database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"


@dataclass(frozen=True, slots=True)
class _SnowflakeConnectionParams:
    account: str
    user: str
    password: str | None = None
    database: str = ""
    schema: str | None = None
    warehouse: str | None = None
    role: str | None = None
    host: str | None = None
    port: int = 443
    authenticator: str | None = None
    token: str | None = None


def _parse_dsn(dsn: str) -> _SnowflakeConnectionParams:
    """Parse Snowflake connection reference from JSON payload or standard URI.

    Accepted formats:
    - JSON credential payload: {"account": "...", "user": "...", ...}
    - DSN URI: snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE
    - Host-style DSN URI: snowflake://user:password@account.snowflakecomputing.com/database
    """
    raw = dsn.strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"invalid Snowflake credential JSON payload: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Snowflake credential JSON payload must be a JSON object")
        account = data.get("account")
        user = data.get("user")
        if not account or not user:
            raise ValueError("Snowflake credentials must include 'account' and 'user'")
        return _SnowflakeConnectionParams(
            account=str(account),
            user=str(user),
            password=str(data["password"]) if "password" in data else None,
            database=str(data.get("database") or ""),
            schema=str(data["schema"]) if "schema" in data else None,
            warehouse=str(data["warehouse"]) if "warehouse" in data else None,
            role=str(data["role"]) if "role" in data else None,
            host=str(data["host"]) if "host" in data else None,
            port=int(data.get("port", 443)),
            authenticator=str(data["authenticator"]) if "authenticator" in data else None,
            token=str(data["token"]) if "token" in data else None,
        )

    parsed = urlsplit(raw)
    if parsed.scheme not in {"snowflake", "snowflake-sql"}:
        raise ValueError(
            "invalid Snowflake connection reference; expected "
            "snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE"
        )
    if not parsed.hostname or not parsed.username:
        raise ValueError("Snowflake connection reference is missing account/host or username")

    # Account identifier extraction
    host_or_account = parsed.hostname
    if host_or_account.endswith(".snowflakecomputing.com"):
        account = host_or_account[: -len(".snowflakecomputing.com")]
        host = host_or_account
    else:
        account = host_or_account
        host = f"{account}.snowflakecomputing.com"

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    database = unquote(path_parts[0]) if len(path_parts) > 0 else ""
    schema = unquote(path_parts[1]) if len(path_parts) > 1 else None

    query_params = parse_qs(parsed.query)
    warehouse = query_params.get("warehouse", [None])[0]
    role = query_params.get("role", [None])[0]
    authenticator = query_params.get("authenticator", [None])[0]

    return _SnowflakeConnectionParams(
        account=account,
        user=unquote(parsed.username),
        password=unquote(parsed.password) if parsed.password is not None else None,
        database=database,
        schema=schema,
        warehouse=warehouse,
        role=role,
        host=host,
        port=parsed.port or 443,
        authenticator=authenticator,
    )


def _extract_snowflake_explain_estimate(explain_data: Any) -> QueryEstimate:
    """Extract rows, bytes, and partition pruning estimates from Snowflake EXPLAIN JSON."""
    total_bytes = 0
    total_rows = 0.0
    partitions_total = 0
    partitions_assigned = 0

    def traverse(node: Any) -> None:
        nonlocal total_bytes, total_rows, partitions_total, partitions_assigned
        if isinstance(node, dict):
            if "bytesAssigned" in node and isinstance(node["bytesAssigned"], int | float):
                total_bytes += int(node["bytesAssigned"])
            elif "bytes" in node and isinstance(node["bytes"], int | float):
                total_bytes += int(node["bytes"])

            if "rowsTotal" in node and isinstance(node["rowsTotal"], int | float):
                total_rows += float(node["rowsTotal"])
            elif "rows" in node and isinstance(node["rows"], int | float):
                total_rows += float(node["rows"])

            if "partitionsTotal" in node and isinstance(node["partitionsTotal"], int | float):
                partitions_total += int(node["partitionsTotal"])
            if "partitionsAssigned" in node and isinstance(node["partitionsAssigned"], int | float):
                partitions_assigned += int(node["partitionsAssigned"])

            for value in node.values():
                traverse(value)
        elif isinstance(node, list):
            for item in node:
                traverse(item)

    if isinstance(explain_data, str):
        try:
            parsed = json.loads(explain_data)
            traverse(parsed)
        except Exception:
            # Fallback regex parse for text-based plan
            rows_match = re.search(r"rows\s*=\s*(\d+)", explain_data, re.IGNORECASE)
            if rows_match:
                total_rows = float(rows_match.group(1))
            bytes_match = re.search(r"bytes\s*=\s*(\d+)", explain_data, re.IGNORECASE)
            if bytes_match:
                total_bytes = int(bytes_match.group(1))
    else:
        traverse(explain_data)

    score = round(max(total_rows * 0.01 + (total_bytes / (1024 * 1024)), 1.0), 2)
    return QueryEstimate(
        score=score,
        kind="SNOWFLAKE_EXPLAIN_PLAN",
        estimated_rows=total_rows if total_rows > 0 else None,
        estimated_bytes=total_bytes if total_bytes > 0 else None,
        evidence={
            "partitions_total": partitions_total,
            "partitions_assigned": partitions_assigned,
            "pruning_ratio": (
                round(1.0 - (partitions_assigned / partitions_total), 4)
                if partitions_total > 0
                else 1.0
            ),
        },
    )


_CONSTRAINT_TYPE_MAP = {
    "PRIMARY KEY": "PRIMARY_KEY",
    "UNIQUE": "UNIQUE",
    "PRIMARY_KEY": "PRIMARY_KEY",
}


def _rows_to_dicts(cursor: Any, rows: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(r) for r in rows]
    col_names = [desc[0].lower() for desc in cursor.description] if cursor.description else []
    return [dict(zip(col_names, row, strict=False)) for row in rows]


# --- Envelope 1.1 (gap/02 N1) ------------------------------------------------
#
# Snowflake exposes three of the four envelope axes through INFORMATION_SCHEMA and
# the fourth only through a metadata command. Each quirk that could make a refusal
# look like an absence is handled explicitly, and the helpers below are pure so the
# refusal paths are unit tested rather than argued about.

# A definition longer than this is stored as a prefix with `truncated=True`. Snowflake
# will hand back up to a 16 MB VARCHAR; view-DDL lineage (N2) has to be able to tell a
# short view from a clipped one.
_MAX_DEFINITION_CHARACTERS = 1_000_000

_VIEW_OBJECT_TYPES = frozenset({"VIEW", "MATERIALIZED_VIEW"})


def _quote_literal(value: str) -> str:
    """Single-quote a value for a Snowflake string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _unqualified_name(value: object) -> str:
    """Take the object name out of a fully-qualified SHOW GRANTS `name` column."""
    text = str(value or "")
    return text.split(".")[-1].strip('"')


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_true(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "y", "1"}


def _split_top_level(signature: str) -> list[str]:
    """Split an argument signature on commas that are not inside parentheses.

    `NUMBER(38,0)` must not be split at its own comma.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for character in signature:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _parse_argument_signature(signature: object) -> tuple[DiscoveredRoutineParameter, ...]:
    """Parse Snowflake's `ARGUMENT_SIGNATURE` text into parameters.

    Snowflake has no `INFORMATION_SCHEMA.PARAMETERS`: `INFORMATION_SCHEMA.FUNCTIONS`
    and `.PROCEDURES` carry the whole argument list as one text column shaped like
    `(A NUMBER, B VARCHAR DEFAULT NULL)`. Parsing it is therefore the only way to
    reach a parameter list, and an unparseable fragment becomes a parameter with an
    empty `physical_type` rather than a silently dropped argument.

    Every Snowflake UDF and stored-procedure argument is an input argument, so `mode`
    is always `IN`.
    """
    text = _optional_text(signature)
    if text is None:
        return ()
    inner = text.strip()
    if inner.startswith("("):
        inner = inner[1:]
    if inner.endswith(")"):
        inner = inner[:-1]
    parameters: list[DiscoveredRoutineParameter] = []
    for position, part in enumerate(_split_top_level(inner), start=1):
        remainder = part
        default_expression: str | None = None
        upper = remainder.upper()
        marker = upper.find(" DEFAULT ")
        if marker >= 0:
            default_expression = remainder[marker + len(" DEFAULT ") :].strip() or None
            remainder = remainder[:marker].strip()
        pieces = remainder.split(None, 1)
        if len(pieces) == 2:
            name, physical_type = pieces[0], pieces[1].strip()
        else:
            name, physical_type = None, remainder
        parameters.append(
            DiscoveredRoutineParameter(
                name=name,
                ordinal_position=position,
                mode="IN",
                physical_type=physical_type,
                default_expression=default_expression,
            )
        )
    return tuple(parameters)


def _build_view_definition(
    definition_text: object,
    *,
    object_label: str,
    is_materialized: bool = False,
    is_secure: object = None,
    is_updatable: object = None,
    check_option: object = None,
    fallback_reason: str | None = None,
    max_characters: int = _MAX_DEFINITION_CHARACTERS,
) -> DiscoveredViewDefinition:
    """Turn one view row into an honest definition.

    Snowflake returns NULL for `VIEW_DEFINITION` on a **secure** view unless the
    session holds the owning role. That NULL is the single most likely way a view's
    text goes missing on Snowflake, and it must never arrive as an empty definition.
    """
    check = _optional_text(check_option)
    definition = DiscoveredViewDefinition(
        definition_sql=None,
        is_materialized=is_materialized,
        is_updatable=None if is_updatable is None else _is_true(is_updatable),
        check_option=None if check is None or check.upper() == "NONE" else check,
    )
    if definition_text is None:
        if _is_true(is_secure):
            reason = (
                f"{object_label} is a secure view; Snowflake withholds VIEW_DEFINITION "
                "from a session whose role does not own it"
            )
        elif fallback_reason is not None:
            reason = fallback_reason
        else:
            reason = (
                f"Snowflake returned no definition text for {object_label} and GET_DDL "
                "was not able to supply one"
            )
        return replace(definition, unavailable_reason=reason)
    text = str(definition_text)
    if len(text) > max_characters:
        return replace(definition, definition_sql=text[:max_characters], truncated=True)
    return replace(definition, definition_sql=text)


def _build_routine(
    row: Mapping[str, Any], *, max_characters: int = _MAX_DEFINITION_CHARACTERS
) -> DiscoveredRoutine:
    """Turn one INFORMATION_SCHEMA.FUNCTIONS / .PROCEDURES row into a routine.

    Snowflake nulls `FUNCTION_DEFINITION` / `PROCEDURE_DEFINITION` for a secure
    routine the session's role does not own, and for routines whose body it does not
    keep (built-ins, external functions). Those arrive as an `unavailable_reason`.

    `security_mode` stays `None`: a procedure's `EXECUTE AS OWNER | CALLER` is not an
    INFORMATION_SCHEMA column, it is only reachable through `SHOW PROCEDURES` /
    `DESCRIBE PROCEDURE`. `is_deterministic` stays `None` for the same reason.
    """
    routine_type = str(row.get("routine_type") or "FUNCTION")
    name = str(row["routine_name"])
    schema_name = str(row.get("routine_schema") or "")
    label = f"{schema_name}.{name}" if schema_name else name
    body = row.get("routine_definition")
    attributes: dict[str, Any] = {}
    signature = _optional_text(row.get("argument_signature"))
    if signature is not None:
        attributes["argument_signature"] = signature
    if _is_true(row.get("is_secure")):
        attributes["is_secure"] = True
    truncated = False
    unavailable_reason: str | None = None
    if body is None:
        if _is_true(row.get("is_secure")):
            unavailable_reason = (
                f"{label} is a secure {routine_type.lower()}; Snowflake withholds its "
                "definition from a session whose role does not own it"
            )
        else:
            unavailable_reason = (
                f"Snowflake returned no definition text for {label}; it keeps no body "
                "for built-in and external routines"
            )
        body_sql: str | None = None
    else:
        body_sql = str(body)
        if len(body_sql) > max_characters:
            body_sql = body_sql[:max_characters]
            truncated = True
    return DiscoveredRoutine(
        name=name,
        routine_type=routine_type,
        language=_optional_text(row.get("routine_language")),
        body_sql=body_sql,
        parameters=_parse_argument_signature(row.get("argument_signature")),
        return_type=_optional_text(row.get("data_type")),
        is_deterministic=None,
        security_mode=None,
        source_description=_optional_text(row.get("comment")),
        truncated=truncated,
        unavailable_reason=unavailable_reason,
        attributes=attributes,
    )


def _grant_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Shape one `SHOW GRANTS` result row for the shared `build_grants`.

    `SHOW GRANTS` is a metadata command, not a view over INFORMATION_SCHEMA: it
    cannot be joined, filtered or aggregated, and it has to be issued one object at a
    time. The only set-returning grant surface Snowflake offers is
    `SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES`, which needs access to the shared
    SNOWFLAKE database and lags reality by up to two hours.

    `object_name` is unqualified here rather than left to `build_grants`, which has
    no notion of Snowflake's dotted `SHOW GRANTS` naming.
    """
    return {
        "schema_name": row.get("schema_name"),
        "grantee": row.get("grantee_name") or "",
        "grantee_type": row.get("granted_to") or "ROLE",
        "privilege": row.get("privilege") or "",
        "object_type": row.get("granted_on") or "SCHEMA",
        "object_name": _unqualified_name(row.get("name")),
        "is_grantable": row.get("grant_option"),
    }


@dataclass(frozen=True, slots=True)
class _SnowflakeEnvelopeRows:
    """Row sets behind envelope 1.1, plus why an axis is missing when it is."""

    views: tuple[dict[str, Any], ...] = ()
    view_ddl: tuple[dict[str, Any], ...] = ()
    routines: tuple[dict[str, Any], ...] = ()
    schemata: tuple[dict[str, Any], ...] = ()
    databases: tuple[dict[str, Any], ...] = ()
    grants: tuple[dict[str, Any], ...] = ()
    unavailable: tuple[tuple[str, str], ...] = ()

    def reason(self, axis: str) -> str | None:
        for name, message in self.unavailable:
            if name == axis:
                return message
        return None


def _fetch_optional_rows(cursor: Any, sql: str) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Run one supplementary metadata query, turning a refusal into a reason."""
    try:
        cursor.execute(sql)
        return tuple(_rows_to_dicts(cursor, cursor.fetchall())), None
    except Exception as exc:
        return (), f"{type(exc).__name__}: {exc}"


def _fetch_envelope_rows(
    cursor: Any, *, database: str, schema_names: Sequence[str], view_keys: Sequence[tuple[str, str]]
) -> _SnowflakeEnvelopeRows:
    """Read every envelope 1.1 axis Snowflake exposes, recording each refusal.

    `view_keys` are the (schema, name) pairs of view-shaped objects discovered from
    INFORMATION_SCHEMA.TABLES. They drive the `GET_DDL` second pass, which is the
    only path to a materialized view's text -- Snowflake's INFORMATION_SCHEMA.VIEWS
    contains no row for a materialized view at all.
    """
    unavailable: list[tuple[str, str]] = []

    def _collect(axis: str, sql: str) -> tuple[dict[str, Any], ...]:
        rows, reason = _fetch_optional_rows(cursor, sql)
        if reason is not None:
            unavailable.append((axis, reason))
        return rows

    databases = _collect(
        "catalog_comment",
        "SELECT database_name, comment FROM information_schema.databases "
        "WHERE database_name = CURRENT_DATABASE()",
    )
    schemata = _collect(
        "schema_comments",
        "SELECT schema_name, comment FROM information_schema.schemata "
        "WHERE schema_name NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')",
    )
    views = _collect(
        "views",
        """
        SELECT
            table_schema,
            table_name,
            view_definition,
            is_secure,
            is_updatable,
            check_option
        FROM information_schema.views
        WHERE table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
        """,
    )
    functions = _collect(
        "functions",
        """
        SELECT
            function_schema AS routine_schema,
            function_name AS routine_name,
            'FUNCTION' AS routine_type,
            function_language AS routine_language,
            function_definition AS routine_definition,
            argument_signature,
            data_type,
            is_secure,
            comment
        FROM information_schema.functions
        WHERE function_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
        """,
    )
    procedures = _collect(
        "procedures",
        """
        SELECT
            procedure_schema AS routine_schema,
            procedure_name AS routine_name,
            'PROCEDURE' AS routine_type,
            procedure_language AS routine_language,
            procedure_definition AS routine_definition,
            argument_signature,
            data_type,
            is_secure,
            comment
        FROM information_schema.procedures
        WHERE procedure_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
        """,
    )

    definitions = {
        (str(row.get("table_schema")), str(row.get("table_name")))
        for row in views
        if row.get("view_definition") is not None
    }
    view_ddl: list[dict[str, Any]] = []
    for schema_name, view_name in view_keys:
        if (schema_name, view_name) in definitions:
            continue
        qualified = (
            f"{database}.{schema_name}.{view_name}" if database else f"{schema_name}.{view_name}"
        )
        rows, reason = _fetch_optional_rows(
            cursor,
            f"SELECT GET_DDL('VIEW', {_quote_literal(qualified)}, TRUE) AS view_definition",  # noqa: S608 -- the identifier is quoted as a string literal, not interpolated as SQL
        )
        definition = rows[0].get("view_definition") if rows else None
        view_ddl.append(
            {
                "table_schema": schema_name,
                "table_name": view_name,
                "view_definition": definition,
                "unavailable_reason": (
                    None
                    if definition is not None
                    else reason
                    or (
                        f"GET_DDL returned no text for {qualified}; Snowflake exposes no "
                        "definition for this object to the session's role"
                    )
                ),
            }
        )

    grants: list[dict[str, Any]] = []
    for schema_name in schema_names:
        qualified = (
            f"{_quote_identifier(database)}.{_quote_identifier(schema_name)}"
            if database
            else _quote_identifier(schema_name)
        )
        rows, reason = _fetch_optional_rows(cursor, f"SHOW GRANTS ON SCHEMA {qualified}")
        if reason is not None:
            unavailable.append((f"grants:{schema_name}", reason))
            continue
        for row in rows:
            grants.append({**row, "schema_name": schema_name})

    return _SnowflakeEnvelopeRows(
        views=views,
        view_ddl=tuple(view_ddl),
        routines=functions + procedures,
        schemata=schemata,
        databases=databases,
        grants=tuple(grants),
        unavailable=tuple(unavailable),
    )


def _table_description_rows(column_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Shape the table-comment half of `column_rows` for `apply_table_descriptions`.

    Snowflake's comments ride along on the same INFORMATION_SCHEMA.COLUMNS /
    .TABLES join that produces `column_rows`, rather than a query of their own.
    """
    return [
        {
            "table_schema": row["table_schema"],
            "table_name": row["table_name"],
            "description": _optional_text(row.get("table_comment")),
        }
        for row in column_rows
    ]


def _column_description_rows(column_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Shape the column-comment half of `column_rows` for `apply_column_descriptions`."""
    return [
        {
            "table_schema": row["table_schema"],
            "table_name": row["table_name"],
            "column_name": row["column_name"],
            "description": _optional_text(row.get("column_comment")),
        }
        for row in column_rows
    ]


def _view_definition_rows(
    tables: TableMap, envelope: _SnowflakeEnvelopeRows
) -> list[dict[str, Any]]:
    """Build one `apply_view_definitions` row per view/materialized-view table.

    INFORMATION_SCHEMA first, GET_DDL second: a materialized view has no
    INFORMATION_SCHEMA.VIEWS row at all, so it always arrives via the GET_DDL pass
    -- or as that call's refusal.
    """
    view_rows = {
        (str(row["table_schema"]), str(row["table_name"])): row for row in envelope.views
    }
    ddl_rows = {
        (str(row["table_schema"]), str(row["table_name"])): row for row in envelope.view_ddl
    }
    views_reason = envelope.reason("views")

    rows: list[dict[str, Any]] = []
    for schema_name, schema_tables in tables.items():
        for table_name, table in schema_tables.items():
            if table.object_type not in _VIEW_OBJECT_TYPES:
                continue
            key = (schema_name, table_name)
            view_row: dict[str, Any] = view_rows.get(key, {})
            ddl_row: dict[str, Any] = ddl_rows.get(key, {})
            # INFORMATION_SCHEMA first, GET_DDL second. A materialized view has no
            # INFORMATION_SCHEMA.VIEWS row at all, so it always arrives via the
            # GET_DDL pass -- or as that call's refusal.
            text = view_row.get("view_definition")
            fallback_reason = None
            if text is None:
                text = ddl_row.get("view_definition")
                fallback_reason = ddl_row.get("unavailable_reason") or views_reason
            definition = _build_view_definition(
                text,
                object_label=f"{schema_name}.{table_name}",
                is_materialized=table.object_type == "MATERIALIZED_VIEW",
                is_secure=view_row.get("is_secure"),
                is_updatable=view_row.get("is_updatable"),
                check_option=view_row.get("check_option"),
                fallback_reason=fallback_reason,
            )
            rows.append(view_definition_row(schema_name, table_name, definition))
    return rows


def _assemble_snowflake_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    pk_rows: list[dict[str, Any]],
    fk_rows: list[dict[str, Any]],
    *,
    envelope: _SnowflakeEnvelopeRows | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble the catalog, folding in any envelope 1.1 axes the caller collected."""
    table_map = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(table_map, pk_rows, constraint_type_map=_CONSTRAINT_TYPE_MAP)
    append_grouped_foreign_key_rows(table_map, fk_rows)
    if envelope is None:
        return assemble_catalog(catalog_name, table_map)

    apply_table_descriptions(table_map, _table_description_rows(column_rows))
    apply_column_descriptions(table_map, _column_description_rows(column_rows))
    apply_view_definitions(table_map, _view_definition_rows(table_map, envelope))

    routines: dict[str, list[DiscoveredRoutine]] = {}
    for row in envelope.routines:
        schema_name = str(row["routine_schema"])
        routines.setdefault(schema_name, []).append(_build_routine(row))
    grants = build_grants([_grant_row(row) for row in envelope.grants])

    # A `None` description is dropped rather than kept -- `assemble_catalog` reads a
    # missing key exactly the same way it would read one mapped to `None`.
    schema_descriptions = {
        str(row["schema_name"]): description
        for row in envelope.schemata
        if (description := _optional_text(row.get("comment"))) is not None
    }

    catalogs = assemble_catalog(
        catalog_name,
        table_map,
        routines=routines,
        grants=grants,
        schema_descriptions=schema_descriptions,
        catalog_description=next(
            (_optional_text(row.get("comment")) for row in envelope.databases), None
        ),
    )
    if envelope.unavailable:
        catalogs = tuple(
            replace(
                catalog,
                attributes={
                    **catalog.attributes,
                    "envelope_v11_unavailable": dict(envelope.unavailable),
                },
            )
            for catalog in catalogs
        )
    return catalogs


class SnowflakeConnector(SqlExecutor):
    """Snowflake native connector conforming to the Atlas Connector protocol."""

    connector_type = "snowflake"
    dialect = "snowflake"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        catalogs=True,
        schemas=True,
        constraints=True,
        indexes=False,
        partitions=True,
        explain=True,
        # INV-9 (tracker AT-D3, 2026-08-30). Advertised `True` while nothing in the
        # platform consumes it -- there is no `get_query_history()` on any connector.
        # Advertising a capability we do not implement is the exact failure this
        # invariant exists to prevent, and under-claiming is the correct direction to
        # fail. Returns to `True` when AT-12 (query-history mining) certifies it.
        query_history=False,
        delegated_identity=True,
        approximate_statistics=True,
        # Envelope 1.1 (gap/02 N1). Each flag is set because `discover()` reads the
        # named surface and lands the result on the envelope, with every refusal
        # arriving as an `unavailable_reason` rather than as an empty value.
        views=True,  # INFORMATION_SCHEMA.VIEWS.VIEW_DEFINITION, GET_DDL fallback
        routines=True,  # INFORMATION_SCHEMA.FUNCTIONS and .PROCEDURES
        object_comments=True,  # COMMENT on DATABASES/SCHEMATA/TABLES/COLUMNS/routines
        grants=True,  # SHOW GRANTS ON SCHEMA (schema-level; see gap/08 for the bound)
    )

    def __init__(self, dsn: str, *, command_timeout: float = 60.0) -> None:
        self._dsn = dsn
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    def _get_connection(self) -> Any:
        """Create a Snowflake DBAPI connection using snowflake-connector-python."""
        try:
            import snowflake.connector
        except ImportError as exc:
            raise RuntimeError(
                "snowflake-connector-python package is required for native Snowflake connectivity. "
                "Install with: pip install snowflake-connector-python"
            ) from exc

        kwargs: dict[str, Any] = {
            "account": self._params.account,
            "user": self._params.user,
            "database": self._params.database or None,
            "schema": self._params.schema or None,
            "warehouse": self._params.warehouse or None,
            "role": self._params.role or None,
            "login_timeout": int(self._command_timeout),
            "network_timeout": int(self._command_timeout),
        }
        if self._params.password is not None:
            kwargs["password"] = self._params.password
        if self._params.authenticator is not None:
            kwargs["authenticator"] = self._params.authenticator
        if self._params.token is not None:
            kwargs["token"] = self._params.token

        return snowflake.connector.connect(**kwargs)

    async def test_connection(self) -> None:
        """Verify warehouse connectivity and session authentication."""

        def _sync_test() -> None:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_ROLE()")
                    cur.fetchone()
                finally:
                    cur.close()
            finally:
                conn.close()

        await asyncio.to_thread(_sync_test)

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        """Discover database catalogs, schemas, tables, columns, and constraints."""

        def _sync_discover() -> tuple[DiscoveredCatalog, ...]:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    # Catalog / Database Name
                    if self._params.database:
                        catalog_name = self._params.database.upper()
                    else:
                        cur.execute("SELECT CURRENT_DATABASE()")
                        row = cur.fetchone()
                        catalog_name = str(row[0]).upper() if row and row[0] else "SNOWFLAKE_DB"

                    # Discover Columns and Tables
                    cur.execute(
                        """
                        SELECT
                            c.table_schema,
                            c.table_name,
                            t.table_type,
                            c.column_name,
                            c.ordinal_position,
                            c.data_type,
                            c.is_nullable,
                            c.column_default,
                            t.comment AS table_comment,
                            c.comment AS column_comment
                        FROM information_schema.columns c
                        JOIN information_schema.tables t
                          ON t.table_catalog = c.table_catalog
                         AND t.table_schema = c.table_schema
                         AND t.table_name = c.table_name
                        WHERE c.table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
                        ORDER BY c.table_schema, c.table_name, c.ordinal_position
                        """
                    )
                    column_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Discover Primary Keys & Unique Constraints
                    cur.execute(
                        """
                        SELECT
                            tc.table_schema,
                            tc.table_name,
                            tc.constraint_name,
                            tc.constraint_type,
                            kcu.column_name,
                            kcu.ordinal_position
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON kcu.constraint_catalog = tc.constraint_catalog
                         AND kcu.constraint_schema = tc.constraint_schema
                         AND kcu.constraint_name = tc.constraint_name
                        WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                          AND tc.table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """
                    )
                    pk_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Discover Foreign Keys
                    cur.execute(
                        """
                        SELECT
                            tc.table_schema,
                            tc.table_name,
                            tc.constraint_name,
                            kcu.column_name,
                            ccu.table_schema AS referenced_schema,
                            ccu.table_name AS referenced_table,
                            ccu.column_name AS referenced_column,
                            kcu.ordinal_position
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.referential_constraints rc
                          ON rc.constraint_catalog = tc.constraint_catalog
                         AND rc.constraint_schema = tc.constraint_schema
                         AND rc.constraint_name = tc.constraint_name
                        JOIN information_schema.key_column_usage kcu
                          ON kcu.constraint_catalog = tc.constraint_catalog
                         AND kcu.constraint_schema = tc.constraint_schema
                         AND kcu.constraint_name = tc.constraint_name
                        JOIN information_schema.constraint_column_usage ccu
                          ON ccu.constraint_catalog = rc.unique_constraint_catalog
                         AND ccu.constraint_schema = rc.unique_constraint_schema
                         AND ccu.constraint_name = rc.unique_constraint_name
                        WHERE tc.constraint_type = 'FOREIGN KEY'
                          AND tc.table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """
                    )
                    fk_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Envelope 1.1 (gap/02 N1): view text, routines with bodies,
                    # object comments and source grants.
                    schema_names = sorted({str(row["table_schema"]) for row in column_rows})
                    view_keys = sorted(
                        {
                            (str(row["table_schema"]), str(row["table_name"]))
                            for row in column_rows
                            if normalize_object_type(str(row.get("table_type") or ""))
                            in _VIEW_OBJECT_TYPES
                        }
                    )
                    envelope = _fetch_envelope_rows(
                        cur,
                        database=self._params.database or catalog_name,
                        schema_names=schema_names,
                        view_keys=view_keys,
                    )

                finally:
                    cur.close()
            finally:
                conn.close()

            # Assemble into Atlas Catalog Graph
            return _assemble_snowflake_catalog(
                catalog_name, column_rows, pk_rows, fk_rows, envelope=envelope
            )

        return await asyncio.to_thread(_sync_discover)

    async def estimate_read_query(
        self, sql: str, *, timeout_seconds: int = 30
    ) -> QueryEstimate:
        """Run EXPLAIN USING JSON to extract cost and partition pruning estimates."""

        def _sync_estimate() -> QueryEstimate:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(f"EXPLAIN USING JSON {sql}")
                    rows = cur.fetchall()
                    if rows and len(rows) > 0 and len(rows[0]) > 0:
                        plan_payload = rows[0][0]
                        return _extract_snowflake_explain_estimate(plan_payload)
                    return QueryEstimate(score=1.0, kind="SNOWFLAKE_EXPLAIN_FALLBACK")
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_estimate)

    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int = 1000,
        column_batch_size: int = 20,
        timeout_seconds: int = 30,
    ) -> TableProfileSnapshot:
        """Compute bounded statistical metrics on the target table."""

        def _sync_profile() -> TableProfileSnapshot:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    db = self._params.database or "DB"
                    target = _qualified_table(db, schema_name, table_name)

                    # Row counts (exact & sampled)
                    cur.execute(f"SELECT COUNT(*) FROM {target}")  # noqa: S608
                    row = cur.fetchone()
                    row_count = int(row[0]) if row and row[0] is not None else 0

                    column_snapshots: list[ColumnProfileSnapshot] = []
                    for start in range(0, len(column_names), column_batch_size):
                        batch = column_names[start : start + column_batch_size]
                        for col in batch:
                            quoted_col = _quote_identifier(col)
                            cur.execute(
                                f"""
                                SELECT
                                    COUNT(*) - COUNT({quoted_col}) AS null_count,
                                    COUNT({quoted_col}) AS non_null_count,
                                    APPROX_COUNT_DISTINCT({quoted_col}) AS distinct_estimate
                                FROM {target}
                                """  # noqa: S608
                            )
                            stats = cur.fetchone()
                            null_c = int(stats[0]) if stats and stats[0] is not None else 0
                            non_null_c = int(stats[1]) if stats and stats[1] is not None else 0
                            approx_distinct = int(stats[2]) if stats and stats[2] is not None else 0

                            column_snapshots.append(
                                ColumnProfileSnapshot(
                                    name=col,
                                    null_count=null_c,
                                    non_null_count=non_null_c,
                                    approximate_distinct_count=approx_distinct,
                                    min_length=None,
                                    max_length=None,
                                )
                            )

                    return TableProfileSnapshot(
                        row_count_estimate=row_count,
                        sampled_row_count=min(row_count, sample_rows),
                        columns=tuple(column_snapshots),
                    )
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_profile)

    async def execute_read_query(
        self,
        sql: str,
        *,
        timeout_seconds: int = 30,
        max_rows: int = 1000,
    ) -> QueryResult:
        """Execute a read-only query bounded by max_rows and timeout."""

        def _sync_execute() -> QueryResult:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    # Enforce statement timeout parameter
                    cur.execute(
                        f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}"
                    )
                    cur.execute(sql)
                    col_names = [desc[0] for desc in cur.description] if cur.description else []
                    rows_raw = cur.fetchmany(max_rows)
                    rows = tuple(dict(zip(col_names, row, strict=False)) for row in rows_raw)
                    sf_query_id = getattr(cur, "sfqid", None)
                    return QueryResult(rows=rows, warehouse_query_id=sf_query_id)
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_execute)

    async def get_query_history(
        self,
        *,
        since: datetime,
        limit: int = 5_000,
        timeout_seconds: int = 30,
    ) -> tuple[QueryLogEntry, ...]:
        """CN-9. Read this account's own query log from
        `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`.

        Deliberately reads only `QUERY_ID`, `QUERY_TEXT`, and `START_TIME` --
        never a result row -- and scopes to this connector's own database and
        to successful queries only, so a failed/cancelled statement (which
        may be a truncated or malformed fragment) never reaches the parser.
        Bounded on both axes the module docstring requires: `since` bounds
        the time window, `limit` caps the row count, enforced by the query
        itself (`LIMIT`) rather than trusted to a caller that might not
        apply one.

        `ACCOUNT_USAGE` requires the connector's role to hold `IMPORTED
        PRIVILEGES` on the `SNOWFLAKE` database -- a broader grant than
        discovery needs. A role without it gets Snowflake's own permission
        error; this method does not catch it and fail closed to an empty
        result, because that would look identical to "the warehouse ran
        nothing in this window" and CN-9's exit condition requires that
        distinction stay visible to certification.
        """

        def _sync_get_query_history() -> tuple[QueryLogEntry, ...]:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    database = (self._params.database or "").upper()
                    cur.execute(
                        """
                        SELECT query_id, query_text, start_time
                        FROM snowflake.account_usage.query_history
                        WHERE start_time >= %(since)s
                          AND execution_status = 'SUCCESS'
                          AND (%(database)s = '' OR upper(database_name) = %(database)s)
                        ORDER BY start_time DESC
                        LIMIT %(limit)s
                        """,
                        {"since": since, "database": database, "limit": limit},
                    )
                    rows = _rows_to_dicts(cur, cur.fetchall())
                finally:
                    cur.close()
            finally:
                conn.close()

            return tuple(
                QueryLogEntry(
                    query_id=str(row["query_id"]),
                    sql_text=str(row["query_text"]),
                    executed_at=row.get("start_time"),
                )
                for row in rows
                if row.get("query_id") is not None and row.get("query_text") is not None
            )

        return await asyncio.to_thread(_sync_get_query_history)
