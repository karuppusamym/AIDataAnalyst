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
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

from aida.connectors.base import (
    ENTROPY_NOT_IMPLEMENTED,
    FACET_REASON_NOT_IMPLEMENTED,
    FACET_UNSUPPORTED,
    OBSERVATION_SCOPE_FULL,
    PROFILE_FACET_BLANKS,
    PROFILE_FACET_LENGTH,
    PROFILE_FACET_LENGTH_DISTRIBUTION,
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredViewDefinition,
    ProfileFacetStatus,
    QueryEstimate,
    QueryLogEntry,
    QueryResult,
    TableProfileSnapshot,
    attach_native_objects,
    build_sequences,
    rows_to_dicts,
)
from aida.connectors.capability_certification import derive_capabilities
from aida.connectors.discovery import (
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INVENTORY,
    FACET_OBJECT_COMMENTS,
    FACET_ROUTINE_BODIES,
    FACET_SEQUENCES,
    FACET_VIEW_DEFINITIONS,
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
    read_facet,
    view_definition_row,
)
from aida.connectors.schema_scope import DiscoveryScope, ScopeSql, discovery_scope
from aida.connectors.sql_execution import SqlExecutor

_COMPLEX_SCALAR_TYPES = frozenset({"VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY"})
_EXCLUDED_SCHEMAS = frozenset({"INFORMATION_SCHEMA", "ACCOUNT_USAGE", "READER_ACCOUNT_USAGE"})


def _quote_identifier(identifier: str) -> str:
    """Snowflake double-quote an identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_table(database: str, schema: str, table: str) -> str:
    """Format a fully-qualified 3-part Snowflake table identifier."""
    return f"{_quote_identifier(database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"


#: R11-FP04: the value-free facets this adapter's `profile_table` does not
#: compute, stated per column rather than left as bare `None`s. Every one of
#: them is expressible in Snowflake SQL, so the status is UNSUPPORTED with
#: reason NOT_IMPLEMENTED -- "this adapter does not ask" -- and never
#: ENGINE_LACKS_FACET, which would blame the warehouse for a gap that is ours.
_UNIMPLEMENTED_FACETS: tuple[ProfileFacetStatus, ...] = (
    ProfileFacetStatus(PROFILE_FACET_LENGTH, FACET_UNSUPPORTED, FACET_REASON_NOT_IMPLEMENTED),
    ProfileFacetStatus(PROFILE_FACET_BLANKS, FACET_UNSUPPORTED, FACET_REASON_NOT_IMPLEMENTED),
    ProfileFacetStatus(
        PROFILE_FACET_LENGTH_DISTRIBUTION, FACET_UNSUPPORTED, FACET_REASON_NOT_IMPLEMENTED
    ),
    ENTROPY_NOT_IMPLEMENTED,
)


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
    #: R11-FP01: INFORMATION_SCHEMA.SEQUENCES rows. There is no trigger field
    #: here and never will be: Snowflake has no trigger object (see
    #: `SnowflakeConnector.DEFAULT_CAPABILITIES`).
    sequences: tuple[dict[str, Any], ...] = ()
    schemata: tuple[dict[str, Any], ...] = ()
    databases: tuple[dict[str, Any], ...] = ()
    grants: tuple[dict[str, Any], ...] = ()
    unavailable: tuple[tuple[str, str], ...] = ()
    #: R11-FP02: the failures the reads above captured, as (facet, exception)
    #: pairs in read order, for `discover()` to replay into `read_facet`.
    #: Deliberately separate from `unavailable`: that is this adapter's own
    #: per-axis reason for the catalog attributes, this is the input to the
    #: platform's per-facet classification, and one refusal produces both. Named
    #: `failures`, not `refusals`: nothing in the thread has judged them yet, and
    #: a replayed failure that is not a refusal ends the run.
    failures: tuple[tuple[str, BaseException], ...] = ()

    def reason(self, axis: str) -> str | None:
        for name, message in self.unavailable:
            if name == axis:
                return message
        return None


# ---------------------------------------------------------------------------
# R11-FP02: reads captured in the driver thread, judged in the coroutine.
#
# `read_facet` is a coroutine and `snowflake.connector` is a synchronous
# driver, so every read here happens inside one `asyncio.to_thread` hop. The
# thread captures each read's failure instead of judging it and the coroutine
# replays the captures into `read_facet`, which classifies them, records them
# against their facet and decides what may be absorbed. One judgement site, no
# connection passed between pool threads.
#
# **One replay mode, since R11-FP02's follow-through (2026-09-18).** The
# envelope 1.1 axes used to be replayed with `read_facet`'s re-raise
# suppressed, because this adapter had always absorbed *any* failure of them
# into a per-axis reason. That absorption was the hazard the facet mechanism
# exists to avoid: a dropped connection on INFORMATION_SCHEMA.VIEWS classified
# UNAVAILABLE, not PERMISSION_DENIED, so `workflows.activities.
# refused_facet_existing` did not protect the view definitions an earlier run
# captured, and the FULL reconciliation retired them against a source that had
# stopped answering. Every capture is now replayed bare -- a refusal is
# absorbed and recorded, anything else is recorded and re-raised -- which is
# the rule the PostgreSQL and SQL Server adapters always had.
#
# Every relation read here is one Snowflake always has: an INFORMATION_SCHEMA
# view, or an object this same scan has just listed (a view for GET_DDL, a
# schema for SHOW GRANTS). So the replay passes `known_relations=True`, and
# Snowflake's "does not exist or not authorized" -- error 2003, which is how it
# refuses GET_DDL on a view the role may not see the text of -- is read as the
# refusal it must then be (`capability_states.HIDDEN_RELATION_SNOWFLAKE_ERRORS`).
# ---------------------------------------------------------------------------
_CapturedRead = Sequence[dict[str, Any]] | BaseException

#: See the comment above: every Snowflake discovery read names a relation that exists.
_KNOWN_RELATIONS: Final = True


@dataclass(frozen=True, slots=True)
class _CapturedReads:
    """One run's discovery reads, as the driver thread hands them back.

    `envelope` is None when the roster read failed: there is no roster to scope
    the envelope queries to, and a run with no objects has nothing to assemble
    anyway -- so the thread stops there and `discover()` replays the roster
    failure first, which ends the run the way it always did.
    """

    catalog_name: str
    columns: _CapturedRead
    primary_keys: _CapturedRead = ()
    foreign_keys: _CapturedRead = ()
    envelope: _SnowflakeEnvelopeRows | None = None


async def _captured(read: _CapturedRead) -> Sequence[dict[str, Any]]:
    """The rows a captured read returned, or the failure it captured, re-raised.

    The awaitable `read_facet` takes. Raising here rather than in the thread is
    the point: the exception reaches `read_facet` inside the coroutine that owns
    the `FacetReadScope`, so it is classified and recorded exactly as a natively
    async driver's failure is.
    """
    if isinstance(read, BaseException):
        raise read
    return read


async def _replay_axis_failures(failures: Sequence[tuple[str, BaseException]]) -> None:
    """Replay each envelope axis's captured failure through `read_facet`, bare.

    A refusal is recorded against its facet and absorbed: its rows are already
    empty and its value-free reason already rendered by the thread. Anything else
    is recorded and re-raised, which ends the run -- and is why the thread may
    word its reasons as refusals without judging anything itself.
    """
    for facet, failure in failures:
        await read_facet(facet, _captured(failure), known_relations=_KNOWN_RELATIONS)


def _refused_reason(relation: str) -> str:
    """Why an axis is empty when the source refused it -- fixed text, never the driver's.

    Before R11-FP02's follow-through this was `f"{type(exc).__name__}: {exc}"`, and it
    reached a view definition's own `unavailable_reason` verbatim: a driver message,
    which can quote the statement or a value (INV-6). A reason is now only ever kept
    for a refusal -- anything else ends the run -- so naming the relation is the
    whole of what is known.
    """
    return (
        f"{relation} was refused for this login's role; the discovery receipt records "
        "the facet as PERMISSION_DENIED"
    )


def _fetch_optional_rows(
    cursor: Any, sql: str, params: Sequence[Any] = ()
) -> tuple[tuple[dict[str, Any], ...], BaseException | None]:
    """Run one supplementary metadata query, capturing -- not judging -- its failure.

    R11-FP01: `params` are the pushed-down selection's values, bound server-side by
    position (the discovery connection is opened with `paramstyle="qmark"`), so
    nothing an operator typed is part of the statement text.
    """
    try:
        if params:
            cursor.execute(sql, list(params))
        else:
            cursor.execute(sql)
        return tuple(rows_to_dicts(cursor, cursor.fetchall())), None
    except Exception as exc:  # noqa: BLE001 -- replayed verbatim into `read_facet`
        return (), exc


#: The INFORMATION_SCHEMA views' fixed exclusion, which every schema-bound read carries.
_SYSTEM_SCHEMA_FILTER = "NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')"


def _fetch_envelope_rows(
    cursor: Any,
    *,
    database: str,
    schema_names: Sequence[str],
    view_keys: Sequence[tuple[str, str, str]],
    scope: DiscoveryScope | None = None,
) -> _SnowflakeEnvelopeRows:
    """Read every envelope 1.1 axis Snowflake exposes, capturing each failure.

    `view_keys` are the (schema, name, kind) triples of view-shaped objects discovered
    from INFORMATION_SCHEMA.TABLES. They drive the `GET_DDL` second pass, which is the
    only path to a materialized view's text -- Snowflake's INFORMATION_SCHEMA.VIEWS
    contains no row for a materialized view at all.

    R11-FP01: `scope` is the pushed-down selection. Every read takes its schema scope;
    INFORMATION_SCHEMA.VIEWS also takes its `schema.object` patterns and the VIEW kind,
    and the GET_DDL pass -- one statement per object, so no predicate to carry --
    skips the objects the same predicate would have excluded. The routine and sequence
    inventories and schema comments establish schemas and take the schema scope only
    (`aida.connectors.schema_scope`). SHOW GRANTS runs per roster schema, so the
    roster's own schema scope already bounds it.
    """
    scope = scope or DiscoveryScope()
    unavailable: list[tuple[str, str]] = []
    failures: list[tuple[str, BaseException]] = []

    def _collect(
        axis: str, relation: str, sql: str, query: ScopeSql | None, *, facet: str
    ) -> tuple[dict[str, Any], ...]:
        rows, failure = _fetch_optional_rows(cursor, sql, query.positional if query else ())
        # R11-FP02: captured, not judged. The judgement is `read_facet`'s, in the
        # coroutine; a failure that is not a refusal ends the run there, so the reason
        # rendered here only ever survives for a refusal.
        if failure is not None:
            unavailable.append((axis, _refused_reason(relation)))
            failures.append((facet, failure))
        return rows

    databases = _collect(
        "catalog_comment",
        "INFORMATION_SCHEMA.DATABASES",
        "SELECT database_name, comment FROM information_schema.databases "
        "WHERE database_name = CURRENT_DATABASE()",
        None,
        facet=FACET_OBJECT_COMMENTS,
    )
    q = ScopeSql(scope, "snowflake")
    schemata = _collect(
        "schema_comments",
        "INFORMATION_SCHEMA.SCHEMATA",
        "SELECT schema_name, comment FROM information_schema.schemata "  # noqa: S608 -- static SQL; every pushed value is a qmark bind
        f"WHERE schema_name {_SYSTEM_SCHEMA_FILTER}{q.schema('schema_name')}",
        q,
        facet=FACET_OBJECT_COMMENTS,
    )
    q = ScopeSql(scope, "snowflake")
    views = _collect(
        "views",
        "INFORMATION_SCHEMA.VIEWS",
        f"""
        SELECT
            table_schema,
            table_name,
            view_definition,
            is_secure,
            is_updatable,
            check_option
        FROM information_schema.views
        WHERE table_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("table_schema")}
          {q.names("table_schema", "table_name")}{q.kind_gate("VIEW")}
        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
        q,
        facet=FACET_VIEW_DEFINITIONS,
    )
    q = ScopeSql(scope, "snowflake")
    functions = _collect(
        "functions",
        "INFORMATION_SCHEMA.FUNCTIONS",
        f"""
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
        WHERE function_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("function_schema")}
        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
        q,
        facet=FACET_ROUTINE_BODIES,
    )
    q = ScopeSql(scope, "snowflake")
    procedures = _collect(
        "procedures",
        "INFORMATION_SCHEMA.PROCEDURES",
        f"""
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
        WHERE procedure_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("procedure_schema")}
        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
        q,
        facet=FACET_ROUTINE_BODIES,
    )

    # R11-FP01: sequences. `NEXT_VALUE` is offered by this view and is
    # deliberately not selected -- it is the value the next insert writes into a
    # customer's row, which is source data rather than metadata (INV-6). Only
    # the declaration is read. Snowflake reports no owning table or column
    # (there is no `serial`-style dependency to report), so those stay absent.
    q = ScopeSql(scope, "snowflake")
    sequences = _collect(
        "sequences",
        "INFORMATION_SCHEMA.SEQUENCES",
        f"""
        SELECT
            sequence_schema,
            sequence_name,
            data_type,
            start_value AS start_with,
            increment AS increment_by,
            comment
        FROM information_schema.sequences
        WHERE sequence_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("sequence_schema")}
        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
        q,
        facet=FACET_SEQUENCES,
    )

    definitions = {
        (str(row.get("table_schema")), str(row.get("table_name")))
        for row in views
        if row.get("view_definition") is not None
    }
    view_ddl: list[dict[str, Any]] = []
    for schema_name, view_name, kind in view_keys:
        if (schema_name, view_name) in definitions:
            continue
        # R11-FP01: GET_DDL is one statement per object, so there is no WHERE for the
        # pushed predicate to live in. The same predicate is evaluated here instead
        # (`ObjectScope.admits_name` has `LIKE` semantics), so an excluded view costs
        # no round trip at all -- and, like every other push, it only ever skips an
        # object the selection would have dropped anyway.
        if not (
            scope.objects.admits_name(schema_name, view_name)
            and scope.objects.admits_kind(kind)
        ):
            continue
        qualified = (
            f"{database}.{schema_name}.{view_name}" if database else f"{schema_name}.{view_name}"
        )
        rows, failure = _fetch_optional_rows(
            cursor,
            f"SELECT GET_DDL('VIEW', {_quote_literal(qualified)}, TRUE) AS view_definition",  # noqa: S608 -- the identifier is quoted as a string literal, not interpolated as SQL
        )
        # R11-FP02: GET_DDL is the second half of the same view-definition
        # facet, so its failure is replayed against that facet -- recorded once,
        # however many views it is refused for (`FacetReadScope.record` keeps the
        # first outcome, and the first is the one that describes this login's access).
        if failure is not None:
            failures.append((FACET_VIEW_DEFINITIONS, failure))
        definition = rows[0].get("view_definition") if rows else None
        view_ddl.append(
            {
                "table_schema": schema_name,
                "table_name": view_name,
                "view_definition": definition,
                "unavailable_reason": (
                    None
                    if definition is not None
                    else _refused_reason(f"GET_DDL on {qualified}")
                    if failure is not None
                    else (
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
        rows, failure = _fetch_optional_rows(cursor, f"SHOW GRANTS ON SCHEMA {qualified}")
        if failure is not None:
            failures.append((FACET_GRANTS, failure))
            unavailable.append(
                (f"grants:{schema_name}", _refused_reason(f"SHOW GRANTS ON SCHEMA {qualified}"))
            )
            continue
        for row in rows:
            grants.append({**row, "schema_name": schema_name})

    return _SnowflakeEnvelopeRows(
        views=views,
        view_ddl=tuple(view_ddl),
        routines=functions + procedures,
        sequences=sequences,
        schemata=schemata,
        databases=databases,
        grants=tuple(grants),
        unavailable=tuple(unavailable),
        failures=tuple(failures),
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

    # R11-FP01: sequences attached after assembly, for the reason recorded on
    # `connectors.base.attach_native_objects`. No triggers: Snowflake has none.
    catalogs = attach_native_objects(
        assemble_catalog(
            catalog_name,
            table_map,
            routines=routines,
            grants=grants,
            schema_descriptions=schema_descriptions,
            catalog_description=next(
                (_optional_text(row.get("comment")) for row in envelope.databases), None
            ),
        ),
        sequences=build_sequences(
            [
                {
                    "sequence_schema": row["sequence_schema"],
                    "sequence_name": row["sequence_name"],
                    "data_type": row.get("data_type"),
                    "start_with": row.get("start_with"),
                    "increment_by": row.get("increment_by"),
                    "description": _optional_text(row.get("comment")),
                }
                for row in envelope.sequences
            ]
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
        # R11-FP01: sequences yes, triggers no, and the two answers have
        # different reasons.
        #
        # `sequences=True` is backed by INFORMATION_SCHEMA.SEQUENCES, read in
        # `_fetch_envelope_rows` like every other axis above.
        #
        # `triggers` stays False and is *not* a gap in this adapter: Snowflake
        # has no trigger object at all. There is no `CREATE TRIGGER`; a stream
        # plus a task is how the same intent is expressed, and neither is a
        # trigger (a task is scheduled, not fired by a DML statement, and a
        # stream is a change-tracking cursor over a table). So this axis reads
        # NOT_APPLICABLE for Snowflake rather than UNSUPPORTED -- which is a
        # fact about the engine and lives in
        # `discovery_selection._NO_TRIGGER_KIND`, not in a flag here. Leaving
        # the flag False is what makes that answer reachable: INV-9's default.
        sequences=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 60.0) -> None:
        self._dsn = dsn
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout
        self._scope = DiscoveryScope()

    @property
    def capabilities(self) -> ConnectorCapabilities:
        # INV-9: `DEFAULT_CAPABILITIES` is this connector's claim. What it advertises is
        # that claim narrowed to what its certification result supports
        # (`aida.connectors.capability_certification`); it can never exceed the claim.
        return derive_capabilities(self.connector_type, self.DEFAULT_CAPABILITIES)

    def scope_discovery(
        self,
        *,
        include_schemas: list[str],
        exclude_schemas: list[str],
        object_kinds: Sequence[str] = (),
        include_objects: Sequence[str] = (),
        exclude_objects: Sequence[str] = (),
    ) -> bool:
        """R11-FP01: push the selection into this adapter's INFORMATION_SCHEMA reads.

        The schema scope reaches every schema-bound read; object kinds and `schema.object`
        patterns reach the reads whose rows belong to one object (the constraint reads,
        INFORMATION_SCHEMA.VIEWS, the GET_DDL pass). Everything pushed is a superset of the
        selection, and `discovery_selection.apply_selection` still runs on the result.
        """
        self._scope = discovery_scope(
            include_schemas=include_schemas,
            exclude_schemas=exclude_schemas,
            object_kinds=object_kinds,
            include_objects=include_objects,
            exclude_objects=exclude_objects,
        )
        return self._scope.narrows(kinds=True)

    def _get_connection(self, *, paramstyle: str | None = None) -> Any:
        """Create a Snowflake DBAPI connection using snowflake-connector-python.

        R11-FP01: discovery asks for `paramstyle="qmark"`, under which the connector
        binds parameters server-side. The default `pyformat` style would have the
        client render each value into the statement text itself -- escaped, but
        interpolated -- which is the one thing the pushed-down selection's patterns
        must never be. The other paths keep the default they were written for
        (`get_query_history` uses `%(name)s`).
        """
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
        if paramstyle is not None:
            kwargs["paramstyle"] = paramstyle
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

    @staticmethod
    def _capture(cur: Any, sql: str, params: Sequence[Any] = ()) -> _CapturedRead:
        """One v1.0 read, captured rather than judged (R11-FP02).

        Both halves are inside the guard, not just the `execute`: the driver
        decides which of the two raises, and a refusal that surfaced at fetch
        time would otherwise escape the capture.
        """
        try:
            if params:
                cur.execute(sql, list(params))
            else:
                cur.execute(sql)
            return rows_to_dicts(cur, cur.fetchall())
        except Exception as exc:  # noqa: BLE001 -- replayed verbatim into `read_facet`
            return exc

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        """Discover database catalogs, schemas, tables, columns, and constraints.

        R11-FP02: the reads happen in one driver thread and are replayed here
        through `read_facet` in the order they were made -- every one of them bare,
        so a refusal costs its facet and anything else ends the run (see the
        `_CapturedReads` comment). The assembly is a pure function of the rows, so
        it moved out of the thread with no change to what it produces.
        """
        reads = await asyncio.to_thread(self._read_facets_sync)
        column_rows = list(
            await read_facet(
                FACET_INVENTORY, _captured(reads.columns), known_relations=_KNOWN_RELATIONS
            )
        )
        pk_rows = list(
            await read_facet(
                FACET_CONSTRAINTS, _captured(reads.primary_keys), known_relations=_KNOWN_RELATIONS
            )
        )
        fk_rows = list(
            await read_facet(
                FACET_CONSTRAINTS, _captured(reads.foreign_keys), known_relations=_KNOWN_RELATIONS
            )
        )
        envelope = reads.envelope
        if envelope is not None:
            await _replay_axis_failures(envelope.failures)

        # Assemble into Atlas Catalog Graph
        return _assemble_snowflake_catalog(
            reads.catalog_name, column_rows, pk_rows, fk_rows, envelope=envelope
        )

    def _read_facets_sync(self) -> _CapturedReads:
        # `qmark` only when there is something to bind: an unscoped discovery sends no
        # parameters at all, so it keeps the connection exactly as it always opened it.
        conn = (
            self._get_connection(paramstyle="qmark")
            if self._scope.restricted
            else self._get_connection()
        )
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

                # Discover Columns and Tables. R11-FP01: the roster takes the schema scope
                # only -- it is what tells a FULL run which in-scope schemas still exist.
                q = ScopeSql(self._scope, "snowflake")
                columns = self._capture(
                    cur,
                    f"""
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
                        WHERE c.table_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("c.table_schema")}
                        ORDER BY c.table_schema, c.table_name, c.ordinal_position
                        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
                    q.positional,
                )
                if isinstance(columns, BaseException):
                    # The roster read failed: there is nothing to scope the
                    # envelope queries to, and nothing to assemble. `discover()`
                    # replays this first and it ends the run, as it always has.
                    return _CapturedReads(catalog_name=catalog_name, columns=columns)
                column_rows = list(columns)

                # Discover Primary Keys & Unique Constraints. A constraint belongs to its
                # table, so these two reads also take the `schema.object` patterns.
                q = ScopeSql(self._scope, "snowflake")
                primary_keys = self._capture(
                    cur,
                    f"""
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
                          AND tc.table_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("tc.table_schema")}
                          {q.names("tc.table_schema", "tc.table_name")}
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
                    q.positional,
                )

                # Discover Foreign Keys
                q = ScopeSql(self._scope, "snowflake")
                foreign_keys = self._capture(
                    cur,
                    f"""
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
                          AND tc.table_schema {_SYSTEM_SCHEMA_FILTER}{q.schema("tc.table_schema")}
                          {q.names("tc.table_schema", "tc.table_name")}
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """,  # noqa: S608 -- static SQL; every pushed value is a qmark bind
                    q.positional,
                )

                # Envelope 1.1 (gap/02 N1): view text, routines with bodies,
                # object comments and source grants.
                schema_names = sorted({str(row["table_schema"]) for row in column_rows})
                view_keys = sorted(
                    {
                        (str(row["table_schema"]), str(row["table_name"]), kind)
                        for row in column_rows
                        if (kind := normalize_object_type(str(row.get("table_type") or "")))
                        in _VIEW_OBJECT_TYPES
                    }
                )
                envelope = _fetch_envelope_rows(
                    cur,
                    database=self._params.database or catalog_name,
                    schema_names=schema_names,
                    view_keys=view_keys,
                    scope=self._scope,
                )
            finally:
                cur.close()
        finally:
            conn.close()

        return _CapturedReads(
            catalog_name=catalog_name,
            columns=column_rows,
            primary_keys=primary_keys,
            foreign_keys=foreign_keys,
            envelope=envelope,
        )

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
                                    # R11-FP04: these NULLs now carry a reason.
                                    # This connector computes three aggregates
                                    # per column and no text-shaped ones at
                                    # all, so a reader seeing `min_length is
                                    # None` could not tell "Snowflake was never
                                    # asked" from "this column has no text
                                    # form" -- two facts with opposite
                                    # implications for whether asking again
                                    # would help. Snowflake can express every
                                    # one of these (`LENGTH(TO_VARCHAR(...))`,
                                    # a grouped entropy aggregate); this
                                    # adapter does not, which is
                                    # NOT_IMPLEMENTED and not UNSUPPORTED.
                                    facet_status=_UNIMPLEMENTED_FACETS,
                                )
                            )

                    # R11-FP04: every aggregate above runs over the *whole*
                    # table -- there is no `LIMIT` anywhere in this method -- so
                    # this is a full observation and must say so. It previously
                    # reported `sampled_row_count = min(row_count,
                    # sample_rows)`, which made a complete profile arrive
                    # downstream as sampled and weakened join evidence that was
                    # in fact exhaustive. `sample_rows` is deliberately unused
                    # here: bounding Snowflake's profiling cost is a real open
                    # question, and the answer to it is a bound in the SQL, not
                    # a smaller number reported for a scan that already
                    # happened.
                    return TableProfileSnapshot(
                        row_count_estimate=row_count,
                        sampled_row_count=row_count,
                        columns=tuple(column_snapshots),
                        observation_scope=OBSERVATION_SCOPE_FULL,
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
                    rows = rows_to_dicts(cur, cur.fetchall())
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
