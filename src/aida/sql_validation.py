"""Deterministic SQL validation as a first-class, value-free result (review item N14).

`Docs/review-2026-08/target/03-context-tools-agents-mcp.md` §5 asks for the
gateway's deterministic pipeline -- AST parse, read-only check, reference
extraction, catalog resolution, per-object authorisation, structural rules,
cost estimate -- to be callable *without executing*, returning structured
findings a coding agent can iterate against.

This module is the finding vocabulary and the pure, value-free assembly of a
report. It deliberately holds **no** connector access and **no** database
access:

* `aida.connectors.execution_access` is protected by the import-linter contract
  "INV-2 connector SQL execution is reachable only from the query gateway", so
  the only module allowed to reach a source is `aida.query_gateway`. The
  catalog reads and the dry-run estimate therefore live on
  `QueryExecutionGateway`, which calls the helpers below.
* `QueryExecutionGateway.execute` runs the *same* helpers through the *same*
  private path (`QueryExecutionGateway._run_validation`), so "what validation
  says" and "what execution enforces" cannot drift: there is one implementation
  and two entry points.

INV-6: nothing produced here may carry a source or user value. Findings carry
object names, stable machine codes, static hints and numbers only. In
particular the sqlglot parse-error message is *withheld* rather than echoed,
because a parser error commonly quotes the offending SQL fragment, literals
included, and any SQL echoed back is redacted by
`aida.query_gateway.redact_sql_literals` first.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlglot import exp, parse_one

from aida.sql_guard import SqlValidationResult

# --- Finding vocabulary ----------------------------------------------------
#
# These strings are a published contract: an agent branches on them, so they
# are append-only. Renaming one is a breaking change to every MCP client.

FINDING_SQL_PARSE_ERROR: Final = "SQL_PARSE_ERROR"
FINDING_READ_ONLY_QUERY_REQUIRED: Final = "READ_ONLY_QUERY_REQUIRED"
FINDING_MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN: Final = "MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN"
FINDING_SELECT_INTO_FORBIDDEN: Final = "SELECT_INTO_FORBIDDEN"
FINDING_EXACTLY_ONE_STATEMENT_REQUIRED: Final = "EXACTLY_ONE_STATEMENT_REQUIRED"
FINDING_CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN: Final = "CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN"
FINDING_SELECT_WILDCARD_FORBIDDEN: Final = "SELECT_WILDCARD_FORBIDDEN"
FINDING_FORBIDDEN_FUNCTION: Final = "FORBIDDEN_FUNCTION"
FINDING_TABLE_VALUED_SOURCE_FORBIDDEN: Final = "TABLE_VALUED_SOURCE_FORBIDDEN"
FINDING_LOCKING_READ_FORBIDDEN: Final = "LOCKING_READ_FORBIDDEN"
FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE: Final = "UNKNOWN_OR_UNAUTHORIZED_TABLE"
FINDING_UNKNOWN_COLUMN: Final = "UNKNOWN_COLUMN"
FINDING_COST_CEILING_EXCEEDED: Final = "COST_CEILING_EXCEEDED"
FINDING_BYTE_BUDGET_EXCEEDED: Final = "BYTE_BUDGET_EXCEEDED"
FINDING_ROW_LIMIT_APPLIED: Final = "ROW_LIMIT_APPLIED"
FINDING_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR: Final = "ESTIMATE_UNAVAILABLE_FOR_CONNECTOR"

SEVERITY_ERROR: Final = "ERROR"
SEVERITY_INFO: Final = "INFO"

#: Guard violations that are structural rather than parametrised. Everything the
#: guard can emit maps onto exactly one finding code, so a rule added to
#: `SqlGuard` surfaces here without a second edit -- an unmapped violation still
#: becomes a blocking finding, it just gets the generic hint.
_GUARD_HINTS: Final[dict[str, str]] = {
    FINDING_READ_ONLY_QUERY_REQUIRED: (
        "only a single read-only query is accepted; this statement is not a query"
    ),
    FINDING_MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN: (
        "the platform is read-only: DDL, DML, transaction control and admin "
        "commands are refused before any source is contacted"
    ),
    FINDING_SELECT_INTO_FORBIDDEN: "SELECT ... INTO writes to the source and is refused",
    FINDING_EXACTLY_ONE_STATEMENT_REQUIRED: (
        "submit exactly one statement; batches cannot be governed as a unit"
    ),
    FINDING_CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN: (
        "every join needs an ON or USING condition; declare the join key"
    ),
    FINDING_SELECT_WILDCARD_FORBIDDEN: (
        "project columns explicitly; SELECT * cannot be classification-checked "
        "or masked reliably"
    ),
    FINDING_FORBIDDEN_FUNCTION: (
        "this function reaches outside the query engine and is not callable here"
    ),
    FINDING_TABLE_VALUED_SOURCE_FORBIDDEN: (
        "a table-valued function, linked-server call, or remote rowset cannot be "
        "resolved against the metadata catalog and is not a valid data source here"
    ),
    FINDING_LOCKING_READ_FORBIDDEN: (
        "FOR UPDATE / FOR SHARE take locks against the source; the gateway only "
        "runs stateless, non-locking reads"
    ),
}

_GUARD_CODES: Final[frozenset[str]] = frozenset(
    {
        FINDING_SQL_PARSE_ERROR,
        *_GUARD_HINTS,
    }
)

_CATALOG_CODES: Final[frozenset[str]] = frozenset(
    {FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE, FINDING_UNKNOWN_COLUMN}
)

_ESTIMATE_CODES: Final[frozenset[str]] = frozenset(
    {
        FINDING_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR,
        FINDING_COST_CEILING_EXCEEDED,
        FINDING_BYTE_BUDGET_EXCEEDED,
    }
)


@dataclass(frozen=True, slots=True)
class SqlFinding:
    """One machine-actionable validation finding.

    `detail` carries numbers and flags only -- never a value read from, or
    destined for, a data source (INV-6).
    """

    code: str
    severity: str
    ref: str | None
    hint: str
    detail: dict[str, float | int | bool | str | None] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "ref": self.ref,
            "hint": self.hint,
        }
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


@dataclass(frozen=True, slots=True)
class EstimateOutcome:
    """The connector-agnostic dry-run outcome, reduced to numbers.

    Built by the gateway from `gate_query_estimate`; `byte_shaped` mirrors that
    function's structural branch selection (`estimate.estimated_bytes is not
    None`) so the finding names the budget that actually applied.
    """

    supported: bool
    plan_cost: float | None = None
    limit: float | None = None
    over_budget: bool = False
    byte_shaped: bool = False
    kind: str | None = None
    estimated_rows: float | None = None
    estimated_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SqlValidationReport:
    """What `validate_sql` returns, and what `execute` enforces.

    `normalized_sql` is the guard-normalised statement with literals already
    redacted; the executable form never leaves the gateway.
    """

    valid: bool
    findings: tuple[SqlFinding, ...]
    dialect: str
    normalized_sql: str | None
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    applied_row_limit: int | None
    column_lineage: tuple[dict[str, Any], ...]
    plan_cost: float | None = None
    estimate_kind: str | None = None
    estimated_rows: float | None = None
    estimated_bytes: int | None = None

    @property
    def blocking_findings(self) -> tuple[SqlFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)

    def rejection_reason(self) -> str | None:
        """Render the blocking findings as the gateway's rejection message.

        Phase-ordered (guard, then catalog, then estimate) and worded to match
        the messages `QueryExecutionGateway.execute` has always raised, so the
        HTTP 422 body and the persisted `error_message` do not change shape now
        that both come from one place.
        """
        blocking = self.blocking_findings
        if not blocking:
            return None
        guard = [finding for finding in blocking if finding.code in _GUARD_CODES]
        if guard:
            return ", ".join(
                f"{finding.code}:{finding.ref}" if finding.ref else finding.code
                for finding in guard
            )
        catalog = [finding for finding in blocking if finding.code in _CATALOG_CODES]
        if catalog:
            parts: list[str] = []
            tables = [
                finding.ref or ""
                for finding in catalog
                if finding.code == FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE
            ]
            columns = [
                finding.ref or "" for finding in catalog if finding.code == FINDING_UNKNOWN_COLUMN
            ]
            if tables:
                parts.append(f"UNKNOWN_OR_UNAUTHORIZED_TABLES: {', '.join(tables)}")
            if columns:
                parts.append(f"UNKNOWN_COLUMNS: {', '.join(columns)}")
            return "; ".join(parts)
        for finding in blocking:
            if finding.code == FINDING_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR:
                return "QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR"
            if finding.code == FINDING_BYTE_BUDGET_EXCEEDED:
                return (
                    f"QUERY_BYTES_EXCEED_POLICY: "
                    f"{finding.detail.get('plan_cost')} > {finding.detail.get('limit')}"
                )
            if finding.code == FINDING_COST_CEILING_EXCEEDED:
                return (
                    f"QUERY_COST_EXCEEDS_POLICY: "
                    f"{finding.detail.get('plan_cost')} > {finding.detail.get('limit')}"
                )
        return ", ".join(finding.code for finding in blocking)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "dialect": self.dialect,
            "findings": [finding.as_dict() for finding in self.findings],
            "normalized_sql": self.normalized_sql,
            "referenced_tables": list(self.referenced_tables),
            "referenced_columns": list(self.referenced_columns),
            "applied_row_limit": self.applied_row_limit,
            "column_lineage": [dict(item) for item in self.column_lineage],
            "estimate": {
                "plan_cost": self.plan_cost,
                "kind": self.estimate_kind,
                "estimated_rows": self.estimated_rows,
                "estimated_bytes": self.estimated_bytes,
            },
            "governance": {
                "executed": False,
                "value_free": True,
                "literals_redacted": True,
            },
        }


# --- Pure finding builders -------------------------------------------------


def findings_from_guard(result: SqlValidationResult) -> list[SqlFinding]:
    """Translate `SqlGuard` violations into the stable finding vocabulary."""
    findings: list[SqlFinding] = []
    for violation in result.violations:
        code, _, argument = violation.partition(":")
        code = code.strip()
        ref = argument.strip() or None
        if code == FINDING_SQL_PARSE_ERROR:
            # INV-6: the parser message quotes the offending SQL fragment,
            # literal values included. It is deliberately not echoed.
            findings.append(
                SqlFinding(
                    code=FINDING_SQL_PARSE_ERROR,
                    severity=SEVERITY_ERROR,
                    ref=None,
                    hint=(
                        "the statement could not be parsed for this dialect; the "
                        "parser message is withheld because it can echo literal values"
                    ),
                )
            )
            continue
        if code != FINDING_FORBIDDEN_FUNCTION:
            ref = None
        findings.append(
            SqlFinding(
                code=code,
                severity=SEVERITY_ERROR,
                ref=ref,
                hint=_GUARD_HINTS.get(code, "the statement violates a deterministic guard rule"),
            )
        )
    return findings


def row_limit_finding(
    result: SqlValidationResult,
    *,
    requested_limit: int | None,
    default_row_limit: int,
    hard_row_limit: int,
) -> SqlFinding | None:
    """Report the bound the gateway applied, and whether it clamped the request."""
    applied = result.applied_row_limit
    if applied is None:
        return None
    asked_for = requested_limit if requested_limit is not None else default_row_limit
    return SqlFinding(
        code=FINDING_ROW_LIMIT_APPLIED,
        severity=SEVERITY_INFO,
        ref=None,
        hint=(
            "the gateway rewrote the statement with this row limit; results are "
            "always bounded, so an unbounded scan is never executed"
        ),
        detail={
            "applied_row_limit": applied,
            "requested_limit": requested_limit,
            "default_row_limit": default_row_limit,
            "hard_row_limit": hard_row_limit,
            "clamped": applied < asked_for,
        },
    )


@dataclass(frozen=True, slots=True)
class ColumnReference:
    """One column mention, with its qualifier resolved through table aliases."""

    name: str
    qualifier: str | None
    table: str | None


def resolve_column_references(sql: str, *, dialect: str) -> tuple[ColumnReference, ...]:
    """Map every column mention onto the physical table it is qualified with.

    Uses the same alias map as `aida.query_gateway.extract_column_lineage`, so a
    column resolves to the same object in a finding as it does in lineage
    evidence.
    """
    statement = parse_one(sql, read=dialect)
    aliases: dict[str, str] = {}
    for table in statement.find_all(exp.Table):
        canonical = ".".join(part for part in (table.catalog, table.db, table.name) if part).lower()
        aliases[table.alias_or_name.lower()] = canonical
    references: dict[tuple[str | None, str], ColumnReference] = {}
    for column in statement.find_all(exp.Column):
        qualifier = column.table.lower() if column.table else None
        resolved = aliases.get(qualifier) if qualifier else None
        identity = (qualifier, column.name.lower())
        if identity not in references:
            references[identity] = ColumnReference(
                name=column.name.lower(), qualifier=qualifier, table=resolved
            )
    return tuple(references.values())


def locally_defined_names(sql: str, *, dialect: str) -> frozenset[str]:
    """Names a query defines for itself: projection aliases and CTE columns.

    A reference to one of these is not a catalog lookup, so it must never be
    reported as an unknown column.

    Only an explicit `AS` alias counts. `alias_or_name` would also return the
    bare name of an unaliased column projection, which would make every selected
    column "locally defined" and silently disable the catalog check entirely.
    """
    statement = parse_one(sql, read=dialect)
    names: set[str] = set()
    for alias in statement.find_all(exp.Alias):
        output_name = alias.alias_or_name
        if output_name:
            names.add(output_name.lower())
    for cte in statement.find_all(exp.CTE):
        for alias_column in cte.args.get("alias", exp.TableAlias()).args.get("columns") or []:
            names.add(alias_column.name.lower())
    return frozenset(names)


def findings_from_catalog(
    *,
    referenced_tables: Sequence[str],
    allowed_tables: Iterable[str],
) -> list[SqlFinding]:
    """Per-object authorisation: every physical table must resolve in the catalog."""
    allowed = frozenset(allowed_tables)
    return [
        SqlFinding(
            code=FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE,
            severity=SEVERITY_ERROR,
            ref=table,
            hint=(
                "this object is not an active table in the catalog binding for this "
                "datasource, or the caller's organization is not bound to it"
            ),
        )
        for table in sorted(referenced_tables)
        if table.lower() not in allowed
    ]


def findings_from_columns(
    references: Sequence[ColumnReference],
    *,
    catalog_columns: Mapping[str, frozenset[str]],
    local_names: frozenset[str],
) -> list[SqlFinding]:
    """Resolve every column mention against the catalog for its table.

    Conservative by construction, because a false `UNKNOWN_COLUMN` would refuse
    a legitimate query:

    * a name the query defines for itself (projection alias, CTE column) is
      never checked;
    * a qualified column is checked against its own table only when that table
      is in the catalog map;
    * an unqualified column is checked against the union of the columns of the
      referenced tables, which is exactly SQL's own resolution rule;
    * when the catalog knows nothing about any referenced table, no column
      finding is produced at all -- the table findings already say so.
    """
    if not catalog_columns:
        return []
    union: set[str] = set()
    for columns in catalog_columns.values():
        union.update(columns)
    findings: list[SqlFinding] = []
    for reference in references:
        if reference.name in local_names:
            continue
        known = catalog_columns.get(reference.table) if reference.table else None
        if known is not None:
            if reference.name in known:
                continue
            ref = f"{reference.table}.{reference.name}"
        else:
            if reference.name in union:
                continue
            ref = (
                f"{reference.qualifier}.{reference.name}" if reference.qualifier else reference.name
            )
        findings.append(
            SqlFinding(
                code=FINDING_UNKNOWN_COLUMN,
                severity=SEVERITY_ERROR,
                ref=ref,
                hint=(
                    "no active column with this name exists on the referenced table "
                    "in the catalog; check for a rename or a stale column list"
                ),
            )
        )
    return findings


def findings_from_estimate(outcome: EstimateOutcome) -> list[SqlFinding]:
    """Turn the dry-run gate decision into findings, without echoing any value."""
    if not outcome.supported:
        return [
            SqlFinding(
                code=FINDING_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR,
                severity=SEVERITY_ERROR,
                ref=None,
                hint=(
                    "this connector does not advertise EXPLAIN, so the cost of the "
                    "statement cannot be bounded before execution; the gateway fails "
                    "closed rather than running an uncosted query"
                ),
            )
        ]
    if not outcome.over_budget:
        return []
    code = (
        FINDING_BYTE_BUDGET_EXCEEDED if outcome.byte_shaped else FINDING_COST_CEILING_EXCEEDED
    )
    hint = (
        "the dry run scans more bytes than the byte budget allows; narrow the "
        "predicate, prune partitions, or project fewer columns"
        if outcome.byte_shaped
        else "the planner's cost estimate exceeds the policy ceiling; add a more "
        "selective predicate or reduce the join fan-out"
    )
    return [
        SqlFinding(
            code=code,
            severity=SEVERITY_ERROR,
            ref=None,
            hint=hint,
            detail={"plan_cost": outcome.plan_cost, "limit": outcome.limit},
        )
    ]


def build_report(
    *,
    dialect: str,
    guard_result: SqlValidationResult,
    findings: Sequence[SqlFinding],
    redacted_sql: str | None,
    column_lineage: Sequence[dict[str, Any]],
    estimate: EstimateOutcome | None,
) -> SqlValidationReport:
    """Assemble the report. `valid` is false iff a blocking finding is present."""
    ordered = tuple(findings)
    return SqlValidationReport(
        valid=not any(finding.blocking for finding in ordered),
        findings=ordered,
        dialect=dialect,
        normalized_sql=redacted_sql,
        referenced_tables=guard_result.referenced_tables,
        referenced_columns=guard_result.referenced_columns,
        applied_row_limit=guard_result.applied_row_limit,
        column_lineage=tuple(column_lineage),
        plan_cost=estimate.plan_cost if estimate is not None else None,
        estimate_kind=estimate.kind if estimate is not None else None,
        estimated_rows=estimate.estimated_rows if estimate is not None else None,
        estimated_bytes=estimate.estimated_bytes if estimate is not None else None,
    )
