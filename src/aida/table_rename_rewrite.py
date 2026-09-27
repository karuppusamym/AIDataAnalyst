"""R11-FP16: name the table a rename replaced, in SQL that still names the old one.

An approved rename (CT-4) merges identity: every stored reference to the old table's id is
repointed at the new one, and the old row is marked `superseded_by_table_id`. What no merge can
touch is *text* -- a governed tool's `sql_template` still says `sales.orders`, which the source
no longer has. Until now the rebuild pass could only propose retiring such a tool, which is the
right answer when the table is gone and the wrong one when it was renamed: the query is still
correct, the name is not.

`rewrite_table_references` produces the same query against the replacement, so
`aida.context_rebuild` can propose a version that works instead of retiring one that would.
The result is a proposal either way -- it is staged as a DRAFT and reviewed through
maker-checker like any other tool version, and `stage_tool_version_draft` runs the SQL guard,
the placeholder check and per-object authorization over it before anyone sees it.

Deliberately narrow:

* only a reference resolving to a replaced table is touched, matching the most qualified name
  first, so `sales.orders` and `orders` can be replaced by different tables;
* a name the query defines for itself -- a CTE -- is never rewritten, however it is spelled;
* nothing is guessed: a statement that will not parse, or that names none of the replaced
  tables, returns `None`, and the caller keeps the proposal it would have made anyway.

The statement is re-rendered by sqlglot, so the proposed SQL is normalized rather than a patch
of the original text. That is the same discipline the view and procedure blueprints already
follow, and the reviewer reads the whole proposed version, not a diff.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError


def table_name_forms(table: exp.Table) -> tuple[str, ...]:
    """Every name one reference can be looked up by, most qualified first, lower-cased."""
    catalog, db, name = table.catalog, table.db, table.name
    if not name:
        return ()
    forms = []
    if catalog and db:
        forms.append(f"{catalog}.{db}.{name}")
    if db:
        forms.append(f"{db}.{name}")
    forms.append(name)
    return tuple(form.lower() for form in forms)


def rewrite_table_references(
    sql: str, *, dialect: str, replacements: Mapping[str, str]
) -> str | None:
    """`sql` with every reference to a replaced table renamed, or `None` if it names none.

    `replacements` maps a lower-case name -- qualified or bare, whichever the SQL might use --
    to the qualified name that replaces it. Aliases are kept: `FROM sales.orders o` becomes
    `FROM sales.orders_2026 o`, so every column qualifier in the query still resolves.
    """
    if not replacements:
        return None
    try:
        statement = parse_one(sql, read=dialect)
    except SqlglotError:
        return None
    if statement is None:
        return None
    local_names = {
        cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }
    rewritten = 0
    for table in statement.find_all(exp.Table):
        if not table.db and not table.catalog and table.name.lower() in local_names:
            continue
        replacement = next(
            (replacements[form] for form in table_name_forms(table) if form in replacements), None
        )
        if replacement is None:
            continue
        try:
            target = exp.to_table(replacement, dialect=dialect)
        except SqlglotError:
            return None
        table.set("this", target.this)
        table.set("db", target.args.get("db"))
        table.set("catalog", target.args.get("catalog"))
        rewritten += 1
    if not rewritten:
        return None
    return statement.sql(dialect=dialect)
