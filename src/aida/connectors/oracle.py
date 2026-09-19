import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final
from urllib.parse import unquote, urlsplit

import oracledb

from aida.connectors.base import (
    ENTROPY_NOT_IMPLEMENTED,
    FACET_REASON_TYPE_HAS_NO_TEXT_FORM,
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredViewDefinition,
    ProfileFacetStatus,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
    attach_native_objects,
    bounded_scan_scope,
    build_sequences,
    build_triggers,
    null_distribution_expressions,
    read_value_free_distribution,
    text_facets_not_applicable,
    value_free_distribution_expressions,
)
from aida.connectors.discovery import (
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INDEXES,
    FACET_INVENTORY,
    FACET_OBJECT_COMMENTS,
    FACET_PARTITIONS,
    FACET_ROUTINE_BODIES,
    FACET_SEQUENCES,
    FACET_TRIGGERS,
    FACET_VIEW_DEFINITIONS,
    TableMap,
    append_grouped_foreign_key_rows,
    append_grouped_index_rows,
    append_grouped_key_rows,
    append_partition_rows,
    apply_column_descriptions,
    apply_table_descriptions,
    apply_view_definitions,
    assemble_catalog,
    build_grants,
    build_table_map_from_column_rows,
    normalize_object_type,
    read_facet,
    read_optional_facet,
    view_definition_row,
)
from aida.connectors.schema_scope import DiscoveryScope, ScopeSql, discovery_scope
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
        # R11-FP04: same honest-placeholder rule as the three above --
        # `TO_CHAR` over a LOB is an error, not a value, so the aliases exist
        # and answer NULL and `_profile_facet_status` says why.
        distribution = null_distribution_expressions(
            position=position, null_literal="CAST(NULL AS NUMBER)"
        )
    else:
        char_form = f"TO_CHAR({quoted_column})"
        text_form = f"LENGTH({char_form})"
        distinct_expression = f"COUNT(DISTINCT {quoted_column}) AS d_{position}"
        min_length_expression = f"MIN({text_form}) AS minl_{position}"
        max_length_expression = f"MAX({text_form}) AS maxl_{position}"
        # Oracle stores '' as NULL, so `blank_count` is structurally always 0
        # here rather than sometimes nonzero -- which is itself the right
        # answer, and a different one from NULL.
        distribution = value_free_distribution_expressions(
            position=position,
            text_form=char_form,
            length_form=text_form,
            trimmed_form=f"TRIM({char_form})",
        )
    return [
        f"SUM(CASE WHEN {quoted_column} IS NULL THEN 1 ELSE 0 END) AS n_{position}",
        f"COUNT({quoted_column}) AS nn_{position}",
        distinct_expression,
        min_length_expression,
        max_length_expression,
        *distribution,
    ]


def _upper_cased_reader(row: dict[str, Any]) -> Callable[[str], Any]:
    """Read a generated lower-case alias out of an Oracle row.

    Oracle folds unquoted column aliases to upper case, so the shared
    `read_value_free_distribution` accessor -- which asks for the alias exactly
    as `value_free_distribution_expressions` generated it -- needs the fold
    applied on the way in.
    """
    return lambda alias: row.get(alias.upper())


def _profile_facet_status(data_type: str) -> tuple[ProfileFacetStatus, ...]:
    """Which facets `_profile_expressions` declined for this column, and why.

    R11-FP04. A LOB's NULL length has a different meaning from an unimplemented
    one: no credential and no retry makes `TO_CHAR` work on a CLOB, whereas
    entropy is simply not asked for here yet.
    """
    if data_type.upper() in _LOB_LIKE_TYPES:
        return (
            *text_facets_not_applicable(FACET_REASON_TYPE_HAS_NO_TEXT_FORM),
            ENTROPY_NOT_IMPLEMENTED,
        )
    return (ENTROPY_NOT_IMPLEMENTED,)


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
    #: R11-FP03: ALL_PROCEDURES rows for subprograms declared inside a package.
    package_members: tuple[dict[str, Any], ...] = ()
    #: R11-FP01: ALL_TRIGGERS and ALL_SEQUENCES rows.
    triggers: tuple[dict[str, Any], ...] = ()
    sequences: tuple[dict[str, Any], ...] = ()
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
                "not against the package object, so this routine carries none; each member "
                "subprogram is reported as its own routine with them"
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
    _append_package_members(envelope, routines)
    return routines


_MemberKey = tuple[str, str, str, int]


def _envelope_member_parameters(
    envelope: _OracleEnvelopeRows,
) -> tuple[dict[_MemberKey, list[DiscoveredRoutineParameter]], dict[_MemberKey, str]]:
    """ALL_ARGUMENTS rows of packaged subprograms, keyed (owner, package, member, subprogram id)
    -- the key two overloads of one member do not share. Oracle writes a single argument-less row
    for a subprogram with no parameters; that placeholder is not a parameter."""
    parameters: dict[_MemberKey, list[DiscoveredRoutineParameter]] = {}
    return_types: dict[_MemberKey, str] = {}
    for row in envelope.arguments:
        package = row.get("PACKAGE_NAME")
        if package is None:
            continue
        key = (
            str(row["OWNER"]),
            str(package),
            str(row["OBJECT_NAME"]),
            _optional_int(row.get("SUBPROGRAM_ID")) or 0,
        )
        position = _optional_int(row.get("POSITION")) or 0
        data_type = _optional_text(row.get("DATA_TYPE"))
        if position == 0:
            if data_type is not None:
                return_types[key] = data_type
            continue
        if data_type is None and _optional_text(row.get("ARGUMENT_NAME")) is None:
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


def _append_package_members(
    envelope: _OracleEnvelopeRows, routines: dict[str, list[DiscoveredRoutine]]
) -> None:
    """R11-FP03: each subprogram a package declares, as its own routine under its package.

    A member has no source of its own -- ALL_SOURCE holds the package spec and body -- so its
    body is absent with that reason, never an invented slice of the package text. Its identity
    carries the package (`attributes["package_name"]`), so it cannot collide with a standalone
    routine of the same name, and its overload number when Oracle gives one.
    """
    parameters, return_types = _envelope_member_parameters(envelope)
    for row in envelope.package_members:
        owner = str(row["OWNER"])
        package = str(row["OBJECT_NAME"])
        member = str(row["PROCEDURE_NAME"])
        key = (owner, package, member, _optional_int(row.get("SUBPROGRAM_ID")) or 0)
        return_type = return_types.get(key)
        attributes: dict[str, Any] = {"package_name": package}
        overload = _optional_text(row.get("OVERLOAD"))
        if overload is not None:
            attributes["overload"] = overload
        routines.setdefault(owner, []).append(
            DiscoveredRoutine(
                name=member,
                routine_type="FUNCTION" if return_type is not None else "PROCEDURE",
                language=None,
                body_sql=None,
                parameters=tuple(parameters.get(key, ())),
                return_type=return_type,
                unavailable_reason=(
                    f"a member subprogram of package {owner}.{package}; its source is the "
                    "package's own"
                ),
                attributes=attributes,
            )
        )


def _table_description_rows(envelope: _OracleEnvelopeRows) -> list[dict[str, Any]]:
    """Shape ALL_TAB_COMMENTS rows for the shared `apply_table_descriptions`.

    `_optional_text` runs here, not in the shared helper: `apply_table_descriptions`
    passes `description` through `str()` verbatim, with no blank-collapsing of its
    own, so a whitespace-only Oracle comment has to be normalized to `None` before
    it gets there or it would land on the table as literal whitespace.
    """
    return [
        {
            "table_schema": row["OWNER"],
            "table_name": row["TABLE_NAME"],
            "description": _optional_text(row.get("COMMENTS")),
        }
        for row in envelope.table_comments
    ]


def _column_description_rows(envelope: _OracleEnvelopeRows) -> list[dict[str, Any]]:
    """Shape ALL_COL_COMMENTS rows for the shared `apply_column_descriptions`.

    See `_table_description_rows` for why `_optional_text` runs here rather than
    being left to the shared helper.
    """
    return [
        {
            "table_schema": row["OWNER"],
            "table_name": row["TABLE_NAME"],
            "column_name": row["COLUMN_NAME"],
            "description": _optional_text(row.get("COMMENTS")),
        }
        for row in envelope.column_comments
    ]


def _view_definition_rows(
    tables: TableMap, envelope: _OracleEnvelopeRows
) -> list[dict[str, Any]]:
    """Build one `apply_view_definitions` row per view/materialized-view table.

    Preserves Oracle's original precedence: a materialized view's ALL_MVIEWS row
    wins regardless of the discovered `object_type`; a plain VIEW-typed table falls
    back to its ALL_VIEWS row, and -- absent both -- to a synthetic row recording
    that ALL_VIEWS exposed nothing for it. A table that is neither gets no row,
    same as before.
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
    views_reason = envelope.reason("views")

    rows: list[dict[str, Any]] = []
    for schema_name, schema_tables in tables.items():
        for table_name, table in schema_tables.items():
            key = (schema_name, table_name)
            definition = materialized_definitions.get(key)
            if definition is None and table.object_type == "VIEW":
                definition = view_definitions.get(key) or DiscoveredViewDefinition(
                    definition_sql=None,
                    unavailable_reason=views_reason
                    or (
                        f"ALL_VIEWS exposed no row for {schema_name}.{table_name}; "
                        "its text was not visible to this session"
                    ),
                )
            if definition is not None:
                rows.append(view_definition_row(schema_name, table_name, definition))
    return rows


#: R11-FP01: `ALL_TRIGGERS.TRIGGER_TYPE` as a timing, matched longest-first.
#:
#: Oracle packs the timing and the orientation into one phrase ('BEFORE EACH
#: ROW', 'AFTER STATEMENT', 'INSTEAD OF', 'COMPOUND'), and the two are separate
#: columns on the envelope because every other engine reports them separately.
#: Matched against this closed table rather than split on whitespace, so an
#: unrecognised phrase contributes no timing instead of putting Oracle prose
#: into platform state -- the same rule `reason_code` applies to reasons.
_TRIGGER_TIMINGS: tuple[str, ...] = ("INSTEAD OF", "COMPOUND", "BEFORE", "AFTER")
_TRIGGER_ORIENTATIONS: tuple[str, ...] = ("EACH ROW", "STATEMENT")


def _trigger_rows(envelope: _OracleEnvelopeRows) -> list[dict[str, Any]]:
    """Shape ALL_TRIGGERS rows for the shared `build_triggers`.

    `TABLE_OWNER` becomes `table_schema` only when it differs from the
    trigger's own owner. Oracle is the one engine where the two can differ, and
    carrying it unconditionally would put a redundant schema name on every
    trigger in the estate while saying nothing -- `None` already means "this
    trigger's own schema".
    """
    rows: list[dict[str, Any]] = []
    for row in envelope.triggers:
        owner = str(row["OWNER"])
        table_owner = _optional_text(row.get("TABLE_OWNER"))
        phrase = (_optional_text(row.get("TRIGGER_TYPE")) or "").upper()
        rows.append(
            {
                "trigger_schema": owner,
                "trigger_name": row["TRIGGER_NAME"],
                "table_name": row["TABLE_NAME"],
                "table_schema": None if table_owner in (None, owner) else table_owner,
                "timing": next(
                    (timing for timing in _TRIGGER_TIMINGS if timing in phrase), ""
                ),
                "orientation": next(
                    (
                        "ROW" if orientation == "EACH ROW" else orientation
                        for orientation in _TRIGGER_ORIENTATIONS
                        if orientation in phrase
                    ),
                    None,
                ),
                "events": _optional_text(row.get("TRIGGERING_EVENT")),
                # ALL_TRIGGERS.STATUS is 'ENABLED' / 'DISABLED'.
                "is_enabled": (_optional_text(row.get("STATUS")) or "").upper() == "ENABLED",
                "body": _optional_text(row.get("TRIGGER_BODY")),
                "unavailable_reason": envelope.reason("triggers"),
            }
        )
    return rows


def _sequence_rows(envelope: _OracleEnvelopeRows) -> list[dict[str, Any]]:
    """Shape ALL_SEQUENCES rows for the shared `build_sequences`.

    Oracle has no `START WITH` column on `ALL_SEQUENCES` at all -- the start is
    consumed by the first `NEXTVAL` and only `LAST_NUMBER` remains, which is the
    one thing this axis must never read (INV-6). `start_with` is therefore
    honestly absent rather than back-computed from the current position, which
    would both be a guess and carry the value it was guessed from.
    `data_type` is absent for the same kind of reason: every Oracle sequence is
    NUMBER, and the dictionary view carries no type column to read it from.

    There is no owning table or column either: an Oracle `IDENTITY` column's
    sequence is a system-named object with no dictionary link back to its
    column, so reporting one would mean matching on a naming convention.
    """
    return [
        {
            "sequence_schema": row["SEQUENCE_OWNER"],
            "sequence_name": row["SEQUENCE_NAME"],
            "increment_by": _optional_text(row.get("INCREMENT_BY")),
            "minimum_bound": _optional_text(row.get("MIN_VALUE")),
            "maximum_bound": _optional_text(row.get("MAX_VALUE")),
            "cache_size": _optional_text(row.get("CACHE_SIZE")),
            # CYCLE_FLAG is 'Y' / 'N', which `build_sequences` reads.
            "cycles": _optional_text(row.get("CYCLE_FLAG")),
        }
        for row in envelope.sequences
    ]


def _grant_rows(envelope: _OracleEnvelopeRows) -> list[dict[str, Any]]:
    """Shape ALL_TAB_PRIVS rows for the shared `build_grants`.

    Defaults are computed here rather than left to `build_grants` so its generic
    fallbacks (`"ROLE"`, `"TABLE"`) never silently replace Oracle's own
    (`"UNKNOWN"`, `"TABLE"`) -- the values were already equal for `object_type`,
    kept explicit for `grantee_type` since they are not.
    """
    return [
        {
            "schema_name": row["TABLE_SCHEMA"],
            "grantee": row["GRANTEE"],
            "grantee_type": row.get("GRANTEE_TYPE") or "UNKNOWN",
            "privilege": row["PRIVILEGE"],
            "object_type": row.get("OBJECT_TYPE") or "TABLE",
            "object_name": row["TABLE_NAME"],
            "is_grantable": row.get("GRANTABLE") or "NO",
        }
        for row in envelope.grants
    ]


async def _fetch_rows(
    cursor: Any, sql: str, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """One metadata query as a single awaitable, so `read_facet` can carry it.

    R11-FP02: `oracledb`'s async cursor separates `execute` from `fetchall`, and
    either half can be the one the source refuses -- a privilege check can fail
    at parse time or at fetch time depending on the dictionary view. One
    coroutine covering both is what makes "this facet's read" a single thing to
    wrap.

    R11-FP01: `params` are the pushed-down selection's patterns and kinds, bound
    by name (`:scope_0`) -- python-oracledb sends them to the server as bind
    values, so nothing an operator typed is ever part of the statement text.
    """
    if params:
        await cursor.execute(sql, params)
    else:
        await cursor.execute(sql)
    return _rows_as_dicts(cursor.description, await cursor.fetchall())


#: R11-FP02: every relation an Oracle discovery read names is an Oracle-supplied
#: `ALL_*` data-dictionary view, present on every release this adapter reads. So
#: ORA-00942 "table or view does not exist" on one of them cannot mean the view
#: is missing -- only that it was hidden from this login, which is how Oracle
#: refuses a view a login holds no privilege on. Passed to `read_facet` as
#: `known_relations=True`; `capability_states.HIDDEN_RELATION_ORACLE_ERRORS`
#: has the whole argument, including why the classifier does not assume it.
_DICTIONARY_READ: Final = True

#: R11-FP02: the dictionary view behind each envelope axis, for the value-free
#: reason an absorbed refusal leaves on the catalog and on the objects it cost.
_AXIS_RELATIONS: dict[str, str] = {
    "views": "ALL_VIEWS",
    "materialized_views": "ALL_MVIEWS",
    "routines": "ALL_OBJECTS / ALL_PROCEDURES",
    "routine_source": "ALL_SOURCE",
    "arguments": "ALL_ARGUMENTS",
    "package_members": "ALL_PROCEDURES",
    "triggers": "ALL_TRIGGERS",
    "sequences": "ALL_SEQUENCES",
    "table_comments": "ALL_TAB_COMMENTS",
    "column_comments": "ALL_COL_COMMENTS",
    "grants": "ALL_TAB_PRIVS",
}


def _refused_reason(axis: str) -> str:
    """Why an axis is empty when the source refused it -- fixed text, never the driver's.

    Before R11-FP02's follow-through this was `f"{type(exc).__name__}: {exc}"`, and it
    reached `metadata_routine.unavailable_reason` and the view definition's own reason
    verbatim: a driver message, which can quote the statement or a value (INV-6). It
    is now only ever rendered for a refusal (anything else re-raises), so a sentence
    naming the dictionary view is the whole of what is known.
    """
    return (
        f"{_AXIS_RELATIONS.get(axis, axis)} was refused for this login; the discovery "
        "receipt records the facet as PERMISSION_DENIED"
    )


#: Envelope axis -> the discovery facet its read answers, for `_fetch_envelope_rows`.
#:
#: R11-FP02. Several axes share a facet because a facet is a *kind of read*, not a
#: query: `ALL_VIEWS` and `ALL_MVIEWS` are both the view-definition facet, and the
#: four routine reads are all the routine-bodies facet, so a login refused either
#: half of one gets that facet recorded once (`FacetReadScope.record` keeps the
#: first outcome). Every axis has one, `triggers` and `sequences` included since
#: `DISCOVERY_FACETS` named them, so no envelope read escapes the classification.
_AXIS_FACETS: dict[str, str] = {
    "views": FACET_VIEW_DEFINITIONS,
    "materialized_views": FACET_VIEW_DEFINITIONS,
    "routines": FACET_ROUTINE_BODIES,
    "routine_source": FACET_ROUTINE_BODIES,
    "arguments": FACET_ROUTINE_BODIES,
    "package_members": FACET_ROUTINE_BODIES,
    "table_comments": FACET_OBJECT_COMMENTS,
    "column_comments": FACET_OBJECT_COMMENTS,
    "grants": FACET_GRANTS,
    # R11-FP01: ALL_TRIGGERS and ALL_SEQUENCES are each one refusable read.
    "triggers": FACET_TRIGGERS,
    "sequences": FACET_SEQUENCES,
}


async def _fetch_optional_rows(
    cursor: Any,
    sql: str,
    *,
    axis: str,
    facet: str,
    params: Mapping[str, Any] | None = None,
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Run one supplementary metadata query; a refusal costs the axis, nothing else does.

    Envelope 1.1 reads dictionary views a least-privilege reader may not hold
    (`ALL_SOURCE`, `ALL_TAB_PRIVS`, `ALL_MVIEWS`). A denial must not read as "the
    source has none of these", so an absorbed refusal comes back as a reason that
    lands on the objects and on the catalog, as well as on the receipt.

    **R11-FP02 follow-through: only a refusal is absorbed.** This function used to
    catch *every* failure and turn it into a reason, so a dropped connection on
    `ALL_TAB_PRIVS` produced an empty grants axis -- and because that failure was
    UNAVAILABLE rather than PERMISSION_DENIED, `workflows.activities.
    refused_facet_existing` did not protect the grants an earlier run captured, and
    the FULL reconciliation retired every one of them against a source that had
    merely stopped answering. It is now exactly `read_facet`'s rule, the one the
    PostgreSQL and SQL Server adapters always had: a refusal is recorded against
    its facet, absorbed and explained; anything else is recorded and re-raised, so
    the run ends INTERRUPTED instead of reconciling. Outside a `facet_read_scope`
    nothing is absorbed at all.

    **Oracle's codes.** `oracledb` reports no SQLSTATE; its `_Error.code` is the ORA
    number, which `capability_states.is_permission_refusal` now reads: ORA-01031
    is a refusal anywhere, and ORA-00942 is one here because every read names a
    dictionary view that exists (`_DICTIONARY_READ`).
    """
    rows, refused = await read_optional_facet(
        facet, _fetch_rows(cursor, sql, params), known_relations=_DICTIONARY_READ
    )
    return tuple(rows), (_refused_reason(axis) if refused else None)


def _owner_scope(query: ScopeSql, owner_column: str) -> str:
    """The system-schema exclusion plus the pushed-down schema scope, on one owner column."""
    return f"{_schema_exclusion_clause(owner_column)}{query.schema(owner_column)}"


#: R11-FP01: selection kind -> `ALL_SOURCE.TYPE`. A package's source is its spec and its
#: body, both of which belong to the one PACKAGE routine the envelope reports.
_SOURCE_TYPE_KINDS: dict[str, tuple[str, ...]] = {
    "PROCEDURE": ("PROCEDURE",),
    "FUNCTION": ("FUNCTION",),
    "PACKAGE": ("PACKAGE", "PACKAGE BODY"),
}


async def _fetch_envelope_rows(
    cursor: Any, scope: DiscoveryScope | None = None
) -> _OracleEnvelopeRows:
    """Read every envelope 1.1 axis Oracle exposes, recording each refusal.

    R11-FP01: `scope` is the pushed-down selection. Every read takes its schema scope;
    the reads whose rows belong to one object -- a view's or materialized view's text, a
    routine's source, a table's comments -- also take its `schema.object` patterns and,
    where the read holds one kind, its kinds. The inventories that establish a schema
    (routines, package members, triggers, sequences, grants) and the argument lists take
    the schema scope only; `aida.connectors.schema_scope`'s module docstring says why.
    """
    scope = scope or DiscoveryScope()
    unavailable: list[tuple[str, str]] = []

    async def _collect(axis: str, sql: str, query: ScopeSql) -> tuple[dict[str, Any], ...]:
        rows, reason = await _fetch_optional_rows(
            cursor, sql, axis=axis, facet=_AXIS_FACETS[axis], params=query.named or None
        )
        if reason is not None:
            unavailable.append((axis, reason))
        return rows

    q = ScopeSql(scope, "oracle")
    views = await _collect(
        "views",
        "SELECT owner, view_name, text_length, text FROM ALL_VIEWS "  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        f"WHERE {_owner_scope(q, 'owner')}{q.names('owner', 'view_name')}{q.kind_gate('VIEW')}",
        q,
    )
    # An Oracle materialized view reaches the roster through its container table (the
    # ALL_OBJECTS row of type TABLE), so its definition belongs to a TABLE-kind object.
    q = ScopeSql(scope, "oracle")
    materialized_views = await _collect(
        "materialized_views",
        "SELECT owner, mview_name, query_len, query FROM ALL_MVIEWS "  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        f"WHERE {_owner_scope(q, 'owner')}{q.names('owner', 'mview_name')}"
        f"{q.kind_gate('TABLE')}",
        q,
    )
    q = ScopeSql(scope, "oracle")
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
          AND {_owner_scope(q, "ao.owner")}
        ORDER BY ao.owner, ao.object_name
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    q = ScopeSql(scope, "oracle")
    routine_source = await _collect(
        "routine_source",
        f"""
        SELECT owner, name, type, line, text
        FROM ALL_SOURCE
        WHERE type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')
          AND {_owner_scope(q, "owner")}{q.names("owner", "name")}
          {q.kinds("type", _SOURCE_TYPE_KINDS)}
        ORDER BY owner, name, type, line
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    # Schema scope only: a packaged member's arguments are keyed by its package, and a
    # member follows its package into the scan (`discovery_selection.routine_in_scope`),
    # so a name filter here would have to reproduce that rule inside SQL.
    q = ScopeSql(scope, "oracle")
    arguments = await _collect(
        "arguments",
        f"""
        SELECT owner, object_name, package_name, subprogram_id, argument_name, position,
               data_type, in_out
        FROM ALL_ARGUMENTS
        WHERE data_level = 0 AND {_owner_scope(q, "owner")}
        ORDER BY owner, object_name, subprogram_id, position
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    # R11-FP03: the subprograms a package declares. SUBPROGRAM_ID keys their arguments, and
    # OVERLOAD tells two same-named members apart.
    q = ScopeSql(scope, "oracle")
    package_members = await _collect(
        "package_members",
        f"""
        SELECT owner, object_name, procedure_name, subprogram_id, overload
        FROM ALL_PROCEDURES
        WHERE procedure_name IS NOT NULL
          AND object_type = 'PACKAGE'
          AND {_owner_scope(q, "owner")}
        ORDER BY owner, object_name, subprogram_id
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    # R11-FP01: triggers.
    #
    # `TRIGGER_TYPE` is a phrase ('BEFORE EACH ROW', 'AFTER STATEMENT',
    # 'INSTEAD OF', 'COMPOUND') and `TRIGGERING_EVENT` is another ('INSERT OR
    # UPDATE'), so both are parsed against closed vocabularies rather than
    # stored as prose -- see `_envelope_triggers` and
    # `connectors.base._trigger_events`.
    #
    # `TABLE_OWNER` is selected because Oracle is the one engine where a
    # trigger's owner may differ from its table's, which is exactly the case
    # `DiscoveredTrigger.table_schema` exists for.
    #
    # `TRIGGER_BODY` is a LONG. `oracledb` returns it as a string for a bounded
    # value and Oracle imposes no length bound on it, so the same rule the view
    # and routine axes carry applies: a NULL arrives as unavailable with a
    # reason rather than as a trigger with no body, and this connector does not
    # claim `truncated` because it has no way to know -- the axis under-claims
    # rather than guessing (INV-9).
    #
    # `BASE_OBJECT_TYPE = 'TABLE' OR 'VIEW'` keeps DDL, DATABASE and SCHEMA
    # triggers out: those have no parent object, so they are not a data path
    # between two objects, which is the reason this axis exists. Recorded as a
    # scope decision rather than left as an unexplained absence.
    q = ScopeSql(scope, "oracle")
    triggers = await _collect(
        "triggers",
        f"""
        SELECT
            owner,
            trigger_name,
            table_owner,
            table_name,
            trigger_type,
            triggering_event,
            status,
            base_object_type,
            trigger_body
        FROM ALL_TRIGGERS
        WHERE base_object_type IN ('TABLE', 'VIEW')
          AND {_owner_scope(q, "owner")}
        ORDER BY owner, table_name, trigger_name
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    # R11-FP01: sequences.
    #
    # `LAST_NUMBER` is deliberately not selected. It is the next value Oracle
    # will hand an insert -- source data, not metadata (INV-6) -- and it moves
    # on every `NEXTVAL`, so a stored copy would be both a live business value
    # and wrong. Only the declaration is read.
    #
    # `MIN_VALUE` / `MAX_VALUE` are Oracle's own column names and land on the
    # envelope as `minimum_bound` / `maximum_bound`: they are limits in a
    # `CREATE SEQUENCE` statement, and a field spelled like a row value invites
    # the confusion INV-6's naming ratchet exists to catch.
    q = ScopeSql(scope, "oracle")
    sequences = await _collect(
        "sequences",
        f"""
        SELECT
            sequence_owner,
            sequence_name,
            min_value,
            max_value,
            increment_by,
            cycle_flag,
            cache_size
        FROM ALL_SEQUENCES
        WHERE {_owner_scope(q, "sequence_owner")}
        ORDER BY sequence_owner, sequence_name
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    q = ScopeSql(scope, "oracle")
    table_comments = await _collect(
        "table_comments",
        "SELECT owner, table_name, comments FROM ALL_TAB_COMMENTS "  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        f"WHERE {_owner_scope(q, 'owner')}{q.names('owner', 'table_name')}",
        q,
    )
    q = ScopeSql(scope, "oracle")
    column_comments = await _collect(
        "column_comments",
        f"""
        SELECT owner, table_name, column_name, comments
        FROM ALL_COL_COMMENTS
        WHERE {_owner_scope(q, "owner")}{q.names("owner", "table_name")}
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    # ALL_TAB_PRIVS names the owning schema TABLE_SCHEMA, where DBA_TAB_PRIVS names it
    # OWNER. ALL_USERS separates a user grantee from a role grantee; Oracle's privilege
    # views do not say which a grantee is. Schema scope only: grants establish a schema.
    q = ScopeSql(scope, "oracle")
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
        WHERE {_owner_scope(q, "p.table_schema")}
        ORDER BY p.table_schema, p.table_name, p.grantee, p.privilege
        """,  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
        q,
    )
    return _OracleEnvelopeRows(
        views=views,
        materialized_views=materialized_views,
        routines=routines,
        routine_source=routine_source,
        arguments=arguments,
        package_members=package_members,
        triggers=triggers,
        sequences=sequences,
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
        # R11-FP01, same rule: the flag is True because `discover()` reads the
        # named dictionary view and lands the result on the envelope.
        triggers=True,  # ALL_TRIGGERS, including TRIGGER_BODY
        sequences=True,  # ALL_SEQUENCES (declaration only -- never LAST_NUMBER)
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout
        self._scope = DiscoveryScope()

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    def scope_discovery(
        self,
        *,
        include_schemas: list[str],
        exclude_schemas: list[str],
        object_kinds: Sequence[str] = (),
        include_objects: Sequence[str] = (),
        exclude_objects: Sequence[str] = (),
    ) -> bool:
        """R11-FP01: push the selection into this adapter's dictionary queries.

        The schema scope reaches every read; object kinds and `schema.object` patterns
        reach the reads whose rows belong to one object (see `_fetch_envelope_rows` and
        `aida.connectors.schema_scope`). Everything pushed is a superset of the
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

                # R11-FP01: the roster takes the schema scope only -- it is what tells a
                # FULL run which in-scope schemas still exist (`schema_scope` docstring).
                q = ScopeSql(self._scope, "oracle")
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
                    WHERE {_owner_scope(q, "atc.owner")}
                    ORDER BY atc.owner, atc.table_name, atc.column_id
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
                # R11-FP02: the inventory read, wrapped as `FACET_INVENTORY` --
                # which records the refusal and then still lets it fail the run,
                # because that facet is in `RETIREMENT_BEARING_FACETS`. A FULL
                # run completing over zero objects would retire the estate; what
                # the wrap buys is an INTERRUPTED receipt that names the read.
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
                    for row in await read_facet(
                        FACET_INVENTORY,
                        _fetch_rows(cursor, columns_query, q.named or None),
                        known_relations=_DICTIONARY_READ,
                    )
                ]

                # R11-FP01: a table's constraints, indexes and partitions belong to it, so
                # these reads also take the `schema.object` patterns, on the owning table.
                q = ScopeSql(self._scope, "oracle")
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
                      AND {_owner_scope(q, "ac.owner")}{q.names("ac.owner", "ac.table_name")}
                    ORDER BY ac.owner, ac.table_name, ac.constraint_name, acc.position
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
                key_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "constraint_name": row["CONSTRAINT_NAME"],
                        "constraint_type": row["CONSTRAINT_TYPE"],
                        "column_name": row["COLUMN_NAME"],
                    }
                    for row in await read_facet(
                        FACET_CONSTRAINTS,
                        _fetch_rows(cursor, keys_query, q.named or None),
                        known_relations=_DICTIONARY_READ,
                    )
                ]

                q = ScopeSql(self._scope, "oracle")
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
                      AND {_owner_scope(q, "ac.owner")}{q.names("ac.owner", "ac.table_name")}
                    ORDER BY ac.owner, ac.table_name, ac.constraint_name, acc.position
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
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
                    for row in await read_facet(
                        FACET_CONSTRAINTS,
                        _fetch_rows(cursor, foreign_keys_query, q.named or None),
                        known_relations=_DICTIONARY_READ,
                    )
                ]

                # CT-3/CN-8: ALL_INDEXES/ALL_IND_COLUMNS mirror ALL_CONSTRAINTS/
                # ALL_CONS_COLUMNS above. Whether an index backs a PRIMARY KEY
                # constraint is surfaced via a LEFT JOIN on (owner, index_name)
                # rather than a second round trip.
                q = ScopeSql(self._scope, "oracle")
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
                    WHERE {_owner_scope(q, "ai.owner")}{q.names("ai.owner", "ai.table_name")}
                    ORDER BY ai.owner, ai.table_name, ai.index_name, aic.column_position
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
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
                    for row in await read_facet(
                        FACET_INDEXES,
                        _fetch_rows(cursor, indexes_query, q.named or None),
                        known_relations=_DICTIONARY_READ,
                    )
                ]

                q = ScopeSql(self._scope, "oracle")
                partition_type_query = f"""
                    SELECT owner AS table_schema, table_name AS table_name,
                           partitioning_type AS partition_type
                    FROM ALL_PART_TABLES
                    WHERE {_owner_scope(q, "owner")}{q.names("owner", "table_name")}
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
                partition_types = {
                    (row["TABLE_SCHEMA"], row["TABLE_NAME"]): row["PARTITION_TYPE"]
                    for row in await read_facet(
                        FACET_PARTITIONS,
                        _fetch_rows(cursor, partition_type_query, q.named or None),
                        known_relations=_DICTIONARY_READ,
                    )
                }

                q = ScopeSql(self._scope, "oracle")
                partition_key_query = f"""
                    SELECT owner AS table_schema, name AS table_name,
                           column_name AS column_name, column_position AS ordinal_position
                    FROM ALL_PART_KEY_COLUMNS
                    WHERE object_type = 'TABLE'
                      AND {_owner_scope(q, "owner")}{q.names("owner", "name")}
                    ORDER BY owner, name, column_position
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
                partition_key_columns: dict[tuple[str, str], list[str]] = {}
                for row in await read_facet(
                    FACET_PARTITIONS,
                    _fetch_rows(cursor, partition_key_query, q.named or None),
                    known_relations=_DICTIONARY_READ,
                ):
                    key = (row["TABLE_SCHEMA"], row["TABLE_NAME"])
                    partition_key_columns.setdefault(key, []).append(row["COLUMN_NAME"])

                # HIGH_VALUE is a LONG column; fetching it reliably needs an
                # output-type handler the async oracledb driver does not expose
                # the same way as the sync client, so partitions are extracted
                # without a high_value bound rather than risk a truncated or
                # failed fetch (same honesty tradeoff as the envelope helpers
                # above make for LONG columns, just without the reason-string
                # machinery since CN-8 is explicitly not an envelope 1.1 axis).
                q = ScopeSql(self._scope, "oracle")
                partitions_query = f"""
                    SELECT
                        table_owner AS table_schema,
                        table_name AS table_name,
                        partition_name AS partition_name,
                        partition_position AS ordinal_position
                    FROM ALL_TAB_PARTITIONS
                    WHERE {_owner_scope(q, "table_owner")}{q.names("table_owner", "table_name")}
                    ORDER BY table_owner, table_name, partition_position
                    """  # noqa: S608 -- static dictionary SQL; every pushed value is a bind
                partition_rows = []
                for row in await read_facet(
                    FACET_PARTITIONS,
                    _fetch_rows(cursor, partitions_query, q.named or None),
                    known_relations=_DICTIONARY_READ,
                ):
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

                envelope = await _fetch_envelope_rows(cursor, self._scope)
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
                        # Oracle folds unquoted aliases to upper case, which is
                        # why the read goes through a case-folding accessor
                        # rather than the alias as generated.
                        blank, whitespace, buckets = read_value_free_distribution(
                            position, _upper_cased_reader(row_dict)
                        )
                        snapshots.append(
                            ColumnProfileSnapshot(
                                name=name,
                                null_count=int(row_dict[f"N_{position}"]),
                                non_null_count=int(row_dict[f"NN_{position}"]),
                                approximate_distinct_count=int(row_dict[f"D_{position}"]),
                                min_length=row_dict[f"MINL_{position}"],
                                max_length=row_dict[f"MAXL_{position}"],
                                blank_count=blank,
                                whitespace_only_count=whitespace,
                                length_bucket_counts=buckets,
                                facet_status=_profile_facet_status(data_types.get(name, "")),
                            )
                        )
        finally:
            await connection.rollback()
            await connection.close()
        return TableProfileSnapshot(
            row_count_estimate=(max(estimate, sampled_row_count) if estimate is not None else None),
            sampled_row_count=sampled_row_count,
            columns=tuple(snapshots),
            # R11-FP04: the `FETCH FIRST n ROWS ONLY` above is the bound.
            # `ALL_TABLES.num_rows` is only as fresh as the last statistics
            # gather, so it can sit either side of the sample and must not be
            # what decides whether this profile saw the whole table.
            observation_scope=bounded_scan_scope(
                sampled_row_count=sampled_row_count, sample_rows=sample_rows
            ),
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
    if envelope is None:
        return assemble_catalog(str(catalog_name), tables)

    apply_table_descriptions(tables, _table_description_rows(envelope))
    apply_column_descriptions(tables, _column_description_rows(envelope))
    apply_view_definitions(tables, _view_definition_rows(tables, envelope))

    routines = _envelope_routines(envelope)
    grants = build_grants(_grant_rows(envelope))
    # R11-FP01: attached after assembly, for the reason recorded on
    # `connectors.base.attach_native_objects`.
    catalogs = attach_native_objects(
        assemble_catalog(
            str(catalog_name),
            tables,
            routines=routines,
            grants=grants,
        ),
        triggers=build_triggers(_trigger_rows(envelope)),
        sequences=build_sequences(_sequence_rows(envelope)),
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
