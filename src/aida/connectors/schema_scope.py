"""R11-FP01: a discovery selection's schema scope, pushed into a source's own metadata queries.

`aida.discovery_selection` applies a selection to what a connector returns, before anything is
persisted, so every connector honours it. That alone still *reads* an excluded schema -- its
tables, columns, view definitions, routine bodies and grants -- and then drops it. A connector that
can take the schema scope into its own queries never reads it at all.

**Only what can be pushed exactly is pushed.** A schema pattern is a case-insensitive `fnmatch`
glob; `*` and `?` translate to SQL `LIKE`'s `%` and `_`, with `%`, `_` and the escape character
escaped. A character class (`[...]`) has no `LIKE` equivalent, so:

* an exclude pattern that cannot be translated is not pushed -- the source returns a little more,
  and the post-filter drops it;
* an include list is pushed only if every pattern in it translates, because pushing part of an
  include list would narrow the scan further than the selection does.

The pushed scope is therefore always a superset of the selection, and the post-filter still runs,
so pushing it down can only avoid reading, never drop an object in scope.

**How it reaches a query.** Every metadata query of the PostgreSQL and SQL Server connectors
already excludes the system schemas with one literal predicate on its schema column. The scope
extends exactly that predicate, with the patterns as bound parameters -- never interpolated -- so a
query that names no schema (the catalog comment, `current_database()`) is left alone.

**The other four adapters (R11-FP01 remainder, 2026-09-18).** Oracle, Snowflake, BigQuery and
Databricks build their metadata queries in code rather than as fixed strings, so instead of a
regular-expression anchor they call `ScopeSql` while building each query: it renders the
predicate in that engine's own bind syntax and collects the values to pass beside the statement.
Oracle and Databricks bind by name (`:scope_0`), BigQuery by named query parameter (`@scope_0`),
Snowflake by server-side `qmark` position (`?`). A pattern never enters the SQL text.

They take more than the schema scope. A selection's `schema.object` patterns and object kinds
(`ObjectScope`) are pushed too -- but only into the reads whose rows *belong to* an object: a
table's constraints, indexes, partitions and comments, a view's text, a routine's source. They are
deliberately **not** pushed into the reads that tell a scan which schemas exist -- the column
roster, the routine, trigger and sequence inventories, grants and schema comments. Those are what
`connectors.discovery.assemble_catalog` unions a catalog's schemas from, and a FULL run retires
every existing schema it did not see. Narrowed by kind or object, a schema in scope whose every
object is out of scope would vanish from the scan and be tombstoned, while the filter-after-reading
scan keeps it (empty) and retires nothing. So those reads take the schema scope only, which cannot
change which in-scope schemas appear, and a pushed scan stays exactly the unpushed scan after
`apply_selection` -- see `tests/test_discovery_pushdown_warehouses.py`, which runs both per engine
and compares.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

#: The system-schema predicates the connectors' metadata queries already carry.
_POSTGRES_ANCHOR: Final = re.compile(
    r"(?P<column>[A-Za-z_][\w.]*) NOT IN \('pg_catalog', 'information_schema'\)"
)
_SQLSERVER_ANCHOR: Final = re.compile(
    r"(?P<column>[A-Za-z_][\w.]*) NOT IN \('sys', 'INFORMATION_SCHEMA'\)"
)
_POSTGRES_PARAMETER: Final = re.compile(r"\$(\d+)")


@dataclass(frozen=True, slots=True)
class SchemaScope:
    """Lower-case `LIKE` patterns, already escaped with a backslash."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        return bool(self.include or self.exclude)


def like_pattern(glob: str) -> str | None:
    """A glob as an exact `LIKE` pattern, or `None` when `LIKE` cannot express it."""
    if "[" in glob:
        return None
    translated: list[str] = []
    for character in glob.lower():
        if character == "*":
            translated.append("%")
        elif character == "?":
            translated.append("_")
        elif character in "%_\\":
            translated.append("\\" + character)
        else:
            translated.append(character)
    return "".join(translated)


def schema_scope(include_schemas: Sequence[str], exclude_schemas: Sequence[str]) -> SchemaScope:
    """The part of a selection's schema scope a source query can apply exactly."""
    includes = [like_pattern(pattern) for pattern in include_schemas]
    include = (
        ()
        if any(pattern is None for pattern in includes)
        else tuple(pattern for pattern in includes if pattern is not None)
    )
    exclude = tuple(
        pattern
        for pattern in (like_pattern(value) for value in exclude_schemas)
        if pattern is not None
    )
    return SchemaScope(include=include, exclude=exclude)


def scoped_postgres_query(
    sql: str, scope: SchemaScope, arguments: Sequence[Any] = ()
) -> tuple[str, list[Any]]:
    """`sql` restricted to the scope, with its positional arguments extended to match.

    The patterns bind as two `text[]` parameters numbered after the query's own. PostgreSQL's
    `LIKE` escapes with a backslash by default, which is the escape `like_pattern` writes.
    """
    bound = list(arguments)
    if not scope.restricted or not _POSTGRES_ANCHOR.search(sql):
        return sql, bound
    used = [int(number) for number in _POSTGRES_PARAMETER.findall(sql)]
    if max(used, default=0) != len(bound):
        raise ValueError("the query's parameters do not match the arguments it was given")
    include_reference = exclude_reference = None
    if scope.include:
        bound.append(list(scope.include))
        include_reference = f"${len(bound)}"
    if scope.exclude:
        bound.append(list(scope.exclude))
        exclude_reference = f"${len(bound)}"

    def restrict(match: re.Match[str]) -> str:
        column = match.group("column")
        predicate = match.group(0)
        if include_reference is not None:
            predicate += f" AND lower({column}) LIKE ANY ({include_reference}::text[])"
        if exclude_reference is not None:
            predicate += f" AND NOT (lower({column}) LIKE ANY ({exclude_reference}::text[]))"
        return predicate

    return _POSTGRES_ANCHOR.sub(restrict, sql), bound


def scoped_sqlserver_query(sql: str, scope: SchemaScope) -> tuple[str, tuple[str, ...]]:
    """`sql` restricted to the scope, with one `%s` parameter per pattern, in textual order."""
    if not scope.restricted or not _SQLSERVER_ANCHOR.search(sql):
        return sql, ()
    if "%" in sql:
        raise ValueError("a query carrying a literal '%' cannot take bound LIKE patterns")
    parameters: list[str] = []

    def any_of(column: str, patterns: tuple[str, ...]) -> str:
        parameters.extend(patterns)
        return " OR ".join(f"LOWER({column}) LIKE %s ESCAPE '\\'" for _ in patterns)

    def restrict(match: re.Match[str]) -> str:
        column = match.group("column")
        predicate = match.group(0)
        if scope.include:
            predicate += f" AND ({any_of(column, scope.include)})"
        if scope.exclude:
            predicate += f" AND NOT ({any_of(column, scope.exclude)})"
        return predicate

    return _SQLSERVER_ANCHOR.sub(restrict, sql), tuple(parameters)


# ---------------------------------------------------------------------------
# R11-FP01 remainder: object kinds and `schema.object` patterns, and the four
# adapters that build their queries in code.
# ---------------------------------------------------------------------------


def _like_regex(pattern: str) -> re.Pattern[str]:
    """A backslash-escaped `LIKE` pattern as the regular expression it means."""
    parts: list[str] = []
    escaped = False
    for character in pattern:
        if escaped:
            parts.append(re.escape(character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "%":
            parts.append(".*")
        elif character == "_":
            parts.append(".")
        else:
            parts.append(re.escape(character))
    return re.compile("".join(parts), re.DOTALL)


@dataclass(frozen=True, slots=True)
class ObjectScope:
    """A selection's object kinds and `schema.object` patterns, as far as a query can apply them.

    `include` and `exclude` are lower-case `LIKE` patterns on `schema || '.' || object`, built by
    `like_pattern` under the same superset rule as `SchemaScope`: an untranslatable exclude is
    dropped, and an include list is kept only if all of it translates. `kinds` are selection
    kinds (`discovery_selection.OBJECT_KINDS`); empty means every kind.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    kinds: frozenset[str] = field(default_factory=frozenset)

    @property
    def restricted(self) -> bool:
        return bool(self.include or self.exclude or self.kinds)

    def admits_name(self, schema: str, name: str) -> bool:
        """Exactly the pushed name predicate, evaluated here instead of at the source.

        For a read the adapter issues once per object (Snowflake's `GET_DDL`), where the
        predicate has no statement to live in. `LIKE` semantics, so it agrees with what the
        source would have answered, and a superset of the selection like the rest.
        """
        qualified = f"{schema}.{name}".lower()
        if self.include and not any(
            _like_regex(pattern).fullmatch(qualified) for pattern in self.include
        ):
            return False
        return not any(_like_regex(pattern).fullmatch(qualified) for pattern in self.exclude)

    def admits_kind(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds


def object_scope(
    object_kinds: Iterable[str], include_objects: Sequence[str], exclude_objects: Sequence[str]
) -> ObjectScope:
    """The part of a selection's object scope a source query can apply exactly."""
    includes = [like_pattern(pattern) for pattern in include_objects]
    include = (
        ()
        if any(pattern is None for pattern in includes)
        else tuple(pattern for pattern in includes if pattern is not None)
    )
    exclude = tuple(
        pattern
        for pattern in (like_pattern(value) for value in exclude_objects)
        if pattern is not None
    )
    return ObjectScope(include=include, exclude=exclude, kinds=frozenset(object_kinds))


@dataclass(frozen=True, slots=True)
class DiscoveryScope:
    """Everything of a selection one adapter's queries can take, schema and object alike."""

    schemas: SchemaScope = field(default_factory=SchemaScope)
    objects: ObjectScope = field(default_factory=ObjectScope)

    @property
    def restricted(self) -> bool:
        return self.schemas.restricted or self.objects.restricted

    def narrows(self, *, kinds: bool) -> bool:
        """Whether an adapter taking this scope narrows any read at all.

        What `Connector.scope_discovery` returns, and the receipt records as
        `selection_pushed_down`, so it may not claim a push that did not happen: an
        adapter that pushes no kind anywhere (Databricks) passes `kinds=False`, and a
        kinds-only selection then reports nothing pushed.
        """
        objects = self.objects
        return (
            self.schemas.restricted
            or bool(objects.include or objects.exclude)
            or (kinds and bool(objects.kinds))
        )


def discovery_scope(
    *,
    include_schemas: Sequence[str],
    exclude_schemas: Sequence[str],
    object_kinds: Iterable[str] = (),
    include_objects: Sequence[str] = (),
    exclude_objects: Sequence[str] = (),
) -> DiscoveryScope:
    return DiscoveryScope(
        schemas=schema_scope(include_schemas, exclude_schemas),
        objects=object_scope(object_kinds, include_objects, exclude_objects),
    )


#: The four engines `ScopeSql` renders for, each with its own bind syntax.
ScopeDialect = Literal["oracle", "snowflake", "bigquery", "databricks"]

#: How each engine is told that `\` escapes the next character of a `LIKE` pattern.
#:
#: * Oracle has no default escape; `ESCAPE '\'` names one, and an Oracle string literal takes a
#:   backslash as itself.
#: * Snowflake has no default escape either, and its string literals *do* process backslash
#:   escapes, so the one-character escape is written `'\\'` in the SQL text.
#: * Databricks (Spark SQL) escapes with `\` by default ("The default escape character is the
#:   '\'"), so no clause is written -- one that spelled the literal wrong under
#:   `spark.sql.parser.escapedStringLiterals` would fail the whole read.
#: * BigQuery's `LIKE` takes no `ESCAPE` clause and treats a backslash in the *pattern* as the
#:   escape ("If you are using raw strings, only a single backslash is required. For example,
#:   r'\%'"). The pattern arrives as a query parameter, so its single backslash is the escape.
_LIKE_ESCAPE: Final[dict[str, str]] = {
    "oracle": " ESCAPE '\\'",
    "snowflake": " ESCAPE '\\\\'",
    "databricks": "",
    "bigquery": "",
}


class ScopeSql:
    """One query's scope predicates, rendered in one engine's bind syntax.

    Build one per statement, call its methods while writing the statement (they return SQL
    fragments beginning with ` AND `, or `""` when there is nothing to push), then pass
    `named` (Oracle, BigQuery, Databricks) or `positional` (Snowflake) beside the statement.
    The values -- operator-supplied patterns and kinds -- are only ever placeholders in the
    text; nothing a selection holds is interpolated.

    Snowflake binds by position (`?`), so its parameters are in the order the fragments were
    rendered: call the methods in the order the fragments appear in the statement, which an
    f-string does by evaluating left to right.
    """

    def __init__(self, scope: DiscoveryScope, dialect: ScopeDialect) -> None:
        self._scope = scope
        self._dialect = dialect
        self._named: dict[str, str] = {}
        self._positional: list[str] = []

    @property
    def named(self) -> dict[str, str]:
        """The bound values by placeholder name (`scope_0`), for the named-bind engines."""
        return dict(self._named)

    @property
    def positional(self) -> list[str]:
        """The bound values in statement order, for Snowflake's `qmark` binding."""
        return list(self._positional)

    def _bind(self, value: str) -> str:
        if self._dialect == "snowflake":
            self._positional.append(value)
            return "?"
        name = f"scope_{len(self._named)}"
        self._named[name] = value
        return f"@{name}" if self._dialect == "bigquery" else f":{name}"

    def _any_like(self, expression: str, patterns: Sequence[str]) -> str:
        escape = _LIKE_ESCAPE[self._dialect]
        return " OR ".join(
            f"LOWER({expression}) LIKE {self._bind(pattern)}{escape}" for pattern in patterns
        )

    def _patterns(self, expression: str, include: Sequence[str], exclude: Sequence[str]) -> str:
        predicate = ""
        if include:
            predicate += f" AND ({self._any_like(expression, include)})"
        if exclude:
            predicate += f" AND NOT ({self._any_like(expression, exclude)})"
        return predicate

    def schema(self, column: str) -> str:
        """The schema scope, on a query's schema column. Safe on every read."""
        scope = self._scope.schemas
        return self._patterns(column, scope.include, scope.exclude)

    def names(self, schema_column: str, name_column: str) -> str:
        """The `schema.object` patterns, on the object a row belongs to.

        Only for a read whose rows belong to one named object and never establish a schema on
        their own -- see the module docstring for why the inventories do not take it.
        """
        scope = self._scope.objects
        return self._patterns(
            f"{schema_column} || '.' || {name_column}", scope.include, scope.exclude
        )

    def kinds(self, column: str, native: Mapping[str, Sequence[str]]) -> str:
        """The selection's kinds, as the engine's own spelling of them on `column`.

        `native` maps each selection kind this read can hold to the values `column` takes for
        it. Unrestricted, or restricted to every kind the read can hold, renders nothing.
        """
        kinds = self._scope.objects.kinds
        if not kinds or set(native) <= kinds:
            return ""
        values = [
            value for kind, spellings in native.items() if kind in kinds for value in spellings
        ]
        if not values:
            return " AND 1 = 0"
        return f" AND {column} IN ({', '.join(self._bind(value) for value in values)})"

    def kind_gate(self, kind: str) -> str:
        """A read that holds one kind only: nothing when it is selected, no rows when it is not."""
        return "" if self._scope.objects.admits_kind(kind) else " AND 1 = 0"
