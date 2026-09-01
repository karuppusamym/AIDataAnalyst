"""Enumeration of the platform's runtime surface, for data-driven invariant tests.

A Tier-0 invariant test is only worth its runtime if it covers whatever the
codebase looks like *today*, including the route somebody merged this morning.
Every helper here therefore derives its answer from the live FastAPI application
object or from the parsed source tree -- never from a hand-maintained list.

Two things in here need explaining.

`iter_api_routes` exists because `FastAPI.routes` is no longer a flat list. Since
FastAPI 0.141 an `app.include_router(...)` call leaves a lazy `_IncludedRouter`
placeholder in `app.routes` and only materialises the real `APIRoute` objects when
a request is matched. Walking `original_router.routes` recursively is what gets the
199 real routes back; naively iterating `app.routes` finds three, which would make
every "we checked every endpoint" claim in this suite quietly false.

`reaches_call` exists because the enforcement point for several invariants is not
in the route handler itself -- it is in a module-private helper the handler calls
(`_load_datasource`, `_source`, `_table`, `_version_scope`) or in a service
function in another module (`policy_engine.authorize`, `QueryExecutionGateway.execute`).
A scan that only looked at the handler body would report violations that are not
violations. It walks the call graph across `src/aida` instead, resolving simple
calls against functions defined in the same module, names imported from another
`aida` module, and method names defined on any class in the tree.
"""

import ast
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "aida"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _flatten(routes: list[Any]) -> Iterator[Any]:
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _flatten(list(included.routes))
        else:
            yield route


@lru_cache(maxsize=1)
def iter_api_routes() -> tuple[APIRoute, ...]:
    """Every `APIRoute` mounted on the application, including lazily-included routers."""
    from aida.main import app

    routes = tuple(route for route in _flatten(list(app.routes)) if isinstance(route, APIRoute))
    if len(routes) < 100:  # pragma: no cover - tripwire, not a branch under test
        raise AssertionError(
            f"route enumeration found only {len(routes)} routes; FastAPI's router "
            "layout has changed and every 'we checked every endpoint' assertion in "
            "the Tier-0 suite has silently stopped covering the application"
        )
    return routes


def route_id(route: APIRoute) -> str:
    """Stable, readable identifier used in assertion messages and parametrize ids."""
    method = sorted(route.methods)[0] if route.methods else "?"
    return f"{method} {route.path} -> {route.endpoint.__module__}.{route.endpoint.__name__}"


def _iter_dependants(dependant: Any) -> Iterator[Any]:
    """Every `Dependant` in a route's dependency tree, the route's own included."""
    yield dependant
    for sub in dependant.dependencies:
        yield from _iter_dependants(sub)


_ROLE_GATE_QUALNAMES = frozenset(
    {
        "require_roles.<locals>.dependency",
        # PG-4: a principal holding none of `allowed` directly may still pass on an
        # active, unexpired delegation, but a principal with neither is refused with
        # 403 exactly like `require_roles` -- see `security.require_roles_or_delegated`'s
        # own docstring. It is still a role gate, just one with an extra escape hatch.
        "require_roles_or_delegated.<locals>.dependency",
    }
)


def require_roles_gate(
    route: APIRoute,
) -> tuple[Callable[..., Awaitable[Any]], tuple[str, ...]] | None:
    """The `require_roles(...)` (or `require_roles_or_delegated(...)`) dependency callable
    and its declared role tuple for `route`.

    `None` when the route carries no role-gating dependency at all (AU-7: a handful of
    routes -- health/metrics, `/v1/me`, `/mcp`, and a few administrative actions that
    manually role-check inside the handler body so the denial path can be audited before
    the 403 is raised, see `detokenization_api.py`'s and `access_review_api.py`'s module
    docstrings -- are gated some other way, by design, and are out of scope for a suite
    about `require_roles` specifically).

    `require_roles` (and `require_roles_or_delegated`) are dependency *factories*:
    `require_roles("PlatformAdmin", ...)` returns a closure over `allowed`, and it is that
    closure -- not the factory itself -- that FastAPI wires into
    `route.dependant.dependencies`. The declared role set is therefore not recoverable from
    `route.endpoint`'s signature or from the source text the way most of this module's
    other helpers work: it lives in the closure FastAPI already built, in `__closure__`,
    keyed by the free-variable name in `__code__.co_freevars`. Reading it back this way --
    rather than re-parsing the `require_roles(...)` call with `ast` -- is what makes this
    work for a role set built from an aliased constant (`require_roles(*COMPILER_ROLES)`),
    where the AST at the call site names a variable, not a role.
    """
    for dependant in _iter_dependants(route.dependant):
        call = dependant.call
        if getattr(call, "__qualname__", "") not in _ROLE_GATE_QUALNAMES:
            continue
        freevars = call.__code__.co_freevars
        cells = call.__closure__ or ()
        for name, cell in zip(freevars, cells, strict=True):
            if name == "allowed":
                return call, tuple(cell.cell_contents)
    return None


@dataclass(frozen=True, slots=True)
class _ModuleIndex:
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    imported_from: dict[str, str]
    imported_modules: frozenset[str]


@lru_cache(maxsize=1)
def _source_index() -> dict[str, _ModuleIndex]:
    index: dict[str, _ModuleIndex] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        parts = path.relative_to(SRC_ROOT).with_suffix("").parts
        module = "aida." + ".".join(parts)
        module = module.removesuffix(".__init__")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        imported_from: dict[str, str] = {}
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions.setdefault(node.name, node)
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                        methods.setdefault(f"{node.name}.{member.name}", member)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("aida"):
                imported_modules.add(node.module or "")
                for alias in node.names:
                    imported_from[alias.asname or alias.name] = node.module or ""
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("aida"):
                        imported_modules.add(alias.name)
        index[module] = _ModuleIndex(
            functions, methods, imported_from, frozenset(imported_modules)
        )
    return index


def _definition(module: str, qualified_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    entry = _source_index().get(module)
    if entry is None:
        return None
    if "." in qualified_name:
        return entry.methods.get(qualified_name)
    return entry.functions.get(qualified_name)


def _candidates(module: str, called_name: str) -> list[tuple[str, str]]:
    index = _source_index()
    found: list[tuple[str, str]] = []
    entry = index.get(module)
    if entry is not None:
        if called_name in entry.functions:
            found.append((module, called_name))
        target = entry.imported_from.get(called_name)
        if target in index and called_name in index[target].functions:
            found.append((target, called_name))
    # Method calls are resolved by name, which is inherently ambiguous -- but only
    # within modules this one can actually reach. Searching the whole tree instead
    # would link `session_factory()` in a health check to an unrelated
    # `organization_id` filter three modules away and make the scan meaningless.
    reachable = {module} | (entry.imported_modules if entry is not None else frozenset())
    for other in sorted(reachable):
        other_entry = index.get(other)
        if other_entry is None:
            continue
        for qualified in other_entry.methods:
            if qualified.rsplit(".", 1)[1] == called_name:
                found.append((other, qualified))
    return found


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def reaches_call(
    module: str,
    function: str,
    targets: frozenset[str],
    *,
    max_depth: int = 6,
    _seen: frozenset[tuple[str, str]] | None = None,
    _depth: int = 0,
) -> bool:
    """True when `module.function` can reach a call to any name in `targets`.

    Follows the static call graph across `src/aida` -- same-module functions,
    names imported from other `aida` modules, and method names defined on any
    class in the tree. Depth-bounded so a cycle cannot hang the suite; the bound
    is generous relative to the deepest handler -> helper -> service chain in the
    codebase (three hops).
    """
    seen = _seen or frozenset()
    key = (module, function)
    if key in seen or _depth > max_depth:
        return False
    node = _definition(module, function)
    if node is None:
        return False
    called = _called_names(node)
    if called & targets:
        return True
    seen = seen | {key}
    for name in sorted(called):
        for candidate_module, candidate_name in _candidates(module, name):
            if reaches_call(
                candidate_module,
                candidate_name,
                targets,
                max_depth=max_depth,
                _seen=seen,
                _depth=_depth + 1,
            ):
                return True
    return False


def references_name(module: str, function: str, names: frozenset[str]) -> bool:
    """True when the body of `module.function` mentions any of `names` at all.

    Weaker than `reaches_call` and used only where the enforcement point is a
    *filter expression* (`Model.organization_id == ...`) rather than a call.
    """
    node = _definition(module, function)
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
    return False


def reaches_reference(
    module: str,
    function: str,
    names: frozenset[str],
    *,
    max_depth: int = 6,
    _seen: frozenset[tuple[str, str]] | None = None,
    _depth: int = 0,
) -> bool:
    """`references_name`, followed transitively through the call graph."""
    seen = _seen or frozenset()
    key = (module, function)
    if key in seen or _depth > max_depth:
        return False
    node = _definition(module, function)
    if node is None:
        return False
    if references_name(module, function, names):
        return True
    seen = seen | {key}
    for name in sorted(_called_names(node)):
        for candidate_module, candidate_name in _candidates(module, name):
            if reaches_reference(
                candidate_module,
                candidate_name,
                names,
                max_depth=max_depth,
                _seen=seen,
                _depth=_depth + 1,
            ):
                return True
    return False


_SESSION_WRITE_METHODS = frozenset({"add", "add_all", "delete", "merge", "commit"})


def _writes_via_session(node: ast.AST) -> bool:
    """True when this function body stages or commits ORM state.

    Matched on the receiver as well as the method name. `add` alone is far too
    common -- `set.add`, `Counter.add`, a dozen local helpers -- and matching it
    loosely turns every read endpoint that builds a set into a "mutation",
    which would bury the real INV-7 findings under seventeen false positives.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute) or func.attr not in _SESSION_WRITE_METHODS:
            continue
        receiver = func.value
        name = None
        if isinstance(receiver, ast.Name):
            name = receiver.id
        elif isinstance(receiver, ast.Attribute):
            name = receiver.attr
        if name and name.endswith("session"):
            return True
    return False


# Writers the mutation scan deliberately does not descend into.
#
# Both record an `AuthorizationShadowRecord`: what the authorization engine *would*
# have decided, on a workspace that is not yet enforcing. That is telemetry about a
# decision, not a change to governed state -- nothing downstream reads it, no object
# differs because it exists, and a request that produces one has done exactly what it
# would have done without it.
#
# Excluding them keeps INV-7 pointed at real mutations. Once a read path is gated,
# every gated GET reaches this code, and without the exclusion the mutation scan
# classifies each of them as a write -- which would either bury the genuine findings
# under a growing exemption list, or push the answer towards adding a `record_audit`
# call to every read, which is a second access log at request volume.
#
# The exclusion is not a free pass: the row itself carries principal, kind, action,
# resource and reason code, so it is attributable by construction, and
# `tests/test_inv4_authorization_wiring.py` asserts that it still does. If either
# function ever writes something other than a shadow record, that test fails and this
# list is wrong.
NON_GOVERNED_WRITERS: frozenset[str] = frozenset(
    {"record_divergence", "record_divergence_durably"}
)


def reaches_session_write(
    module: str,
    function: str,
    *,
    max_depth: int = 6,
    _seen: frozenset[tuple[str, str]] | None = None,
    _depth: int = 0,
) -> bool:
    """True when `module.function` can reach a session write, transitively."""
    seen = _seen or frozenset()
    key = (module, function)
    if key in seen or _depth > max_depth:
        return False
    node = _definition(module, function)
    if node is None:
        return False
    if _writes_via_session(node):
        return True
    seen = seen | {key}
    for name in sorted(_called_names(node)):
        if name in NON_GOVERNED_WRITERS:
            continue
        for candidate_module, candidate_name in _candidates(module, name):
            if reaches_session_write(
                candidate_module,
                candidate_name,
                max_depth=max_depth,
                _seen=seen,
                _depth=_depth + 1,
            ):
                return True
    return False
