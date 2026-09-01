"""Source-native row/column policy synchronization (QG-2, module 16 open work).

Module 16 (`Docs/20-modules/16-query-gateway.md`, §7 "Masking") states the target
plainly: the gateway's application-level masking -- conservative, classification-driven,
alias/derived-expression propagation, implemented and load-bearing today in
`query_gateway.py` -- should additionally be **synchronized to source-native
enforcement where the underlying database supports it**. This module is that
synchronization path. It is defense in depth, not a replacement: nothing here
weakens or bypasses `query_gateway.py`'s existing masking, which keeps running
unconditionally whether or not a sync ever ran, or whether it succeeded.

**What is synchronized, and what deliberately is not.** The platform's governed
row/column obligations already exist as `aida.policy_engine.PolicyRecord` rows with
effect ``FILTER`` (a row predicate) or ``MASK`` (a classification + masking profile) --
`aida.business_graph.load_policies` is the existing loader. This module does not
invent a second policy language; it reads that same ABAC policy set and asks a
narrower question than `policy_engine.evaluate()` does: not "would principal X be
allowed to see this row", but "does this resource carry an obligation that holds for
*every* principal, unconditionally, and can therefore be pushed down to a database
engine that has no notion of Atlas's roles or purpose". Concretely, a policy is
eligible for native sync only when:

* its ``resource_match`` is expressible purely in terms of ``datasource_ids``,
  ``schema_pattern``, ``resource_types`` and ``classifications`` -- the attributes
  this module can resolve without a business-graph closure query -- and
* its ``subject_match`` is empty.

A policy scoped to a business node, a certification state, or a specific subject
(role, purpose, principal kind) is **not** synced: expressing "mask this only for
AGENT principals" as an unconditional native `ALTER COLUMN ... ADD MASKED` would
either mask it for humans too (a functional regression from what the policy actually
says) or require a session-variable bridge between Atlas's subject attributes and the
source engine's session context, which does not exist yet. Silently narrowing
enforcement to fit what a `CREATE POLICY` statement can express would be a security
bug wearing a feature's clothes, so such policies are left to the application-level
enforcement that already handles them correctly, and are counted in
`NativeSyncPlan.unsupported` rather than silently dropped.

**Which sources.** Real, tested DDL generation exists for exactly two connector
types, matching the two with real native pull adapters this platform can already
reason about and test against real syntax:

* **PostgreSQL** -- row-level security (`CREATE POLICY ... USING (...)`). Postgres has
  no core column-masking construct (it would need the third-party
  `postgresql_anonymizer` extension, not assumed present), so column `MASK` policies
  for a Postgres datasource are left unsynced -- reported in `unsupported`, still
  enforced at the application layer.
* **SQL Server** -- column-level Dynamic Data Masking
  (`ALTER TABLE ... ALTER COLUMN ... ADD MASKED WITH (FUNCTION = ...)`). SQL Server's
  native row-level security (`CREATE SECURITY POLICY` plus a predicate inline table
  function) is real T-SQL but is **not implemented here** -- it needs a schema-bound
  predicate function generated and deployed ahead of the policy, which is a second
  object lifecycle this module does not yet manage. `FILTER` policies for a SQL
  Server datasource are therefore also left unsynced today, reported the same way.
  Documented as future work rather than shipped half-correct, matching this
  codebase's convention of being honest about what is verified (see QG-5's Vault
  provider, tested only against a mocked transport, for the same posture).

Every other connector type (Snowflake, BigQuery, Oracle, dbt-fed sources, ...) is
out of scope for native sync entirely; `SUPPORTED_CONNECTOR_TYPES` names exactly the
two above and `build_native_sync_plan` refuses anything else.

**Safety of generated SQL.** Two different kinds of caller-adjacent text end up in
generated DDL, and each is handled differently:

* Identifiers (schema, table, column name, policy code) come from the metadata
  catalog and `AccessPolicy.code` -- already-trusted platform data -- but are still
  quoted defensively (`"..."` doubling for Postgres, `[...]` doubling for SQL Server,
  the same escaping every connector in `aida.connectors` already uses for identifiers
  reflected back from `information_schema`/`sys.*`).
* A `FILTER` policy's row-filter expression is governance-authored SQL predicate
  text -- that is its entire purpose, the same way `SourceBinding.masking_profile` or
  a governed tool's SQL template is trusted, reviewed text rather than untrusted
  input. It is still round-tripped through `sqlglot` (`_validate_predicate`) before
  being concatenated into a `CREATE POLICY ... USING (...)` clause: parsed as a
  `WHERE` condition, rejected if it does not parse to exactly one boolean expression,
  rejected if it contains any DDL/DML/administrative node (the same denylist
  `sql_guard.SqlGuard` already applies to gateway-bound SQL), and re-rendered from
  the parsed AST rather than passed through as raw text -- so a semicolon-separated
  second statement or a comment-smuggled tail cannot survive into the generated DDL.

**Apply mode is a distinct execution surface from the query gateway, by design, not
by oversight.** `aida.connectors.execution_access` -- the only source of a
`SqlExecutor` -- is import-linter-restricted to `aida.query_gateway` alone (INV-2,
`pyproject.toml`), and `query_gateway.SqlGuard` refuses every DDL/administrative
statement outright: `CREATE POLICY` and `ALTER TABLE ... ADD MASKED` could never
pass through that pipeline, by the same rule that makes the gateway safe for
governed reads. This module therefore never imports `execution_access` and never
calls `SqlExecutor.execute_read_query`/`estimate_read_query` -- `apply_native_sync_plan`
opens its own narrowly-scoped administrative connection (`asyncpg` for Postgres,
`pytds` for SQL Server, the same drivers the read connectors use) and executes
*only* the exact statements this module generated, inside one transaction, nothing
caller-supplied. `tests/test_tier0_invariants.py::test_no_connector_execution_outside_gateway`
statically confirms this module never calls either gateway-restricted method.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from aida.policy_engine import PolicyRecord

#: The only connector types with a real, tested native-DDL path (see module
#: docstring). Every other connector type is honestly unsupported.
SUPPORTED_CONNECTOR_TYPES = frozenset({"postgres", "sqlserver"})

_MAX_IDENTIFIER_LENGTH = 128
_SAFE_CODE_CHARS = re.compile(r"[^A-Za-z0-9_]")

#: SQL Server Dynamic Data Masking functions keyed by the platform's
#: `masking_profile` vocabulary (`SourceBinding.masking_profile`, free text with a
#: "DEFAULT" convention -- see `models.py`). Unknown or absent profiles fall back to
#: `default()`, the strictest DDM function, matching module 16 §7's "default is
#: conservative -- when classification is uncertain, mask" rule.
MSSQL_MASKING_FUNCTIONS: dict[str, str] = {
    "DEFAULT": "default()",
    "EMAIL": "email()",
    "PARTIAL": 'partial(1, "XXXXXXX", 0)',
    "RANDOM": "random(1, 100)",
}


class PolicyNativeSyncError(RuntimeError):
    """A governed policy or plan could not be safely translated into native DDL."""


# ---------------------------------------------------------------------------
# Resolution: governed ABAC policy records -> unconditional native obligations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NativeRowPolicy:
    """One unconditional row-filter obligation, ready to become an RLS policy."""

    schema_name: str
    table_name: str
    policy_code: str
    policy_version: int
    row_filter: str


@dataclass(frozen=True, slots=True)
class NativeColumnPolicy:
    """One unconditional column-mask obligation, ready to become a DDM column."""

    schema_name: str
    table_name: str
    column_name: str
    classification: str
    policy_code: str
    policy_version: int
    masking_profile: str


def _condition_applies(condition: dict[str, Any], *, now: datetime) -> bool:
    """Clock-only condition check -- the one part of `policy.condition` that is not
    itself an attribute of the resource or subject and so has to be evaluated here
    rather than left to the source. Mirrors `policy_engine._matches_condition`
    (kept as an independent, deliberately small copy -- see the module docstring on
    why this module does not import `policy_engine`'s private matching helpers).
    """
    not_before = condition.get("not_before")
    if not_before is not None and now < datetime.fromisoformat(not_before):
        return False
    not_after = condition.get("not_after")
    if not_after is not None and now > datetime.fromisoformat(not_after):
        return False
    return True


def _is_unconditional_on_subject(policy: PolicyRecord) -> bool:
    """True when this policy's obligation holds for every principal.

    An empty `subject_match` is the platform's own spelling of "applies to
    everyone" (`policy_engine._matches_subject` returns True immediately for one).
    That is exactly the class of policy expressible as a native DB construct, which
    by definition cannot see Atlas's roles, purpose, or principal kind.
    """
    return not policy.subject_match


def _is_resolvable_resource_match(resource_match: dict[str, Any]) -> bool:
    """True when every key this policy's `resource_match` sets is one this module
    can resolve locally (datasource, schema, resource type, classification).

    `business_node_ids` and `certifications` require a business-graph closure query
    this module deliberately does not perform (see module docstring); a policy that
    sets either is left to application-level enforcement rather than synced with a
    silently-narrowed meaning.
    """
    unresolvable_keys = {"business_node_ids", "certifications"}
    return not (unresolvable_keys & resource_match.keys())


def _datasource_matches(resource_match: dict[str, Any], datasource_id: UUID) -> bool:
    datasource_ids = resource_match.get("datasource_ids")
    if datasource_ids is None:
        return True
    return str(datasource_id) in {str(value) for value in datasource_ids}


def _schema_matches(resource_match: dict[str, Any], schema_name: str) -> bool:
    from fnmatch import fnmatchcase

    pattern = resource_match.get("schema_pattern")
    if pattern is None:
        return True
    return fnmatchcase(schema_name, pattern)


def _resource_type_matches(resource_match: dict[str, Any], resource_type: str) -> bool:
    types = resource_match.get("resource_types")
    if types is None:
        return True
    return resource_type in types


def unconditional_row_filter_policies(
    policies: Sequence[PolicyRecord],
    *,
    datasource_id: UUID,
    schema_name: str,
    now: datetime | None = None,
) -> tuple[PolicyRecord, ...]:
    """`FILTER`-effect policies eligible for native row-level sync on this table.

    "Eligible" means: effect is `FILTER`, the resource match resolves against a
    `table`-shaped resource without a business-graph query, it names this
    datasource (or no datasource restriction at all) and this schema (or no schema
    restriction), it carries no subject restriction, and any clock condition holds
    now. Order is preserved from `policies` (the caller's load order), so a
    deterministic re-sync produces the same statement order every time.
    """
    resolved_now = now or datetime.now(UTC)
    matched: list[PolicyRecord] = []
    for policy in policies:
        if policy.effect != "FILTER":
            continue
        if not _is_unconditional_on_subject(policy):
            continue
        if not _is_resolvable_resource_match(policy.resource_match):
            continue
        if not _resource_type_matches(policy.resource_match, "table"):
            continue
        if not _datasource_matches(policy.resource_match, datasource_id):
            continue
        if not _schema_matches(policy.resource_match, schema_name):
            continue
        if not _condition_applies(policy.condition, now=resolved_now):
            continue
        if not policy.transform.get("row_filter"):
            continue
        matched.append(policy)
    return tuple(matched)


def unconditional_mask_policies_for_column(
    policies: Sequence[PolicyRecord],
    *,
    datasource_id: UUID,
    schema_name: str,
    classification: str,
    now: datetime | None = None,
) -> tuple[PolicyRecord, ...]:
    """`MASK`-effect policies eligible for native column sync on one column.

    Same eligibility shape as `unconditional_row_filter_policies`, for a `column`
    resource carrying `classification`, plus: the policy's `resource_match`
    classifications (when set) must include `classification` -- an unset
    `classifications` match applies to every classification, same as
    `policy_engine._matches_resource`.
    """
    resolved_now = now or datetime.now(UTC)
    matched: list[PolicyRecord] = []
    for policy in policies:
        if policy.effect != "MASK":
            continue
        if not _is_unconditional_on_subject(policy):
            continue
        if not _is_resolvable_resource_match(policy.resource_match):
            continue
        if not _resource_type_matches(policy.resource_match, "column"):
            continue
        if not _datasource_matches(policy.resource_match, datasource_id):
            continue
        if not _schema_matches(policy.resource_match, schema_name):
            continue
        classifications = policy.resource_match.get("classifications")
        if classifications is not None and classification not in classifications:
            continue
        if not _condition_applies(policy.condition, now=resolved_now):
            continue
        matched.append(policy)
    return tuple(matched)


def resolve_native_table_policies(
    policies: Sequence[PolicyRecord],
    *,
    datasource_id: UUID,
    schema_name: str,
    table_name: str,
    columns: Sequence[tuple[str, str]],
    now: datetime | None = None,
) -> tuple[tuple[NativeRowPolicy, ...], tuple[NativeColumnPolicy, ...]]:
    """Resolve one table's governed obligations into native-sync-eligible policies.

    `columns` is `(column_name, classification)` pairs -- an empty or falsy
    classification means "not classified", and is skipped rather than matched
    against a `MASK` policy with no `classifications` restriction, so an
    unclassified column is never masked by inference alone.

    The highest-priority eligible policy wins per row/column, mirroring
    `policy_engine.evaluate`'s own tie-break (highest priority, then lowest policy
    id) -- native sync should not disagree with the application layer about which
    policy is authoritative when more than one applies.
    """
    resolved_now = now or datetime.now(UTC)
    row_candidates = unconditional_row_filter_policies(
        policies, datasource_id=datasource_id, schema_name=schema_name, now=resolved_now
    )
    row_policies = tuple(
        NativeRowPolicy(
            schema_name=schema_name,
            table_name=table_name,
            policy_code=policy.code,
            policy_version=policy.version,
            row_filter=str(policy.transform["row_filter"]),
        )
        for policy in sorted(
            row_candidates, key=lambda item: (-item.priority, str(item.id))
        )
    )

    column_policies: list[NativeColumnPolicy] = []
    for column_name, classification in columns:
        # "UNCLASSIFIED" is `MetadataColumn.classification`'s own default value
        # (models.py) -- treated the same as an empty string, since a column that
        # has never been classified is not evidence a `MASK` policy should apply.
        if not classification or classification == "UNCLASSIFIED":
            continue
        candidates = unconditional_mask_policies_for_column(
            policies,
            datasource_id=datasource_id,
            schema_name=schema_name,
            classification=classification,
            now=resolved_now,
        )
        if not candidates:
            continue
        winner = sorted(candidates, key=lambda item: (-item.priority, str(item.id)))[0]
        column_policies.append(
            NativeColumnPolicy(
                schema_name=schema_name,
                table_name=table_name,
                column_name=column_name,
                classification=classification,
                policy_code=winner.code,
                policy_version=winner.version,
                masking_profile=str(winner.transform.get("masking_profile") or "DEFAULT"),
            )
        )
    return row_policies, tuple(column_policies)


# ---------------------------------------------------------------------------
# DDL generation -- pure, exhaustively testable, no I/O
# ---------------------------------------------------------------------------


def _validate_identifier(name: str, *, label: str) -> str:
    if not name or len(name) > _MAX_IDENTIFIER_LENGTH or "\x00" in name:
        raise PolicyNativeSyncError(f"invalid {label}: {name!r}")
    return name


def _sanitize_code(code: str) -> str:
    """A policy code, made safe to embed unquoted inside a generated object name.

    `AccessPolicy.code` is governance-authored but not guaranteed identifier-safe
    (it is a free-text `String(80)`), so this is a denylist substitution
    (non-alnum/underscore -> `_`) rather than a quoting scheme -- the resulting
    object name is still wrapped in `_pg_quote_ident`/`_mssql_quote_ident` by the
    caller, this just keeps it human-recognisable and collision-resistant.
    """
    sanitized = _SAFE_CODE_CHARS.sub("_", code.strip())
    if not sanitized:
        raise PolicyNativeSyncError("policy code must contain at least one identifier character")
    return sanitized.lower()


def _pg_quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _mssql_quote_ident(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _mssql_quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


#: Node types that must never appear in a validated row-filter predicate. The same
#: DDL/DML/administrative denylist `sql_guard.SqlGuard` applies to gateway-bound
#: SQL (see its module docstring) -- a row-filter predicate is a boolean expression,
#: not a statement, and none of these belong in one.
_FORBIDDEN_PREDICATE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Alter,
    exp.Command,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Into,
    exp.Lock,
    exp.Merge,
    exp.Transaction,
    exp.TruncateTable,
    exp.Update,
)

#: A predicate can legally contain a subquery (`tenant_id IN (SELECT ...)` is a
#: real, common RLS pattern), so `_FORBIDDEN_PREDICATE_NODES` alone is not enough --
#: a subquery is exactly where a call to a network/filesystem/session-control
#: function would otherwise hide from that node-type check. Deliberately the same
#: Postgres denylist `sql_guard.SqlGuard._forbidden_functions_by_dialect["postgres"]`
#: applies to gateway-bound SQL, independently duplicated (not imported -- see the
#: module docstring on why this module does not reach into another module's private
#: members) because a `CREATE POLICY ... USING (...)` clause runs on every future
#: query against the table, unattended, which makes it at least as sensitive a
#: place for `pg_sleep`/`dblink_connect`/`pg_read_file` to reach as the read path
#: `sql_guard.py` already refuses them from.
_FORBIDDEN_PREDICATE_FUNCTIONS_BY_DIALECT: dict[str, frozenset[str]] = {
    "postgres": frozenset(
        {
            "dblink",
            "dblink_connect",
            "dblink_connect_u",
            "dblink_exec",
            "dblink_open",
            "dblink_fetch",
            "dblink_send_query",
            "lo_export",
            "lo_import",
            "lo_read",
            "lo_write",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            "pg_stat_file",
            "pg_sleep",
            "pg_sleep_for",
            "pg_sleep_until",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "pg_reload_conf",
            "pg_rotate_logfile",
            "pg_switch_wal",
        }
    ),
}


def _validate_predicate(expr: str, *, dialect: str) -> str:
    """Parse `expr` as a single boolean `WHERE` condition and re-render it.

    Re-rendering from the parsed AST (rather than passing the input through) is the
    load-bearing safety property: a semicolon-separated second statement or a
    comment-smuggled tail cannot survive a round trip through `sqlglot`, the same
    property `query_gateway._run_validation` relies on for the SQL it accepts.
    """
    if ";" in expr:
        raise PolicyNativeSyncError(
            "row filter predicate must be a single expression -- semicolons are not permitted"
        )
    try:
        wrapped = parse_one(f"SELECT 1 WHERE {expr}", read=dialect)
    except ParseError as exc:
        raise PolicyNativeSyncError(f"unparseable row filter predicate: {exc}") from exc
    where = wrapped.args.get("where")
    condition = where.this if where is not None else None
    if condition is None:
        raise PolicyNativeSyncError("row filter predicate did not parse to a boolean condition")
    for forbidden in _FORBIDDEN_PREDICATE_NODES:
        if condition.find(forbidden) is not None:
            raise PolicyNativeSyncError(
                f"row filter predicate contains a forbidden construct: {forbidden.__name__}"
            )
    forbidden_functions = _FORBIDDEN_PREDICATE_FUNCTIONS_BY_DIALECT.get(dialect, frozenset())
    for function in condition.find_all(exp.Func):
        function_name = (function.name or function.sql_name()).lower()
        if function_name in forbidden_functions:
            raise PolicyNativeSyncError(
                f"row filter predicate contains a forbidden function: {function_name}"
            )
    return str(condition.sql(dialect=dialect))


@dataclass(frozen=True, slots=True)
class NativeStatement:
    """One generated DDL statement, with enough context to preview or audit it."""

    kind: str
    sql: str
    target_schema: str
    target_table: str
    target_column: str | None
    policy_code: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sql": self.sql,
            "target_schema": self.target_schema,
            "target_table": self.target_table,
            "target_column": self.target_column,
            "policy_code": self.policy_code,
        }


def postgres_row_policy_statements(policy: NativeRowPolicy) -> tuple[NativeStatement, ...]:
    """`ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY` + `CREATE POLICY ... USING (...)`.

    `FORCE ROW LEVEL SECURITY` is included deliberately: without it, RLS does not
    apply to the table owner, which -- for a warehouse where the query gateway's
    connection role is commonly granted broad read access -- would make the native
    policy a no-op for exactly the traffic it exists to constrain.

    A leading `DROP POLICY IF EXISTS` makes re-sync idempotent: re-running this
    against a table already carrying this policy's previous version replaces it
    rather than erroring on a duplicate name.
    """
    schema = _pg_quote_ident(_validate_identifier(policy.schema_name, label="schema name"))
    table = _pg_quote_ident(_validate_identifier(policy.table_name, label="table name"))
    qualified = f"{schema}.{table}"
    predicate = _validate_predicate(policy.row_filter, dialect="postgres")
    policy_name = _pg_quote_ident(
        f"atlas_rowpolicy_{_sanitize_code(policy.policy_code)}_v{policy.policy_version}"
    )
    return (
        NativeStatement(
            kind="ENABLE_ROW_LEVEL_SECURITY",
            sql=f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY;",
            target_schema=policy.schema_name,
            target_table=policy.table_name,
            target_column=None,
            policy_code=policy.policy_code,
        ),
        NativeStatement(
            kind="FORCE_ROW_LEVEL_SECURITY",
            sql=f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY;",
            target_schema=policy.schema_name,
            target_table=policy.table_name,
            target_column=None,
            policy_code=policy.policy_code,
        ),
        NativeStatement(
            kind="DROP_EXISTING_ROW_POLICY",
            sql=f"DROP POLICY IF EXISTS {policy_name} ON {qualified};",
            target_schema=policy.schema_name,
            target_table=policy.table_name,
            target_column=None,
            policy_code=policy.policy_code,
        ),
        NativeStatement(
            kind="CREATE_ROW_POLICY",
            sql=(
                f"CREATE POLICY {policy_name} ON {qualified}\n"
                f"    FOR SELECT\n"
                f"    USING ({predicate});"
            ),
            target_schema=policy.schema_name,
            target_table=policy.table_name,
            target_column=None,
            policy_code=policy.policy_code,
        ),
    )


def sqlserver_column_mask_statements(policy: NativeColumnPolicy) -> tuple[NativeStatement, ...]:
    """A leading conditional `DROP MASKED` (idempotent resync) plus `ADD MASKED WITH (...)`.

    SQL Server errors on `ADD MASKED` against a column that already carries a mask,
    so the drop is not optional for a policy that changes on re-sync -- it is
    wrapped in `IF EXISTS (SELECT ... sys.masked_columns ...)` so the *first* sync
    of a never-masked column does not fail on a mask that was never there.
    """
    schema_name = _validate_identifier(policy.schema_name, label="schema name")
    table_name = _validate_identifier(policy.table_name, label="table name")
    column_name = _validate_identifier(policy.column_name, label="column name")
    schema = _mssql_quote_ident(schema_name)
    table = _mssql_quote_ident(table_name)
    column = _mssql_quote_ident(column_name)
    qualified = f"{schema}.{table}"
    function = MSSQL_MASKING_FUNCTIONS.get(
        policy.masking_profile.upper(), MSSQL_MASKING_FUNCTIONS["DEFAULT"]
    )
    # Flagged by shape (string-built SQL) but not by substance: every interpolated
    # value below is either an `_mssql_quote_ident`-quoted identifier or an
    # `_mssql_quote_literal`-escaped string literal -- the same "identifiers are
    # quoted, limits are validated" exemption connectors/postgres.py and
    # connectors/sqlserver.py already carry for their own generated SQL.
    exists_check = (
        "IF EXISTS (SELECT 1 FROM sys.masked_columns mc "  # noqa: S608
        "JOIN sys.tables t ON t.object_id = mc.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name = {_mssql_quote_literal(schema_name)} "
        f"AND t.name = {_mssql_quote_literal(table_name)} "
        f"AND mc.name = {_mssql_quote_literal(column_name)}) "
        f"ALTER TABLE {qualified} ALTER COLUMN {column} DROP MASKED;"
    )
    return (
        NativeStatement(
            kind="DROP_EXISTING_COLUMN_MASK",
            sql=exists_check,
            target_schema=policy.schema_name,
            target_table=policy.table_name,
            target_column=policy.column_name,
            policy_code=policy.policy_code,
        ),
        NativeStatement(
            kind="ADD_COLUMN_MASK",
            sql=(
                f"ALTER TABLE {qualified} ALTER COLUMN {column} "
                f"ADD MASKED WITH (FUNCTION = '{function}');"
            ),
            target_schema=policy.schema_name,
            target_table=policy.table_name,
            target_column=policy.column_name,
            policy_code=policy.policy_code,
        ),
    )


@dataclass(frozen=True, slots=True)
class NativeSyncPlan:
    """The generated DDL for one table, plus an honest account of what was skipped."""

    datasource_id: UUID
    connector_type: str
    schema_name: str
    table_name: str
    row_policies: tuple[NativeRowPolicy, ...]
    column_policies: tuple[NativeColumnPolicy, ...]
    statements: tuple[NativeStatement, ...]
    #: Human-readable reasons obligations were left unsynced -- e.g. "PostgreSQL
    #: has no native column-masking construct; N column policy(ies) remain
    #: application-level only". Never empty by omission: every skip is named.
    unsupported: tuple[str, ...] = field(default_factory=tuple)


def build_native_sync_plan(
    policies: Sequence[PolicyRecord],
    *,
    datasource_id: UUID,
    connector_type: str,
    schema_name: str,
    table_name: str,
    columns: Sequence[tuple[str, str]],
    now: datetime | None = None,
) -> NativeSyncPlan:
    """Resolve governed policies for one table and generate the matching native DDL.

    Pure and DB-free: `columns` is supplied by the caller (typically the
    `metadata_column` catalog rows for this table), `policies` by
    `aida.business_graph.load_policies`. Raises `PolicyNativeSyncError` only for a
    connector type with no native-sync path at all (`SUPPORTED_CONNECTOR_TYPES`);
    an unsupported *obligation* for an otherwise-supported connector (e.g. a column
    `MASK` policy on a Postgres table) is reported in `unsupported`, not raised --
    the row-level part of the same table's policy set is still worth synchronizing.
    """
    if connector_type not in SUPPORTED_CONNECTOR_TYPES:
        raise PolicyNativeSyncError(
            f"source-native policy sync has no implementation for connector type "
            f"{connector_type!r}; supported today: {sorted(SUPPORTED_CONNECTOR_TYPES)} "
            "-- see Docs/20-modules/16-query-gateway.md for other sources as future work"
        )
    row_policies, column_policies = resolve_native_table_policies(
        policies,
        datasource_id=datasource_id,
        schema_name=schema_name,
        table_name=table_name,
        columns=columns,
        now=now,
    )
    statements: list[NativeStatement] = []
    unsupported: list[str] = []
    if connector_type == "postgres":
        for row_policy in row_policies:
            statements.extend(postgres_row_policy_statements(row_policy))
        if column_policies:
            unsupported.append(
                f"PostgreSQL has no native column-masking construct without the "
                f"postgresql_anonymizer extension (not assumed present); "
                f"{len(column_policies)} column policy(ies) remain application-level only"
            )
            column_policies = ()
    elif connector_type == "sqlserver":
        for column_policy in column_policies:
            statements.extend(sqlserver_column_mask_statements(column_policy))
        if row_policies:
            unsupported.append(
                f"SQL Server native row-level security (CREATE SECURITY POLICY) is "
                f"not yet implemented -- it requires deploying a schema-bound "
                f"predicate function, a second object lifecycle this module does "
                f"not yet manage; {len(row_policies)} row policy(ies) remain "
                f"application-level only"
            )
            row_policies = ()
    return NativeSyncPlan(
        datasource_id=datasource_id,
        connector_type=connector_type,
        schema_name=schema_name,
        table_name=table_name,
        row_policies=row_policies,
        column_policies=column_policies,
        statements=tuple(statements),
        unsupported=tuple(unsupported),
    )


# ---------------------------------------------------------------------------
# Apply mode -- a distinct, narrowly-scoped administrative connection (see the
# module docstring for why this is not routed through the query gateway).
# ---------------------------------------------------------------------------


class _AsyncConnection(Protocol):
    async def execute(self, sql: str, /) -> Any: ...
    async def close(self) -> None: ...


class _AsyncTransaction(Protocol):
    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, *exc_info: object) -> Any: ...


class _AsyncpgLikeConnection(_AsyncConnection, Protocol):
    def transaction(self) -> _AsyncTransaction: ...


PostgresConnect = Callable[..., Awaitable[_AsyncpgLikeConnection]]


async def _default_postgres_connect(
    dsn: str, *, timeout_seconds: float
) -> _AsyncpgLikeConnection:
    import asyncpg

    return await asyncpg.connect(dsn, command_timeout=timeout_seconds)  # type: ignore[no-any-return]


async def _apply_postgres(
    plan: NativeSyncPlan,
    dsn: str,
    *,
    timeout_seconds: float,
    connect: PostgresConnect | None,
) -> None:
    opener = connect or _default_postgres_connect
    connection = await opener(dsn, timeout_seconds=timeout_seconds)
    try:
        async with connection.transaction():
            for statement in plan.statements:
                await connection.execute(statement.sql)
    finally:
        await connection.close()


@dataclass(frozen=True, slots=True)
class _MssqlConnectionParams:
    host: str
    port: int
    database: str
    user: str
    password: str


def _parse_mssql_dsn(dsn: str) -> _MssqlConnectionParams:
    """Independent copy of `connectors.sqlserver._parse_dsn` (module-private there).

    Kept deliberately duplicated rather than imported: this module's whole
    argument for staying outside INV-2's protected surface is that it never
    reaches into the read-execution connector module at all (see the module
    docstring) -- importing a private helper from `connectors.sqlserver` would
    quietly recreate exactly the coupling that argument depends on not existing.
    """
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"mssql", "sqlserver"}:
        raise PolicyNativeSyncError(
            "invalid SQL Server connection reference; expected "
            "mssql://user:password@host:port/database"
        )
    if not parsed.hostname or not parsed.username or parsed.password is None:
        raise PolicyNativeSyncError(
            "SQL Server connection reference is missing host, user, or password"
        )
    database = parsed.path.lstrip("/")
    if not database:
        raise PolicyNativeSyncError("SQL Server connection reference must include a database name")
    return _MssqlConnectionParams(
        host=parsed.hostname,
        port=parsed.port or 1433,
        database=database,
        user=unquote(parsed.username),
        password=unquote(parsed.password),
    )


class _DbApiConnection(Protocol):
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


SqlServerConnect = Callable[..., _DbApiConnection]


def _default_sqlserver_connect(
    params: _MssqlConnectionParams, *, timeout_seconds: float
) -> _DbApiConnection:
    import pytds

    return pytds.connect(  # type: ignore[no-any-return]
        server=params.host,
        port=params.port,
        database=params.database,
        user=params.user,
        password=params.password,
        timeout=timeout_seconds,
        login_timeout=min(timeout_seconds, 15.0),
        autocommit=False,
    )


def _apply_sqlserver_sync(
    plan: NativeSyncPlan,
    params: _MssqlConnectionParams,
    *,
    timeout_seconds: float,
    connect: SqlServerConnect,
) -> None:
    connection = connect(params, timeout_seconds=timeout_seconds)
    try:
        cursor = connection.cursor()
        try:
            for statement in plan.statements:
                cursor.execute(statement.sql)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    finally:
        connection.close()


async def _apply_sqlserver(
    plan: NativeSyncPlan,
    dsn: str,
    *,
    timeout_seconds: float,
    connect: SqlServerConnect | None,
) -> None:
    params = _parse_mssql_dsn(dsn)
    opener = connect or _default_sqlserver_connect
    await asyncio.to_thread(
        _apply_sqlserver_sync, plan, params, timeout_seconds=timeout_seconds, connect=opener
    )


async def apply_native_sync_plan(
    plan: NativeSyncPlan,
    *,
    dsn: str,
    timeout_seconds: float = 30.0,
    postgres_connect: PostgresConnect | None = None,
    sqlserver_connect: SqlServerConnect | None = None,
) -> None:
    """Execute `plan.statements` against the live source, in one transaction.

    Never called except from the gated apply path in `policy_native_sync_api.py`,
    which only reaches here after the maker-checker decision on the plan has been
    recorded `APPROVED` by a principal other than the one who requested it (see
    that module). A no-op for an empty plan -- nothing to apply is not an error.
    `postgres_connect`/`sqlserver_connect` exist for tests to inject a fake
    connection; production callers never pass them.
    """
    if not plan.statements:
        return
    if plan.connector_type == "postgres":
        await _apply_postgres(
            plan, dsn, timeout_seconds=timeout_seconds, connect=postgres_connect
        )
    elif plan.connector_type == "sqlserver":
        await _apply_sqlserver(
            plan, dsn, timeout_seconds=timeout_seconds, connect=sqlserver_connect
        )
    else:  # pragma: no cover - build_native_sync_plan already refuses this
        raise PolicyNativeSyncError(
            f"apply is not implemented for connector type {plan.connector_type!r}"
        )
