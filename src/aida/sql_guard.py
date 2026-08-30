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


def _flatten_dot_chain(node: exp.Expr) -> list[str]:
    """Collect the left-to-right identifier names of a dotted reference chain.

    ``SYS.DBMS_LOCK.SLEEP(5)`` parses as nested ``exp.Dot`` nodes with the
    package/schema qualifiers on the left and the call itself buried on the
    right. This walks that shape and returns ``["sys", "dbms_lock"]`` --
    everything that qualifies the call, in order, lower-cased -- so a
    package-prefix rule (QG-1) can be applied regardless of how many
    qualifiers are present. Non-identifier segments (the call itself) simply
    contribute nothing, which is what makes this safe to run on the whole
    chain rather than needing to know where the call starts.
    """
    if isinstance(node, exp.Dot):
        return _flatten_dot_chain(node.this) + _flatten_dot_chain(node.expression)
    if isinstance(node, exp.Identifier | exp.Column):
        return [node.name.lower()]
    return []


def _package_qualifiers(function: exp.Func) -> list[str]:
    """The dotted qualifiers leading to a function call, e.g. ``["utl_http"]``."""
    node: exp.Expr = function
    while isinstance(node.parent, exp.Dot):
        node = node.parent
    return [name for name in _flatten_dot_chain(node) if name]


class SqlGuard:
    #: Dangerous functions blocked regardless of dialect -- none currently, kept
    #: for symmetry with the per-dialect maps below.
    _forbidden_functions_common: frozenset[str] = frozenset()

    #: Per-dialect adversarial function denylist (QG-1). Each entry reaches
    #: outside the query engine -- the network, the filesystem, the OS, a
    #: linked/remote server, or the running session/warehouse itself -- so none
    #: of them is a legitimate way to read a governed table, and every one of
    #: them is a documented bypass technique for a read-only SQL gateway.
    _forbidden_functions_by_dialect: dict[str, frozenset[str]] = {
        "postgres": frozenset(
            {
                "dblink",
                "dblink_connect",
                "dblink_connect_u",
                "dblink_disconnect",
                "dblink_exec",
                "dblink_open",
                "dblink_fetch",
                "dblink_close",
                "dblink_send_query",
                "dblink_is_busy",
                "dblink_get_result",
                "dblink_get_connections",
                "dblink_cancel_query",
                "dblink_error_message",
                "lo_export",
                "lo_import",
                "lo_read",
                "lo_write",
                "lo_open",
                "lo_create",
                "lo_creat",
                "lo_unlink",
                "lo_get",
                "lo_put",
                "pg_read_file",
                "pg_read_binary_file",
                "pg_ls_dir",
                "pg_ls_logdir",
                "pg_ls_waldir",
                "pg_ls_archive_statusdir",
                "pg_stat_file",
                "pg_sleep",
                "pg_sleep_for",
                "pg_sleep_until",
                "pg_terminate_backend",
                "pg_cancel_backend",
                "pg_reload_conf",
                "pg_rotate_logfile",
                "pg_switch_wal",
                "pg_create_restore_point",
                "pg_promote",
                "pg_export_snapshot",
            }
        ),
        "tsql": frozenset(
            {
                "xp_cmdshell",
                "xp_regread",
                "xp_regwrite",
                "xp_regdeletekey",
                "xp_regdeletevalue",
                "xp_regenumvalues",
                "xp_regenumkeys",
                "xp_dirtree",
                "xp_fileexist",
                "xp_fixeddrives",
                "xp_availablemedia",
                "xp_subdirs",
                "xp_servicecontrol",
                "xp_instance_regread",
                "sp_configure",
                "sp_addlinkedserver",
                "sp_addlinkedsrvlogin",
                "sp_dropserver",
                "sp_oacreate",
                "sp_oamethod",
                "sp_oagetproperty",
                "sp_oadestroy",
                "sp_oasetproperty",
                "sp_executesql",
                "openrowset",
                "openquery",
                "opendatasource",
            }
        ),
        "oracle": frozenset(
            {
                "sys_context",
            }
        ),
        "snowflake": frozenset(
            {
                "system$wait",
                "system$cancel_all_queries",
                "system$abort_session",
                "system$abort_transaction",
                "system$send_email",
                "system$user_task_cancel_ongoing_executions",
                "system$get_privatelink_config",
                "system$allowlist",
                "system$request_id",
                "system$set_span_attributes",
            }
        ),
        "bigquery": frozenset(
            {
                "external_query",
            }
        ),
    }

    #: Package/schema qualifier prefixes that are forbidden regardless of the
    #: unqualified function name -- Oracle's ``DBMS_*``/``UTL_*`` built-in
    #: packages reach the network, the filesystem and session control
    #: (``UTL_HTTP``, ``UTL_TCP``, ``UTL_FILE``, ``DBMS_LOCK``,
    #: ``DBMS_SCHEDULER``, ``DBMS_PIPE`` and dozens more), so the prefix is
    #: blocked wholesale instead of enumerating every package name -- an
    #: enumerated list is exactly the kind of allowlist-shaped gap this task
    #: exists to close.
    _forbidden_package_prefixes_by_dialect: dict[str, tuple[str, ...]] = {
        "oracle": ("dbms_", "utl_"),
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
        if statement.find(exp.Lock) is not None:
            # FOR UPDATE / FOR SHARE take row or table locks against the
            # source -- not a mutation, but not the stateless, bounded read
            # this module promises either (QG-1): it can stall concurrent
            # workloads on a shared source and is a documented technique for
            # turning a "read-only" gateway into a contention or timing
            # side-channel.
            violations.append("LOCKING_READ_FORBIDDEN")
        # T-SQL spells the same intent as a table hint rather than a `FOR
        # UPDATE` clause -- `WITH (UPDLOCK, HOLDLOCK, XLOCK, TABLOCKX)` --
        # which parses as `exp.WithTableHint`, not `exp.Lock`. Only the
        # locking hints are refused; NOLOCK (a dirty-read hint, the opposite
        # problem) and plan hints like INDEX()/FORCESEEK are left alone.
        locking_table_hints = {"updlock", "holdlock", "xlock", "tablockx"}
        for hint in statement.find_all(exp.WithTableHint):
            hint_names = {
                str(getattr(item, "name", "")).lower() for item in hint.expressions
            }
            if hint_names & locking_table_hints:
                violations.append("LOCKING_READ_FORBIDDEN")
                break

        for join in statement.find_all(exp.Join):
            kind = str(join.args.get("kind") or "").upper()
            condition = join.args.get("on")
            has_condition = condition is not None or join.args.get("using") is not None
            # A join condition that references no column at all -- `ON true`,
            # `ON 1=1`, `ON 'x'='x'` -- has the same effect as a cross join,
            # it just satisfies the "has an ON clause" check by shape rather
            # than by substance. This is a documented technique for disguising
            # an unbounded join past a naive "join must have an ON" rule
            # (QG-1), so the condition must actually relate the two sides.
            vacuous_condition = condition is not None and condition.find(exp.Column) is None
            if kind == "CROSS" or not has_condition or vacuous_condition:
                violations.append("CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN")
                break

        dialect_forbidden_functions = self._forbidden_functions_by_dialect.get(
            dialect, frozenset()
        )
        package_prefixes = self._forbidden_package_prefixes_by_dialect.get(dialect, ())
        for function in statement.find_all(exp.Func):
            function_name = (function.name or function.sql_name()).lower()
            if (
                function_name in self._forbidden_functions_common
                or function_name in dialect_forbidden_functions
            ):
                violations.append(f"FORBIDDEN_FUNCTION:{function_name}")
                continue
            if package_prefixes:
                qualifiers = _package_qualifiers(function)
                if any(qualifier.startswith(package_prefixes) for qualifier in qualifiers):
                    qualified_name = ".".join((*qualifiers, function_name))
                    violations.append(f"FORBIDDEN_FUNCTION:{qualified_name}")

        # A data source that is not a plain catalog table -- a table-valued
        # function call used as a FROM/JOIN source (T-SQL OPENQUERY/OPENROWSET,
        # Snowflake/BigQuery TABLE(...)) -- cannot be resolved against the
        # metadata catalog at all, because it never produces an `exp.Table`
        # with a plain identifier. Left unblocked, it is a structural bypass of
        # the catalog allowlist regardless of dialect or function name, so it
        # is refused outright rather than enumerated (QG-1).
        table_sources_are_plain = all(
            isinstance(table.this, exp.Identifier) for table in statement.find_all(exp.Table)
        )
        if not table_sources_are_plain or statement.find(exp.TableFromRows) is not None:
            violations.append("TABLE_VALUED_SOURCE_FORBIDDEN")

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
