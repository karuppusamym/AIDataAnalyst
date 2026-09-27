"""R11-FP01: the schema scope a connector pushes into its own metadata queries.

Pushing a selection down must only ever avoid reading: the post-filter still runs, so the pushed
scope has to be a superset of the selection. These tests pin the translation from `fnmatch` globs
to `LIKE`, the rules for what cannot be translated, how the scope binds into each dialect's query,
and that no schema-bound discovery query of either connector can escape it.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from types import ModuleType

import pytest

from aida.connectors import postgres, sqlserver
from aida.connectors.schema_scope import (
    SchemaScope,
    like_pattern,
    schema_scope,
    scoped_postgres_query,
    scoped_sqlserver_query,
)


def _like(value: str, pattern: str) -> bool:
    """SQL `LIKE` with a backslash escape, evaluated in Python for the translation check."""
    translated = []
    escaped = False
    for character in pattern:
        if escaped:
            translated.append(character.replace("*", "[*]").replace("?", "[?]"))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "%":
            translated.append("*")
        elif character == "_":
            translated.append("?")
        elif character in "*?[":
            translated.append(f"[{character}]")
        else:
            translated.append(character)
    return fnmatchcase(value, "".join(translated))


@pytest.mark.parametrize(
    ("glob", "names"),
    [
        ("sales", ["sales", "sales2", "SALES", "presales"]),
        ("stg_*", ["stg_orders", "stg", "stgxorders", "STG_A"]),
        ("tmp?", ["tmp1", "tmp", "tmp12", "TMP_"]),
        ("a%b_c\\d", ["a%b_c\\d", "axbyc\\d", "a%b_cd"]),
    ],
)
def test_a_glob_translates_to_exactly_the_like_pattern_that_matches_the_same_names(
    glob: str, names: list[str]
) -> None:
    pattern = like_pattern(glob)
    assert pattern is not None
    for name in names:
        assert _like(name.lower(), pattern) == fnmatchcase(name.lower(), glob.lower()), name


def test_what_like_cannot_express_is_never_pushed_in_a_way_that_narrows() -> None:
    assert like_pattern("sales_[ab]") is None
    # An untranslatable exclude is dropped: the source returns more, the post-filter removes it.
    assert schema_scope([], ["stg_*", "tmp[0-9]"]) == SchemaScope(exclude=("stg\\_%",))
    # One untranslatable include withdraws the whole include list: part of it would narrow.
    assert schema_scope(["sales", "fin[ae]nce"], []) == SchemaScope()
    assert not schema_scope([], []).restricted


def test_the_postgres_scope_binds_after_the_query_own_parameters_on_every_anchor() -> None:
    scope = schema_scope(["sales*"], ["sales_tmp"])
    sql = (
        "SELECT 1 FROM a WHERE a.table_schema NOT IN ('pg_catalog', 'information_schema') "
        "AND (a.x, a.y) IN (SELECT * FROM unnest($1::text[], $2::text[])) "
        "UNION SELECT 1 FROM b WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')"
    )

    scoped, bound = scoped_postgres_query(sql, scope, (["s"], ["t"]))

    assert bound == [["s"], ["t"], ["sales%"], ["sales\\_tmp"]]
    assert scoped.count("lower(a.table_schema) LIKE ANY ($3::text[])") == 1
    assert scoped.count("NOT (lower(n.nspname) LIKE ANY ($4::text[]))") == 1
    assert scoped_postgres_query("SELECT current_database()", scope) == (
        "SELECT current_database()",
        [],
    )
    with pytest.raises(ValueError, match="parameters"):
        scoped_postgres_query(sql, scope, (["s"],))


def test_the_sqlserver_scope_binds_one_parameter_per_pattern_in_textual_order() -> None:
    scope = schema_scope(["sales", "finance"], ["fin_tmp"])
    sql = (
        "SELECT 1 FROM a WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
        "UNION SELECT 1 FROM b WHERE c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')"
    )

    scoped, parameters = scoped_sqlserver_query(sql, scope)

    assert parameters == ("sales", "finance", "fin\\_tmp") * 2
    assert scoped.count("%s") == len(parameters)
    assert "LOWER(c.TABLE_SCHEMA) LIKE %s ESCAPE '\\'" in scoped
    assert scoped_sqlserver_query("SELECT DB_NAME()", scope) == ("SELECT DB_NAME()", ())


@pytest.mark.parametrize(
    ("module", "anchor"),
    [
        (postgres, "NOT IN ('pg_catalog', 'information_schema')"),
        (sqlserver, "NOT IN ('sys', 'INFORMATION_SCHEMA')"),
    ],
)
def test_every_schema_bound_discovery_query_carries_the_predicate_the_scope_extends(
    module: ModuleType, anchor: str
) -> None:
    """A new metadata query without the system-schema predicate would read excluded schemas."""
    queries = {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    unscoped = sorted(name for name, value in queries.items() if anchor not in value)
    # The catalog-level comment names no schema.
    assert unscoped == ["_CATALOG_COMMENT_SQL"], unscoped
