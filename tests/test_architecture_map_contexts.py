"""R04 (review-2026-09-05) — the bounded-context list cannot drift.

`scripts/generate_architecture_map.py` groups every module under `src/` for the
generated map at `Docs/10-architecture/14-generated-architecture-map.md`. Its
`BOUNDED_CONTEXTS` tuple is hand-maintained, and `group_of()` falls back to the
`aida domain modules` group for anything it does not recognise.

That fallback is the trap this test exists for. When the profiling context was
relocated out of `aida.models`/`aida.schemas` in the R04 pass, the generator's
`--check` gate stayed green and the map regenerated cleanly — while reporting
the twelve brand-new `atlas.modules.profiling.*` modules as *aida domain
modules*, so the headline count for the monolith went **up** by twelve in the
same change that made it smaller. Nothing failed; the map was simply wrong, in
the direction that makes the refactor look like a regression.

Two directions are checked, because only one of them is the silent one:

* A directory under `src/atlas/modules/` that `BOUNDED_CONTEXTS` does not name.
  This is the silent failure: no error, wrong numbers.
* A name in `BOUNDED_CONTEXTS` with no directory. This one already fails loudly
  inside the generator (it globs a path that does not exist), but asserting it
  here gives the reader the reason rather than a stack trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES_ROOT = REPO_ROOT / "src" / "atlas" / "modules"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_architecture_map import BOUNDED_CONTEXTS  # noqa: E402


def _module_directories() -> set[str]:
    return {
        path.name
        for path in MODULES_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }


def test_every_module_directory_is_a_declared_bounded_context() -> None:
    missing = _module_directories() - set(BOUNDED_CONTEXTS)
    assert not missing, (
        "module directories exist that scripts/generate_architecture_map.py does not "
        f"know about: {sorted(missing)}. Add them to BOUNDED_CONTEXTS and regenerate "
        "the map, or their modules are silently counted as 'aida domain modules' — "
        "which reports a relocation as growth in the monolith."
    )


def test_every_declared_bounded_context_has_a_module_directory() -> None:
    stale = set(BOUNDED_CONTEXTS) - _module_directories()
    assert not stale, (
        "BOUNDED_CONTEXTS names contexts with no directory under src/atlas/modules/: "
        f"{sorted(stale)}."
    )


def test_each_context_has_a_domain_guide() -> None:
    """The generated map links every context to its guide by a derived path.

    A context added to `BOUNDED_CONTEXTS` without its guide produces a broken
    link in a *generated* file, which `scripts/check_docs_links.py` reports
    against a file nobody edited by hand.
    """
    guides = REPO_ROOT / "Docs" / "20-modules" / "domain-guides"
    missing = [
        name for name in BOUNDED_CONTEXTS if not (guides / f"{name.replace('_', '-')}.md").is_file()
    ]
    assert not missing, f"bounded contexts with no domain guide: {missing}"
