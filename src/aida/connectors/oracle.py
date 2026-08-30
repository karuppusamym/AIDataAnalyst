import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import unquote, urlsplit

import oracledb

from aida.connectors.base import (
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredGrant,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredViewDefinition,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    append_grouped_foreign_key_rows,
    append_grouped_index_rows,
    append_grouped_key_rows,
    append_partition_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
    normalize_object_type,
)
from aida.connectors.sql_execution import SqlExecutor

_EXCLUDED_SCHEMAS = (
    "SYS",
    "SYSTEM",
    "OUTLN",
    "XDB",
    "ORDS_METADATA",
    "ORDS_PUBLIC_USER",
    "APPQOSSYS",
    "DBSFWUSER",
    "DBSNMP",
    "GSMADMIN_INTERNAL",
    "MDSYS",
    "OLAPSYS",
    "ORDDATA",
    "ORDPLUGINS",
    "CTXSYS",
    "WMSYS",
    "GGSYS",
    "REMOTE_SCHEDULER_AGENT",
    "DVSYS",
    "DVF",
    "LBACSYS",
    "AUDSYS",
    "SYSBACKUP",
    "SYSKM",
    "SYSRAC",
    "SYSDG",
)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# Oracle LOB and long-form types reject COUNT(DISTINCT ...) and TO_CHAR(...) directly;
# profiling falls back to honest placeholders instead of failing the whole batch.
_LOB_LIKE_TYPES = frozenset({"BLOB", "CLOB", "NCLOB", "LONG", "LONG RAW", "BFILE", "XMLTYPE"})


def _profile_expressions(quoted_column: str, position: int, data_type: str) -> list[str]:
    """Build the per-column aggregate expressions used by bounded profiling.

    Standard scalar types get exact null/non-null counts, an approximate distinct
    count, and character-length bounds via TO_CHAR. LOB-like types only support the
    null/non-null counts; distinct-count and length expressions are replaced with
    honest static placeholders rather than raising or fabricating a value.
    """
    if data_type.upper() in _LOB_LIKE_TYPES:
        distinct_expression = f"CAST(0 AS NUMBER) AS d_{position}"
        min_length_expression = f"CAST(NULL AS NUMBER) AS minl_{position}"
        max_length_expression = f"CAST(NULL AS NUMBER) AS maxl_{position}"
    else:
        text_form = f"LENGTH(TO_CHAR({quoted_column}))"
        distinct_expression = f"COUNT(DISTINCT {quoted_column}) AS d_{position}"
        min_length_expression = f"MIN({text_form}) AS minl_{position}"
        max_length_expression = f"MAX({text_form}) AS maxl_{position}"
    return [
        f"SUM(CASE WHEN {quoted_column} IS NULL THEN 1 ELSE 0 END) AS n_{position}",
        f"COUNT({quoted_column}) AS nn_{position}",
        distinct_expression,
        min_length_expression,
        max_length_expression,
    ]


@dataclass(frozen=True, slots=True)
class _ConnectionParams:
    host: str
    port: int
    service_name: str
    user: str
    password: str


def _parse_dsn(dsn: str) -> _ConnectionParams:
    """Parse an opaque resolved-secret value shaped as oracle://user:password@host:port/service_name.

    The credential_reference the API accepts is never a connection string; only the
    secret value it resolves to may be. This mirrors how PostgresConnector and
    SqlServerConnector treat their resolved secrets as driver-ready values, adapted
    because python-oracledb's connect_async() takes an "easy connect" dsn string
    (host:port/service_name) rather than a full URL.
    """
    parsed = urlsplit(dsn)
    if parsed.scheme != "oracle":
        raise ValueError(
            "invalid Oracle connection reference; expected "
            "oracle://user:password@host:port/service_name"
        )
    if not parsed.hostname or not parsed.username or parsed.password is None:
        raise ValueError("Oracle connection reference is missing host, user, or password")
    service_name = parsed.path.lstrip("/")
    if not service_name:
        raise ValueError("Oracle connection reference must include a service name")
    return _ConnectionParams(
        host=parsed.hostname,
        port=parsed.port or 1521,
        service_name=service_name,
        user=unquote(parsed.username),
        password=unquote(parsed.password),
    )


def _schema_exclusion_clause(alias: str) -> str:
    quoted = ", ".join(f"'{name}'" for name in _EXCLUDED_SCHEMAS)
    return f"{alias} NOT IN ({quoted})"


# Envelope 1.1 (gap/02 N1). Oracle exposes a view's text, a PL/SQL body and an
# argument list through LONG columns and through dictionary views a least-privilege
# reader may not hold. Every one of those failure modes has to arrive as an explicit
# reason rather than as an empty definition, so the helpers below are pure and unit
# tested against each of them.
#
# A definition longer than this is stored as a prefix with `truncated=True`. It is
# never silently whole-looking: view-DDL lineage (N2) would read a silent clip as a
# lineage gap in the estate rather than a gap in this extraction.
_MAX_DEFINITION_CHARACTERS = 1_000_000

# `wrap`ped PL/SQL announces itself in the first few lines of ALL_SOURCE.
_WRAP_MARKER_LINES = 4

# ALL_SOURCE splits a package into its spec and its body; both belong to the one
# routine the envelope reports.
_SOURCE_TYPES_FOR_OBJECT: dict[str, tuple[str, ...]] = {
    "PACKAGE": ("PACKAGE", "PACKAGE BODY"),
    "PROCEDURE": ("PROCEDURE",),
    "FUNCTION": ("FUNCTION",),
}

_ARGUMENT_MODES = {"IN": "IN", "OUT": "OUT", "IN/OUT": "INOUT"}

_AUTHID_TO_SECURITY_MODE = {"DEFINER": "DEFINER", "CURRENT_USER": "INVOKER"}


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_view_definition(
    definition_text: object,
    declared_length: object,
    *,
    object_label: str,
    is_materialized: bool = False,
    max_characters: int = _MAX_DEFINITION_CHARACTERS,
) -> DiscoveredViewDefinition:
    """Turn one ALL_VIEWS / ALL_MVIEWS row into an honest view definition.

    ``ALL_VIEWS.TEXT`` and ``ALL_MVIEWS.QUERY`` are LONG columns, and a LONG the
    session could not materialise arrives as NULL or as an empty value while the
    companion length column (``TEXT_LENGTH`` / ``QUERY_LEN``) still reports the real
    size. Both are recorded as *unavailable*, never as an empty definition: an empty
    ``definition_sql`` is reserved for a view whose text really is empty.

    Oracle's ``ALL_VIEWS`` carries no updatability or WITH CHECK OPTION column, so
    ``is_updatable`` and ``check_option`` stay ``None`` rather than being guessed.
    """
    declared = _optional_int(declared_length)
    if definition_text is None:
        return DiscoveredViewDefinition(
            definition_sql=None,
            is_materialized=is_materialized,
            unavailable_reason=(
                f"Oracle returned NULL for the definition text of {object_label}; "
                "the LONG column was not readable in this session"
            ),
        )
    text = str(definition_text)
    if not text and declared:
        return DiscoveredViewDefinition(
            definition_sql=None,
            is_materialized=is_materialized,
            unavailable_reason=(
                f"Oracle reported {declared} characters of definition text for "
                f"{object_label} but the LONG column fetched as an empty value"
            ),
        )
    truncated = declared is not None and len(text) < declared
    if len(text) > max_characters:
        text = text[:max_characters]
        truncated = True
    return DiscoveredViewDefinition(
        definition_sql=text,
        is_materialized=is_materialized,
        truncated=truncated,
    )


def _is_wrapped_source(body: str) -> bool:
    """True when ALL_SOURCE returned Oracle's obfuscated `wrap` output."""
    for line in body.splitlines()[:_WRAP_MARKER_LINES]:
        stripped = line.strip().lower()
        if stripped == "wrapped" or stripped.endswith(" wrapped"):
            return True
    return False


def _build_routine_body(
    source_lines: Sequence[object],
    *,
    object_label: str,
    max_characters: int = _MAX_DEFINITION_CHARACTERS,
) -> tuple[str | None, bool, str | None]:
    """Return ``(body_sql, truncated, unavailable_reason)`` for one PL/SQL object.

    Three states, kept apart on purpose: no ALL_SOURCE rows (not visible to this
    session), a wrapped body (present but obfuscated, so not a body anything can
    parse), and a body longer than the cap (a prefix, flagged as such).
    """
    if not source_lines:
        return (
            None,
            False,
            f"ALL_SOURCE exposed no rows for {object_label}; PL/SQL text is visible "
            "only to the owner and to a session holding an explicit privilege on it",
        )
    body = "".join(str(line) for line in source_lines)
    if _is_wrapped_source(body):
        return (
            None,
            False,
            f"the PL/SQL source of {object_label} is wrapped; ALL_SOURCE returns only "
            "the obfuscated form, which is not a parseable body",
        )
    if len(body) > max_characters:
        return body[:max_characters], True, None
    return body, False, None


def _normalize_argument_mode(value: object) -> str:
    if value is None:
        return "IN"
    normalized = str(value).strip().upper()
    return _ARGUMENT_MODES.get(normalized, normalized)


@dataclass(frozen=True, slots=True)
class _OracleEnvelopeRows:
    """Row sets behind envelope 1.1, plus why an axis is missing when it is.

    ``unavailable`` maps an axis name to the reason its dictionary view refused. It
    is what stops a denied ``ALL_TAB_PRIVS`` from reading as "this schema grants
    nothing", which is the failure INV-9 exists to prevent.
    """

    views: tuple[dict[str, Any], ...] = ()
    materialized_views: tuple[dict[str, Any], ...] = ()
    routines: tuple[dict[str, Any], ...] = ()
    routine_source: tuple[dict[str, Any], ...] = ()
    arguments: tuple[dict[str, Any], ...] = ()
    table_comments: tuple[dict[str, Any], ...] = ()
    column_comments: tuple[dict[str, Any], ...] = ()
    grants: tuple[dict[str, Any], ...] = ()
    unavailable: tuple[tuple[str, str], ...] = ()

    def reason(self, axis: str) -> str | None:
        for name, message in self.unavailable:
            if name == axis:
                return message
        return None


def _envelope_routine_parameters(
    envelope: _OracleEnvelopeRows,
) -> tuple[dict[tuple[str, str], list[DiscoveredRoutineParameter]], dict[tuple[str, str], str]]:
    """Group ALL_ARGUMENTS rows into parameter lists and return types.

    Only rows with a NULL ``PACKAGE_NAME`` are used. Oracle records a packaged
    subprogram's arguments against the subprogram, not against the package object the
    envelope reports, so a package honestly carries no parameter list rather than an
    arbitrary merge of its subprograms'.

    ``POSITION = 0`` is a function's return value, not a parameter.
    ``ALL_ARGUMENTS.DEFAULT_VALUE`` is a LONG that Oracle does not populate, so
    ``default_expression`` is always ``None`` here.
    """
    parameters: dict[tuple[str, str], list[DiscoveredRoutineParameter]] = {}
    return_types: dict[tuple[str, str], str] = {}
    for row in envelope.arguments:
        if row.get("PACKAGE_NAME") is not None:
            continue
        key = (str(row["OWNER"]), str(row["OBJECT_NAME"]))
        position = _optional_int(row.get("POSITION")) or 0
        data_type = _optional_text(row.get("DATA_TYPE"))
        if position == 0:
            if data_type is not None:
                return_types[key] = data_type
            continue
        parameters.setdefault(key, []).append(
            DiscoveredRoutineParameter(
                name=_optional_text(row.get("ARGUMENT_NAME")),
                ordinal_position=position,
                mode=_normalize_argument_mode(row.get("IN_OUT")),
                physical_type=data_type or "",
            )
        )
    return parameters, return_types


def _envelope_routines(envelope: _OracleEnvelopeRows) -> dict[str, list[DiscoveredRoutine]]:
    source_by_object: dict[tuple[str, str, str], list[str]] = {}
    for row in envelope.routine_source:
        key = (str(row["OWNER"]), str(row["NAME"]), str(row["TYPE"]))
        source_by_object.setdefault(key, []).append(str(row["TEXT"] or ""))

    parameters, return_types = _envelope_routine_parameters(envelope)
    source_reason = envelope.reason("routine_source")

    routines: dict[str, list[DiscoveredRoutine]] = {}
    for row in envelope.routines:
        owner = str(row["OWNER"])
        name = str(row["OBJECT_NAME"])
        object_type = normalize_object_type(str(row["OBJECT_TYPE"]))
        label = f"{owner}.{name}"
        lines: list[str] = []
        for source_type in _SOURCE_TYPES_FOR_OBJECT.get(object_type, (object_type,)):
            lines.extend(source_by_object.get((owner, name, source_type), []))
        body, truncated, reason = _build_routine_body(lines, object_label=label)
        if body is None and source_reason is not None:
            reason = source_reason
        attributes: dict[str, Any] = {}
        if reason is not None and "wrapped" in reason:
            attributes["wrapped"] = True
        if object_type == "PACKAGE":
            attributes["packaged_subprogram_parameters"] = (
                "ALL_ARGUMENTS records arguments against each packaged subprogram, "
                "not against the package object, so this routine carries none"
            )
        deterministic = _optional_text(row.get("DETERMINISTIC"))
        routines.setdefault(owner, []).append(
            DiscoveredRoutine(
                name=name,
                routine_type=object_type,
                language="PLSQL" if body is not None else None,
                body_sql=body,
                parameters=tuple(parameters.get((owner, name), ())),
                return_type=return_types.get((owner, name)),
                is_deterministic=(
                    None if deterministic is None else deterministic.upper() == "YES"
                ),
                security_mode=_AUTHID_TO_SECURITY_MODE.get(
                    (_optional_text(row.get("AUTHID")) or "").upper()
                ),
                source_description=None,
                truncated=truncated,
                unavailable_reason=reason,
                attributes=attributes,
            )
        )
    return routines


def _envelope_grants(envelope: _OracleEnvelopeRows) -> dict[str, list[DiscoveredGrant]]:
    grants: dict[str, list[DiscoveredGrant]] = {}
    for row in envelope.grants:
        schema_name = str(row["TABLE_SCHEMA"])
        grants.setdefault(schema_name, []).append(
            DiscoveredGrant(
                grantee=str(row["GRANTEE"]),
                grantee_type=str(row.get("GRANTEE_TYPE") or "UNKNOWN"),
                privilege=str(row["PRIVILEGE"]),
                object_type=_optional_text(row.get("OBJECT_TYPE")) or "TABLE",
                object_name=str(row["TABLE_NAME"]),
                schema_name=schema_name,
                is_grantable=str(row.get("GRANTABLE") or "NO").strip().upper() == "YES",
            )
        )
    return grants


def _apply_envelope(
    catalogs: tuple[DiscoveredCatalog, ...], envelope: _OracleEnvelopeRows
) -> tuple[DiscoveredCatalog, ...]:
    """Fold envelope 1.1 rows onto an already-assembled catalog.

    Written as a rebuild rather than as a change to `aida.connectors.discovery`
    because the shared assembly helpers are on the v1.0 contract and are used by
    connectors this workstream does not own.
    """
    view_definitions = {
        (str(row["OWNER"]), str(row["VIEW_NAME"])): _build_view_definition(
            row.get("TEXT"),
            row.get("TEXT_LENGTH"),
            object_label=f"{row['OWNER']}.{row['VIEW_NAME']}",
        )
        for row in envelope.views
    }
    materialized_definitions = {
        (str(row["OWNER"]), str(row["MVIEW_NAME"])): _build_view_definition(
            row.get("QUERY"),
            row.get("QUERY_LEN"),
            object_label=f"{row['OWNER']}.{row['MVIEW_NAME']}",
            is_materialized=True,
        )
        for row in envelope.materialized_views
    }
    table_comments = {
        (str(row["OWNER"]), str(row["TABLE_NAME"])): _optional_text(row.get("COMMENTS"))
        for row in envelope.table_comments
    }
    column_comments = {
        (str(row["OWNER"]), str(row["TABLE_NAME"]), str(row["COLUMN_NAME"])): _optional_text(
            row.get("COMMENTS")
        )
        for row in envelope.column_comments
    }
    routines = _envelope_routines(envelope)
    grants = _envelope_grants(envelope)
    views_reason = envelope.reason("views")

    rebuilt: list[DiscoveredCatalog] = []
    for catalog in catalogs:
        schemas = []
        for schema in catalog.schemas:
            tables = []
            for table in schema.tables:
                key = (schema.name, table.name)
                definition = materialized_definitions.get(key)
                if definition is None and table.object_type == "VIEW":
                    definition = view_definitions.get(key) or DiscoveredViewDefinition(
                        definition_sql=None,
                        unavailable_reason=views_reason
                        or (
                            f"ALL_VIEWS exposed no row for {schema.name}.{table.name}; "
                            "its text was not visible to this session"
                        ),
                    )
                columns = tuple(
                    replace(
                        column,
                        source_description=column_comments.get(
                            (schema.name, table.name, column.name)
                        ),
                    )
                    for column in table.columns
                )
                tables.append(
                    replace(
                        table,
                        columns=columns,
                        source_description=table_comments.get(key),
                        view_definition=definition,
                    )
                )
            schemas.append(
                replace(
                    schema,
                    tables=tuple(tables),
                    routines=tuple(routines.get(schema.name, ())),
                    grants=tuple(grants.get(schema.name, ())),
                )
            )
        attributes = dict(catalog.attributes)
        if envelope.unavailable:
            attributes["envelope_v11_unavailable"] = dict(envelope.unavailable)
        rebuilt.append(replace(catalog, schemas=tuple(schemas), attributes=attributes))
    return tuple(rebuilt)


async def _fetch_optional_rows(
    cursor: Any, sql: str
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Run one supplementary metadata query, turning a refusal into a reason.

    Envelope 1.1 reads dictionary views a least-privilege reader may not hold
    (`ALL_SOURCE`, `ALL_TAB_PRIVS`, `ALL_MVIEWS`). A denial must not read as "the
    source has none of these", so it comes back as a reason string that lands on the
    object or on the catalog rather than as a silent empty list.
    """
    try:
        await cursor.execute(sql)
        rows = _rows_as_dicts(cursor.description, await cursor.fetchall())
    except Exception as exc:
        return (), f"{type(exc).__name__}: {exc}"
    return tuple(rows), None


async def _fetch_envelope_rows(cursor: Any) -> _OracleEnvelopeRows:
    """Read every envelope 1.1 axis Oracle exposes, recording each refusal."""
    owner_clause = _schema_exclusion_clause("owner")
    unavailable: list[tuple[str, str]] = []

    async def _collect(axis: str, sql: str) -> tuple[dict[str, Any], ...]:
        rows, reason = await _fetch_optional_rows(cursor, sql)
        if reason is not None:
            unavailable.append((axis, reason))
        return rows

    views = await _collect(
        "views",
        f"SELECT owner, view_name, text_length, text FROM ALL_VIEWS WHERE {owner_clause}",  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    materialized_views = await _collect(
        "materialized_views",
        f"SELECT owner, mview_name, query_len, query FROM ALL_MVIEWS WHERE {owner_clause}",  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    routines = await _collect(
        "routines",
        f"""
        SELECT
            ao.owner AS owner,
            ao.object_name AS object_name,
            ao.object_type AS object_type,
            ap.deterministic AS deterministic,
            ap.authid AS authid
        FROM ALL_OBJECTS ao
        LEFT JOIN ALL_PROCEDURES ap
          ON ap.owner = ao.owner
         AND ap.object_name = ao.object_name
         AND ap.procedure_name IS NULL
        WHERE ao.object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE')
          AND {_schema_exclusion_clause("ao.owner")}
        ORDER BY ao.owner, ao.object_name
        """,  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    routine_source = await _collect(
        "routine_source",
        f"""
        SELECT owner, name, type, line, text
        FROM ALL_SOURCE
        WHERE type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')
          AND {owner_clause}
        ORDER BY owner, name, type, line
        """,  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    arguments = await _collect(
        "arguments",
        f"""
        SELECT owner, object_name, package_name, argument_name, position, data_type, in_out
        FROM ALL_ARGUMENTS
        WHERE data_level = 0 AND {owner_clause}
        ORDER BY owner, object_name, position
        """,  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    table_comments = await _collect(
        "table_comments",
        f"SELECT owner, table_name, comments FROM ALL_TAB_COMMENTS WHERE {owner_clause}",  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    column_comments = await _collect(
        "column_comments",
        f"""
        SELECT owner, table_name, column_name, comments
        FROM ALL_COL_COMMENTS
        WHERE {owner_clause}
        """,  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    # ALL_TAB_PRIVS names the owning schema TABLE_SCHEMA, where DBA_TAB_PRIVS names it
    # OWNER. ALL_USERS separates a user grantee from a role grantee; Oracle's privilege
    # views do not say which a grantee is.
    grants = await _collect(
        "grants",
        f"""
        SELECT
            p.grantee AS grantee,
            p.table_schema AS table_schema,
            p.table_name AS table_name,
            p.privilege AS privilege,
            p.grantable AS grantable,
            p.type AS object_type,
            CASE
                WHEN p.grantee = 'PUBLIC' THEN 'PUBLIC'
                WHEN u.username IS NOT NULL THEN 'USER'
                ELSE 'ROLE'
            END AS grantee_type
        FROM ALL_TAB_PRIVS p
        LEFT JOIN ALL_USERS u ON u.username = p.grantee
        WHERE {_schema_exclusion_clause("p.table_schema")}
        ORDER BY p.table_schema, p.table_name, p.grantee, p.privilege
        """,  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
    )
    return _OracleEnvelopeRows(
        views=views,
        materialized_views=materialized_views,
        routines=routines,
        routine_source=routine_source,
        arguments=arguments,
        table_comments=table_comments,
        column_comments=column_comments,
        grants=grants,
        unavailable=tuple(unavailable),
    )


class OracleConnector(SqlExecutor):
    connector_type = "oracle"
    dialect = "oracle"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        # CT-3/CN-8: indexes -> ALL_INDEXES/ALL_IND_COLUMNS; partitions ->
        # ALL_PART_TABLES + ALL_PART_KEY_COLUMNS + ALL_TAB_PARTITIONS.
        indexes=True,
        partitions=True,
        explain=False,
        delegated_identity=False,
        approximate_statistics=True,
        # Envelope 1.1 (gap/02 N1). Each flag is set because `discover()` reads the
        # named dictionary view and lands the result on the envelope, and each
        # refusal arrives as an `unavailable_reason` rather than as an empty value.
        views=True,  # ALL_VIEWS.TEXT, ALL_MVIEWS.QUERY
        routines=True,  # ALL_OBJECTS + ALL_PROCEDURES, ALL_SOURCE, ALL_ARGUMENTS
        object_comments=True,  # ALL_TAB_COMMENTS, ALL_COL_COMMENTS (table and column)
        grants=True,  # ALL_TAB_PRIVS
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    async def _connect(self, *, timeout_seconds: float) -> Any:
        connection = await oracledb.connect_async(
            user=self._params.user,
            password=self._params.password,
            dsn=f"{self._params.host}:{self._params.port}/{self._params.service_name}",
            tcp_connect_timeout=min(timeout_seconds, 15.0),
        )
        connection.call_timeout = int(timeout_seconds * 1000)
        connection.autocommit = False
        return connection

    async def test_connection(self) -> None:
        connection = await self._connect(timeout_seconds=self._command_timeout)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM DUAL")
                await cursor.fetchall()
        finally:
            await connection.close()

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        connection = await self._connect(timeout_seconds=self._command_timeout)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT SYS_CONTEXT('USERENV', 'DB_NAME') AS catalog_name FROM DUAL"
                )
                catalog_row = await cursor.fetchone()
                catalog_name = str(catalog_row[0]) if catalog_row else ""

                columns_query = f"""
                    SELECT
                        atc.owner AS table_schema,
                        atc.table_name AS table_name,
                        CASE WHEN ao.object_type = 'TABLE' THEN 'BASE TABLE'
                             ELSE ao.object_type END AS table_type,
                        atc.column_name AS column_name,
                        atc.column_id AS ordinal_position,
                        atc.data_type AS data_type,
                        atc.nullable AS is_nullable,
                        atc.data_default AS column_default
                    FROM ALL_TAB_COLUMNS atc
                    JOIN ALL_OBJECTS ao
                      ON ao.owner = atc.owner
                     AND ao.object_name = atc.table_name
                     AND ao.object_type IN ('TABLE', 'VIEW')
                    WHERE {_schema_exclusion_clause("atc.owner")}
                    ORDER BY atc.owner, atc.table_name, atc.column_id
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(columns_query)
                column_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "table_type": row["TABLE_TYPE"],
                        "column_name": row["COLUMN_NAME"],
                        "ordinal_position": row["ORDINAL_POSITION"],
                        "data_type": row["DATA_TYPE"],
                        "is_nullable": row["IS_NULLABLE"],
                        "column_default": row["COLUMN_DEFAULT"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                keys_query = f"""
                    SELECT
                        ac.owner AS table_schema,
                        ac.table_name AS table_name,
                        ac.constraint_name AS constraint_name,
                        ac.constraint_type AS constraint_type,
                        acc.column_name AS column_name,
                        acc.position AS ordinal_position
                    FROM ALL_CONSTRAINTS ac
                    JOIN ALL_CONS_COLUMNS acc
                      ON acc.owner = ac.owner AND acc.constraint_name = ac.constraint_name
                    WHERE ac.constraint_type IN ('P', 'U')
                      AND {_schema_exclusion_clause("ac.owner")}
                    ORDER BY ac.owner, ac.table_name, ac.constraint_name, acc.position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(keys_query)
                key_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "constraint_name": row["CONSTRAINT_NAME"],
                        "constraint_type": row["CONSTRAINT_TYPE"],
                        "column_name": row["COLUMN_NAME"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                foreign_keys_query = f"""
                    SELECT
                        ac.owner AS table_schema,
                        ac.table_name AS table_name,
                        ac.constraint_name AS constraint_name,
                        r_ac.owner AS referenced_schema,
                        r_ac.table_name AS referenced_table,
                        acc.column_name AS column_name,
                        r_acc.column_name AS referenced_column,
                        acc.position AS ordinal_position
                    FROM ALL_CONSTRAINTS ac
                    JOIN ALL_CONS_COLUMNS acc
                      ON acc.owner = ac.owner AND acc.constraint_name = ac.constraint_name
                    JOIN ALL_CONSTRAINTS r_ac
                      ON r_ac.owner = ac.r_owner AND r_ac.constraint_name = ac.r_constraint_name
                    JOIN ALL_CONS_COLUMNS r_acc
                      ON r_acc.owner = r_ac.owner
                     AND r_acc.constraint_name = r_ac.constraint_name
                     AND r_acc.position = acc.position
                    WHERE ac.constraint_type = 'R'
                      AND {_schema_exclusion_clause("ac.owner")}
                    ORDER BY ac.owner, ac.table_name, ac.constraint_name, acc.position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(foreign_keys_query)
                foreign_key_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "constraint_name": row["CONSTRAINT_NAME"],
                        "referenced_schema": row["REFERENCED_SCHEMA"],
                        "referenced_table": row["REFERENCED_TABLE"],
                        "column_name": row["COLUMN_NAME"],
                        "referenced_column": row["REFERENCED_COLUMN"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                # CT-3/CN-8: ALL_INDEXES/ALL_IND_COLUMNS mirror ALL_CONSTRAINTS/
                # ALL_CONS_COLUMNS above. Whether an index backs a PRIMARY KEY
                # constraint is surfaced via a LEFT JOIN on (owner, index_name)
                # rather than a second round trip.
                indexes_query = f"""
                    SELECT
                        ai.owner AS table_schema,
                        ai.table_name AS table_name,
                        ai.index_name AS index_name,
                        ai.index_type AS index_type,
                        ai.uniqueness AS uniqueness,
                        aic.column_name AS column_name,
                        aic.column_position AS ordinal_position,
                        ac.constraint_type AS backing_constraint_type
                    FROM ALL_INDEXES ai
                    JOIN ALL_IND_COLUMNS aic
                      ON aic.index_owner = ai.owner
                     AND aic.index_name = ai.index_name
                     AND aic.table_name = ai.table_name
                    LEFT JOIN ALL_CONSTRAINTS ac
                      ON ac.owner = ai.owner
                     AND ac.constraint_name = ai.index_name
                     AND ac.constraint_type = 'P'
                    WHERE {_schema_exclusion_clause("ai.owner")}
                    ORDER BY ai.owner, ai.table_name, ai.index_name, aic.column_position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(indexes_query)
                index_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "index_name": row["INDEX_NAME"],
                        "index_type": row["INDEX_TYPE"],
                        "is_unique": str(row["UNIQUENESS"]).upper() == "UNIQUE",
                        "is_primary": row["BACKING_CONSTRAINT_TYPE"] == "P",
                        "column_name": row["COLUMN_NAME"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                partition_type_query = f"""
                    SELECT owner AS table_schema, table_name AS table_name,
                           partitioning_type AS partition_type
                    FROM ALL_PART_TABLES
                    WHERE {_schema_exclusion_clause("owner")}
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(partition_type_query)
                partition_types = {
                    (row["TABLE_SCHEMA"], row["TABLE_NAME"]): row["PARTITION_TYPE"]
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                }

                partition_key_query = f"""
                    SELECT owner AS table_schema, name AS table_name,
                           column_name AS column_name, column_position AS ordinal_position
                    FROM ALL_PART_KEY_COLUMNS
                    WHERE object_type = 'TABLE' AND {_schema_exclusion_clause("owner")}
                    ORDER BY owner, name, column_position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(partition_key_query)
                partition_key_columns: dict[tuple[str, str], list[str]] = {}
                for row in _rows_as_dicts(cursor.description, await cursor.fetchall()):
                    key = (row["TABLE_SCHEMA"], row["TABLE_NAME"])
                    partition_key_columns.setdefault(key, []).append(row["COLUMN_NAME"])

                # HIGH_VALUE is a LONG column; fetching it reliably needs an
                # output-type handler the async oracledb driver does not expose
                # the same way as the sync client, so partitions are extracted
                # without a high_value bound rather than risk a truncated or
                # failed fetch (same honesty tradeoff as the envelope helpers
                # above make for LONG columns, just without the reason-string
                # machinery since CN-8 is explicitly not an envelope 1.1 axis).
                partitions_query = f"""
                    SELECT
                        table_owner AS table_schema,
                        table_name AS table_name,
                        partition_name AS partition_name,
                        partition_position AS ordinal_position
                    FROM ALL_TAB_PARTITIONS
                    WHERE {_schema_exclusion_clause("table_owner")}
                    ORDER BY table_owner, table_name, partition_position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(partitions_query)
                partition_rows = []
                for row in _rows_as_dicts(cursor.description, await cursor.fetchall()):
                    schema_name = row["TABLE_SCHEMA"]
                    table_name = row["TABLE_NAME"]
                    partition_rows.append(
                        {
                            "table_schema": schema_name,
                            "table_name": table_name,
                            "partition_name": row["PARTITION_NAME"],
                            "ordinal_position": row["ORDINAL_POSITION"],
                            "partition_type": partition_types.get(
                                (schema_name, table_name), "UNKNOWN"
                            ),
                            "key_columns": partition_key_columns.get((schema_name, table_name), []),
                        }
                    )

                envelope = await _fetch_envelope_rows(cursor)
        finally:
            await connection.close()

        return _assemble_catalog(
            catalog_name,
            column_rows,
            key_rows,
            foreign_key_rows,
            envelope=envelope,
            index_rows=index_rows,
            partition_rows=partition_rows,
        )

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        connection = await self._connect(timeout_seconds=timeout_seconds)
        statement_id = uuid.uuid4().hex[:28]
        try:
            async with connection.cursor() as cursor:
                try:
                    await cursor.execute(
                        f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {sql}"
                    )
                    await cursor.execute(
                        "SELECT cost, cardinality FROM plan_table "
                        "WHERE statement_id = :1 AND id = 0",
                        [statement_id],
                    )
                    row = await cursor.fetchone()
                    if row is None or row[0] is None:
                        raise RuntimeError("source returned an EXPLAIN PLAN without a total cost")
                    total_cost = float(row[0])
                    estimated_rows = float(row[1]) if row[1] is not None else None
                    return QueryEstimate(
                        score=total_cost,
                        kind="EXPLAIN_PLAN_COST",
                        estimated_rows=estimated_rows,
                        evidence={
                            "statement_id": statement_id,
                            "cost": total_cost,
                            "cardinality": estimated_rows,
                        },
                    )
                finally:
                    await cursor.execute(
                        "DELETE FROM plan_table WHERE statement_id = :1", [statement_id]
                    )
        finally:
            await connection.rollback()
            await connection.close()

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        connection = await self._connect(timeout_seconds=timeout_seconds)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM DUAL")
                session_row = await cursor.fetchone()
                session_id = (
                    str(session_row[0]) if session_row is not None else uuid.uuid4().hex[:12]
                )
                await cursor.execute(sql)
                rows = _rows_as_dicts(cursor.description, await cursor.fetchall())
                return QueryResult(
                    rows=tuple(rows),
                    warehouse_query_id=f"oracle-sid:{session_id}",
                )
        finally:
            await connection.rollback()
            await connection.close()

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
        """Collect bounded statistics without returning or persisting source values."""
        if not column_names:
            return TableProfileSnapshot(None, 0, ())
        if sample_rows < 1 or column_batch_size < 1:
            raise ValueError("profiling limits must be positive")
        qualified_table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
        connection = await self._connect(timeout_seconds=timeout_seconds)
        snapshots: list[ColumnProfileSnapshot] = []
        sampled_row_count = 0
        estimate: int | None = None
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT num_rows FROM ALL_TABLES WHERE owner = :1 AND table_name = :2",
                    [schema_name, table_name],
                )
                estimate_row = await cursor.fetchone()
                if estimate_row is not None and estimate_row[0] is not None:
                    estimate = int(estimate_row[0])

                await cursor.execute(
                    "SELECT column_name, data_type FROM ALL_TAB_COLUMNS "
                    "WHERE owner = :1 AND table_name = :2",
                    [schema_name, table_name],
                )
                data_types = {str(row[0]): str(row[1]) for row in await cursor.fetchall()}

                for start in range(0, len(column_names), column_batch_size):
                    batch = column_names[start : start + column_batch_size]
                    selected = ", ".join(_quote_identifier(name) for name in batch)
                    expressions = ["COUNT(*) AS sampled_row_count"]
                    for position, name in enumerate(batch):
                        quoted = _quote_identifier(name)
                        data_type = data_types.get(name, "")
                        expressions.extend(_profile_expressions(quoted, position, data_type))
                    profile_sql = (
                        f"WITH bounded_sample AS (SELECT {selected} FROM {qualified_table} "  # noqa: S608 -- identifiers are double-quoted and limits are validated integers
                        f"FETCH FIRST {int(sample_rows)} ROWS ONLY) "
                        f"SELECT {', '.join(expressions)} FROM bounded_sample"
                    )
                    await cursor.execute(profile_sql)
                    row = await cursor.fetchone()
                    if row is None:
                        continue
                    row_dict = _rows_as_dicts(cursor.description, [row])[0]
                    sampled_row_count = max(sampled_row_count, int(row_dict["SAMPLED_ROW_COUNT"]))
                    for position, name in enumerate(batch):
                        snapshots.append(
                            ColumnProfileSnapshot(
                                name=name,
                                null_count=int(row_dict[f"N_{position}"]),
                                non_null_count=int(row_dict[f"NN_{position}"]),
                                approximate_distinct_count=int(row_dict[f"D_{position}"]),
                                min_length=row_dict[f"MINL_{position}"],
                                max_length=row_dict[f"MAXL_{position}"],
                            )
                        )
        finally:
            await connection.rollback()
            await connection.close()
        return TableProfileSnapshot(
            row_count_estimate=(max(estimate, sampled_row_count) if estimate is not None else None),
            sampled_row_count=sampled_row_count,
            columns=tuple(snapshots),
        )


def _rows_as_dicts(description: Any, rows: list[Any]) -> list[dict[str, Any]]:
    columns = [column[0] for column in description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _assemble_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    key_rows: list[dict[str, Any]],
    foreign_key_rows: list[dict[str, Any]],
    *,
    envelope: _OracleEnvelopeRows | None = None,
    index_rows: list[dict[str, Any]] | None = None,
    partition_rows: list[dict[str, Any]] | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble a catalog from already-normalized, lowercase-keyed discovery rows.

    Callers (namely ``discover()``) are responsible for translating Oracle's
    uppercase-folded column names into the lowercase keys the shared
    ``aida.connectors.discovery`` helpers expect; this function performs no
    case remapping itself so its contract matches every other caller of those
    helpers.
    """
    tables = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(
        tables,
        key_rows,
        constraint_type_map={"P": "PRIMARY_KEY", "U": "UNIQUE"},
    )
    append_grouped_foreign_key_rows(tables, foreign_key_rows)
    if index_rows:
        append_grouped_index_rows(tables, index_rows)
    if partition_rows:
        append_partition_rows(tables, partition_rows)
    catalogs = assemble_catalog(str(catalog_name), tables)
    if envelope is None:
        return catalogs
    return _apply_envelope(catalogs, envelope)
