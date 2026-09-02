"""Scanning/parsing helpers for the event-catalog CI gate (tracker item TS-11).

Two independent readers feed the gate in ``test_event_catalog_gate.py``:

* :func:`scan_emitted_event_types` walks ``src/`` with the ``ast`` module and finds every
  ``event_type=`` keyword argument passed to :func:`aida.events.record_outbox` -- the single
  function that actually writes a domain event to the outbox (see ``src/aida/events.py``).
  Plain string literals and module-level ``str`` constants are resolved automatically. A
  keyword value that is a local variable is resolved too, by collecting every literal string
  assigned to that name earlier in the same function (this covers the common
  ``event_type = "foo.v1"`` / ``elif ...: event_type = "bar.v1"`` / ``record_outbox(...,
  event_type=event_type, ...)`` pattern). Anything left over -- an f-string with a non-literal
  part, a value built from another function's return value, an attribute lookup, etc. -- is
  reported as "not statically checkable" rather than silently dropped.

* :func:`parse_catalog_event_names` reads ``Docs/30-contracts/04-event-catalog.md`` and
  extracts every event name from the ``| Event | Trigger | Key payload |`` tables. A catalog
  row sometimes documents a family of events in one row using a shorthand, e.g.
  ``` `tool.drafted` / `.submitted` / `.published` / `.deprecated` ``` or
  ``` `glossary.term.approved.v1` / `.rejected.v1` ```. Each row's first backtick-quoted name is
  taken verbatim; each subsequent ``.suffix`` token is expanded against that first name with a
  trailing ``.vN`` version tag (if any) held aside so it lands back at the end -- e.g.
  ``glossary.term.approved.v1`` + ``.rejected.v1`` -> ``glossary.term.rejected.v1``. This
  matches every multi-name row actually used in the catalog; note in the module docstring (and
  in the gate's own comments) that this expansion is a best-effort reading of an inherently
  informal shorthand, not a guaranteed-exact parse of every conceivable row shape.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

RECORD_OUTBOX = "record_outbox"


@dataclass(frozen=True)
class UnresolvedSite:
    path: str
    lineno: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}:{self.lineno}  {self.detail}"


@dataclass
class ScanResult:
    literals: set[str] = field(default_factory=set)
    unresolved: list[UnresolvedSite] = field(default_factory=list)
    # Literal strings seen in a *partially*-literal branch of an otherwise-unresolvable
    # event_type= (e.g. one arm of an if/elif dispatch assigns a plain string, another arm
    # calls a helper function). Not safe to treat as "definitely emitted with nothing else
    # possible" the way `literals` is, but real enough that a value appearing here should not
    # be reported as unemitted/stale elsewhere.
    possible_literals: set[str] = field(default_factory=set)


def _is_record_outbox_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == RECORD_OUTBOX
    if isinstance(func, ast.Attribute):
        return func.attr == RECORD_OUTBOX
    return False


def _module_level_str_constants(tree: ast.Module) -> dict[str, str]:
    """True module-level (top-of-file, not inside any def/class) string constants only."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and isinstance(node.target, ast.Name):
                consts[node.target.id] = node.value.value
    return consts


def _find_enclosing_function(
    tree: ast.Module, call: ast.Call
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Smallest-range enclosing (Async)FunctionDef for `call`, by line-range containment."""
    target_line = call.lineno
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    best_span: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            lines = [n.lineno for n in ast.walk(node) if hasattr(n, "lineno")]
            if not lines:
                continue
            lo, hi = min(lines), max(lines)
            if lo <= target_line <= hi:
                span = hi - lo
                if best_span is None or span < best_span:
                    best, best_span = node, span
    return best


def _local_literal_values(
    func: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> tuple[set[str], bool]:
    """Every literal string assigned to `name` inside `func` (not descending into nested
    defs). Returns (values, fully_literal) -- fully_literal is False if some assignment to
    `name` in that same function is anything other than a string literal."""
    state = {"values": set(), "fully_literal": True}

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # don't descend
            pass

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # don't descend
            pass

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        state["values"].add(node.value.value)
                    else:
                        state["fully_literal"] = False
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.generic_visit(func)  # visit func's children directly; func itself is a
    # (Async)FunctionDef and the dispatch-based .visit() would short-circuit on it.
    return state["values"], state["fully_literal"]


def _resolve_keyword_value(
    value: ast.expr,
    mod_consts: dict[str, str],
    tree: ast.Module,
    call: ast.Call,
) -> tuple[set[str] | None, set[str], str | None]:
    """Returns (resolved literal values or None if not fully resolvable, "possible" literal
    values seen even when not fully resolvable, human description if unresolved/partial)."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}, {value.value}, None

    if isinstance(value, ast.Name):
        name = value.id
        if name in mod_consts:
            return {mod_consts[name]}, {mod_consts[name]}, None
        func = _find_enclosing_function(tree, call)
        if func is not None:
            values, fully_literal = _local_literal_values(func, name)
            if values and fully_literal:
                return values, values, None
            if values and not fully_literal:
                return (
                    None,
                    values,
                    f"event_type=Name('{name}') is set to a literal in some branches "
                    f"({sorted(values)}) but to a non-literal expression (e.g. another "
                    f"function's return value) in at least one other branch of the same "
                    f"function -- not fully statically checkable",
                )
        return (
            None,
            set(),
            f"event_type=Name('{name}') is neither a module-level constant nor a locally "
            f"assigned string literal -- not statically checkable",
        )

    if isinstance(value, ast.JoinedStr):
        parts: list[str] = []
        resolvable = True
        for piece in value.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif (
                isinstance(piece, ast.FormattedValue)
                and isinstance(piece.value, ast.Name)
                and piece.value.id in mod_consts
            ):
                parts.append(str(mod_consts[piece.value.id]))
            else:
                resolvable = False
        if resolvable:
            joined = "".join(parts)
            return {joined}, {joined}, None
        return None, set(), "event_type=f-string has a non-literal interpolated part"

    return None, set(), f"event_type= is an unresolvable expression: {ast.dump(value)[:120]}"


def scan_emitted_event_types(src_root: Path) -> ScanResult:
    """Walk every ``*.py`` file under `src_root` and collect the ``event_type=`` values passed
    to every ``record_outbox(...)`` call."""
    result = ScanResult()
    for pyfile in sorted(src_root.rglob("*.py")):
        text = pyfile.read_text(encoding="utf-8")
        if RECORD_OUTBOX + "(" not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(pyfile))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        mod_consts = _module_level_str_constants(tree)
        for node in ast.walk(tree):
            if not _is_record_outbox_call(node):
                continue
            for kw in node.keywords:
                if kw.arg != "event_type":
                    continue
                values, possible, detail = _resolve_keyword_value(kw.value, mod_consts, tree, node)
                if values:
                    result.literals |= values
                result.possible_literals |= possible
                if detail:
                    rel = pyfile.as_posix()
                    result.unresolved.append(UnresolvedSite(rel, node.lineno, detail))
    return result


_TABLE_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_VERSION_SUFFIX_RE = re.compile(r"\.v\d+$")


def _expand_event_family(cell: str) -> set[str]:
    """Expand one catalog row's first column (may name one event or a `/`-joined family)
    into the full set of literal event names it documents."""
    tokens = [t.strip() for t in cell.split("/")]
    names: set[str] = set()
    base_full: str | None = None
    for token in tokens:
        matches = _BACKTICK_RE.findall(token)
        if not matches:
            continue
        name = matches[0].strip()
        if not name:
            continue
        if name.startswith("."):
            if base_full is None:
                # A row that opens with a bare suffix is malformed; nothing sane to expand.
                continue
            stem = _VERSION_SUFFIX_RE.sub("", base_full)
            prefix = stem.rsplit(".", 1)[0] if "." in stem else stem
            names.add(prefix + name)
        else:
            if base_full is None:
                base_full = name
            names.add(name)
    return names


def parse_catalog_event_names(catalog_path: Path) -> set[str]:
    """Extract every documented event name from the catalog's `| Event | Trigger | ... |`
    tables (skips the header/separator rows and any table whose first column isn't `Event`)."""
    names: set[str] = set()
    in_event_table = False
    for line in catalog_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_event_table = False
            continue
        match = _TABLE_ROW_RE.match(stripped)
        if not match:
            continue
        first_col = match.group(1).strip()
        if first_col == "Event":
            in_event_table = True
            continue
        if set(first_col) <= {"-", " "}:
            # the `|---|---|---|` separator row directly under the header
            continue
        if not in_event_table:
            continue
        names |= _expand_event_family(first_col)
    return names
