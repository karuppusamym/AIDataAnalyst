#!/usr/bin/env python3
"""Generate the compatibility-shim register from the source tree.

Review 2026-09-05, D03: *"Retain compatibility shims until supported callers
migrate. DB/config/models/schema re-exports and moved API aliases are intentional
seams recognized by import contracts. Assign replacement paths, owners, caller
counts, and removal conditions. Do not remove them solely because they look
redundant."*

The first remediation pass removed no shim, which was right, but produced no
register, which was the actual ask. This script is that register. It deletes
nothing and changes no import; it only measures.

Three rules shape it.

**Caller counts are measured, never typed.** A hand-counted number is stale the
next day, and a stale count is worse than none: it is the evidence somebody will
use to decide a shim is safe to delete. Every count below comes from an `ast`
walk of the repository at generation time (`--check` re-runs it and fails when
the committed file has drifted).

**A zero count is not permission to delete.** The review's own warning. A static
scan cannot see a dynamic import, a `monkeypatch.setattr("aida.db.engine", ...)`
string target, an out-of-repo consumer, or a pickle whose payload names the old
module path. Zero-caller shims are reported in their own section, marked, and
kept -- the removal condition column, not the count column, is what authorises a
removal.

**Judgement columns are hand-written and labelled as such.** "Replacement path",
"owner area" and "removal condition" are decisions; they live in `SHIMS` below
and the generated document says which columns came from where.

What the scan can and cannot see, stated plainly:

* Counted: `import aida.db`, `from aida.db import x`, and `from aida.db.sub
  import x`, in every `.py` file under the scanned roots.
* Counted separately: string constants that start with the shim's module path
  (`importlib.import_module("aida.db")`, `monkeypatch.setattr("aida.db.engine",
  ...)`, a `mock.patch` target). These are real callers that an import-only scan
  misses, so they are reported in their own column rather than folded in.
* Not counted at all: anything outside this repository, a `getattr` on an
  already-imported package object, and a module path stored as data (a pickle,
  a database row, a config file that is not scanned here).
* For the two *partial* shims (`aida.models`, `aida.schemas` -- files with
  thousands of lines of their own content plus a re-export block), a file counts
  only if it imports at least one of the re-exported names. Importing
  `aida.models.QueryExecution`, which still genuinely lives in that file, is not
  use of the shim.

Discovery, so the register cannot silently go stale in the other direction: the
script also *finds* shim-shaped files (a module docstring announcing a
backward-compatible re-export; a TypeScript file whose whole body is a comment
plus `export * from`) and fails `--check` if one exists that the register does
not list.

Standard library only, so it runs in the dependency-free `docs` CI job.

Usage
-----
    python scripts/shim_register.py            # write the register
    python scripts/shim_register.py --check    # fail if stale or undeclared
    python scripts/shim_register.py --stdout   # print only
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "Docs" / "40-engineering" / "09-compatibility-shim-register.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Every root a Python caller of a shim can live in. `migrations/` is included
# because `migrations/env.py` imports `aida.db` and `aida.config` -- Alembic is a
# caller like any other, and forgetting it is exactly how a "zero callers" claim
# gets made about a module the migration environment cannot start without.
PY_CALLER_ROOTS = ("src", "tests", "scripts", "sdk", "migrations")
TS_CALLER_ROOT = "ui-next/src"

# Directories never scanned for callers.
SKIP_DIR_NAMES = frozenset({"__pycache__", "node_modules", "dist", "build", ".venv"})


# ---------------------------------------------------------------------------
# The register's hand-written half
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shim:
    """One compatibility seam.

    `module`/`ts_path`, `replacement`, `owner` and `removal_condition` are
    hand-written; everything measured about the shim is computed and lives on
    `Measured` below.
    """

    key: str
    kind: str  # "python", "python-partial", "typescript"
    path: str  # repository-relative file path
    module: str  # dotted module path (python) or import specifier stem (ts)
    replacement: str  # short enough for a table cell; markdown, not plain text
    replacement_detail: str  # the full answer, for the per-shim section
    owner: str
    introduced: str
    removal_condition: str
    note: str = ""


SHIMS: tuple[Shim, ...] = (
    # --- Phase 1 (tracker ST-04): platform infrastructure moved to atlas.platform ---
    Shim(
        key="aida.db",
        kind="python",
        path="src/aida/db.py",
        module="aida.db",
        replacement="`atlas.platform.db`",
        replacement_detail="`atlas.platform.db` — the same objects, moved, not copied.",
        owner="Platform infrastructure",
        introduced="ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Import callers and string references both reach zero, **and** "
            "`migrations/env.py` imports `Base` from the canonical module instead. "
            "Alembic's environment is the caller most easily forgotten: it is not "
            "under `src/`, and breaking it breaks every migration rather than a test."
        ),
        note=(
            "Lazy `__getattr__` for `engine`/`session_factory`/`settings`, so importing "
            "the shim does not construct an engine. A rewrite of the shim must keep that."
        ),
    ),
    Shim(
        key="aida.config",
        kind="python",
        path="src/aida/config.py",
        module="aida.config",
        replacement="`atlas.platform.config`",
        replacement_detail="`atlas.platform.config`.",
        owner="Platform infrastructure",
        introduced="ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Import callers and string references both reach zero, **and** "
            "`migrations/env.py` reads settings from the canonical module. `Settings` is "
            "also the type annotation on FastAPI dependency callables, so a caller count "
            "here undercounts nothing only because those callers import the name."
        ),
    ),
    Shim(
        key="aida.context",
        kind="python",
        path="src/aida/context.py",
        module="aida.context",
        replacement="`atlas.platform.context`",
        replacement_detail="`atlas.platform.context`.",
        owner="Platform infrastructure",
        introduced="ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Import callers and string references both reach zero. The `ContextVar` "
            "identity matters: correlation ids set through one import path must be "
            "visible through the other, which they are because the shim re-exports the "
            "same object rather than defining a second one. Any migration must move "
            "callers, never copy the variable."
        ),
    ),
    Shim(
        key="aida.logging",
        kind="python",
        path="src/aida/logging.py",
        module="aida.logging",
        replacement="`atlas.platform.logging`",
        replacement_detail="`atlas.platform.logging`.",
        owner="Platform infrastructure",
        introduced="ST-04, Phase 1 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Import callers and string references both reach zero. `configure_logging` is "
            "called once per process from each of the five entry points, so this shim "
            "cannot go before every entry point has moved."
        ),
    ),
    # --- Phase 3 (tracker ST-05): model / schema re-exports ---
    Shim(
        key="aida.models",
        kind="python-partial",
        path="src/aida/models.py",
        module="aida.models",
        replacement="`atlas.modules.<context>.models` (re-exported classes only)",
        replacement_detail=(
            "Each re-exported class has moved to the `models` module of the bounded "
            "context that owns it; import it from there. The rest of the file — the "
            "large majority of it -- has not moved and has no replacement path yet."
        ),
        owner="Bounded contexts (catalog, connectivity, identity_tenancy, ingestion, "
        "observability_audit, profiling) jointly",
        introduced="ST-05, Phase 3 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "**Not removable as a file at all** until every remaining class in it has "
            "moved to a context -- it is a partial shim, not a shim. The re-export "
            "*block* can go when no caller imports a re-exported name and the "
            "`aida.models` entry disappears from every `allowed_importers` list in "
            "`pyproject.toml`. Note that `Base.metadata` must keep seeing all of these "
            "classes for Alembic autogenerate to be correct, so removing the block "
            "requires `migrations/env.py` to import the context model modules directly."
        ),
        note=(
            "Named in the `allowed_importers` list of all six module-privacy contracts, "
            "which is what lets the shim import the private module it re-exports from."
        ),
    ),
    Shim(
        key="aida.schemas",
        kind="python-partial",
        path="src/aida/schemas.py",
        module="aida.schemas",
        replacement="`atlas.modules.<context>.schemas` (re-exported DTOs only)",
        replacement_detail=(
            "Each re-exported DTO has moved to the `schemas` module of the bounded "
            "context that owns it. The rest of the file has not moved."
        ),
        owner="Bounded contexts (catalog, connectivity, identity_tenancy, ingestion, "
        "observability_audit, profiling) jointly",
        introduced="ST-05, Phase 3 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Same shape as `aida.models`, plus one hard constraint: the moved DTO "
            "modules import `ApiModel` back from this file, so the re-export block "
            "cannot be removed before `ApiModel` moves somewhere neither side owns. "
            "The circular import resolves today only because the block sits below "
            "`ApiModel`'s definition."
        ),
    ),
    # --- Phase 5 (tracker ST-07 Commits A/B): catalog service moves ---
    Shim(
        key="aida.catalog_read_model",
        kind="python",
        path="src/aida/catalog_read_model.py",
        module="aida.catalog_read_model",
        replacement="`atlas.modules.catalog.service` / `.repository`",
        replacement_detail=(
            "`compose_catalog_rows` moved to `atlas.modules.catalog.service`; the "
            "underscore-prefixed batch helpers moved to "
            "`atlas.modules.catalog.repository`, where they are still private."
        ),
        owner="Bounded context: catalog",
        introduced="ST-07 Commit A, Phase 5 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Blocked on a decision, not on a count. Four `aida` modules import the "
            "underscore-prefixed helpers, which are private in the canonical location "
            "too -- so moving those callers to the canonical path would only relocate a "
            "private-name dependency, not remove it. The shim can go once those helpers "
            "are promoted to named functions on `atlas.modules.catalog.api` and the "
            "callers move to that public surface. Until then the shim is the boundary."
        ),
    ),
    Shim(
        key="aida.catalog_bulk_actions",
        kind="python",
        path="src/aida/catalog_bulk_actions.py",
        module="aida.catalog_bulk_actions",
        replacement="`atlas.modules.catalog.service`",
        replacement_detail=(
            "The bulk-action constants, DTOs and per-item apply functions all live in "
            "the \"Bulk actions\" section of `atlas.modules.catalog.service`."
        ),
        owner="Bounded context: catalog",
        introduced="ST-07 Commit B, Phase 5 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "The bulk endpoints that dispatch to these functions are scheduled to move "
            "into `atlas.modules.catalog.router` under the rest of ST-07. This shim can "
            "go once they have, and once `aida.schemas` no longer imports "
            "`ALLOWED_CLASSIFICATIONS` from it -- that import is what puts the shim on "
            "the transitive path of nearly every module in the tree."
        ),
    ),
    # --- Phase 5 (tracker ST-07 Commit C): moved router modules ---
    Shim(
        key="aida.workspace_api",
        kind="python",
        path="src/aida/workspace_api.py",
        module="aida.workspace_api",
        replacement="`atlas.modules.identity_tenancy.router`",
        replacement_detail=(
            "Handlers live in `atlas.modules.identity_tenancy.router`. The router "
            "*object* should be taken from that module's `api` face instead, which is "
            "what the module-privacy contract expects an app-assembly file to import."
        ),
        owner="Bounded context: identity_tenancy",
        introduced="ST-07 Commit C, Phase 5 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "`aida.main` mounts the router through this path. It can go once `main.py` "
            "imports the router from `atlas.modules.identity_tenancy.api` (the module's "
            "public face -- not `.router`, which the module-privacy contract protects) "
            "the way it already does for catalog and connectivity, and once no test "
            "imports a handler function from here."
        ),
        note=(
            "Re-exports every handler, not only the ones with callers, so a future test "
            "that wants to bypass HTTP does not have to change import paths first."
        ),
    ),
    Shim(
        key="aida.ingestion_api",
        kind="python",
        path="src/aida/ingestion_api.py",
        module="aida.ingestion_api",
        replacement="`atlas.modules.ingestion.router`",
        replacement_detail=(
            "Handlers live in `atlas.modules.ingestion.router`; the router object "
            "should come from that module's `api` face."
        ),
        owner="Bounded context: ingestion",
        introduced="ST-07 Commit C, Phase 5 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Same as `aida.workspace_api`: `main.py` mounts through it, and "
            "`tests/test_in2_batch_controls.py` imports four batch-control handlers "
            "from it directly to test transitions without HTTP. Both have to move."
        ),
    ),
    Shim(
        key="aida.observability_api",
        kind="python",
        path="src/aida/observability_api.py",
        module="aida.observability_api",
        replacement="`atlas.modules.observability_audit.router`",
        replacement_detail=(
            "Handlers live in `atlas.modules.observability_audit.router`; the router "
            "object should come from that module's `api` face."
        ),
        owner="Bounded context: observability_audit",
        introduced="ST-07 Commit C, Phase 5 of Docs/40-engineering/06-refactor-plan.md",
        removal_condition=(
            "Same as `aida.workspace_api`: `main.py` mounts through it, and two tests "
            "import handler functions from it directly. Both have to move."
        ),
    ),
    # --- ui-next (review 2026-09-05, R05): api.ts split into lib/api/* ---
    Shim(
        key="_api_append.ts",
        kind="typescript",
        path="ui-next/src/lib/_api_append.ts",
        module="./api/glossary",
        replacement="`ui-next/src/lib/api/glossary.ts`",
        replacement_detail=(
            "`ui-next/src/lib/api/glossary.ts`, or `ui-next/src/lib/api.ts`, which "
            "re-exports it and is the intended import surface for screens."
        ),
        owner="Experience shell (ui-next)",
        introduced="R05, Docs/review-2026-09-05",
        removal_condition=(
            "Zero importers of the `_api_append` specifier remain -- screens and their "
            "tests import it directly today. Unlike the Python shims this one has no "
            "out-of-repo consumer and no dynamic-import path, so for the frontend a "
            "measured zero is close to sufficient; the bundler resolves specifiers "
            "statically and `tsc --noEmit` fails on a missing one."
        ),
    ),
    Shim(
        key="_cross_source_api.ts",
        kind="typescript",
        path="ui-next/src/lib/_cross_source_api.ts",
        module="./api/crossSource",
        replacement="`ui-next/src/lib/api/crossSource.ts`",
        replacement_detail=(
            "`ui-next/src/lib/api/crossSource.ts`, or the `ui-next/src/lib/api.ts` "
            "barrel that re-exports it."
        ),
        owner="Experience shell (ui-next)",
        introduced="R05, Docs/review-2026-09-05",
        removal_condition="Same as `_api_append.ts`: zero importers of the specifier.",
    ),
    Shim(
        key="_column_documentation_api.ts",
        kind="typescript",
        path="ui-next/src/lib/_column_documentation_api.ts",
        module="./api/columnDocumentation",
        replacement="`ui-next/src/lib/api/columnDocumentation.ts`",
        replacement_detail=(
            "`ui-next/src/lib/api/columnDocumentation.ts`, or the "
            "`ui-next/src/lib/api.ts` barrel that re-exports it."
        ),
        owner="Experience shell (ui-next)",
        introduced="R05, Docs/review-2026-09-05",
        removal_condition="Same as `_api_append.ts`: zero importers of the specifier.",
    ),
)

# Files that look shim-shaped to the discovery pass but are not compatibility
# seams, with the reason. Each is a *deliberate architectural facade* whose whole
# job is to re-export -- removing it would break a boundary rather than tidy one.
DISCOVERY_EXEMPTIONS: dict[str, str] = {
    "src/atlas/modules/catalog/api.py": (
        "the module's PUBLIC interface. Re-exporting `router` here is what keeps "
        "`aida.main` from reaching past `api.py` into the contract-protected `router.py`."
    ),
    "src/atlas/modules/connectivity/api.py": "same: the module's PUBLIC interface.",
}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass
class Measured:
    """Everything about a shim that is derived rather than decided."""

    reexported_names: tuple[str, ...] = ()
    reexport_sources: tuple[str, ...] = ()
    caller_files: tuple[str, ...] = ()
    caller_statements: int = 0
    string_reference_files: tuple[str, ...] = ()
    contracts: tuple[str, ...] = ()
    lines: int = 0


@dataclass
class Row:
    shim: Shim
    measured: Measured = field(default_factory=Measured)

    @property
    def caller_count(self) -> int:
        return len(self.measured.caller_files)


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if SKIP_DIR_NAMES & set(path.relative_to(REPO_ROOT).parts):
            continue
        yield path


def _python_files() -> list[Path]:
    """Every Python file a shim caller could live in, except this generator.

    This file names every shim module as a string literal, which would otherwise
    make it a "string reference" to all fourteen of them -- a self-inflicted
    signal that says nothing about the code.
    """
    me = Path(__file__).resolve()
    out: list[Path] = []
    for root in PY_CALLER_ROOTS:
        out.extend(p for p in _iter_files(REPO_ROOT / root, (".py",)) if p.resolve() != me)
    return out


def _ts_files() -> list[Path]:
    return list(_iter_files(REPO_ROOT / TS_CALLER_ROOT, (".ts", ".tsx")))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _reexported_names(path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Names a Python shim re-exports from `atlas.*`, and the modules they come from.

    Only `from atlas... import X` statements count. `X as X` (the explicit
    re-export spelling mypy requires) and a plain `X` are both accepted -- the
    four Phase 1 platform shims use the plain form with an `__all__`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    sources: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not (module == "atlas" or module.startswith("atlas.")):
            continue
        sources.add(module)
        for alias in node.names:
            if alias.name != "*":
                names.add(alias.name)
    return tuple(sorted(names)), tuple(sorted(sources))


def _imported_names_from(tree: ast.Module, module: str) -> tuple[bool, set[str], int]:
    """(imports the module at all, names imported from it, statement count)."""
    hit = False
    names: set[str] = set()
    statements = 0
    prefix = f"{module}."
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.startswith(prefix):
                    hit = True
                    statements += 1
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: never names an absolute shim module
                continue
            target = node.module or ""
            if target == module or target.startswith(prefix):
                hit = True
                statements += 1
                for alias in node.names:
                    names.add(alias.name)
    return hit, names, statements


def _string_references(tree: ast.Module, module: str) -> bool:
    """A string constant naming the module: a patch target or a dynamic import."""
    prefix = f"{module}."
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == module or node.value.startswith(prefix):
                return True
    return False


def _measure_python(shim: Shim, trees: dict[Path, ast.Module]) -> Measured:
    shim_path = REPO_ROOT / shim.path
    names, sources = _reexported_names(shim_path)
    partial = shim.kind == "python-partial"

    callers: list[str] = []
    string_refs: list[str] = []
    statements = 0
    for path, tree in trees.items():
        if path == shim_path:
            continue
        hit, imported, count = _imported_names_from(tree, shim.module)
        if hit and (not partial or (imported & set(names))):
            callers.append(_rel(path))
            statements += count
        if _string_references(tree, shim.module):
            string_refs.append(_rel(path))
    return Measured(
        reexported_names=names,
        reexport_sources=sources,
        caller_files=tuple(sorted(callers)),
        caller_statements=statements,
        string_reference_files=tuple(sorted(string_refs)),
        contracts=tuple(_contracts_naming(shim.module)),
        lines=len(shim_path.read_text(encoding="utf-8").splitlines()),
    )


# `from "./x"`, `from './x'`, `import("./x")` and `export ... from "./x"`.
TS_SPECIFIER_RE = re.compile(r"""(?:from|import)\s*\(?\s*["']([^"']+)["']""")


def _resolve_ts(importer: Path, specifier: str) -> Path | None:
    if not specifier.startswith("."):
        return None
    base = (importer.parent / specifier).resolve()
    for candidate in (
        base,
        base.with_suffix(".ts"),
        base.with_suffix(".tsx"),
        base / "index.ts",
        base / "index.tsx",
    ):
        if candidate.is_file():
            return candidate
    return None


def _measure_typescript(shim: Shim, ts_files: list[Path]) -> Measured:
    shim_path = (REPO_ROOT / shim.path).resolve()
    callers: list[str] = []
    statements = 0
    for path in ts_files:
        if path.resolve() == shim_path:
            continue
        text = path.read_text(encoding="utf-8")
        hits = sum(
            1
            for m in TS_SPECIFIER_RE.finditer(text)
            if _resolve_ts(path, m.group(1)) == shim_path
        )
        if hits:
            callers.append(_rel(path))
            statements += hits
    body = (REPO_ROOT / shim.path).read_text(encoding="utf-8")
    return Measured(
        reexported_names=("* (star re-export)",),
        reexport_sources=(shim.module,),
        caller_files=tuple(sorted(callers)),
        caller_statements=statements,
        lines=len(body.splitlines()),
    )


def _contracts_naming(module: str) -> list[str]:
    """Import-linter contracts that name this module in any of their lists."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    contracts = data.get("tool", {}).get("importlinter", {}).get("contracts", [])
    out: list[str] = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        listed: list[str] = []
        for key in ("allowed_importers", "protected_modules", "source_modules",
                    "forbidden_modules", "modules"):
            value = contract.get(key)
            if isinstance(value, list):
                listed.extend(str(v) for v in value)
        if module in listed:
            out.append(str(contract.get("name", "?")))
    return sorted(out)


# ---------------------------------------------------------------------------
# Discovery -- shim-shaped files the register does not list
# ---------------------------------------------------------------------------

# Anchored at the start of the docstring on purpose. A *mention* of the phrase
# further down usually means the opposite -- `atlas.modules.identity_tenancy.router`
# says "the old path remains as a re-export shim", describing the shim that points
# at it. Only a file that opens by declaring itself one is one.
_PY_SHIM_DOCSTRING_RE = re.compile(r"^backward[- ]compat\w*\s+re-export", re.IGNORECASE)
_TS_STAR_REEXPORT_RE = re.compile(r"^\s*export\s+\*\s+from\s+[\"'][^\"']+[\"'];?\s*$")


def discover_shim_shaped_files() -> list[str]:
    found: list[str] = []
    for path in _iter_files(REPO_ROOT / "src", (".py",)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        doc = ast.get_docstring(tree) or ""
        explicit_reexport = any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("atlas")
            and any(alias.asname == alias.name for alias in node.names)
            for node in tree.body
        )
        if _PY_SHIM_DOCSTRING_RE.search(doc) or explicit_reexport:
            found.append(_rel(path))
    for path in _ts_files():
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith(("//", "/*", "*"))
        ]
        if lines and all(_TS_STAR_REEXPORT_RE.match(line) for line in lines):
            found.append(_rel(path))
    return sorted(found)


def undeclared_shims(discovered: Iterable[str]) -> list[str]:
    declared = {shim.path for shim in SHIMS} | set(DISCOVERY_EXEMPTIONS)
    return [path for path in discovered if path not in declared]


def stale_exemptions(discovered: Iterable[str]) -> list[str]:
    """Exemptions the discovery pass no longer matches.

    An exemption that matches nothing is a claim nobody is checking. Either the
    file is gone, or it stopped looking like a shim -- both mean the entry should
    be deleted rather than left as decoration.
    """
    found = set(discovered)
    return sorted(path for path in DISCOVERY_EXEMPTIONS if path not in found)


def missing_shim_files() -> list[str]:
    return [shim.path for shim in SHIMS if not (REPO_ROOT / shim.path).is_file()]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def collect_rows() -> list[Row]:
    trees: dict[Path, ast.Module] = {}
    for path in _python_files():
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
    ts_files = _ts_files()

    rows: list[Row] = []
    for shim in SHIMS:
        if shim.kind == "typescript":
            measured = _measure_typescript(shim, ts_files)
        else:
            measured = _measure_python(shim, trees)
        rows.append(Row(shim=shim, measured=measured))
    return rows


# Below this many callers the register lists every one of them by name: that is
# the range where a migration is actually planned, and a name is worth more than
# a number. Above it, naming an arbitrary alphabetical handful adds nothing and
# makes the file churn every time someone adds a file that sorts early, so the
# breakdown by source root is given instead.
NAME_CALLERS_UPTO = 8


def _fmt_files(files: Iterable[str], limit: int = 8) -> str:
    files = list(files)
    if not files:
        return "none"
    shown = ", ".join(f"`{f}`" for f in files[:limit])
    if len(files) > limit:
        shown += f", +{len(files) - limit} more"
    return shown


def _caller_breakdown(files: Iterable[str]) -> str:
    """Callers per source root -- stable under the addition of one more caller."""
    buckets: dict[str, int] = defaultdict(int)
    for path in files:
        parts = path.split("/")
        if parts[0] == "src" and len(parts) > 1:
            buckets[f"src/{parts[1]}"] += 1
        elif parts[0] == "ui-next" and len(parts) > 2:
            buckets[f"ui-next/{parts[1]}/{parts[2]}"] += 1
        else:
            buckets[parts[0]] += 1
    return ", ".join(f"`{root}` {count}" for root, count in sorted(buckets.items()))


def render(rows: list[Row]) -> str:
    generated_by = "scripts/shim_register.py"
    edges = sum(row.caller_count for row in rows)
    distinct_callers = len({f for row in rows for f in row.measured.caller_files})
    zero = [row for row in rows if row.caller_count == 0]

    lines: list[str] = [
        "# Compatibility shim register",
        "",
        "> **Generated file — do not edit by hand.**",
        f"> Regenerate with `python {generated_by}`; `--check` fails when it is stale.",
        "> The judgement columns live in that script's `SHIMS` table, not in this file.",
        "",
        "Review 2026-09-05 point **D03** asks for the thing this file is: every",
        "compatibility shim with a replacement path, an owner, a measured caller count",
        "and a removal condition. Its closing instruction is the one that matters most —",
        "*do not remove them solely because they look redundant.* Nothing here authorises",
        "a deletion; the register exists so that a deletion, when it happens, is a",
        "decision against a stated condition rather than a guess against an appearance.",
        "",
        "Related: the generated",
        "[architecture map](../10-architecture/14-generated-architecture-map.md) shows which",
        "bounded contexts are still reached through one of these shims rather than through",
        "their own public face, and the",
        "[domain guides](../20-modules/domain-guides/) say what each of those contexts owns.",
        "",
        "## Which columns are generated and which are not",
        "",
        "| Column | Source |",
        "|---|---|",
        "| Shim, file, lines | Filesystem |",
        "| Re-exported names | `ast` walk of the shim file |",
        "| Caller files, caller count, import statements | `ast` walk of every `.py` "
        "under `src/`, `tests/`, `scripts/`, `sdk/`, `migrations/` |",
        "| String references | `ast` string-constant scan over the same files |",
        "| Import-linter contracts | `pyproject.toml`, parsed |",
        "| Frontend caller counts | Import-specifier scan over `ui-next/src`, resolved "
        "relative to each importing file |",
        "| **Replacement path** | Hand-written |",
        "| **Owner area** | Hand-written |",
        "| **Removal condition** | Hand-written — it is a judgement, not a fact |",
        "",
        "There is no `CODEOWNERS` file in this repository, so *owner* names an **area of",
        "the system**, not a person. An area is the unit that can actually satisfy the",
        "removal condition.",
        "",
        "## What the caller count does and does not see",
        "",
        "Counted: `import X`, `from X import y`, and `from X.sub import y`. Counted",
        "separately, in its own column: a string constant naming the module — a",
        "`monkeypatch.setattr(\"aida.db.engine\", ...)` target or an",
        "`importlib.import_module` argument — because an import-only scan misses those",
        "and they are real dependencies.",
        "",
        "Not counted at all: consumers outside this repository, attribute access on an",
        "already-imported package object, and a module path stored as data. **This is why",
        "a zero count is evidence and not permission.** The review says so directly, and",
        "the removal-condition column, not the count, is what a removal has to satisfy.",
        "",
        "For the two *partial* shims (`aida.models`, `aida.schemas` — large files that",
        "keep most of their own content and re-export a block on top) a file counts as a",
        "caller only when it imports one of the re-exported names. Importing something",
        "that still genuinely lives in the file is not use of the shim.",
        "",
        "## Register",
        "",
        f"{len(rows)} shims, {edges} shim-to-caller-file relationships across "
        f"{distinct_callers} distinct files, "
        f"{len(zero)} shim(s) with a measured caller count of zero.",
        "",
        "| Shim | Kind | Replacement path | Owner area | Callers | Import stmts | "
        "String refs |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        s = row.shim
        lines.append(
            f"| [`{s.key}`](#{_anchor(s.key)}) | {s.kind} | {s.replacement} | "
            f"{s.owner} | {row.caller_count} | {row.measured.caller_statements} | "
            f"{len(row.measured.string_reference_files)} |"
        )
    lines.append("")

    if zero:
        lines += [
            "### Shims with no measured caller",
            "",
            "Reported, **not** deleted. A zero from a static scan is exactly the evidence",
            "D03 warns is insufficient on its own.",
            "",
        ]
        for row in zero:
            lines.append(
                f"- `{row.shim.key}` (`{row.shim.path}`) — "
                f"{len(row.measured.string_reference_files)} string reference(s). "
                f"Removal still requires: {row.shim.removal_condition}"
            )
        lines.append("")
    else:
        lines += [
            "### Shims with no measured caller",
            "",
            "None: every shim in the register has at least one in-repository import",
            "caller today.",
            "",
        ]

    lines += ["## Detail", ""]
    for row in rows:
        lines += _render_detail(row)

    lines += [
        "## Discovery guard",
        "",
        "The generator also *finds* shim-shaped files — a Python module whose docstring",
        "announces a backward-compatible re-export, a Python module that re-exports",
        "`atlas` names in the explicit `X as X` form, or a TypeScript file whose entire",
        "body is a comment plus `export * from` — and fails `--check` if one is not in",
        "the register. A new shim therefore cannot be added without appearing here.",
        "",
        "Files the discovery pass matches that are **not** compatibility seams. An entry",
        "here that the pass stops matching also fails `--check`, so this list cannot",
        "quietly outlive its subject:",
        "",
    ]
    for path, reason in sorted(DISCOVERY_EXEMPTIONS.items()):
        lines.append(f"- `{path}` — {reason}")
    lines += [
        "",
        "Two near-misses worth knowing about, neither of them a shim:",
        "",
        "- `ui-next/src/lib/api.ts` ends with four `export * from` lines but is not",
        "  matched, because the rest of the file is real code. It is the canonical client",
        "  barrel — the intended import surface for screens — not a compatibility path.",
        "- Four of the six bounded contexts' `api.py` files do not re-export their",
        "  router, so they are not matched either. For three of them that is not tidiness:",
        "  `aida.main` still mounts those routers through the `aida.*` shims above, which",
        "  is exactly what their removal conditions say has to change first. The fourth,",
        "  `profiling`, has no routes yet at all — only its models and DTOs have been",
        "  relocated (see `40-engineering/10-bounded-context-relocation-procedure.md`).",
        "",
    ]
    return "\n".join(lines)


def _anchor(heading: str) -> str:
    """GitHub's heading-anchor rules, kept byte-identical to the ones
    `scripts/check_docs_links.py` validates fragments against -- an anchor this
    generator emits has to survive that gate."""
    text = heading.replace("`", "").replace("*", "").replace("_", "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def _render_detail(row: Row) -> list[str]:
    s, m = row.shim, row.measured
    out = [
        f"### {s.key}",
        "",
        f"- **File** — `{s.path}` ({m.lines} lines)",
        f"- **Replacement path** *(hand-written)* — {s.replacement_detail}",
        f"- **Owner area** *(hand-written)* — {s.owner}",
        f"- **Introduced by** — {s.introduced}",
        f"- **Re-exports** — {len(m.reexported_names)} name(s) from "
        f"{_fmt_files(m.reexport_sources, limit=8)}",
        f"- **Callers** — {row.caller_count} file(s), {m.caller_statements} import "
        f"statement(s)",
    ]
    if row.caller_count == 0:
        out.append("  - No in-repository importer. **Not evidence enough to remove it.**")
    elif row.caller_count <= NAME_CALLERS_UPTO:
        out.append(f"  - {_fmt_files(m.caller_files, limit=NAME_CALLERS_UPTO)}")
    else:
        out.append(f"  - By source root: {_caller_breakdown(m.caller_files)}")
    if m.string_reference_files:
        out.append(
            f"- **String references** — {len(m.string_reference_files)} file(s): "
            f"{_fmt_files(m.string_reference_files)}"
        )
    if m.contracts:
        out.append(
            "- **Named in import-linter contracts** — "
            + ", ".join(f"`{c}`" for c in m.contracts)
        )
    out.append(f"- **Removal condition** *(hand-written)* — {s.removal_condition}")
    if s.note:
        out.append(f"- **Note** — {s.note}")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the compatibility-shim register.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the committed register is stale or a shim is undeclared.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args()

    problems: list[str] = []
    for path in missing_shim_files():
        problems.append(
            f"{path} is in SHIMS but does not exist. If the shim was removed, remove its "
            "entry (and record the removal); do not leave a phantom row."
        )
    discovered = discover_shim_shaped_files()
    for path in stale_exemptions(discovered):
        problems.append(
            f"{path} is in DISCOVERY_EXEMPTIONS but the discovery pass no longer matches "
            "it (the file is gone, or it stopped looking like a shim); drop the entry."
        )
    for path in undeclared_shims(discovered):
        problems.append(
            f"{path} looks like a compatibility shim but is in neither SHIMS nor "
            "DISCOVERY_EXEMPTIONS. Add it to one of them."
        )

    rows = collect_rows()
    content = render(rows)

    if args.stdout:
        print(content)
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        return 1 if problems else 0

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist; run without --check to create it.")
            return 1
        if args.output.read_text(encoding="utf-8") != content:
            problems.append(f"{args.output} is stale; regenerate it.")
        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date ({len(rows)} shims).")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(
        f"wrote {args.output} -- {len(rows)} shims, "
        f"{sum(r.caller_count for r in rows)} caller files",
        file=sys.stderr,
    )
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
