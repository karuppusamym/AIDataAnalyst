"""SQL-based view and procedure lineage extraction.

Parses SQL view definitions and stored procedure bodies using sqlglot to
extract column-level lineage edges.  Definitions are NEVER executed -- this
is parse-only analysis.  Literal values in SQL are REDACTED (replaced by
placeholders) so no source data leaks into lineage metadata.

Supported dialects: postgres, snowflake, bigquery, tsql (SQL Server), oracle.
Graceful degradation: if a parse fails the module returns an empty edge list
with LOW confidence rather than raising.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel

    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False


class TransformationType(str, Enum):
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    AGGREGATED = "AGGREGATED"
    FILTERED = "FILTERED"


class Confidence(str, Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    LOW = "LOW"


DialectName = Literal["postgres", "snowflake", "bigquery", "tsql", "oracle"]

_SQLGLOT_DIALECT_MAP: dict[str, str] = {
    "postgres": "postgres",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "tsql": "tsql",
    "oracle": "oracle",
}


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """One column-level lineage edge extracted from SQL."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    transformation_type: str
    confidence: str
    dialect: str


@dataclass(slots=True)
class ParseResult:
    """Result of a lineage parse operation."""

    edges: list[LineageEdge] = field(default_factory=list)
    confidence: str = Confidence.LOW.value
    dialect: str = ""
    sql_hash: str = ""
    errors: list[str] = field(default_factory=list)


def _redact_literals(sql: str) -> str:
    """Replace string and numeric literals with placeholders.

    This ensures no actual data values from SQL definitions are persisted in
    lineage metadata.
    """
    redacted = re.sub(r"'[^']*'", "'<REDACTED>'", sql)
    redacted = re.sub(r"\b\d+\.?\d*\b", "<NUM>", redacted)
    return redacted


def _compute_sql_hash(sql: str) -> str:
    """SHA-256 hash of the redacted SQL definition."""
    return hashlib.sha256(_redact_literals(sql).encode("utf-8")).hexdigest()


def _resolve_table_name(table_expr: object) -> str:
    """Extract the fully qualified table name from a sqlglot Table expression."""
    if not _SQLGLOT_AVAILABLE:
        return ""
    if not isinstance(table_expr, exp.Table):
        return ""
    parts: list[str] = []
    if table_expr.catalog:
        parts.append(table_expr.catalog)
    if table_expr.db:
        parts.append(table_expr.db)
    if table_expr.name:
        parts.append(table_expr.name)
    return ".".join(parts) if parts else ""


def _classify_transformation(
    column_expr: object,
    has_aggregation: bool,
    has_filter: bool,
) -> str:
    """Classify the transformation type of a column expression."""
    if has_aggregation:
        return TransformationType.AGGREGATED.value
    if has_filter:
        return TransformationType.FILTERED.value
    if not _SQLGLOT_AVAILABLE:
        return TransformationType.DERIVED.value
    if isinstance(column_expr, exp.Column):
        return TransformationType.DIRECT.value
    return TransformationType.DERIVED.value


def _extract_source_columns(
    expression: object,
) -> list[tuple[str, str]]:
    """Extract (table, column) pairs referenced in an expression.

    Walks the AST to find all Column nodes and resolves their table references.
    """
    if not _SQLGLOT_AVAILABLE:
        return []
    results: list[tuple[str, str]] = []
    if not isinstance(expression, exp.Expression):
        return results
    for column in expression.find_all(exp.Column):
        table_name = ""
        if column.table:
            table_name = column.table
        col_name = column.name
        if col_name:
            results.append((table_name, col_name))
    return results


def _resolve_alias_to_table(
    alias: str,
    table_aliases: dict[str, str],
) -> str:
    """Resolve a table alias to its fully qualified name."""
    return table_aliases.get(alias, alias)


def _collect_table_aliases(statement: object) -> dict[str, str]:
    """Collect table alias -> fully qualified name mappings from a statement."""
    aliases: dict[str, str] = {}
    if not _SQLGLOT_AVAILABLE or not isinstance(statement, exp.Expression):
        return aliases
    for table in statement.find_all(exp.Table):
        fqn = _resolve_table_name(table)
        if fqn:
            if table.alias:
                aliases[table.alias] = fqn
            aliases[fqn] = fqn
            # Also register the short name for unqualified references
            if table.name:
                aliases[table.name] = fqn
    return aliases


def _has_aggregate_functions(expression: object) -> bool:
    """Check whether an expression contains aggregate functions."""
    if not _SQLGLOT_AVAILABLE or not isinstance(expression, exp.Expression):
        return False
    agg_types = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
    return any(True for _ in expression.find_all(*agg_types))


def _has_where_clause(statement: object) -> bool:
    """Check whether a statement has a WHERE clause."""
    if not _SQLGLOT_AVAILABLE or not isinstance(statement, exp.Expression):
        return False
    return statement.find(exp.Where) is not None


def _extract_target_table(statement: object) -> str:
    """Extract the target table for CREATE VIEW or INSERT INTO statements."""
    if not _SQLGLOT_AVAILABLE or not isinstance(statement, exp.Expression):
        return ""

    if isinstance(statement, exp.Create):
        this = statement.this
        if isinstance(this, exp.Table):
            return _resolve_table_name(this)
        # CREATE VIEW creates a Schema expression wrapping the table
        if isinstance(this, exp.Schema):
            table = this.this
            if isinstance(table, exp.Table):
                return _resolve_table_name(table)

    if isinstance(statement, exp.Insert):
        table = statement.this
        if isinstance(table, exp.Table):
            return _resolve_table_name(table)

    return ""


def _extract_edges_from_select(
    select_stmt: object,
    target_table: str,
    dialect: str,
    table_aliases: dict[str, str],
    parent_has_where: bool,
) -> list[LineageEdge]:
    """Extract lineage edges from a SELECT statement."""
    if not _SQLGLOT_AVAILABLE or not isinstance(select_stmt, exp.Expression):
        return []

    edges: list[LineageEdge] = []
    has_agg = _has_aggregate_functions(select_stmt)
    has_filter = parent_has_where or _has_where_clause(select_stmt)

    # Collect CTE aliases so CTE references resolve correctly
    cte_aliases: dict[str, str] = {}
    for cte_node in select_stmt.find_all(exp.CTE):
        cte_name = cte_node.alias
        if cte_name:
            cte_aliases[cte_name] = cte_name

    # Merge CTE aliases into table_aliases for resolution
    merged_aliases = {**table_aliases}
    merged_aliases.update(cte_aliases)

    # Also collect aliases from subqueries and CTEs in this select
    for table in select_stmt.find_all(exp.Table):
        fqn = _resolve_table_name(table)
        if fqn:
            if table.alias:
                merged_aliases[table.alias] = fqn
            merged_aliases[fqn] = fqn
            if table.name:
                merged_aliases[table.name] = fqn

    # Handle UNION queries
    if isinstance(select_stmt, exp.Union):
        for branch in [select_stmt.left, select_stmt.right]:
            edges.extend(
                _extract_edges_from_select(
                    branch, target_table, dialect, merged_aliases, has_filter
                )
            )
        return edges

    # Find the innermost Select (skip over Subquery wrappers)
    inner_select = select_stmt
    if isinstance(inner_select, exp.Subquery):
        inner_select = inner_select.this

    if not isinstance(inner_select, exp.Select):
        return edges

    # Process each output column
    for i, select_expr in enumerate(inner_select.expressions):
        target_col = ""
        if isinstance(select_expr, exp.Alias):
            target_col = select_expr.alias
            source_expr = select_expr.this
        elif isinstance(select_expr, exp.Column):
            target_col = select_expr.name
            source_expr = select_expr
        else:
            target_col = f"_col{i}"
            source_expr = select_expr

        # Star expansion - we cannot resolve individual columns
        if isinstance(source_expr, exp.Star):
            continue

        source_refs = _extract_source_columns(source_expr)
        transformation = _classify_transformation(source_expr, has_agg, has_filter)

        for table_ref, col_name in source_refs:
            resolved_table = _resolve_alias_to_table(table_ref, merged_aliases)
            # Skip CTE self-references (they will be resolved through their own edges)
            if resolved_table in cte_aliases and resolved_table not in table_aliases:
                continue
            if not resolved_table:
                resolved_table = "<UNKNOWN>"

            edges.append(
                LineageEdge(
                    source_table=resolved_table,
                    source_column=col_name,
                    target_table=target_table,
                    target_column=target_col,
                    transformation_type=transformation,
                    confidence=Confidence.FULL.value,
                    dialect=dialect,
                )
            )

    return edges


def _parse_sql(sql: str, dialect: str) -> ParseResult:
    """Core parser: parse SQL and extract lineage edges."""
    if not _SQLGLOT_AVAILABLE:
        return ParseResult(
            confidence=Confidence.LOW.value,
            dialect=dialect,
            sql_hash=_compute_sql_hash(sql),
            errors=["sqlglot library is not available"],
        )

    sqlglot_dialect = _SQLGLOT_DIALECT_MAP.get(dialect, dialect)
    sql_hash = _compute_sql_hash(sql)

    try:
        statements = sqlglot.parse(sql, dialect=sqlglot_dialect, error_level=ErrorLevel.WARN)
    except Exception as exc:
        return ParseResult(
            confidence=Confidence.LOW.value,
            dialect=dialect,
            sql_hash=sql_hash,
            errors=[f"parse error: {exc!s}"],
        )

    if not statements:
        return ParseResult(
            confidence=Confidence.LOW.value,
            dialect=dialect,
            sql_hash=sql_hash,
            errors=["no SQL statements found"],
        )

    all_edges: list[LineageEdge] = []
    errors: list[str] = []

    for statement in statements:
        if statement is None:
            continue
        try:
            edges = _extract_from_statement(statement, dialect)
            all_edges.extend(edges)
        except Exception as exc:
            errors.append(f"extraction error: {exc!s}")

    # Determine overall confidence
    if errors and not all_edges:
        confidence = Confidence.LOW.value
    elif errors:
        confidence = Confidence.PARTIAL.value
    elif all_edges:
        confidence = Confidence.FULL.value
    else:
        confidence = Confidence.PARTIAL.value

    return ParseResult(
        edges=all_edges,
        confidence=confidence,
        dialect=dialect,
        sql_hash=sql_hash,
        errors=errors,
    )


def _extract_from_statement(
    statement: object,
    dialect: str,
) -> list[LineageEdge]:
    """Extract lineage edges from a single parsed statement."""
    if not _SQLGLOT_AVAILABLE or not isinstance(statement, exp.Expression):
        return []

    table_aliases = _collect_table_aliases(statement)
    has_filter = _has_where_clause(statement)

    # CREATE VIEW AS SELECT / CREATE TABLE AS SELECT
    if isinstance(statement, exp.Create):
        target_table = _extract_target_table(statement)
        if not target_table:
            return []
        inner_select = statement.find(exp.Select)
        if inner_select is None:
            # Might be a UNION
            inner_select = statement.find(exp.Union)
        if inner_select is None:
            return []
        return _extract_edges_from_select(
            inner_select, target_table, dialect, table_aliases, has_filter
        )

    # INSERT INTO ... SELECT
    if isinstance(statement, exp.Insert):
        target_table = _extract_target_table(statement)
        if not target_table:
            return []
        inner_select = statement.find(exp.Select)
        if inner_select is None:
            inner_select = statement.find(exp.Union)
        if inner_select is None:
            return []
        return _extract_edges_from_select(
            inner_select, target_table, dialect, table_aliases, has_filter
        )

    # MERGE statements
    if isinstance(statement, exp.Merge):
        target = statement.this
        target_table = _resolve_table_name(target) if isinstance(target, exp.Table) else ""
        if not target_table:
            return []
        edges: list[LineageEdge] = []
        for select in statement.find_all(exp.Select):
            edges.extend(
                _extract_edges_from_select(
                    select, target_table, dialect, table_aliases, has_filter
                )
            )
        return edges

    # Standalone SELECT (for procedure analysis)
    if isinstance(statement, (exp.Select, exp.Union)):
        return _extract_edges_from_select(
            statement, "<RESULT>", dialect, table_aliases, has_filter
        )

    return []


def parse_view_lineage(sql: str, dialect: str = "postgres") -> ParseResult:
    """Parse a view definition (CREATE VIEW AS ...) and extract column-level lineage.

    The SQL is never executed.  Literal values are redacted from hashes.

    Args:
        sql: The SQL view definition (e.g. CREATE VIEW v AS SELECT ...)
        dialect: Target SQL dialect (postgres, snowflake, bigquery, tsql, oracle)

    Returns:
        ParseResult with extracted edges and confidence level.
    """
    if dialect not in _SQLGLOT_DIALECT_MAP:
        return ParseResult(
            confidence=Confidence.LOW.value,
            dialect=dialect,
            sql_hash=_compute_sql_hash(sql),
            errors=[f"unsupported dialect: {dialect}"],
        )
    return _parse_sql(sql, dialect)


def parse_procedure_lineage(sql: str, dialect: str = "postgres") -> ParseResult:
    """Parse a stored procedure body and extract column-level lineage.

    Extracts lineage from DML statements within the procedure body.
    The SQL is never executed.  Literal values are redacted from hashes.

    For procedures, the parser extracts lineage from individual DML statements
    (SELECT, INSERT, UPDATE, MERGE) found within the body.

    Args:
        sql: The SQL procedure body
        dialect: Target SQL dialect

    Returns:
        ParseResult with extracted edges and confidence level.
    """
    if dialect not in _SQLGLOT_DIALECT_MAP:
        return ParseResult(
            confidence=Confidence.LOW.value,
            dialect=dialect,
            sql_hash=_compute_sql_hash(sql),
            errors=[f"unsupported dialect: {dialect}"],
        )
    return _parse_sql(sql, dialect)
