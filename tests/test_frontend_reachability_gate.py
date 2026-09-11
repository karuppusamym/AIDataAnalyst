"""The frontend half of the reachability review (REVIEW.md section 7).

`tests/test_reachability_gate.py` is the backend half: an AST import graph from
the five real backend entry points, failing on any unreachable `src/aida`
module. This is its `ui-next` counterpart, and it exists because the frontend
had no such gate -- which is how `ui-next/src/components/ProposalCard.tsx` sat
importable from nothing until a human review found it (D01).

The walker itself lives in `scripts/check_frontend_reachability.py` so it can
also run as a standalone, stdlib-only CI step without `uv sync` or `npm ci`
(same shape as `scripts/check_docs_links.py`). This module is the pytest face of
it plus the regression tests that pin the two properties the walker would
otherwise be easy to get quietly wrong:

1. Following `React.lazy(() => import("..."))`. Without it, all 45 lazily-loaded
   screens read as dead and the gate gets switched off within a day.
2. NOT following a statement-level `import type`. TypeScript erases it, so a
   module reached only that way ships in nothing -- and that is exactly what
   `ProposalCard.tsx` was: named by one `import type` in `lib/fixtures.ts` and by
   nothing else. `test_type_only_import_does_not_confer_reachability` is that
   case, reduced to a fixture tree so it stays true after the real file is gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from check_frontend_reachability import (  # noqa: E402
    ALLOWLIST,
    analyse,
    build_graph,
    discover_entry_points,
    specifiers_in,
)

UI_ROOT = REPO_ROOT / "ui-next"


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    """Materialize a miniature ui-next: index.html + vitest.config.ts + src."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        '<!doctype html><script type="module" src="/src/main.tsx"></script>',
        encoding="utf-8",
    )
    (root / "vitest.config.ts").write_text(
        'export default { test: { setupFiles: ["./src/test/setup.ts"] } };',
        encoding="utf-8",
    )
    (root / "src" / "test").mkdir(parents=True, exist_ok=True)
    (root / "src" / "test" / "setup.ts").write_text("export {};\n", encoding="utf-8")
    for rel, content in files.items():
        path = root / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_entry_points_are_derived_not_hardcoded() -> None:
    """A stale hand-written root would silently shrink the graph and make the
    whole gate a no-op. Both roots must come out of the files that declare them.
    """
    result = analyse(UI_ROOT)
    entries = result["entries"]
    assert isinstance(entries, dict)
    assert result["problems"] == [], result["problems"]
    assert "main.tsx" in entries, (
        "ui-next/index.html no longer declares a resolvable "
        '<script type="module" src=...>, so this gate has no browser entry point '
        "and would report an empty graph as healthy."
    )
    assert any("vitest" in why for why in entries.values()), (
        "ui-next/vitest.config.ts no longer names a resolvable setupFiles entry."
    )


def test_lazy_dynamic_imports_are_followed() -> None:
    """`App.tsx` reaches almost every screen only through
    `React.lazy(() => import("./screens/X"))`. If those edges were dropped, the
    unreachable set would be dozens of screens rather than a handful of files.
    """
    graph, _ = build_graph((UI_ROOT / "src").resolve())
    app_edges = graph["App.tsx"]
    lazy_screens = {edge for edge in app_edges if edge.startswith("screens/")}
    assert len(lazy_screens) > 20, (
        "App.tsx resolves only "
        f"{sorted(lazy_screens)} under screens/ -- the dynamic-import edges are "
        "not being followed, which would make this gate report every lazily "
        "loaded screen as dead."
    )
    result = analyse(UI_ROOT)
    unreachable = result["unreachable"]
    assert isinstance(unreachable, set)
    dead_screens = sorted(m for m in unreachable if m.startswith("screens/"))
    assert not dead_screens, f"screens reported unreachable: {dead_screens}"


def test_type_only_import_does_not_confer_reachability(tmp_path: Path) -> None:
    """The ProposalCard case, as a fixture.

    A component whose only non-test importer names it with a statement-level
    `import type` is NOT reachable: TypeScript erases the import, so the file is
    in no bundle. Counting that edge is what made the defect invisible; this test
    fails if anyone reinstates it.
    """
    root = _write_tree(
        tmp_path / "ui",
        {
            "main.tsx": 'import "./lib/fixtures";\n',
            "lib/fixtures.ts": (
                'import type { Proposal } from "../components/ProposalCard";\n'
                "export const p: Proposal | null = null;\n"
            ),
            "components/ProposalCard.tsx": (
                'import "./ProposalCard.css";\n'
                "export type Proposal = { id: string };\n"
                "export function ProposalCard() { return null; }\n"
            ),
            "components/ProposalCard.css": ".prop { color: red; }\n",
            "components/ProposalCard.test.tsx": (
                'import { ProposalCard } from "./ProposalCard";\n'
                "export default ProposalCard;\n"
            ),
        },
    )
    result = analyse(root)
    unreachable = result["unreachable"]
    assert isinstance(unreachable, set)
    assert "components/ProposalCard.tsx" in unreachable, (
        "the walker treated a statement-level `import type` as a runtime edge, so "
        "a component reached only by an erased type import reads as live -- the "
        "exact reason ProposalCard.tsx survived every automated check."
    )
    # And its stylesheet goes with it, since nothing live imports the component.
    assert "components/ProposalCard.css" in unreachable
    # The report must say *why* -- "nothing names this file" and "only an erased
    # type import names it" call for different fixes.
    type_only = result["type_only_importers"]
    assert isinstance(type_only, dict)
    assert type_only["components/ProposalCard.tsx"] == ["lib/fixtures.ts"]


def test_a_test_file_alone_does_not_make_a_module_reachable(tmp_path: Path) -> None:
    """"It has a passing test" is not reachability -- the whole premise of both
    halves of this gate.
    """
    root = _write_tree(
        tmp_path / "ui",
        {
            "main.tsx": "export {};\n",
            "components/Orphan.tsx": "export function Orphan() { return null; }\n",
            "components/Orphan.test.tsx": (
                'import { Orphan } from "./Orphan";\nexport default Orphan;\n'
            ),
        },
    )
    result = analyse(root)
    unreachable = result["unreachable"]
    assert isinstance(unreachable, set)
    assert "components/Orphan.tsx" in unreachable
    # The test file itself is exempt: vitest is a real entry point.
    assert "components/Orphan.test.tsx" not in unreachable


def test_comments_are_not_mistaken_for_imports(tmp_path: Path) -> None:
    """A commented-out import must not keep a dead module alive, and a URL
    containing `//` inside a string must not be mistaken for a comment (the
    naive strip-`//` approach corrupts every `"http://..."` in this codebase).
    """
    root = _write_tree(
        tmp_path / "ui",
        {
            "main.tsx": (
                'const base = "http://localhost:8000/v1";\n'
                '// import { Gone } from "./components/Gone";\n'
                '/* import { AlsoGone } from "./components/AlsoGone"; */\n'
                'import { Live } from "./components/Live";\n'
                "export { base, Live };\n"
            ),
            "components/Live.tsx": "export function Live() { return null; }\n",
            "components/Gone.tsx": "export function Gone() { return null; }\n",
            "components/AlsoGone.tsx": "export function AlsoGone() { return null; }\n",
        },
    )
    value, _ = specifiers_in(root / "src" / "main.tsx")
    assert value == {"./components/Live"}, value
    result = analyse(root)
    unreachable = result["unreachable"]
    assert isinstance(unreachable, set)
    assert {"components/Gone.tsx", "components/AlsoGone.tsx"} <= unreachable
    assert "components/Live.tsx" not in unreachable


def test_allowlist_has_no_stale_entries() -> None:
    """Same contract as the backend gate's allow-list: an entry for a deleted
    file is a dead reference, and an entry for a file somebody has since wired in
    hides a real fix and silently shrinks this gate's coverage.
    """
    result = analyse(UI_ROOT)
    stale = result["stale_allowlist"]
    assert isinstance(stale, list)
    assert not stale, "stale entries in check_frontend_reachability.ALLOWLIST:\n" + "\n".join(stale)


def test_every_allowlist_entry_carries_a_reason() -> None:
    """A bare path with no justification is how an allow-list becomes a way to
    hide the backlog instead of record it.
    """
    thin = [module for module, why in ALLOWLIST.items() if len(why.strip()) < 40]
    assert not thin, f"ALLOWLIST entries without a real justification: {thin}"


def test_all_ui_modules_reachable_or_allowlisted() -> None:
    """The gate."""
    result = analyse(UI_ROOT)
    unjustified = result["unjustified"]
    assert isinstance(unjustified, list)
    assert not unjustified, (
        "The following ui-next/src modules are imported from no entry point "
        "(index.html's module script, vitest's setup file) and are not on the "
        "ALLOWLIST in scripts/check_frontend_reachability.py:\n"
        + "\n".join(f"  - {m}" for m in unjustified)
        + "\n\nThis is the ProposalCard.tsx failure mode: a module its own test "
        "renders happily and no screen references. Import it from a live path, "
        "delete it, or -- only for a genuine, already-tracked item -- add it to "
        "ALLOWLIST with the row that owns it. Do not add it just to make this pass."
    )


def test_every_top_level_page_is_an_entry_point(tmp_path: Path) -> None:
    """A second Vite page (the Excel add-in's task pane) seeds the graph too.

    Without this, everything behind `excel-addin.html` would read as dead code
    the moment it shipped -- the ProposalCard failure in reverse, where the gate
    is wrong and a working module gets deleted to satisfy it.
    """
    ui = tmp_path / "ui"
    (ui / "src" / "addin").mkdir(parents=True)
    (ui / "index.html").write_text('<script type="module" src="/src/main.tsx"></script>')
    (ui / "src" / "main.tsx").write_text("export {};\n")
    (ui / "addin.html").write_text(
        '<script src="https://cdn.example/office.js"></script>'
        '<script type="module" src="/src/addin/main.tsx"></script>'
    )
    (ui / "src" / "addin" / "main.tsx").write_text("export {};\n")

    entries, problems = discover_entry_points(ui, (ui / "src").resolve())

    assert "addin/main.tsx" in entries
    assert "main.tsx" in entries
    assert problems == []


def test_a_page_naming_a_missing_script_is_a_problem(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    (ui / "src").mkdir(parents=True)
    (ui / "index.html").write_text('<script type="module" src="/src/main.tsx"></script>')
    (ui / "src" / "main.tsx").write_text("export {};\n")
    (ui / "addin.html").write_text('<script type="module" src="/src/addin/gone.tsx"></script>')

    _, problems = discover_entry_points(ui, (ui / "src").resolve())

    assert any("addin.html" in problem for problem in problems)
