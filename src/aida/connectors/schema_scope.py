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
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

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
