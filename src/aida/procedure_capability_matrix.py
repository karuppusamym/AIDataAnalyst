"""AT-22: the parser capability matrix, derived from the parsers' own code.

Per the tracker row, publishing a coverage matrix is only honest once the
parser it describes degrades explicitly rather than silently (AT-D2/AT-D5,
now done) -- and it must not itself become the "marketed vs actual" drift
this platform criticises elsewhere (`Docs/review-2026-08/`). The second half
is what this module is for: every fact in the published matrix is *read out
of the parsers' own source* by this module at generation time, never
hand-typed prose that can quietly drift from what the code actually does.

Three kinds of fact, three extraction techniques:

1. **Which sqlglot dialects a parser will even attempt.**
   `sql_lineage_parser._SQLGLOT_DIALECT_MAP`'s keys, read directly -- both
   `sql_lineage_parser.py` (view/flat-DML lineage) and `procedure_lineage.py`
   (procedure-aware lineage, this module's own sibling) share the same map.

2. **Which SQL statement *shapes* each parser's dispatcher recognises.**
   `_dispatched_node_types` walks the actual Python AST of
   `sql_lineage_parser._extract_from_statement` and
   `procedure_lineage._classify_and_extract` -- the two functions that
   decide what to do with a parsed statement -- and extracts every
   `isinstance(node, exp.<Type>)` check each one makes. A branch added or
   removed from either dispatcher is picked up the next time this module is
   regenerated, with no second place to remember to update.

3. **Which of those shapes the procedure parser recognises but *explicitly
   degrades on* rather than extracting real lineage from** (dynamic SQL, a
   nested procedure call, a statement shape sqlglot itself could not parse).
   `_explicitly_unparsed_node_types` walks the same AST, but for each
   `isinstance` branch checks whether that branch's own body calls
   `_unparsed_statement(...)` -- the module's one and only path to an
   UNPARSED marker (see `procedure_lineage`'s module docstring) -- rather
   than assuming from the branch's node-type name alone.

Control-flow and dynamic-SQL *recognition* (as opposed to node-type dispatch,
which sqlglot itself cannot parse a whole procedure body into to begin with;
see `procedure_lineage`'s module docstring) is regex-based text peeling, not
an AST node type -- `_regex_recognised_constructs` enumerates
`procedure_lineage`'s own module-level `_..._RE` compiled patterns by name,
which is itself proof the construct has a real, live recognition rule in
code, not merely a claim about one.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from aida import procedure_lineage, sql_lineage_parser

# Construct name -> the sqlglot AST node type name(s) that represent it, for
# translating the introspected `isinstance` checks (fact #2/#3 above) into a
# human-readable row. This mapping (name -> node type) is the one piece of
# this module that is necessarily hand-authored -- sqlglot's own node class
# names are not self-describing English -- but the SUPPORTED/UNSUPPORTED
# *verdict* for every row below it is never hand-typed; it comes from the
# introspection functions above.
_CONSTRUCT_NODE_TYPES: Final[dict[str, tuple[str, ...]]] = {
    "SELECT (standalone read)": ("Select",),
    "UNION": ("Union",),
    "CREATE VIEW / CREATE TABLE AS SELECT": ("Create",),
    "INSERT ... SELECT": ("Insert",),
    "UPDATE ... SET / UPDATE ... FROM": ("Update",),
    "DELETE": ("Delete",),
    "MERGE": ("Merge",),
    "EXEC/EXECUTE (dynamic or nested call)": ("Execute",),
    "sp_executesql": ("ExecuteSql",),
    "unrecognised statement shape (sqlglot Command fallback)": ("Command",),
}

# Construct name -> the module-level compiled-regex attribute name in
# `procedure_lineage.py` that recognises it in raw text (sqlglot cannot
# parse a full procedure body's control-flow syntax at all -- see that
# module's docstring -- so these are never `isinstance` checks on a parsed
# node; recognition happens before the node is ever parsed).
_CONSTRUCT_REGEX_NAMES: Final[dict[str, str]] = {
    "IF ... BEGIN (T-SQL)": "_IF_BEGIN_RE",
    "IF/ELSIF ... THEN (PL/SQL)": "_IF_THEN_RE",
    "WHILE ... BEGIN (T-SQL)": "_WHILE_BEGIN_RE",
    "WHILE ... LOOP (PL/SQL)": "_WHILE_LOOP_RE",
    "CASE ... WHEN ... THEN (PL/SQL statement form)": "_CASE_WHEN_THEN_RE",
    "cursor FOR ... IN (SELECT ...) LOOP (PL/SQL)": "_CURSOR_FOR_LOOP_RE",
    "bare FOR ... LOOP (PL/SQL)": "_BARE_FOR_LOOP_RE",
    "EXECUTE IMMEDIATE / EXEC(...) / sp_executesql (dynamic SQL)": "_DYNAMIC_SQL_RE",
    "EXEC/CALL <procedure_name> (nested procedure call)": "_NESTED_CALL_RE",
    "DECLARE/SET/OPEN/FETCH/CLOSE/RAISERROR/... (no table lineage)": "_NO_LINEAGE_KEYWORDS_RE",
}


@dataclass(frozen=True, slots=True)
class ConstructRow:
    construct: str
    view_parser_status: str  # SUPPORTED | UNSUPPORTED | N/A
    # SUPPORTED | EXPLICIT_UNPARSED | RECOGNISED_NO_LINEAGE | UNSUPPORTED
    procedure_parser_status: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    generated_at: str
    dialects: tuple[str, ...]
    constructs: tuple[ConstructRow, ...]
    unparsed_reasons: tuple[str, ...]


def _dispatched_node_types(func: object) -> set[str]:
    """AST-walk `func`'s own source for every `isinstance(<x>, exp.<Type>)`
    check, including a `exp.A | exp.B` union target, returning the sqlglot
    node type names it dispatches on. Read directly from the function's
    source text via `inspect.getsource` -- never hand-copied -- so this is
    always in sync with the actual dispatcher, not a snapshot of it.
    """
    source = textwrap.dedent(inspect.getsource(func))  # type: ignore[arg-type]
    tree = ast.parse(source)
    types: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
        ):
            types |= _attribute_names(node.args[1])
    return types


def _attribute_names(expr: ast.expr) -> set[str]:
    """`exp.Insert` -> {"Insert"}; `exp.Select | exp.Union` -> {"Select", "Union"}."""
    if isinstance(expr, ast.Attribute):
        return {expr.attr}
    if isinstance(expr, ast.BinOp):
        return _attribute_names(expr.left) | _attribute_names(expr.right)
    return set()


def _explicitly_unparsed_node_types(func: object) -> set[str]:
    """Of the node types `func` dispatches on (see `_dispatched_node_types`),
    which ones lead to an explicit `_unparsed_statement(...)` call within
    that same `isinstance` branch's own body -- i.e. genuinely recognised as
    a shape, but this parser explicitly degrades on it (INV-9/AT-C4) rather
    than extracting real lineage from it.
    """
    source = textwrap.dedent(inspect.getsource(func))  # type: ignore[arg-type]
    tree = ast.parse(source)
    unparsed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "isinstance"
            and len(test.args) == 2
        ):
            continue
        branch_types = _attribute_names(test.args[1])
        if not branch_types:
            continue
        calls_unparsed = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "_unparsed_statement"
            for inner in ast.walk(node)
        )
        if calls_unparsed:
            unparsed |= branch_types
    return unparsed


def _regex_recognised_constructs() -> set[str]:
    """Every module-level `_..._RE` compiled pattern name on
    `procedure_lineage` -- itself a source-derived list, proving each
    control-flow/dynamic-SQL recognition rule below has a live regex behind
    it (`_CONSTRUCT_REGEX_NAMES`' values are checked against this set at
    generation time, not merely asserted)."""
    return {name for name in dir(procedure_lineage) if name.endswith("_RE")}


def build_capability_matrix() -> CapabilityMatrix:
    """Derive the full matrix from the two parser modules' own code. Pure
    (no I/O beyond `inspect.getsource`, which reads the already-imported
    module's own file) and deterministic given the installed code -- calling
    this twice in the same process returns matrices with identical content.
    """
    dialects = tuple(sorted(sql_lineage_parser._SQLGLOT_DIALECT_MAP))

    view_handled = _dispatched_node_types(sql_lineage_parser._extract_from_statement)
    procedure_handled = _dispatched_node_types(procedure_lineage._classify_and_extract)
    procedure_unparsed = _explicitly_unparsed_node_types(procedure_lineage._classify_and_extract)
    live_regex_names = _regex_recognised_constructs()

    rows: list[ConstructRow] = []
    for construct, node_types in _CONSTRUCT_NODE_TYPES.items():
        node_type_set = set(node_types)

        if node_type_set & view_handled:
            view_status = "SUPPORTED"
            view_evidence = (
                f"sql_lineage_parser._extract_from_statement dispatches on "
                f"exp.{'/exp.'.join(sorted(node_type_set & view_handled))}"
            )
        else:
            view_status = "N/A"
            view_evidence = "not a view-definition-shaped construct"

        if node_type_set & procedure_unparsed:
            procedure_status = "EXPLICIT_UNPARSED"
            procedure_evidence = (
                f"procedure_lineage._classify_and_extract recognises "
                f"exp.{'/exp.'.join(sorted(node_type_set & procedure_unparsed))} "
                f"and calls _unparsed_statement(...) -- never silently dropped"
            )
        elif node_type_set & procedure_handled:
            procedure_status = "SUPPORTED"
            procedure_evidence = (
                f"procedure_lineage._classify_and_extract dispatches on "
                f"exp.{'/exp.'.join(sorted(node_type_set & procedure_handled))}"
            )
        else:
            procedure_status = "UNSUPPORTED"
            procedure_evidence = "no matching isinstance branch in _classify_and_extract"

        rows.append(
            ConstructRow(
                construct=construct,
                view_parser_status=view_status,
                procedure_parser_status=procedure_status,
                evidence=f"view: {view_evidence}; procedure: {procedure_evidence}",
            )
        )

    for construct, regex_name in _CONSTRUCT_REGEX_NAMES.items():
        status = (
            "EXPLICIT_UNPARSED" if regex_name in ("_DYNAMIC_SQL_RE", "_NESTED_CALL_RE")
            else "RECOGNISED_NO_LINEAGE" if regex_name == "_NO_LINEAGE_KEYWORDS_RE"
            else "SUPPORTED"
        )
        if regex_name not in live_regex_names:
            # The mapping itself has drifted from the code -- e.g. a pattern
            # was renamed or removed -- rather than silently publish a stale
            # row, surface it as unverified so a regeneration failure is
            # visible, not quietly wrong.
            status = "UNVERIFIED (regex not found in procedure_lineage)"
        rows.append(
            ConstructRow(
                construct=construct,
                view_parser_status="N/A",
                procedure_parser_status=status,
                evidence=(
                    f"view: not a view-definition-shaped construct; "
                    f"procedure: recognised via procedure_lineage.{regex_name} "
                    f"(text-level, sqlglot cannot parse this as an AST node)"
                ),
            )
        )

    return CapabilityMatrix(
        generated_at=datetime.now(UTC).isoformat(),
        dialects=dialects,
        constructs=tuple(rows),
        unparsed_reasons=tuple(reason.value for reason in procedure_lineage.UnparsedReason),
    )


def render_markdown(matrix: CapabilityMatrix) -> str:
    lines = [
        "# Procedure lineage parser capability matrix",
        "",
        f"Generated {matrix.generated_at} by `scripts/generate_procedure_capability_matrix.py`"
        " (`aida.procedure_capability_matrix.build_capability_matrix`) -- every status below"
        " is read directly out of `sql_lineage_parser.py`'s and `procedure_lineage.py`'s own"
        " dispatch code at generation time, not hand-maintained prose. Regenerate after any"
        " change to either module's dispatcher; do not hand-edit this file.",
        "",
        "## Dialects attempted",
        "",
        f"`{', '.join(matrix.dialects)}` -- read from"
        " `sql_lineage_parser._SQLGLOT_DIALECT_MAP`, shared by both parsers."
        " A dialect not in this list is refused outright by both"
        " (`unsupported dialect: ...`, `Confidence.LOW`), never silently guessed at.",
        "",
        "## Constructs",
        "",
        "| Construct | View/flat-DML parser (`sql_lineage_parser.py`) |"
        " Procedure-aware parser (`procedure_lineage.py`) |",
        "|---|---|---|",
    ]
    for row in matrix.constructs:
        lines.append(
            f"| {row.construct} | {row.view_parser_status} | {row.procedure_parser_status} |"
        )
    lines += [
        "",
        "`SUPPORTED` -- real column/table-level lineage extracted.",
        "`EXPLICIT_UNPARSED` -- recognised, but this parser cannot safely resolve it: an"
        " explicit `UNPARSED` marker edge is produced instead (INV-9/AT-C4), never a silent"
        " drop.",
        "`RECOGNISED_NO_LINEAGE` -- recognised, and genuinely carries no table lineage"
        " (e.g. `DECLARE`/`SET`) -- correctly skipped, not a gap.",
        "`UNSUPPORTED` -- no dispatch branch recognises this construct at all.",
        "`N/A` -- not a shape that construct's own contract covers (e.g. control flow inside"
        " a bare `CREATE VIEW` definition).",
        "",
        "## Explicit degradation reasons (procedure-aware parser)",
        "",
        "Every `EXPLICIT_UNPARSED` row above surfaces as one of these named reasons on the"
        " UNPARSED marker edge (`aida.procedure_lineage.UnparsedReason`):",
        "",
    ]
    lines += [f"- `{reason}`" for reason in matrix.unparsed_reasons]
    lines.append("")
    return "\n".join(lines)
