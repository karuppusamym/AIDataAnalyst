#!/usr/bin/env python3
"""Frontend import-reachability gate -- the `ui-next` counterpart of
`tests/test_reachability_gate.py`.

`Docs/review-2026-09-05/REVIEW.md` section 7 asks for a reachability review
"across both namespaces". The backend half exists: `tests/test_reachability_gate.py`
walks an AST import graph from the five real backend entry points and fails on any
`src/aida` module reachable from none of them. The frontend half did not, and that
is exactly how `ui-next/src/components/ProposalCard.tsx` sat importable from
nothing -- fully rendered by its own test file, referenced by no screen -- long
enough for the review to be the thing that found it (D01).

This script is that missing half. It builds a static import graph over
``ui-next/src`` and fails when a module there is reachable from none of the
application's real entry points.

What counts as an edge
----------------------
* ``import ... from "./x"``, ``import "./x.css"``, ``export ... from "./x"``
  -- static value imports.
* ``import("./x")`` -- the dynamic form. This is not optional here: `App.tsx`
  reaches **every screen but two** through
  ``React.lazy(() => import("./screens/..."))``, so a walker that followed only
  static imports would report all 45 lazy screens as dead and be immediately
  switched off.
* ``@import "./x.css"`` inside a stylesheet.

A statement-level ``import type`` / ``export type`` is tracked **separately and
does not make its target reachable**, because TypeScript erases it: no such edge
survives into the bundle, so a module reached only that way ships in nothing.
This is not a detail. `ProposalCard.tsx` was reached by exactly one importer --
``import type { Proposal } from "../components/ProposalCard"`` in
`lib/fixtures.ts` -- and a walker that counted type-only edges as reachability
reports it as healthy, which is how D01 describes it ("types reachable only from
a fixture with no consumer"). Verified: with type-only edges counted, this script
passes on the pre-fix tree; with them separated, it names `ProposalCard.tsx`.
Inline type modifiers (``import { type Foo, bar } from "./x"``) are value
imports -- the statement still emits an import -- and are treated as such.

Only relative specifiers are resolved. A bare specifier (``react``,
``@tanstack/react-virtual``) is an npm package, not a file in this tree, and is
ignored -- dependency risk is `scripts/check_npm_audit.py`'s job, not this one.

What counts as an entry point
-----------------------------
Derived from the files that declare them, never hand-listed:

* ``ui-next/index.html``'s ``<script type="module" src="...">`` -- the browser's
  only way in, and therefore the application's real root.
* ``ui-next/vitest.config.ts``'s ``setupFiles`` -- loaded by the test runner
  before any test module.

A stale hand-written root would silently shrink the graph and make the whole
gate a no-op, so both are parsed out of their own configuration files and
``--check`` fails loudly if either yields nothing.

Test files are deliberately NOT seeds
-------------------------------------
``*.test.ts`` / ``*.test.tsx`` are entry points of the *vitest* process, so they
are exempt from having to be reachable themselves. But their imports do not make
anything else reachable, because "reached only by its own test" is precisely the
condition this gate exists to detect. That single rule is what would have caught
`ProposalCard.tsx`: it had a test, and nothing else.

Usage
-----
    python scripts/check_frontend_reachability.py           # report
    python scripts/check_frontend_reachability.py --check   # fail CI on a gap
    python scripts/check_frontend_reachability.py --root DIR  # walk another tree

Standard library only, so it runs in CI without `npm ci` and without adding a
dependency to either side of the repository.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UI_ROOT = REPO_ROOT / "ui-next"

# Files that participate in the graph at all. `.css` is included because the
# stylesheets are real modules here -- they are pulled in by `import "./x.css"`
# and bundled by Vite -- and because D01's finding was half a CSS finding: the
# `ProposalCard` component was dead while its stylesheet was live.
MODULE_SUFFIXES = (".ts", ".tsx", ".css")

# Resolution order for a relative specifier without an explicit extension,
# matching what Vite/TypeScript actually do for this project.
RESOLUTION_CANDIDATES = (
    "{spec}",
    "{spec}.ts",
    "{spec}.tsx",
    "{spec}.css",
    "{spec}/index.ts",
    "{spec}/index.tsx",
)

TEST_SUFFIXES = (".test.ts", ".test.tsx")

# --- Allow-list -------------------------------------------------------------
#
# Same contract as `tests/test_reachability_gate.py`'s ALLOWLIST: an entry is a
# claim, re-checked on every run (see `stale_allowlist_entries`), not a way to
# silence the gate. Do NOT add a module here to make the check pass -- import it
# from a live path, or delete it. Paths are POSIX-relative to `ui-next/src`.
# Every entry was verified against this tree on 2026-09-06, not inherited from a
# previous revision of the list.
ALLOWLIST: dict[str, str] = {
    "vite-env.d.ts": (
        "Type-only: ambient declarations for Vite's client types. Included by "
        "tsconfig and imported by no module -- being unimported is what an ambient "
        ".d.ts is for, so this is a permanent exception, not a backlog item."
    ),
    "components/OrgPicker.tsx": (
        "Docs/review-2026-09-05/POINTS-TRACKER.md D01: named by the review "
        "(REVIEW.md line 301) alongside ProposalCard as unreached from main. Unlike "
        "ProposalCard it was deliberately KEPT -- the shell's active org control is "
        "`ScopePicker`, and `AdministrationScreen` documents `OrgPicker` as the "
        "shell-nav org control this screen deliberately does not duplicate. It is a "
        "decision with an owner (D01), recorded here rather than hidden: wire it "
        "into the shell nav or delete it, and remove this entry either way."
    ),
    "lib/_fixtures_append.ts": (
        "Zero-byte file left behind by the R05 frontend API-client split "
        "(POINTS-TRACKER.md section 3, R05 = partial). Its three siblings "
        "(`_api_append.ts`, `_column_documentation_api.ts`, `_cross_source_api.ts`) "
        "all have live importers; this one has none and no content. Deleting an "
        "empty file is R05's call, not this gate's."
    ),
}


def _blank_comments(source: str) -> str:
    """Replace comment bodies with spaces, leaving string literals intact.

    A regex that simply strips ``//...`` would corrupt every ``"http://..."`` in
    the file and change which specifiers are found, so this is a real (small)
    scanner over the four states that matter: code, line comment, block comment,
    and inside a quoted or template string. Line count and offsets are preserved
    so error messages stay honest.
    """
    out: list[str] = []
    i, n = 0, len(source)
    quote: str | None = None
    while i < n:
        ch = source[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if ch in "'\"`":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


# `import ... from "x"` / `export ... from "x"`. The middle is `[^'";]*?` rather
# than `[\s\S]*?` so a match can never span a statement boundary or swallow an
# unrelated string literal; newlines are allowed, so multi-line named-import
# lists (which this codebase uses heavily) are matched. Group 1 is the
# statement-level `type` keyword when present -- that and only that is the
# erased, non-runtime form.
IMPORT_FROM = re.compile(
    r"\b(?:import|export)\s+(type\s+)?[^'\";]*?\bfrom\s*['\"]([^'\"]+)['\"]"
)
# Side-effect import: `import "./tokens.css"`.
IMPORT_BARE = re.compile(r"\bimport\s+['\"]([^'\"]+)['\"]")
# `React.lazy(() => import("./screens/X"))` and any other dynamic import.
IMPORT_DYNAMIC = re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]")
# Stylesheet imports.
CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)['\"]")
# `<script type="module" src="/src/main.tsx">` in index.html.
HTML_MODULE_SCRIPT = re.compile(
    r"<script[^>]*\btype\s*=\s*['\"]module['\"][^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]"
)
# `setupFiles: ["./src/test/setup.ts"]` in vitest.config.ts.
VITEST_SETUP_FILES = re.compile(r"setupFiles\s*:\s*\[([^\]]*)\]")
QUOTED = re.compile(r"['\"]([^'\"]+)['\"]")


def specifiers_in(path: Path) -> tuple[set[str], set[str]]:
    """(value specifiers, type-only specifiers) a single file references.

    A specifier imported both ways somewhere in the same file counts as a value
    import -- one surviving edge is enough to ship the module.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".css":
        return set(CSS_IMPORT.findall(text)), set()
    code = _blank_comments(text)
    value: set[str] = set()
    type_only: set[str] = set()
    for type_keyword, spec in IMPORT_FROM.findall(code):
        (type_only if type_keyword else value).add(spec)
    value.update(IMPORT_BARE.findall(code))
    value.update(IMPORT_DYNAMIC.findall(code))
    return value, type_only - value


def _iter_modules(src_root: Path) -> list[Path]:
    return sorted(p for p in src_root.rglob("*") if p.is_file() and p.suffix in MODULE_SUFFIXES)


def _resolve(spec: str, importer: Path, src_root: Path) -> Path | None:
    """Resolve a relative specifier to a file under `src_root`, or None."""
    # Vite query suffixes (`?raw`, `?url`, `?worker`) name the same file.
    spec = spec.split("?", 1)[0].split("#", 1)[0]
    if not spec.startswith("."):
        return None  # bare specifier: an npm package, not this tree
    base = (importer.parent / spec).resolve()
    for template in RESOLUTION_CANDIDATES:
        candidate = Path(template.format(spec=str(base)))
        if candidate.is_file() and candidate.suffix in MODULE_SUFFIXES:
            try:
                candidate.relative_to(src_root)
            except ValueError:
                return None
            return candidate
    return None


def build_graph(src_root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(runtime graph, type-only graph), keyed by POSIX path relative to src_root.

    The runtime graph is what reachability is computed over. The type-only graph
    exists so the report can distinguish "nothing names this file at all" from
    "only an erased type import names it" -- two different conversations, and the
    second one is the ProposalCard shape.
    """
    graph: dict[str, set[str]] = {}
    type_graph: dict[str, set[str]] = {}
    for path in _iter_modules(src_root):
        key = path.relative_to(src_root).as_posix()
        value_specs, type_specs = specifiers_in(path)
        for specs, target_graph in ((value_specs, graph), (type_specs, type_graph)):
            edges: set[str] = set()
            for spec in specs:
                target = _resolve(spec, path, src_root)
                if target is not None:
                    edges.add(target.relative_to(src_root).as_posix())
            target_graph[key] = edges
    return graph, type_graph


def is_test_module(rel: str) -> bool:
    return rel.endswith(TEST_SUFFIXES)


def discover_entry_points(ui_root: Path, src_root: Path) -> tuple[dict[str, str], list[str]]:
    """Entry points, read out of the files that declare them.

    Returns (entry -> why, problems). A `problems` entry means a declaration file
    was present but named nothing this walker could resolve, which would silently
    shrink the graph -- the caller turns that into a failure rather than walking a
    smaller graph and reporting a clean result.
    """
    entries: dict[str, str] = {}
    problems: list[str] = []

    index_html = ui_root / "index.html"
    if not index_html.is_file():
        problems.append(f"{index_html} is missing; the browser entry point cannot be derived.")
    else:
        srcs = HTML_MODULE_SCRIPT.findall(index_html.read_text(encoding="utf-8"))
        resolved = 0
        for src in srcs:
            # index.html uses a root-absolute URL (`/src/main.tsx`).
            candidate = (ui_root / src.lstrip("/")).resolve()
            if candidate.is_file():
                entries[candidate.relative_to(src_root).as_posix()] = (
                    "browser entry point (index.html <script type=module>)"
                )
                resolved += 1
        if not resolved:
            problems.append(
                f"{index_html} declares no resolvable <script type=\"module\" src=...>; "
                "without it this gate would walk an empty graph."
            )

    vitest_config = ui_root / "vitest.config.ts"
    if vitest_config.is_file():
        match = VITEST_SETUP_FILES.search(vitest_config.read_text(encoding="utf-8"))
        if match:
            for spec in QUOTED.findall(match.group(1)):
                candidate = (ui_root / spec.lstrip("./")).resolve()
                if candidate.is_file():
                    entries[candidate.relative_to(src_root).as_posix()] = (
                        "test-runner entry point (vitest.config.ts setupFiles)"
                    )

    return entries, problems


def reachable_from(graph: dict[str, set[str]], seeds: Iterable[str]) -> set[str]:
    seen = set(seeds)
    stack = list(seen)
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def analyse(ui_root: Path) -> dict[str, object]:
    src_root = (ui_root / "src").resolve()
    graph, type_graph = build_graph(src_root)
    entries, problems = discover_entry_points(ui_root, src_root)
    reachable = reachable_from(graph, entries)
    unreachable = {m for m in graph if m not in reachable and not is_test_module(m)}
    # Modules whose only importer is an erased `import type`. Called out
    # separately because the fix differs: the file ships in nothing, but a live
    # module does still name its types, so "delete it" is not the whole answer.
    type_only_importers: dict[str, list[str]] = {}
    for module in sorted(unreachable):
        importers = sorted(src for src, edges in type_graph.items() if module in edges)
        if importers:
            type_only_importers[module] = importers
    return {
        "src_root": src_root,
        "graph": graph,
        "type_graph": type_graph,
        "entries": entries,
        "problems": problems,
        "reachable": reachable,
        "unreachable": unreachable,
        "type_only_importers": type_only_importers,
        "tests": {m for m in graph if is_test_module(m)},
        "unjustified": sorted(unreachable - set(ALLOWLIST)),
        "stale_allowlist": stale_allowlist_entries(graph, unreachable),
    }


def stale_allowlist_entries(graph: dict[str, str | set[str]], unreachable: set[str]) -> list[str]:
    """Allow-list entries that no longer describe reality.

    Same reasoning as the backend gate's `test_allowlist_has_no_stale_entries`:
    an entry for a deleted file is a dead reference, and an entry for a file
    somebody has since wired in hides a real fix and shrinks this gate's
    effective coverage.
    """
    stale: list[str] = []
    for module in sorted(ALLOWLIST):
        if module not in graph:
            stale.append(f"{module}: no such file under ui-next/src (delete the entry)")
        elif module not in unreachable:
            stale.append(f"{module}: now reachable from an entry point (delete the entry)")
    return stale


def _report(result: dict[str, object]) -> str:
    graph = result["graph"]
    assert isinstance(graph, dict)
    entries = result["entries"]
    assert isinstance(entries, dict)
    unreachable = result["unreachable"]
    assert isinstance(unreachable, set)
    tests = result["tests"]
    assert isinstance(tests, set)
    type_only = result["type_only_importers"]
    assert isinstance(type_only, dict)
    lines = [
        f"modules under ui-next/src: {len(graph)} "
        f"({len(graph) - len(tests)} application, {len(tests)} test)",
        "entry points: " + ", ".join(f"{k} ({v})" for k, v in sorted(entries.items())),
        f"reachable: {len(result['reachable'])}",  # type: ignore[arg-type]
        f"unreachable (excluding test files): {len(unreachable)}",
    ]
    for module in sorted(unreachable):
        reason = ALLOWLIST.get(module)
        lines.append(f"  - {module}" + (f"  [allow-listed] {reason}" if reason else "  [FAIL]"))
        if module in type_only:
            lines.append(
                "      named only by an erased `import type` in: "
                + ", ".join(type_only[module])
                + " -- the file ships in nothing, but its types are still referenced."
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_UI_ROOT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero on an unreachable, non-allow-listed module or a stale entry.",
    )
    args = parser.parse_args()

    result = analyse(args.root.resolve())
    print(_report(result))

    problems = result["problems"]
    assert isinstance(problems, list)
    unjustified = result["unjustified"]
    assert isinstance(unjustified, list)
    stale = result["stale_allowlist"]
    assert isinstance(stale, list)

    failed = False
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
        failed = True
    if unjustified:
        print(
            "\nERROR: the following ui-next/src modules are imported from no entry point "
            "and are not allow-listed:\n"
            + "\n".join(f"  - {m}" for m in unjustified)
            + "\n\nThis is the ProposalCard.tsx failure mode (review D06/D01): a module its "
            "own test renders happily and no screen references. Import it from a live path, "
            "delete it, or -- only for a genuine, already-tracked item -- add it to "
            "ALLOWLIST in this file with the tracker row that owns it.",
            file=sys.stderr,
        )
        failed = True
    if stale:
        print(
            "\nERROR: stale ALLOWLIST entries in scripts/check_frontend_reachability.py:\n"
            + "\n".join(f"  - {s}" for s in stale),
            file=sys.stderr,
        )
        failed = True

    if not args.check:
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
