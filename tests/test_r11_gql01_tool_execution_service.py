"""R11-GQL01: the GraphQL facade reaches no router, execution path included.

`executeGovernedTool` runs the same function a REST execute route and a persisted tool plan
run. That function lived in the `aida.tool_api` router, so `aida.graphql_schema` reached a
router through `aida.governed_execution` -- the one chain that kept the GraphQL modules out
of the import contract that forbids exactly this. The function moved, unchanged, to
`aida.tool_execution`; the router re-exports it.

These tests hold both halves: every surface still executes through one object rather than a
copy, and no module of the facade can reach a router by any chain of imports. The second is
proven here by walking the imports as well as by the contract, so it fails in the test suite
rather than only where the contract runs.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from aida import governed_execution, tool_api, tool_execution, tool_plans_api

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "src" / "aida"

#: A router is a module that mounts routes. `graphql_api` is the facade's own route module,
#: which is why the facade may contain it but never import another one.
ROUTERS = {f"aida.{path.stem}" for path in SOURCE.glob("*_api.py")} | {
    "aida.main",
    "aida.mcp_server",
}
FACADE = ("aida.graphql_api", "aida.graphql_limits", "aida.graphql_reads", "aida.graphql_schema")


def _imports(module: str) -> set[str]:
    """Every `aida.*` module this one imports, including inside functions."""
    path = SOURCE / f"{module.removeprefix('aida.')}.py"
    if not path.exists():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("aida"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("aida"))
    return found


def _reachable(start: str) -> dict[str, list[str]]:
    """Every module reachable from `start`, with the shortest chain that reaches it."""
    paths = {start: [start]}
    queue = [start]
    while queue:
        module = queue.pop(0)
        for imported in sorted(_imports(module)):
            if imported not in paths:
                paths[imported] = [*paths[module], imported]
                queue.append(imported)
    return paths


def test_every_surface_executes_through_one_object() -> None:
    """Not a copy with the same shape: the same function object, so a change to the execution
    path cannot reach one surface and miss another."""
    assert governed_execution.execute_tool_version is tool_execution.execute_tool_version
    assert tool_api.execute_tool_version is tool_execution.execute_tool_version
    assert tool_plans_api.execute_tool_version is tool_execution.execute_tool_version
    assert tool_api._enforce_agent_contract is tool_execution._enforce_agent_contract


def test_the_execution_service_imports_no_router() -> None:
    assert _imports("aida.tool_execution") & ROUTERS == set()


@pytest.mark.parametrize("module", FACADE)
def test_no_facade_module_reaches_a_router_by_any_chain(module: str) -> None:
    """The chain this closes was `graphql_schema -> governed_execution -> tool_api`."""
    paths = _reachable(module)
    reached = {name: chain for name, chain in paths.items() if name in ROUTERS and name != module}
    assert reached == {}, "; ".join(" -> ".join(chain) for chain in reached.values())


def test_the_walk_would_see_a_router_it_could_reach() -> None:
    """The negative test above is worth what this is: the same walk from a module that does
    import a router reports it."""
    paths = _reachable("aida.tool_plans_api")
    assert "aida.tool_api" in paths or paths.keys() & ROUTERS


def test_the_contract_holds_the_whole_facade() -> None:
    """The walk above reads this repository; the contract reads the installed graph. Both name
    the same modules, so neither is the only thing standing between GraphQL and a router."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    (contract,) = [
        item
        for item in config["tool"]["importlinter"]["contracts"]
        if item["name"].startswith("R11-GQL01")
    ]
    sources: set[str] = set(contract["source_modules"])
    assert {"aida.graphql_schema", "aida.governed_execution", "aida.tool_execution"} <= sources
    assert set(FACADE) - {"aida.graphql_api"} <= sources


def test_the_router_still_serves_the_name_its_importers_use() -> None:
    """`tool_api` re-exports both names, declared in `__all__` so a type checker treats them as
    exported; four modules and two test suites import them from there."""
    assert set(tool_api.__all__) == {"execute_tool_version", "_enforce_agent_contract"}
    assert sys.modules["aida.tool_api"].execute_tool_version is tool_execution.execute_tool_version


def test_the_execution_path_moved_whole() -> None:
    """The service holds the function and the agent-contract check it makes first, and the
    router holds neither: one definition, not two that can drift."""
    router_source = (SOURCE / "tool_api.py").read_text(encoding="utf-8")
    service_source = (SOURCE / "tool_execution.py").read_text(encoding="utf-8")
    defined: dict[str, set[str]] = {}
    for label, source in (("router", router_source), ("service", service_source)):
        defined[label] = {
            node.name
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
    moved = {"execute_tool_version", "_enforce_agent_contract"}
    assert moved <= defined["service"]
    assert moved & defined["router"] == set()


def test_the_service_is_not_named_like_a_router() -> None:
    """`tool_execution`, not `tool_execution_api`: the forbidden list is the enumerated `*_api`
    set, so a service named like a router would be forbidden to the facade it serves."""
    value: Any = tool_execution.__name__
    assert not value.endswith("_api")


def test_the_tools_listing_no_longer_reports_a_write_it_never_makes() -> None:
    """A side effect of the move, recorded because the surface matrix is evidence.

    `list_tools` is a read: it gets a project, counts and selects. The matrix reported it as
    writing, auditing and workspace-gated, because the call-graph walk resolves a method call by
    name within the modules its own module imports -- and `tool_api` imported the query gateway
    for the execution path, so this route's `session.execute(...)` resolved to
    `QueryExecutionGateway.execute`. The execution path left the module and took that import
    with it, so the row now says what the route does.
    """
    from tests.support.app_surface import reaches_call, reaches_session_write

    assert not reaches_session_write("aida.tool_api", "list_tools")
    assert not reaches_call("aida.tool_api", "list_tools", frozenset({"QueryExecutionGateway"}))
    matrix = (REPO_ROOT / "Docs" / "50-security" / "surface-control-matrix.md").read_text(
        encoding="utf-8"
    )
    row = next(
        line
        for line in matrix.splitlines()
        if line.startswith("| `GET /v1/projects/{project_id}/tools` |")
    )
    cells = [cell.strip() for cell in row.strip("| ").split(" | ")]
    assert (cells[5], cells[6], cells[7]) == ("no", "read", "no"), row
    # The execution route, which does all three, still says so.
    assert reaches_session_write("aida.tool_execution", "execute_tool_version")
