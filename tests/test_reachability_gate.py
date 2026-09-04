"""AU-1 — CI reachability gate.

`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` found ~4,600 lines of
implementation behind 17 tracker rows marked DONE (six P0) that were
importable from no running entry point: fully unit-tested in isolation,
never wired into anything that actually runs. Nothing in CI detected it,
because a module with passing unit tests and no callers is indistinguishable
from a healthy one at the level pytest checks.

This test is the permanent version of the ~80-line one-off script the audit
used to find them (its method is in the audit's own §7). It builds a full
AST import graph of every module under ``src/aida``, computes what is
transitively reachable from the five processes that constitute the running
application, and fails the build if anything else is not reachable and not
on the explicit, justified allow-list below.

Scope, deliberately: this is a *module*-reachability gate, matching AU-1's
exit criterion verbatim ("fails on any unreachable module"). It answers
"does anything on a live path even import this file". It does NOT answer
"does a live code path call this specific function" -- a module whose
router is registered on the FastAPI app is reachable here even if every
handler in it is dead weight forwarding to nothing. That finer-grained
question ("a DONE row requires a named call site on a live path, not a
passing unit test") is AU-2's job, not this gate's. Concretely: this branch
already wires `abac_api` and `ai_decision_lineage_api` onto the running app
(`main.py`), so `abac.py` and `ai_decision_lineage.py` are module-reachable
today and correctly do NOT appear on the allow-list below, even though the
audit separately found their core functions (`record_decision`, real ABAC
enforcement) have no live caller -- that is AU-2 territory.

Do NOT add a module to ALLOWLIST to make this test pass. Wire it into a
live call path, delete it, or -- only if it is a genuine, already-tracked
backlog item -- add it here with the tracker row that owns the gap. Modules
here are re-verified on every run (see
``test_allowlist_has_no_stale_entries``): remove an entry the moment its
module becomes reachable, and shrink the list as those tracker rows close.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "aida"
PACKAGE = "aida"

# The five real, currently-running processes that constitute this
# application (Docs/60-delivery/04-end-to-end-audit-2026-08-30.md §2, §7).
# If one of these ever moves, this dict must move with it -- that is the
# point: a stale entry point here would silently make the whole gate a
# no-op, so `test_entry_points_exist_on_disk` fails loudly instead.
ENTRY_POINTS: dict[str, str] = {
    "aida.main": "FastAPI app (uvicorn aida.main:app)",
    "aida.workflows.worker": "Temporal worker process",
    "aida.workflows.scheduler": "fleet scheduler process (polling loop)",
    "aida.projectors.graph_projector": "lineage graph projector (Kafka consumer)",
    "aida.projectors.outbox_publisher": "outbox publisher (Kafka producer)",
}

# Modules confirmed genuinely unreachable as of this run (re-verify before
# trusting an old comment -- concurrent work on this branch wires these in
# regularly; `test_allowlist_has_no_stale_entries` below re-checks every
# entry on every CI run so a fixed module cannot linger here unnoticed).
# Each entry names the tracker row(s) that own the gap per
# Docs/60-delivery/03-tracker.md, so this list doubles as the honest
# backlog record, not a way to hide it.
ALLOWLIST: dict[str, str] = {
    # --- Observability (§20, all P0) ---
    # observability.py, siem_routing.py and worm_archive.py were wired into the live
    # startup/request/audit path after the audit was written (OB-1/OB-2/OB-3 closed:
    # main.lifespan now calls configure_tracing/configure_metrics and starts the WORM
    # archive sweep; aida.events.record_audit and aida.security.get_security_context now
    # call route_to_siem), so they are correctly no longer on this list.
    # --- Retrieval stack (§12, P0 family) ---
    # retrieval.py, fusion_ranking.py, graph_retrieval.py, embedding_provider.py and
    # vector_retrieval.py were wired into the live orchestration path after the audit was
    # written (RT-1/RT-2/RT-3/RT-9/SM-2 closed), so they are correctly no longer on this
    # list. vector_store.py -- the *persisted* vector index, as opposed to
    # vector_retrieval.py's live-embed-per-query approach -- is a separate module that
    # remains genuinely unwired; that commit's own known-limitations note says so.
    "aida.vector_store": "RT-1: zero importers anywhere in src/. Known gap.",
    # --- Injection defense (§13, P0 family) ---
    # injection_defense.py itself was wired into ingest_screening.screen_text (and from
    # there into the live ingestion write path and mcp_server._transformation_detail)
    # after the audit was written, so it is correctly no longer on this list. Its sibling
    # injection_corpus.py is a separate module -- not imported by injection_defense.py,
    # only referenced in a docstring -- and remains genuinely unreachable.
    "aida.injection_corpus": (
        "AG-1/AG-2/TS-6: standalone corpus module, not imported by injection_defense.py "
        "or anything else outside its own test. Known gap."
    ),
}


def _module_name_for(path: Path) -> tuple[str, bool]:
    """Return (dotted module name, is_package) for a .py file under SRC_ROOT."""
    rel_parts = list(path.relative_to(SRC_ROOT).parts)
    if rel_parts[-1] == "__init__.py":
        return ".".join(rel_parts[:-1]), True
    return ".".join([*rel_parts[:-1], rel_parts[-1][:-3]]), False


def _discover_modules() -> dict[str, tuple[Path, bool]]:
    modules: dict[str, tuple[Path, bool]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        name, is_pkg = _module_name_for(path)
        modules[name] = (path, is_pkg)
    return modules


def _touched_dotted_names(
    cur_module: str, is_pkg: bool, node: ast.Import | ast.ImportFrom
) -> set[str]:
    """Every fully-qualified name a single import statement causes Python to
    load or reference. Deliberately over-inclusive (e.g. it includes names
    that turn out to be a class/function rather than a submodule) -- callers
    filter against the known module set, so an over-inclusive touch here
    never manufactures a false *reachable* result for a module that doesn't
    actually exist.
    """
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}

    # ImportFrom, possibly relative.
    if node.level == 0:
        base = node.module or ""
    else:
        # `__package__` semantics: a package module's own package is
        # itself; a regular module's package is its parent.
        own_package = (
            cur_module if is_pkg else (cur_module.rsplit(".", 1)[0] if "." in cur_module else "")
        )
        parts = own_package.split(".") if own_package else []
        strip = node.level - 1  # level=1 ("from . import x") means own_package unchanged
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


def _atlas_bridge_deps() -> dict[str, set[str]]:
    """Map each `atlas.modules.*.router` module to the aida.* modules it
    imports.

    2026-09-03 strangle bridge. When an aida module imports an
    `atlas.modules.<name>.router` (as `main.py` does when mounting a
    strangled router, and as the aida-side shims do to re-export the
    router object), the router in turn imports back from aida
    (`aida.security`, `aida.models`, `aida.events`, ...). Without this
    bridge, the pure-aida reachability walk would false-positive every
    aida module used only by a router that moved. This helper lets the
    graph builder treat those aida deps as if the aida importer had
    imported them directly.

    Deliberately limited to `router.py` files under `atlas/modules/`:
    that's the only shape the strangle produces today. Extending it to
    `service.py` / `repository.py` / etc. would over-approximate
    reachability -- those modules are only reached from their own
    router or api file, and if that's a shim, the router file's own
    aida deps are what matters.
    """
    bridges: dict[str, set[str]] = {}
    atlas_root = SRC_ROOT / "atlas" / "modules"
    if not atlas_root.exists():
        return bridges
    for router_path in atlas_root.rglob("router.py"):
        rel = router_path.relative_to(SRC_ROOT)
        mod = ".".join(rel.with_suffix("").parts)
        deps: set[str] = set()
        try:
            tree = ast.parse(router_path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if base == PACKAGE or base.startswith(f"{PACKAGE}."):
                    deps.add(base)
                    for alias in node.names:
                        if alias.name != "*":
                            deps.add(f"{base}.{alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == PACKAGE or alias.name.startswith(f"{PACKAGE}."):
                        deps.add(alias.name)
        bridges[mod] = deps
    return bridges


def build_import_graph() -> tuple[dict[str, set[str]], set[str]]:
    """Full static import graph of src/aida: module -> set of aida modules
    it causes to load, restricted to modules that actually exist on disk.
    """
    modules = _discover_modules()
    all_modules = set(modules)
    graph: dict[str, set[str]] = {name: set() for name in all_modules}
    atlas_bridges = _atlas_bridge_deps()

    for name, (path, is_pkg) in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for dotted in _touched_dotted_names(name, is_pkg, node):
                # Aida-prefixed name: normal case, add every real prefix.
                if dotted == PACKAGE or dotted.startswith(f"{PACKAGE}."):
                    parts = dotted.split(".")
                    for i in range(1, len(parts) + 1):
                        prefix = ".".join(parts[: i])
                        if prefix in all_modules:
                            graph[name].add(prefix)
                    continue
                # Strangle bridge: an atlas.modules.*.router import brings
                # in whatever aida modules that router itself imports (the
                # router still depends on aida.security / aida.models /
                # aida.events / etc. -- only the endpoint handlers moved).
                for bridge_mod, bridge_deps in atlas_bridges.items():
                    if dotted == bridge_mod or dotted.startswith(f"{bridge_mod}."):
                        for aida_dep in bridge_deps:
                            dparts = aida_dep.split(".")
                            for i in range(1, len(dparts) + 1):
                                prefix = ".".join(dparts[: i])
                                if prefix in all_modules:
                                    graph[name].add(prefix)
                        break

    return graph, all_modules


def _entry_point_seeds(all_modules: set[str]) -> set[str]:
    """Running e.g. `python -m aida.projectors.graph_projector` also
    executes every parent package's __init__.py first, exactly like a live
    `from aida.x import y` elsewhere in the graph -- seed with entry points'
    own parent-package prefixes too, not just the leaf module.
    """
    seeds: set[str] = set()
    for ep in ENTRY_POINTS:
        parts = ep.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[: i])
            if prefix in all_modules:
                seeds.add(prefix)
    return seeds


def _reachable_from(graph: dict[str, set[str]], seeds: set[str]) -> set[str]:
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        current = stack.pop()
        for nxt in graph.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _compute() -> tuple[set[str], set[str]]:
    """Returns (unreachable modules, all modules)."""
    graph, all_modules = build_import_graph()
    seeds = _entry_point_seeds(all_modules)
    reachable = _reachable_from(graph, seeds)
    return all_modules - reachable, all_modules


def test_entry_points_exist_on_disk() -> None:
    """A renamed/moved entry point must break this test loudly, not silently
    shrink the graph this gate walks.
    """
    _, all_modules = build_import_graph()
    missing = {ep: why for ep, why in ENTRY_POINTS.items() if ep not in all_modules}
    assert not missing, (
        "ENTRY_POINTS in tests/test_reachability_gate.py names modules that no longer "
        f"exist on disk: {missing}. Find where each process's entry point moved to and "
        "update ENTRY_POINTS -- until then this gate is silently checking the wrong graph."
    )


def test_no_dynamic_module_dispatch() -> None:
    """The reachability graph is purely static (ast-based). If anything in
    src/ can select a module or callable at runtime by name, the graph above
    could under-report reachability -- exactly the caveat the audit ruled
    out by grep (§7). Re-verify that on every run rather than trusting a
    comment: any hit here must be reconciled (accounted for explicitly, or
    removed) before the static graph above can be trusted again.
    """
    dynamic_patterns = [
        re.compile(r"\bimportlib\b"),
        re.compile(r"\bimport_module\s*\("),
        re.compile(r"__import__\s*\("),
        re.compile(r"\bentry_points\s*\("),
        re.compile(r"\bsys\.modules\s*\["),
        re.compile(r"\bglobals\(\)\s*\["),
    ]
    hits: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in dynamic_patterns:
            if pattern.search(text):
                hits.append(f"{path.relative_to(REPO_ROOT)}: matches {pattern.pattern!r}")
    assert not hits, (
        "Found dynamic module/callable dispatch under src/aida/, which the static AST "
        "import graph in this file cannot see -- it can make the reachability gate report "
        "a module as unreachable when a live path actually reaches it dynamically (or vice "
        "versa). Reconcile each hit explicitly (teach build_import_graph about it, or "
        "confirm it's inert) rather than ignoring this failure:\n" + "\n".join(hits)
    )


def test_no_undeclared_console_entry_points() -> None:
    """The audit's method also checked pyproject.toml for console entry
    points that could reach a module outside the five processes above.
    There are none today; if one is ever added, this fails and forces
    ENTRY_POINTS to be reconsidered rather than silently trusting a sixth,
    unaudited way into the package.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})
    scripts = project.get("scripts", {})
    entry_points = project.get("entry-points", {})
    assert not scripts and not entry_points, (
        "pyproject.toml now declares console_scripts / entry-points "
        f"(scripts={scripts!r}, entry-points={entry_points!r}). These are additional ways "
        "into src/aida/ that this gate's five ENTRY_POINTS do not account for -- add the "
        "modules they target to ENTRY_POINTS (with justification) before trusting this gate."
    )


def test_all_modules_reachable_or_allowlisted() -> None:
    """The gate. Every module under src/aida/ must be reachable, via static
    imports, from one of the five real entry points -- or be named on
    ALLOWLIST with a tracker row that owns the gap.
    """
    unreachable, _ = _compute()
    unjustified = sorted(unreachable - set(ALLOWLIST))
    assert not unjustified, (
        "The following src/aida modules are importable from none of the five real entry "
        f"points ({sorted(ENTRY_POINTS)}) and are not on the ALLOWLIST in this file:\n"
        + "\n".join(f"  - {m}" for m in unjustified)
        + "\n\nThis is exactly the failure mode in "
        "Docs/60-delivery/04-end-to-end-audit-2026-08-30.md: code that passes its own unit "
        "tests with zero live callers. Wire the module into a real call path, delete it, or "
        "-- only for a genuine, already-tracked backlog item -- add it to ALLOWLIST with the "
        "owning tracker row. Do not add it just to make this test pass."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """The allow-list is a claim ('these are still genuinely unreachable
    today'), re-checked every run so it can't silently drift from the code:

    - an allow-listed module that got deleted should have its entry removed
      (dead reference to nothing);
    - an allow-listed module that became reachable (another agent wired it
      in) should have its entry removed -- leaving it in place would hide a
      real fix and shrink test_all_modules_reachable_or_allowlisted's
      effective coverage for no reason.
    """
    unreachable, all_modules = _compute()

    deleted = sorted(m for m in ALLOWLIST if m not in all_modules)
    assert not deleted, (
        f"ALLOWLIST names modules that no longer exist: {deleted}. Remove these entries -- "
        "the module was presumably deleted, and its tracker row (if still open) should be "
        "re-pointed or closed separately."
    )

    now_reachable = sorted(m for m in ALLOWLIST if m not in unreachable)
    assert not now_reachable, (
        f"ALLOWLIST names modules that are now reachable from a live entry point: "
        f"{now_reachable}. Someone wired this in since the allow-list entry was written -- "
        "remove the entry (and if you know which tracker row that closes, update it)."
    )
