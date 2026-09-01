"""INV-5 -- tenant isolation is total (`Docs/10-architecture/01-principles-and-invariants.md`).

**Statement.** Every governed record carries an organization boundary and, where
applicable, legal entity, LOB, and project. Authorization defaults to deny. Cache
keys, graph nodes, vector documents, artifacts, events, logs, and metrics preserve
these boundaries.

**Why it is Tier 0.** Atlas is sold into banks that run one platform across legal
entities which are forbidden by regulation to see each other's data. A leak here is
not a bug report, it is a reportable incident. The quality-attribute ordering in
§4 of the invariants document puts isolation second only to correctness, ahead of
explainability, latency and cost -- "a leak is worse than an outage".

**Why these tests are enumerated rather than sampled.** The specced test is
`test_cross_tenant_denial`: *every* list/read/write endpoint exercised with a
foreign tenant context. A test that checks three hand-picked endpoints stops
covering the system the moment somebody adds a fourth, and says nothing at all
about the ninety-six it never knew existed. Every test in this module therefore
derives its subject list from the live FastAPI application (see
`tests/support/app_surface.py`), so a route added tomorrow is covered tomorrow.

Exclusions are named individually below, with the reason each one carries no
tenant-scoped data. Excluding by explicit list rather than by pattern is
deliberate: an auditor can read the list and disagree with it.
"""

import inspect
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import BaseModel

from aida.config import Settings
from aida.security_types import SecurityContext
from tests.support.app_surface import (
    MUTATING_METHODS,
    iter_api_routes,
    reaches_call,
    reaches_reference,
    route_id,
)
from tests.support.doubles import ExplodingSession, security_context

# --- exclusions, each one argued ------------------------------------------

# Routes that serve no tenant-scoped data at all and therefore have nothing to
# scope. Every entry is an operational or static-catalogue endpoint; none of them
# reads a governed table.
_TENANT_FREE_ROUTES: dict[str, str] = {
    "GET /health/live": "process liveness; returns service name and version only",
    "GET /health/ready": "dependency reachability; returns UP/DOWN strings only",
    "GET /metrics": "Prometheus exposition of process-level counters",
    "GET /v1/ai/runtime-status": (
        "platform security posture derived from Settings -- identity provider, "
        "credential provider, model-route state; no governed table is read"
    ),
    "GET /v1/ai-assessment-templates": (
        "the static AI-assessment questionnaire catalogue, identical for every "
        "tenant; built from module constants, not from a query"
    ),
    "GET /v1/connectors/capability-matrix": (
        "the connector registry's own definitions; process-global, not per-tenant"
    ),
    "POST /v1/context-compiler/validate": (
        "pure validation of a caller-supplied artifact; takes no session and "
        "persists nothing"
    ),
    "POST /v1/studio/parameter-contracts/validate": (
        "pure validation of a caller-supplied tool parameter contract (ST-A4); "
        "takes no session and persists nothing"
    ),
    "POST /v1/organizations": (
        "creates the organization boundary itself, so there is no prior boundary "
        "to check against; gated to PlatformAdmin by require_roles"
    ),
}

# The three routes above that additionally have no principal at all. Kept as a
# separate, deliberately tiny list: an unauthenticated route is a much stronger
# claim than an unscoped one, and this list is the one an auditor should read first.
_UNAUTHENTICATED_ROUTES = frozenset(
    {"GET /health/live", "GET /health/ready", "GET /metrics"}
)

_TENANT_GATE_CALLS = frozenset({"enforce_organization", "require_organization", "authorize"})
_TENANT_GATE_REFERENCES = frozenset({"organization_id"})


def _route_key(route: APIRoute) -> str:
    return f"{sorted(route.methods)[0]} {route.path}"


def _dependants(dependant: Any) -> list[Any]:
    collected = [dependant]
    for sub in dependant.dependencies:
        collected.extend(_dependants(sub))
    return collected


# --- every route is behind an identity ------------------------------------


def test_every_route_requires_an_authenticated_principal() -> None:
    """INV-5, first clause: authorization defaults to deny, which is impossible
    for a route that never resolves a principal.

    Walks the dependency tree FastAPI built for every mounted route and requires
    that `get_security_context` (directly, or via the closure `require_roles`
    returns) appears in it. Prevents the failure where a new router is included
    without a security dependency and is anonymous to the whole internet.
    """
    anonymous = []
    for route in iter_api_routes():
        qualnames = {
            getattr(dependant.call, "__qualname__", "") or ""
            for dependant in _dependants(route.dependant)
        }
        secured = any(
            "get_security_context" in name or "require_roles" in name for name in qualnames
        )
        if not secured and _route_key(route) not in _UNAUTHENTICATED_ROUTES:
            anonymous.append(route_id(route))
    assert anonymous == [], (
        "these routes resolve no principal and are therefore anonymous; add a "
        f"security dependency or justify them in _UNAUTHENTICATED_ROUTES: {anonymous}"
    )


def test_the_unauthenticated_route_list_stays_closed() -> None:
    """Guards the exclusion list above rather than the application: if the set of
    genuinely anonymous routes ever grows, that is an architecture decision and
    must be made explicitly here, not absorbed silently by the test that reads it.
    """
    actual = set()
    for route in iter_api_routes():
        qualnames = {
            getattr(dependant.call, "__qualname__", "") or ""
            for dependant in _dependants(route.dependant)
        }
        if not any(
            "get_security_context" in name or "require_roles" in name for name in qualnames
        ):
            actual.add(_route_key(route))
    assert actual == set(_UNAUTHENTICATED_ROUTES)


# --- cross-tenant denial, driven against every organization-scoped route ----

_ORGANIZATION_SCOPED_ROUTES = [
    route for route in iter_api_routes() if "{organization_id}" in route.path
]


def _foreign_call_arguments(route: APIRoute, foreign_organization: UUID) -> dict[str, Any]:
    """Build a call for `route.endpoint` from a tenant that does not own it.

    Request bodies are built with `model_construct`, which skips validation. That
    is intentional: a correctly-ordered handler denies the foreign tenant before
    it looks at the body at all, so a body that would not validate is exactly the
    right probe. A handler that reads the body first will raise `AttributeError`
    here and fail this test, which is the correct outcome -- ordering the tenancy
    check after request processing is the bug this test exists to catch.
    """
    arguments: dict[str, Any] = {}
    for name, parameter in inspect.signature(route.endpoint).parameters.items():
        annotation = parameter.annotation
        if name == "organization_id":
            arguments[name] = foreign_organization
        elif annotation is SecurityContext:
            # Every non-PlatformAdmin role at once: this must be denied on the
            # tenant boundary, not incidentally on a missing role.
            arguments[name] = security_context(
                organization_id=uuid4(),
                roles=frozenset(
                    {
                        "OrganizationAdmin",
                        "DataAdmin",
                        "MetadataAdmin",
                        "DataSteward",
                        "Analyst",
                        "AgentDeveloper",
                        "Operations",
                        "Auditor",
                        "Viewer",
                    }
                ),
            )
        elif annotation is Settings:
            arguments[name] = Settings(_env_file=None)
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            arguments[name] = annotation.model_construct()
        elif annotation is UUID:
            arguments[name] = uuid4()
        elif parameter.default is not inspect.Parameter.empty and not hasattr(
            parameter.default, "dependency"
        ):
            arguments[name] = parameter.default
        elif annotation is str:
            arguments[name] = "probe"
        elif annotation is int:
            arguments[name] = 1
        elif annotation is bool:
            arguments[name] = False
        else:
            # Sessions and anything else unrecognised: a double that fails the
            # test on first use.
            arguments[name] = ExplodingSession()
    return arguments


@pytest.mark.parametrize(
    "route",
    _ORGANIZATION_SCOPED_ROUTES,
    ids=[route_id(route) for route in _ORGANIZATION_SCOPED_ROUTES],
)
async def test_cross_tenant_denial(route: APIRoute) -> None:
    """INV-5: every endpoint that names an organization denies a caller from a
    different one -- before it touches the database.

    Drives the real handler with a foreign organization in the path and a session
    that raises on first use. A 403 proves two things at once: the boundary is
    enforced, and it is enforced ahead of any data access, so a mis-scoped query
    cannot leak rows while the check is still pending.

    Prevents the classic multi-tenant failure of trusting a path parameter, and
    the subtler one of checking the boundary only after the query has run.
    """
    with pytest.raises(HTTPException) as denied:
        result = route.endpoint(**_foreign_call_arguments(route, uuid4()))
        if inspect.isawaitable(result):
            await result

    assert denied.value.status_code == 403, (
        f"{route_id(route)} answered a foreign tenant with "
        f"{denied.value.status_code}: {denied.value.detail}"
    )
    assert "cross-organization" in str(denied.value.detail)


def test_the_organization_scoped_route_set_is_not_empty() -> None:
    """Sanity check on the parametrization above: if the route enumeration ever
    returns nothing, `test_cross_tenant_denial` would report zero failures while
    checking nothing at all.
    """
    assert len(_ORGANIZATION_SCOPED_ROUTES) >= 40


# --- every remaining route reaches a boundary check -------------------------


def test_every_route_reaches_a_tenant_boundary_check() -> None:
    """INV-5, for the routes whose tenant is implied by the resource rather than
    named in the path (`/v1/tables/{table_id}/columns`, and 150 others).

    Those cannot be probed by handing them a foreign organization -- there is no
    organization argument to poison -- so the property is proven structurally
    instead: the handler must reach `enforce_organization`, `require_organization`,
    the policy engine's `authorize`, or an `organization_id` filter, following the
    call graph through the module-private loaders (`_load_datasource`, `_source`,
    `_table`, `_version_scope`) where the enforcement actually lives.

    Prevents a new endpoint that resolves a resource by primary key and returns it
    without ever asking whose tenant it belongs to.
    """
    unscoped = []
    for route in iter_api_routes():
        if _route_key(route) in _TENANT_FREE_ROUTES:
            continue
        endpoint = route.endpoint
        module, name = endpoint.__module__, endpoint.__name__
        gated = reaches_call(module, name, _TENANT_GATE_CALLS) or reaches_reference(
            module, name, _TENANT_GATE_REFERENCES
        )
        if not gated:
            unscoped.append(route_id(route))
    assert unscoped == [], (
        "these routes never reach a tenant boundary check; scope them, or add "
        f"them to _TENANT_FREE_ROUTES with a reason: {unscoped}"
    )


def test_the_tenant_free_route_list_stays_closed() -> None:
    """The exclusion list above is the only thing standing between this suite and
    a false pass, so it is itself asserted: every entry must still name a mounted
    route, and every entry must still genuinely fail to reach a boundary check.

    Without this, an entry that stopped being true -- a route deleted, or one that
    grew a proper check -- would sit in the list forever, training the next reader
    to trust a list that no longer means anything.
    """
    mounted = {_route_key(route) for route in iter_api_routes()}
    stale = sorted(set(_TENANT_FREE_ROUTES) - mounted)
    assert stale == [], f"_TENANT_FREE_ROUTES names routes that no longer exist: {stale}"

    # This direction uses only the call-based predicate, not the reference-based
    # one. `reaches_reference` is deliberately permissive -- it accepts any mention
    # of `organization_id`, because the boundary is sometimes a query filter rather
    # than a call -- and permissiveness in the accepting direction only ever makes
    # the test above weaker. Reusing it here would instead produce false failures:
    # `create_organization` mentions `organization_id` when it stamps the audit and
    # outbox rows for the organization it just created, which is not a boundary
    # check on a pre-existing tenant.
    now_scoped = []
    for route in iter_api_routes():
        key = _route_key(route)
        if key not in _TENANT_FREE_ROUTES:
            continue
        endpoint = route.endpoint
        if reaches_call(endpoint.__module__, endpoint.__name__, _TENANT_GATE_CALLS):
            now_scoped.append(key)
    assert now_scoped == [], (
        "these routes now reach a tenant boundary check and no longer need an "
        f"exclusion; remove them from _TENANT_FREE_ROUTES: {now_scoped}"
    )


# --- background workers -----------------------------------------------------


# Workers whose tenant scope is transitive rather than an explicit
# `organization_id` predicate. **Empty.** The distinction is real -- a
# `datasource_id` filter *is* a tenant scope (a datasource belongs to exactly one
# organization) but it relies on the FK rather than restating the boundary, so it
# loses the defence in depth every other query in the platform has.
#
# `aida.workflows.activities.plan_profile_tasks` was the platform's only entry and
# now carries an explicit `MetadataTable.organization_id == run.organization_id`
# predicate (`Docs/review-2026-08/gap/09-inv7-audit-closeout.md` s3). Every
# background worker is now explicitly scoped, with no exemption, and
# `test_every_background_worker_is_tenant_scoped` below fails on the first one that
# is not -- adding an entry here is a visible act, not a quiet one.
_TRANSITIVELY_SCOPED_WORKERS: dict[str, str] = {}


def _temporal_activities() -> list[str]:
    import aida.workflows.activities as activities

    names = []
    for name, value in vars(activities).items():
        if name.startswith("_") or not callable(value):
            continue
        if getattr(value, "__temporal_activity_definition", None) is not None:
            names.append(name)
    return sorted(names)


def test_every_background_worker_is_tenant_scoped() -> None:
    """INV-5 covers background workers as well as endpoints, and workers are the
    more dangerous half: nothing about a Temporal activity carries a caller, so a
    missing scope produces a silent cross-tenant read with no request to trace it
    to.

    Enumerates every registered Temporal activity plus the projector entry points
    and requires each to reach an `organization_id` scope. `profile_table_task`
    and friends resolve their tenancy through the run they were handed, so the
    check follows the call graph rather than the activity body alone.
    """
    subjects = [("aida.workflows.activities", name) for name in _temporal_activities()]
    subjects += [
        ("aida.projectors.graph_projector", "load_projection"),
        ("aida.projectors.graph_projector", "project_discovery"),
        ("aida.projectors.graph_projector", "load_unified_lineage_projection"),
        ("aida.projectors.graph_projector", "project_unified_lineage"),
    ]
    assert len(subjects) > 4, "no Temporal activities were discovered; the scan is broken"

    unscoped = [
        f"{module}.{name}"
        for module, name in subjects
        if not reaches_reference(module, name, _TENANT_GATE_REFERENCES)
    ]
    unexpected = [name for name in unscoped if name not in _TRANSITIVELY_SCOPED_WORKERS]
    assert unexpected == [], f"these background workers are not tenant-scoped: {unexpected}"

    stale = sorted(set(_TRANSITIVELY_SCOPED_WORKERS) - set(unscoped))
    assert stale == [], (
        "these workers now carry an explicit organization_id scope; remove them "
        f"from _TRANSITIVELY_SCOPED_WORKERS: {stale}"
    )


def test_mutating_routes_are_a_meaningful_share_of_the_surface() -> None:
    """Tripwire for the enumeration itself, shared with the INV-7 module: if
    method detection breaks, every "all mutating endpoints" assertion in this
    suite would pass by checking an empty set.
    """
    mutating = [route for route in iter_api_routes() if set(route.methods) & MUTATING_METHODS]
    assert len(mutating) >= 90
