#!/usr/bin/env python3
"""Module template generator (tracker ST-01).

Scaffolds the uniform module anatomy defined in
`Docs/10-architecture/04-module-decomposition.md` Sec.7 under
`src/atlas/modules/<name>/`. This is Phase 0 scaffolding only (see
`Docs/40-engineering/06-refactor-plan.md`): it creates an empty,
correctly-shaped module skeleton and moves no behavior. Populating a
generated module with real logic -- moving code out of the current flat
`src/aida/` package -- is a separate, later step (Phase 3/4 of the refactor
plan), done one module at a time, never by this script.

Usage:
    python3 scripts/generate_module.py <module_name> [--dest DIR] [--force]

Example:
    python3 scripts/generate_module.py identity_tenancy
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from string import Template

_MODULE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")

_DEFAULT_MODULES_ROOT = Path("src/atlas/modules")

# Module-name -> schema-name exceptions, from
# `Docs/10-architecture/04-module-decomposition.md` Sec.6. Anything not listed
# here uses its own name as the schema name.
_SCHEMA_ALIASES = {
    "glossary_stewardship": "glossary",
    "context_products_mcp": "context_products",
    "knowledge_graph": "graph_projection",
    "semantic_layer": "semantics",
    "policy_governance": "governance",
    "data_quality": "quality",
    "observability_audit": "audit",
    "identity_tenancy": "identity",
    "query_gateway": "execution",
}

_FILE_TEMPLATES: dict[str, Template] = {
    "__init__.py": Template('"""$title module."""\n'),
    "api.py": Template(
        '"""$title -- PUBLIC interface.\n'
        "\n"
        "Other modules import only from here and from `contracts.py`. Nothing else\n"
        "in this module is reachable from outside it -- enforced mechanically by\n"
        "the `module-privacy` import-linter contract once it exists (tracker ST-02).\n"
        "\n"
        "Status: scaffold only (tracker ST-01). No behavior has moved here yet; see\n"
        "`Docs/40-engineering/06-refactor-plan.md` for the extraction sequence.\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "contracts.py": Template(
        '"""$title -- PUBLIC contracts: DTOs, enums, events, and error types.\n'
        "\n"
        "Cross-module data crosses as these types, never as ORM entities (MD-3 in\n"
        "`Docs/10-architecture/04-module-decomposition.md`).\n"
        "\n"
        'Status: scaffold only (tracker ST-01).\n"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "router.py": Template(
        '"""$title -- HTTP routes, mounted by the app entrypoint.\n'
        "\n"
        'Status: scaffold only (tracker ST-01). No routes have moved here yet.\n"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "from fastapi import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/v1", tags=["$tag"])\n'
    ),
    "service.py": Template(
        '"""$title -- domain logic. The only place this module\'s business rules\n'
        "live.\n"
        "\n"
        'Status: scaffold only (tracker ST-01).\n"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "models.py": Template(
        '"""$title -- PRIVATE. SQLAlchemy models in this module\'s own schema\n'
        "(`$schema`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).\n"
        "\n"
        "Not importable from outside this module once the `module-privacy`\n"
        "contract (tracker ST-02) is enforced.\n"
        "\n"
        'Status: scaffold only (tracker ST-01).\n"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "schemas.py": Template(
        '"""$title -- PRIVATE. Request/response models for `router.py`.\n'
        "\n"
        'Status: scaffold only (tracker ST-01).\n"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "repository.py": Template(
        '"""$title -- PRIVATE. Data access. Tenant scope is a required argument\n'
        "on every query (MD-1, INV-5).\n"
        "\n"
        'Status: scaffold only (tracker ST-01).\n"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "events.py": Template(
        '"""$title -- domain events this module emits.\n'
        "\n"
        "Status: scaffold only (tracker ST-01). See\n"
        "`Docs/30-contracts/04-event-catalog.md` for the published event catalog\n"
        "once this module's events are registered there.\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
    ),
    "workers/__init__.py": Template(
        '"""$title -- Temporal activities and background jobs owned by this\n'
        "module.\n"
        "\n"
        'Status: scaffold only (tracker ST-01).\n"""\n'
    ),
    "migrations/README.md": Template(
        "# $title migrations\n"
        "\n"
        "Alembic revisions scoped to this module's `$schema` schema.\n"
        "\n"
        "Status: scaffold only (tracker ST-01) -- no revisions exist yet.\n"
        "Per-module Alembic schema conventions are set up when this module's\n"
        "tables are actually split out of the shared `models.py` (tracker ST-05).\n"
    ),
    "tests/__init__.py": Template(""),
    "tests/test_module_scaffold.py": Template(
        '"""Standalone self-check for the $name module scaffold.\n'
        "\n"
        "Run in isolation with: `pytest src/atlas/modules/$name` (per the module\n"
        "anatomy's \"runs standalone\" requirement in\n"
        "`Docs/10-architecture/04-module-decomposition.md` Sec.7).\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import ast\n"
        "import importlib\n"
        "from pathlib import Path\n"
        "\n"
        "_PRIVATE_MODULES = (\"models\", \"schemas\", \"repository\", \"service\")\n"
        "\n"
        "\n"
        "def test_public_surface_imports_cleanly() -> None:\n"
        '    """The two public files (`api.py`, `contracts.py`) must import\n'
        "    without error and without requiring anything from the rest of the\n"
        "    codebase -- they are this module's entire surface to the outside\n"
        '    world.\n    """\n'
        '    api = importlib.import_module("atlas.modules.$name.api")\n'
        '    contracts = importlib.import_module("atlas.modules.$name.contracts")\n'
        "\n"
        "    assert api is not None\n"
        "    assert contracts is not None\n"
        "\n"
        "\n"
        "def test_private_files_are_not_imported_by_the_public_surface() -> None:\n"
        '    """A cheap, hand-rolled precursor to the `module-privacy`\n'
        "    import-linter contract (tracker ST-02): the public files must not\n"
        "    import the private ones, so nothing private leaks through\n"
        '    `api.py`/`contracts.py` re-exports.\n    """\n'
        "    module_dir = Path(__file__).resolve().parent.parent\n"
        '    for public_file in ("api.py", "contracts.py"):\n'
        "        tree = ast.parse((module_dir / public_file).read_text(encoding=\"utf-8\"))\n"
        "        imported_names = set()\n"
        "        for node in ast.walk(tree):\n"
        "            if isinstance(node, ast.Import | ast.ImportFrom):\n"
        "                for alias in node.names:\n"
        '                    imported_names.add(alias.name.split(".")[-1])\n'
        "        leaked = imported_names.intersection(_PRIVATE_MODULES)\n"
        "        assert not leaked, (\n"
        '            "public file imports private module internals: "\n'
        "            + str(leaked)\n"
        "        )\n"
    ),
}


def _schema_name(module_name: str) -> str:
    return _SCHEMA_ALIASES.get(module_name, module_name)


def generate_module(
    name: str, *, dest_root: Path = _DEFAULT_MODULES_ROOT, force: bool = False
) -> list[Path]:
    """Scaffold ``dest_root/<name>/`` with the full module anatomy.

    Returns the list of files written. Refuses to touch an existing module
    directory unless ``force=True``, so re-running this can never silently
    clobber a module that has since been populated with real code.
    """
    if not _MODULE_NAME_PATTERN.match(name):
        raise ValueError(
            f"module name {name!r} must be lowercase snake_case, e.g. 'identity_tenancy'"
        )
    module_dir = dest_root / name
    if module_dir.exists() and not force:
        raise FileExistsError(
            f"{module_dir} already exists; pass force=True to regenerate "
            "(existing non-template files are left alone)"
        )
    substitutions = {
        "title": name.replace("_", " "),
        "tag": name.replace("_", "-"),
        "schema": _schema_name(name),
        "name": name,
    }
    written: list[Path] = []
    for relative, template in _FILE_TEMPLATES.items():
        target = module_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template.substitute(**substitutions), encoding="utf-8")
        written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Scaffold a target-structure module (ST-01).")
    parser.add_argument("name", help="lowercase snake_case module name, e.g. identity_tenancy")
    parser.add_argument("--dest", type=Path, default=_DEFAULT_MODULES_ROOT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    written = generate_module(args.name, dest_root=args.dest, force=args.force)
    print(f"Generated {len(written)} files under {args.dest / args.name}")
    for path in written:
        print(f"  {path}")


if __name__ == "__main__":
    main()
