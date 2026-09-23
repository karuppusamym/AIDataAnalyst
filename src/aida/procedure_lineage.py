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
`SELECT` is captured when written as `DECLARE cur CURSOR [options] FOR SELECT
...` or `SET @c = CURSOR FOR SELECT ...` (since 2026-09-19; before that the
DECLARE keyword dropped it and this sentence overclaimed), but the fetch
loop's per-row processing, and data that flows through a variable, is not
modeled beyond its own statements -- a PL/SQL or PL/pgSQL cursor FOR loop's
*record* is (2026-09-19, below); `TRY`/`CATCH` error-handling
logic itself (its *contents* are
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

Token ranges (R11-FP07, 2026-09-19). Inside that statement, each edge also
says where its two ends are named -- `source_token_range` (the column reference
it reads, or for a table-grain edge the table reference) and
`target_token_range` (the column it writes, or the write target) -- in the same
text, as `aida.procedure_token_ranges.TokenRange`. Here sqlglot's identifier
positions *are* used, because they are proved first: each rewrite below keeps
the statement's tail in place (clauses sqlglot cannot read are blanked with
spaces, not cut), and every identifier of the parsed statement must slice out
of the stored text unchanged before any token of it is recorded. A token is
recorded only when exactly one reference in the statement can be the edge's
evidence; two candidates -- the same table named twice, one column read twice
in one expression -- record NULL, never a guess.

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
member-grain and package-grain facts.

PL/SQL calls and declaration sections (R11-FP03, 2026-09-19). Three silent gaps
the member split left:

* **A bare call statement.** PL/SQL calls a procedure by naming it --
  `p(x);`, `pkg.p;` -- with no CALL or EXEC, and sqlglot read that as a lone
  expression with no table in it, so the statement was classed lineage-free. It
  is now a NESTED_PROCEDURE_CALL gap like `CALL p()` (`_PLSQL_CALL_STATEMENT_RE`),
  which `aida.routine_call_descent` reads through where the callee is captured.
  Assignments, NULL/COMMIT/RETURN/RAISE, collection methods and Oracle's own
  housekeeping calls (DBMS_OUTPUT, RAISE_APPLICATION_ERROR, ...) stay
  lineage-free; a call into DBMS_SQL is dynamic SQL.
* **A call between members of one package.** A sibling member's body is in the
  text being parsed, and a member's own routine is captured with no body, so
  catalog descent could never read it. The parse reads it through instead
  (`_member_calls_read_through`): the callee is found among the package's own
  members -- an overload by the arguments as written, never guessed -- and its
  lineage is the caller's at the call, as descent does across routines. A local
  subprogram of the same name shadows the member, as PL/SQL resolves it.
* **Declaration sections.** A routine's, a member's, a nested subprogram's and
  the package's own declarations are walked (`_consume_declarations`): a
  `CURSOR c IS <query>` is read into routine-local state, the declarations PL/SQL
  admits otherwise are lineage-free and produce no statement, and one this
  reader does not recognise is reported. A standalone routine is read the way a
  member is (`_oracle_block`), because taking its body from the first BEGIN in
  the text took a nested subprogram's body instead of its own.

Attribution by scope, and the reads that were still dropped (2026-09-19, found
by R11-FP07 and R11-FP03 and left by both):

* **An unqualified column resolves against its own scope**
  (`aida.procedure_column_owners`). It used to be given to "the statement's one
  source", counted over the whole statement minus its write target -- so a
  DELETE's own target was never a candidate and a table only a subquery names
  was: `DELETE FROM dbo.final WHERE id IN (SELECT r.id FROM dbo.rejects r)`
  recorded `dbo.rejects.id`. Now the query level the column is written in, plus
  the levels it can correlate to, are the candidates; two tables that could own
  it leave it UNRESOLVED, never guessed. A derived table or CTE passes a column
  through only by the same name.
* **A name the routine declares is not a column** -- a parameter, a local, a
  cursor's parameter, a loop record. PL/pgSQL rejects a name that is both, so
  there it is the variable and records nothing (as a T-SQL `@variable` never
  has); PL/SQL lets a column of the same name win, so there it is UNRESOLVED.
  `rec.col` on a declared record is its field; a qualifier that names nothing in
  scope (`seq.NEXTVAL`, `EXCLUDED.v`) is never recorded as a table.
* **Cursor reads.** `OPEN c FOR <query>` (PL/SQL, PL/pgSQL; `FOR <string>` and
  `FOR EXECUTE` are dynamic SQL), T-SQL `DECLARE c CURSOR ... FOR <query>`, and a
  PL/pgSQL routine's DECLARE section -- `c CURSOR (p int) FOR <query>` and a
  variable whose default is a query, nested blocks included -- are read into
  routine-local state. (Later the same day an OUT ref cursor was told apart from a
  local one; see below.)
* **INSERT with a column list** reads its own source query (it used to take the
  first UNION or SELECT anywhere in the statement), zips every branch of a set
  operation, and keeps each branch's WHERE as filter evidence.
* `pkg.delete(x)` is a call unless `pkg` is a declared collection; a simple
  `CASE x WHEN ...` statement and a `<<label>>` are read, not PARSE_ERROR gaps.

Loop records, result cursors and reads that name no column (R11-FP07, 2026-09-19):

* **A cursor FOR loop reads into its record.** `FOR rec IN (<query>) LOOP`, `FOR rec
  IN <query> LOOP` and `FOR rec IN c LOOP` used to put the query's rows in the
  routine's *result set* (or, for a declared cursor, nowhere). Each loop's rows are
  now an intermediate of their own, `<LOCAL:rec@N>` (`loop_record_target`), and a
  statement inside the loop that reads `rec.col` reads that intermediate -- so the hop
  pass joins the loop query's sources to what the loop writes, exactly as it does
  through a temp table. The walk keeps a stack of open LOOPs across chunks, so the
  binding holds from the loop's header to its END LOOP and nowhere else.
* **A ref cursor handed to the caller is the result set.** `OPEN c FOR <query>` on an
  OUT/IN OUT ref-cursor parameter (`SYS_REFCURSOR`, a `REF CURSOR` type the text
  declares, PL/pgSQL `refcursor`), or on the cursor a function returning a ref cursor
  RETURNs, targets `PROCEDURE_RESULT_TARGET`, as `RETURN QUERY` does. Any other
  cursor's rows stay local.
* **A table read without naming a column is a table-grain edge** (`TABLE_ROWS`,
  `_table_rows_read`): `count(*)`, `EXISTS (SELECT 1 ...)`, `SELECT 1 FROM t`. An
  IF/ELSIF/WHILE condition holding a query is read (`CONDITION`) instead of being
  discarded with its header.
* **T-SQL `END` then `IF`/`WHILE`** is two statements (`_ends_compound`); it was read
  as `END IF`/`END WHILE`, and the body's BEGIN never closed.

What the loop-record pass left wrong or missing (R11-FP07, 2026-09-19, second pass):

* **`FOREACH x IN ARRAY <expr> LOOP` is a loop** (`_FOREACH_HEADER_RE`). It was not peeled, so
  it glued itself to the loop's first statement -- one PARSE_ERROR gap, every write inside it
  lost -- but its `END LOOP` was still counted, and closed the *enclosing* loop's record
  binding: a `rec.col` read after it lost its source, and where an inner loop shadows an
  outer record of the same name the popped binding uncovered the outer's rows, a write
  recorded as coming from the wrong table. It binds no record of its own: its variable is
  scalar state, and no scalar variable's value is followed anywhere in this parse (the same
  holds for `SELECT INTO v` and a positional `FOR a, b IN <query>`). An array that holds a
  query is a read into routine-local state (`FOREACH_ARRAY_CONTEXT`).
* **A gap marker's identity is its statement ordinal**, in `_dedupe_edges` and in the stored
  natural key, and every marker read out of one statement carried the statement's own: a
  second table function, or a call in the same statement, was dropped as a duplicate, and
  descent could read the first through and report the routine fully parsed with the second
  never read. Each table-function marker now sits in the counter slot the walk already
  advanced past for it (`_emit`); nothing that is not a marker is renumbered.
* **T-SQL `SET @v = (SELECT ...)`** reads its tables into routine-local state
  (`_TSQL_SET_ASSIGNMENT_RE`). Only an expression that holds a query is read; a scalar
  function's call in a SET is still no call site, as it was.
* **`FETCH c INTO r`** (an explicit open/fetch/close cursor loop) reads a declared cursor's
  rows into the record `r` (`_fetch_read`, `CURSOR_FETCH_CONTEXT`), bound from the FETCH to
  the end of its unit, so `r.col` in what follows carries the cursor's sources to the writes.
  Only where that is sound: a cursor this parse read, ONE target name (several are scalar
  variables; `BULK COLLECT` fills a collection), and a record fetched from one cursor in the
  unit -- two cursors' rows in one record have no one intermediate to stand for them (a shared
  one would put `b -> out1` in the lineage of `FETCH c1 INTO r; INSERT out1 ...r; FETCH c2
  INTO r`), so such a record stays unbound, as before. The same cursor fetched again (the
  priming read) fills the same intermediate. Not modelled: a record's value after its loop.
* **A table counts as read when an edge names it, and only then** (`_table_rows_read`). A
  column in `JOIN ... ON`, a MERGE's `ON` or `WHEN ... AND` condition, `GROUP BY`, `HAVING`,
  `ORDER BY` or a derived table's own clauses named its table without producing an edge, so
  a table read only there was in no answer at all; it is now a table-grain `TABLE_ROWS` read.
  In T-SQL the FROM item an UPDATE or DELETE designates *is* the target
  (`_from_items_the_target_designates`), and a DELETE's `this` is its first FROM item, not
  necessarily its target (`_delete_target`): `DELETE t FROM dbo.a a JOIN dbo.tgt t ON ...` was
  recorded as writing `dbo.a`.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final
from uuid import UUID

from aida.procedure_column_owners import (
    NO_DECLARED_NAMES,
    DeclaredNames,
    resolve_column_owners,
    table_function_name,
    table_reference_name,
)
from aida.procedure_token_ranges import (
    TokenRange,
    locate_edge_tokens,
    remember_parsed_text,
)
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

#: (2026-09-19) Routine-local state that is *read back*: a cursor FOR loop's record. Each
#: loop's rows are an intermediate of their own, `<LOCAL:rec@N>` -- the record's name and
#: the ordinal of the loop's statement -- because `rec` is the conventional name of every
#: loop's record, and one shared name would join the first loop's query to the second
#: loop's writes. Like `<LOCAL>` it can never be a catalog name.
LOOP_RECORD_PREFIX: Final[str] = "<LOCAL:"


def loop_record_target(record: str, ordinal: int, position: int | None = None) -> str:
    """The intermediate a loop record's rows are read into. `position` tells apart a second
    loop opened in the same statement chunk, which shares its ordinal."""
    where = f"{ordinal}" if position is None else f"{ordinal}.{position}"
    return f"{LOOP_RECORD_PREFIX}{record}@{where}>"


def is_routine_local(name: str) -> bool:
    """Whether `name` is one of this parser's routine-local placeholders, never a table."""
    return name == PROCEDURE_LOCAL_TARGET or name.startswith(LOOP_RECORD_PREFIX)


def owned_loop_record(name: str | None, routine: str) -> str | None:
    """A called routine's loop record, renamed as that routine's: `<LOCAL:ops.p:rec@0>`.

    `aida.routine_call_descent` splices a callee's edges into the caller and runs the hop
    pass over both. Ordinals restart in every body, so the callee's `<LOCAL:rec@0>` and the
    caller's are the same name for two different loops -- and the hop pass would join each
    loop's query to the other's writes. Any other name is returned as it is."""
    if name is None or not name.startswith(LOOP_RECORD_PREFIX):
        return name
    return f"{LOOP_RECORD_PREFIX}{routine}:{name[len(LOOP_RECORD_PREFIX) :]}"


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

    __slots__ = ("_length", "_line_starts", "text")

    def __init__(self, text: str) -> None:
        #: The text itself, which R11-FP07's token ranges are verified against.
        self.text = text
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


#: R11-FP03: why a call between members of one package was not read through, written
#: after the callee as `name (CODE)` -- the same words, and the same shape,
#: `aida.routine_call_descent` uses for a call across routines (restated because that
#: module imports this one). No single member accepts the call as written -- two
#: overloads the text cannot tell apart, or none with those parameters:
MEMBER_CALL_AMBIGUOUS: Final[str] = "AMBIGUOUS"
#: The member called, or one it calls in turn, has a gap of its own:
MEMBER_CALL_NOT_FULLY_PARSED: Final[str] = "CALLEE_NOT_FULLY_PARSED"


@dataclass(frozen=True, slots=True)
class PackageMember:
    """One member subprogram a package body defines, as the parse found it.

    `parameter_names` is how the package body spells the member's parameters,
    in order: identifiers, never defaults (a default can be a literal). PL/SQL
    requires a body's header to repeat its spec's parameter names, which is what
    lets `routine_lineage_edges.resolve_package_member_ids` tell two overloads
    of one name apart against the captured members' parameters.
    `first_ordinal`/`last_ordinal` bound the statements walked from this
    member -- its declarations, nested subprograms and body since R11-FP03's
    second pass, and the calls it makes; both `None` for a member with none.
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
    # R11-FP03: the callee's own captured routine id -- the routine `via_routine` names,
    # never the caller's -- when it is already known (a cross-routine call resolved by
    # `aida.routine_call_descent`, which has the catalog in hand). `None` either because
    # the edge is not spliced at all, or because it is spliced from an in-package sibling
    # call, whose callee is resolved from `via_routine_locator` instead (below): the pure
    # parser has no catalog to ask.
    via_routine_id: UUID | None = None
    # R11-FP03: set only on an edge spliced in from an in-package sibling member call
    # (`_read_through`) or from this module's own post-descent reconciliation of one --
    # a statement ordinal inside the *callee* member's own `[first_ordinal, last_ordinal]`
    # span, in this same parse's numbering. `routine_edge_row` resolves it to the
    # callee's captured routine id the same way `member_routine_id` is resolved, via
    # `resolve_package_member_ids`, because only that lookup can tell two overloads of
    # one member name apart. Never set alongside `via_routine_id`.
    via_routine_locator: int | None = None
    # R11-FP07 source-range maps: where `statement_ordinal`'s statement is in the
    # text this parse read, what the range is the range of, and the digest of that
    # text. Defaults are the honest "not located" -- an edge built on a path that
    # has no text to point into never claims a position.
    statement_range: StatementRange | None = None
    statement_range_status: str = StatementRangeStatus.NOT_LOCATED.value
    statement_text_digest: str | None = None
    # R11-FP07 token grain: where, inside that statement and in the same text, the
    # edge's source and target are named (`aida.procedure_token_ranges`). `None`
    # wherever the reference is not exactly one token of this statement.
    source_token_range: TokenRange | None = None
    target_token_range: TokenRange | None = None
    # R11-FP03: for an Oracle package's parse, the member this edge belongs to and
    # the grain it is attributed at (`MemberAttribution`). Both `None` on an edge
    # from any other routine.
    package_member: str | None = None
    member_attribution: str | None = None


@dataclass(frozen=True, slots=True)
class PendingMemberCall:
    """R11-FP03: a sibling-member call whose completeness `_member_calls_read_through`
    could not decide at parse time, because a member it reaches still had its own
    not-yet-resolved external call -- one only catalog descent (`aida.routine_call_descent`,
    which runs *after* this parse) can resolve. Recorded so descent can re-check the
    call once it has resolved every ordinary gap in this same pass, instead of the
    call being stuck with the pessimistic marker `_member_calls_read_through` had to
    write when it could not yet know better.

    `statement_ordinal` identifies the call's own UNPARSED marker edge (one call site
    per statement, so this is unique). `reached_member_indices` are the members this
    call reads through, in resolution order and indexing `ProcedureParseResult.
    package_members` -- the first is the member named at the call, matching
    `via_routine`, which is the display name an eager in-package resolution would
    have used.
    """

    statement_ordinal: int
    reached_member_indices: tuple[int, ...]
    via_routine: str


@dataclass(frozen=True, slots=True)
class CallSite:
    """R11-FP03: a PL/SQL call statement as written -- the name it calls, and for each
    argument the parameter it is passed to by name (`p_id => x`), or `None` when it is
    passed by position. Never an argument's value (INV-6): names are all that telling
    two overloads apart needs."""

    callee: str
    argument_names: tuple[str | None, ...]


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
    #: R11-FP03: a PL/SQL call statement's callee and argument names, which a split
    #: package resolves against its own members; `None` on anything else.
    call_site: CallSite | None = None


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
    # R11-FP03: sibling calls whose completeness `_member_calls_read_through` deferred
    # to descent; see `PendingMemberCall`. Empty for anything but a split package with
    # at least one such call. Cleared once `aida.routine_call_descent` reconciles them.
    pending_member_calls: tuple[PendingMemberCall, ...] = ()


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
            if following in _NON_BEGIN_END_CLOSERS and _ends_compound(words, index + 1):
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


def _ends_compound(words: list[tuple[int, int, str]], at: int) -> bool:
    """Whether the IF/LOOP/WHILE at `at`, just after an END, is that END's own -- `END IF;`,
    `END LOOP [label];` -- rather than the next statement's first word.

    (2026-09-19) T-SQL has no END IF or END WHILE: its `END` is followed by the next
    statement, and `END` then `IF @x = 1 BEGIN` or `WHILE (SELECT ...) > 0 BEGIN` is two
    statements. Read as a compound closer, that END closed nothing, so the body's BEGIN
    never closed and the whole definition -- header included -- was parsed as the body:
    the first statement glued to `CREATE PROCEDURE` and whatever it wrote was lost. A
    compound closer ends its statement: a `;` (or the text) follows it, or for LOOP a
    label and then the `;`. Callers pass every token -- `;` and parentheses included."""
    after = words[at + 1][2] if at + 1 < len(words) else None
    if after is None or after == ";":
        return True
    return (
        words[at][2] == "LOOP"
        and after not in ("(", ")")
        and (at + 2 >= len(words) or words[at + 2][2] == ";")
    )


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
    # Every token, `;` included: `_ends_compound` needs to see where a statement ends.
    words = [(start, end, sql[start:end].upper()) for start, end, _kind in tokens]
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
# PL/SQL `FOR rec IN (<query>) LOOP`, up to its opening parenthesis: the query ends at the
# parenthesis that closes it (`_parenthesised_loop`). A greedy match to the last `)` before
# a LOOP swallowed a nested loop's header opened in the same chunk into this loop's query.
_CURSOR_FOR_LOOP_RE = re.compile(r"^\s*FOR\s+(?P<record>\w+)\s+IN\s*\(", re.IGNORECASE)
_LOOP_KEYWORD_RE = re.compile(r"\s*LOOP\b\s*", re.IGNORECASE)
_BARE_FOR_LOOP_RE = re.compile(r"^\s*FOR\b.*?\bLOOP\b\s*", re.IGNORECASE | re.DOTALL)
# PL/pgSQL `FOR rec IN SELECT ... LOOP` -- the query is not parenthesised, so
# without this the bare-FOR peel above would discard it silently. `IN EXECUTE`
# is handed on too, and classified as dynamic SQL.
_FOR_IN_QUERY_LOOP_RE = re.compile(
    r"^\s*FOR\s+(?P<targets>[A-Za-z_][\w$]*(?:\s*,\s*[A-Za-z_][\w$]*)*)\s+IN\s+"
    r"(?P<select>(?:SELECT|WITH|EXECUTE)\b.*?)\s*\bLOOP\b\s*",
    re.IGNORECASE | re.DOTALL,
)
# (2026-09-19) `FOR rec IN c [(args)] LOOP` -- a loop over a cursor the routine declared
# (PL/SQL's explicit cursor FOR loop; PL/pgSQL's loop over a bound cursor). It fetches
# the declared query's rows into `rec`, which the bare-FOR peel read as a loop with no
# rows at all. Checked after the two query-loop shapes and before the bare one: `FOR i IN
# 1..10`, `FOR i IN REVERSE a..b` and `FOR i IN lo .. hi` never have a lone name followed
# by LOOP or an argument list.
_DECLARED_CURSOR_LOOP_RE = re.compile(
    r"^\s*FOR\s+(?P<record>[A-Za-z_][\w$#]*)\s+IN\s+"
    r"(?P<cursor>[A-Za-z_][\w$#]*(?:\s*\.\s*[A-Za-z_][\w$#]*)?)\s*(?:\([^;]*?\)\s*)?LOOP\b\s*",
    re.IGNORECASE | re.DOTALL,
)
_BARE_LOOP_RE = re.compile(r"^\s*LOOP\b\s*", re.IGNORECASE)
# (2026-09-19) PL/pgSQL `FOREACH x [SLICE n] IN ARRAY <expression> LOOP`, up to the start of the
# expression: the expression ends at the LOOP keyword (`_foreach_loop`). `FOR\b` never matched
# it, so its header was not peeled -- it glued itself to the loop's first statement, one
# PARSE_ERROR gap, and every write inside a FOREACH was lost -- while its END LOOP was still
# counted, and closed the enclosing loop's record binding.
_FOREACH_HEADER_RE = re.compile(
    r"^\s*FOREACH\s+[A-Za-z_][\w$]*(?:\s*,\s*[A-Za-z_][\w$]*)*\s+(?:SLICE\s+\d+\s+)?"
    r"IN\s+ARRAY\b\s*",
    re.IGNORECASE,
)
#: The `control_flow_context` of the read a FOREACH loop's array holds -- `FOREACH x IN ARRAY
#: (SELECT array_agg(...) FROM t) LOOP`.
FOREACH_ARRAY_CONTEXT: Final[str] = "FOREACH_ARRAY"
# A bare BEGIN/END with more text following in the same chunk: T-SQL does
# not require a `;` after a bare BEGIN/END, so the statement splitter (which
# only splits on `;`) legitimately produces e.g. "END\n\nINSERT INTO ..." as
# one raw chunk when the source omits that optional semicolon -- these peel
# the leading structural keyword off so the real statement underneath still
# gets classified, rather than the whole chunk falling through to UNPARSED.
_BARE_BEGIN_MID_RE = re.compile(r"^\s*BEGIN\s+", re.IGNORECASE)
# (2026-09-19) Not `END IF` / `END WHILE`: with more text after it in the same chunk that is
# T-SQL's END followed by the next statement (`END` then `IF @x = 1 BEGIN ...`), and eating
# the IF left its condition to be parsed as a statement. PL/SQL's and PL/pgSQL's END IF
# ends its chunk, which `_STRUCTURAL_ONLY_RE` reads. See `_ends_compound`.
_BARE_END_MID_RE = re.compile(r"^\s*END(?:\s+(?:LOOP|CASE|TRY|CATCH))?\s+", re.IGNORECASE)
# A searched `CASE WHEN c THEN` or (2026-09-19) a simple `CASE selector WHEN v THEN`
# statement; the selector was left in front of the branch's statement, which made
# every simple CASE statement a PARSE_ERROR gap. A chunk starting with CASE is always
# the statement form: an expression cannot start a statement.
_CASE_WHEN_THEN_RE = re.compile(
    r"^\s*(?:CASE\b.*?)?\bWHEN\b.*?\bTHEN\b\s*", re.IGNORECASE | re.DOTALL
)
# (2026-09-19) A PL/SQL or PL/pgSQL statement label, `<<name>>`, in front of a
# statement or block. It names the construct for EXIT/CONTINUE/GOTO and carries no
# lineage; left in place it made the statement after it a PARSE_ERROR gap.
_LABEL_RE = re.compile(r"^\s*<<\s*[A-Za-z_][\w$#]*\s*>>\s*")
# PL/SQL and PL/pgSQL `EXCEPTION WHEN <condition> THEN`: the handler's statements
# are walked like any branch; later `WHEN ... THEN` handlers peel as CASE_BRANCH.
_EXCEPTION_WHEN_RE = re.compile(
    r"^\s*EXCEPTION\s+WHEN\b.*?\bTHEN\b\s*", re.IGNORECASE | re.DOTALL
)
_MAX_PEEL_ITERATIONS: Final[int] = 8
#: `END LOOP`, alone or at the front of a chunk: it closes the innermost open loop.
_END_LOOP_RE = re.compile(r"^\s*END\s+LOOP\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _LoopHeader:
    """(2026-09-19) One `FOR <record> IN ... LOOP` header a chunk opened: where its rows
    come from and the variable each is fetched into.

    Offsets are in the chunk the header was peeled from. `query` is the loop's own query
    as written (PL/SQL: inside its parentheses), or None for a loop over a declared
    cursor, which names `cursor` instead. `record` is None when the loop fetches into
    several variables (PL/pgSQL `FOR a, b IN SELECT ...`): positional variables are not
    modelled, so nothing reads the rows back.
    """

    record: str | None
    header_start: int
    header_end: int
    query: str | None = None
    query_offset: int | None = None
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class _Condition:
    """(2026-09-19) An IF/ELSIF/WHILE condition that holds a query -- `IF EXISTS (SELECT 1
    FROM t)` -- and where it starts in its chunk. The peel used to discard every condition,
    so the read in one was never seen at all."""

    text: str
    offset: int
    #: What the read is labelled: None for a branch or loop condition (`CONDITION_CONTEXT`),
    #: `FOREACH_ARRAY_CONTEXT` for the array a FOREACH walks.
    context: str | None = None


@dataclass(frozen=True, slots=True)
class _PeelResult:
    remainder: str
    control_flow_context: str | None
    #: R11-FP07: where `remainder` starts in the chunk it was peeled from. The peel
    #: only ever cuts from the front and trims, so the remainder is a contiguous
    #: piece of the chunk and this one number locates it.
    remainder_offset: int = 0
    #: R11-FP03: an END was peeled off this chunk, so a lone name left after it is
    #: the END's label (`END refresh;`, `END LOOP outer;`) -- not a PL/SQL call.
    follows_end: bool = False
    #: (2026-09-19) Every LOOP this chunk opens, in order: a `_LoopHeader` for a cursor
    #: FOR loop, None for one that fetches no rows (WHILE, bare LOOP, a numeric FOR). The
    #: walk keeps them on a stack so a statement inside a loop can read its record.
    loops_opened: tuple[_LoopHeader | None, ...] = ()
    #: How many `END LOOP`s it closes. They come first: an `END LOOP` ends its chunk.
    loops_closed: int = 0
    #: The conditions it peeled that hold a query.
    conditions: tuple[_Condition, ...] = ()


def _parenthesised_loop(text: str, opened: int) -> tuple[int, int] | None:
    """For a `FOR rec IN (` whose `(` is at `opened`: the index of the `)` that closes it --
    quote- and comment-aware -- and the end of the `LOOP` keyword after it; None when no
    LOOP follows (`FOR i IN (lo)..(hi) LOOP` is a numeric range, not a query)."""
    depth = 0
    for start, _end, kind in _scan_tokens(text[opened:]):
        if kind != "other":
            continue
        depth += 1 if text[opened + start] == "(" else -1
        if depth == 0:
            close = opened + start
            after = _LOOP_KEYWORD_RE.match(text, close + 1)
            return (close, after.end()) if after else None
    return None


def _foreach_loop(text: str, start: int) -> tuple[int, int] | None:
    """For a `FOREACH x IN ARRAY <expression> LOOP` whose expression begins at `start`: where
    the expression ends -- at the LOOP keyword -- and where the header does, past it and the
    space after. The keyword is the first LOOP outside parentheses, quotes and comments, so a
    `LOOP` inside a string literal in the expression ends nothing. None when no LOOP follows:
    a header this cannot find the end of is left for the caller to report."""
    depth = 0
    for first, last, kind in _scan_tokens(text[start:]):
        at = start + first
        if kind == "other":
            depth += 1 if text[at] == "(" else -1
        elif kind == "word" and depth == 0 and text[at : start + last].upper() == "LOOP":
            after = _LOOP_KEYWORD_RE.match(text, at)
            return (at, after.end()) if after else None
    return None


def _peel_control_flow_prefix(chunk: str) -> _PeelResult:
    """Repeatedly strip a recognised control-flow header from the front of
    `chunk`. Returns the leftover text (empty if the chunk was purely
    structural), the innermost control-flow context peeled (for evidence),
    and what the headers themselves read: each cursor FOR loop's query (a
    PL/SQL `FOR r IN (SELECT ...) LOOP`, a PL/pgSQL `FOR r IN SELECT ... LOOP`)
    or declared cursor, and each IF/WHILE condition that holds a query -- all
    carrying their offset in `chunk` (R11-FP07), counted as each header is cut
    off, and read separately by the caller.
    """
    if _STRUCTURAL_ONLY_RE.match(chunk):
        return _PeelResult("", None, len(chunk), loops_closed=int(bool(_END_LOOP_RE.match(chunk))))

    remainder = chunk
    consumed = 0
    context: str | None = None
    follows_end = False
    loops: list[_LoopHeader | None] = []
    closed = 0
    conditions: list[_Condition] = []

    def condition(match: re.Match[str]) -> None:
        text = match.group("cond")
        if _has_query_word(text):
            conditions.append(_Condition(text, consumed + match.start("cond")))

    for _ in range(_MAX_PEEL_ITERATIONS):
        # A comment between two headers (`IF x BEGIN -- why` then the statement) is
        # not the statement; skipping it keeps each header visible to the next peel
        # and starts the remainder -- and its range -- at the statement itself.
        skipped = _skip_trivia(remainder, 0, len(remainder))
        consumed += skipped
        remainder = remainder[skipped:]
        if match := _LABEL_RE.match(remainder):
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if (match := _CURSOR_FOR_LOOP_RE.match(remainder)) and (
            spans := _parenthesised_loop(remainder, match.end() - 1)
        ):
            close, end = spans
            loops.append(
                _LoopHeader(
                    record=match.group("record"),
                    header_start=consumed,
                    header_end=consumed + end,
                    query=remainder[match.end() : close],
                    query_offset=consumed + match.end(),
                )
            )
            context = "CURSOR_FOR_LOOP"
            consumed += end
            remainder = remainder[end:]
            continue
        if match := _IF_BEGIN_RE.match(remainder):
            condition(match)
            context = "IF_BRANCH"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _IF_THEN_RE.match(remainder):
            condition(match)
            context = "IF_BRANCH"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _WHILE_BEGIN_RE.match(remainder):
            condition(match)
            context = "WHILE_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _WHILE_LOOP_RE.match(remainder):
            condition(match)
            loops.append(None)
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
            targets = [name.strip() for name in match.group("targets").split(",")]
            loops.append(
                _LoopHeader(
                    record=targets[0] if len(targets) == 1 else None,
                    header_start=consumed,
                    header_end=consumed + match.end(),
                    query=match.group("select"),
                    query_offset=consumed + match.start("select"),
                )
            )
            context = "CURSOR_FOR_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _DECLARED_CURSOR_LOOP_RE.match(remainder):
            loops.append(
                _LoopHeader(
                    record=match.group("record"),
                    header_start=consumed,
                    header_end=consumed + len(match.group(0).rstrip()),
                    cursor=re.sub(r"\s+", "", match.group("cursor")),
                )
            )
            context = "CURSOR_FOR_LOOP"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if (match := _FOREACH_HEADER_RE.match(remainder)) and (
            spans := _foreach_loop(remainder, match.end())
        ):
            # A loop of its own: it pushes one entry so its END LOOP pops that entry and no
            # other. It binds no record -- its variable is scalar state, whose value is the
            # array's, and no scalar variable's value is followed anywhere in this parse.
            array_end, header_end = spans
            array = remainder[match.end() : array_end]
            if _has_query_word(array):
                conditions.append(_Condition(array, consumed + match.end(), FOREACH_ARRAY_CONTEXT))
            loops.append(None)
            context = "FOR_LOOP"
            consumed += header_end
            remainder = remainder[header_end:]
            continue
        if match := _BARE_FOR_LOOP_RE.match(remainder):
            loops.append(None)
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
            loops.append(None)
            context = context or "LOOP_BLOCK"
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _BARE_BEGIN_MID_RE.match(remainder):
            consumed += match.end()
            remainder = remainder[match.end() :]
            continue
        if match := _BARE_END_MID_RE.match(remainder):
            closed += int(bool(_END_LOOP_RE.match(remainder)))
            consumed += match.end()
            remainder = remainder[match.end() :]
            follows_end = True
            continue
        if _STRUCTURAL_ONLY_RE.match(remainder):
            closed += int(bool(_END_LOOP_RE.match(remainder)))
            remainder = ""
            break
        break
    lead = len(remainder) - len(remainder.lstrip())
    return _PeelResult(
        remainder.strip(),
        context,
        consumed + lead,
        follows_end,
        loops_opened=tuple(loops),
        loops_closed=closed,
        conditions=tuple(conditions),
    )


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
#: R11-FP03: a PL/SQL call statement. PL/SQL calls a procedure by naming it -- `p;`,
#: `p(x)`, `pkg.p(a => 1)`, `schema.pkg.p(x)`, `p@link(x)` -- with no CALL or EXEC,
#: so sqlglot reads the statement as a lone function or column expression with no
#: table in it, and every such call used to be classed lineage-free: a body that
#: calls a writer read as fully parsed and read-only. Oracle only -- T-SQL needs
#: EXEC and PL/pgSQL PERFORM or CALL, so a bare name there is not a call. That the
#: argument list is one balanced group is checked by `_plsql_call`, quote-aware.
_PLSQL_CALL_STATEMENT_RE = re.compile(
    r"^\s*(?P<callee>[A-Za-z][\w$#]*(?:\s*\.\s*[A-Za-z][\w$#]*){0,2}"
    r"(?:\s*@\s*[A-Za-z][\w$#]*(?:\.[A-Za-z][\w$#]*)*)?)"
    r"\s*(?P<arguments>\(.*\))?\s*$",
    re.DOTALL,
)
#: What an END's label leaves once `_BARE_END_MID_RE` has peeled the END: a lone
#: name, the very shape of a call with no arguments (`_PeelResult.follows_end`).
_PLSQL_END_LABEL_RE = re.compile(r"^\s*[A-Za-z][\w$#]*\s*$")
#: Words a PL/SQL statement may consist of alone that name no subprogram.
_PLSQL_STATEMENT_WORDS: Final = frozenset(
    {"NULL", "BEGIN", "END", "ELSE", "LOOP", "RETURN", "EXIT", "CONTINUE", "RAISE",
     "COMMIT", "ROLLBACK", "TRUE", "FALSE"}
)
#: Oracle-supplied subprograms whose call moves no table data: raising an error, and
#: the output, session, instrumentation and statistics packages. `DBMS_OUTPUT` is
#: already lineage-free by keyword (`_NO_LINEAGE_KEYWORDS_RE`); it is here for the
#: `SYS.`-qualified spelling. A call into anything else Oracle supplies stays a
#: nested call -- `DBMS_MVIEW.REFRESH` writes a materialized view, `UTL_FILE` writes
#: files -- because an unknown callee is a gap, never a clean statement.
_PLSQL_LINEAGE_FREE_ROUTINES: Final = frozenset({"RAISE_APPLICATION_ERROR"})
_PLSQL_LINEAGE_FREE_PACKAGES: Final = frozenset(
    {"DBMS_OUTPUT", "DBMS_APPLICATION_INFO", "DBMS_LOCK", "DBMS_SESSION", "DBMS_STATS"}
)
#: Oracle's dynamic-SQL package: a call into it runs a string built at runtime.
_PLSQL_DYNAMIC_SQL_PACKAGES: Final = frozenset({"DBMS_SQL"})
#: Collection methods written as statements (`v.EXTEND;`, `v.DELETE(1);`): they resize
#: a variable, never a table.
_PLSQL_COLLECTION_METHODS: Final = frozenset({"DELETE", "EXTEND", "TRIM"})
#: R11-FP03: a PL/SQL cursor declaration -- `CURSOR c [(params)] [RETURN type] IS
#: <query>`, or a cursor spec with no IS and no query. Its query is a read that used
#: to be dropped silently, because no declaration section was walked. `DECLARE` may
#: lead it where a nested block's first declaration reached the splitter glued to
#: its DECLARE -- which otherwise classed the chunk lineage-free by that keyword.
_PLSQL_CURSOR_DECLARATION_RE = re.compile(
    r"^\s*(?:DECLARE\s+)?CURSOR\s+[A-Za-z][\w$#]*", re.IGNORECASE
)
#: R11-FP03: every other declaration a PL/SQL declaration section holds -- TYPE,
#: SUBTYPE and PRAGMA, and item declarations: `v NUMBER := 0`, `c CONSTANT ...`,
#: `e EXCEPTION`, `r ops.orders%ROWTYPE`, `n ops.orders.id%TYPE`. None reads a table.
#: PL/SQL admits no subquery in a default (PLS-00405), and a `%TYPE`/`%ROWTYPE`
#: anchor takes a table's *structure* -- its column list and types -- not its rows:
#: no data flows from the table, so recording it as a read would put the routine in
#: every "who reads this table?" answer it has no part in. That is a dependency on
#: the table's definition, which this lineage does not model.
_PLSQL_LINEAGE_FREE_DECLARATION_RE = re.compile(
    r'^\s*(?:(?:TYPE|SUBTYPE|PRAGMA)\b|(?:"[^"]+"|[A-Za-z][\w$#]*)\s+[A-Za-z"])',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Cursor reads that were dropped (2026-09-19, found by R11-FP03/FP07). Each of
# these used to reach `_NO_LINEAGE_KEYWORDS_RE` (OPEN, DECLARE, SET) or never be
# walked at all, so the query it names read as no lineage.
# ---------------------------------------------------------------------------

#: `OPEN c FOR <query>` -- a PL/SQL ref cursor, a PL/pgSQL unbound cursor, which may
#: say `[NO] SCROLL`. What follows FOR is a query, or a string built at runtime:
#: `OPEN c FOR v_sql [USING ...]` (PL/SQL), `OPEN c FOR EXECUTE ...` (PL/pgSQL).
_OPEN_FOR_RE = re.compile(
    r"^\s*OPEN\s+:?(?P<cursor>[A-Za-z_][\w$#.]*)\s+(?:(?:NO\s+)?SCROLL\s+)?FOR\b\s*",
    re.IGNORECASE,
)
#: What starts a query rather than an expression that evaluates to one.
_QUERY_START_RE = re.compile(r"^\s*(?:\(\s*)*(?:SELECT|WITH)\b", re.IGNORECASE)
#: `FETCH c INTO r` (PL/SQL) and `FETCH [NEXT] [FROM | IN] c INTO r` (PL/pgSQL) into ONE name --
#: a record, which the rows of a cursor the routine declared are fetched into (2026-09-19).
#: Several targets are scalar variables, `BULK COLLECT` fills a collection, and T-SQL's `@v`
#: never matches a bare name: none of those is a record whose fields a later statement reads.
_FETCH_INTO_RE = re.compile(
    r"^\s*FETCH\s+(?:(?:NEXT|PRIOR|FIRST|LAST|FORWARD|BACKWARD)\s+)?(?:(?:FROM|IN)\s+)?"
    r"(?P<cursor>[A-Za-z_][\w$#]*)\s+INTO\s+(?P<target>[A-Za-z_][\w$#]*)\s*$",
    re.IGNORECASE,
)
#: The `control_flow_context` of the read a `FETCH c INTO r` states.
CURSOR_FETCH_CONTEXT: Final[str] = "CURSOR_FETCH"
#: T-SQL `DECLARE c [INSENSITIVE] [SCROLL] CURSOR [options] FOR <query>` and a cursor
#: variable's `SET @c = CURSOR [options] FOR <query>`. The module docstring has long
#: said the declaration's query was captured; it was dropped by the DECLARE keyword.
_TSQL_CURSOR_DECLARATION_RE = re.compile(
    r"^\s*(?:DECLARE\s+[A-Za-z_#][\w@#$]*\s+(?:INSENSITIVE\s+)?(?:SCROLL\s+)?CURSOR"
    r"|SET\s+@[\w@#$]+\s*=\s*CURSOR)\b(?:\s+[A-Za-z_]+)*?\s+FOR\b\s*",
    re.IGNORECASE,
)
#: ... whose query may end `FOR READ ONLY` or `FOR UPDATE [OF cols]`: the cursor's
#: updatability, not the query's lineage, and not T-SQL sqlglot reads.
_TSQL_CURSOR_TAIL_RE = re.compile(
    r"\s+FOR\s+(?:READ\s+ONLY|UPDATE(?:\s+OF\s+.+)?)\s*$", re.IGNORECASE | re.DOTALL
)
#: T-SQL `SET @v = <expression>` -- and `+=`, `-=` and the other compound forms -- up to the
#: expression. The `SET` keyword marks a statement lineage-free (`_NO_LINEAGE_KEYWORDS_RE`), which
#: is right for `SET NOCOUNT ON` and `SET @v = @v + 1` and wrong for `SET @v = (SELECT ...)`: a
#: scalar subquery read into a variable is a read of its tables (2026-09-19). Only an
#: expression that holds a query is read here (`_has_query_word`, quote-aware).
_TSQL_SET_ASSIGNMENT_RE = re.compile(
    r"^\s*SET\s+@[\w@#$]+\s*[-+*/%&|^]?=\s*(?P<expr>.+)$", re.IGNORECASE | re.DOTALL
)
#: PL/pgSQL `name [[NO] SCROLL] CURSOR [(args)] {FOR | IS} <query>` -- a bound cursor
#: declaration -- led by DECLARE when it is a nested block's first declaration.
_PLPGSQL_CURSOR_DECLARATION_RE = re.compile(
    r"^\s*(?:DECLARE\s+)?[A-Za-z_][\w$]*\s+(?:(?:NO\s+)?SCROLL\s+)?CURSOR\b", re.IGNORECASE
)
#: PL/pgSQL `name [CONSTANT] type [COLLATE c] [NOT NULL] [{DEFAULT | := | =} expr]`:
#: the name, and everything after it. `name ALIAS FOR $n` matches too, and is lineage-free.
_PLPGSQL_ITEM_DECLARATION_RE = re.compile(
    r'^\s*(?:DECLARE\s+)?(?P<name>"[^"]+"|[A-Za-z_][\w$]*)\s+(?P<rest>[A-Za-z_"].*)$',
    re.IGNORECASE | re.DOTALL,
)
#: A PL/pgSQL declaration section's opening keyword, when a nested block's first
#: declaration reaches the splitter glued to it.
_DECLARE_PREFIX_RE = re.compile(r"^\s*DECLARE\b\s*", re.IGNORECASE)
#: Words that begin a PL/pgSQL *statement*: a chunk starting with one is never read as
#: a declaration outside a declaration section, whatever follows it.
_PLPGSQL_STATEMENT_WORDS: Final = frozenset(
    {
        "ALTER", "ANALYZE", "ASSERT", "BEGIN", "CALL", "CASE", "CLOSE", "CLUSTER", "COMMENT",
        "COMMIT", "CONTINUE", "COPY", "CREATE", "DEALLOCATE", "DELETE", "DISCARD", "DO", "DROP",
        "ELSE", "ELSIF", "ELSEIF", "END", "EXECUTE", "EXIT", "EXPLAIN", "FETCH", "FOR",
        "FOREACH", "GET", "GRANT", "IF", "IMPORT", "INSERT", "LISTEN", "LOCK", "LOOP", "MERGE",
        "MOVE", "NOTIFY", "NULL", "OPEN", "PERFORM", "PREPARE", "RAISE", "REFRESH", "REINDEX",
        "RELEASE", "RESET", "RETURN", "REVOKE", "ROLLBACK", "SAVEPOINT", "SECURITY", "SELECT",
        "SET", "SHOW", "TRUNCATE", "UPDATE", "VACUUM", "VALUES", "WHEN", "WHILE", "WITH",
    }
)
#: PL/pgSQL's per-function override of how a name that is both a variable and a
#: column resolves (`aida.procedure_column_owners.DeclaredNames`).
_VARIABLE_CONFLICT_RE = re.compile(
    r"#\s*variable_conflict\s+(?P<mode>error|use_variable|use_column)\b", re.IGNORECASE
)

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


#: A table-valued function used as a source (`FROM s.fn(2) n`): its qualified name, or
#: `None` for a table. Lives with the scope resolver, which needs it too; see there.
_table_function_name = table_function_name


@dataclass(frozen=True, slots=True)
class _ScopedNames(DeclaredNames):
    """What the routine declares, plus what two kinds of declared name *mean* at the point
    a statement is read (2026-09-19). Carried where `DeclaredNames` already is, because
    both answer the question it answers -- what is this name, if not a column?

    * `records`: each loop record open around the statement, bound to the intermediate
      its loop's rows are read into (`loop_record_target`). `rec.amount` inside the
      loop is that intermediate's `amount`, so the write that uses it continues the
      loop query's lineage instead of ending it.
    * `result_cursors`: the ref cursors whose rows the routine hands its caller -- an
      OUT (or IN OUT/INOUT) ref-cursor parameter, or a cursor variable a function
      returning a ref cursor RETURNs. `OPEN c FOR <query>` on one is the routine's result.
    """

    #: `(lower-cased record name, intermediate)`, outermost loop first.
    records: tuple[tuple[str, str], ...] = ()
    #: Lower-cased.
    result_cursors: frozenset[str] = frozenset()

    def with_names(self, more: frozenset[str]) -> _ScopedNames:
        return replace(self, names=self.names | more)


def _scoped(names: DeclaredNames) -> _ScopedNames:
    return (
        names
        if isinstance(names, _ScopedNames)
        else _ScopedNames(names.names, names.variable_wins)
    )


def _records(names: DeclaredNames) -> dict[str, str]:
    """The loop records bound where a statement is read; the innermost binding wins."""
    return dict(names.records) if isinstance(names, _ScopedNames) else {}


def _resolve_owners(
    node: object,
    subject: Mapping[str, str] | None,
    names: DeclaredNames,
) -> None:
    """Resolve every column reference in `node` by scope (`aida.procedure_column_owners`),
    before any extractor reads it. Every parsed statement passes through here: the owners
    it records replace the single-source attribution this module used to apply after the
    fact, which counted tables over the whole statement and never its own target.

    A loop record open around the statement (2026-09-19) is bound here, under the
    spelling each reference uses: the resolver reads a qualifier that names no FROM item
    through this alias map, so `rec.amount` resolves to the loop's intermediate -- and a
    FROM item the statement itself calls `rec` still wins, as it does in the engine."""
    aliases = _collect_table_aliases_with_temp(node, subject)[0]
    records = _records(names)
    if records and isinstance(node, exp.Expression):
        for column in node.find_all(exp.Column):
            bound = records.get(column.table.lower()) if column.table else None
            if bound is not None:
                aliases.setdefault(column.table, bound)
    resolve_column_owners(node, aliases=aliases, declared=names)


def _relation_tables(relation: object) -> list[exp.Table]:
    """The `exp.Table`s a single FROM/JOIN item contributes at its own level --
    mirrors `procedure_column_owners._Resolver._relation_items`'s walk (a
    parenthesised join unwraps; a derived table, UNNEST, LATERAL or VALUES
    contributes none, since none of those is a `Table` a bare alias can be
    resolved through) without that class's per-column bookkeeping, which this
    alias map does not need."""
    if not isinstance(relation, exp.Expression):
        return []
    if isinstance(relation, exp.Table):
        tables = [relation]
    elif isinstance(relation, exp.Subquery) and not isinstance(relation.this, exp.Query):
        tables = _relation_tables(relation.this)  # a parenthesised join, not a query
    else:
        tables = []
    for join in relation.args.get("joins") or []:
        if isinstance(join, exp.Join):
            tables.extend(_relation_tables(join.this))
    return tables


def _query_scope_tables(query: object) -> list[exp.Table]:
    """The tables visible at `query`'s own top level: its FROM/JOIN items, and
    -- recursively -- both arms of a set operation. Never a derived table's,
    a CTE's, or a WHERE/SET/branch subquery's own body: each of those opens a
    scope of its own, invisible outside itself, exactly as
    `procedure_column_owners._Resolver._levels` already treats it when it
    resolves a column one statement over. `_statement_scope_tables` is the
    entry point; this only recurses into a set operation's arms, which are
    siblings of the same top-level scope, not a nested one."""
    if isinstance(query, exp.Select):
        tables: list[exp.Table] = []
        from_ = query.args.get("from_")
        if isinstance(from_, exp.From):
            tables.extend(_relation_tables(from_.this))
        for join in query.args.get("joins") or []:
            if isinstance(join, exp.Join):
                tables.extend(_relation_tables(join.this))
        return tables
    if isinstance(query, exp.SetOperation):
        return _query_scope_tables(query.this) + _query_scope_tables(query.expression)
    return []


def _statement_scope_tables(statement: exp.Expression) -> list[exp.Table]:
    """Every `exp.Table` in `statement`'s own FROM-scope -- what a reference
    written directly in it (not inside a nested query) can name by alias or
    by table name.

    Never crosses into a nested query's own body. A WHERE/SET's `IN`,
    `EXISTS`, `ANY`/`ALL` or scalar subquery, a MERGE branch's own source
    and a CTE's definition each bind their own aliases, which the engine
    -- and `resolve_column_owners`'s scope walk -- never lets leak to the
    statement around them. Before this, `_collect_table_aliases_with_temp`
    walked the *entire* statement with one `find_all(exp.Table)`, so a
    subquery reusing the target's alias for a different table overwrote it
    in this one flat map (found alongside R11-FP07's Oracle collection-source
    fix, 2026-09-20): `UPDATE t SET t.total = t.qty * 2 FROM dbo.totals t
    WHERE EXISTS (SELECT 1 FROM dbo.other t WHERE t.flag = 1)` resolved both
    the target and `t.qty` to `dbo.other`, a table the statement never
    writes and reads only inside its own EXISTS. Scoping this map the same
    way the resolver already scopes columns closes that."""
    if isinstance(statement, exp.Update | exp.Delete | exp.Merge):
        tables: list[exp.Table] = list(_relation_tables(statement.this))
        from_ = statement.args.get("from_")
        if isinstance(from_, exp.From):
            tables.extend(_relation_tables(from_.this))
        for join in statement.args.get("joins") or []:
            if isinstance(join, exp.Join):
                tables.extend(_relation_tables(join.this))
        using = statement.args.get("using")
        for relation in using if isinstance(using, list) else [using]:
            tables.extend(_relation_tables(relation))
        for table in statement.args.get("tables") or []:  # T-SQL `DELETE alias FROM ...`
            if isinstance(table, exp.Table):
                tables.append(table)
        return tables
    if isinstance(statement, exp.Insert):
        target = statement.this
        table = target.this if isinstance(target, exp.Schema) else target
        tables = [table] if isinstance(table, exp.Table) else []
        tables.extend(_query_scope_tables(statement.expression))
        return tables
    if isinstance(statement, exp.Create):
        # `CREATE [TEMP] TABLE t (...)`/`CREATE TABLE t AS SELECT ...`: `t` is a
        # target, like an INSERT's, and its own `AS SELECT` (if any) is this
        # statement's top-level query, like an INSERT's source.
        this = statement.this
        table = this.this if isinstance(this, exp.Schema) else this
        tables = [table] if isinstance(table, exp.Table) else []
        expression = statement.args.get("expression")
        if isinstance(expression, exp.Expression):
            tables.extend(_query_scope_tables(expression))
        return tables
    if isinstance(statement, exp.Select):
        # T-SQL `SELECT ... INTO target FROM ...`: `target` is this statement's
        # own write target, in scope for it exactly as an UPDATE's or MERGE's is.
        into = statement.args.get("into")
        if isinstance(into, exp.Into) and isinstance(into.this, exp.Table):
            return [into.this, *_query_scope_tables(statement)]
    return _query_scope_tables(statement)


def _collect_table_aliases_with_temp(
    statement: object, subject: Mapping[str, str] | None = None
) -> tuple[dict[str, str], set[str]]:
    """The statement's own alias map: each FROM/target item in its own
    FROM-scope (`_statement_scope_tables`), keyed by alias, by table name and
    by fully-qualified name, plus which of those are temp tables/variables.

    `subject` is a trigger's firing-row binding (`trigger_subject_aliases`);
    `None` -- every routine-body caller -- leaves the map as just that."""
    aliases: dict[str, str] = {}
    temp: set[str] = set()
    if not _SQLGLOT_AVAILABLE or not isinstance(statement, exp.Expression):
        return aliases, temp
    for table in _statement_scope_tables(statement):
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
    `sql_lineage_parser._extract_from_statement`, in the ways this fixes:

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
    3. (2026-09-19) The query is the INSERT's own source. It used to be the first
       UNION -- or failing that the first SELECT -- found *anywhere* in the
       statement, so `INSERT INTO t (a) SELECT x FROM p WHERE k IN (SELECT 1 UNION
       SELECT 2)` mapped the subquery's projections onto `t`. With a column list,
       every branch of a set operation is zipped (the second branch's sources were
       dropped), and each branch's WHERE is filter evidence, as it always was
       without a column list.
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
    inner_select: exp.Expression | None = statement.expression
    while isinstance(inner_select, exp.Subquery):
        inner_select = inner_select.this
    if not isinstance(inner_select, exp.Select | exp.SetOperation):
        return (
            _edges_from_values(statement, target_table, target_columns, dialect, table_aliases),
            target_table,
        )

    if not target_columns:
        return (
            _extract_edges_from_select(inner_select, target_table, dialect, table_aliases),
            target_table,
        )

    edges: list[LineageEdge] = []
    for branch in _set_operation_branches(inner_select):
        select_list_refs: set[tuple[str, str]] = set()
        for target_col, select_expr in zip(target_columns, branch.expressions, strict=False):
            source_expr = select_expr.this if isinstance(select_expr, exp.Alias) else select_expr
            if isinstance(source_expr, exp.Star) or (
                isinstance(source_expr, exp.Column) and isinstance(source_expr.this, exp.Star)
            ):
                star_alias = source_expr.table if isinstance(source_expr, exp.Column) else None
                edges.extend(
                    _extract_star_edges(
                        star_alias, target_table, dialect, table_aliases, {}, table_aliases,
                        branch,
                    )
                )
                continue
            has_agg = _has_aggregate_functions(source_expr)
            transformation = _classify_transformation(source_expr, has_agg)
            for table_ref, col_name in _extract_source_columns(source_expr):
                resolved, ok = _resolve_or_mark_unresolved(table_ref, table_aliases)
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
                branch.args.get("where"), target_table, dialect, table_aliases, select_list_refs
            )
        )
    return edges, target_table


def _set_operation_branches(query: exp.Expression) -> list[exp.Select]:
    """The SELECTs a query is made of, left to right: itself, or each branch of a
    UNION/INTERSECT/EXCEPT, however nested."""
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        return [
            *_set_operation_branches(query.this),
            *_set_operation_branches(query.expression),
        ]
    return [query] if isinstance(query, exp.Select) else []


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


#: R11-FP03: a function call inside an expression -- `SELECT pkg.fn(x) FROM t`,
#: `v := pkg.fn(x) + 1` -- is a call for descent purposes too, not just a bare
#: statement-level `CALL`/`EXEC`/`PERFORM`. See `_augmented_with_expression_call`.
def _dot_chain_names(node: exp.Expression) -> list[str] | None:
    """`node` read as a plain qualifier chain -- `s.pkg` from
    `Dot(Identifier(s), Identifier(pkg))`, which is how sqlglot hangs a qualified
    call's prefix off the call itself -- most significant part first. None when
    some part of it is not a plain name (an expression, a subquery, a function
    call of its own): nothing this parser would use as a schema/package name."""
    if isinstance(node, exp.Identifier):
        return [node.this]
    if (
        isinstance(node, exp.Column)
        and not node.args.get("table")
        and isinstance(node.this, exp.Identifier)
    ):
        return [node.this.this]
    if isinstance(node, exp.Dot):
        left = _dot_chain_names(node.this)
        right = _dot_chain_names(node.expression)
        return [*left, *right] if left is not None and right is not None else None
    return None


def _expression_call_site(call: exp.Anonymous) -> CallSite | None:
    """`call` -- an `exp.Anonymous`, sqlglot's own catch-all for a function call it
    does not recognise as one of its dialect's builtins -- read as a `CallSite`,
    qualified by however many `Dot` levels sqlglot hung it under (`pkg.fn`,
    `s.pkg.fn`), the same shape a written call has. `argument_names` reads
    Oracle's `p => x` inside a call written this way too, exactly as a bare
    `CALL`/`EXEC` statement's does; everything else is positional. None when the
    call carries no name sqlglot kept as plain text."""
    name = call.this if isinstance(call.this, str) and call.this else None
    if name is None:
        return None
    parent = call.parent
    if isinstance(parent, exp.Dot) and parent.expression is call:
        prefix = _dot_chain_names(parent.this)
        if prefix is not None:
            name = ".".join([*prefix, name])
    return CallSite(
        name,
        tuple(
            argument.this.this
            if isinstance(argument, exp.Kwarg) and isinstance(argument.this, exp.Var)
            else None
            for argument in call.expressions
        ),
    )


def _augmented_with_expression_call(
    statement: ParsedStatement, node: object, dialect: str
) -> ParsedStatement:
    """`statement`, with a NESTED_PROCEDURE_CALL gap added for the first function
    call inside `node` that sqlglot could not classify as one of its own builtins.
    `statement`'s own edges -- whatever ordinary lineage its table references gave
    it -- are kept exactly as they were: this only adds the fact that the routine
    also touches whatever the call touches, for the same gap-reading pass
    (in-package splicing, or catalog descent) that already reads a bare call
    through, resolved by the same rules (`aida.routine_call_descent.called_routine`
    reads either shape identically).

    At most one: `ParsedStatement.call_site` is a single field, the same
    assumption a bare call statement has always made, so only the first call
    sqlglot's own tree order finds is recognised; a second, in the same
    statement, is not read as a separate call. Never applied to a statement that
    is already unparsed, or already has a call site of its own (a bare
    `CALL p()`/`EXEC p` never reaches here with an unset `call_site`).

    A function sqlglot's dialect grammar does not special-case (an Oracle
    `SYS_CONTEXT`, a PostgreSQL `jsonb_build_object`) looks exactly like a real
    call here and resolves NOT_CAPTURED like any other name nothing captured --
    honest, not silent, but noisier than a hand-written exception list would be;
    every resolver in this module is name-only already and takes the same risk.
    """
    if statement.call_site is not None or statement.is_unparsed or not isinstance(
        node, exp.Expression
    ):
        return statement
    # A table-valued function reference (`FROM s.fn(x) n`) parses as
    # `Table(this=Anonymous(...))` -- sqlglot's shape for a function-as-table, which
    # `_table_function_markers` already reads as its own TABLE_FUNCTION_READ gap.
    # Skip it here so the two mechanisms never race over the same call.
    call = next(
        (
            found
            for found in node.find_all(exp.Anonymous)
            if not isinstance(found.parent, exp.Table)
        ),
        None,
    )
    site = _expression_call_site(call) if call is not None else None
    if site is None:
        return statement
    marker = _unparsed_statement(
        statement.ordinal, dialect, statement.control_flow_context,
        f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: {site.callee}",
    )
    return replace(
        statement,
        is_unparsed=True,
        is_no_lineage=False,
        unparsed_reason=marker.unparsed_reason,
        call_site=site,
        edges=(*statement.edges, marker.edges[0]),
    )


def _local_statement(
    ordinal: int,
    node: exp.Expr,
    dialect: str,
    context: str | None,
    subject: Mapping[str, str] | None = None,
    names: DeclaredNames = NO_DECLARED_NAMES,
    target: str = PROCEDURE_LOCAL_TARGET,
) -> ParsedStatement:
    """A query whose rows stay inside the routine. Its reads are real
    dependencies, so its edges are kept -- into `PROCEDURE_LOCAL_TARGET`, marked
    intermediate: never a table, never a write, never the routine's result.

    `target` names other routine-local state (a loop record's intermediate) -- or,
    for the one query whose rows leave through a ref cursor the routine hands its
    caller (2026-09-19), `PROCEDURE_RESULT_TARGET`, which is not intermediate."""
    intermediate = target != PROCEDURE_RESULT_TARGET
    if not isinstance(node, exp.Expression) or node.find(exp.Table) is None:
        return _augmented_with_expression_call(
            ParsedStatement(
                ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=True,
                unparsed_reason=None, control_flow_context=context,
                target_table=None, is_intermediate_target=False, node=node, edges=(),
            ),
            node,
            dialect,
        )
    _resolve_owners(node, subject, names)
    edges = _extract_edges_from_select(
        node, target, dialect, _collect_table_aliases_with_temp(node, subject)[0]
    )
    return _augmented_with_expression_call(
        ParsedStatement(
            ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=False,
            unparsed_reason=None, control_flow_context=context,
            target_table=target, is_intermediate_target=intermediate, node=node,
            edges=tuple(_wrap(edges, ordinal, False, intermediate, context)),
        ),
        node,
        dialect,
    )


def _parse_local_query(
    ordinal: int,
    sql: str,
    dialect: str,
    sqlglot_dialect: str,
    context: str | None,
    subject: Mapping[str, str] | None = None,
    names: DeclaredNames = NO_DECLARED_NAMES,
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
    # R11-FP07: `sql` is `SELECT ` plus a suffix of the statement, so its tail is
    # the statement's tail -- what `procedure_token_ranges` aligns on.
    remember_parsed_text(node, sql)
    return _local_statement(ordinal, node, dialect, context, subject, names)


def _classify_plpgsql_statement(
    ordinal: int,
    remainder: str,
    dialect: str,
    sqlglot_dialect: str,
    context: str | None,
    subject: Mapping[str, str] | None = None,
    names: DeclaredNames = NO_DECLARED_NAMES,
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
            ordinal, f"SELECT {expression}", dialect, sqlglot_dialect, context, subject, names
        ), ""
    if match := _PLPGSQL_ASSIGNMENT_RE.match(remainder):
        return _parse_local_query(
            ordinal, f"SELECT {match.group('expr')}", dialect, sqlglot_dialect, context,
            subject, names,
        ), ""
    # R11-FP07: both rewrites blank what they remove rather than cutting it, so
    # every character after it keeps its offset (`procedure_token_ranges`).
    remainder = _PLPGSQL_INTO_STRICT_RE.sub(
        lambda match: "INTO" + " " * (len(match.group(0)) - len("INTO")), remainder
    )
    return None, _PLPGSQL_RETURNING_INTO_RE.sub(
        lambda match: match.group("returning") + " " * (match.end() - match.end("returning")),
        remainder,
    )


# ---------------------------------------------------------------------------
# Step 4: the per-chunk dispatcher.
# ---------------------------------------------------------------------------


#: (2026-09-19) The `control_flow_context` of a cursor FOR loop's read -- its own query, or
#: the declared cursor it walks -- and of an IF/ELSIF/WHILE condition's.
CURSOR_FOR_LOOP_CONTEXT: Final[str] = "CURSOR_FOR_LOOP"
CONDITION_CONTEXT: Final[str] = "CONDITION"

#: One open LOOP on the walk's stack: the record it binds and that record's
#: intermediate, or None for a loop that fetches no rows (or none this parse can read).
_OpenLoop = tuple[str, str] | None


def _bound(context: _WalkContext, loops: list[_OpenLoop]) -> _ScopedNames:
    """The routine's declared names, with every record bound around the statement: the records
    a FETCH filled, then each open loop's -- the innermost binding of a name wins, so a loop
    variable of the same name shadows a fetched record inside its loop."""
    return replace(
        _scoped(context.names),
        records=(
            *context.fetched.items(),
            *(binding for binding in loops if binding is not None),
        ),
    )


def _classify_chunk(
    ordinal: int,
    raw_chunk: str,
    chunk_offset: int,
    context: _WalkContext,
    loops: list[_OpenLoop],
) -> list[ParsedStatement]:
    """Peel one chunk, classify what is left, and locate every statement it gave.

    `chunk_offset` is where `raw_chunk` starts in the text `context.locator` was
    built over. What the peeled headers read comes first, each located at its own
    offset inside the chunk: a query-bearing condition, then each cursor FOR loop's
    rows. `loops` is the walk's stack of open LOOPs, across chunks: an `END LOOP`
    pops one and a LOOP header pushes one, so the statements after a cursor FOR
    loop's header -- in this chunk and the ones that follow, until its END LOOP --
    read its record, and nothing outside the loop does.
    """
    peeled = _peel_control_flow_prefix(raw_chunk)
    for _closed in range(peeled.loops_closed):
        if loops:
            loops.pop()
    results: list[ParsedStatement] = []
    for condition in peeled.conditions:
        results.append(
            _condition_read(ordinal, condition, chunk_offset, context, _bound(context, loops))
        )
    for position, header in enumerate(peeled.loops_opened):
        if header is None:
            loops.append(None)
            continue
        # Several targets (`FOR a, b IN SELECT ...`) are positional variables, which are
        # not modelled: the rows are read into plain local state and nothing reads them back.
        target = (
            PROCEDURE_LOCAL_TARGET
            if header.record is None
            # Two loops opened in one chunk share its ordinal; the second is told apart by
            # its place among them.
            else loop_record_target(header.record, ordinal, position or None)
        )
        read = _loop_read(ordinal, header, target, chunk_offset, context, _bound(context, loops))
        if read is not None:
            results.append(read)
        # Only a loop whose rows were read binds its record: a `rec.col` of rows this
        # parse cannot see (`FOR r IN EXECUTE ...`, a cursor it has not read) stays the
        # record's field, never an intermediate nothing fills.
        fills = read is not None and any(
            edge.target_table == target and edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
            for edge in read.edges
        )
        loops.append(
            (header.record.lower(), target) if fills and header.record is not None else None
        )
    start = chunk_offset + peeled.remainder_offset
    where = context.locator.span(start, start + len(peeled.remainder))
    if (fetch := _fetch_read(ordinal, peeled, context)) is not None:
        results.append(_located(fetch, where, context.digest))
        return results
    results.extend(
        _located(statement, where, context.digest)
        for statement in _classify_and_extract(
            ordinal,
            peeled,
            context.dialect,
            context.sqlglot_dialect,
            context.plpgsql,
            context.subject,
            _bound(context, loops),
        )
    )
    return results


def _trimmed(text: str, offset: int) -> tuple[str, int]:
    """`text` without leading trivia or trailing whitespace, and where it now starts --
    the span a statement read out of a header is located at."""
    first = _skip_trivia(text, 0, len(text))
    return text[first:].rstrip(), offset + first


def _condition_read(
    ordinal: int,
    condition: _Condition,
    chunk_offset: int,
    context: _WalkContext,
    names: DeclaredNames,
) -> ParsedStatement:
    """An IF/ELSIF/WHILE condition that holds a query -- `IF EXISTS (SELECT 1 FROM t)`,
    `WHILE (SELECT count(*) FROM q) > 0` -- read as what it is: tables read into the
    routine-local value that steers the branch. Read as `SELECT <condition>`, the rewrite
    `v := (<query>)` already gets, so its tail stays where the text has it."""
    text, offset = _trimmed(condition.text, chunk_offset + condition.offset)
    statement = _parse_local_query(
        ordinal, f"SELECT {text}", context.dialect, context.sqlglot_dialect,
        condition.context or CONDITION_CONTEXT, context.subject, names,
    )
    return _located(statement, context.locator.span(offset, offset + len(text)), context.digest)


def _loop_read(
    ordinal: int,
    header: _LoopHeader,
    target: str,
    chunk_offset: int,
    context: _WalkContext,
    names: DeclaredNames,
) -> ParsedStatement | None:
    """What a cursor FOR loop fetches, read into `target` -- its record's intermediate.

    Its own query (`FOR r IN (SELECT ...)`, `FOR r IN SELECT ...`) is parsed and located
    where it is written. `IN EXECUTE <string>` is dynamic SQL. A loop over a declared
    cursor (`FOR r IN c LOOP`) fetches that cursor's rows, which its declaration's read
    already names column by column: they are re-stated into the record at the loop --
    where the cursor is opened and its query runs -- with no token, since the tokens are
    in the declaration. A cursor this parse did not read (declared in another package)
    reads as None: no fact, and no record bound."""
    if header.query is not None and header.query_offset is not None:
        query, offset = _trimmed(header.query, chunk_offset + header.query_offset)
        where = context.locator.span(offset, offset + len(query))
        if _PLPGSQL_EXECUTE_RE.match(query):
            statement = _unparsed_statement(
                ordinal,
                context.dialect,
                CURSOR_FOR_LOOP_CONTEXT,
                f"{UnparsedReason.DYNAMIC_SQL.value}: "
                "PL/pgSQL EXECUTE runs a string built at runtime",
            )
        elif not _QUERY_START_RE.match(query):
            # `FOR i IN (lo)..(hi) LOOP`: a numeric range in parentheses, which reads nothing.
            return None
        else:
            statement = _query_into_local(
                ordinal, query, context.dialect, context.sqlglot_dialect,
                CURSOR_FOR_LOOP_CONTEXT, context.subject, names,
                what="a cursor FOR loop whose query does not parse as one", target=target,
            )
        return _located(statement, where, context.digest)
    declared = context.cursors.get((header.cursor or "").lower())
    if not declared:
        return None
    statement = _cursor_rows_into(ordinal, declared, target, CURSOR_FOR_LOOP_CONTEXT)
    start = chunk_offset + header.header_start
    return _located(
        statement, context.locator.span(start, chunk_offset + header.header_end), context.digest
    )


def _cursor_rows_into(
    ordinal: int,
    declared: tuple[ProcedureLineageEdgeRecord, ...],
    target: str,
    control_flow_context: str,
) -> ParsedStatement:
    """A declared cursor's read, re-stated into `target` at the statement that fetches it.

    The declaration already named the query's sources column by column; the statement that
    fetches the rows -- a cursor FOR loop, a `FETCH c INTO r` -- is where they land in a
    record. Located by the caller at that statement, with no token: the query's tokens are in
    the declaration, which keeps its own read."""
    edges = tuple(
        replace(
            edge,
            target_table=target,
            statement_ordinal=ordinal,
            control_flow_context=control_flow_context,
            is_write=False,
            is_intermediate=True,
            statement_range=None,
            statement_range_status=StatementRangeStatus.NOT_LOCATED.value,
            statement_text_digest=None,
            source_token_range=None,
            target_token_range=None,
        )
        for edge in declared
    )
    return ParsedStatement(
        ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=False,
        unparsed_reason=None, control_flow_context=control_flow_context,
        target_table=target, is_intermediate_target=True, node=None, edges=edges,
    )


def _fetch_read(
    ordinal: int, peeled: _PeelResult, context: _WalkContext
) -> ParsedStatement | None:
    """`FETCH c INTO r`: the rows of the declared cursor `c`, fetched into the record `r`
    (2026-09-19). They are read into an intermediate of the record's own, `<LOCAL:r@N>`, and
    `r` is bound to it from here to the end of the unit -- so `r.col` in what follows reads
    that intermediate and the hop pass joins the cursor's sources to what the routine writes,
    as it does for a cursor FOR loop's record.

    Only where it is sound. The cursor is one this parse read (declared in scope), the target
    is one name, and the record is fetched from one cursor in the unit: two cursors' rows in
    one record have no one intermediate to stand for them. The same cursor fetched again --
    the priming read of `FETCH c INTO r; WHILE c%FOUND LOOP ... FETCH c INTO r;` -- fills the
    same intermediate. Anything else stays what it was: lineage-free, the record's fields
    reading nothing."""
    if not (context.dialect == "oracle" or context.plpgsql):
        return None
    fetched = _FETCH_INTO_RE.match(peeled.remainder)
    if fetched is None:
        return None
    record = fetched.group("target").lower()
    declared = context.cursors.get(_bare(fetched.group("cursor")))
    if not declared or record in context.fetch_conflicts:
        return None
    target = context.fetched.setdefault(record, loop_record_target(record, ordinal))
    return _cursor_rows_into(ordinal, declared, target, CURSOR_FETCH_CONTEXT)


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
    names: DeclaredNames = NO_DECLARED_NAMES,
) -> list[ParsedStatement]:
    """The dispatcher: what one peeled statement is, and the lineage it carries.

    Takes the peel rather than the raw chunk since R11-FP07, because
    `_classify_chunk` needs the peel's offsets to locate what this returns; the
    cursor loop's query is classified there too. The `isinstance` branches below
    are what `procedure_capability_matrix` introspects, so they stay here.
    `names` is what the routine declares, which decides what an unqualified name
    in a statement is (`aida.procedure_column_owners`) and whether `v.DELETE(1)` is
    a collection method.
    """
    results: list[ParsedStatement] = []

    remainder = peeled.remainder
    if not remainder:
        # Purely structural (BEGIN/END/ELSE/...) -- genuinely no lineage.
        return results

    if dialect == "oracle" and _PLSQL_CURSOR_DECLARATION_RE.match(remainder):
        # R11-FP03: ahead of the keyword check, which reads a leading DECLARE as
        # lineage-free and would drop the cursor's query with it.
        results.append(
            _cursor_declaration(ordinal, remainder, dialect, sqlglot_dialect, subject, names)
        )
        return results

    # 2026-09-19: three cursor shapes and PL/pgSQL declarations, each ahead of the
    # keyword check for the same reason -- OPEN, DECLARE and SET lead them.
    if (dialect == "oracle" or plpgsql) and (opened := _OPEN_FOR_RE.match(remainder)):
        results.append(
            _open_for(
                ordinal, remainder[opened.end() :], dialect, sqlglot_dialect, subject, names,
                cursor=opened.group("cursor"),
            )
        )
        return results
    if dialect == "tsql" and (declared := _TSQL_CURSOR_DECLARATION_RE.match(remainder)):
        results.append(
            _tsql_cursor(
                ordinal, remainder[declared.end() :], dialect, sqlglot_dialect, subject, names
            )
        )
        return results
    if dialect == "tsql" and (
        assigned := _TSQL_SET_ASSIGNMENT_RE.match(remainder)
    ) and _has_query_word(assigned.group("expr")):
        # (2026-09-19) `SET @v = (SELECT ...)` reads the tables its subquery names, into the
        # variable -- routine-local state, as `v := (<query>)` does in PL/pgSQL. The cursor form
        # above is checked first; a SET whose expression holds no query stays lineage-free.
        results.append(
            _parse_local_query(
                ordinal, f"SELECT {assigned.group('expr')}", dialect, sqlglot_dialect,
                peeled.control_flow_context, subject, names,
            )
        )
        return results
    if plpgsql and (declaration := _plpgsql_declaration_in_body(remainder)) is not None:
        statement = _plpgsql_declaration(
            ordinal, declaration, dialect, sqlglot_dialect, subject, names
        )
        results.append(statement or _lineage_free(ordinal, peeled.control_flow_context))
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
        # Blanked, not cut: R11-FP07's token ranges need every later offset kept.
        remainder = _PG_TEMP_ON_COMMIT_RE.sub(
            lambda match: match.group("head") + " " * (match.end() - match.end("head")),
            remainder,
            count=1,
        )
    if plpgsql:
        plpgsql_statement, remainder = _classify_plpgsql_statement(
            ordinal, remainder, dialect, sqlglot_dialect, peeled.control_flow_context, subject,
            names,
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

    if dialect == "oracle" and peeled.follows_end and _PLSQL_END_LABEL_RE.match(remainder):
        # R11-FP03: `END refresh;`, `END LOOP outer;` -- the END's label, which reads
        # nothing, and is exactly what the call statement `refresh;` looks like.
        results.append(_lineage_free(ordinal, peeled.control_flow_context))
        return results

    if dialect == "oracle" and (call := _plsql_call(remainder)) is not None:
        results.append(
            _plsql_call_statement(ordinal, call, dialect, peeled.control_flow_context, names)
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
    remember_parsed_text(node, remainder)  # R11-FP07: what its positions index

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

    _resolve_owners(node, subject, names)
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
            _local_statement(ordinal, node, dialect, peeled.control_flow_context, subject, names)
        )
        return results

    if isinstance(node, exp.Select | exp.Union):
        edges, target = _extract_edges_from_select_into(node, dialect, table_aliases)
        is_write = target != PROCEDURE_RESULT_TARGET
        temp = target in _collect_table_aliases_with_temp(node)[1] if is_write else False
        results.append(
            _augmented_with_expression_call(
                ParsedStatement(
                    ordinal=ordinal, is_write=is_write, is_unparsed=False, is_no_lineage=False,
                    unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                    target_table=target, is_intermediate_target=temp, node=node,
                    edges=tuple(
                        _wrap(edges, ordinal, is_write, temp, peeled.control_flow_context)
                    ),
                ),
                node,
                dialect,
            )
        )
        return results

    if isinstance(node, exp.Insert):
        edges, target = _extract_edges_from_insert(node, dialect, subject)
        temp = target in _collect_table_aliases_with_temp(node)[1]
        results.append(
            _augmented_with_expression_call(
                ParsedStatement(
                    ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                    unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                    target_table=target or None, is_intermediate_target=temp, node=node,
                    edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
                ),
                node,
                dialect,
            )
        )
        return results

    if isinstance(node, exp.Update):
        edges, target = _extract_edges_from_update(node, dialect, subject)
        temp = target in _collect_table_aliases_with_temp(node)[1]
        results.append(
            _augmented_with_expression_call(
                ParsedStatement(
                    ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                    unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                    target_table=target or None, is_intermediate_target=temp, node=node,
                    edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
                ),
                node,
                dialect,
            )
        )
        return results

    if isinstance(node, exp.Delete):
        aliases, temp_set = _collect_table_aliases_with_temp(node, subject)
        target_expr = _delete_target(node, dialect)
        target = _resolve_table_name(target_expr) if isinstance(target_expr, exp.Table) else ""
        target = aliases.get(target, target)
        temp = target in temp_set
        edges = _where_filter_edges(
            node.args.get("where"), target or "<UNKNOWN_TARGET>", dialect, aliases, set()
        )
        results.append(
            _augmented_with_expression_call(
                ParsedStatement(
                    ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                    unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                    target_table=target or None, is_intermediate_target=temp, node=node,
                    edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
                ),
                node,
                dialect,
            )
        )
        return results

    if isinstance(node, exp.Merge):
        edges, target = _extract_edges_from_merge(node, dialect, subject)
        temp = target in _collect_table_aliases_with_temp(node)[1]
        results.append(
            _augmented_with_expression_call(
                ParsedStatement(
                    ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                    unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                    target_table=target or None, is_intermediate_target=temp, node=node,
                    edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
                ),
                node,
                dialect,
            )
        )
        return results

    if isinstance(node, exp.Create):
        target = _extract_target_table(node)
        temp = target in _collect_table_aliases_with_temp(node)[1] or _creates_temporary_table(node)
        # [] for a plain CREATE TABLE, non-empty for CREATE TABLE ... AS SELECT.
        edges = _extract_from_statement(node, dialect)
        results.append(
            _augmented_with_expression_call(
                ParsedStatement(
                    ordinal=ordinal, is_write=True, is_unparsed=False, is_no_lineage=False,
                    unparsed_reason=None, control_flow_context=peeled.control_flow_context,
                    target_table=target or None, is_intermediate_target=temp, node=node,
                    edges=tuple(_wrap(edges, ordinal, True, temp, peeled.control_flow_context)),
                ),
                node,
                dialect,
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
# Step 4b (R11-FP03): PL/SQL call statements and cursor declarations.
# ---------------------------------------------------------------------------

#: The `control_flow_context` of a cursor declaration's read.
CURSOR_DECLARATION_CONTEXT: Final[str] = "CURSOR_DECLARATION"
#: ... and of any other declaration's: a PL/pgSQL variable whose default is a query
#: (2026-09-19), or a declaration this reader could not recognise.
DECLARATION_CONTEXT: Final[str] = "DECLARATION"


def _lineage_free(ordinal: int, context: str | None) -> ParsedStatement:
    return ParsedStatement(
        ordinal=ordinal, is_write=False, is_unparsed=False, is_no_lineage=True,
        unparsed_reason=None, control_flow_context=context,
        target_table=None, is_intermediate_target=False, node=None, edges=(),
    )


def _plsql_call(text: str) -> CallSite | None:
    """`text` as a PL/SQL call statement, or None when it is not one."""
    match = _PLSQL_CALL_STATEMENT_RE.match(text)
    if match is None:
        return None
    callee = re.sub(r"\s+", "", match.group("callee"))
    if callee.split(".", 1)[0].upper() in _PLSQL_STATEMENT_WORDS:
        return None
    arguments = match.group("arguments")
    if arguments is None:
        return CallSite(callee, ())
    inner = _parenthesised(arguments)
    if inner is None:
        return None
    return CallSite(
        callee,
        tuple(_argument_name(piece) for piece in _top_level_pieces(inner) if piece.strip()),
    )


def _parenthesised(text: str) -> str | None:
    """What is inside `text` when all of it is one balanced parenthesised group --
    quote- and comment-aware -- or None (`p(a)(b)`, or a group that never closes)."""
    depth = 0
    for start, _end, kind in _scan_tokens(text):
        if kind != "other":
            continue
        depth += 1 if text[start] == "(" else -1
        if depth == 0:
            return text[1:start] if start == len(text) - 1 else None
    return None


def _argument_name(piece: str) -> str | None:
    """The parameter one argument is passed to by name (`p_id => x`), or None when it
    is passed by position. The value is never looked at."""
    match = re.match(r"\s*([A-Za-z][\w$#]*)\s*=>", piece)
    return match.group(1) if match else None


def _plsql_call_statement(
    ordinal: int,
    call: CallSite,
    dialect: str,
    context: str | None,
    names: DeclaredNames = NO_DECLARED_NAMES,
) -> ParsedStatement:
    """A PL/SQL call statement: a nested call naming its callee, as `CALL p()` is --
    unless the callee is one Oracle supplies that moves no table data, a collection
    method, or Oracle's dynamic-SQL package.

    A collection method is qualified by a collection -- a variable the routine, its
    package or an enclosing unit declares (`v.EXTEND`, `r.items.DELETE(1)`). Any other
    qualifier names a package, and `cleanup.delete(5)` is a call to its procedure
    called DELETE; R11-FP03 read every `x.DELETE` as the method, a clean statement
    where the call can write anything its callee writes (2026-09-19)."""
    parts = call.callee.split("@", 1)[0].upper().split(".")
    if len(parts) > 1 and parts[0] == "SYS":
        parts = parts[1:]
    if (
        (len(parts) == 1 and parts[0] in _PLSQL_LINEAGE_FREE_ROUTINES)
        or (len(parts) == 2 and parts[0] in _PLSQL_LINEAGE_FREE_PACKAGES)
        or (len(parts) > 1 and parts[-1] in _PLSQL_COLLECTION_METHODS and parts[0] in names)
    ):
        return _lineage_free(ordinal, context)
    if len(parts) == 2 and parts[0] in _PLSQL_DYNAMIC_SQL_PACKAGES:
        return _unparsed_statement(
            ordinal, dialect, context,
            f"{UnparsedReason.DYNAMIC_SQL.value}: {parts[0]} runs a string built at runtime",
        )
    marker = _unparsed_statement(
        ordinal, dialect, context,
        f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: {call.callee}",
    )
    return replace(marker, call_site=call)


def _cursor_query(text: str) -> str | None:
    """The query a cursor declaration's IS introduces -- empty when nothing follows
    it -- or None for a cursor spec, which has no IS."""
    depth = 0
    for start, end, kind in _scan_tokens(text):
        if kind == "other":
            depth += 1 if text[start] == "(" else -1
        elif kind == "word" and depth == 0 and text[start:end].upper() == "IS":
            return text[end:].strip()
    return None


def _cursor_declaration(
    ordinal: int,
    text: str,
    dialect: str,
    sqlglot_dialect: str,
    subject: Mapping[str, str] | None = None,
    names: DeclaredNames = NO_DECLARED_NAMES,
) -> ParsedStatement:
    """A cursor declaration's query, read as what it is: the rows the cursor fetches
    into routine-local state. Its edges go to `PROCEDURE_LOCAL_TARGET`, as `SELECT
    ... INTO v` does -- never a table write, never the routine's result. A cursor spec
    names a query declared elsewhere in the same text, which is read there.
    """
    context = CURSOR_DECLARATION_CONTEXT
    query = _cursor_query(text)
    if query is None:
        return _lineage_free(ordinal, context)
    return _query_into_local(
        ordinal, query, dialect, sqlglot_dialect, context, subject, names,
        what="a cursor declaration whose IS introduces no query",
    )


def _query_into_local(
    ordinal: int,
    query: str,
    dialect: str,
    sqlglot_dialect: str,
    context: str,
    subject: Mapping[str, str] | None,
    names: DeclaredNames,
    *,
    what: str,
    target: str = PROCEDURE_LOCAL_TARGET,
) -> ParsedStatement:
    """`query` -- the tail of a cursor's declaration or OPEN, or a cursor FOR loop's own
    query -- read into routine-local state (`target`), or the gap that says why it could
    not be. `what` names the shape for a query that parses as something else; it is
    fixed text, never the body's."""
    if not _SQLGLOT_AVAILABLE:
        return _unparsed_statement(
            ordinal, dialect, context,
            f"{UnparsedReason.PARSE_ERROR.value}: sqlglot library is not available",
        )
    try:
        node = sqlglot.parse_one(query, dialect=sqlglot_dialect, error_level=ErrorLevel.RAISE)
    except Exception as exc:  # sqlglot raises a broad ParseError/TokenError family
        return _unparsed_statement(
            ordinal, dialect, context, f"{UnparsedReason.PARSE_ERROR.value}: {exc!s}"[:300]
        )
    if not isinstance(node, exp.Select | exp.Union):
        return _unparsed_statement(
            ordinal, dialect, context,
            f"{UnparsedReason.UNSUPPORTED_STATEMENT_SHAPE.value}: {what}",
        )
    # R11-FP07: the query is the statement's tail, which is what token ranges align on.
    remember_parsed_text(node, query)
    return _local_statement(ordinal, node, dialect, context, subject, names, target)


#: The `control_flow_context` of `OPEN c FOR <query>`'s read (2026-09-19).
CURSOR_OPEN_CONTEXT: Final[str] = "CURSOR_OPEN"


def _open_for(
    ordinal: int,
    tail: str,
    dialect: str,
    sqlglot_dialect: str,
    subject: Mapping[str, str] | None,
    names: DeclaredNames,
    *,
    cursor: str = "",
) -> ParsedStatement:
    """`OPEN c FOR <tail>`: a query read into the cursor, or dynamic SQL.

    The rows go to `PROCEDURE_LOCAL_TARGET`, as a declared cursor's do -- unless `c` is
    a ref cursor the routine hands its caller (`_ScopedNames.result_cursors`: an OUT
    `SYS_REFCURSOR` or `refcursor` parameter, or the cursor a function returning one
    RETURNs). Then the query is the routine's result set, exactly as PL/pgSQL's `RETURN
    QUERY` is, and `PROCEDURE_RESULT_TARGET` says so (2026-09-19). A cursor this parse
    cannot show is handed back stays local: the read is recorded either way, and only
    the claim about where its rows go waits for the text to prove it."""
    if not _QUERY_START_RE.match(tail):
        return _unparsed_statement(
            ordinal, dialect, CURSOR_OPEN_CONTEXT,
            f"{UnparsedReason.DYNAMIC_SQL.value}: OPEN ... FOR a string built at runtime",
        )
    results = names.result_cursors if isinstance(names, _ScopedNames) else frozenset()
    return _query_into_local(
        ordinal, tail, dialect, sqlglot_dialect, CURSOR_OPEN_CONTEXT, subject, names,
        what="OPEN ... FOR whose query does not parse as one",
        target=(
            PROCEDURE_RESULT_TARGET if cursor.lower() in results else PROCEDURE_LOCAL_TARGET
        ),
    )


def _tsql_cursor(
    ordinal: int,
    tail: str,
    dialect: str,
    sqlglot_dialect: str,
    subject: Mapping[str, str] | None,
    names: DeclaredNames,
) -> ParsedStatement:
    """T-SQL `DECLARE c CURSOR ... FOR <tail>` / `SET @c = CURSOR ... FOR <tail>`. A
    trailing `FOR READ ONLY` / `FOR UPDATE [OF ...]` is blanked, not cut, so the query's
    tail stays where the stored text has it (R11-FP07's token alignment)."""
    if match := _TSQL_CURSOR_TAIL_RE.search(tail):
        tail = tail[: match.start()] + " " * (len(tail) - match.start())
    return _query_into_local(
        ordinal, tail, dialect, sqlglot_dialect, CURSOR_DECLARATION_CONTEXT, subject, names,
        what="a cursor declaration whose FOR introduces no query",
    )


def _plpgsql_declaration_in_body(text: str) -> str | None:
    """`text` without a leading DECLARE, when it is a PL/pgSQL declaration reached among
    a body's statements -- a nested block's section -- else None.

    A chunk led by DECLARE is its block's first declaration, whatever follows. After it,
    only shapes no statement has are read as declarations: `name CURSOR ...`, and `name
    type ... := <expr>`, a name that is not a statement's first word followed by more
    than a name before the operator (an assignment is `name :=`, read before this)."""
    if match := _DECLARE_PREFIX_RE.match(text):
        return text[match.end() :]
    if _PLPGSQL_CURSOR_DECLARATION_RE.match(text):
        return text
    item = _PLPGSQL_ITEM_DECLARATION_RE.match(text)
    if item is None or item.group("name").upper() in _PLPGSQL_STATEMENT_WORDS:
        return None
    return text if _declaration_default(text) is not None else None


def _declaration_default(text: str) -> str | None:
    """The default expression of a PL/pgSQL item declaration -- everything after its
    first `:=`, `=` or DEFAULT outside literals and comments -- or None when it has
    none. A suffix of `text`, so a query in it keeps the declaration's tail."""
    code = re.sub(
        r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/",
        lambda match: " " * len(match.group(0)),
        text,
        flags=re.DOTALL,
    )
    operator = re.search(r":=|(?<![<>!:=])=(?![=>])|\bDEFAULT\b", code, re.IGNORECASE)
    return None if operator is None else text[operator.end() :]


def _has_query_word(text: str) -> bool:
    return any(
        kind == "word" and text[start:end].upper() == "SELECT"
        for start, end, kind in _scan_tokens(text)
    )


def _plpgsql_declaration(
    ordinal: int,
    text: str,
    dialect: str,
    sqlglot_dialect: str,
    subject: Mapping[str, str] | None,
    names: DeclaredNames,
) -> ParsedStatement | None:
    """One PL/pgSQL declaration, without its section's DECLARE: the statement its read
    makes, or None when it reads nothing -- which then produces no statement, as an
    Oracle item declaration does (R11-FP03).

    * `c [[NO] SCROLL] CURSOR [(args)] {FOR | IS} <query>`: the query, read into
      routine-local state, as PL/SQL's cursor declaration is.
    * `v type := (<query>)`: the default runs when the block is entered, so its query
      is a read into the variable -- `v := (<query>)` in a declaration's clothes.
    * Anything else PL/pgSQL admits -- a type, a `%TYPE`/`%ROWTYPE` anchor (a table's
      structure, not its rows), `ALIAS FOR $n`, a default with no query -- reads
      nothing. A declaration that matches none of these is a gap (the caller's).
    """
    if not text.strip():
        return None
    if _PLPGSQL_CURSOR_DECLARATION_RE.match(text):
        query = _cursor_for_query(text)
        if query is None:
            return _unparsed_statement(
                ordinal, dialect, CURSOR_DECLARATION_CONTEXT,
                f"{UnparsedReason.UNSUPPORTED_STATEMENT_SHAPE.value}: "
                "a cursor declaration with no FOR or IS query",
            )
        return _query_into_local(
            ordinal, query, dialect, sqlglot_dialect, CURSOR_DECLARATION_CONTEXT, subject,
            names, what="a cursor declaration whose FOR introduces no query",
        )
    default = _declaration_default(text)
    if default is None or not _has_query_word(default):
        return None
    return _parse_local_query(
        ordinal, f"SELECT {default}", dialect, sqlglot_dialect, DECLARATION_CONTEXT,
        subject, names,
    )


def _cursor_for_query(text: str) -> str | None:
    """The query after a PL/pgSQL cursor declaration's FOR (or IS), outside its
    argument list's parentheses."""
    depth = 0
    for start, end, kind in _scan_tokens(text):
        if kind == "other":
            depth += 1 if text[start] == "(" else -1
        elif kind == "word" and depth == 0 and text[start:end].upper() in ("FOR", "IS"):
            return text[end:].strip()
    return None


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
                        # Its target is named in `e`'s statement; its source is
                        # read in another one, so no source token is carried.
                        target_token_range=e.target_token_range,
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


def _tokens_located(statement: ParsedStatement, context: _WalkContext) -> ParsedStatement:
    """R11-FP07 token grain: each edge's source and target token inside its
    statement, where exactly one can be proved (`aida.procedure_token_ranges`).

    Each column reference carries the table its scope resolved it to
    (`aida.procedure_column_owners`), so an unqualified reference counts as reading
    exactly the table the parse gave it -- and two references to one fact, one
    qualified and one not, compete for it rather than each claiming its own token.
    Only a located statement is narrowed: a token range refines a statement range
    and never stands without one, and it is searched for inside exactly that span."""
    where = statement.statement_range
    if where is None or statement.node is None or not statement.edges:
        return statement
    tokens = locate_edge_tokens(
        statement.node,
        statement.edges,
        text=context.locator.text,
        start=where.start_offset,
        end=where.end_offset,
        aliases=_collect_table_aliases_with_temp(statement.node, context.subject)[0],
    )
    return replace(
        statement,
        edges=tuple(
            replace(edge, source_token_range=source, target_token_range=target)
            for edge, (source, target) in zip(statement.edges, tokens, strict=True)
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
    #: What the routine declares, in scope for the statements walked with this context.
    names: DeclaredNames = NO_DECLARED_NAMES
    #: (2026-09-19) Each cursor declared in scope, by lower-cased name, with the edges its
    #: declaration read: what `FOR rec IN c LOOP` fetches into `rec`. A unit walks with a
    #: copy, so one member's cursors are not another's.
    cursors: dict[str, tuple[ProcedureLineageEdgeRecord, ...]] = field(default_factory=dict)
    #: (2026-09-19) Each record a `FETCH c INTO r` filled, by lower-cased name, with the
    #: intermediate its rows are read into: bound from the FETCH to the end of the unit. A unit
    #: walks with a dict of its own, as it does for `cursors`.
    fetched: dict[str, str] = field(default_factory=dict)
    #: A record fetched from more than one cursor in the span being walked, which no single
    #: intermediate can stand for: left unbound, as it was.
    fetch_conflicts: frozenset[str] = frozenset()


def _walk_span(
    sql: str, start: int, end: int, ordinal: int, context: _WalkContext
) -> tuple[list[ParsedStatement], int]:
    """Split, peel, classify and locate every statement of `sql[start:end]`.

    Returns the statements and the next free ordinal. The ordinal is advanced
    once per statement produced, and a chunk's statements are all numbered from
    the ordinal the chunk started at -- exactly the numbering the walk has
    always used, so no stored natural key moves. The LOOPs open at each point
    are tracked across the span's chunks (`_classify_chunk`)."""
    body = sql[start:end]
    statements: list[ParsedStatement] = []
    loops: list[_OpenLoop] = []
    spans = _split_top_level_statement_spans(body)
    context = replace(context, fetch_conflicts=_fetch_conflicts(body, spans))
    for chunk_start, chunk_end in spans:
        chunk = body[chunk_start:chunk_end]
        for parsed in _classify_chunk(ordinal, chunk, start + chunk_start, context, loops):
            emitted = len(statements)
            ordinal = _emit(statements, parsed, ordinal, context)
            _remember_cursor(context, chunk, statements[emitted])
    return statements, ordinal


def _fetch_conflicts(body: str, spans: list[tuple[int, int]]) -> frozenset[str]:
    """The records `body` fetches from more than one cursor (`_fetch_read` leaves them alone)."""
    cursors: dict[str, set[str]] = {}
    for first, last in spans:
        chunk = body[first:last]
        if "fetch" not in chunk.lower():
            continue
        if match := _FETCH_INTO_RE.match(_peel_control_flow_prefix(chunk).remainder):
            cursors.setdefault(match.group("target").lower(), set()).add(
                _bare(match.group("cursor"))
            )
    return frozenset(name for name, found in cursors.items() if len(found) > 1)


def _remember_cursor(context: _WalkContext, text: str, statement: ParsedStatement) -> None:
    """Keep a cursor declaration's read for the loops that walk the cursor (2026-09-19)."""
    if statement.control_flow_context != CURSOR_DECLARATION_CONTEXT or statement.is_unparsed:
        return
    declared = _CURSOR_NAMES_RE.match(_LABEL_RE.sub("", text, count=1))
    if declared is None:
        return
    name = _bare(declared.group("plsql") or declared.group("plpgsql"))
    context.cursors[name] = tuple(
        edge for edge in statement.edges if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
    )


def _emit(
    statements: list[ParsedStatement],
    parsed: ParsedStatement,
    ordinal: int,
    context: _WalkContext,
) -> int:
    """Append one located statement, token-grained, and a marker for each table
    function it reads; return the next free ordinal. A table the statement reads
    and names no column of gets its table-grain edge first (`_table_rows_read`),
    so that edge is token-grained like any other."""
    statement = _tokens_located(_table_rows_read(parsed, context), context)
    statements.append(statement)
    ordinal += 1
    for marker in _table_function_markers(statement, context.dialect, context.digest):
        # (2026-09-19) A gap marker's identity -- in `_dedupe_edges` and in the stored natural
        # key -- is its statement ordinal, and every marker read out of one statement carried the
        # statement's own. A second table function, or a call in the same statement, was dropped
        # as a duplicate of the first; descent then read the first through, removed its marker,
        # and reported the routine fully parsed with the second never read. The counter was
        # already advanced once per marker, so each marker sits in its own slot -- and nothing
        # that is not a marker is renumbered, which is what a decided edge's key depends on.
        statements.append(_renumbered(marker, ordinal))
        ordinal += 1
    return ordinal


def _renumbered(statement: ParsedStatement, ordinal: int) -> ParsedStatement:
    """`statement`, and every edge it carries, numbered `ordinal`."""
    return replace(
        statement,
        ordinal=ordinal,
        edges=tuple(replace(edge, statement_ordinal=ordinal) for edge in statement.edges),
    )


#: Oracle's one-row dummy table. `SELECT seq.NEXTVAL INTO v FROM dual` reads nothing from
#: it; a table-grain edge out of it would put every such routine among its readers.
_ORACLE_DUMMY_TABLES: Final = frozenset({"dual", "sys.dual", "public.dual"})


def _table_rows_read(statement: ParsedStatement, context: _WalkContext) -> ParsedStatement:
    """(2026-09-19) One table-grain edge for each table `statement` reads without naming
    any of its columns.

    `SELECT count(*) INTO v FROM t`, `WHERE EXISTS (SELECT 1 FROM t)`, `SELECT 1 FROM t`,
    `FROM a CROSS JOIN t`: the statement depends on `t` -- on how many rows it has, or
    whether any match -- yet no column of it reaches an edge, so the read produced no
    fact at all and `t` was absent from every answer about who reads it. The edge follows
    the `*` convention for "the table as a whole" (`STAR_COLUMN_MARKER` at both ends, as
    `_edges_from_values` and a `SELECT *` do), under its own transformation type,
    `TABLE_ROWS`: `TABLE_STAR` says every column flows, and here none does.

    A table counts as named when an edge reads it, and only then. It also counted when any
    column reference resolved to it, but the edge extractors read the select list and the
    WHERE and nothing else: a column in `JOIN b ON a.id = b.id`, in `MERGE ... ON t.id =
    s.id AND EXISTS (SELECT 1 FROM c WHERE c.k = s.k)`, in `GROUP BY`, `HAVING`, `ORDER BY`,
    or in a derived table's own clauses, named its table without producing an edge, so a
    table read only there appeared in no answer at all (2026-09-19). Never a source: the
    statement's own write target (a DELETE with no WHERE reads nothing) -- in T-SQL the
    FROM item the target designates (`_from_items_the_target_designates`) -- an INTO target
    (a variable), a CTE (its body's tables are what is read), a table function (its
    TABLE_FUNCTION_READ gap already states the read), Oracle's DUAL. PARTIAL, as every
    table-grain edge is.
    """
    node = statement.node
    if (
        not isinstance(node, exp.Expression)
        or statement.is_unparsed
        or statement.target_table is None
    ):
        return statement
    aliases = _collect_table_aliases_with_temp(node, context.subject)[0]
    named = {edge.source_table.lower() for edge in statement.edges if edge.source_resolved}
    ctes = {cte.alias.lower() for cte in node.find_all(exp.CTE) if cte.alias}
    target = _write_target_table(node, context.dialect)
    written = [target] if target is not None else []
    if isinstance(node, exp.Delete):
        # T-SQL `DELETE f FROM ...` lists what it deletes from in `tables`; sqlglot also
        # puts `DELETE TOP (1) FROM q`'s TOP there, as a table called TOP. Excluded by
        # node, not by name: `DELETE FROM q WHERE EXISTS (SELECT 1 FROM q)` does read q.
        written += [item for item in node.args.get("tables") or [] if isinstance(item, exp.Table)]
    written += _from_items_the_target_designates(node, context.dialect)
    unnamed: list[str] = []
    for table in node.find_all(exp.Table):
        if any(table is item for item in written) or table.find_ancestor(exp.Into) is not None:
            continue
        if _table_function_name(table) is not None:
            continue
        if not table.db and not table.catalog and table.name.lower() in ctes:
            continue
        name = table_reference_name(table)
        name = aliases.get(name, name)
        if (
            not name
            or name.lower() in named
            or (context.dialect == "oracle" and name.lower() in _ORACLE_DUMMY_TABLES)
        ):
            continue
        named.add(name.lower())
        unnamed.append(name)
    if not unnamed:
        return statement
    where = statement.statement_range
    added = tuple(
        ProcedureLineageEdgeRecord(
            source_table=name,
            source_column=STAR_COLUMN_MARKER,
            target_table=statement.target_table,
            target_column=STAR_COLUMN_MARKER,
            transformation_type=TransformationType.TABLE_ROWS.value,
            confidence=Confidence.PARTIAL.value,
            dialect=context.dialect,
            source_resolved=True,
            statement_ordinal=statement.ordinal,
            is_write=statement.is_write,
            is_intermediate=statement.is_intermediate_target,
            control_flow_context=statement.control_flow_context,
            statement_range=where,
            statement_range_status=(
                StatementRangeStatus.STATEMENT.value
                if where is not None
                else StatementRangeStatus.NOT_LOCATED.value
            ),
            statement_text_digest=context.digest if where is not None else None,
        )
        for name in unnamed
    )
    return replace(statement, is_no_lineage=False, edges=(*statement.edges, *added))


def _from_items_the_target_designates(node: exp.Expression, dialect: str) -> list[exp.Table]:
    """T-SQL names the table an UPDATE or DELETE writes through its FROM clause: `UPDATE t SET
    ... FROM dbo.tgt t`, `UPDATE dbo.tgt SET ... FROM dbo.tgt`, `DELETE t FROM dbo.a a JOIN
    dbo.tgt t ON ...`. The FROM item the target designates *is* the target, not a source --
    the same table node the statement writes, spelled again -- so it is never a table the
    statement reads. Those items, and only those, at the statement's own level: a table in a
    subquery is a read, and a second instance of the target under another alias
    (`FROM dbo.a a JOIN dbo.tgt t2 ON ...` for `UPDATE dbo.tgt`) is one too.

    A designation names an item by its alias -- or, when the item has none, by its own name --
    if it is unqualified; a qualified designation names the one item spelled the same and
    unaliased, as SQL Server does. Every other dialect writes a target its FROM never names
    (PostgreSQL's `UPDATE t ... FROM t t2` reads `t2`), so nothing is designated there."""
    if dialect != "tsql":
        return []
    if isinstance(node, exp.Update):
        designations = [node.this]
    elif isinstance(node, exp.Delete):
        designations = list(node.args.get("tables") or [])
    else:
        return []
    designators = [item for item in designations if isinstance(item, exp.Table)]
    designated: list[exp.Table] = []
    for item in node.find_all(exp.Table):
        if (
            any(item is designator for designator in designators)
            or item.find_ancestor(exp.Select, exp.Subquery, exp.CTE) is not None
        ):
            continue
        for designator in designators:
            if not designator.db and not designator.catalog:
                same = (item.alias or item.name).lower() == designator.name.lower()
            else:
                same = not item.alias and (
                    (item.catalog or "").lower(),
                    (item.db or "").lower(),
                    item.name.lower(),
                ) == (
                    (designator.catalog or "").lower(),
                    (designator.db or "").lower(),
                    designator.name.lower(),
                )
            if same:
                designated.append(item)
                break
    return designated


def _delete_target(node: exp.Delete, dialect: str) -> exp.Expression | None:
    """The table node a DELETE deletes from.

    sqlglot's `this` is the *first* FROM item; T-SQL's `DELETE t FROM dbo.a a JOIN dbo.tgt t ON
    ...` deletes from the item its `tables` designate -- here the joined one. Taking `this`
    recorded the delete, and the filter evidence in its WHERE, as a write to `dbo.a`, a table
    it only reads (2026-09-19). The designated FROM item when there is one, else `this`."""
    designated = _from_items_the_target_designates(node, dialect)
    return designated[0] if designated else node.this


def _write_target_table(node: exp.Expression, dialect: str = "") -> exp.Table | None:
    """The table node a statement writes -- never one of its sources."""
    target: object = None
    if isinstance(node, exp.Insert | exp.Create):
        target = node.this.this if isinstance(node.this, exp.Schema) else node.this
    elif isinstance(node, exp.Delete):
        target = _delete_target(node, dialect)
    elif isinstance(node, exp.Update | exp.Merge):
        target = node.this
    return target if isinstance(target, exp.Table) else None


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
    #: R11-FP03: its declaration section in order -- each declaration's span, or a
    #: subprogram defined there (`_consume_declarations`).
    declarations: tuple[_Declaration, ...] = ()
    #: Per parameter, whether it has a default and so may be left out of a call.
    parameter_defaults: tuple[bool, ...] = ()
    #: (2026-09-19) `(name, type)` of each OUT or IN OUT parameter, the type as written --
    #: what tells a ref cursor handed to the caller from a local one.
    out_parameters: tuple[tuple[str, str], ...] = ()
    #: A function's RETURN type as written; None for a procedure.
    return_type: str | None = None


#: One item of a PL/SQL declaration section: a declaration's `(start, end)` span in
#: the text, without its `;`, or a subprogram defined there.
_Declaration = tuple[int, int] | _MemberSpan


@dataclass(frozen=True, slots=True)
class _PackageLayout:
    members: tuple[_MemberSpan, ...]
    #: The package's own statements to walk: the initialization block, and anything
    #: after the package's END.
    package_segments: tuple[tuple[int, int], ...]
    #: R11-FP03: the package's own declarations -- the spec's, and the body's outside
    #: every member -- read by `_walk_declaration`, not as statements.
    package_declarations: tuple[tuple[int, int], ...] = ()
    #: The package's name as the body's header spells it, and its schema when the
    #: header qualifies it: what a call qualified with the package is matched on.
    name: str | None = None
    schema: str | None = None


def _is_package_text(sql: str, dialect: str) -> bool:
    return dialect == "oracle" and bool(_PACKAGE_TEXT_RE.match(sql))


def _parameter_names(text: str) -> tuple[str, ...]:
    """The parameter names of a subprogram header's parameter list, in order.

    Keeps each parameter's first identifier. A default expression is never kept --
    it can be a literal (INV-6), and the name is all resolution needs."""
    return tuple(name for name, _default in _parameters(text))


def _parameters(text: str) -> tuple[tuple[str, bool], ...]:
    """Each parameter of a header's parameter list: its name, and whether it has a
    default (`DEFAULT x` or `:= x`) -- whether a call may leave it out. Whether is
    read with the list's literals and comments blanked, so a default's text can
    never be mistaken for the keyword."""
    parameters: list[tuple[str, bool]] = []
    for piece in _top_level_pieces(text):
        match = re.match(r"\s*([A-Za-z_][\w$#]*)", piece)
        if match:
            code = re.sub(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", " ", piece, flags=re.DOTALL)
            default = ":=" in code or re.search(r"\bDEFAULT\b", code, re.IGNORECASE) is not None
            parameters.append((match.group(1), default))
    return tuple(parameters)


# ---------------------------------------------------------------------------
# (2026-09-19) Which ref cursors a routine hands its caller. `OPEN c FOR <query>` on
# one of them is the routine's result set, as `RETURN QUERY` is; on any other cursor
# the rows stay in the routine. Read from the header and the body -- the parameter's
# mode and type, and what a function returning a ref cursor RETURNs -- never guessed
# from the cursor's name.
# ---------------------------------------------------------------------------

#: A PL/SQL parameter passed back to the caller: `name OUT [NOCOPY] type` or `name IN
#: OUT [NOCOPY] type`, the type as written (`SYS_REFCURSOR`, `pkg.t_rc`).
_PLSQL_OUT_PARAMETER_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][\w$#]*)\s+(?:IN\s+)?OUT\s+(?:NOCOPY\s+)?"
    r"(?P<type>[A-Za-z][\w$#]*(?:\s*\.\s*[A-Za-z][\w$#]*)*)",
    re.IGNORECASE,
)
#: A PL/SQL function header's `RETURN type`.
_PLSQL_RETURN_TYPE_RE = re.compile(
    r"\bRETURN\s+(?P<type>[A-Za-z][\w$#]*(?:\s*\.\s*[A-Za-z][\w$#]*)*)", re.IGNORECASE
)
#: `TYPE t IS REF CURSOR [RETURN ...]`: a ref cursor type a package or unit declares.
_REF_CURSOR_TYPE_RE = re.compile(
    r"^\s*TYPE\s+(?P<name>[A-Za-z][\w$#]*)\s+IS\s+REF\s+CURSOR\b", re.IGNORECASE
)
#: A PL/pgSQL function's `RETURNS refcursor` or `RETURNS SETOF refcursor`.
_PG_RETURNS_REFCURSOR_RE = re.compile(r"\bRETURNS\s+(?:SETOF\s+)?refcursor\b", re.IGNORECASE)
#: `RETURN v` / PL/pgSQL `RETURN NEXT v`: the one name a statement hands back.
_RETURNED_NAME_RE = re.compile(r"^RETURN\s+(?:NEXT\s+)?(?P<name>[A-Za-z_][\w$#]*)$", re.IGNORECASE)


def _out_parameters(text: str) -> tuple[tuple[str, str], ...]:
    """`(name, type)` of each OUT or IN OUT parameter in a PL/SQL parameter list."""
    return tuple(
        (match.group("name"), re.sub(r"\s+", "", match.group("type")))
        for piece in _top_level_pieces(text)
        if (match := _PLSQL_OUT_PARAMETER_RE.match(piece))
    )


def _ref_cursor_types(sql: str, spans: Iterable[tuple[int, int]]) -> frozenset[str]:
    """The ref cursor types the declarations at `spans` define, lower-cased."""
    return frozenset(
        match.group("name").lower()
        for start, end in spans
        if (match := _REF_CURSOR_TYPE_RE.match(sql[start:end]))
    )


def _is_ref_cursor_type(type_name: str, declared: frozenset[str]) -> bool:
    """`SYS_REFCURSOR`, or a ref cursor type declared where this parse can see it. A type
    another package declares is not known here, so its cursor stays local."""
    last = type_name.split(".")[-1].lower()
    return last == "sys_refcursor" or last in declared


def _returned_names(sql: str, start: int, end: int) -> frozenset[str]:
    """Every name a `RETURN v` / `RETURN NEXT v` statement in `sql[start:end]` hands back."""
    body = sql[start:end]
    names: set[str] = set()
    for first, last in _split_top_level_statement_spans(body):
        returned = _RETURNED_NAME_RE.match(_peel_control_flow_prefix(body[first:last]).remainder)
        if returned:
            names.add(returned.group("name").lower())
    return frozenset(names)


def _unit_result_cursors(sql: str, unit: _MemberSpan, ref_types: frozenset[str]) -> frozenset[str]:
    """The ref cursors an Oracle routine or member hands its caller: its OUT ref-cursor
    parameters, and -- for a function returning a ref cursor -- what it RETURNs."""
    cursors = {
        name.lower() for name, type_name in unit.out_parameters
        if _is_ref_cursor_type(type_name, ref_types)
    }
    if unit.return_type is not None and _is_ref_cursor_type(unit.return_type, ref_types):
        cursors |= _returned_names(sql, unit.body_start, unit.body_end)
    return frozenset(cursors)


def _pg_result_cursors(sql: str) -> frozenset[str]:
    """The refcursors a PL/pgSQL routine hands its caller: an OUT or INOUT `refcursor`
    parameter, and what a function `RETURNS [SETOF] refcursor` RETURNs."""
    span = _dollar_quoted_body(sql)
    if span is None:
        return frozenset()
    header = sql[: span[0]]
    cursors: set[str] = set()
    if (routine := _ROUTINE_NAME_PAREN_RE.search(header)) is not None:
        close = _matching_paren(header, routine.end() - 1)
        for piece in _top_level_pieces(header[routine.end() : close] if close else ""):
            words = piece.split()
            if (
                len(words) >= 3
                and words[0].upper() in ("OUT", "INOUT")
                and words[2].lower().startswith("refcursor")
            ):
                cursors.add(_bare(words[1]))
    if _PG_RETURNS_REFCURSOR_RE.search(header):
        cursors |= _returned_names(sql, *span)
    return frozenset(cursors)


def _top_level_pieces(text: str) -> list[str]:
    """`text` split on its commas outside parentheses, quotes and comments -- a
    parameter list, or a call's argument list."""
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
    return pieces


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


def _consume_declarations(
    sql: str, tokens: list[_Token], index: int, stop: int
) -> tuple[list[_Declaration], int] | PackageSplitFailure:
    """Read a PL/SQL declaration section from token `index` (just past the IS, AS or
    DECLARE that opens it) up to `stop`: each declaration's span and each subprogram
    defined there, in order, and the index of the BEGIN or END that closes it.

    A declaration ends at its `;` outside parentheses and CASE expressions -- a
    default may hold either -- and a subprogram is consumed whole, so the `;`s
    inside it end nothing here. A declaration its `;` never closed ends at the
    BEGIN or END that closes the section, and is kept, so what could not be read
    is reported rather than dropped.
    """
    declarations: list[_Declaration] = []
    piece = tokens[index - 1][1]  # where the next declaration's text may begin
    first: int | None = None  # its first character, once a token of it is seen
    depth = 0
    cases = 0
    at = index
    while at < stop:
        start, end, text = tokens[at]
        if first is None:
            if text in ("BEGIN", "END"):
                return declarations, at
            if text in ("PROCEDURE", "FUNCTION"):
                consumed = _consume_subprogram(sql, tokens, at)
                if isinstance(consumed, PackageSplitFailure):
                    return consumed
                member, at = consumed
                if member is not None:
                    declarations.append(member)
                piece = tokens[at - 1][1]
                continue
            if text == ";":  # an empty declaration
                piece = end
                at += 1
                continue
            # A quoted name leaves no token of its own, so the text, not the token,
            # says where the declaration begins.
            first = _skip_trivia(sql, piece, start)
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif text == "CASE":
            cases += 1
        elif text == "END" and cases:
            cases -= 1
        elif depth == 0 and text == ";":
            declarations.append((first, start))
            first, piece = None, end
        elif depth == 0 and text in ("BEGIN", "END"):
            declarations.append((first, start))
            return declarations, at
        at += 1
    return PackageSplitFailure.UNBALANCED_BLOCKS


def _consume_subprogram(
    sql: str, tokens: list[_Token], index: int
) -> tuple[_MemberSpan | None, int] | PackageSplitFailure:
    """Read the subprogram whose PROCEDURE/FUNCTION keyword is at `index`.

    Returns the member (None for a forward declaration, which ends at its `;`)
    and the index just past it. A nested subprogram in the member's declaration
    section is consumed recursively and belongs to the member that contains it;
    since R11-FP03's second pass it is kept, with every other declaration, in
    `declarations`, which `_walk_unit` walks.
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
    # (2026-09-19) A standalone routine's header may qualify its name --
    # `PROCEDURE ops.p(x NUMBER)`, as DBMS_METADATA spells it. The scanner drops the
    # dot, so this read `ops` as the name and `p` as the start of the header's tail:
    # the unit was named after its schema and its parameters were never read. A
    # package member cannot be qualified, so a member header is unchanged by this.
    while at < len(tokens) and sql.startswith(".", _skip_trivia(sql, tokens[at - 1][1], len(sql))):
        if tokens[at][2] in (";", "(", ")") or '"' in sql[tokens[at - 1][1] : tokens[at][0]]:
            return PackageSplitFailure.UNREADABLE_MEMBER
        name = sql[tokens[at][0] : tokens[at][1]]
        at += 1
    parameters: tuple[tuple[str, bool], ...] = ()
    out_parameters: tuple[tuple[str, str], ...] = ()
    if at < len(tokens) and tokens[at][2] == "(":
        close = _matching_paren_token(tokens, at)
        if close is None:
            return PackageSplitFailure.UNBALANCED_BLOCKS
        listed = sql[tokens[at][1] : tokens[close][0]]
        parameters = _parameters(listed)
        out_parameters = _out_parameters(listed)
        at = close + 1
    parameter_names = tuple(parameter for parameter, _default in parameters)
    parameter_defaults = tuple(default for _parameter, default in parameters)
    tail = tokens[at - 1][1]  # where the header's tail -- RETURN type, IS/AS -- begins
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
    returns = _PLSQL_RETURN_TYPE_RE.search(sql, tail, tokens[at][0])
    return_type = re.sub(r"\s+", "", returns.group("type")) if returns else None
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
                tokens[at][0], tokens[at][0], parameter_defaults=parameter_defaults,
                out_parameters=out_parameters, return_type=return_type,
            ),
            at + 1,
        )
    section = _consume_declarations(sql, tokens, at, len(tokens))
    if isinstance(section, PackageSplitFailure):
        return section
    declarations, begin = section
    if tokens[begin][2] != "BEGIN":
        return PackageSplitFailure.UNBALANCED_BLOCKS
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
        declarations=tuple(declarations),
        parameter_defaults=parameter_defaults,
        out_parameters=out_parameters,
        return_type=return_type,
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
    declarations: list[tuple[int, int]] = []

    # The spec, when the text carries one: its declarations are package-level code.
    spec = next((index for index in range(body) if tokens[index][2] == "PACKAGE"), None)
    if spec is not None:
        opened = _header_end(tokens, spec + 1)
        if opened is None:
            return PackageSplitFailure.UNBALANCED_BLOCKS
        section = _consume_declarations(sql, tokens, opened, body)
        if isinstance(section, PackageSplitFailure):
            return section
        items, closed = section
        after = _statement_end(tokens, closed + 1) if tokens[closed][2] == "END" else None
        if after is None or any(
            tokens[index][2] not in _PACKAGE_HEADER_WORDS for index in range(after, body)
        ):
            return PackageSplitFailure.UNBALANCED_BLOCKS
        # A spec declares its subprograms; one that defines a body is not a spec.
        if any(isinstance(item, _MemberSpan) for item in items):
            return PackageSplitFailure.UNBALANCED_BLOCKS
        declarations.extend(item for item in items if not isinstance(item, _MemberSpan))

    opened = _header_end(tokens, body + 2)
    if opened is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    section = _consume_declarations(sql, tokens, opened, len(tokens))
    if isinstance(section, PackageSplitFailure):
        return section
    items, closing = section
    members = [item for item in items if isinstance(item, _MemberSpan)]
    declarations.extend(item for item in items if not isinstance(item, _MemberSpan))
    if tokens[closing][2] == "BEGIN":
        # The initialization block, closed by the package's own END.
        close = _matching_end(tokens, closing)
        if close is None:
            return PackageSplitFailure.UNBALANCED_BLOCKS
        segments.append((tokens[closing][1], tokens[close][0]))
        closing = close
    tail = _statement_end(tokens, closing + 1)
    if tail is None:
        return PackageSplitFailure.UNBALANCED_BLOCKS
    if tail < len(tokens):
        # Anything after the package's own END is walked, never dropped.
        segments.append((tokens[tail][0], len(sql)))
    schema, name = _package_name(sql, tokens, body)
    return _PackageLayout(
        members=tuple(members),
        package_segments=tuple((start, end) for start, end in segments if end > start),
        package_declarations=tuple(
            (start, end) for start, end in declarations if end > start
        ),
        name=name,
        schema=schema,
    )


def _package_name(sql: str, tokens: list[_Token], body: int) -> tuple[str | None, str | None]:
    """`(schema, name)` of the package as its body's header spells them -- the schema
    None when the header does not qualify the name, both None for a quoted name."""
    at = body + 2
    if at >= len(tokens) or '"' in sql[tokens[body + 1][1] : tokens[at][0]]:
        return None, None
    first = sql[tokens[at][0] : tokens[at][1]]
    dot = _skip_trivia(sql, tokens[at][1], len(sql))
    if not sql.startswith(".", dot):
        return None, first
    if at + 1 >= len(tokens) or '"' in sql[dot : tokens[at + 1][0]]:
        return None, None
    return first, sql[tokens[at + 1][0] : tokens[at + 1][1]]


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
    pending_member_calls: tuple[PendingMemberCall, ...] = ()


# ---------------------------------------------------------------------------
# What a routine declares (2026-09-19): the names `aida.procedure_column_owners`
# tells apart from columns. Collected from the text, per unit where the text has
# units (an Oracle routine, member, nested subprogram or block) and per routine
# otherwise. A name collected too widely can only make a reference ambiguous in
# PL/SQL, never attribute it; in PL/pgSQL a routine is one text.
# ---------------------------------------------------------------------------

#: Words a PostgreSQL parameter's type may start with, so an unnamed parameter
#: (`f(double precision)`) is not read as one named `double`.
_PG_TYPE_WORDS: Final = frozenset(
    {
        "anyarray", "anyelement", "bigint", "bit", "bool", "boolean", "bytea", "char",
        "character", "cidr", "date", "decimal", "double", "float", "inet", "int", "integer",
        "interval", "json", "jsonb", "money", "national", "numeric", "oid", "real", "record",
        "refcursor", "regclass", "setof", "smallint", "text", "time", "timestamp",
        "timestamptz", "uuid", "varchar", "void", "xml",
    }
)
#: A parameter's mode, which precedes its name.
_PARAMETER_MODES: Final = frozenset({"IN", "OUT", "INOUT", "VARIADIC"})
_ROUTINE_NAME_PAREN_RE = re.compile(
    r'\b(?:FUNCTION|PROCEDURE)\s+(?:"[^"]+"|[\w$#]+)(?:\s*\.\s*(?:"[^"]+"|[\w$#]+))*\s*\(',
    re.IGNORECASE,
)
_RETURNS_TABLE_RE = re.compile(r"\bRETURNS\s+TABLE\s*\(", re.IGNORECASE)
#: `CURSOR c [(params)]` (PL/SQL) or `c [[NO] SCROLL] CURSOR [(params)]` (PL/pgSQL).
_CURSOR_NAMES_RE = re.compile(
    r'^\s*(?:DECLARE\s+)?(?:CURSOR\s+(?P<plsql>"[^"]+"|[\w$#]+)'
    r'|(?P<plpgsql>"[^"]+"|[\w$#]+)\s+(?:(?:NO\s+)?SCROLL\s+)?CURSOR)\s*(?P<paren>\()?',
    re.IGNORECASE,
)
_FIRST_NAME_RE = re.compile(r'^\s*(?:DECLARE\s+)?("[^"]+"|[A-Za-z_][\w$#]*)', re.IGNORECASE)


def _bare(name: str) -> str:
    return name.strip('"').lower()


def _declaration_names(text: str) -> frozenset[str]:
    """The names one declaration puts in scope: a variable's, a cursor's and its
    parameters. A TYPE, SUBTYPE or PRAGMA names no value a statement can read."""
    if cursor := _CURSOR_NAMES_RE.match(text):
        names = {_bare(cursor.group("plsql") or cursor.group("plpgsql"))}
        if cursor.group("paren"):
            close = _matching_paren(text, cursor.end() - 1)
            if close is not None:
                names |= {_bare(name) for name in _parameter_names(text[cursor.end() : close])}
        return frozenset(names)
    first = _FIRST_NAME_RE.match(text)
    if first is None or first.group(1).upper() in ("TYPE", "SUBTYPE", "PRAGMA"):
        return frozenset()
    return frozenset({_bare(first.group(1))})


def _block_declared_names(sql: str, start: int, end: int) -> frozenset[str]:
    """Names declared inside `sql[start:end]`: every DECLARE section's (a nested block,
    an anonymous block, a PL/pgSQL routine's own) and every loop variable -- `FOR r IN`,
    `FOR i IN REVERSE`, `FOREACH x [SLICE n] IN ARRAY`, `FORALL i IN`."""
    tokens: list[_Token] = [
        (start + first, start + last, sql[start + first : start + last].upper())
        for first, last, _kind in _scan_tokens(sql[start:end])
    ]
    names: set[str] = set()
    for index, (_first, _last, word) in enumerate(tokens):
        if word in ("FOR", "FOREACH", "FORALL"):
            loop: list[str] = []
            at = index + 1
            while at < len(tokens) and len(loop) < 4 and tokens[at][2] not in (
                "IN", ";", "(", ")", "LOOP",
            ):
                loop.append(sql[tokens[at][0] : tokens[at][1]])
                at += 1
            if at < len(tokens) and tokens[at][2] == "IN":
                names |= {_bare(name) for name in loop if name.upper() != "SLICE"}
        elif word == "DECLARE":
            section = _consume_declarations(sql, tokens, index + 1, len(tokens))
            if isinstance(section, PackageSplitFailure):
                continue
            for item in section[0]:
                if not isinstance(item, _MemberSpan):
                    names |= _declaration_names(sql[item[0] : item[1]])
    return frozenset(names)


def _unit_declared_names(sql: str, unit: _MemberSpan) -> frozenset[str]:
    """What one Oracle unit declares: its parameters, its declaration section, and the
    nested blocks and loop variables of its own body."""
    names = {_bare(name) for name in unit.parameter_names}
    for item in unit.declarations:
        if not isinstance(item, _MemberSpan):
            names |= _declaration_names(sql[item[0] : item[1]])
    return frozenset(names) | _block_declared_names(sql, unit.body_start, unit.body_end)


def _pg_parameter_name(piece: str) -> str | None:
    head = re.split(r"\bDEFAULT\b|=", piece, maxsplit=1, flags=re.IGNORECASE)[0].split()
    if head and head[0].upper() in _PARAMETER_MODES:
        head = head[1:]
    if len(head) < 2 or head[0].lower() in _PG_TYPE_WORDS:
        return None  # an unnamed parameter: its type alone
    return _bare(head[0])


def _header_parameter_names(sql: str, dialect: str) -> frozenset[str]:
    """The named parameters of a CREATE FUNCTION/PROCEDURE header, and on PostgreSQL the
    columns of `RETURNS TABLE (...)` -- OUT parameters by another spelling."""
    if not _has_routine_header(sql, dialect):
        return frozenset()
    span = _dollar_quoted_body(sql)
    header = sql[: span[0]] if span is not None else sql
    names: set[str] = set()
    for pattern in (_ROUTINE_NAME_PAREN_RE, _RETURNS_TABLE_RE):
        match = pattern.search(header)
        if match is None:
            continue
        close = _matching_paren(header, match.end() - 1)
        if close is None:
            continue
        inner = header[match.end() : close]
        if dialect == "postgres":
            names |= {
                name for piece in _top_level_pieces(inner) if (name := _pg_parameter_name(piece))
            }
        else:
            names |= {_bare(name) for name in _parameter_names(inner)}
    return frozenset(names)


def _routine_declared_names(sql: str, dialect: str, plpgsql: bool) -> DeclaredNames:
    """What the routine as a whole declares, and what such a name means in a statement.

    PL/pgSQL: its parameters, RETURNS TABLE columns, DECLARE sections and loop variables;
    a declared name is the variable -- PostgreSQL's default `variable_conflict = error`
    rejects one that could also be a column -- unless the body says `#variable_conflict
    use_column`. A LANGUAGE sql function's parameters: a column of the same name wins.
    PL/SQL: a column wins too (its name resolution gives the column precedence); the
    names of a routine read as units are added unit by unit (`_walk_unit`), so here only
    what a body read whole declares -- a block with no declaration section, a package
    that could not be split. Other engines' variables are not bare names (T-SQL's `@v`).
    """
    if dialect == "postgres":
        header = _header_parameter_names(sql, dialect)
        if not plpgsql:
            return DeclaredNames(header, variable_wins=False)
        span = _dollar_quoted_body(sql)
        start, end = span if span is not None else (0, len(sql))
        conflict = _VARIABLE_CONFLICT_RE.search(sql, start, end)
        return DeclaredNames(
            header | _block_declared_names(sql, start, end),
            variable_wins=conflict is None or conflict.group("mode").lower() != "use_column",
        )
    if dialect == "oracle":
        return DeclaredNames(
            _header_parameter_names(sql, dialect) | _block_declared_names(sql, 0, len(sql)),
            variable_wins=False,
        )
    return NO_DECLARED_NAMES


def _plpgsql_declaration_section(sql: str, body_start: int) -> tuple[int, int] | None:
    """`(start, end)` of a PL/pgSQL routine's own DECLARE section: after its DECLARE,
    up to the BEGIN that `body_start` is just past. None when it has none."""
    span = _dollar_quoted_body(sql)
    if span is None or not span[0] < body_start <= span[1]:
        return None
    words = [
        (span[0] + first, span[0] + last, sql[span[0] + first : span[0] + last].upper())
        for first, last, kind in _scan_tokens(sql[span[0] : body_start])
        if kind == "word"
    ]
    if not words or words[-1][2] != "BEGIN":
        return None
    declare = next((word for word in words if word[2] == "DECLARE"), None)
    if declare is None:
        return None
    return declare[1], words[-1][0]


def _walk_plpgsql_declarations(
    sql: str, start: int, end: int, ordinal: int, context: _WalkContext
) -> tuple[list[ParsedStatement], int]:
    """A PL/pgSQL DECLARE section, one declaration at a time (`_plpgsql_declaration`).

    A declaration that reads nothing produces no statement, so the body's ordinals move
    only by the reads and gaps found here. One that is not a declaration shape at all is
    a gap located at it, never dropped."""
    statements: list[ParsedStatement] = []
    for first, last in _split_top_level_statement_spans(sql[start:end]):
        at, until = start + first, start + last
        text = sql[at:until]
        statement = _plpgsql_declaration(
            ordinal, text, context.dialect, context.sqlglot_dialect, context.subject,
            context.names,
        )
        if statement is None:
            if _PLPGSQL_ITEM_DECLARATION_RE.match(text):
                continue
            statement = _unparsed_statement(
                ordinal, context.dialect, DECLARATION_CONTEXT,
                f"{UnparsedReason.UNSUPPORTED_STATEMENT_SHAPE.value}: "
                "unrecognised PL/pgSQL declaration",
            )
        located = _located(statement, context.locator.span(at, until), context.digest)
        emitted = len(statements)
        ordinal = _emit(statements, located, ordinal, context)
        _remember_cursor(context, text, statements[emitted])
    return statements, ordinal


def _walk_declaration(
    sql: str, start: int, end: int, ordinal: int, context: _WalkContext
) -> tuple[list[ParsedStatement], int]:
    """One declaration of a PL/SQL declaration section (R11-FP03).

    A cursor's query is read, as the statement the chunk classifier makes of it. A
    cursor spec, and every other declaration PL/SQL admits there, is lineage-free
    (`_PLSQL_LINEAGE_FREE_DECLARATION_RE` says why) and produces no statement at
    all, so the statements after it keep the ordinals they always had. Anything
    else -- including an item declaration holding a query, which PL/SQL itself
    refuses -- is reported as a gap located at the declaration, never dropped.
    """
    text = sql[start:end]
    if _PLSQL_CURSOR_DECLARATION_RE.match(text):
        if _cursor_query(text) is None:
            return [], ordinal
        return _walk_span(sql, start, end, ordinal, context)
    if _PLSQL_LINEAGE_FREE_DECLARATION_RE.match(text) and not any(
        kind == "word" and text[first:last].upper() == "SELECT"
        for first, last, kind in _scan_tokens(text)
    ):
        return [], ordinal
    last = end
    while last > start and sql[last - 1].isspace():
        last -= 1
    marker = _unparsed_statement(
        ordinal,
        context.dialect,
        DECLARATION_CONTEXT,
        f"{UnparsedReason.UNSUPPORTED_STATEMENT_SHAPE.value}: unrecognised PL/SQL declaration",
    )
    return [_located(marker, context.locator.span(start, last), context.digest)], ordinal + 1


def _walk_unit(
    sql: str,
    unit: _MemberSpan,
    ordinal: int,
    context: _WalkContext,
    scope: frozenset[str] = frozenset(),
    *,
    nested: bool = False,
    ref_types: frozenset[str] = frozenset(),
) -> tuple[list[ParsedStatement], int]:
    """A subprogram's declarations -- its nested subprograms included -- then its
    own body, in text order (R11-FP03).

    A nested subprogram's statements are this unit's: it can only run when this
    unit calls it. So a call to a subprogram declared here or in a unit enclosing
    this one (`scope`) adds nothing and is no gap -- and it is settled here, first,
    because PL/SQL resolves the name to that local subprogram before any package
    member or schema-level routine of the same name.

    (2026-09-19) The ref cursors the unit hands its caller are its own OUT ref-cursor
    parameters and returned cursors (`_unit_result_cursors`, knowing the ref cursor
    types `ref_types` and its own declarations define). A `nested` subprogram hands
    its OUT cursor to the unit that calls it, so there only the enclosing unit's result
    cursors count -- less any name the nested one declares over them. Each unit walks
    with its own copy of the cursors in scope.
    """
    local = scope | {
        item.name.lower() for item in unit.declarations if isinstance(item, _MemberSpan)
    }
    ref_types = ref_types | _ref_cursor_types(
        sql, (item for item in unit.declarations if not isinstance(item, _MemberSpan))
    )
    # 2026-09-19: what this unit declares is in scope for its statements and for those
    # of the subprograms nested in it, on top of what encloses it.
    declared = _unit_declared_names(sql, unit)
    names = _scoped(context.names)
    results = (
        names.result_cursors - declared
        if nested
        else _unit_result_cursors(sql, unit, ref_types)
    )
    context = replace(
        context,
        names=replace(names.with_names(declared), result_cursors=results),
        cursors=dict(context.cursors),
        fetched={},
    )
    statements: list[ParsedStatement] = []
    for item in unit.declarations:
        if isinstance(item, _MemberSpan):
            walked, ordinal = _walk_unit(
                sql, item, ordinal, context, local, nested=True, ref_types=ref_types
            )
        else:
            walked, ordinal = _walk_declaration(sql, item[0], item[1], ordinal, context)
        statements.extend(walked)
    walked, ordinal = _walk_span(sql, unit.body_start, unit.body_end, ordinal, context)
    statements.extend(walked)
    return [_resolved_locally(statement, local) for statement in statements], ordinal


def _resolved_locally(statement: ParsedStatement, local: frozenset[str]) -> ParsedStatement:
    call = statement.call_site
    if (
        call is None
        or not statement.is_unparsed
        or "." in call.callee
        or call.callee.lower() not in local
    ):
        return statement
    return replace(
        statement, is_unparsed=False, is_no_lineage=True, unparsed_reason=None, edges=()
    )


def _oracle_block(sql: str) -> _MemberSpan | None:
    """A standalone Oracle routine or anonymous block, read as a package member is
    read: its declarations and nested subprograms, then its own BEGIN ... END.

    `_extract_body_span` takes the text between the first BEGIN and its END. For a
    routine whose declaration section defines a subprogram, that is the nested
    subprogram's body: the routine's own statements were never seen, and it read
    as fully parsed. None when the text is not one of these shapes or does not read
    as one -- a quoted name, an external implementation, a block that does not
    close -- and the caller keeps that older reading.
    """
    tokens: list[_Token] = [
        (start, end, sql[start:end].upper()) for start, end, _kind in _scan_tokens(sql)
    ]
    if not tokens:
        return None
    if _ORACLE_SOURCE_HEADER_RE.match(sql):
        keyword = next(
            (
                index
                for index, token in enumerate(tokens[:6])
                if token[2] in ("PROCEDURE", "FUNCTION")
            ),
            None,
        )
        if keyword is None:
            return None
        consumed = _consume_subprogram(sql, tokens, keyword)
        if isinstance(consumed, PackageSplitFailure) or consumed[0] is None:
            return None
        unit = consumed[0]
        return unit if unit.body_end > unit.body_start else None
    # An anonymous block with declarations: what ALL_TRIGGERS.TRIGGER_BODY holds for an
    # Oracle trigger that has any, or the same after a CREATE TRIGGER header.
    opened: int | None = None
    if tokens[0][2] == "DECLARE":
        opened = 1
    elif _HEADER_RE.match(sql) and "TRIGGER" in (token[2] for token in tokens[:4]):
        depth = 0
        for index, (_start, _end, text) in enumerate(tokens):
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            elif depth == 0 and text in ("DECLARE", "BEGIN", "COMPOUND"):
                opened = index + 1 if text == "DECLARE" else None
                break
    if opened is None:
        return None
    section = _consume_declarations(sql, tokens, opened, len(tokens))
    if isinstance(section, PackageSplitFailure) or tokens[section[1]][2] != "BEGIN":
        return None
    declarations, begin = section
    close = _matching_end(tokens, begin)
    if close is None:
        return None
    return _MemberSpan(
        name="",
        kind="BLOCK",
        parameter_names=(),
        start=tokens[0][0],
        end=tokens[close][1],
        body_start=tokens[begin][1],
        body_end=tokens[close][0],
        declarations=tuple(declarations),
    )


def _accepts(member: _MemberSpan, call: CallSite) -> bool:
    """Whether `member`'s parameter list accepts `call`'s arguments as written.

    PL/SQL's mixed notation: positional arguments first, then named ones, and every
    parameter with no default given one way or the other. Only names and counts are
    compared -- the types the text does not state are not guessed at, so two
    overloads that differ only by type both accept, and the call is ambiguous."""
    names = [name.lower() for name in member.parameter_names]
    defaults = member.parameter_defaults or (False,) * len(names)
    positional = 0
    while positional < len(call.argument_names) and call.argument_names[positional] is None:
        positional += 1
    named: list[str] = []
    for argument in call.argument_names[positional:]:
        if argument is None:
            return False  # a positional argument after a named one
        named.append(argument.lower())
    if positional > len(names) or len(set(named)) != len(named):
        return False
    if any(name not in names[positional:] for name in named):
        return False
    return all(
        index < positional or names[index] in named or defaults[index]
        for index in range(len(names))
    )


def _member_call_target(call: CallSite, layout: _PackageLayout) -> int | str | None:
    """The member of this package `call` invokes: its index in `layout.members`,
    `MEMBER_CALL_AMBIGUOUS` when no single member accepts the call as written, or
    None when the call is not into this package -- a schema-level routine, another
    package's, a remote one -- which is left to catalog descent."""
    if "@" in call.callee:
        return None
    parts = [part.lower() for part in call.callee.split(".")]
    package = (layout.name or "").lower()
    schema = (layout.schema or "").lower()
    if len(parts) == 1:
        name = parts[0]
    elif len(parts) == 2 and package and parts[0] == package:
        name = parts[1]
    elif len(parts) == 3 and package and schema and parts[:2] == [schema, package]:
        name = parts[2]
    else:
        return None
    named = [
        index for index, member in enumerate(layout.members) if member.name.lower() == name
    ]
    if not named:
        return None
    accepting = [index for index in named if _accepts(layout.members[index], call)]
    return accepting[0] if len(accepting) == 1 else MEMBER_CALL_AMBIGUOUS


_CONFIDENCE_RANK: Final = {
    Confidence.LOW.value: 0, Confidence.PARTIAL.value: 1, Confidence.FULL.value: 2
}


def _read_through(
    call: ProcedureLineageEdgeRecord,
    edge: ProcedureLineageEdgeRecord,
    callee: str,
    via_routine_locator: int | None = None,
) -> ProcedureLineageEdgeRecord:
    """`edge`, read from a member this one calls, as the caller's fact at the call.

    Built from the call's own marker edge, so everything that says where the fact
    is -- ordinal, range, digest, member and grain -- is the call's, and nothing
    that indexes the callee's statement comes with it. At most PARTIAL, because a
    parameter can steer the callee's branches, and a callee's result set stays in
    the callee: a PL/SQL call returns none. `aida.routine_call_descent` does the
    same across routines. `via_routine_locator` (R11-FP03) is a statement ordinal
    inside the *callee* member's own span, so `routine_edge_row` can later resolve
    the callee's actual captured routine id -- never the caller's -- the same way
    `member_routine_id` is resolved, because only that lookup tells two overloads
    of one member name apart."""
    target, intermediate, write = edge.target_table, edge.is_intermediate, edge.is_write
    if target == PROCEDURE_RESULT_TARGET:
        target, intermediate, write = PROCEDURE_LOCAL_TARGET, True, False
    return replace(
        call,
        source_table=edge.source_table,
        source_column=edge.source_column,
        target_table=target,
        target_column=edge.target_column,
        transformation_type=edge.transformation_type,
        confidence=(
            edge.confidence
            if _CONFIDENCE_RANK.get(edge.confidence, 0) < _CONFIDENCE_RANK[Confidence.PARTIAL.value]
            else Confidence.PARTIAL.value
        ),
        source_resolved=edge.source_resolved,
        is_write=write,
        is_intermediate=intermediate,
        control_flow_context=call.control_flow_context or edge.control_flow_context,
        unparsed_reason=None,
        via_temp_table=edge.via_temp_table,
        via_routine=callee,
        via_routine_locator=via_routine_locator,
        statement_range_status=(
            StatementRangeStatus.CALL_SITE.value
            if call.statement_range is not None
            else StatementRangeStatus.NOT_LOCATED.value
        ),
    )


def _member_calls_read_through(
    walked: list[tuple[int | None, list[ParsedStatement]]], layout: _PackageLayout
) -> tuple[list[tuple[int | None, list[ParsedStatement]]], list[PendingMemberCall]]:
    """Each call between members of this package, read through at the call.

    `walked` is each unit's statements with the member it belongs to (an index in
    `layout.members`, None for package-level code). A call resolves to a member by
    `_member_call_target`; the callee's lineage is its own statements' edges and
    those of every member it reaches in turn -- never re-entering the calling member,
    whose own facts are already its own, which is also what ends a recursive walk.
    The call's gap marker goes only when all of that was fully parsed; otherwise it
    stays, naming why in `aida.routine_call_descent`'s words, next to what was read.
    """
    targets: dict[tuple[int, int], int | str] = {}
    calls: dict[int, set[int]] = {index: set() for index, _group in walked if index is not None}
    for position, (member, group) in enumerate(walked):
        for offset, statement in enumerate(group):
            if statement.call_site is None or not statement.is_unparsed:
                continue
            target = _member_call_target(statement.call_site, layout)
            if target is None:
                continue
            targets[(position, offset)] = target
            if member is not None and isinstance(target, int):
                calls[member].add(target)
    if not targets:
        return walked, []

    own: dict[int, list[ProcedureLineageEdgeRecord]] = {}
    gap: dict[int, bool] = {}
    # R11-FP03: a member's own first ordinal, so a caller's spliced edge can carry
    # *this* member as `via_routine_locator` -- resolved to its captured routine id
    # later, by ordinal, the same way `member_routine_id` already is.
    first_ordinal: dict[int, int] = {}
    for position, (member, group) in enumerate(walked):
        if member is None:
            continue
        own[member] = [
            edge
            for statement in group
            for edge in statement.edges
            if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
        ]
        gap[member] = any(
            statement.is_unparsed
            and not isinstance(targets.get((position, offset)), int)
            for offset, statement in enumerate(group)
        )
        ordinals = [statement.ordinal for statement in group]
        if ordinals:
            first_ordinal[member] = min(ordinals)

    pending: list[PendingMemberCall] = []
    result: list[tuple[int | None, list[ParsedStatement]]] = []
    for position, (member, group) in enumerate(walked):
        statements = list(group)
        for offset, statement in enumerate(group):
            target = targets.get((position, offset))
            if target is None:
                continue
            marker = statement.edges[0]
            if not isinstance(target, int):
                reason = f"{marker.unparsed_reason} ({target})"
                statements[offset] = replace(
                    statement,
                    unparsed_reason=reason,
                    edges=(replace(marker, unparsed_reason=reason),),
                )
                continue
            reached: list[int] = []
            frontier = [] if target == member else [target]
            seen = set(frontier)
            while frontier:
                index = frontier.pop()
                reached.append(index)
                for onward in sorted(calls.get(index, ())):
                    if onward != member and onward not in seen:
                        seen.add(onward)
                        frontier.append(onward)
            callee = layout.members[target].name
            via = f"{layout.name}.{callee}" if layout.name else callee
            via_ordinal = first_ordinal.get(target)
            spliced = [
                _read_through(marker, edge, via, via_ordinal)
                for index in sorted(reached)
                for edge in own.get(index, [])
            ]
            complete = not any(gap.get(index, False) for index in reached)
            written = any(edge.is_write for edge in spliced)
            if complete:
                statements[offset] = replace(
                    statement,
                    is_unparsed=False,
                    is_no_lineage=not spliced,
                    is_write=written,
                    unparsed_reason=None,
                    edges=tuple(spliced),
                )
            else:
                reason = f"{marker.unparsed_reason} ({MEMBER_CALL_NOT_FULLY_PARSED})"
                statements[offset] = replace(
                    statement,
                    is_write=written,
                    unparsed_reason=reason,
                    edges=(*spliced, replace(marker, unparsed_reason=reason)),
                )
                # R11-FP03: deferred, not decided -- a reached member's own blocking
                # gap may still be an ordinary, undecided external call that catalog
                # descent (running after this whole parse) can resolve; recorded so
                # it can re-check this call once it does, rather than this call
                # being stuck with today's pessimistic marker forever. `statement`
                # (not `statements[offset]`) is the pre-splice ordinal, unchanged.
                pending.append(
                    PendingMemberCall(
                        statement_ordinal=statement.ordinal,
                        reached_member_indices=tuple(reached),
                        via_routine=via,
                    )
                )
        result.append((member, statements))
    return result, pending


def _walk_package(sql: str, layout: _PackageLayout, context: _WalkContext) -> _Walk:
    #: (start, kind, span or member index): the package's own declarations and
    #: statements, and its members, walked in text order.
    units: list[tuple[int, str, tuple[int, int] | int]] = sorted(
        [(start, "declaration", (start, end)) for start, end in layout.package_declarations]
        + [(start, "code", (start, end)) for start, end in layout.package_segments]
        + [(member.start, "member", index) for index, member in enumerate(layout.members)],
        key=lambda unit: unit[0],
    )
    walked: list[tuple[int | None, list[ParsedStatement]]] = []
    ordinal = 0
    # 2026-09-19: the package's own variables are in scope in every member.
    package_names: set[str] = set()
    for start, end in layout.package_declarations:
        package_names |= _declaration_names(sql[start:end])
    for start, end in layout.package_segments:
        package_names |= _block_declared_names(sql, start, end)
    context = replace(context, names=context.names.with_names(frozenset(package_names)))
    ref_types = _ref_cursor_types(sql, layout.package_declarations)
    for _start, kind, where in units:
        if isinstance(where, int):
            member = layout.members[where]
            statements, ordinal = _walk_unit(sql, member, ordinal, context, ref_types=ref_types)
            attributed = [
                _attributed_to(s, member.name, MemberAttribution.MEMBER) for s in statements
            ]
            walked.append((where, attributed))
            continue
        start, end = where
        if kind == "declaration":
            statements, ordinal = _walk_declaration(sql, start, end, ordinal, context)
        else:
            statements, ordinal = _walk_span(sql, start, end, ordinal, context)
        attributed = [_attributed_to(s, None, MemberAttribution.PACKAGE_LEVEL) for s in statements]
        walked.append((None, attributed))
    walked, pending_member_calls = _member_calls_read_through(walked, layout)
    members: list[PackageMember] = []
    for index, group in walked:
        if index is None:
            continue
        member = layout.members[index]
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
    groups = [group for _index, group in walked]
    return _Walk(
        statements=[statement for group in groups for statement in group],
        groups=groups,
        digest=context.digest,
        member_attribution=MemberAttribution.MEMBER.value,
        members=tuple(members),
        pending_member_calls=tuple(pending_member_calls),
    )


def _walk(sql: str, dialect: str, subject: Mapping[str, str] | None) -> _Walk:
    if not _SQLGLOT_AVAILABLE or dialect not in _SQLGLOT_DIALECT_MAP:
        return _Walk(statements=[], groups=[], digest=None)
    plpgsql = _is_plpgsql(sql, dialect)
    names = _routine_declared_names(sql, dialect, plpgsql)
    context = _WalkContext(
        dialect=dialect,
        sqlglot_dialect=_SQLGLOT_DIALECT_MAP[dialect],
        plpgsql=plpgsql,
        # A body with no firing table still names firing rows if it is a trigger
        # function; see `unbound_subject_aliases`.
        subject=subject or unbound_subject_aliases(dialect),
        locator=_Locator(sql),
        digest=statement_text_digest(sql),
        # An Oracle routine read as units collects its names unit by unit, on top of its
        # header's parameters. `_consume_subprogram` reads a schema-qualified name since
        # 2026-09-19 (`PROCEDURE ops.p(x ...)` read as a unit named `ops`, with no
        # parameters); the header's are kept here for a text no unit reading accepts.
        names=(
            DeclaredNames(_header_parameter_names(sql, dialect)) if dialect == "oracle" else names
        ),
    )
    fallback: PackageSplitFailure | None = None
    if _is_package_text(sql, dialect):
        layout = _package_layout(sql)
        if isinstance(layout, _PackageLayout):
            return _walk_package(sql, layout, context)
        fallback = layout
    # R11-FP03: a standalone routine or block is read with its declaration section;
    # a package that could not be split keeps the whole-body reading it always had.
    block = _oracle_block(sql) if dialect == "oracle" and fallback is None else None
    if block is not None:
        statements, _next = _walk_unit(sql, block, 0, context)
    else:
        # (2026-09-19) A PL/pgSQL routine's OUT refcursor, or the one it RETURNs, carries
        # its result set; see `_open_for`.
        context = replace(
            context,
            names=replace(
                _scoped(names),
                result_cursors=_pg_result_cursors(sql) if plpgsql else frozenset(),
            ),
        )
        start, end = _extract_body_span(sql, dialect)
        statements, ordinal = [], 0
        section = _plpgsql_declaration_section(sql, start) if plpgsql else None
        if section is not None:
            # 2026-09-19: a PL/pgSQL routine's DECLARE section sits outside the BEGIN ...
            # END the body is taken from, so it was never walked at all.
            statements, ordinal = _walk_plpgsql_declarations(sql, *section, ordinal, context)
        body, _next = _walk_span(sql, start, end, ordinal, context)
        statements.extend(body)
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
            pending_member_calls=walk.pending_member_calls,
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
        pending_member_calls=walk.pending_member_calls,
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
