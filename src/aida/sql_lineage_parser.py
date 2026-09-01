"""SQL-based view and procedure lineage extraction.

Parses SQL view definitions and stored procedure bodies using sqlglot to
extract column-level lineage edges.  Definitions are NEVER executed -- this
is parse-only analysis.  Literal values in SQL are REDACTED (replaced by
placeholders) so no source data leaks into lineage metadata.

Supported dialects: postgres, snowflake, bigquery, tsql (SQL Server), oracle.
Graceful degradation: if a parse fails the module returns an empty edge list
with LOW confidence rather than raising.

Two facts about a source column are orthogonal and never override one
another: what a column's own SELECT-list expression is (a plain pass-through,
a derived expression, or an aggregate -- `DIRECT`/`DERIVED`/`AGGREGATED`) and
whether that column also happens to appear in the statement's WHERE clause.
A column that is filtered on but never selected is not silently dropped
either -- it gets its own `FILTERED` evidence edge, targeting the reserved
`FILTER_EVIDENCE_TARGET_COLUMN` marker rather than a real output column.

A `SELECT *` (or `alias.*`) projection cannot be resolved to individual
output columns without the source table's real column list -- this module is
deliberately catalog- and database-free (see `AT-D2`) -- so it is recorded as
honest table-level evidence (`TABLE_STAR`) rather than silently discarded.

A source table reference sqlglot could not resolve to a name (an unqualified
column with no single table in scope, an alias sqlglot did not bind, ...) is
never represented by a bare string that could collide with a real table
name.  `LineageEdge.source_resolved` is the authoritative, type-level signal
-- callers must check it, never string-compare `source_table` against
`UNRESOLVED_TABLE`, which is a cosmetic label only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal

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
    # A column referenced only in a WHERE predicate -- never in the SELECT
    # list -- so there is no real output column to attribute it to (see
    # `FILTER_EVIDENCE_TARGET_COLUMN`). Not used to override a SELECT-list
    # column's own classification anymore; see the module docstring.
    FILTERED = "FILTERED"
    # Table-level evidence for a `SELECT *` / `alias.*` projection: the view
    # or procedure depends on every column of the named source table, but
    # individual columns could not be resolved (see `parse_view_lineage`).
    TABLE_STAR = "TABLE_STAR"


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

# Cosmetic label for an edge whose `source_table` sqlglot could not resolve
# to a name. Never compare `source_table` against this to detect
# unresolved edges -- a real table could coincidentally share the name.
# `LineageEdge.source_resolved` is the real, type-level signal.
UNRESOLVED_TABLE: Final[str] = "UNRESOLVED"

# Reserved `target_table` for a standalone SELECT (procedure-body analysis
# with no INSERT/CREATE VIEW target) -- not customer data, so no collision
# risk with a real table name.
PROCEDURE_RESULT_TARGET: Final[str] = "<RESULT>"

# Reserved `target_column` for a `FILTERED` (filter-only) evidence edge --
# there is no real output column since the source column was never selected.
FILTER_EVIDENCE_TARGET_COLUMN: Final[str] = "<FILTER_PREDICATE>"

# `source_column` / `target_column` for a `TABLE_STAR` table-level edge --
# the literal star notation, which can never collide with a real column name.
STAR_COLUMN_MARKER: Final[str] = "*"


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
    # Real, typed signal for whether `source_table` is an actual resolved
    # name or just carries the cosmetic `UNRESOLVED_TABLE` label. Defaults to
    # True so every existing call site that builds an edge for a table it
    # positively resolved does not need to change.
    source_resolved: bool = True


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


def _classify_transformation(column_expr: object, has_aggregation: bool) -> str:
    """Classify a SELECT-list column's own transformation type.

    `has_aggregation` must be evaluated on this specific column's own
    expression, never on the statement as a whole -- otherwise one aggregated
    column in a SELECT list marks every sibling column AGGREGATED too. WHERE
    clause presence never factors in here: whether a column is filtered on is
    an orthogonal fact, recorded separately (see `_extract_filter_only_edges`)
    rather than overriding what this column's SELECT-list expression actually
    is.
    """
    if has_aggregation:
        return TransformationType.AGGREGATED.value
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


def _immediate_source_tables(select_stmt: object) -> list[str]:
    """Resolve the table(s) named in a Select's own FROM/JOIN clauses.

    Deliberately scoped to this select's immediate FROM and JOIN nodes (not a
    recursive `find_all` over the whole subtree, which would also sweep up
    tables from nested subqueries several levels down) -- used to attribute
    an unqualified `SELECT *` to the table(s) actually in scope for it.
    """
    if not _SQLGLOT_AVAILABLE or not isinstance(select_stmt, exp.Select):
        return []
    tables: list[str] = []
    seen: set[str] = set()
    nodes: list[exp.Expression] = []
    # sqlglot's Select stores its FROM clause under the arg key "from_" (the
    # trailing underscore avoids shadowing the `from` keyword) -- "from"
    # is never a key here.
    from_clause = select_stmt.args.get("from_")
    if isinstance(from_clause, exp.Expression):
        nodes.append(from_clause)
    for join in select_stmt.args.get("joins") or []:
        if isinstance(join, exp.Expression):
            nodes.append(join)
    for node in nodes:
        for table in node.find_all(exp.Table):
            fqn = _resolve_table_name(table)
            if fqn and fqn not in seen:
                seen.add(fqn)
                tables.append(fqn)
    return tables


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


def _resolve_or_mark_unresolved(table_ref: str, merged_aliases: dict[str, str]) -> tuple[str, bool]:
    """Resolve a raw table reference; report whether resolution succeeded.

    Returns the raw resolved key (which may be `""`, used as the dedupe key
    shared with `_extract_filter_only_edges`) alongside the boolean that
    tells the caller whether it is a real name or should be displayed as
    `UNRESOLVED_TABLE`.
    """
    resolved = _resolve_alias_to_table(table_ref, merged_aliases)
    return resolved, bool(resolved)


def _extract_star_edges(
    star_table_alias: str | None,
    target_table: str,
    dialect: str,
    merged_aliases: dict[str, str],
    cte_aliases: dict[str, str],
    table_aliases: dict[str, str],
    inner_select: object,
) -> list[LineageEdge]:
    """Emit honest table-level evidence for a `SELECT *` / `alias.*` projection.

    Individual output columns cannot be resolved without the source table's
    real column list, which this deliberately catalog- and database-free
    module does not have -- so this records one edge per source table the
    star expands over (all tables in scope for an unqualified `*`, or just
    the one the alias names for `alias.*`) rather than the previous bare
    `continue`, which made a star view indistinguishable from a view with no
    upstreams at all.
    """
    if star_table_alias:
        candidates = [_resolve_alias_to_table(star_table_alias, merged_aliases)]
    else:
        candidates = _immediate_source_tables(inner_select)

    resolved_tables = [
        table
        for table in candidates
        if table and not (table in cte_aliases and table not in table_aliases)
    ]

    if not resolved_tables:
        return [
            LineageEdge(
                source_table=UNRESOLVED_TABLE,
                source_column=STAR_COLUMN_MARKER,
                target_table=target_table,
                target_column=STAR_COLUMN_MARKER,
                transformation_type=TransformationType.TABLE_STAR.value,
                confidence=Confidence.LOW.value,
                dialect=dialect,
                source_resolved=False,
            )
        ]

    return [
        LineageEdge(
            source_table=table,
            source_column=STAR_COLUMN_MARKER,
            target_table=target_table,
            target_column=STAR_COLUMN_MARKER,
            transformation_type=TransformationType.TABLE_STAR.value,
            confidence=Confidence.PARTIAL.value,
            dialect=dialect,
            source_resolved=True,
        )
        for table in resolved_tables
    ]


def _extract_filter_only_edges(
    inner_select: object,
    target_table: str,
    dialect: str,
    merged_aliases: dict[str, str],
    cte_aliases: dict[str, str],
    table_aliases: dict[str, str],
    already_sourced: set[tuple[str, str]],
) -> list[LineageEdge]:
    """Emit evidence edges for columns referenced only in this select's own
    WHERE clause -- never in its SELECT list.

    Only this select's own WHERE is examined (`.args["where"]`, a direct
    attribute, not a recursive `find`), so a WHERE in a sibling UNION branch
    or an unrelated nested subquery can never leak filter evidence onto this
    select's own columns -- the "assigned per-statement" bug AT-D2 names.
    `already_sourced` (raw resolve keys, pre-`UNRESOLVED_TABLE` substitution)
    excludes any column that already has a real SELECT-list edge -- a column
    that is both selected and filtered on keeps its SELECT-list
    classification and does not also get a redundant FILTERED edge.
    """
    if not _SQLGLOT_AVAILABLE or not isinstance(inner_select, exp.Select):
        return []
    where_node = inner_select.args.get("where")
    if where_node is None:
        return []

    edges: list[LineageEdge] = []
    seen: set[tuple[str, str]] = set()
    for table_ref, col_name in _extract_source_columns(where_node):
        resolved_table, source_resolved = _resolve_or_mark_unresolved(table_ref, merged_aliases)
        if resolved_table in cte_aliases and resolved_table not in table_aliases:
            continue
        key = (resolved_table, col_name)
        if key in already_sourced or key in seen:
            continue
        seen.add(key)
        edges.append(
            LineageEdge(
                source_table=resolved_table if source_resolved else UNRESOLVED_TABLE,
                source_column=col_name,
                target_table=target_table,
                target_column=FILTER_EVIDENCE_TARGET_COLUMN,
                transformation_type=TransformationType.FILTERED.value,
                confidence=Confidence.PARTIAL.value,
                dialect=dialect,
                source_resolved=source_resolved,
            )
        )
    return edges


def _extract_edges_from_select(
    select_stmt: object,
    target_table: str,
    dialect: str,
    table_aliases: dict[str, str],
) -> list[LineageEdge]:
    """Extract lineage edges from a SELECT statement."""
    if not _SQLGLOT_AVAILABLE or not isinstance(select_stmt, exp.Expression):
        return []

    edges: list[LineageEdge] = []

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

    # Handle UNION queries -- each branch resolves its own WHERE/aggregation
    # independently; nothing is inherited from the union as a whole.
    if isinstance(select_stmt, exp.Union):
        for branch in [select_stmt.left, select_stmt.right]:
            edges.extend(
                _extract_edges_from_select(branch, target_table, dialect, merged_aliases)
            )
        return edges

    # Find the innermost Select (skip over Subquery wrappers)
    inner_select = select_stmt
    if isinstance(inner_select, exp.Subquery):
        inner_select = inner_select.this

    if not isinstance(inner_select, exp.Select):
        return edges

    # (resolved_table, column) pairs that already have a real SELECT-list
    # edge, so filter-only evidence below does not duplicate them.
    select_list_refs: set[tuple[str, str]] = set()

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

        # Star expansion: bare `SELECT *` (source_expr is the Star itself) or
        # a qualified `alias.*` (source_expr is a Column wrapping a Star).
        if isinstance(source_expr, exp.Star):
            edges.extend(
                _extract_star_edges(
                    None, target_table, dialect, merged_aliases, cte_aliases,
                    table_aliases, inner_select,
                )
            )
            continue
        if isinstance(source_expr, exp.Column) and isinstance(source_expr.this, exp.Star):
            edges.extend(
                _extract_star_edges(
                    source_expr.table or None, target_table, dialect, merged_aliases,
                    cte_aliases, table_aliases, inner_select,
                )
            )
            continue

        source_refs = _extract_source_columns(source_expr)
        # Evaluated on this column's own expression, never the whole
        # statement -- one aggregated sibling must not mark every column
        # AGGREGATED (the "assigned per-statement" bug AT-D2 names).
        has_agg = _has_aggregate_functions(source_expr)
        transformation = _classify_transformation(source_expr, has_agg)

        for table_ref, col_name in source_refs:
            resolved_table, source_resolved = _resolve_or_mark_unresolved(
                table_ref, merged_aliases
            )
            # Skip CTE self-references (they will be resolved through their own edges)
            if resolved_table in cte_aliases and resolved_table not in table_aliases:
                continue
            select_list_refs.add((resolved_table, col_name))

            edges.append(
                LineageEdge(
                    source_table=resolved_table if source_resolved else UNRESOLVED_TABLE,
                    source_column=col_name,
                    target_table=target_table,
                    target_column=target_col,
                    transformation_type=transformation,
                    confidence=(
                        Confidence.FULL.value if source_resolved else Confidence.PARTIAL.value
                    ),
                    dialect=dialect,
                    source_resolved=source_resolved,
                )
            )

    edges.extend(
        _extract_filter_only_edges(
            inner_select, target_table, dialect, merged_aliases, cte_aliases,
            table_aliases, select_list_refs,
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

    # Determine overall confidence. Rolled up from each edge's own confidence
    # (never hard-coded FULL, AT-D2) -- a view or procedure that resolved
    # every reference cleanly is FULL; one that leans on any unresolved
    # reference, filter-only evidence, or a `SELECT *` table-level fallback
    # is honestly PARTIAL, whichever entry point produced it.
    if errors and not all_edges:
        confidence = Confidence.LOW.value
    elif errors:
        confidence = Confidence.PARTIAL.value
    elif all_edges:
        if all(edge.confidence == Confidence.FULL.value for edge in all_edges):
            confidence = Confidence.FULL.value
        else:
            confidence = Confidence.PARTIAL.value
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

    # CREATE VIEW AS SELECT / CREATE TABLE AS SELECT
    if isinstance(statement, exp.Create):
        target_table = _extract_target_table(statement)
        if not target_table:
            return []
        # Union must be checked first: `find` is a preorder search, and a
        # UNION's own leaf Select branches would otherwise match `find
        # (exp.Select)` before the Union wrapping them is ever considered --
        # silently truncating the query to just its first branch and
        # dropping every other branch's edges (the same "per-statement"
        # under-scoping shape as AT-D2's FILTERED/AGGREGATED defect).
        inner_select: exp.Select | exp.Union | None = statement.find(exp.Union)
        if inner_select is None:
            inner_select = statement.find(exp.Select)
        if inner_select is None:
            return []
        return _extract_edges_from_select(inner_select, target_table, dialect, table_aliases)

    # INSERT INTO ... SELECT
    if isinstance(statement, exp.Insert):
        target_table = _extract_target_table(statement)
        if not target_table:
            return []
        inner_select = statement.find(exp.Union) or statement.find(exp.Select)
        if inner_select is None:
            return []
        return _extract_edges_from_select(inner_select, target_table, dialect, table_aliases)

    # MERGE statements
    if isinstance(statement, exp.Merge):
        target = statement.this
        target_table = _resolve_table_name(target) if isinstance(target, exp.Table) else ""
        if not target_table:
            return []
        edges: list[LineageEdge] = []
        for select in statement.find_all(exp.Select):
            edges.extend(_extract_edges_from_select(select, target_table, dialect, table_aliases))
        return edges

    # Standalone SELECT (for procedure analysis)
    if isinstance(statement, exp.Select | exp.Union):
        return _extract_edges_from_select(
            statement, PROCEDURE_RESULT_TARGET, dialect, table_aliases
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
