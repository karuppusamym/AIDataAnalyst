#!/usr/bin/env python3
"""Generate the architecture map from the import graph.

Review 2026-09-05 §7 asks for "concise domain guides and generated architecture
maps". The domain guides are hand-written orientation (`Docs/20-modules/`); this
is the generated half, and it is generated for one reason: a hand-drawn diagram
of a 300-module codebase is wrong within a week and nothing notices.

Aggregation, which is the whole design problem
----------------------------------------------
The raw graph has roughly 300 nodes and several thousand edges. Rendered whole it
is a hairball -- technically accurate, useless as a map. So every module is
assigned to exactly one **group**, and the map is drawn at group level.

The grouping is derived from the filesystem and one naming convention already
used throughout the repository, never from a hand-maintained list:

* `atlas.platform.*`                 -> platform
* `atlas.modules.<context>.*`        -> one group per bounded context
* `aida.connectors.*`                -> connectors
* `aida.projectors.*`                -> projectors
* `aida.workflows.*`                 -> workflows
* `aida.<name>_api`                  -> aida routers      (the `_api` suffix)
* everything else flat in `aida.*`   -> aida domain modules

That last split is the one that carries information: the flat package is the bulk
of the system, and separating its HTTP layer from everything else turns an opaque
blob into a statement a reader can check -- and, where the statement fails, the
generator reports the failures by name rather than smoothing them away.

Edges are weighted by how many underlying module-to-module imports they
aggregate, so a thick line and a hairline are visibly different things.

What this map is not
--------------------
* Not a runtime call graph. An import edge means "loading A loads B", not "A calls
  B". A module reachable here can still be dead weight -- that is what
  `tests/test_reachability_gate.py` (modules) and the review's AU-2 (functions)
  separate.
* Not a database or deployment diagram. Owned tables appear per bounded context
  because they are derivable from the models; nothing here says where anything runs.
* Blind to dynamic imports, exactly like every other static pass in this repo.

Standard library only, so it runs in the dependency-free `docs` CI job.

Usage
-----
    python scripts/generate_architecture_map.py            # write the map
    python scripts/generate_architecture_map.py --check    # fail if stale
    python scripts/generate_architecture_map.py --stdout   # print only
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DEFAULT_OUTPUT = REPO_ROOT / "Docs" / "10-architecture" / "14-generated-architecture-map.md"

SKIP_DIR_NAMES = frozenset({"__pycache__"})

# The five real processes. `tests/test_reachability_gate.py` already declares this
# set as the authority on what runs, so the *names* below are verified against
# that file's own `ENTRY_POINTS` literal at generation time
# (`_assert_entry_points_match_reachability_gate`) rather than maintained twice:
# two lists of the same five processes that can disagree is how one of them
# silently becomes a no-op. Only the human-readable label is local.
ENTRY_POINTS: dict[str, str] = {
    "aida.main": "FastAPI application (`uvicorn aida.main:app`)",
    "aida.workflows.worker": "Temporal worker",
    "aida.workflows.scheduler": "Fleet scheduler (polling loop)",
    "aida.projectors.graph_projector": "Lineage graph projector (Kafka consumer)",
    "aida.projectors.outbox_publisher": "Outbox publisher (Kafka producer)",
}

BOUNDED_CONTEXTS = (
    "catalog",
    "connectivity",
    "identity_tenancy",
    "ingestion",
    "observability_audit",
)

# Group ids are mermaid node ids as well, so they stay identifier-safe.
GROUP_LABELS: dict[str, str] = {
    "app": "aida.main (composition root)",
    "platform": "atlas.platform",
    "routers": "aida routers (*_api)",
    "domain": "aida domain modules",
    "connectors": "aida.connectors",
    "projectors": "aida.projectors",
    "workflows": "aida.workflows",
    **{f"ctx_{name}": f"atlas.modules.{name}" for name in BOUNDED_CONTEXTS},
}

GROUP_ORDER = (
    "app",
    "routers",
    "domain",
    *[f"ctx_{name}" for name in BOUNDED_CONTEXTS],
    "connectors",
    "workflows",
    "projectors",
    "platform",
)


def group_of(module: str) -> str:
    """The one group a module belongs to. Pure function of its dotted path."""
    if module == "aida.main":
        # Its own group, not a domain module. `main.py` is the composition root:
        # importing 60 routers is its job, and folding it in with the domain
        # modules would drown the one direction claim this map makes in 55
        # edges that are all correct.
        return "app"
    if module.startswith("atlas.platform"):
        return "platform"
    if module.startswith("atlas.modules."):
        context = module.split(".")[2]
        return f"ctx_{context}" if f"ctx_{context}" in GROUP_LABELS else "domain"
    if module.startswith("aida.connectors"):
        return "connectors"
    if module.startswith("aida.projectors"):
        return "projectors"
    if module.startswith("aida.workflows"):
        return "workflows"
    if module.rsplit(".", 1)[-1].endswith("_api"):
        return "routers"
    return "domain"


# ---------------------------------------------------------------------------
# Import graph
# ---------------------------------------------------------------------------


def _assert_entry_points_match_reachability_gate() -> None:
    """Cross-check `ENTRY_POINTS` against the reachability gate's own list.

    Read with `ast` rather than imported: the gate is a pytest module, and this
    script has to run in the dependency-free `docs` CI job.
    """
    gate = REPO_ROOT / "tests" / "test_reachability_gate.py"
    if not gate.is_file():
        raise SystemExit(f"{gate} is missing; the entry-point list has no cross-check.")
    tree = ast.parse(gate.read_text(encoding="utf-8"), filename=str(gate))
    names: set[str] | None = None
    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else
            list(node.targets) if isinstance(node, ast.Assign) else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "ENTRY_POINTS" for t in targets):
            continue
        value = node.value
        if isinstance(value, ast.Dict):
            names = {
                k.value
                for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    if names is None:
        raise SystemExit(
            "tests/test_reachability_gate.py no longer defines a literal ENTRY_POINTS "
            "dict; this map's entry-point list can no longer be cross-checked."
        )
    if names != set(ENTRY_POINTS):
        raise SystemExit(
            "Entry points disagree with tests/test_reachability_gate.py. "
            f"Only here: {sorted(set(ENTRY_POINTS) - names)}. "
            f"Only there: {sorted(names - set(ENTRY_POINTS))}."
        )


def _module_name(path: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(SRC_ROOT).parts)
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1]), True
    return ".".join([*parts[:-1], parts[-1][:-3]]), False


def discover_modules() -> dict[str, tuple[Path, bool]]:
    modules: dict[str, tuple[Path, bool]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if SKIP_DIR_NAMES & set(path.relative_to(SRC_ROOT).parts):
            continue
        name, is_pkg = _module_name(path)
        modules[name] = (path, is_pkg)
    return modules


def _touched(module: str, is_pkg: bool, node: ast.Import | ast.ImportFrom) -> set[str]:
    """Every dotted name one import statement references.

    Over-inclusive on purpose (an imported *class* looks like a submodule here);
    the caller keeps only names that exist as modules on disk, so over-inclusion
    can never invent an edge to something that is not there.
    """
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if node.level == 0:
        base = node.module or ""
    else:
        own = module if is_pkg else (module.rsplit(".", 1)[0] if "." in module else "")
        parts = own.split(".") if own else []
        strip = node.level - 1
        if strip:
            parts = parts[: len(parts) - strip] if strip <= len(parts) else []
        base = ".".join(parts)
        if node.module:
            base = f"{base}.{node.module}" if base else node.module
    if not base:
        return set()
    names = {base}
    for alias in node.names:
        if alias.name != "*":
            names.add(f"{base}.{alias.name}")
    return names


def build_graph() -> tuple[dict[str, set[str]], dict[str, tuple[Path, bool]]]:
    modules = discover_modules()
    known = set(modules)
    graph: dict[str, set[str]] = {name: set() for name in known}
    for name, (path, is_pkg) in modules.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for dotted in _touched(name, is_pkg, node):
                parts = dotted.split(".")
                for i in range(1, len(parts) + 1):
                    prefix = ".".join(parts[:i])
                    if prefix in known and prefix != name:
                        graph[name].add(prefix)
    return graph, modules


def reachable_from(graph: dict[str, set[str]], seeds: set[str]) -> set[str]:
    seen, stack = set(seeds), list(seeds)
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def entry_seeds(entry: str, known: set[str]) -> set[str]:
    """Running `python -m a.b.c` executes `a`, then `a.b`, then `a.b.c`."""
    parts = entry.split(".")
    return {
        ".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in known
    }


# ---------------------------------------------------------------------------
# Derived facts about the bounded contexts
# ---------------------------------------------------------------------------

ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete"})


@dataclass
class ContextFacts:
    name: str
    files: tuple[str, ...]
    tables: tuple[tuple[str, str], ...]
    routes: tuple[tuple[str, str], ...]
    modules: int
    contract: str | None
    mounted_via: str


def _tables(models_path: Path) -> tuple[tuple[str, str], ...]:
    if not models_path.is_file():
        return ()
    tree = ast.parse(models_path.read_text(encoding="utf-8"), filename=str(models_path))
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets
            ):
                if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    out.append((node.name, stmt.value.value))
    return tuple(out)


def _routes(router_path: Path) -> tuple[tuple[str, str], ...]:
    if not router_path.is_file():
        return ()
    tree = ast.parse(router_path.read_text(encoding="utf-8"), filename=str(router_path))
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in ROUTE_DECORATORS
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                out.append((dec.func.attr.upper(), str(dec.args[0].value)))
    return tuple(out)


def _contract_for(module_prefix: str, contracts: list[dict]) -> str | None:
    for contract in contracts:
        protected = contract.get("protected_modules")
        if isinstance(protected, list) and any(
            str(m).startswith(module_prefix) for m in protected
        ):
            return str(contract.get("name", "?"))
    return None


# The `aida.*` module each context's router is still reachable through, where one
# exists. These are the ST-07 Commit C re-export shims; the register at
# `Docs/40-engineering/09-compatibility-shim-register.md` is their record.
CONTEXT_SHIM: dict[str, str] = {
    "identity_tenancy": "aida.workspace_api",
    "ingestion": "aida.ingestion_api",
    "observability_audit": "aida.observability_api",
}


def _mounted_via(context: str, main_imports: set[str]) -> str:
    api_module = f"atlas.modules.{context}.api"
    if api_module in main_imports:
        return f"`{api_module}` (public face)"
    shim = CONTEXT_SHIM.get(context)
    if shim and shim in main_imports:
        return f"`{shim}` (compatibility shim)"
    return "not mounted from `aida.main`"


def _main_imports() -> set[str]:
    tree = ast.parse((SRC_ROOT / "aida" / "main.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return out


def context_facts(
    modules: dict[str, tuple[Path, bool]], contracts: list[dict]
) -> list[ContextFacts]:
    main_imports = _main_imports()
    out: list[ContextFacts] = []
    for name in BOUNDED_CONTEXTS:
        base = SRC_ROOT / "atlas" / "modules" / name
        files = tuple(
            sorted(
                p.relative_to(base).as_posix()
                for p in base.rglob("*.py")
                if not (SKIP_DIR_NAMES & set(p.relative_to(base).parts))
            )
        )
        prefix = f"atlas.modules.{name}"
        out.append(
            ContextFacts(
                name=name,
                files=files,
                tables=_tables(base / "models.py"),
                routes=_routes(base / "router.py"),
                modules=sum(1 for m in modules if m == prefix or m.startswith(prefix + ".")),
                contract=_contract_for(prefix, contracts),
                mounted_via=_mounted_via(name, main_imports),
            )
        )
    return out


def load_contracts() -> list[dict]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    raw = data.get("tool", {}).get("importlinter", {}).get("contracts", [])
    return [c for c in raw if isinstance(c, dict)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _mermaid_id(group: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", group)


def render() -> str:
    _assert_entry_points_match_reachability_gate()
    graph, modules = build_graph()
    known = set(modules)
    contracts = load_contracts()

    group_members: dict[str, list[str]] = defaultdict(list)
    for module in sorted(known):
        group_members[group_of(module)].append(module)

    group_edges: dict[tuple[str, str], int] = defaultdict(int)
    for src, targets in graph.items():
        for dst in targets:
            g_src, g_dst = group_of(src), group_of(dst)
            if g_src != g_dst:
                group_edges[(g_src, g_dst)] += 1

    reach: dict[str, set[str]] = {
        entry: reachable_from(graph, entry_seeds(entry, known)) for entry in ENTRY_POINTS
    }
    all_reached = set().union(*reach.values()) if reach else set()

    fan_in: dict[str, int] = defaultdict(int)
    for targets in graph.values():
        for dst in targets:
            fan_in[dst] += 1

    # The one place the aggregation makes a checkable claim: domain modules
    # should not import routers.
    upward = sorted(
        (src, dst)
        for src, targets in graph.items()
        for dst in targets
        if group_of(dst) == "routers" and group_of(src) in {"domain", "platform", "connectors"}
    )

    facts = context_facts(modules, contracts)

    lines: list[str] = [
        "# Architecture map (generated)",
        "",
        "> **Generated file — do not edit by hand.**",
        "> Regenerate with `python scripts/generate_architecture_map.py`; `--check` fails",
        "> when it is stale. Every number and every edge below is read out of the source",
        "> tree and `pyproject.toml` at generation time.",
        "",
        f"{len(known)} Python modules under `src/`, "
        f"{sum(len(v) for v in graph.values())} intra-`src` import edges.",
        "",
        "## How this map aggregates",
        "",
        "Drawn whole, the module graph is a hairball: accurate and unreadable. Every",
        "module is therefore assigned to exactly one **group**, and the diagrams are drawn",
        "at group level with edge weights showing how many module-to-module imports each",
        "line stands for. The assignment is a pure function of a module's dotted path —",
        "package location, plus the `_api` filename suffix this repository already uses",
        "for its HTTP layer — so it cannot drift from the tree it describes.",
        "",
        "| Group | Rule | Modules |",
        "|---|---|---:|",
    ]
    rules = {
        "app": "`aida.main` alone -- the composition root",
        "platform": "`atlas.platform.*`",
        "routers": "flat `aida.*` whose filename ends `_api`",
        "domain": "everything else flat in `aida.*`",
        "connectors": "`aida.connectors.*`",
        "projectors": "`aida.projectors.*`",
        "workflows": "`aida.workflows.*`",
        **{f"ctx_{n}": f"`atlas.modules.{n}.*`" for n in BOUNDED_CONTEXTS},
    }
    for group in GROUP_ORDER:
        lines.append(
            f"| {GROUP_LABELS[group]} | {rules[group]} | {len(group_members.get(group, []))} |"
        )
    lines += [
        "",
        "What is deliberately *not* aggregated away: the five bounded contexts each keep",
        "their own group even though four of them are small, because the point of the map",
        "is to show how much of the system has and has not moved into one.",
        "",
        "## Module graph, by group",
        "",
        "An edge means *importing anything in the source group loads something in the",
        "target group*. Weights are the number of module-level imports aggregated into",
        "that line.",
        "",
        "```mermaid",
        "graph LR",
    ]
    for group in GROUP_ORDER:
        count = len(group_members.get(group, []))
        lines.append(f'  {_mermaid_id(group)}["{GROUP_LABELS[group]}<br/>{count} modules"]')
    for (src, dst), weight in sorted(group_edges.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {_mermaid_id(src)} -->|{weight}| {_mermaid_id(dst)}")
    lines += [
        "```",
        "",
    ]

    lines += [
        "### The claim this grouping makes, and whether it holds",
        "",
        "Splitting the flat package into *routers* and *domain modules* is only useful if",
        "the dependency runs one way. It mostly does — and where it does not, the",
        "exceptions are named here rather than hidden by the aggregation.",
        "",
    ]
    if upward:
        lines += [
            f"{len(upward)} import(s) run the wrong way (a domain, platform or connector",
            "module importing a router):",
            "",
            "| Importer | Router imported |",
            "|---|---|",
        ]
        lines += [f"| `{src}` | `{dst}` |" for src, dst in upward]
        lines += [
            "",
            "These are facts, not verdicts. Two of the import-linter contracts in",
            "`pyproject.toml` exist precisely because edges of this shape closed a cycle",
            "once; the ones listed here are the ones no contract currently forbids.",
            "",
        ]
    else:
        lines += [
            "No domain, platform or connector module imports a router. The direction holds",
            "everywhere in the current tree.",
            "",
        ]

    lines += [
        "## Process entry points and what each reaches",
        "",
        "The five processes that constitute the running system. *Reaches* is transitive",
        "import reachability from that process's own module, which is what determines",
        "whether code can run in it at all — not whether it does.",
        "",
        "| Entry point | Process | Modules reached |",
        "|---|---|---:|",
    ]
    for entry, label in ENTRY_POINTS.items():
        lines.append(f"| `{entry}` | {label} | {len(reach[entry])} |")
    lines += [
        "",
        f"Union of all five: {len(all_reached)} of {len(known)} modules.",
        "",
        "Per group, how much of each group each process pulls in:",
        "",
        "| Group | " + " | ".join(e.split(".")[-1] for e in ENTRY_POINTS) + " | Group size |",
        "|---|" + "---:|" * (len(ENTRY_POINTS) + 1),
    ]
    for group in GROUP_ORDER:
        members = set(group_members.get(group, []))
        cells = [str(len(members & reach[entry])) for entry in ENTRY_POINTS]
        lines.append(f"| {GROUP_LABELS[group]} | " + " | ".join(cells) + f" | {len(members)} |")
    # A five-by-eleven edge diagram would be the hairball this map exists to
    # avoid, and the table above already carries those numbers exactly. What a
    # diagram adds is the shape the table hides: how much of the tree every
    # process loads in common, and how much is that process's alone.
    shared = set.intersection(*(reach[e] for e in ENTRY_POINTS)) if reach else set()
    exclusive = {
        entry: reach[entry] - set().union(*(reach[o] for o in ENTRY_POINTS if o != entry))
        for entry in ENTRY_POINTS
    }
    lines += [
        "",
        f"**{len(shared)} modules are loaded by all five processes** — the shared",
        "substrate every process pays for. The rest divides into what each process alone",
        "pulls in:",
        "",
        "```mermaid",
        "graph LR",
        f'  shared["shared substrate<br/>{len(shared)} modules"]',
    ]
    for entry in ENTRY_POINTS:
        lines.append(f'  {_mermaid_id(entry)}(["{entry}<br/>{len(reach[entry])} reached"])')
    for entry in ENTRY_POINTS:
        lines.append(f"  {_mermaid_id(entry)} --> shared")
        only = len(exclusive[entry])
        if only:
            node = f"only_{_mermaid_id(entry)}"
            lines.append(f'  {node}["only this process<br/>{only} modules"]')
            lines.append(f"  {_mermaid_id(entry)} --> {node}")
    lines += ["```", ""]

    lines += [
        "## Bounded contexts",
        "",
        "The five module directories under `src/atlas/modules/`. Tables and routes are",
        "read out of each context's own `models.py` and `router.py`; *mounted via* is read",
        "out of `aida.main`'s imports, which is the fact that says whether a context's",
        "public face is being used or a compatibility shim still stands in front of it.",
        "",
        "| Context | Modules | Owned tables | Routes | Mounted via | Privacy contract |",
        "|---|---:|---:|---:|---|---|",
    ]
    for fact in facts:
        contract = f"`{fact.contract}`" if fact.contract else "none"
        lines.append(
            f"| [`{fact.name}`](../20-modules/domain-guides/{fact.name.replace('_', '-')}.md) "
            f"| {fact.modules} | {len(fact.tables)} | {len(fact.routes)} | "
            f"{fact.mounted_via} | {contract} |"
        )
    lines += [
        "",
        "Each context's own guide is linked from the name. Owned tables, per context:",
        "",
    ]
    for fact in facts:
        names = ", ".join(f"`{table}`" for _, table in fact.tables) or "none"
        lines.append(f"- **{fact.name}** — {names}")
    lines += [
        "",
        "## Import-linter contracts actually enforced",
        "",
        "Parsed from `pyproject.toml`. These run in CI as `lint-imports` in the",
        "`Lint, types and architecture` job, so every one of them is enforced on every",
        "push rather than described.",
        "",
        "| Contract | Type | Guards |",
        "|---|---|---|",
    ]
    for contract in contracts:
        kind = str(contract.get("type", "?"))
        if kind == "protected":
            guarded = (
                f"{len(contract.get('protected_modules', []))} protected module(s), "
                f"{len(contract.get('allowed_importers', []))} allowed importer(s)"
            )
        elif kind == "forbidden":
            guarded = (
                f"{len(contract.get('source_modules', []))} source module(s) may not import "
                f"{len(contract.get('forbidden_modules', []))} module(s)"
            )
        else:
            guarded = f"{len(contract.get('modules', []))} module(s)"
        lines.append(f"| {contract.get('name', '?')} | {kind} | {guarded} |")

    forbidden_pairs = [
        (str(s), str(f))
        for contract in contracts
        if contract.get("type") == "forbidden"
        for s in contract.get("source_modules", [])
        for f in contract.get("forbidden_modules", [])
    ]
    lines += [
        "",
        f"{len(contracts)} contracts, {len(forbidden_pairs)} forbidden module pairs. The",
        "forbidden edges, drawn — a dashed line is an import the build rejects:",
        "",
        "```mermaid",
        "graph LR",
    ]
    drawn: set[str] = set()
    for src, dst in forbidden_pairs:
        for node in (src, dst):
            if node not in drawn:
                lines.append(f'  {_mermaid_id(node)}["{node}"]')
                drawn.add(node)
    for src, dst in forbidden_pairs:
        lines.append(f"  {_mermaid_id(src)} -.->|forbidden| {_mermaid_id(dst)}")
    lines += ["```", ""]

    lines += [
        "## Most-imported modules",
        "",
        "The hubs: what a change here touches. Fan-in counts direct importers inside",
        "`src/`, so a high number means a wide blast radius, not importance.",
        "",
        "| Module | Group | Direct importers |",
        "|---|---|---:|",
    ]
    for module, count in sorted(fan_in.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        lines.append(f"| `{module}` | {GROUP_LABELS[group_of(module)]} | {count} |")
    lines += [
        "",
        "## What this map cannot tell you",
        "",
        "- An import edge is not a call. Reachable code can still be dead;",
        "  `tests/test_reachability_gate.py` is the module-level gate and function-level",
        "  liveness is a separate, open question.",
        "- Dynamic imports are invisible here, as in every static pass in this repository.",
        "- Group membership is a path rule, not a judgement about what a module is for.",
        "  A misfiled module is grouped by where it sits, which is the honest answer.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the architecture map.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the committed map differs from what would be generated.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args()

    content = render()
    if args.stdout:
        print(content)
        return 0
    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist; run without --check to create it.")
            return 1
        if args.output.read_text(encoding="utf-8") != content:
            print(f"{args.output} is out of date with the source tree; regenerate it.")
            return 1
        print(f"{args.output} is up to date.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
