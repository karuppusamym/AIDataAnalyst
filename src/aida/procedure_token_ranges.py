"""R11-FP07 token-grain ranges: where, inside its statement, each end of an edge is named.

`procedure_lineage` locates every edge at its *statement* (`StatementRange`). This
module narrows that to the *token*: the column reference an edge reads
(`o.amount`), the column it writes (`amount` in `INSERT INTO t (amount)`), or --
for a table-grain edge -- the table reference (`dbo.orders`). A reviewer shown an
edge can then highlight exactly the evidence rather than a whole statement.

**Which text.** The same text the statement range indexes: the one the parser
was handed, which for every persisted edge is the stored, redacted body
(`body_sql_redacted`) -- never the customer's source, which the platform does not
hold (R11-D16, INV-6). Offsets are half-open code-point indices into it, so
`text[start_offset:end_offset]` is the token as stored, and the edge's
`statement_text_digest` pins that text. A token range always lies inside its
edge's statement range; an edge with no statement range has no token range.

**Where the positions come from.** sqlglot 30 records `start`/`end` (inclusive)
on identifier leaves, relative to the string it parsed -- which is the peeled
statement, and for PL/pgSQL a rewritten one. Every rewrite the parser makes is
either length-preserving (it blanks `ON COMMIT ...`, `STRICT` and a `RETURNING
... INTO` target with spaces) or replaces a prefix (`RETURN QUERY`, `PERFORM`,
`v :=`), so the parsed string's *tail* is always the statement's tail and one
offset aligns the two. That alignment is then **proved, not assumed**: every
identifier in the parsed statement must slice out of the stored text exactly as
it sits in the parsed string, or no token of that statement is located at all.

**Which token, and the NULL rule.** An edge's evidence is found by searching the
parsed statement, never by trusting a name: the candidates are every reference
that could have produced the edge (a superset of the one the extractor used),
and a token is recorded only when **exactly one** candidate remains. Two
candidates -- the same column read twice in one output expression, the same
table named twice in one statement, a MERGE naming its target column in both its
UPDATE and INSERT branches -- record NULL, never the first occurrence and never
all of them: a reviewer highlighting one of two is being told something the
parse does not know. The candidate sets:

* **Source, column grain.** Column references with the edge's column name inside
  the expressions that can produce the edge's target column: projections named
  after it (or at its position in an INSERT column list), VALUES entries at that
  position, the right side of a SET assignment to it; for a FILTERED edge, every
  WHERE clause. When several remain, the qualifier is resolved through the
  statement's own alias map -- only where that map provably equals the
  extractor's (no CTE, not CREATE ... AS) -- and each reference counts as reading
  the table its scope resolved it to (`procedure_column_owners`): an unqualified
  `id` the scope gives `dbo.rejects` and a qualified `r.id` are two references to
  one fact, while one the scope gives the statement's own target is not. A
  routine's variable is never a candidate.
* **Source, table grain** (`source_column == '*'`): table references, other than
  the statement's write target, that resolve to the edge's source.
* **Target, column grain.** The node that *names* the target column: the INSERT
  column-list entry, the SET assignment's left side (UPDATE, MERGE), MERGE's
  INSERT column entry, or a projection's alias or bare column name.
* **Target, table grain** (`*` or the FILTER marker): the statement's write
  target, when it resolves to the edge's target table.

A transitive edge (`via_temp_table`) keeps its target token and has no source
token -- its source is read in another statement. An edge read from a called
routine (`CALL_SITE`) has neither: the callee's positions index the callee's
body. An UNPARSED marker has neither: there was no parse to locate anything in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from aida.sql_lineage_parser import (
    _SQLGLOT_AVAILABLE,
    COLUMN_OWNER_META,
    FILTER_EVIDENCE_TARGET_COLUMN,
    STAR_COLUMN_MARKER,
    VARIABLE_REFERENCE,
    _resolve_table_name,
)

try:
    from sqlglot import exp
except ImportError:  # pragma: no cover -- see _SQLGLOT_AVAILABLE above
    pass

#: Where `remember_parsed_text` keeps, on a parsed node, the exact string sqlglot
#: was given -- the only thing its identifier positions are relative to.
PARSED_TEXT_META: Final[str] = "aida_parsed_text"

#: `transformation_type` of an UNPARSED marker (`procedure_lineage`); restated to
#: keep this module free of an import cycle.
_UNPARSED: Final[str] = "UNPARSED"


class TokenRangeKind(StrEnum):
    """What a token range is the range *of*."""

    #: A column reference: `o.amount`, `"Big Amt"`, or the name a projection,
    #: INSERT column list or SET assignment gives a target column.
    COLUMN = "COLUMN"
    #: A table reference as written, without its alias: `dbo.orders`, `[s].[t]`.
    TABLE = "TABLE"


@dataclass(frozen=True, slots=True)
class TokenRange:
    """A token's span in the parsed text -- the stored body. Positions, never content.

    Half-open code-point offsets into the same text as the edge's
    `StatementRange`, and always inside it."""

    kind: str
    start_offset: int
    end_offset: int


class LineageEdgeLike(Protocol):
    """The parts of a `ProcedureLineageEdgeRecord` token location reads."""

    @property
    def source_table(self) -> str: ...
    @property
    def source_column(self) -> str: ...
    @property
    def target_table(self) -> str: ...
    @property
    def target_column(self) -> str: ...
    @property
    def transformation_type(self) -> str: ...
    @property
    def source_resolved(self) -> bool: ...


def remember_parsed_text(node: object, text: str) -> None:
    """Keep the exact string `node` was parsed from on the node itself, so its
    identifier positions can later be aligned with the stored body."""
    if _SQLGLOT_AVAILABLE and isinstance(node, exp.Expression):
        node.meta[PARSED_TEXT_META] = text


@dataclass(frozen=True, slots=True)
class _Alignment:
    """The parsed string, placed in the stored text at `base`."""

    parsed: str
    text: str
    base: int
    start: int
    end: int

    def place(self, first: int, last: int) -> tuple[int, int] | None:
        """`parsed[first:last]` as a span of the stored text, or None unless it
        lies inside the statement and slices out of the stored text unchanged."""
        begin, finish = self.base + first, self.base + last
        if begin < self.start or finish > self.end or finish <= begin:
            return None
        if self.text[begin:finish] != self.parsed[first:last]:
            return None
        return begin, finish


def _identifier_span(identifier: object, parsed: str) -> tuple[int, int] | None:
    """Half-open span of one identifier in the string it was parsed from."""
    if not isinstance(identifier, exp.Identifier):
        return None
    start, last = identifier.meta.get("start"), identifier.meta.get("end")
    if not isinstance(start, int) or not isinstance(last, int):
        return None
    end = last + 1
    # A T-SQL `#temp` / `##global` name is read with its hashes stripped; the
    # reference as written includes them.
    hashes = 2 if identifier.args.get("global_") else 1 if identifier.args.get("temporary") else 0
    if hashes and parsed[max(start - hashes, 0) : start] == "#" * hashes:
        start -= hashes
    if not 0 <= start < end <= len(parsed):
        return None
    return start, end


def _align(node: exp.Expression, text: str, start: int, end: int) -> _Alignment | None:
    parsed = node.meta.get(PARSED_TEXT_META)
    if not isinstance(parsed, str):
        return None
    # Right-aligned: every rewrite keeps the statement's tail where it was.
    alignment = _Alignment(parsed, text, end - len(parsed), start, end)
    for identifier in node.find_all(exp.Identifier):
        span = _identifier_span(identifier, parsed)
        if span is not None and alignment.place(*span) is None:
            # One identifier out of place means the alignment is not the one this
            # module assumes; no position from this statement can be trusted.
            return None
    return alignment


def _parts_span(parts: Sequence[object], alignment: _Alignment) -> tuple[int, int] | None:
    """The span from a dotted reference's first part to its last."""
    spans = [_identifier_span(part, alignment.parsed) for part in parts]
    if not spans:
        return None
    first, last = spans[0], spans[-1]
    if first is None or last is None or any(span is None for span in spans):
        return None
    return alignment.place(first[0], last[1])


def _node_span(node: object, alignment: _Alignment) -> tuple[int, int] | None:
    if isinstance(node, exp.Column):
        if isinstance(node.this, exp.Star):
            return None
        return _parts_span(node.parts, alignment)
    if isinstance(node, exp.Table):
        if not isinstance(node.this, exp.Identifier):
            return None  # a table-valued function or a `@table` variable
        return _parts_span(node.parts, alignment)
    return _parts_span([node], alignment)


def _output_name(projection: object, index: int) -> str:
    """The name `sql_lineage_parser._extract_edges_from_select` gives a projection."""
    if isinstance(projection, exp.Alias):
        return projection.alias
    if isinstance(projection, exp.Column):
        return projection.name
    return f"_col{index}"


def _is_assignment(node: object) -> bool:
    """`SET c = ...` in an UPDATE or a MERGE's WHEN MATCHED THEN UPDATE."""
    return (
        isinstance(node, exp.EQ)
        and isinstance(node.parent, exp.Update)
        and node.arg_key == "expressions"
        and isinstance(node.this, exp.Column)
    )


def _write_target(node: exp.Expression) -> exp.Table | None:
    target: object = None
    if isinstance(node, exp.Insert | exp.Create):
        target = node.this.this if isinstance(node.this, exp.Schema) else node.this
    elif isinstance(node, exp.Update | exp.Delete | exp.Merge):
        target = node.this
    elif isinstance(node, exp.Select):
        into = node.args.get("into")
        target = into.this if into is not None else None
    return target if isinstance(target, exp.Table) else None


def _names_of(table: exp.Table, aliases: Mapping[str, str]) -> set[str]:
    """Every name the extractors can have given `table`."""
    structural = _resolve_table_name(table)
    names = {structural, aliases.get(structural, "")}
    if table.alias:
        names.add(aliases.get(table.alias, ""))
    if table.name:
        names.add(aliases.get(table.name, ""))
    names.discard("")
    return names


def _listed_columns(insert: exp.Insert) -> list[exp.Expression]:
    """An INSERT's explicit column list, filtered exactly as
    `procedure_lineage._extract_edges_from_insert` filters it; empty if none."""
    if not isinstance(insert.this, exp.Schema):
        return []
    return [
        column
        for column in insert.this.expressions
        if isinstance(column, exp.Column | exp.Identifier)
    ]


class _Statement:
    """One parsed statement, indexed once for every edge located in it.

    `producers` maps a target column name to every expression that can have
    produced an edge into it -- a superset of the one the extractor used, which
    is what makes "exactly one candidate" exact. `namers` maps it to every node
    that can have named it."""

    def __init__(
        self,
        node: exp.Expression,
        alignment: _Alignment,
        aliases: Mapping[str, str],
        unqualified_source: str | None,
    ) -> None:
        self.node = node
        self.alignment = alignment
        self.aliases = aliases
        self.unqualified_source = unqualified_source
        # The qualifier decides between candidates only where this alias map is the
        # one the extractor resolved with: a CTE's name overrides an alias there, and
        # CREATE ... AS resolves through `sql_lineage_parser._collect_table_aliases`.
        self.qualifiers_decide = node.find(exp.CTE) is None and not isinstance(node, exp.Create)
        self.write_target = _write_target(node)
        self.wheres: list[exp.Expression] = list(node.find_all(exp.Where))
        self.tables: list[exp.Table] = list(node.find_all(exp.Table))
        self.producers: dict[str, list[exp.Expression]] = {}
        self.namers: dict[str, list[exp.Expression]] = {}
        selects = list(node.find_all(exp.Select))
        assignments = [eq for eq in node.find_all(exp.EQ) if _is_assignment(eq)]
        inserts = list(node.find_all(exp.Insert))

        for select in selects:
            for index, projection in enumerate(select.expressions):
                self._produces(_output_name(projection, index), projection)
        for insert in inserts:
            for position, column in enumerate(_listed_columns(insert)):
                for select in insert.find_all(exp.Select):
                    if position < len(select.expressions):
                        self._produces(column.name, select.expressions[position])
                for values in insert.find_all(exp.Values):
                    for row in values.expressions:
                        items = list(row.expressions) if isinstance(row, exp.Tuple) else [row]
                        if position < len(items):
                            self._produces(column.name, items[position])
            if isinstance(insert.this, exp.Tuple):
                # MERGE ... WHEN NOT MATCHED THEN INSERT (a, b) VALUES (x, y)
                sources = insert.expression
                items = list(sources.expressions) if isinstance(sources, exp.Tuple) else []
                for position, column in enumerate(insert.this.expressions):
                    if isinstance(column, exp.Column) and position < len(items):
                        self._produces(column.name, items[position])
        for assignment in assignments:
            self._produces(assignment.this.name, assignment.expression)

        # Who names a target column follows the statement's own shape, exactly as
        # the extractor that built its edges chose it.
        listed = _listed_columns(node) if isinstance(node, exp.Insert) else []
        if listed:
            for column in listed:
                self._names(column.name, column)
        elif isinstance(node, exp.Update | exp.Merge):
            for assignment in assignments:
                self._names(assignment.this.name, assignment.this)
            for insert in inserts:
                if isinstance(insert.this, exp.Tuple):
                    for column in insert.this.expressions:
                        if isinstance(column, exp.Column):
                            self._names(column.name, column)
        else:
            for select in selects:
                for index, projection in enumerate(select.expressions):
                    namer: object = projection  # `_colN`: counted, but no token names it
                    if isinstance(projection, exp.Alias):
                        namer = projection.args.get("alias")
                    elif isinstance(projection, exp.Column):
                        namer = projection.this
                    self._names(_output_name(projection, index), namer)

    def _produces(self, name: str, expression: object) -> None:
        if isinstance(expression, exp.Expression):
            self.producers.setdefault(name, []).append(expression)

    def _names(self, name: str, node: object) -> None:
        if isinstance(node, exp.Expression):
            self.namers.setdefault(name, []).append(node)

    def token(self, kind: TokenRangeKind, node: object) -> TokenRange | None:
        span = _node_span(node, self.alignment)
        return TokenRange(kind.value, *span) if span else None

    def source(self, edge: LineageEdgeLike) -> TokenRange | None:
        if edge.source_column == STAR_COLUMN_MARKER:
            tables = [
                table
                for table in self.tables
                if table is not self.write_target
                and edge.source_table in _names_of(table, self.aliases)
            ]
            return self.token(TokenRangeKind.TABLE, tables[0]) if len(tables) == 1 else None
        if edge.target_column == FILTER_EVIDENCE_TARGET_COLUMN:
            scopes = self.wheres
        else:
            # Nothing found means the edge came from a shape not indexed above: the
            # whole statement is still a superset, so uniqueness in it is still exact.
            scopes = self.producers.get(edge.target_column) or [self.node]
        seen: set[int] = set()
        candidates: list[exp.Column] = []
        for scope in scopes:
            for column in scope.find_all(exp.Column):
                if id(column) in seen or isinstance(column.this, exp.Star):
                    continue
                if column.meta.get(COLUMN_OWNER_META) == VARIABLE_REFERENCE:
                    continue  # a routine's variable: evidence of no edge
                seen.add(id(column))
                if column.name == edge.source_column:
                    candidates.append(column)
        if len(candidates) > 1 and self.qualifiers_decide:
            candidates = [column for column in candidates if self._reads(column, edge)]
        return self.token(TokenRangeKind.COLUMN, candidates[0]) if len(candidates) == 1 else None

    def _reads(self, column: exp.Column, edge: LineageEdgeLike) -> bool:
        """Whether `column` resolves to the edge's source exactly as the parse
        resolves it: the table its scope gave it (`procedure_column_owners`, which
        `sql_lineage_parser._extract_source_columns` reports), then
        `sql_lineage_parser._resolve_or_mark_unresolved`. A statement parsed without
        scope owners falls back to its qualifier and `unqualified_source`."""
        owner = column.meta.get(COLUMN_OWNER_META)
        if isinstance(owner, str):
            reference = owner
        else:
            reference = column.table
        resolved = self.aliases.get(reference, reference) if reference else ""
        if owner is None and not resolved and self.unqualified_source is not None:
            resolved = self.unqualified_source
        return resolved == edge.source_table if edge.source_resolved else resolved == ""

    def target(self, edge: LineageEdgeLike) -> TokenRange | None:
        if edge.target_column in (STAR_COLUMN_MARKER, FILTER_EVIDENCE_TARGET_COLUMN):
            table = self.write_target
            if table is None or edge.target_table not in _names_of(table, self.aliases):
                return None  # the routine's result set or a local: no table is named
            return self.token(TokenRangeKind.TABLE, table)
        named = self.namers.get(edge.target_column, [])
        return self.token(TokenRangeKind.COLUMN, named[0]) if len(named) == 1 else None


def locate_edge_tokens(
    node: object,
    edges: Sequence[LineageEdgeLike],
    *,
    text: str,
    start: int,
    end: int,
    aliases: Mapping[str, str],
    unqualified_source: str | None = None,
) -> list[tuple[TokenRange | None, TokenRange | None]]:
    """`(source, target)` token ranges for each of `edges`, read from `node`.

    `text[start:end]` is the statement `node` was parsed from, as it sits in the
    text the edges' statement ranges index; `aliases` is the alias map the
    dispatcher resolved `node`'s qualifiers with, and `unqualified_source` the
    table the parse attributed every unresolved column to, if it did. Every pair
    is `(None, None)` when the parsed string cannot be proved to align with that
    text.
    """
    unlocated: list[tuple[TokenRange | None, TokenRange | None]] = [(None, None)] * len(edges)
    if not _SQLGLOT_AVAILABLE or not isinstance(node, exp.Expression):
        return unlocated
    alignment = _align(node, text, start, end)
    if alignment is None:
        return unlocated
    statement = _Statement(node, alignment, aliases, unqualified_source)
    return [
        (None, None)
        if edge.transformation_type == _UNPARSED
        else (statement.source(edge), statement.target(edge))
        for edge in edges
    ]
