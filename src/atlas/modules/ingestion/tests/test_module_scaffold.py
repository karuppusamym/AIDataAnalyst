"""Standalone self-check for the ingestion module scaffold.

Run in isolation with: `pytest src/atlas/modules/ingestion` (per the module
anatomy's "runs standalone" requirement in
`Docs/10-architecture/04-module-decomposition.md` Sec.7).
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

_PRIVATE_MODULES = ("models", "schemas", "repository", "service")


def test_public_surface_imports_cleanly() -> None:
    """The two public files (`api.py`, `contracts.py`) must import
    without error and without requiring anything from the rest of the
    codebase -- they are this module's entire surface to the outside
    world.
    """
    api = importlib.import_module("atlas.modules.ingestion.api")
    contracts = importlib.import_module("atlas.modules.ingestion.contracts")

    assert api is not None
    assert contracts is not None


def test_private_files_are_not_imported_by_the_public_surface() -> None:
    """A cheap, hand-rolled precursor to the `module-privacy`
    import-linter contract (tracker ST-02): the public files must not
    import the private ones, so nothing private leaks through
    `api.py`/`contracts.py` re-exports.
    """
    module_dir = Path(__file__).resolve().parent.parent
    for public_file in ("api.py", "contracts.py"):
        tree = ast.parse((module_dir / public_file).read_text(encoding="utf-8"))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.name.split(".")[-1])
        leaked = imported_names.intersection(_PRIVATE_MODULES)
        assert not leaked, (
            "public file imports private module internals: "
            + str(leaked)
        )
