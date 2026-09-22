"""R11-GQL01: the compile route's read decision has one implementation.

`context_compiler_api._load_source` (REST, and the OKF store through it) and the coverage read in
`graphql_reads` both decide "may this caller read this version, and what does it pin?". Until
2026-09-19 GraphQL rebuilt that decision from the shared checks
(`graphql_reads._compiled_read_decision`), because a router module cannot be imported by a GraphQL
module, and only parity tests held the two copies in line.

The decision now lives in `aida.context_product_read_service`, and both surfaces call it. These
tests are structural, in the manner of `tests/test_okf_source_bundles.py`'s one-door tests: they
fail if either surface stops calling the shared service, or if a copy of one of the decision's
checks reappears beside it -- the failure mode the parity tests could only notice after the
copies had already diverged. The behavioural half (the same refusals on both surfaces, for every
reader class) stays in `tests/test_graphql_coverage.py`.
"""

import ast
import tomllib
from pathlib import Path
from typing import Any

import pytest

from aida import context_compiler_api, context_product_read_service, graphql_reads, okf_store
from tests.support.app_surface import reaches_call
from tests.test_graphql_api import _module_imports

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE = "aida.context_product_read_service"

#: Names that only the read decision uses. Any of them appearing in a surface's own code is a
#: second copy of a check the service owns.
_DECISION_CHECKS = frozenset(
    {
        "_enforce_capability_envelope",
        "evaluate_context_product_purpose",
        "evaluate_context_product_quality_from_db",
        "allowed_consumer_roles",
        "quality_requirements",
        "policy_summary",
        "lifecycle_status",
        "load_routine_references",
        "load_view_coverage",
        "load_source_freshness",
        "load_ontology_meaning",
        "load_pinned_meaning",
        "load_coverage_changes",
    }
)


def _names_used(module: str) -> set[str]:
    """Every name the module's code mentions -- not the names it imports."""
    tree = ast.parse((REPO_ROOT / "src" / "aida" / f"{module}.py").read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


def _defined_functions(module: str) -> set[str]:
    tree = ast.parse((REPO_ROOT / "src" / "aida" / f"{module}.py").read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


@pytest.mark.parametrize(
    "handler",
    [
        "compile_context_product_version",
        "download_context_compilation",
        "inspect_context_compilation_drift",
    ],
)
def test_every_compile_route_reads_through_the_shared_decision(handler: str) -> None:
    assert reaches_call("aida.context_compiler_api", handler, frozenset({"_load_source"}))
    assert reaches_call("aida.context_compiler_api", handler, frozenset({"decide_compiled_read"}))
    assert reaches_call(
        "aida.context_compiler_api", handler, frozenset({"resolve_pinned_references"})
    )


def test_graphql_coverage_reads_through_the_same_decision_and_resolution() -> None:
    for target in ("decide_compiled_read", "resolve_pinned_references", "load_coverage_extras"):
        assert reaches_call(
            "aida.graphql_reads", "get_context_product_coverage", frozenset({target})
        ), target


def test_the_okf_store_scope_resolver_is_the_same_decision() -> None:
    """The product bundle read is the third caller: it resolves scope with the compile route's
    own load, and reaches it through the service rather than through a router."""
    assert reaches_call("aida.okf_store", "read_published_bundle", frozenset({"_load_source"}))
    assert reaches_call(
        "aida.okf_store", "read_published_bundle", frozenset({"decide_compiled_read"})
    )
    assert "aida.context_compiler_api" not in _module_imports(
        REPO_ROOT / "src" / "aida" / "okf_store.py"
    )
    assert okf_store._load_source is context_product_read_service._load_source


def test_the_callers_hold_the_services_own_objects_not_copies() -> None:
    """Identity, not equality: a copy that happens to hold the same members today is the copy
    that drifts tomorrow."""
    assert context_compiler_api._load_source is context_product_read_service._load_source
    assert context_compiler_api.COMPILER_ROLES is context_product_read_service.COMPILER_ROLES
    assert graphql_reads.CONTEXT_COMPILER_ROLES is context_product_read_service.COMPILER_ROLES
    assert (
        graphql_reads.CONTEXT_COMPILER_LIFECYCLE_READERS
        is context_product_read_service.COMPILER_LIFECYCLE_READERS
    )
    assert graphql_reads.decide_compiled_read is context_product_read_service.decide_compiled_read
    assert (
        graphql_reads.resolve_pinned_references
        is context_product_read_service.resolve_pinned_references
    )
    assert (
        context_compiler_api.load_coverage_extras
        is context_product_read_service.load_coverage_extras
        is graphql_reads.load_coverage_extras
    )


@pytest.mark.parametrize("module", ["context_compiler_api", "graphql_reads"])
def test_neither_surface_holds_a_copy_of_the_decisions_checks(module: str) -> None:
    """The surface calls the decision; it does not perform any of the decision's checks."""
    assert _names_used(module) & _DECISION_CHECKS == set()


def test_the_copy_graphql_used_to_hold_is_gone() -> None:
    assert "_compiled_read_decision" not in _defined_functions("graphql_reads")
    assert "_load_source" not in _defined_functions("context_compiler_api")
    assert "_load_source" in _defined_functions("context_product_read_service")


def test_the_service_imports_no_router() -> None:
    """The reason it exists: a module GraphQL and the OKF store may import needs to be one
    neither has to import a router to reach."""
    imported = _module_imports(REPO_ROOT / "src" / "aida" / "context_product_read_service.py")
    routers = {
        name
        for name in imported
        if (name.endswith("_api") or name in {"aida.api", "aida.mcp_server"})
        and name != "aida.graphql_api"
    }
    assert routers == set()
    assert SERVICE not in {name for name in imported if name.endswith("_api")}


def _read_service_contract() -> dict[str, Any]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    (contract,) = [
        item
        for item in config["tool"]["importlinter"]["contracts"]
        if item["name"].startswith("R11-GQL01")
    ]
    found: dict[str, Any] = contract
    return found


def test_an_import_contract_pins_the_shared_read_services_away_from_every_router() -> None:
    """The structural tests above read what these modules import directly; the import-linter
    contract reads every chain. It exists so the next shared decision is a service and not an
    import back into the router it lives beside -- and it names every `*_api` module, because an
    import-linter wildcard cannot match a suffix, so a router added later is caught here rather
    than forgotten there."""
    contract = _read_service_contract()
    assert contract["type"] == "forbidden"
    assert {
        "aida.context_product_read_service",
        "aida.context_product_reads",
        "aida.graphql_okf",
        "aida.graphql_reads",
        "aida.okf_read_model",
        "aida.okf_store",
    } <= set(contract["source_modules"])
    routers = {
        f"aida.{path.stem}"
        for path in (REPO_ROOT / "src" / "aida").glob("*_api.py")
        if path.stem != "graphql_api"
    }
    missing = routers - set(contract["forbidden_modules"])
    assert missing == set(), f"add these routers to the R11-GQL01 contract: {sorted(missing)}"
    assert {"aida.api", "aida.mcp_server", "aida.context_compiler_api"} <= set(
        contract["forbidden_modules"]
    )


def test_the_decisions_one_reader_set_is_the_compile_routes() -> None:
    """The set a lifecycle reader is drawn from is the route's, member for member: it is what
    lets a steward read a draft's coverage over both surfaces."""
    assert context_product_read_service.COMPILER_LIFECYCLE_READERS == frozenset(
        {"PlatformAdmin", "MetadataAdmin", "DataSteward"}
    )
    assert set(context_product_read_service.COMPILER_LIFECYCLE_READERS) <= set(
        context_product_read_service.COMPILER_ROLES
    )
