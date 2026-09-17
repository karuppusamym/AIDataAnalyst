from collections.abc import Iterable
from dataclasses import dataclass

from sqlglot import TokenType, exp, parse, tokenize
from sqlglot.errors import ParseError, TokenError


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


def _attached_with(node: exp.Expr) -> exp.With | None:
    """The ``WITH`` clause attached directly to ``node``, if it has one.

    Found by scanning ``node``'s own argument values rather than by naming the
    argument key, because sqlglot has spelled it both ``with`` and ``with_``
    across versions and a key that silently stops matching would reopen F01's
    G6 defect without failing a single parse.
    """
    for value in node.args.values():
        if isinstance(value, exp.With):
            return value
    return None


def _visible_cte_names(table: exp.Table) -> set[str]:
    """The CTE names in scope *at this table's own position* in the statement.

    F01 (G6): this used to be one flat, scope-blind set over the whole
    statement -- ``{cte.alias_or_name for cte in statement.find_all(exp.CTE)}``
    -- so a CTE declared inside a subquery's ``WITH`` shadowed an
    identically-named physical table referenced unqualified *anywhere else* in
    the same statement, and that table was silently dropped from
    ``referenced_tables``. Every downstream control consumes only that list --
    the catalog allowlist (`sql_validation.findings_from_catalog`), the ABAC
    axes (`policy_resource_attributes`), the context-product boundary
    (`context_product_execution_scope`) and the orchestrator's post-execution
    re-check -- so a dropped name escaped all of them at once. A name the guard
    does not report is a name nothing else can refuse.

    So visibility is walked rather than assumed. Climbing from the table to the
    root, a ``WITH`` attached to an ancestor query is in scope, and a ``WITH``
    belonging to some sibling subquery is never an ancestor, which is exactly
    the distinction the flat set lost.

    Inside one of a ``WITH``'s own CTE bodies only the CTEs declared *before*
    it are in scope, plus itself when the ``WITH`` is ``RECURSIVE`` -- SQL's own
    rule, and the conservative direction here: treating a later sibling's name
    as shadowing would drop a physical table again.
    """
    visible: set[str] = set()
    child: exp.Expr = table
    node: exp.Expr | None = table.parent
    while node is not None:
        if isinstance(node, exp.With):
            ctes = list(node.expressions)
            position = next(
                (index for index, cte in enumerate(ctes) if cte is child), len(ctes)
            )
            visible.update(cte.alias_or_name.lower() for cte in ctes[:position])
            if node.args.get("recursive") and position < len(ctes):
                visible.add(ctes[position].alias_or_name.lower())
        else:
            with_node = _attached_with(node)
            # `with_node is child` is the step already handled by the branch
            # above: the walk passes *through* the `With` node on its way out of
            # a CTE body, so counting it again here would put every sibling CTE
            # name back in scope.
            if with_node is not None and with_node is not child:
                visible.update(cte.alias_or_name.lower() for cte in with_node.expressions)
        child = node
        node = node.parent
    return visible


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

    #: R11-FP14: documented built-in functions sqlglot does not model as a typed expression, per
    #: dialect. A denylist cannot close the function boundary: a user-defined function's effects
    #: are unknown here, and a SELECT can call one that writes, sleeps or reaches outside the
    #: engine. So a call the parser does not recognise (`exp.Anonymous`) is refused unless it is
    #: one of these built-ins or an operator authorized it (`allowed_functions`), and a
    #: schema- or package-qualified call -- a user-defined function by construction -- is refused
    #: unless authorized by its qualified name. Generated by probing each name with sqlglot; a
    #: built-in sqlglot does model never reaches this list, because it is not `exp.Anonymous`.
    _unmodelled_builtin_functions_by_dialect: dict[str, frozenset[str]] = {
        "postgres": frozenset(
            {
                "age",
                "array_dims",
                "array_fill",
                "array_lower",
                "array_ndims",
                "array_positions",
                "array_replace",
                "array_upper",
                "btrim",
                "cardinality",
                "clock_timestamp",
                "every",
                "gcd",
                "isfinite",
                "json_array_length",
                "json_build_array",
                "json_build_object",
                "json_typeof",
                "jsonb_agg",
                "jsonb_array_length",
                "jsonb_build_array",
                "jsonb_build_object",
                "jsonb_extract_path",
                "jsonb_extract_path_text",
                "jsonb_path_query",
                "jsonb_strip_nulls",
                "jsonb_typeof",
                "lcm",
                "make_date",
                "make_timestamptz",
                "num_nonnulls",
                "num_nulls",
                "octet_length",
                "quote_ident",
                "quote_literal",
                "quote_nullable",
                "regexp_match",
                "regexp_matches",
                "regexp_split_to_array",
                "regexp_split_to_table",
                "row_to_json",
                "scale",
                "sha224",
                "statement_timestamp",
                "string_to_table",
                "to_ascii",
                "to_json",
                "to_jsonb",
                "transaction_timestamp",
            }
        ),
        "tsql": frozenset(
            {
                "binary_checksum",
                "checksum",
                "checksum_agg",
                "choose",
                "date_bucket",
                "datetime2fromparts",
                "datetimeoffsetfromparts",
                "difference",
                "getutcdate",
                "hashbytes",
                "isdate",
                "isjson",
                "isnumeric",
                "nchar",
                "parse",
                "patindex",
                "quotename",
                "smalldatetimefromparts",
                "stdevp",
                "str",
                "string_escape",
                "switchoffset",
                "sysutcdatetime",
                "todatetimeoffset",
                "try_parse",
                "var",
                "varp",
            }
        ),
        "oracle": frozenset(
            {
                "bitand",
                "json_query",
                "json_value",
                "lnnvl",
                "new_time",
                "nls_initcap",
                "nls_lower",
                "nls_upper",
                "numtodsinterval",
                "numtoyminterval",
                "remainder",
                "to_nchar",
                "to_timestamp",
                "trunc",
            }
        ),
        "snowflake": frozenset(
            {
                "as_number",
                "as_varchar",
                "hash",
                "ratio_to_report",
                "to_timestamp",
                "to_timestamp_ltz",
                "to_timestamp_ntz",
                "to_timestamp_tz",
                "trunc",
                "try_to_timestamp",
            }
        ),
        "bigquery": frozenset({"ieee_divide"}),
        "databricks": frozenset({"bround", "from_json", "hash", "make_date", "pmod"}),
    }

    #: Qualifiers that name the engine's own built-in namespace rather than a user schema.
    _builtin_qualifiers_by_dialect: dict[str, frozenset[str]] = {
        "postgres": frozenset({"pg_catalog"}),
        "bigquery": frozenset({"safe"}),
    }

    def __init__(
        self,
        *,
        default_row_limit: int,
        hard_row_limit: int,
        allowed_functions: Iterable[str] = (),
    ) -> None:
        self.default_row_limit = default_row_limit
        self.hard_row_limit = hard_row_limit
        #: Functions an operator reviewed for effects and authorized, lower-cased, matched by the
        #: exact name the SQL uses: `fn_rate`, or `finance.fn_rate` for a qualified call.
        self.allowed_functions = frozenset(
            name.strip().lower() for name in allowed_functions if name.strip()
        )

    def validate(
        self,
        sql: str,
        *,
        dialect: str,
        requested_limit: int | None = None,
        user_defined_functions: Iterable[str] = (),
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

        declared_routines = {name.lower() for name in user_defined_functions if name}
        if declared_routines:
            # R11-FP14: sqlglot models about six hundred function names, for every dialect, so a
            # call it recognises is not evidence of a built-in -- a user-defined `nvl` or `median`
            # parses as the modelled expression and reaches no check below. The parser also
            # rewrites the name (`nvl` becomes COALESCE), so the call as the author wrote it
            # survives only in the tokens. A name this source declares as a routine is a
            # user-defined function whatever the parser made of it.
            try:
                tokens = tokenize(sql, read=dialect)
            except (ParseError, TokenError, ValueError):
                tokens = []
            for token, following in zip(tokens, tokens[1:], strict=False):
                name = token.text.lower()
                if (
                    following.token_type is TokenType.L_PAREN
                    and name in declared_routines
                    and name not in self.allowed_functions
                ):
                    violations.append(f"UNAUTHORIZED_FUNCTION:{name}")
                    break
        dialect_forbidden_functions = self._forbidden_functions_by_dialect.get(
            dialect, frozenset()
        )
        package_prefixes = self._forbidden_package_prefixes_by_dialect.get(dialect, ())
        builtin_names = self._unmodelled_builtin_functions_by_dialect.get(dialect, frozenset())
        builtin_qualifiers = self._builtin_qualifiers_by_dialect.get(dialect, frozenset())
        for function in statement.find_all(exp.Func):
            function_name = (function.name or function.sql_name()).lower()
            if (
                function_name in self._forbidden_functions_common
                or function_name in dialect_forbidden_functions
            ):
                violations.append(f"FORBIDDEN_FUNCTION:{function_name}")
                continue
            qualifiers = _package_qualifiers(function)
            qualified_name = ".".join((*qualifiers, function_name))
            if package_prefixes and any(
                qualifier.startswith(package_prefixes) for qualifier in qualifiers
            ):
                violations.append(f"FORBIDDEN_FUNCTION:{qualified_name}")
                continue
            if qualified_name in self.allowed_functions:
                continue
            user_schema_qualified = any(
                qualifier not in builtin_qualifiers for qualifier in qualifiers
            )
            unrecognised = (
                isinstance(function, exp.Anonymous) and function_name not in builtin_names
            )
            if user_schema_qualified or unrecognised:
                violations.append(f"UNAUTHORIZED_FUNCTION:{qualified_name}")

        # Advancing a sequence is a write, and a read-only transaction is enforced by the server
        # on Postgres alone, so a guard that reads as "SELECT only" must refuse it here. Postgres
        # spells it `nextval('s')`, a call the unrecognised-function rule above already refuses.
        # Oracle and Snowflake spell it `seq.NEXTVAL`, which parses as a qualified column, and
        # T-SQL as `NEXT VALUE FOR seq`. A column of a table the query already reads is a column,
        # not a sequence, so the qualifier is what separates them.
        if statement.find(exp.NextValueFor) is not None:
            violations.append("SEQUENCE_ADVANCE_FORBIDDEN")
        else:
            query_sources = {
                name.lower()
                for table in statement.find_all(exp.Table)
                for name in (table.name, table.alias)
                if name
            }
            for column in statement.find_all(exp.Column):
                qualifier = column.table
                if (
                    column.name.lower() == "nextval"
                    and qualifier
                    and qualifier.lower() not in query_sources
                ):
                    violations.append("SEQUENCE_ADVANCE_FORBIDDEN")
                    break

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

        # A qualified name is a physical table even when a CTE shares its leaf
        # name: `FROM public.active` reads the schema's table, not the `active`
        # CTE, so only an unqualified reference can be shadowed -- and only by a
        # CTE actually visible where it stands (`_visible_cte_names`, F01 G6).
        tables = sorted(
            {
                ".".join(part for part in (table.catalog, table.db, table.name) if part)
                for table in statement.find_all(exp.Table)
                if table.catalog
                or table.db
                or table.name.lower() not in _visible_cte_names(table)
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
