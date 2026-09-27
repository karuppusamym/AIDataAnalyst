"""Which table a column reference in a routine's statement belongs to -- by scope.

`procedure_lineage` used to give every column it could not resolve from its own
qualifier to "the statement's one source": the one table the statement names,
counted over the whole statement and excluding its write target. Both halves of
that were wrong for part of the grammar, and each produced a fact the text does
not state:

* A DELETE's, UPDATE's or MERGE's own target *is* in scope for its WHERE and SET,
  and a table named only inside a subquery is *not* in scope for the statement
  around it. `DELETE FROM dbo.final WHERE id IN (SELECT r.id FROM dbo.rejects r)`
  recorded its outer `id` as `dbo.rejects.id`.
* A name the routine declares -- a parameter, a local variable, a cursor's
  parameter, a loop record -- is not a column of a table, and `CURSOR c(p_id
  NUMBER) IS SELECT ... FROM ops.flags WHERE id = p_id` recorded `ops.flags.p_id`.

This module resolves each reference the way the engine's name resolution does,
as far as the text alone can prove it, and records the answer on the reference
itself (`sql_lineage_parser.COLUMN_OWNER_META`), where the shared extractors read
it. Nothing about the statement is rewritten: its SQL, its positions and what
`procedure_tool_blueprint` reads from it are the text as written.

**Scope.** A column is resolved against the FROM items of the query level it is
written in -- for UPDATE and DELETE, the target and the FROM/USING tables; for
MERGE, the target and the USING source, except that WHEN NOT MATCHED THEN INSERT
sees only the source and WHEN NOT MATCHED BY SOURCE only the target -- and then of
every level it can correlate to: a subquery in WHERE, in a projection or in SET
sees the levels around it; a derived table, a CTE's body, a set-operation's
statement and the query an INSERT or CREATE ... AS reads do not. A derived table
or a CTE passes a column through only under the same name (a projection `x.a`,
`a`, or a star); a column it renames or computes is not a column of the table
underneath it, so it resolves to nothing.

**The "not guessed" rule.** Without the catalog the parse cannot know which of two
tables has a column, and SQL binds an unqualified name to the innermost level
that has it, so every table visible from the reference's level outward is a
candidate. Exactly one distinct table resolves it; more than one -- or a source
whose columns the text does not show (a VALUES list, a table function's output
seen through a table variable, ...) -- leaves it `""`: recorded `UNRESOLVED`,
exactly as a column the parser cannot place always was, never the first or the
only-inner table.

**Declared names.** `DeclaredNames` holds what the routine declares.
PostgreSQL's PL/pgSQL rejects a name that could be both a variable and a column
(`plpgsql.variable_conflict = error`, its default), so there a declared name is
the variable, and is no source at all -- the treatment a T-SQL `@variable` has
always had, since sqlglot never parses one as a column. Where a column of the same
name would win instead -- PL/SQL, whose resolution gives the column precedence; a
LANGUAGE sql function; a PL/pgSQL body that says `#variable_conflict use_column`
-- the name is ambiguous without the catalog and resolves to `""`. A qualifier
that names no FROM item but a declared name (`rec.amount`, `v_row.id`) is a
record's field in either language. A qualifier that names nothing in scope at all
(`seq.NEXTVAL`, `EXCLUDED.v`, a package variable) is not a table either, and is
never recorded as one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from aida.sql_lineage_parser import (
    _SQLGLOT_AVAILABLE,
    COLUMN_OWNER_META,
    VARIABLE_REFERENCE,
    _resolve_table_name,
)

try:
    from sqlglot import exp
except ImportError:  # pragma: no cover -- see _SQLGLOT_AVAILABLE above
    pass

#: Set on a parsed statement once its references carry owners, so a statement
#: handed through two extraction paths is resolved once.
_RESOLVED_META: Final[str] = "aida_column_owners_resolved"


@dataclass(frozen=True, slots=True)
class DeclaredNames:
    """The names a routine declares, lower-cased, and what one of them means when a
    statement references it unqualified."""

    names: frozenset[str] = frozenset()
    #: True where the engine rejects a name that is both a variable and a column in
    #: scope, so a declared name is the variable (PL/pgSQL's default). False where a
    #: column of the same name would win, so the name is ambiguous without the catalog.
    variable_wins: bool = False

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self.names

    def with_names(self, more: frozenset[str]) -> DeclaredNames:
        return DeclaredNames(self.names | more, self.variable_wins)


NO_DECLARED_NAMES: Final = DeclaredNames()


def table_function_name(table: object) -> str | None:
    """The qualified name of a table-valued function used as a source, or `None` for a table.

    `FROM s.fn(2) n` parses as a `Table` whose `this` is the function call, so
    `_resolve_table_name` returns `s` alone -- the schema, stated as if it were the table. A
    function's rows are a routine's result, never a table's: it is an intermediate here, named
    after the function, and `aida.routine_call_descent` reads it through when that function is a
    routine captured in the same source.
    """
    if not _SQLGLOT_AVAILABLE or not isinstance(table, exp.Table):
        return None
    this = table.args.get("this")
    if not isinstance(this, exp.Func):
        return None
    name = this.name or this.sql_name()
    if name.upper() in _COLLECTION_WRAPPERS:
        return _unnested_function_name(this)
    parts = [part for part in (table.catalog, table.db, name) if part]
    return ".".join(parts) if parts else None


#: Oracle unnests a collection into rows with `TABLE(<expr>)`, and with the older `THE(<expr>)`.
#: R11-FP07: the wrapper is not the source. `FROM TABLE(pkg.fn(x))` parses as a table whose
#: function is the wrapper, so the source read as a table named `TABLE` -- a wrong edge, and a
#: gap naming a routine no source has.
_COLLECTION_WRAPPERS = frozenset({"TABLE", "THE"})


def _unnested_function_name(wrapper: exp.Func) -> str | None:
    """The function whose rows `TABLE(...)` unnests, or `None` when it unnests something else.

    `TABLE(pkg.fn(x))` is the function's result, named after it, exactly as `FROM pkg.fn(x)`
    is; a `CAST(... AS a_collection_type)` around it says what type the rows are, not where
    they come from. `TABLE(l_rows)` unnests a collection the routine declared and filled: it
    names no function and no table, so it is left unnamed here rather than reported as either.
    """
    arguments = wrapper.args.get("expressions") or []
    if len(arguments) != 1:
        return None
    argument = arguments[0]
    while isinstance(argument, exp.Cast):
        argument = argument.this
    if isinstance(argument, exp.Dot):
        parts = list(argument.flatten())
        if not parts or not isinstance(parts[-1], exp.Func):
            return None
        named = [part.name for part in parts if getattr(part, "name", "")]
        return ".".join(named) if named else None
    if isinstance(argument, exp.Func):
        return argument.name or argument.sql_name()
    return None


def table_reference_name(table: exp.Table) -> str:
    """The name `procedure_lineage`'s alias map keys a FROM-item table by."""
    return table_function_name(table) or _resolve_table_name(table)


def resolve_column_owners(
    node: object, *, aliases: Mapping[str, str], declared: DeclaredNames = NO_DECLARED_NAMES
) -> None:
    """Record, on each column reference in `node`, the table its scope resolves it to.

    `aliases` is the statement's alias map as `procedure_lineage` builds it -- a trigger's
    firing-row names already bound -- which is what a FROM item's name is canonicalised
    through, so an UPDATE's target alias and the table it names are one candidate, not two.
    Idempotent: a statement already resolved is left as it is.
    """
    if not _SQLGLOT_AVAILABLE or not isinstance(node, exp.Expression):
        return
    if node.meta.get(_RESOLVED_META):
        return
    resolver = _Resolver(node, aliases, declared)
    for column in node.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or not column.name:
            continue
        column.meta[COLUMN_OWNER_META] = resolver.owner(column)
    node.meta[_RESOLVED_META] = True


#: A FROM item's contribution to resolving a name: the tables that may own it
#: (empty when the item provably has no such column), or None when the item's
#: columns cannot be read from the text, which makes the name ambiguous.
_Provided = frozenset[str] | None


def _output_name(projection: exp.Expression) -> str:
    if isinstance(projection, exp.Alias):
        return projection.alias
    if isinstance(projection, exp.Column):
        return projection.name
    return ""


def _is_star(node: object) -> bool:
    return isinstance(node, exp.Star) or (
        isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
    )


class _Resolver:
    def __init__(
        self, root: exp.Expression, aliases: Mapping[str, str], declared: DeclaredNames
    ) -> None:
        self.aliases = aliases
        self.declared = declared
        self.ctes: dict[str, exp.CTE] = {}
        for cte in root.find_all(exp.CTE):
            if cte.alias:
                self.ctes.setdefault(cte.alias.lower(), cte)
        self._visiting: set[int] = set()

    # -- the answer ---------------------------------------------------------------

    def owner(self, column: exp.Column) -> str:
        """The table `column` belongs to, `""` when no single one is provable, or
        `VARIABLE_REFERENCE` when it is a routine's variable or a record's field."""
        qualifier = column.table
        if qualifier:
            return self._qualified_owner(column, qualifier)
        if column.name in self.declared:
            return VARIABLE_REFERENCE if self.declared.variable_wins else ""
        found: set[str] = set()
        for level in self._levels(column):
            for item in level:
                provided = self._provides(item, column.name)
                if provided is None:
                    return ""
                found |= provided
        if "" in found or len(found) != 1:
            return ""
        return next(iter(found))

    def _qualified_owner(self, column: exp.Column, qualifier: str) -> str:
        for level in self._levels(column):
            item = self._named(level, qualifier)
            if item is None:
                continue
            if isinstance(item, exp.Table) and self._cte(item) is None:
                return self._canonical(item)
            provided = self._provides(item, column.name)
            return next(iter(provided)) if provided is not None and len(provided) == 1 else ""
        if qualifier in self.aliases:
            # A name the statement's alias map binds that no level placed: a trigger's
            # firing row (`inserted.x`, `NEW.x`), resolved exactly as it always was.
            return self.aliases[qualifier]
        if qualifier in self.declared:
            return VARIABLE_REFERENCE
        # Names nothing in scope -- a sequence (`seq.NEXTVAL`), `EXCLUDED`, a package
        # variable: whatever it is, it is not a table this statement reads.
        return ""

    # -- scope --------------------------------------------------------------------

    def _levels(self, column: exp.Column) -> list[list[exp.Expression]]:
        """The FROM items visible to `column`, one list per query level, innermost first."""
        levels: list[list[exp.Expression]] = []
        node = column.parent
        while node is not None:
            if isinstance(node, exp.Update | exp.Insert) and isinstance(node.parent, exp.When):
                pass  # a MERGE branch's action: the MERGE is the level
            elif isinstance(node, exp.Select):
                levels.append(self._select_items(node))
                if not self._correlates(node):
                    break
            elif isinstance(node, exp.Update | exp.Delete):
                levels.append(self._dml_items(node))
                break
            elif isinstance(node, exp.Merge):
                levels.append(self._merge_items(node, column))
                break
            elif isinstance(node, exp.Insert | exp.Create):
                break  # the write target is not in scope of the query that feeds it
            node = node.parent
        return levels

    @staticmethod
    def _correlates(select: exp.Select) -> bool:
        """Whether the levels around `select` are visible to its columns."""
        node: exp.Expression = select
        while isinstance(node.parent, exp.Subquery | exp.SetOperation | exp.Paren):
            node = node.parent
        parent = node.parent
        if parent is None or isinstance(parent, exp.From | exp.Join | exp.CTE):
            return False
        if isinstance(parent, exp.Insert | exp.Create):
            return False
        return not (isinstance(parent, exp.Merge) and node.arg_key == "using")

    def _relation_items(self, relation: object) -> list[exp.Expression]:
        if not isinstance(relation, exp.Expression):
            return []
        if isinstance(relation, exp.Subquery) and not isinstance(relation.this, exp.Query):
            return self._relation_items(relation.this)  # a parenthesised join, not a query
        items = [relation]
        for join in relation.args.get("joins") or []:
            if isinstance(join, exp.Join):
                items.extend(self._relation_items(join.this))
        return items

    def _select_items(self, select: exp.Select) -> list[exp.Expression]:
        items: list[exp.Expression] = []
        from_ = select.args.get("from_")
        if isinstance(from_, exp.From):
            items.extend(self._relation_items(from_.this))
        for join in select.args.get("joins") or []:
            if isinstance(join, exp.Join):
                items.extend(self._relation_items(join.this))
        items.extend(select.args.get("laterals") or [])
        return items

    def _dml_items(self, node: exp.Update | exp.Delete) -> list[exp.Expression]:
        items = self._relation_items(node.this)
        from_ = node.args.get("from_")
        if isinstance(from_, exp.From):
            items.extend(self._relation_items(from_.this))
        using = node.args.get("using")
        for relation in using if isinstance(using, list) else [using]:
            items.extend(self._relation_items(relation))
        for table in node.args.get("tables") or []:
            items.extend(self._relation_items(table))  # T-SQL `DELETE f FROM ... f`
        return items

    def _merge_items(self, merge: exp.Merge, column: exp.Column) -> list[exp.Expression]:
        target = self._relation_items(merge.this)
        source = self._relation_items(merge.args.get("using"))
        when = column.find_ancestor(exp.When)
        if when is not None and when.find_ancestor(exp.Merge) is merge:
            then = when.args.get("then")
            if isinstance(then, exp.Insert) and column.find_ancestor(exp.Insert) is then:
                return source  # the target row does not exist yet
            if when.args.get("source"):
                return target  # WHEN NOT MATCHED BY SOURCE: no source row
        return target + source

    # -- what a FROM item provides --------------------------------------------------

    def _canonical(self, table: exp.Table) -> str:
        raw = table_reference_name(table)
        return self.aliases.get(raw, raw)

    def _cte(self, table: exp.Table) -> exp.CTE | None:
        if table.db or table.catalog or not isinstance(table.this, exp.Identifier):
            return None
        return self.ctes.get(table.name.lower())

    @staticmethod
    def _named(level: list[exp.Expression], qualifier: str) -> exp.Expression | None:
        wanted = qualifier.lower()
        for item in level:
            if (item.alias_or_name or "").lower() == wanted:
                return item
        for item in level:
            if isinstance(item, exp.Table) and not item.alias and item.name.lower() == wanted:
                return item
        return None

    def _provides(self, item: exp.Expression, name: str) -> _Provided:
        if isinstance(item, exp.Table):
            cte = self._cte(item)
            if cte is not None:
                return self._query_provides(cte.this, name)
            canonical = self._canonical(item)
            return frozenset({canonical}) if canonical else None
        if isinstance(item, exp.Subquery) and isinstance(item.this, exp.Query):
            return self._query_provides(item.this, name)
        return None  # VALUES, UNNEST, LATERAL, ...: its columns are not in the text

    def _query_provides(self, query: object, name: str) -> _Provided:
        """What a derived table or CTE body provides under `name`: the column it passes
        through by that name, or every table under a star it selects."""
        if not isinstance(query, exp.Select) or id(query) in self._visiting:
            return None  # a set operation, or a CTE that reads itself
        self._visiting.add(id(query))
        try:
            wanted = name.lower()
            named = [p for p in query.expressions if _output_name(p).lower() == wanted]
            if named:
                inner = named[0].this if isinstance(named[0], exp.Alias) else named[0]
                if (
                    len(named) > 1
                    or not isinstance(inner, exp.Column)
                    or inner.name.lower() != wanted
                ):
                    return None  # renamed or computed: not that table's column
                owner = self.owner(inner)
                return None if owner in ("", VARIABLE_REFERENCE) else frozenset({owner})
            found: set[str] = set()
            for projection in query.expressions:
                if not _is_star(projection):
                    continue
                qualifier = projection.table if isinstance(projection, exp.Column) else ""
                items = self._select_items(query)
                if qualifier:
                    one = self._named(items, qualifier)
                    if one is None:
                        return None
                    items = [one]
                for item in items:
                    provided = self._provides(item, name)
                    if provided is None:
                        return None
                    found |= provided
            return frozenset(found)
        finally:
            self._visiting.discard(id(query))
