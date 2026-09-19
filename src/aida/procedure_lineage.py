"""N3: procedure-body-aware SQL lineage extraction (T-SQL and PL/SQL first).

AT-D5 established that `sql_lineage_parser.parse_procedure_lineage` was
`_parse_sql` under a procedure-flavoured name (R11-X5 has since deleted that
alias; `sql_lineage_parser.parse_view_lineage` is the same flat parse): it
hands the *entire* procedure body to `sqlglot.parse` as if it were a flat
sequence of ordinary statements. That is provably wrong for a real
`CREATE PROCEDURE ... AS
BEGIN ... END` body -- sqlglot's tsql/oracle dialects do not understand
T-SQL/PL-SQL control-flow syntax (`IF`/`WHILE`/`BEGIN..END`/`LOOP`/cursor
`FOR` loops), and `sqlglot.parse` on such a body does not raise: it falls
back to an opaque `Command` node covering everything from the first
unrecognised token onward, silently discarding every statement after that
point. A caller relying on that flat parse gets a lineage
graph that is either right for a body with no control flow at all, or
silently truncated for any body that has some -- with nothing distinguishing
the two.

This module is a real, procedure-aware replacement. It never asks sqlglot to
parse a whole body in one call. Instead it:

  1. Strips the `CREATE [OR REPLACE] PROCEDURE ... AS/IS` header and the
     outer `BEGIN ... END` (or `$$ ... $$`) wrapper, quote/comment-aware
     (`_extract_body`).
  2. Splits the body into top-level, semicolon-delimited statement chunks,
     also quote/comment-aware so a `;` inside a string literal or a comment
     is never mistaken for a statement boundary (`_split_top_level_statements`).
  3. Peels recognised control-flow *headers* off the front of a chunk --
     `IF ... BEGIN`, `WHILE ... LOOP`, `ELSE`, bare `BEGIN`/`END`, PL/SQL
     `IF ... THEN`/`ELSIF ... THEN`/`CASE ... WHEN ... THEN`, T-SQL
     `BEGIN TRY`/`END CATCH`, and PL/SQL cursor `FOR rec IN (SELECT ...)
     LOOP` (whose parenthesised SELECT *does* carry real lineage and is
     extracted as its own read) -- leaving the real DML statement, if any,
     to parse on its own (`_peel_control_flow_prefix`).
  4. Classifies what is left. A bare structural leftover (`BEGIN`, `END`,
     `ELSE`, an empty remainder) carries no lineage and is skipped, not
     flagged -- there is genuinely nothing to report. Everything else is
     either dispatched to a per-statement-kind extractor (SELECT/INSERT/
     UPDATE/DELETE/MERGE/CREATE, reusing `sql_lineage_parser`'s own
     extraction where the shape already matches it, and new logic here for
     UPDATE/MERGE/DELETE and `SELECT ... INTO`, which
     `sql_lineage_parser._extract_from_statement` does not handle at all),
     or -- and this is the module's core invariant, extending INV-9 exactly
     the way AT-C4 already scoped it for lineage parsers -- explicitly
     marked **UNPARSED** with a named reason when it cannot be: dynamic SQL
     (`EXEC(@sql)`, `sp_executesql`, `EXECUTE IMMEDIATE`), a nested
     procedure call whose own body this pass does not descend into, a
     sqlglot parse error, or a statement shape sqlglot itself could not
     parse (its own `Command` fallback). An UNPARSED chunk is never
     silently dropped: it always produces its own `ProcedureLineageEdgeRecord`
     (`transformation_type="UNPARSED"`, `source_resolved=False`) carrying the
     reason, so a consumer reading only the edge list -- never a side-channel
     flag that is easy to ignore -- still sees the gap.
  5. Resolves intermediate writes. A write whose target is a T-SQL `#temp`/
     `##temp` table or `@table` variable, or a `SELECT ... INTO` target, is
     tagged `is_intermediate=True`. A later statement reading FROM that same
     intermediate additionally gets a synthesised *transitive* edge
     (`via_temp_table` set) linking the original upstream source straight to
     the real final target -- so "does the procedure's output ultimately
     depend on `orders.amount`" is answerable without the caller manually
     chasing every temp-table hop themselves. The direct hop-by-hop edges are
     kept alongside it, never replaced, so no evidence is lost either way.
     Iterated to a fixed point (bounded) so a two-hop temp chain
     (`orders -> #a -> #b -> final`) still collapses to one transitive edge.

What this module deliberately does NOT attempt (each surfaces as an explicit
UNPARSED marker on the statement that needed it, never a silent gap):
dynamic SQL of any form; a nested `EXEC other_proc` / `CALL other_proc(...)`
whose own body is not fetched and analyzed; PL/SQL collection/record variable
assignment (not SQL DML at all, so not a sqlglot construct in the first
place); cursor `OPEN`/`FETCH`/`CLOSE` (T-SQL) -- the *declaration*'s own
`SELECT` is captured if written as `DECLARE cur CURSOR FOR SELECT ...`, but
the fetch loop's per-row processing is not modeled beyond its own
statements; `TRY`/`CATCH` error-handling logic itself (its *contents* are
still walked as ordinary statements). See `Docs/90-reference/procedure-lineage-capability-matrix.md`
(AT-22, generated by `procedure_capability_matrix.py`) for the exhaustive,
code-derived version of this list.

PL/pgSQL (FP-07, 2026-09-15). A PostgreSQL routine whose declared language is
plpgsql -- or a body handed over as a bare `BEGIN ... END` block -- gets the
meanings PL/pgSQL gives keywords the other dialects use differently: `EXECUTE
<expr>` is dynamic SQL (never a nested call named after the expression),
`PERFORM fn(...)` is a nested call, `RETURN QUERY <query>` is the routine's
result set, and `SELECT ... INTO v`, `v := (<query>)` and `PERFORM <query>` read
tables into routine-local state: their edges target `PROCEDURE_LOCAL_TARGET`,
marked intermediate, so they are never mistaken for a table write or for the
result. `CREATE TEMP TABLE ... [ON COMMIT ...] AS` is an intermediate exactly
like a T-SQL `#temp`. A dollar-quoted body may carry any tag --
`pg_get_functiondef`, which the PostgreSQL connector stores, returns
`$function$`/`$procedure$`, not `$$`.

Triggers (R11-FP01, 2026-09-17). A trigger body is the same artifact as a
routine body and is parsed by the same walk -- `CREATE [OR REPLACE] TRIGGER` is
recognised as a header to strip, so a SQL Server or Oracle trigger's own
`BEGIN ... END` is walked rather than becoming one opaque `Command` chunk. It
has one thing a routine body does not: an **implicit subject**. `NEW`/`OLD`
(PostgreSQL), `INSERTED`/`DELETED` (SQL Server) are the firing table's row, and
the firing table is named nowhere in the body, so a body writing `audit` from
`NEW.*` states a path out of a table whose name the text does not contain. Only
the catalog knows which table that is, so `parse_trigger_lineage` takes it and
binds it to those names (`TRIGGER_SUBJECT_RELATIONS`) before any edge is built.
Where the binding cannot be made -- Oracle spells the same rows `:NEW`/`:OLD`,
which is bind-variable syntax sqlglot resolves to a placeholder, leaving no table
reference to bind -- the parse records an UNRESOLVED_TRIGGER_SUBJECT marker
rather than reporting a body whose sources it does not actually know.

Statement ranges (R11-FP07, 2026-09-18). Every edge now says *where* its
statement is, as a line/column span and a half-open character-offset range
(`StatementRange`), not only which statement it was (`statement_ordinal`).
**The offsets index the exact text this parser was handed** -- for every
persisted edge that is the stored, redacted body (`body_sql_redacted`), because
that is the only text `require_eligible_routine_body` ever gives it. They are
not offsets into the customer's source: the platform never holds those bytes
(R11-D16), and a lexical scrub that folds a multi-line literal into one
placeholder moves every line after it. So each located parse carries
`statement_text_digest`, the SHA-256 of that text -- the digest rule
`definition_history_api` already publishes for stored definitions -- and a
reader proves a range is current by digesting the stored body and comparing,
rather than trusting a number that may point into a body that has since moved.
The range comes from this module's own quote/comment-aware scanner, never from
sqlglot's token positions: sqlglot 30 puts `line`/`col`/`start`/`end` only on
identifier leaves, relative to the string *it* was given (the peeled, and for
PL/pgSQL rewritten, remainder), and `col` is the column of the token's last
character. The splitter and the control-flow peel already know where each
statement's text begins and ends in the body, so that is what is recorded.
`StatementRangeStatus` says what the range is the range *of*; a fact that is not
bound to a statement of this text (a body that could not be reached, the
body-level unresolved-trigger-subject marker) carries no range and says
`NOT_LOCATED` -- a missing range is never a range of zero.

Oracle package members (R11-FP03, 2026-09-18). A stored PACKAGE is its spec and
its body joined, and it used to be parsed as one body: every member's reads and
writes were the package's, and the `PACKAGE BODY ... AS PROCEDURE p IS BEGIN`
header glued the first statement of the first member into an unparseable chunk.
`_package_layout` now finds each member subprogram of the package body with a
block matcher (BEGIN/CASE open, END closes, `END IF|LOOP|WHILE` close nothing
this counts), each member's body is walked on its own -- its own hop
propagation, since one member's temp state is not another's -- and its edges
carry `package_member` and `member_attribution=MEMBER`. The package's own code
(spec declarations, package-level declarations, the initialization block) is
`PACKAGE_LEVEL`. When the text cannot be split -- no package body in it, blocks
that do not balance (a truncated body), a member header this scanner cannot
read -- the whole text is parsed exactly as before and every edge says
`PACKAGE_FALLBACK`, with the reason on the result: never a silent mix of
member-grain and package-grain facts. A member's declaration section is not
walked, which is the treatment every standalone routine's declaration section
already gets from `_extract_body`.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from aida.sql_lineage_parser import (
    _SQLGLOT_AVAILABLE,
    _SQLGLOT_DIALECT_MAP,
    PROCEDURE_RESULT_TARGET,
    STAR_COLUMN_MARKER,
    UNRESOLVED_TABLE,
    Confidence,
    LineageEdge,
    TransformationType,
    _classify_transformation,
    _compute_sql_hash,
    _extract_edges_from_select,
    _extract_from_statement,
    _extract_source_columns,
    _extract_star_edges,
    _extract_target_table,
    _has_aggregate_functions,
    _resolve_or_mark_unresolved,
    _resolve_table_name,
)

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel
except ImportError:  # pragma: no cover -- see _SQLGLOT_AVAILABLE above
    pass


# ---------------------------------------------------------------------------
# UNPARSED: the reason a chunk this module could not resolve is named. Never
# used as a `str` the caller has to string-compare by convention -- every
# unparsed chunk carries one of these as `ParsedStatement.unparsed_reason`
# (prefix) plus, where useful, a short suffix with the specific detail
# (parse error text, node type name, callee name).
# ---------------------------------------------------------------------------
class UnparsedReason(StrEnum):
    DYNAMIC_SQL = "DYNAMIC_SQL"
    NESTED_PROCEDURE_CALL = "NESTED_PROCEDURE_CALL"
    # R11-FP07: a source that is a table-valued function, not a table.
    TABLE_FUNCTION_READ = "TABLE_FUNCTION_READ"
    UNSUPPORTED_STATEMENT_SHAPE = "UNSUPPORTED_STATEMENT_SHAPE"
    PARSE_ERROR = "PARSE_ERROR"
    UNRESOLVED_CONTROL_FLOW = "UNRESOLVED_CONTROL_FLOW"
    # R11-FP01: a trigger body's implicit subject -- the firing table's row --
    # could not be bound on this engine, so the reads attributed to it are not
    # stated rather than guessed. See `parse_trigger_lineage`.
    UNRESOLVED_TRIGGER_SUBJECT = "UNRESOLVED_TRIGGER_SUBJECT"


# Marker `transformation_type` for an UNPARSED edge -- deliberately not added
# to `sql_lineage_parser.TransformationType` (that module is not touched by
# this one at all); `LineageEdge.transformation_type` is a plain `str`, so
# this needs no change there.
UNPARSED_TRANSFORMATION_TYPE: Final[str] = "UNPARSED"

# Placeholder source/target for an UNPARSED chunk: neither side is known, so
# neither is a name that could collide with a real table.
UNPARSED_MARKER: Final[str] = "<UNPARSED>"

# Target of a query whose rows stay inside the routine -- a PL/pgSQL variable
# assignment or a discarded `PERFORM` row set. Not a table, not the routine's
# result: edges into it are always non-write intermediates.
PROCEDURE_LOCAL_TARGET: Final[str] = "<LOCAL>"


# ---------------------------------------------------------------------------
# R11-FP01: a trigger body's implicit subject. See the module docstring.
# ---------------------------------------------------------------------------
#: What each dialect calls the firing table's row inside a trigger body. The
#: values are the *names a body writes*, not tables: `trigger_subject_aliases`
#: turns them into alias entries pointing at the firing table the catalog holds.
TRIGGER_SUBJECT_RELATIONS: Final[dict[str, tuple[str, ...]]] = {
    "postgres": ("NEW", "OLD"),
    "tsql": ("INSERTED", "DELETED"),
}
#: Oracle's `:NEW.col` / `:OLD.col`. Deliberately not in the map above: sqlglot
#: reads the leading colon as a bind-variable placeholder, so the parsed
#: statement carries no table-qualified reference for any binding to attach to.
#: A body that uses one is reported unresolved, never parsed as though the row
#: reference were absent.
_BIND_SUBJECT_RE = re.compile(r":\s*(?:NEW|OLD)\b\s*\.", re.IGNORECASE)
#: Any spelling of a firing-row reference, for a dialect this module has no
#: vocabulary for at all.
_ANY_SUBJECT_RE = re.compile(
    r"(?::\s*)?\b(?:NEW|OLD|INSERTED|DELETED)\b\s*\.", re.IGNORECASE
)


def trigger_subject_aliases(dialect: str, firing_table: str) -> dict[str, str]:
    """Alias entries binding this dialect's firing-row names to `firing_table`.

    Registered in every case a body may have written, because alias resolution
    (`sql_lineage_parser._resolve_alias_to_table`) is an exact dictionary
    lookup on the identifier as the source spelled it. Empty for a dialect with
    no firing-row vocabulary here, which `unbound_trigger_subject` reports.
    """
    aliases: dict[str, str] = {}
    for name in TRIGGER_SUBJECT_RELATIONS.get(dialect, ()):
        for spelling in (name, name.lower(), name.capitalize()):
            aliases[spelling] = firing_table
    return aliases


def unbound_trigger_subject(sql: str, dialect: str) -> bool:
    """True when `sql` refers to the firing row in a form no binding can reach.

    Only the *presence* of the reference is read; the text is never returned,
    stored or quoted (INV-6).
    """
    if dialect not in TRIGGER_SUBJECT_RELATIONS:
        return bool(_ANY_SUBJECT_RE.search(sql))
    return bool(_BIND_SUBJECT_RE.search(sql))


def unbound_subject_aliases(dialect: str) -> dict[str, str]:
    """This dialect's firing-row names, bound to nothing.

    Applied to every parse that is *not* a trigger's, which is what a PostgreSQL
    trigger function's parse is: it lives on the routine axis, its body says
    `NEW.customer_id`, and no firing table is in sight -- the same function may be
    attached to several tables, so the routine axis cannot resolve the reference
    even in principle. Without this the qualifier resolved to itself, because
    `sql_lineage_parser._resolve_or_mark_unresolved` treats any non-empty
    reference as resolved, and the parse reported an edge out of a table called
    `NEW`. An invented table is worse than an admitted gap (INV-9), so the empty
    binding makes it honestly UNRESOLVED instead.
    """
    return {
        spelling: ""
        for name in TRIGGER_SUBJECT_RELATIONS.get(dialect, ())
        for spelling in (name, name.lower(), name.capitalize())
    }


def _bind_subject(aliases: dict[str, str], subject: Mapping[str, str]) -> None:
    """Point every reference to a firing-row name at the firing table, or, where
    there is no firing table, at nothing.

    Two steps, because a body may name the row directly (`NEW.amount`) or behind
    an alias the statement itself declared (`FROM inserted i`, then `i.amount`,
    where the walk above has already recorded `i -> inserted`): an alias whose
    target is a firing-row name is remapped first, then the names themselves are
    added. Applied after the walk so a real binding wins over the raw name.

    An *empty* binding never overwrites what the walk found, because a routine on
    an engine whose firing-row name is an ordinary identifier may legitimately
    select from a table of that name -- `FROM inserted` outside a T-SQL trigger is
    a real table. The empty binding only fills a name the walk saw no table for.
    """
    bound = {name.lower(): table for name, table in subject.items() if table}
    for key, value in list(aliases.items()):
        table = bound.get(value.lower())
        if table is not None:
            aliases[key] = table
    for name, table in subject.items():
        if table:
            aliases[name] = table
        else:
            aliases.setdefault(name, "")


# ---------------------------------------------------------------------------
# R11-FP07: where a statement is in the text that was parsed. See the module
# docstring for which text that is and why the range is the scanner's own.
# ---------------------------------------------------------------------------


class StatementRangeStatus(StrEnum):
    """What an edge's `statement_range` is the range *of*.

    Four codes because an edge's relation to the text is not always "this
    statement produced it", and a reader pointed at a range has to know which
    case it is looking at.
    """

    #: The statement the edge was read from -- or, for a transitive edge through
    #: an intermediate, the statement `statement_ordinal` names (the one that
    #: writes its target), which is exactly the attribution the ordinal makes.
    STATEMENT = "STATEMENT"
    #: An UNPARSED marker: the statement where the gap is. For a statement the
    #: parser could not read at all, the span is where that unread text is; its
    #: boundaries are the splitter's, since there was no parse to confirm them.
    GAP_STATEMENT = "GAP_STATEMENT"
    #: Read from a routine this one calls (`via_routine`): the range is the call
    #: in *this* body. The statement that reads or writes is in the callee's body,
    #: whose own offsets would index a different text, so they are not carried.
    CALL_SITE = "CALL_SITE"
    #: No range. The fact is not bound to a statement of this text -- a body that
    #: could not be reached, or a finding about the body as a whole.
    NOT_LOCATED = "NOT_LOCATED"


@dataclass(frozen=True, slots=True)
class StatementRange:
    """A statement's span in the parsed text. Positions, never content (INV-6).

    Offsets are code-point indices, half-open: `text[start_offset:end_offset]`
    is the statement. Lines and columns are 1-based; `end_line`/`end_column`
    locate the statement's last character, so a one-line statement reads
    `start_line == end_line`. A line ends at `\\n`, so a CRLF body counts lines
    the same way; a bare CR is not a line break.
    """

    start_offset: int
    end_offset: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int


class _Locator:
    """Offsets to lines and columns over one text, computed once per parse."""

    __slots__ = ("_length", "_line_starts")

    def __init__(self, text: str) -> None:
        self._length = len(text)
        self._line_starts = [0, *(index + 1 for index, ch in enumerate(text) if ch == "\n")]

    def span(self, start: int, end: int) -> StatementRange | None:
        # An empty or out-of-text span is not a location. Returning one would be
        # the "range of zero" a missing range must never be mistaken for.
        if start < 0 or end <= start or end > self._length:
            return None
        last = end - 1
        start_line = bisect_right(self._line_starts, start) - 1
        end_line = bisect_right(self._line_starts, last) - 1
        return StatementRange(
            start_offset=start,
            end_offset=end,
            start_line=start_line + 1,
            start_column=start - self._line_starts[start_line] + 1,
            end_line=end_line + 1,
            end_column=last - self._line_starts[end_line] + 1,
        )


def statement_text_digest(text: str) -> str:
    """SHA-256 of the text a parse's ranges index -- the stored body.

    The same rule `definition_history_api._digest` and
    `context_product_coverage._digest` publish for a stored definition, so a
    reader holding a range and the routine's current `body_sql_redacted` can
    prove the range still points into that text: digest it and compare. A
    digest of value-free text is not text (INV-6)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# R11-FP03: which member of an Oracle package an edge belongs to.
# ---------------------------------------------------------------------------


class MemberAttribution(StrEnum):
    """The grain an edge from a PACKAGE's body is attributed at. `None` on an
    edge from anything that is not a package."""

    #: Inside the member subprogram `package_member` names.
    MEMBER = "MEMBER"
    #: The package's own code, outside every member: spec declarations,
    #: package-level declarations, the initialization block.
    PACKAGE_LEVEL = "PACKAGE_LEVEL"
    #: The package could not be split into members, so which member (if any)
    #: holds this statement is not known. Recorded on every edge of that parse,
    #: never mixed with MEMBER edges from the same parse.
    PACKAGE_FALLBACK = "PACKAGE_FALLBACK"


class PackageSplitFailure(StrEnum):
    """Why a package's text could not be split into members."""

    #: The text holds no `PACKAGE BODY` -- a spec whose body was not visible.
    NO_PACKAGE_BODY = "NO_PACKAGE_BODY"
    #: A BEGIN/CASE/END or a parenthesis does not close -- typically a body the
    #: source truncated -- so member boundaries would be guesses.
    UNBALANCED_BLOCKS = "UNBALANCED_BLOCKS"
    #: A PROCEDURE/FUNCTION at package level whose name this scanner cannot read
    #: (a quoted identifier, which the scanner steps over as a string).
    UNREADABLE_MEMBER = "UNREADABLE_MEMBER"


@dataclass(frozen=True, slots=True)
class PackageMember:
    """One member subprogram a package body defines, as the parse found it.

    `parameter_names` is how the package body spells the member's parameters,
    in order: identifiers, never defaults (a default can be a literal). PL/SQL
    requires a body's header to repeat its spec's parameter names, which is what
    lets `routine_lineage_edges.resolve_package_member_ids` tell two overloads
    of one name apart against the captured members' parameters.
    `first_ordinal`/`last_ordinal` bound the statements walked from this
    member's body; both `None` for a member whose body holds none.
    """

    name: str
    kind: str
    parameter_names: tuple[str, ...]
    start_offset: int
    end_offset: int
    first_ordinal: int | None
    last_ordinal: int | None


@dataclass(frozen=True, slots=True)
class ProcedureLineageEdgeRecord:
    """One column-level (or, for `UNPARSED`, statement-level) lineage fact
    extracted from a procedure body."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    transformation_type: str
    confidence: str
    dialect: str
    source_resolved: bool
    statement_ordinal: int
    is_write: bool
    is_intermediate: bool
    control_flow_context: str | None = None
    unparsed_reason: str | None = None
    # Set only on a synthesised transitive edge: the intermediate (temp
    # table/variable) name this edge's source->target link was resolved
    # *through*, so a consumer can always tell a direct hop from a
    # multi-statement derivation rather than the two looking identical.
    via_temp_table: str | None = None
    # R11-FP07: set only on an edge read from a routine this one calls -- that callee's
    # qualified name (`aida.routine_call_descent`).
    via_routine: str | None = None
    # R11-FP07 source-range maps: where `statement_ordinal`'s statement is in the
    # text this parse read, what the range is the range of, and the digest of that
    # text. Defaults are the honest "not located" -- an edge built on a path that
    # has no text to point into never claims a position.
    statement_range: StatementRange | None = None
    statement_range_status: str = StatementRangeStatus.NOT_LOCATED.value
    statement_text_digest: str | None = None
    # R11-FP03: for an Oracle package's parse, the member this edge belongs to and
    # the grain it is attributed at (`MemberAttribution`). Both `None` on an edge
    # from any other routine.
    package_member: str | None = None
    member_attribution: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """One top-level statement chunk of a procedure body, after control-flow
    peeling and classification -- the unit `find_single_read_only_result_statement`
    (procedure_tool_blueprint.py, N12) and `parse_procedure_lineage` are both
    built from."""

    ordinal: int
    is_write: bool
    is_unparsed: bool
    is_no_lineage: bool  # DECLARE/SET/session-config -- genuinely nothing to report
    unparsed_reason: str | None
    control_flow_context: str | None
    target_table: str | None
    is_intermediate_target: bool
    node: exp.Expr | None  # the parsed sqlglot node, or None if not resolved to one
    edges: tuple[ProcedureLineageEdgeRecord, ...]
    #: R11-FP07: this statement's span in the parsed text; `None` until located.
    statement_range: StatementRange | None = None


@dataclass(slots=True)
class ProcedureParseResult:
    """Result of a procedure-aware lineage parse."""

    edges: list[ProcedureLineageEdgeRecord] = field(default_factory=list)
    statement_count: int = 0
    confidence: str = Confidence.LOW.value
    dialect: str = ""
    sql_hash: str = ""
    errors: list[str] = field(default_factory=list)
    # True iff every statement chunk in the body was either resolved to a
    # concrete DML/DDL shape or recognised as genuinely lineage-free
    # (DECLARE/SET/structural control-flow keyword) -- i.e. zero UNPARSED
    # chunks. This is the "every branch accounted for" signal N12 requires;
    # it is never inferred from an empty edge list, only from this.
    is_fully_parsed: bool = False
    # True iff `is_fully_parsed` AND no statement touched INSERT/UPDATE/
    # DELETE/MERGE/CREATE (any DDL/DML write) -- i.e. proven read-only, not
    # merely "no write statement happened to be found". See the module
    # docstring and N12 in the tracker.
    is_read_only: bool = False
    # R11-FP07: the digest of the text every located edge's range indexes;
    # `None` when nothing was located (no body was reached).
    statement_text_digest: str | None = None
    # R11-FP03: for an Oracle package, `MEMBER` when its body was split into
    # members and `PACKAGE_FALLBACK` when it was not, with the
    # `PackageSplitFailure` code saying why; all three unset for anything else.
    member_attribution: str | None = None
    member_fallback_reason: str | None = None
    package_members: tuple[PackageMember, ...] = ()


# ---------------------------------------------------------------------------
# Step 1: strip the CREATE PROCEDURE/FUNCTION header and outer BEGIN..END (or
# $$..$$) wrapper, quote/comment-aware. If no such wrapper is recognised the
# whole input is treated as already being the body -- matching the convention
# the removed `sql_lineage_parser.parse_procedure_lineage` documented, that
# a caller may hand in an already-unwrapped body.
# ---------------------------------------------------------------------------

# R11-FP07: `DO $$ ... $$` is an anonymous block -- a body with no name, whose statements
# are read exactly like a routine's.
# R11-FP01: `TRIGGER` joins them. A SQL Server or Oracle trigger keeps its code
# in the trigger itself, and without this its whole `CREATE TRIGGER ... AS BEGIN`
# header became one opaque `Command` chunk -- an UNPARSED marker on a body that
# is in fact perfectly readable once the header is off.
_HEADER_RE = re.compile(
    r"^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:PROCEDURE|FUNCTION|TRIGGER)|DO)\b",
    re.IGNORECASE,
)
#: R11-FP03: an Oracle subprogram as ALL_SOURCE keeps it -- `PROCEDURE p IS ...`
#: with no `CREATE`, which is what the Oracle connector stores for a standalone
#: routine and exactly the shape of a package member -- or as DBMS_METADATA spells
#: it, with `EDITIONABLE`. Without this the header was never stripped: the
#: declaration section and the first statement after `BEGIN` became one chunk,
#: an UNPARSED marker on a statement that parses perfectly well on its own.
#: Applied to the `oracle` dialect only; no other engine stores a bare header.
_ORACLE_SOURCE_HEADER_RE = re.compile(
    r"^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?)?(?:PROCEDURE|FUNCTION)\b",
    re.IGNORECASE,
)
#: R11-FP03: the text of an Oracle PACKAGE -- spec, body, or the two joined, as
#: the Oracle connector stores it -- which `_package_layout` splits into members.
_PACKAGE_TEXT_RE = re.compile(
    r"^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?)?PACKAGE\b",
    re.IGNORECASE,
)
#: T-SQL inline table-valued function: its body is the query of `AS RETURN ( ... )`.
_TSQL_INLINE_RETURN_RE = re.compile(r"\bAS\s+RETURN\s*\(", re.IGNORECASE)
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_LANGUAGE_RE = re.compile(r"\bLANGUAGE\s+'?([A-Za-z0-9_]+)'?", re.IGNORECASE)


def _dollar_quoted_body(sql: str) -> tuple[int, int] | None:
    """`(start, end)` of the first dollar-quoted span's content, whatever its tag."""
    tag = _DOLLAR_TAG_RE.search(sql)
    if tag is None:
        return None
    close = sql.find(tag.group(0), tag.end())
    return None if close == -1 else (tag.end(), close)


def _scan_tokens(sql: str) -> list[tuple[int, int, str]]:
    """Scan `sql` once, quote/comment-aware, yielding `(start, end, kind)`
    spans for the meaningful pieces: `"word"` for a bare identifier/keyword
    run, `"other"` for a single significant character (`;`, `(`, `)`), and
    nothing at all for whitespace, comments, or the *inside* of a quoted
    literal/identifier (so a keyword or `;` inside a string can never be
    mistaken for a real one). Shared by the body extractor and the
    statement splitter so both agree on what counts as "inside a string".
    """
    tokens: list[tuple[int, int, str]] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql[j : j + 2] != "''":
                    j += 1
                    break
                j = j + 2 if sql[j : j + 2] == "''" else j + 1
            i = j
            continue
        if ch == '"' or ch == "[":
            close = '"' if ch == '"' else "]"
            j = sql.find(close, i + 1)
            i = n if j == -1 else j + 1
            continue
        if ch == "$" and (tag := _DOLLAR_TAG_RE.match(sql, i)):
            j = sql.find(tag.group(0), tag.end())
            i = n if j == -1 else j + len(tag.group(0))
            continue
        if ch.isalpha() or ch == "_" or ch == "@" or ch == "#":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_@#$"):
                j += 1
            tokens.append((i, j, "word"))
            i = j
            continue
        if ch in ";()":
            tokens.append((i, i + 1, "other" if ch != ";" else ";"))
            i += 1
            continue
        i += 1
    return tokens


def _has_routine_header(sql: str, dialect: str | None) -> bool:
    return bool(_HEADER_RE.match(sql)) or (
        dialect == "oracle" and bool(_ORACLE_SOURCE_HEADER_RE.match(sql))
    )


def _extract_body_span(sql: str, dialect: str | None = None) -> tuple[int, int]:
    """`(start, end)` of the body inside a `CREATE PROCEDURE/FUNCTION ... AS/IS
    BEGIN ... END` (or `$$ ... $$`) wrapper. Best-effort: falls back to the whole
    input when the header/wrapper is not clearly recognised, which is always safe
    (the statement splitter below still walks it correctly; a genuinely malformed
    or unrecognised header just becomes its own UNPARSED chunk rather than
    crashing anything).

    A span rather than the text since R11-FP07, so every statement found inside
    the body can be located in the text the caller handed over -- the body is
    where the splitter starts counting, and the wrapper is what it skipped.
    """
    whole = (0, len(sql))
    if not _has_routine_header(sql, dialect):
        return whole

    tokens = _scan_tokens(sql)
    # Postgres/PL-pgSQL: CREATE FUNCTION ... AS $$ ... $$ LANGUAGE plpgsql, or
    # the `$function$`/`$procedure$` tags `pg_get_functiondef` really returns.
    body_span = _dollar_quoted_body(sql)
    if body_span is not None:
        inner_start, inner_end = body_span
        inner = sql[inner_start:inner_end]
        # The dollar-quoted body may itself be a BEGIN..END block; if so
        # strip that too so plpgsql matches the T-SQL/PL-SQL shape. An empty
        # block keeps the dollar-quoted text, as it always did.
        stripped = _begin_end_span(inner, _scan_tokens(inner))
        if stripped is None or stripped[0] == stripped[1]:
            return body_span
        return inner_start + stripped[0], inner_start + stripped[1]
    stripped = _begin_end_span(sql, tokens)
    if stripped is not None:
        return stripped
    # A T-SQL inline table-valued function has no BEGIN..END at all.
    if match := _TSQL_INLINE_RETURN_RE.search(sql):
        opened = match.end() - 1
        closed = _matching_paren(sql, opened)
        if closed is not None:
            return opened + 1, closed
    return whole


def _extract_body(sql: str, dialect: str | None = None) -> str:
    start, end = _extract_body_span(sql, dialect)
    return sql[start:end]


def _strip_begin_end(text: str) -> str | None:
    return _strip_begin_end_from_tokens(text, _scan_tokens(text))


#: Closers of a construct no opener was counted for: `END IF`, `END LOOP`,
#: `END WHILE` close an IF/LOOP/WHILE, which never opened a level here.
_NON_BEGIN_END_CLOSERS = {"LOOP", "IF", "WHILE"}


def _matching_end(words: list[tuple[int, int, str]], begin: int) -> int | None:
    """Index in `words` of the `END` that closes the block opened at `begin`.

    `words` are upper-cased word tokens. `BEGIN` opens a level, and so does
    `CASE` -- a CASE *expression* (`CASE WHEN ... END`) is closed by a bare
    `END`, and a CASE *statement* by `END CASE`, whose trailing CASE opens
    nothing. `END IF`/`END LOOP`/`END WHILE` close constructs that never opened
    a level, so they are skipped.

    R11-FP03 found the CASE half of this missing: CASE opened nothing, so a CASE
    expression's `END` closed the enclosing `BEGIN` -- `SELECT CASE WHEN ... END`
    anywhere in a procedure body silently truncated the body at that point, and
    every statement after it was never seen. Shared by the body stripper and the
    package-member splitter, so the two can never disagree on where a block ends.
    """
    depth = 0
    index = begin
    while index < len(words):
        word = words[index][2]
        following = words[index + 1][2] if index + 1 < len(words) else None
        if word in ("BEGIN", "CASE"):
            depth += 1
        elif word == "END":
            if following in _NON_BEGIN_END_CLOSERS:
                index += 2
                continue
            depth -= 1
            if depth == 0:
                return index
            if following == "CASE":
                index += 2
                continue
        index += 1
    return None


def _begin_end_span(sql: str, tokens: list[tuple[int, int, str]]) -> tuple[int, int] | None:
    """`(start, end)` strictly between the first top-level `BEGIN` and its
    matching `END`, or None if there is no `BEGIN` or it never closes.

    A compound closer (`END IF`/`END LOOP`/`END WHILE`) must NOT decrement the
    counter: nothing incremented it for that construct in the first place, and
    treating it as if it did closes the outer `BEGIN...END` early -- silently
    truncating everything after a `LOOP`/`IF` nested inside the procedure body,
    which is exactly the silent-truncation failure mode this module exists to
    avoid. See `_matching_end` for CASE.
    """
    words = [
        (start, end, sql[start:end].upper()) for start, end, kind in tokens if kind == "word"
    ]
    begin = next((index for index, word in enumerate(words) if word[2] == "BEGIN"), None)
    if begin is None:
        return None
    close = _matching_end(words, begin)
    if close is None:
        return None
    return words[begin][1], words[close][0]


def _strip_begin_end_from_tokens(sql: str, tokens: list[tuple[int, int, str]]) -> str | None:
    """The text strictly between the first top-level `BEGIN` and its matching
    `END` (`_begin_end_span`), or None."""
    span = _begin_end_span(sql, tokens)
    return None if span is None else sql[span[0] : span[1]]


# ---------------------------------------------------------------------------
# Step 2: split the body into top-level, `;`-delimited statement chunks.
# ---------------------------------------------------------------------------


def _skip_trivia(text: str, index: int, end: int) -> int:
    """The first index at or after `index` that is neither whitespace nor inside a
    comment, or `end` if there is none before it."""
    while index < end:
        if text[index].isspace():
            index += 1
        elif text.startswith("--", index):
            close = text.find("\n", index, end)
            index = end if close == -1 else close + 1
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2, end)
            index = end if close == -1 else close + 2
        else:
            return index
    return end


def _split_top_level_statement_spans(body: str) -> list[tuple[int, int]]:
    """`(start, end)` of every top-level `;`-delimited chunk of `body`, trimmed
    of surrounding whitespace, empty chunks dropped. Spans rather than strings
    since R11-FP07, so a statement can be located in the body it came from.

    Leading comments are trimmed too, and a chunk of nothing but comments is
    dropped. R11-FP07 found both: a range began at the comment above its
    statement rather than at the statement, and a comment after a body's last
    `;` became a chunk sqlglot could not parse -- a PARSE_ERROR marker quoting
    the comment, on a body with nothing unread in it. A leading comment also hid
    an `IF`/`WHILE` header from the control-flow peel, which only looks at the
    front of the chunk."""
    raw: list[tuple[int, int]] = []
    chunk_start = 0
    for start, end, kind in _scan_tokens(body):
        if kind == ";":
            raw.append((chunk_start, start))
            chunk_start = end
    raw.append((chunk_start, len(body)))
    spans: list[tuple[int, int]] = []
    for start, end in raw:
        first = _skip_trivia(body, start, end)
        if first >= end:
            continue
        last = end
        while last > first and body[last - 1].isspace():
            last -= 1
        spans.append((first, last))
    return spans


def _split_top_level_statements(body: str) -> list[str]:
    return [body[start:end] for start, end in _split_top_level_statement_spans(body)]


# ---------------------------------------------------------------------------
# Step 3: peel a recognised control-flow header off the front of a chunk.
# ---------------------------------------------------------------------------

_STRUCTURAL_ONLY_RE = re.compile(
    r"^\s*(BEGIN\s+TRY|END\s+TRY|BEGIN\s+CATCH|END\s+CATCH|BEGIN|END(?:\s+(?:IF|LOOP|WHILE|CASE))?|ELSE)\s*$",
    re.IGNORECASE,
)
_IF_BEGIN_RE = re.compile(r"^\s*IF\b(?P<cond>.*?)\bBEGIN\b\s*", re.IGNORECASE | re.DOTALL)
_IF_THEN_RE = re.compile(
    r"^\s*(?:IF|ELSIF|ELSEIF)\b(?P<cond>.*?)\bTHEN\b\s*", re.IGNORECASE | re.DOTALL
)
_ELSE_RE = re.compile(r"^\s*ELSE\b\s*", re.IGNORECASE)
_WHILE_BEGIN_RE = re.compile(r"^\s*WHILE\b(?P<cond>.*?)\bBEGIN\b\s*", re.IGNORECASE | re.DOTALL)
_WHILE_LOOP_RE = re.compile(r"^\s*WHILE\b(?P<cond>.*?)\bLOOP\b\s*", re.IGNORECASE | re.DOTALL)
_CURSOR_FOR_LOOP_RE = re.compile(
    r"^\s*FOR\s+\w+\s+IN\s*\((?P<select>.*)\)\s*LOOP\s*", re.IGNORECASE | re.DOTALL
)
_BARE_FOR_LOOP_RE = re.compile(r"^\s*FOR\b.*?\bLOOP\b\s*", re.IGNORECASE | re.DOTALL)
# PL/pgSQL `FOR rec IN SELECT ... LOOP` -- the query is not parenthesised, so
# without this the bare-FOR peel above would discard it silently. `IN EXECUTE`
# is handed on too, and classified as dynamic SQL.
_FOR_IN_QUERY_LOOP_RE = re.compile(
    r"^\s*FOR\s+[A-Za-z_][\w$]*(?:\s*,\s*[A-Za-z_][\w$]*)*\s+IN\s+"
    r"(?P<select>(?:SELECT|WITH|EXECUTE)\b.*?)\s*\bLOOP\b\s*",
    re.IGNORECASE | re.DOTALL,
)
_BARE_LOOP_RE = re.compile(r"^\s*LOOP\b\s*", re.IGNORECASE)
# A bare BEGIN/END with more text following in the same chunk: T-SQL does
# not require a `;` after a bare BEGIN/END, so the statement splitter (which
# only splits on `;`) legitimately produces e.g. "END\n\nINSERT INTO ..." as
# one raw chunk when the source omits that optional semicolon -- these peel
# the leading structural keyword off so the real statement underneath still
# gets classified, rather than the whole chunk falling through to UNPARSED.
_BARE_BEGIN_MID_RE = re.compile(r"^\s*BEGIN\s+", re.IGNORECASE)
_BARE_END_MID_RE = re.compile(r"^\s*END(?:\s+(?:IF|LOOP|WHILE|CASE|TRY|CATCH))?\s+", re.IGNORECASE)
_CASE_WHEN_THEN_RE = re.compile(r"^\s*(?:CASE\s*)?WHEN\b.*?\bTHEN\b\s*", re.IGNORECASE | re.DOTALL)
# PL/SQL and PL/pgSQL `EXCEPTION WHEN <condition> THEN`: the handler's statements
# are walked like any branch; later `WHEN ... THEN` handlers peel as CASE_BRANCH.
_EXCEPTION_WHEN_RE = re.compile(
    r"^\s*EXCEPTION\s+WHEN\b.*?\bTHEN\b\s*", re.IGNORECASE | re.DOTALL
)
_MAX_PEEL_ITERATIONS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class _PeelResult:
    remainder: str
    control_flow_context: str | None
    cursor_loop_source_sql: str | None
    #: R11-FP07: where `remainder` starts in the chunk it was peeled from. The peel
    #: only ever cuts from the front and trims, so the remainder is a contiguous
    #: piece of the chunk and this one number locates it.
    remainder_offset: int = 0
    #: ... and where `cursor_loop_source_sql` starts, when there is one.
    cursor_offset: int | None = None


def _peel_control_flow_prefix(chunk: str) -> _PeelResult:
    """Repeatedly strip a recognised control-flow header from the front of
    `chunk`. Returns the leftover text (empty if the chunk was purely
    structural), the innermost control-flow context peeled (for evidence),
    and -- for a PL/SQL cursor `FOR ... IN (SELECT ...) LOOP` -- the
    parenthesised SELECT text, which carries real lineage of its own and is
    extracted separately by the caller. Both carry their offset in `chunk`
    (R11-FP07), counted as each header is cut off.
    """
    if _STRUCTURAL_ONLY_RE.match(chunk):
        return _PeelResult("", None, None, len(chunk))

    remainder = chunk
    consumed = 0
    context: str | None = None
    cursor_sql: str | None = None
    cursor_offset: int | None = None
    for _ in range(_MAX_PEEL_ITERATIONS):
        # A comment between two headers (`IF x BEGIN -- why` then the statement) is
        # not the statement; skipping it keeps each header visible to the next peel
        # and starts the remainder -- and its range -- at the statement itself.
        skipped = _skip_trivia(remainder, 0, len(remainder))
        consumed += skipped
        remainder = remainder[skipped:]
        if match := _CURSOR_FOR_LOOP_RE.match(remainder):
            cursor_sql = match.group("select")
            cursor_offset = consumed + match.start("select")
            context = "CURSOR_FOR_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _IF_BEGIN_RE.match(remainder):
            context = "IF_BRANCH"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _IF_THEN_RE.match(remainder):
            context = "IF_BRANCH"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _WHILE_BEGIN_RE.match(remainder):
            context = "WHILE_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _WHILE_LOOP_RE.match(remainder):
            context = "WHILE_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _EXCEPTION_WHEN_RE.match(remainder):
            context = "EXCEPTION_HANDLER"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _CASE_WHEN_THEN_RE.match(remainder):
            context = "CASE_BRANCH"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _FOR_IN_QUERY_LOOP_RE.match(remainder):
            cursor_sql = match.group("select")
            cursor_offset = consumed + match.start("select")
            context = "CURSOR_FOR_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _BARE_FOR_LOOP_RE.match(remainder):
            context = "FOR_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _ELSE_RE.match(remainder):
            context = "ELSE_BRANCH"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _BARE_LOOP_RE.match(remainder):
            context = context or "LOOP_BLOCK"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _BARE_BEGIN_MID_RE.match(remainder):
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _BARE_END_MID_RE.match(remainder):
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if _STRUCTURAL_ONLY_RE.match(remainder):
            remainder = ""
            break
        break
    lead = len(remainder) - len(remainder.lstrip())
    return _PeelResult(remainder.strip(), context, cursor_sql, consumed + lead, cursor_offset)


# ---------------------------------------------------------------------------
# Step 3b: recognise dynamic SQL / nested procedure calls before attempting a
# generic sqlglot parse -- both would otherwise either fail to parse at all
# (EXECUTE IMMEDIATE under the oracle dialect) or parse "successfully" into a
# shape with no table references at all (`EXEC(@sql)`), silently producing
# zero edges either way with nothing flagging the gap.
# ---------------------------------------------------------------------------

_DYNAMIC_SQL_RE = re.compile(
    r"\bEXECUTE\s+IMMEDIATE\b|\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(", re.IGNORECASE
)
_NESTED_CALL_RE = re.compile(
    r"^\s*(?:EXEC(?:UTE)?|CALL)\s+([A-Za-z_][\w.$#]*)", re.IGNORECASE
)
_NO_LINEAGE_KEYWORDS_RE = re.compile(
    r"^\s*(SET|DECLARE|EXIT|CONTINUE|LEAVE|GOTO|RAISERROR|RAISE|PRINT|THROW|COMMIT|"
    r"ROLLBACK|SAVEPOINT|OPEN|CLOSE|FETCH|DBMS_OUTPUT|GET\s+DIAGNOSTICS)\b",
    re.IGNORECASE,
)
#: R11-FP03: an Oracle subprogram *declaration* -- `PROCEDURE p(a NUMBER)` or
#: `FUNCTION f RETURN NUMBER`, with no IS/AS, ended by `;`. In a package spec it is
#: the member list; in a body it is a forward declaration. Either way the member's
#: code is elsewhere in the same text and is read there, so the declaration carries
#: no lineage of its own. Before this every line of a package spec was a PARSE_ERROR
#: marker, and no package could ever read as fully parsed.
_SUBPROGRAM_DECLARATION_RE = re.compile(
    r"^\s*(?:PROCEDURE|FUNCTION)\s+[A-Za-z_][\w$#]*\b(?!.*\b(?:IS|AS)\b).*$",
    re.IGNORECASE | re.DOTALL,
)
#: R11-FP03: PL/SQL `RETURN <expression>`. PL/SQL admits no subquery in an
#: expression outside a SQL statement (PLS-00405), so a RETURN reads no table;
#: sqlglot's oracle grammar nonetheless fails on `RETURN 0`, which made every
#: packaged function returning a constant carry a PARSE_ERROR marker. Oracle only:
#: T-SQL's `RETURN (SELECT ...)` does read a table and is parsed as before.
_PLSQL_RETURN_RE = re.compile(r"^\s*RETURN\b", re.IGNORECASE)

# PL/pgSQL (FP-07). Applied only to a routine `_is_plpgsql` recognises, because
# each gives a keyword a meaning it does not have in T-SQL or PL/SQL: `EXECUTE
# <expr>` always runs a string (a nested call is `CALL`/`PERFORM`), and `SELECT
# ... INTO v` assigns a variable instead of creating a table.
_PLPGSQL_EXECUTE_RE = re.compile(r"^\s*EXECUTE\b", re.IGNORECASE)
_PLPGSQL_RETURN_QUERY_RE = re.compile(r"^\s*RETURN\s+QUERY\b\s*", re.IGNORECASE)
_PLPGSQL_PERFORM_RE = re.compile(r"^\s*PERFORM\b\s*", re.IGNORECASE)
_PLPGSQL_ASSIGNMENT_RE = re.compile(
    r"^\s*[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*(?:\[[^\]]*\])?\s*:?=\s*(?P<expr>.+)$",
    re.DOTALL,
)
_PLPGSQL_INTO_STRICT_RE = re.compile(r"\bINTO\s+STRICT\b", re.IGNORECASE)
_PLPGSQL_RETURNING_INTO_RE = re.compile(
    r"(?P<returning>\bRETURNING\b.*?)\s+INTO\s+(?:STRICT\s+)?"
    r"[A-Za-z_][\w$.]*(?:\s*,\s*[A-Za-z_][\w$.]*)*\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CALLEE_RE = re.compile(r"(?P<callee>[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s*\(")
# `CREATE TEMP TABLE t ON COMMIT DROP AS SELECT ...` is valid PostgreSQL that
# sqlglot keeps as an opaque Command. The clause governs the table's lifetime,
# not its lineage, so it is removed before parsing; temp-ness survives as the
# parsed node's TemporaryProperty (`_creates_temporary_table`).
_PG_TEMP_ON_COMMIT_RE = re.compile(
    r"^(?P<head>\s*CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP|TEMPORARY)\s+TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?[^\s(]+)\s+ON\s+COMMIT\s+(?:DROP|DELETE\s+ROWS|PRESERVE\s+ROWS)\b",
    re.IGNORECASE,
)


def _table_is_temp(table: object) -> bool:
    if not _SQLGLOT_AVAILABLE or not isinstance(table, exp.Table):
        return False
    this = table.args.get("this")
    if isinstance(this, exp.Parameter):
        return True
    if isinstance(this, exp.Identifier) and this.args.get("temporary"):
        return True
    return False


def _table_function_name(table: object) -> str | None:
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
    parts = [part for part in (table.catalog, table.db, name) if part]
    return ".".join(parts) if parts else None


def _collect_table_aliases_with_temp(
    statement: object, subject: Mapping[str, str] | None = None
) -> tuple[dict[str, str], set[str]]:
    """Mirrors `sql_lineage_parser._collect_table_aliases`'s exact walk
    order (so alias resolution stays consistent) while additionally
    recording which resolved names are temp tables/variables.

    `subject` is a trigger's firing-row binding (`trigger_subject_aliases`);
    `None` -- every routine-body caller -- leaves the walk exactly as it was."""
    aliases: dict[str, str] = {}
    temp: set[str] = set()
    if not _SQLGLOT_AVAILABLE or not isinstance(statement, exp.Expression):
        return aliases, temp
    for table in statement.find_all(exp.Table):
        function = _table_function_name(table)
        fqn = function or _resolve_table_name(table)
        if not fqn:
            continue
        is_temp = function is not None or _table_is_temp(table)
        if table.alias:
            aliases[table.alias] = fqn
            if is_temp:
                temp.add(table.alias)
        aliases[fqn] = fqn
        if table.name:
            aliases[table.name] = fqn
        if is_temp:
            temp.add(fqn)
    if subject:
        _bind_subject(aliases, subject)
    return aliases, temp


def _where_filter_edges(
    where_node: object,
    target_table: str,
    dialect: str,
    aliases: dict[str, str],
    exclude: set[tuple[str, str]],
) -> list[LineageEdge]:
    """FILTERED evidence for columns referenced only in a WHERE clause --
    the UPDATE/DELETE counterpart of `sql_lineage_parser._extract_filter_only_edges`
    (that helper is typed to `exp.Select` specifically and is not reused
    here; this mirrors its behaviour and its `FILTER_EVIDENCE_TARGET_COLUMN`
    convention exactly)."""
    if not _SQLGLOT_AVAILABLE or where_node is None:
        return []
    from aida.sql_lineage_parser import FILTER_EVIDENCE_TARGET_COLUMN

    edges: list[LineageEdge] = []
    seen: set[tuple[str, str]] = set()
    for table_ref, col_name in _extract_source_columns(where_node):
        resolved, ok = _resolve_or_mark_unresolved(table_ref, aliases)
        key = (resolved, col_name)
        if key in exclude or key in seen:
            continue
        seen.add(key)
        edges.append(
            LineageEdge(
                source_table=resolved if ok else UNRESOLVED_TABLE,
                source_column=col_name,
                target_table=target_table,
                target_column=FILTER_EVIDENCE_TARGET_COLUMN,
                transformation_type=TransformationType.FILTERED.value,
                confidence=Confidence.PARTIAL.value,
                dialect=dialect,
                source_resolved=ok,
            )
        )
    return edges


def _extract_edges_from_update(
    statement: exp.Update, dialect: str, subject: Mapping[str, str] | None = None
) -> tuple[list[LineageEdge], str]:
    """UPDATE ... SET ... [FROM ...] [WHERE ...] -- not handled by
    `sql_lineage_parser._extract_from_statement` at all. Supports both the
    ANSI shape (`UPDATE t SET t.c = ... WHERE ...`) and the T-SQL
    `UPDATE alias SET ... FROM real_table alias JOIN ... ` shape, where the
    target named after UPDATE is only an alias for the real table named in
    FROM -- resolved through the same alias table JOIN/FROM tables
    contribute, exactly like a SELECT's FROM/JOIN.
    """
    aliases, _temp = _collect_table_aliases_with_temp(statement, subject)
    raw_target = (
        _resolve_table_name(statement.this) if isinstance(statement.this, exp.Table) else ""
    )
    target_table = aliases.get(raw_target, raw_target)

    edges: list[LineageEdge] = []
    select_list_refs: set[tuple[str, str]] = set()
    for assignment in statement.expressions:
        if not isinstance(assignment, exp.EQ) or not isinstance(
            assignment.this, exp.Column
        ):
            continue
        target_col = assignment.this.name
        source_expr = assignment.expression
        has_agg = _has_aggregate_functions(source_expr)
        transformation = _classify_transformation(source_expr, has_agg)
        for table_ref, col_name in _extract_source_columns(source_expr):
            resolved, ok = _resolve_or_mark_unresolved(table_ref, aliases)
            select_list_refs.add((resolved, col_name))
            edges.append(
                LineageEdge(
                    source_table=resolved if ok else UNRESOLVED_TABLE,
                    source_column=col_name,
                    target_table=target_table,
                    target_column=target_col,
                    transformation_type=transformation,
                    confidence=Confidence.FULL.value if ok else Confidence.PARTIAL.value,
                    dialect=dialect,
                    source_resolved=ok,
                )
            )
    edges.extend(
        _where_filter_edges(
            statement.args.get("where"), target_table, dialect, aliases, select_list_refs
        )
    )
    return edges, target_table


def _extract_edges_from_merge(
    statement: exp.Merge, dialect: str, subject: Mapping[str, str] | None = None
) -> tuple[list[LineageEdge], str]:
    """MERGE INTO target USING source ON (...) WHEN MATCHED THEN UPDATE SET
    ... WHEN NOT MATCHED THEN INSERT (...) VALUES (...) -- column-level for
    each WHEN branch. `sql_lineage_parser._extract_from_statement`'s MERGE
    handling only walks nested `SELECT`s (the USING source, if it is itself
    a subquery); it does not map WHEN-branch columns at all. This does, in
    addition -- both are additive, not a replacement of what that gives.
    """
    # `subject` was accepted and then dropped here, so a T-SQL `MERGE ... USING
    # inserted i` in a trigger resolved its source to a table named `inserted`
    # rather than to the firing table. Every sibling extractor threads it through.
    aliases, _temp = _collect_table_aliases_with_temp(statement, subject)
    target_table = (
        _resolve_table_name(statement.this) if isinstance(statement.this, exp.Table) else ""
    )
    if not target_table:
        return [], ""

    edges: list[LineageEdge] = []
    whens = statement.args.get("whens")
    when_exprs = whens.expressions if whens is not None else []
    for when in when_exprs:
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            for assignment in then.expressions:
                if not isinstance(assignment, exp.EQ) or not isinstance(
                    assignment.this, exp.Column
                ):
                    continue
                target_col = assignment.this.name
                source_expr = assignment.expression
                has_agg = _has_aggregate_functions(source_expr)
                transformation = _classify_transformation(source_expr, has_agg)
                for table_ref, col_name in _extract_source_columns(source_expr):
                    resolved, ok = _resolve_or_mark_unresolved(table_ref, aliases)
                    edges.append(
                        LineageEdge(
                            source_table=resolved if ok else UNRESOLVED_TABLE,
                            source_column=col_name,
                            target_table=target_table,
                            target_column=target_col,
                            transformation_type=transformation,
                            confidence=(
                                Confidence.FULL.value if ok else Confidence.PARTIAL.value
                            ),
                            dialect=dialect,
                            source_resolved=ok,
                        )
                    )
        elif isinstance(then, exp.Insert):
            target_tuple = then.this
            source_tuple = then.expression
            target_cols = (
                target_tuple.expressions if isinstance(target_tuple, exp.Tuple) else []
            )
            source_exprs = (
                source_tuple.expressions if isinstance(source_tuple, exp.Tuple) else []
            )
            for target_col_expr, source_expr in zip(target_cols, source_exprs, strict=False):
                if not isinstance(target_col_expr, exp.Column):
                    continue
                target_col = target_col_expr.name
                has_agg = _has_aggregate_functions(source_expr)
                transformation = _classify_transformation(source_expr, has_agg)
                for table_ref, col_name in _extract_source_columns(source_expr):
                    resolved, ok = _resolve_or_mark_unresolved(table_ref, aliases)
                    edges.append(
                        LineageEdge(
                            source_table=resolved if ok else UNRESOLVED_TABLE,
                            source_column=col_name,
                            target_table=target_table,
                            target_column=target_col,
                            transformation_type=transformation,
                            confidence=(
                                Confidence.FULL.value if ok else Confidence.PARTIAL.value
                            ),
                            dialect=dialect,
                            source_resolved=ok,
                        )
                    )
        # WHEN [NOT] MATCHED THEN DELETE, or any other branch shape: no
        # column-level mapping to add -- the MERGE as a whole is still a
        # write (tracked by the caller regardless of edges), just not one
        # this branch fabricates column lineage for.
    return edges, target_table


def _extract_edges_from_insert(
    statement: exp.Insert, dialect: str, subject: Mapping[str, str] | None = None
) -> tuple[list[LineageEdge], str]:
    """INSERT INTO t [(col, ...)] SELECT ... -- not fully handled by
    `sql_lineage_parser._extract_from_statement`, in two ways this fixes:

    1. `_extract_target_table`'s `Insert` branch only unwraps a bare
       `exp.Table`; an INSERT with an explicit column list parses its target
       as `exp.Schema` wrapping the table (the same shape `CREATE VIEW`
       already unwraps), so `INSERT INTO t (a, b) SELECT x, y` silently
       resolved to an empty target table and produced zero edges -- a
       pre-existing gap in the shared helper, not introduced by AT-D2, found
       while building this module. Not fixed in `sql_lineage_parser.py`
       itself (out of this module's file-ownership scope); worked around
       here instead.
    2. Even once the target resolves, an explicit column list is
       authoritative for target column *names* -- `INSERT INTO t (a, b)
       SELECT x, y` must produce `a<-x, b<-y`, not `x<-x, y<-y` (what
       reusing `_extract_edges_from_select`'s own alias-based naming would
       give, since `x`/`y` are the select list's own names, not `t`'s).
       Positionally zipped against the column list when one is given;
       falls back to the select list's own alias/name (the existing,
       already-tested behaviour) when the INSERT has no explicit list.
    """
    this = statement.this
    target_columns: list[str] | None = None
    if isinstance(this, exp.Schema):
        table = this.this
        target_table = _resolve_table_name(table) if isinstance(table, exp.Table) else ""
        target_columns = [
            column.name
            for column in this.expressions
            if isinstance(column, exp.Column | exp.Identifier)
        ]
    elif isinstance(this, exp.Table):
        target_table = _resolve_table_name(this)
    else:
        target_table = ""
    if not target_table:
        return [], ""

    table_aliases, _ = _collect_table_aliases_with_temp(statement, subject)
    inner_select = statement.find(exp.Union) or statement.find(exp.Select)
    if inner_select is None:
        return (
            _edges_from_values(statement, target_table, target_columns, dialect, table_aliases),
            target_table,
        )

    if not target_columns:
        return (
            _extract_edges_from_select(inner_select, target_table, dialect, table_aliases),
            target_table,
        )

    projections = (
        inner_select.left.expressions
        if isinstance(inner_select, exp.Union) and isinstance(inner_select.left, exp.Select)
        else (inner_select.expressions if isinstance(inner_select, exp.Select) else [])
    )
    edges: list[LineageEdge] = []
    for target_col, select_expr in zip(target_columns, projections, strict=False):
        source_expr = select_expr.this if isinstance(select_expr, exp.Alias) else select_expr
        if isinstance(source_expr, exp.Star) or (
            isinstance(source_expr, exp.Column) and isinstance(source_expr.this, exp.Star)
        ):
            star_alias = source_expr.table if isinstance(source_expr, exp.Column) else None
            edges.extend(
                _extract_star_edges(
                    star_alias, target_table, dialect, table_aliases, {}, table_aliases,
                    inner_select if isinstance(inner_select, exp.Select) else inner_select,
                )
            )
            continue
        has_agg = _has_aggregate_functions(source_expr)
        transformation = _classify_transformation(source_expr, has_agg)
        for table_ref, col_name in _extract_source_columns(source_expr):
            resolved, ok = _resolve_or_mark_unresolved(table_ref, table_aliases)
            edges.append(
                LineageEdge(
                    source_table=resolved if ok else UNRESOLVED_TABLE,
                    source_column=col_name,
                    target_table=target_table,
                    target_column=target_col,
                    transformation_type=transformation,
                    confidence=Confidence.FULL.value if ok else Confidence.PARTIAL.value,
                    dialect=dialect,
                    source_resolved=ok,
                )
            )
    return edges, target_table


def _edges_from_values(
    statement: exp.Insert,
    target_table: str,
    target_columns: list[str] | None,
    dialect: str,
    table_aliases: dict[str, str],
) -> list[LineageEdge]:
    """`INSERT INTO t (a, b) VALUES (x.a, f(x.b))` -- a row built from
    expressions rather than from a query.

    Added for R11-FP01, because it is the shape a row trigger's write almost
    always has (`INSERT INTO audit (...) VALUES (NEW....)`) and with no branch
    for it the statement parsed cleanly and produced no edge at all: a body
    reported fully parsed with no lineage in it, which is the one outcome INV-9
    forbids. Literals contribute nothing and are never inspected -- only column
    references become edges -- so a VALUES list of constants is honestly
    edge-free rather than unparsed.

    Without an explicit column list the target columns are positional, and this
    module is deliberately catalog-free so it cannot know the table's column
    order. One `TABLE_STAR` edge per source table is recorded instead, the same
    honest table-level evidence `_extract_star_edges` gives a `SELECT *`, rather
    than guessing a name onto each position.
    """
    values = statement.find(exp.Values)
    if values is None:
        return []
    if target_columns is None:
        found: set[str] = set()
        for tuple_expr in values.expressions:
            for table_ref, _col_name in _extract_source_columns(tuple_expr):
                resolved, ok = _resolve_or_mark_unresolved(table_ref, table_aliases)
                if ok:
                    found.add(resolved)
        sources = sorted(found)
        return [
            LineageEdge(
                source_table=source,
                source_column=STAR_COLUMN_MARKER,
                target_table=target_table,
                target_column=STAR_COLUMN_MARKER,
                transformation_type=TransformationType.TABLE_STAR.value,
                confidence=Confidence.PARTIAL.value,
                dialect=dialect,
                source_resolved=True,
            )
            for source in sources
        ]
    edges: list[LineageEdge] = []
    for tuple_expr in values.expressions:
        expressions = (
            list(tuple_expr.expressions) if isinstance(tuple_expr, exp.Tuple) else [tuple_expr]
        )
        for target_col, source_expr in zip(target_columns, expressions, strict=False):
            has_agg = _has_aggregate_functions(source_expr)
            transformation = _classify_transformation(source_expr, has_agg)
            for table_ref, col_name in _extract_source_columns(source_expr):
                resolved, ok = _resolve_or_mark_unresolved(table_ref, table_aliases)
                edges.append(
                    LineageEdge(
                        source_table=resolved if ok else UNRESOLVED_TABLE,
                        source_column=col_name,
                        target_table=target_table,
                        target_column=target_col,
                        transformation_type=transformation,
                        confidence=Confidence.FULL.value if ok else Confidence.PARTIAL.value,
                        dialect=dialect,
                        source_resolved=ok,
                    )
                )
    return edges


def _extract_edges_from_select_into(
    statement: object, dialect: str, table_aliases: dict[str, str]
) -> tuple[list[LineageEdge], str]:
    into = statement.args.get("into") if isinstance(statement, exp.Select) else None
    into_table = into.this if into is not None else None
    if isinstance(into_table, exp.Table):
        target_table = _resolve_table_name(into_table)
    else:
        target_table = PROCEDURE_RESULT_TARGET
    edges = _extract_edges_from_select(statement, target_table, dialect, table_aliases)
    return edges, target_table


def _creates_temporary_table(node: object) -> bool:
    """`CREATE TEMP|TEMPORARY TABLE`: PostgreSQL and Snowflake mark temp-ness on
    the statement, not on the table identifier the way T-SQL's `#name` does."""
    properties = node.args.get("properties") if isinstance(node, exp.Create) else None
    return properties is not None and any(
        isinstance(prop, exp.TemporaryProperty) for prop in properties.expressions
    )


def _declared_language(sql: str) -> str | None:
    """The routine's `LANGUAGE` clause, read outside its dollar-quoted body."""
    outside = sql
    if (span := _dollar_quoted_body(sql)) is not None:
        outside = sql[: span[0]] + sql[span[1] :]
    match = _LANGUAGE_RE.search(outside)
    return match.group(1).lower() if match else None


def _is_plpgsql(sql: str, dialect: str) -> bool:
    if dialect != "postgres":
        return False
    language = _declared_language(sql)
    if language is not None:
        return language == "plpgsql"
    # A body handed over without its CREATE header: a BEGIN..END block is
    # PL/pgSQL, where a LANGUAGE sql body is plain statements.
    return _strip_begin_end(sql) is not None


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _local_statement(
    ordinal: int,
    node: exp.Expr,
    dialect: str,
    context: str | None,
    subject: Mapping[str, str] | None = None,
) -> ParsedStatement:
    """A query whose rows stay inside the routine. Its reads are real
    dependencies, so its edges are kept -- into `PROCEDURE_LOCAL_TARGET`, marked
    intermediate: never a table, never a write, never the routine's result."""
    if not isinstance(node, exp.Expression) or node.find(exp.Table) is None:
        return ParsedStatement(
            ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=True,
            unparsed_reason=None, control_flow_context=context,
            target_table=None, is_intermediate_target=False, node=node, edges=(),
        )
    edges = _extract_edges_from_select(
        node, PROCEDURE_LOCAL_TARGET, dialect, _collect_table_aliases_with_temp(node, subject)[0]
    )
    return ParsedStatement(
        ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=False,
        unparsed_reason=None, control_flow_context=context,
        target_table=PROCEDURE_LOCAL_TARGET, is_intermediate_target=True, node=node,
        edges=tuple(_wrap(edges, ordinal, False, True, context)),
    )


def _parse_local_query(
    ordinal: int,
    sql: str,
    dialect: str,
    sqlglot_dialect: str,
    context: str | None,
    subject: Mapping[str, str] | None = None,
) -> ParsedStatement:
    try:
        node = sqlglot.parse_one(sql, dialect=sqlglot_dialect, error_level=ErrorLevel.RAISE)
    except Exception as exc:  # sqlglot raises a broad ParseError/TokenError family
        return _unparsed_statement(
            ordinal, dialect, context, f"{UnparsedReason.PARSE_ERROR.value}: {exc!s}"[:300]
        )
    if not isinstance(node, exp.Select | exp.Union):
        return _unparsed_statement(
            ordinal, dialect, context,
            f"{UnparsedReason.UNSUPPORTED_STATEMENT_SHAPE.value}: "
            f"PL/pgSQL expression is not a query ({sql[:120]!r})",
        )
    return _local_statement(ordinal, node, dialect, context, subject)


def _classify_plpgsql_statement(
    ordinal: int,
    remainder: str,
    dialect: str,
    sqlglot_dialect: str,
    context: str | None,
    subject: Mapping[str, str] | None = None,
) -> tuple[ParsedStatement | None, str]:
    """Resolve a PL/pgSQL statement that is not plain SQL -- `(statement, "")` --
    or return `(None, sql)` with the SQL still to dispatch."""
    if match := _PLPGSQL_RETURN_QUERY_RE.match(remainder):
        # `RETURN QUERY <query>` streams that query as the routine's result set.
        remainder = remainder[match.end() :]
    if _PLPGSQL_EXECUTE_RE.match(remainder):
        return _unparsed_statement(
            ordinal, dialect, context,
            f"{UnparsedReason.DYNAMIC_SQL.value}: PL/pgSQL EXECUTE runs a string built at runtime",
        ), ""
    if match := _PLPGSQL_PERFORM_RE.match(remainder):
        expression = remainder[match.end() :]
        call = _CALLEE_RE.match(expression)
        close = _matching_paren(expression, call.end() - 1) if call else None
        if call and close is not None and not expression[close + 1 :].strip():
            return _unparsed_statement(
                ordinal, dialect, context,
                f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: {call.group('callee')}",
            ), ""
        return _parse_local_query(
            ordinal, f"SELECT {expression}", dialect, sqlglot_dialect, context, subject
        ), ""
    if match := _PLPGSQL_ASSIGNMENT_RE.match(remainder):
        return _parse_local_query(
            ordinal, f"SELECT {match.group('expr')}", dialect, sqlglot_dialect, context, subject
        ), ""
    remainder = _PLPGSQL_INTO_STRICT_RE.sub("INTO", remainder)
    return None, _PLPGSQL_RETURNING_INTO_RE.sub(r"\g<returning>", remainder)


# ---------------------------------------------------------------------------
# Step 4: the per-chunk dispatcher.
# ---------------------------------------------------------------------------


def _classify_chunk(
    ordinal: int,
    raw_chunk: str,
    chunk_offset: int,
    dialect: str,
    sqlglot_dialect: str,
    plpgsql: bool,
    subject: Mapping[str, str] | None,
    locator: _Locator,
    digest: str,
) -> list[ParsedStatement]:
    """Peel one chunk, classify what is left, and locate every statement it gave.

    `chunk_offset` is where `raw_chunk` starts in the text `locator` was built
    over. A cursor loop's own query is classified first, located at its own
    offset inside the chunk, exactly as the dispatcher's cursor recursion did
    before R11-FP07 moved it here to know where each piece is.
    """
    peeled = _peel_control_flow_prefix(raw_chunk)
    results: list[ParsedStatement] = []
    if peeled.cursor_loop_source_sql and peeled.cursor_offset is not None:
        results.extend(
            _classify_chunk(
                ordinal,
                peeled.cursor_loop_source_sql,
                chunk_offset + peeled.cursor_offset,
                dialect,
                sqlglot_dialect,
                plpgsql,
                subject,
                locator,
                digest,
            )
        )
    start = chunk_offset + peeled.remainder_offset
    where = locator.span(start, start + len(peeled.remainder))
    results.extend(
        _located(statement, where, digest)
        for statement in _classify_and_extract(
            ordinal, peeled, dialect, sqlglot_dialect, plpgsql, subject
        )
    )
    return results


def _located(
    statement: ParsedStatement, where: StatementRange | None, digest: str
) -> ParsedStatement:
    """`statement` and its edges, pointed at `where` in the text `digest` names.

    A marker's range is the statement where its gap is (`GAP_STATEMENT`); any
    other edge's is the statement it was read from (`STATEMENT`). No range, no
    status beyond `NOT_LOCATED` -- the default every edge starts with."""
    if where is None:
        return statement
    return replace(
        statement,
        statement_range=where,
        edges=tuple(
            replace(
                edge,
                statement_range=where,
                statement_range_status=(
                    StatementRangeStatus.GAP_STATEMENT.value
                    if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
                    else StatementRangeStatus.STATEMENT.value
                ),
                statement_text_digest=digest,
            )
            for edge in statement.edges
        ),
    )


def _classify_and_extract(
    ordinal: int,
    peeled: _PeelResult,
    dialect: str,
    sqlglot_dialect: str,
    plpgsql: bool = False,
    subject: Mapping[str, str] | None = None,
) -> list[ParsedStatement]:
    """The dispatcher: what one peeled statement is, and the lineage it carries.

    Takes the peel rather than the raw chunk since R11-FP07, because
    `_classify_chunk` needs the peel's offsets to locate what this returns; the
    cursor loop's query is classified there too. The `isinstance` branches below
    are what `procedure_capability_matrix` introspects, so they stay here.
    """
    results: list[ParsedStatement] = []

    remainder = peeled.remainder
    if not remainder:
        # Purely structural (BEGIN/END/ELSE/...) -- genuinely no lineage.
        return results

    if _NO_LINEAGE_KEYWORDS_RE.match(remainder) or (
        dialect == "oracle"
        and (_SUBPROGRAM_DECLARATION_RE.match(remainder) or _PLSQL_RETURN_RE.match(remainder))
    ):
        results.append(
            ParsedStatement(
                ordinal=ordinal,
                is_write=False,
                is_unparsed=False,
                is_no_lineage=True,
                unparsed_reason=None,
                control_flow_context=peeled.control_flow_context,
                target_table=None,
                is_intermediate_target=False,
                node=None,
                edges=(),
            )
        )
        return results

    if dialect == "postgres":
        remainder = _PG_TEMP_ON_COMMIT_RE.sub(r"\g<head>", remainder, count=1)
    if plpgsql:
        plpgsql_statement, remainder = _classify_plpgsql_statement(
            ordinal, remainder, dialect, sqlglot_dialect, peeled.control_flow_context, subject
        )
        if plpgsql_statement is not None:
            results.append(plpgsql_statement)
            return results

    if _DYNAMIC_SQL_RE.search(remainder):
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.DYNAMIC_SQL.value}: {remainder[:120]!r}",
            )
        )
        return results

    if match := _NESTED_CALL_RE.match(remainder):
        callee = match.group(1)
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: {callee}",
            )
        )
        return results

    if not _SQLGLOT_AVAILABLE:
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.PARSE_ERROR.value}: sqlglot library is not available",
            )
        )
        return results

    try:
        node = sqlglot.parse_one(remainder, dialect=sqlglot_dialect, error_level=ErrorLevel.RAISE)
    except Exception as exc:  # sqlglot raises a broad ParseError/TokenError family
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.PARSE_ERROR.value}: {exc!s}"[:300],
            )
        )
        return results

    if isinstance(node, exp.Command):
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.UNSUPPORTED_STATEMENT_SHAPE.value}: "
                f"sqlglot could not parse this statement shape "
                f"({remainder[:120]!r})",
            )
        )
        return results

    table_aliases, _ = _collect_table_aliases_with_temp(node, subject)

    if (
        (plpgsql or dialect == "oracle")
        and isinstance(node, exp.Select)
        and node.args.get("into") is not None
    ):
        # PL/pgSQL `SELECT ... INTO target` assigns variables; it creates no table.
        # R11-FP03: so does PL/SQL's -- Oracle SQL has no SELECT INTO a table at all
        # (that is CREATE TABLE AS) -- and splitting packages into members put every
        # packaged function's `SELECT ... INTO v` in front of a reader as a member
        # writing a table named `v`, the wrong fact PL/pgSQL was cured of in FP-07.
        results.append(
            _local_statement(ordinal, node, dialect, peeled.control_flow_context, subject)
        )
        return results

    if isinstance(node, exp.Select | exp.Union):
        edges, target = _extract_edges_from_select_into(node, dialect, table_aliases)
        is_write = target != PROCEDURE_RESULT_TARGET
        temp = target in _collect_table_aliases_with_temp(node)[1] if is_write else False
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=is_write, is_unparsed=False, is_no_lineage=False,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=target, is_intermediate_target=temp, node=node,
                edges=tuple(_wrap(edges, ordinal, is_write, temp, peeled.control_flow_context)),
            )
        )
        return results

    if isinstance(node, exp.Insert):
        edges, target = _extract_edges_from_insert(node, dialect, subject)
        temp = target in _collect_table_aliases_with_temp(node)[1]
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=target or None, is_intermediate_target=temp, node=node,
                edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
            )
        )
        return results

    if isinstance(node, exp.Update):
        edges, target = _extract_edges_from_update(node, dialect, subject)
        temp = target in _collect_table_aliases_with_temp(node)[1]
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=target or None, is_intermediate_target=temp, node=node,
                edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
            )
        )
        return results

    if isinstance(node, exp.Delete):
        aliases, temp_set = _collect_table_aliases_with_temp(node, subject)
        target_expr = node.this
        target = _resolve_table_name(target_expr) if isinstance(target_expr, exp.Table) else ""
        target = aliases.get(target, target)
        temp = target in temp_set
        edges = _where_filter_edges(
            node.args.get("where"), target or "<UNKNOWN_TARGET>", dialect, aliases, set()
        )
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=target or None, is_intermediate_target=temp, node=node,
                edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
            )
        )
        return results

    if isinstance(node, exp.Merge):
        edges, target = _extract_edges_from_merge(node, dialect, subject)
        temp = target in _collect_table_aliases_with_temp(node)[1]
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=target or None, is_intermediate_target=temp, node=node,
                edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
            )
        )
        return results

    if isinstance(node, exp.Create):
        target = _extract_target_table(node)
        temp = target in _collect_table_aliases_with_temp(node)[1] or _creates_temporary_table(node)
        # [] for a plain CREATE TABLE, non-empty for CREATE TABLE ... AS SELECT.
        edges = _extract_from_statement(node, dialect)
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=target or None, is_intermediate_target=temp, node=node,
                edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
            )
        )
        return results

    if isinstance(node, exp.Execute):
        this = node.args.get("this")
        # `EXEC(@sql)`/`EXECUTE(@sql)`: a parenthesised expression, not a
        # plain procedure name -- dynamic SQL sqlglot happened to parse
        # cleanly into a shape with no table references at all.
        if isinstance(this, exp.Paren) or not isinstance(this, exp.Table | exp.Column):
            results.append(
                _unparsed_statement(
                    ordinal, dialect, peeled.control_flow_context,
                    f"{UnparsedReason.DYNAMIC_SQL.value}: EXEC(...) with a dynamic argument",
                )
            )
            return results
        callee = _resolve_table_name(this) if isinstance(this, exp.Table) else str(this)
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: {callee}",
            )
        )
        return results

    if isinstance(node, exp.ExecuteSql):
        results.append(
            _unparsed_statement(
                ordinal, dialect, peeled.control_flow_context,
                f"{UnparsedReason.DYNAMIC_SQL.value}: sp_executesql",
            )
        )
        return results

    if isinstance(node, exp.Set | exp.Declare):
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=True,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=None, is_intermediate_target=False, node=node, edges=(),
            )
        )
        return results

    # Anything else sqlglot did successfully parse into a *specific* node
    # type this dispatcher does not recognise: if it references no table at
    # all, it cannot carry table-level lineage (a bare RAISERROR/PRINT/THROW
    # function-call statement, for instance) -- genuinely nothing to report.
    # If it does reference a table, INV-9/AT-C4 says flag it rather than
    # guess: never fabricate lineage for a shape this module was not written
    # to interpret.
    references_table = isinstance(node, exp.Expression) and node.find(exp.Table) is not None
    if not references_table:
        results.append(
            ParsedStatement(
                ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=True,
                unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                target_table=None, is_intermediate_target=False, node=node, edges=(),
            )
        )
        return results

    results.append(
        _unparsed_statement(
            ordinal, dialect, peeled.control_flow_context,
            f"{UnparsedReason.UNRESOLVED_CONTROL_FLOW.value}: unrecognised statement shape "
            f"{type(node).__name__} references a table but is not one of this module's "
            f"known DML/DDL kinds",
        )
    )
    return results


def _wrap(
    edges: list[LineageEdge],
    ordinal: int,
    is_write: bool,
    is_intermediate: bool,
    control_flow_context: str | None,
) -> list[ProcedureLineageEdgeRecord]:
    return [
        ProcedureLineageEdgeRecord(
            source_table=e.source_table,
            source_column=e.source_column,
            target_table=e.target_table,
            target_column=e.target_column,
            transformation_type=e.transformation_type,
            confidence=e.confidence,
            dialect=e.dialect,
            source_resolved=e.source_resolved,
            statement_ordinal=ordinal,
            is_write=is_write,
            is_intermediate=is_intermediate,
            control_flow_context=control_flow_context,
        )
        for e in edges
    ]


def _unparsed_statement(
    ordinal: int, dialect: str, control_flow_context: str | None, reason: str
) -> ParsedStatement:
    edge = ProcedureLineageEdgeRecord(
        source_table=UNRESOLVED_TABLE,
        source_column=UNPARSED_MARKER,
        target_table=PROCEDURE_RESULT_TARGET,
        target_column=UNPARSED_MARKER,
        transformation_type=UNPARSED_TRANSFORMATION_TYPE,
        confidence=Confidence.LOW.value,
        dialect=dialect,
        source_resolved=False,
        statement_ordinal=ordinal,
        is_write=False,
        is_intermediate=False,
        control_flow_context=control_flow_context,
        unparsed_reason=reason,
    )
    return ParsedStatement(
        ordinal=ordinal, is_write=False, is_unparsed=True, is_no_lineage=False,
        unparsed_reason=reason, control_flow_context=control_flow_context,
        target_table=None, is_intermediate_target=False, node=None, edges=(edge,),
    )


# ---------------------------------------------------------------------------
# Step 5: temp-table/variable hop propagation, iterated to a fixed point.
# ---------------------------------------------------------------------------

_MAX_PROPAGATION_PASSES: Final[int] = 5


def _propagate_intermediate_hops(
    edges: list[ProcedureLineageEdgeRecord],
) -> list[ProcedureLineageEdgeRecord]:
    def _key(
        source_table: str, source_column: str, target_table: str, target_column: str
    ) -> tuple[str, str, str, str]:
        return (
            source_table.lower(), source_column.lower(),
            target_table.lower(), target_column.lower(),
        )

    synthesized: list[ProcedureLineageEdgeRecord] = []
    known_keys = {
        _key(e.source_table, e.source_column, e.target_table, e.target_column) for e in edges
    }
    frontier = list(edges)
    for _ in range(_MAX_PROPAGATION_PASSES):
        # (temp_table.lower(), temp_column.lower()) -> upstream edges that fed it
        temp_writes: dict[tuple[str, str], list[ProcedureLineageEdgeRecord]] = {}
        for e in frontier + synthesized:
            if e.is_intermediate and e.transformation_type != UNPARSED_TRANSFORMATION_TYPE:
                temp_key = (e.target_table.lower(), e.target_column.lower())
                temp_writes.setdefault(temp_key, []).append(e)

        new_edges: list[ProcedureLineageEdgeRecord] = []
        for e in edges + synthesized:
            if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE:
                continue
            upstream = temp_writes.get((e.source_table.lower(), e.source_column.lower()))
            if not upstream:
                continue
            for hop in upstream:
                key = _key(hop.source_table, hop.source_column, e.target_table, e.target_column)
                if key in known_keys:
                    continue
                known_keys.add(key)
                is_agg = TransformationType.AGGREGATED.value in {
                    hop.transformation_type, e.transformation_type,
                }
                new_edges.append(
                    ProcedureLineageEdgeRecord(
                        source_table=hop.source_table,
                        source_column=hop.source_column,
                        target_table=e.target_table,
                        target_column=e.target_column,
                        transformation_type=(
                            TransformationType.AGGREGATED.value
                            if is_agg
                            else TransformationType.DERIVED.value
                        ),
                        confidence=Confidence.PARTIAL.value,
                        dialect=e.dialect,
                        source_resolved=hop.source_resolved,
                        statement_ordinal=e.statement_ordinal,
                        is_write=e.is_write,
                        is_intermediate=e.is_intermediate,
                        control_flow_context=e.control_flow_context,
                        via_temp_table=e.source_table,
                        # R11-FP07/FP03: a transitive edge is attributed to the
                        # statement that writes its target -- `e`'s -- so it is
                        # located and attributed exactly where `e` is.
                        statement_range=e.statement_range,
                        statement_range_status=e.statement_range_status,
                        statement_text_digest=e.statement_text_digest,
                        package_member=e.package_member,
                        member_attribution=e.member_attribution,
                    )
                )
        if not new_edges:
            break
        synthesized.extend(new_edges)
    return synthesized


def propagate_intermediate_hops(
    edges: list[ProcedureLineageEdgeRecord],
) -> list[ProcedureLineageEdgeRecord]:
    """The transitive edges an intermediate implies -- a temp table, a variable, or a table
    function's rows. Public for `aida.routine_call_descent`, which splices a called
    routine's edges in and then needs this same fixed-point pass run over the result."""
    return _propagate_intermediate_hops(edges)


def _dedupe_edges(
    edges: list[ProcedureLineageEdgeRecord],
) -> list[ProcedureLineageEdgeRecord]:
    """Two different branches of the same statement (e.g. a MERGE's WHEN
    MATCHED UPDATE and WHEN NOT MATCHED INSERT both mapping the same source
    column onto the same target column) can legitimately produce the exact
    same fact twice. Kept as one edge, not silently multiplied -- this also
    keeps the natural key the persistence layer's unique constraint uses
    collision-free without that layer needing to know about branches at all.
    """
    seen: set[tuple[str, str, str, str, str, int, str | None]] = set()
    deduped: list[ProcedureLineageEdgeRecord] = []
    for edge in edges:
        key = (
            edge.source_table, edge.source_column, edge.target_table, edge.target_column,
            edge.transformation_type, edge.statement_ordinal, edge.via_temp_table,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


def _attributed(
    statement: ParsedStatement, subject: Mapping[str, str] | None = None
) -> ParsedStatement:
    """A statement with exactly one source attributes its unqualified columns to it.

    `SELECT customer_id, net_revenue FROM s.customer_revenue` qualifies nothing, so every column
    resolved as UNRESOLVED even though there is one table they can have come from. The statement's
    own target is not a source, so an `INSERT INTO t (...) SELECT ... FROM one_table` counts as one.
    """
    if statement.node is None or not statement.edges:
        return statement
    bound = {name.lower(): table for name, table in (subject or {}).items() if table}
    names = {
        bound.get(raw.lower(), raw)
        for raw in (
            _table_function_name(table) or _resolve_table_name(table)
            for table in statement.node.find_all(exp.Table)
        )
    }
    names.discard("")
    names.discard(statement.target_table or "")
    if len(names) != 1:
        return statement
    only = next(iter(names))
    return replace(
        statement,
        edges=tuple(
            edge
            if edge.source_resolved or edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
            else replace(edge, source_table=only, source_resolved=True)
            for edge in statement.edges
        ),
    )


def _table_function_markers(
    statement: ParsedStatement, dialect: str, digest: str | None = None
) -> list[ParsedStatement]:
    """One marker per table-valued function a parsed statement reads.

    The statement's own edges stay right -- its function source is an intermediate, so nothing
    claims the function is a table -- but what that function reads is not in this body.
    `aida.routine_call_descent` reads it through when the function is captured here.
    R11-FP07: the marker's gap is in the reading statement, so it is located there.
    """
    if statement.node is None:
        return []
    markers: list[ParsedStatement] = []
    seen: set[str] = set()
    for table in statement.node.find_all(exp.Table):
        name = _table_function_name(table)
        if name is None or name.lower() in seen:
            continue
        seen.add(name.lower())
        marker = _unparsed_statement(
            statement.ordinal,
            dialect,
            statement.control_flow_context,
            f"{UnparsedReason.TABLE_FUNCTION_READ.value}: {name}",
        )
        if digest is not None:
            marker = _located(marker, statement.statement_range, digest)
        markers.append(marker)
    return markers


@dataclass(frozen=True, slots=True)
class _WalkContext:
    """What every chunk of one parse is classified and located with."""

    dialect: str
    sqlglot_dialect: str
    plpgsql: bool
    subject: Mapping[str, str] | None
    locator: _Locator
    digest: str


def _walk_span(
    sql: str, start: int, end: int, ordinal: int, context: _WalkContext
) -> tuple[list[ParsedStatement], int]:
    """Split, peel, classify and locate every statement of `sql[start:end]`.

    Returns the statements and the next free ordinal. The ordinal is advanced
    once per statement produced, and a chunk's statements are all numbered from
    the ordinal the chunk started at -- exactly the numbering the walk has
    always used, so no stored natural key moves."""
    body = sql[start:end]
    statements: list[ParsedStatement] = []
    for chunk_start, chunk_end in _split_top_level_statement_spans(body):
        for parsed in _classify_chunk(
            ordinal,
            body[chunk_start:chunk_end],
            start + chunk_start,
            context.dialect,
            context.sqlglot_dialect,
            context.plpgsql,
            context.subject,
            context.locator,
            context.digest,
        ):
            statement = _attributed(parsed, context.subject)
            statements.append(statement)
            ordinal += 1
            for marker in _table_function_markers(statement, context.dialect, context.digest):
                statements.append(marker)
                ordinal += 1
    return statements, ordinal


# ---------------------------------------------------------------------------
# Step 6 (R11-FP03): an Oracle package, split into its member subprograms.
# ---------------------------------------------------------------------------

#: (start, end, upper-cased text) -- a word, or one of `;`, `(`, `)`.
_Token = tuple[int, int, str]

#: Words that may sit between a package spec's `;` and its body's `PACKAGE`.
_PACKAGE_HEADER_WORDS: Final = frozenset(
    {"CREATE", "OR", "REPLACE", "EDITIONABLE", "NONEDITIONABLE"}
)


@dataclass(frozen=True, slots=True)
class _MemberSpan:
    name: str
    kind: str
    parameter_names: tuple[str, ...]
    #: The whole member: its PROCEDURE/FUNCTION keyword to just past its `;`.
    start: int
    end: int
    #: Strictly between the member's own BEGIN and END; equal for a member with
    #: no PL/SQL body (an external implementation).
    body_start: int
    body_end: int


@dataclass(frozen=True, slots=True)
class _PackageLayout:
    members: tuple[_MemberSpan, ...]
    #: The package's own code to walk: spec declarations, the body's
    #: package-level declarations, and the initialization block.
    package_segments: tuple[tuple[int, int], ...]


def _is_package_text(sql: str, dialect: str) -> bool:
    return dialect == "oracle" and bool(_PACKAGE_TEXT_RE.match(sql))


def _parameter_names(text: str) -> tuple[str, ...]:
    """The parameter names of a subprogram header's parameter list, in order.

    Splits on commas outside parentheses, quotes and comments, and keeps each
    piece's first identifier. A default expression is never kept -- it can be a
    literal (INV-6), and the name is all resolution needs."""
    pieces: list[str] = []
    depth = 0
    piece_start = 0
    index = 0
    while index < len(text):
        ch = text[index]
        if ch in "'\"":
            close = text.find(ch, index + 1)
            index = len(text) if close == -1 else close + 1
            continue
        if text.startswith("--", index):
            close = text.find("\n", index)
            index = len(text) if close == -1 else close + 1
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close == -1 else close + 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            pieces.append(text[piece_start:index])
            piece_start = index + 1
        index += 1
    pieces.append(text[piece_start:])
    names: list[str] = []
    for piece in pieces:
        match = re.match(r"\s*([A-Za-z_][\w$#]*)", piece)
        if match:
            names.append(match.group(1))
    return tuple(names)


def _statement_end(tokens: list[_Token], index: int) -> int | None:
    """Past `END [label] ;` starting at the token after END: the index after `;`."""
    if index < len(tokens) and tokens[index][2] not in (";", "(", ")"):
        index += 1
    if index < len(tokens) and tokens[index][2] == ";":
        return index + 1
    return None


def _header_end(tokens: list[_Token], index: int) -> int | None:
    """The index just past the IS/AS that ends a PACKAGE header, or None."""
    depth = 0
    while index < len(tokens):
        text = tokens[index][2]
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and text == ";":
            return None
        elif depth == 0 and text in ("IS", "AS"):
            return index + 1
        index += 1
    return None


def _declarations_end(tokens: list[_Token], index: int, stop: int) -> int | None:
    """The index of the END closing a declaration list that starts at `index`,
    before `stop` -- CASE expressions in a default open and close their own
    level, so only a depth-0 END ends the list."""
    depth = 0
    while index < stop:
        text = tokens[index][2]
        following = tokens[index + 1][2] if index + 1 < len(tokens) else None
        if text == "CASE":
            depth += 1
        elif text == "END":
            if depth == 0:
                return index
            depth -= 1
            if following == "CASE":
                index += 1
        index += 1
    return None


def _consume_subprogram(
    sql: str, tokens: list[_Token], index: int
) -> tuple[_MemberSpan | None, int] | PackageSplitFailure:
    """Read the subprogram whose PROCEDURE/FUNCTION keyword is at `index`.

    Returns the member (None for a forward declaration, which ends at its `;`)
    and the index just past it. A nested subprogram in the member's declaration
    section is consumed recursively and belongs to the member that contains it.
    """
    keyword = tokens[index]
    at = index + 1
    if at >= len(tokens) or tokens[at][2] in (";", "(", ")"):
        return PackageSplitFailure.UNREADABLE_MEMBER
    # The scanner steps over a quoted identifier as it does a string, so a quoted
    # member name leaves some other word here. Refuse rather than misname it.
    if '"' in sql[keyword[1] : tokens[at][0]]:
        return PackageSplitFailure.UNREADABLE_MEMBER
    name = sql[tokens[at][0] : tokens[at][1]]
    at += 1
    parameter_names: tuple[str, ...] = ()
    if at < len(tokens) and tokens[at][2] == "(":
        close = _matching_paren_token(tokens, at)
        if close is None:
            return PackageSplitFailure.UNBALANCED_BLOCKS
        parameter_names = _parameter_names(sql[tokens[at][1] : tokens[close][0]])
        at = close + 1
    depth = 0
    while at < len(tokens):
        text = tokens[at][2]
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and text == ";":
            return None, at + 1  # a forward declaration
        elif depth == 0 and text in ("IS", "AS"):
            break
        at += 1
    else:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    at += 1
    if at < len(tokens) and tokens[at][2] in ("LANGUAGE", "EXTERNAL"):
        # An external implementation (a Java or C call spec): no PL/SQL body.
        while at < len(tokens) and tokens[at][2] != ";":
            at += 1
        if at >= len(tokens):
            return PackageSplitFailure.UNBALANCED_BLOCKS
        return (
            _MemberSpan(
                name, keyword[2], parameter_names, keyword[0], tokens[at][1],
                tokens[at][0], tokens[at][0],
            ),
            at + 1,
        )
    depth = 0
    while at < len(tokens):
        text = tokens[at][2]
        if depth == 0 and text in ("PROCEDURE", "FUNCTION"):
            nested = _consume_subprogram(sql, tokens, at)
            if isinstance(nested, PackageSplitFailure):
                return nested
            at = nested[1]
            continue
        if depth == 0 and text == "BEGIN":
            break
        if text == "CASE":
            depth += 1
        elif text == "END" and depth > 0:
            depth -= 1
        at += 1
    else:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    begin = at
    close = _matching_end(tokens, begin)
    if close is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    after = _statement_end(tokens, close + 1)
    if after is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    member = _MemberSpan(
        name=name,
        kind=keyword[2],
        parameter_names=parameter_names,
        start=keyword[0],
        end=tokens[after - 1][1],
        body_start=tokens[begin][1],
        body_end=tokens[close][0],
    )
    return member, after


def _matching_paren_token(tokens: list[_Token], index: int) -> int | None:
    depth = 0
    for at in range(index, len(tokens)):
        if tokens[at][2] == "(":
            depth += 1
        elif tokens[at][2] == ")":
            depth -= 1
            if depth == 0:
                return at
    return None


def _package_layout(sql: str) -> _PackageLayout | PackageSplitFailure:
    """Where each member subprogram of a stored Oracle package is, and where the
    package's own code is -- or the reason the text cannot be split.

    Works on the quote/comment-aware token stream, so a keyword inside a string
    or a comment is never a boundary. Every block has to close for the split to
    stand: one that does not (a truncated body is the usual cause) fails the
    whole split rather than guessing where the last member ends.
    """
    tokens: list[_Token] = [
        (start, end, sql[start:end].upper()) for start, end, _kind in _scan_tokens(sql)
    ]
    body = next(
        (
            index
            for index in range(len(tokens) - 1)
            if tokens[index][2] == "PACKAGE" and tokens[index + 1][2] == "BODY"
        ),
        None,
    )
    if body is None:
        return PackageSplitFailure.NO_PACKAGE_BODY
    segments: list[tuple[int, int]] = []

    # The spec, when the text carries one: its declarations are package-level code.
    spec = next((index for index in range(body) if tokens[index][2] == "PACKAGE"), None)
    if spec is not None:
        opened = _header_end(tokens, spec + 1)
        if opened is None:
            return PackageSplitFailure.UNBALANCED_BLOCKS
        closed = _declarations_end(tokens, opened, body)
        if closed is None:
            return PackageSplitFailure.UNBALANCED_BLOCKS
        after = _statement_end(tokens, closed + 1)
        if after is None or any(
            tokens[index][2] not in _PACKAGE_HEADER_WORDS for index in range(after, body)
        ):
            return PackageSplitFailure.UNBALANCED_BLOCKS
        segments.append((tokens[opened - 1][1], tokens[closed][0]))

    opened = _header_end(tokens, body + 2)
    if opened is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    members: list[_MemberSpan] = []
    segment_start = tokens[opened - 1][1]
    index = opened
    depth = 0
    closing: int | None = None
    while index < len(tokens):
        start, end, text = tokens[index]
        following = tokens[index + 1][2] if index + 1 < len(tokens) else None
        if depth == 0 and text in ("PROCEDURE", "FUNCTION"):
            consumed = _consume_subprogram(sql, tokens, index)
            if isinstance(consumed, PackageSplitFailure):
                return consumed
            member, index = consumed
            if member is not None:
                segments.append((segment_start, member.start))
                members.append(member)
                segment_start = member.end
            continue
        if depth == 0 and text == "BEGIN":
            # The initialization block, closed by the package's own END.
            close = _matching_end(tokens, index)
            if close is None:
                return PackageSplitFailure.UNBALANCED_BLOCKS
            segments.append((segment_start, start))
            segments.append((end, tokens[close][0]))
            closing = close
            break
        if depth == 0 and text == "END":
            segments.append((segment_start, start))
            closing = index
            break
        if text == "CASE":
            depth += 1
        elif text == "END":
            depth -= 1
            if following == "CASE":
                index += 1
        index += 1
    if closing is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    tail = _statement_end(tokens, closing + 1)
    if tail is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    if tail < len(tokens):
        # Anything after the package's own END is walked, never dropped.
        segments.append((tokens[tail][0], len(sql)))
    return _PackageLayout(
        members=tuple(members),
        package_segments=tuple((start, end) for start, end in segments if end > start),
    )


def _attributed_to(
    statement: ParsedStatement, member: str | None, attribution: MemberAttribution
) -> ParsedStatement:
    return replace(
        statement,
        edges=tuple(
            replace(edge, package_member=member, member_attribution=attribution.value)
            for edge in statement.edges
        ),
    )


@dataclass(slots=True)
class _Walk:
    statements: list[ParsedStatement]
    #: The statements by the body they came from: one group for a routine, one per
    #: member plus one per stretch of package-level code for a split package. Hop
    #: propagation runs within a group, because an intermediate is local to its body.
    groups: list[list[ParsedStatement]]
    digest: str | None
    member_attribution: str | None = None
    member_fallback_reason: str | None = None
    members: tuple[PackageMember, ...] = ()


def _walk_package(sql: str, layout: _PackageLayout, context: _WalkContext) -> _Walk:
    units: list[tuple[int, int, _MemberSpan | None]] = [
        (start, end, None) for start, end in layout.package_segments
    ] + [(member.body_start, member.body_end, member) for member in layout.members]
    units.sort(key=lambda unit: unit[0])
    statements: list[ParsedStatement] = []
    groups: list[list[ParsedStatement]] = []
    members: list[PackageMember] = []
    ordinal = 0
    for start, end, member in units:
        walked, ordinal = _walk_span(sql, start, end, ordinal, context)
        if member is None:
            group = [_attributed_to(s, None, MemberAttribution.PACKAGE_LEVEL) for s in walked]
        else:
            group = [_attributed_to(s, member.name, MemberAttribution.MEMBER) for s in walked]
            ordinals = [statement.ordinal for statement in group]
            members.append(
                PackageMember(
                    name=member.name,
                    kind=member.kind,
                    parameter_names=member.parameter_names,
                    start_offset=member.start,
                    end_offset=member.end,
                    first_ordinal=min(ordinals) if ordinals else None,
                    last_ordinal=max(ordinals) if ordinals else None,
                )
            )
        statements.extend(group)
        groups.append(group)
    return _Walk(
        statements=statements,
        groups=groups,
        digest=context.digest,
        member_attribution=MemberAttribution.MEMBER.value,
        members=tuple(members),
    )


def _walk(sql: str, dialect: str, subject: Mapping[str, str] | None) -> _Walk:
    if not _SQLGLOT_AVAILABLE or dialect not in _SQLGLOT_DIALECT_MAP:
        return _Walk(statements=[], groups=[], digest=None)
    context = _WalkContext(
        dialect=dialect,
        sqlglot_dialect=_SQLGLOT_DIALECT_MAP[dialect],
        plpgsql=_is_plpgsql(sql, dialect),
        # A body with no firing table still names firing rows if it is a trigger
        # function; see `unbound_subject_aliases`.
        subject=subject or unbound_subject_aliases(dialect),
        locator=_Locator(sql),
        digest=statement_text_digest(sql),
    )
    fallback: PackageSplitFailure | None = None
    if _is_package_text(sql, dialect):
        layout = _package_layout(sql)
        if isinstance(layout, _PackageLayout):
            return _walk_package(sql, layout, context)
        fallback = layout
    start, end = _extract_body_span(sql, dialect)
    statements, _next = _walk_span(sql, start, end, 0, context)
    if fallback is None:
        return _Walk(statements=statements, groups=[statements], digest=context.digest)
    # R11-FP03: the package could not be split, so it is parsed as the one body it
    # always was -- and every fact says so, rather than reading like a member's.
    statements = [
        _attributed_to(statement, None, MemberAttribution.PACKAGE_FALLBACK)
        for statement in statements
    ]
    return _Walk(
        statements=statements,
        groups=[statements],
        digest=context.digest,
        member_attribution=MemberAttribution.PACKAGE_FALLBACK.value,
        member_fallback_reason=fallback.value,
    )


def walk_procedure_statements(
    sql: str, dialect: str, subject: Mapping[str, str] | None = None
) -> list[ParsedStatement]:
    """Split, peel, and classify every top-level statement in a procedure
    body. Exposed (not just an internal helper of `parse_procedure_lineage`)
    because `procedure_tool_blueprint.py` (N12) needs the parsed AST nodes
    themselves, not just the flattened edge list.
    """
    return _walk(sql, dialect, subject).statements


def parse_procedure_lineage(
    sql: str, dialect: str = "postgres", subject: Mapping[str, str] | None = None
) -> ProcedureParseResult:
    """Procedure-aware column-level lineage extraction (N3). See the module
    docstring for the algorithm and its explicit, code-derived limitations.

    The SQL is never executed. Literal values are never inspected for
    anything but statement-hashing (`_compute_sql_hash`, which itself
    redacts first).

    `subject` binds names the body uses for a relation it does not declare to
    real tables -- a trigger's firing row, the only such case today. Callers
    with a routine body pass nothing and get exactly the parse they always did;
    `parse_trigger_lineage` is the one that supplies it.
    """
    sql_hash = _compute_sql_hash(sql)
    if dialect not in _SQLGLOT_DIALECT_MAP:
        return ProcedureParseResult(
            confidence=Confidence.LOW.value, dialect=dialect, sql_hash=sql_hash,
            errors=[f"unsupported dialect: {dialect}"],
        )
    if not _SQLGLOT_AVAILABLE:
        return ProcedureParseResult(
            confidence=Confidence.LOW.value, dialect=dialect, sql_hash=sql_hash,
            errors=["sqlglot library is not available"],
        )

    walk = _walk(sql, dialect, subject)
    statements = walk.statements
    if not statements:
        return ProcedureParseResult(
            confidence=Confidence.LOW.value, dialect=dialect, sql_hash=sql_hash,
            errors=["no statements found in procedure body"],
            member_attribution=walk.member_attribution,
            member_fallback_reason=walk.member_fallback_reason,
            package_members=walk.members,
        )

    edges: list[ProcedureLineageEdgeRecord] = []
    for group in walk.groups:
        # R11-FP03: hops propagate within one body. A routine is one group, so this
        # is the pass it always had; a split package runs it per member.
        group_edges = [edge for statement in group for edge in statement.edges]
        edges.extend(group_edges)
        edges.extend(_propagate_intermediate_hops(group_edges))
    edges = _dedupe_edges(edges)

    unparsed_reasons = [
        s.unparsed_reason for s in statements if s.is_unparsed and s.unparsed_reason
    ]
    is_fully_parsed = not unparsed_reasons
    has_write = any(s.is_write for s in statements)
    has_real_statement = any(not s.is_no_lineage for s in statements)
    is_read_only = is_fully_parsed and not has_write and has_real_statement

    if not is_fully_parsed:
        confidence = Confidence.LOW.value if not edges else Confidence.PARTIAL.value
    elif edges:
        confidence = (
            Confidence.FULL.value
            if all(e.confidence == Confidence.FULL.value for e in edges)
            else Confidence.PARTIAL.value
        )
    else:
        confidence = Confidence.PARTIAL.value

    return ProcedureParseResult(
        edges=edges,
        statement_count=len(statements),
        confidence=confidence,
        dialect=dialect,
        sql_hash=sql_hash,
        errors=list(unparsed_reasons),
        is_fully_parsed=is_fully_parsed,
        is_read_only=is_read_only,
        statement_text_digest=walk.digest,
        member_attribution=walk.member_attribution,
        member_fallback_reason=walk.member_fallback_reason,
        package_members=walk.members,
    )


def unparsed_marker_result(
    *, reason: str, dialect: str, sql_hash: str, via_routine: str | None = None
) -> ProcedureParseResult:
    """A parse result holding one UNPARSED marker and nothing else.

    For a caller that could not reach a body at all -- R11-FP01's PostgreSQL
    trigger whose action routine is not captured here is the case that needed it.
    Never an empty result: a zero-edge parse reads as an object that touches
    nothing, which is the false-clean reading INV-9 forbids. The marker is built
    by the same helper every in-body gap uses, so its shape cannot drift from
    theirs, and `reason` is the caller's own value-free code -- no body text
    reaches it, because there is none to reach.
    """
    marker = _unparsed_statement(0, dialect, None, reason)
    edges = [replace(edge, via_routine=via_routine) for edge in marker.edges]
    return ProcedureParseResult(
        edges=edges,
        statement_count=1,
        confidence=Confidence.LOW.value,
        dialect=dialect,
        sql_hash=sql_hash,
        errors=[reason],
        is_fully_parsed=False,
        is_read_only=False,
    )


def parse_trigger_lineage(
    sql: str, *, dialect: str, firing_table: str
) -> ProcedureParseResult:
    """One captured trigger body's lineage, with its implicit subject bound.

    The same parse a routine body gets -- a trigger body is the same artifact
    under the same screening rules -- plus the binding a routine body has no
    need of: `firing_table` is attached to this dialect's firing-row names
    before any edge is built, so `INSERT INTO audit ... NEW.customer_id` states
    an edge *from the firing table* rather than from the parser's `UNRESOLVED`
    placeholder. That is the direction that matters: without it the write is
    recorded with no upstream, which reads as a table that changes by itself.

    A firing-row reference this parser cannot bind adds an
    UNRESOLVED_TRIGGER_SUBJECT marker and clears `is_fully_parsed`: the body was
    read, so its writes stand, but the claim that its *sources* are known does
    not. `is_read_only` is cleared with it -- a body whose subject is unbound has
    not been proven to touch nothing.

    Nothing here stores, returns, logs or quotes the body: the marker's reason
    carries the dialect and nothing else (INV-6).
    """
    subject = trigger_subject_aliases(dialect, firing_table)
    result = parse_procedure_lineage(sql, dialect, subject or None)
    if not unbound_trigger_subject(sql, dialect):
        return result
    reason = f"{UnparsedReason.UNRESOLVED_TRIGGER_SUBJECT.value}: {dialect}"
    # R11-FP07: a finding about the body as a whole -- the reference may occur in
    # several statements -- so the marker is NOT_LOCATED, never pinned to one.
    marker = _unparsed_statement(result.statement_count, dialect, None, reason)
    return ProcedureParseResult(
        edges=[*result.edges, *marker.edges],
        statement_count=result.statement_count + 1,
        confidence=Confidence.PARTIAL.value if result.edges else Confidence.LOW.value,
        dialect=result.dialect,
        sql_hash=result.sql_hash,
        errors=[*result.errors, reason],
        is_fully_parsed=False,
        is_read_only=False,
        statement_text_digest=result.statement_text_digest,
    )
