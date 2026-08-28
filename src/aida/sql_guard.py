from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError


@dataclass(frozen=True, slots=True)
class SqlValidationResult:
    valid: bool
    normalized_sql: str | None
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    violations: tuple[str, ...]
    applied_row_limit: int | None


class SqlGuard:
    _forbidden_functions = {
        "dblink",
        "dblink_exec",
        "lo_export",
        "lo_import",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_sleep",
        "pg_terminate_backend",
    }

    def __init__(self, *, default_row_limit: int, hard_row_limit: int) -> None:
        self.default_row_limit = default_row_limit
        self.hard_row_limit = hard_row_limit

    def validate(
        self, sql: str, *, dialect: str, requested_limit: int | None = None
    ) -> SqlValidationResult:
        violations: list[str] = []
        try:
            statements = [
                statement for statement in parse(sql, read=dialect) if statement is not None
            ]
        except (ParseError, ValueError) as exc:
            return SqlValidationResult(
                valid=False,
                normalized_sql=None,
                referenced_tables=(),
                referenced_columns=(),
                violations=(f"SQL_PARSE_ERROR: {exc}",),
                applied_row_limit=None,
            )

        if len(statements) != 1:
            violations.append("EXACTLY_ONE_STATEMENT_REQUIRED")
        if not statements:
            return SqlValidationResult(False, None, (), (), tuple(violations), None)

        statement = statements[0]
        if not isinstance(statement, exp.Query):
            violations.append("READ_ONLY_QUERY_REQUIRED")

        forbidden_nodes = (
            exp.Alter,
            exp.Command,
            exp.Create,
            exp.Delete,
            exp.Drop,
            exp.Insert,
            exp.Merge,
            exp.Transaction,
            exp.TruncateTable,
            exp.Update,
        )
        if any(statement.find(node_type) is not None for node_type in forbidden_nodes):
            violations.append("MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN")
        if statement.find(exp.Into) is not None:
            violations.append("SELECT_INTO_FORBIDDEN")

        for join in statement.find_all(exp.Join):
            kind = str(join.args.get("kind") or "").upper()
            has_condition = join.args.get("on") is not None or join.args.get("using") is not None
            if kind == "CROSS" or not has_condition:
                violations.append("CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN")
                break

        for function in statement.find_all(exp.Func):
            function_name = function.name or function.sql_name()
            if function_name.lower() in self._forbidden_functions:
                violations.append(f"FORBIDDEN_FUNCTION:{function_name.lower()}")

        for star in statement.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                violations.append("SELECT_WILDCARD_FORBIDDEN")
                break

        cte_aliases = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        tables = sorted(
            {
                ".".join(part for part in (table.catalog, table.db, table.name) if part)
                for table in statement.find_all(exp.Table)
                if not (not table.catalog and not table.db and table.name.lower() in cte_aliases)
            }
        )
        columns = sorted({column.sql(dialect=dialect) for column in statement.find_all(exp.Column)})

        applied_limit: int | None = None
        if isinstance(statement, exp.Query):
            target_limit = min(requested_limit or self.default_row_limit, self.hard_row_limit)
            limit_node = statement.args.get("limit")
            existing_limit: int | None = None
            if isinstance(limit_node, exp.Limit):
                expression = limit_node.expression
                if isinstance(expression, exp.Literal) and not expression.is_string:
                    existing_limit = int(expression.this)
            applied_limit = min(existing_limit, target_limit) if existing_limit else target_limit
            statement.limit(applied_limit, copy=False)

        unique_violations = tuple(dict.fromkeys(violations))
        return SqlValidationResult(
            valid=not unique_violations,
            normalized_sql=statement.sql(dialect=dialect, pretty=True),
            referenced_tables=tuple(tables),
            referenced_columns=tuple(columns),
            violations=unique_violations,
            applied_row_limit=applied_limit,
        )
