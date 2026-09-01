"""AU-7 -- behavioural authorization tests for `require_roles`
(`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` s5).

**The finding.** `require_roles` had 348 call sites and zero behavioural tests. Nothing
constructed a principal with the wrong role and asserted 403 -- the presence of a
`require_roles("Viewer")` decorator was trusted as proof the gate worked, when it is only
proof the gate was *written*. A route declared `require_roles("Viewer")` that should be
`PlatformAdmin`-only would have passed every gate in this repo.

**What these tests prove, and what they do not.** For every route in the live app that
carries a `require_roles(...)` dependency, `test_wrong_role_is_denied` constructs a
principal holding a role that is provably not in that route's declared allowed set and
asserts the gate itself raises 403; `test_an_allowed_role_passes_the_role_gate` constructs
a principal holding one of the declared roles and asserts the gate does not reject it. Both
drive the actual `Callable` FastAPI wired into the route's dependency tree -- extracted by
`tests.support.app_surface.require_roles_gate`, which reads it back out of the app object
rather than re-deriving it from source -- not a reimplementation of `require_roles`'s logic,
so a change to the real function (e.g. a broken `isdisjoint`) fails this suite immediately.

This suite is deliberately narrow to the role gate alone, per the tracker's own scoping: the
positive case proves only that `require_roles` does not itself reject an allowed role, not
that the request as a whole would succeed -- a route can still deny that same principal for
tenancy (INV-5, `test_inv5_tenant_isolation.py`) or any other reason. Testing the full stack
is out of scope here on purpose, so a tenancy failure in this route's handler body can never
be mistaken for a role-gate bug reported by this file.

**Why the route list is generated, not hand-maintained.** Same reasoning as INV-5/INV-7:
a hand-maintained list of "routes with the right roles" stops covering the system the moment
someone adds a route tomorrow, and says nothing about the ones it never knew existed. Every
route in `_ROLE_GATED_ROUTES` below comes from `tests.support.app_surface.iter_api_routes`,
so a newly merged `require_roles(...)` call is covered by this suite the same day it lands --
and a newly merged route that *should* carry `require_roles(...)` but does not is either
covered by `test_every_route_requires_an_authenticated_principal` in
`test_inv5_tenant_isolation.py` (if it resolves no principal at all) or must be added, with a
reason, to `_NOT_ROLE_GATED_ROUTES` below (if it resolves one but checks no role) --
`test_the_not_role_gated_route_list_stays_closed` fails loudly the moment that list drifts
from the live app in either direction, so a route cannot quietly fall out of both nets.

**AU-7 audit finding (2026-08-31).** Every mutating route, every route whose path or handler
name names a sensitive concern (`kill-switch`, `security`, `credential`, `policy`,
`organization`, `revoke`, `token`, `admin`, ...), and every `READ_ROLES`/`WRITE_ROLES`-style
paired-constant module was checked by hand against its own module's documented intent before
this suite was written. No route allows a role narrower than its own documentation calls for
and no mutating route grants an unqualified read-only role write access it was not
deliberately given (`graph_perspectives_api.py`'s broad-reader-role writes are a documented
exception: a personal productivity artifact with its own owner-only check inside the handler
body, not a governed object -- see that module's docstring). This is a clean bill of health,
not the absence of a check: see the tracker row for the specific routes inspected.
"""

import pytest
from fastapi import HTTPException

from aida.security_types import SecurityContext
from tests.support.app_surface import iter_api_routes, require_roles_gate, route_id

# A role name that is not, and never will be, a real platform role: it does not appear in
# any `require_roles(...)` call, any `*_ROLES` constant, or `oidc.PLATFORM_ROLES` anywhere in
# `src/aida`. Guaranteed disjoint from every route's declared allowed set by construction, so
# it proves the gate rejects a genuinely wrong role rather than merely a role that happens not
# to be this particular route's -- the same probe works unmodified for a route allowing one
# role and a route allowing nine.
_UNKNOWN_ROLE = "AU7-Probe-Unknown-Role"


def _context(roles: frozenset[str]) -> SecurityContext:
    return SecurityContext(
        principal_id="au7-probe",
        principal_type="USER",
        organization_id=None,
        roles=roles,
    )


def _route_key(route: object) -> str:
    methods = sorted(route.methods) if route.methods else ["?"]  # type: ignore[attr-defined]
    return f"{methods[0]} {route.path}"  # type: ignore[attr-defined]


# Every route the live app gates with `require_roles(...)`, paired with the exact dependency
# callable FastAPI wired in and the role tuple closed over it. Computed once at collection
# time, same as `_ORGANIZATION_SCOPED_ROUTES` in `test_inv5_tenant_isolation.py`.
_ROLE_GATED_ROUTES = [
    (route, gate[0], gate[1])
    for route in iter_api_routes()
    if (gate := require_roles_gate(route)) is not None
]

_ROLE_GATED_IDS = [route_id(route) for route, _call, _allowed in _ROLE_GATED_ROUTES]


# Routes authenticated (`get_security_context`) but not gated by a `require_roles(...)`
# dependency, each with the reason. Mirrors `test_inv5_tenant_isolation.py`'s
# `_TENANT_FREE_ROUTES` convention: an exclusion an auditor can read and disagree with,
# rather than a silent absence from `_ROLE_GATED_ROUTES` above. The three genuinely
# unauthenticated routes (`_UNAUTHENTICATED_ROUTES` in that same module) are included here
# too -- they resolve no principal at all, so a fortiori they check no role.
_NOT_ROLE_GATED_ROUTES: dict[str, str] = {
    "GET /health/live": "process liveness; unauthenticated by design (INV-5)",
    "GET /health/ready": "dependency reachability; unauthenticated by design (INV-5)",
    "GET /metrics": "Prometheus exposition of process-level counters; unauthenticated by design",
    "GET /v1/me": (
        "returns the caller's own resolved identity; any authenticated principal may read "
        "their own security context, so there is no role to gate on"
    ),
    "POST /mcp": (
        "the MCP JSON-RPC transport entrypoint; per-tool authorization happens inside "
        "aida.mcp_server (role and policy checks per call), not at this route"
    ),
    "POST /v1/security/tokens/revoke": (
        "manually role-checked inside the handler against token_revocation_api._ADMIN_ROLES "
        "so a denied revoke attempt is itself audited before the 403 is raised -- a "
        "Depends(require_roles(...)) failure never reaches a handler body, which would make "
        "an unauthorized attempt unaccountable rather than merely refused"
    ),
    "POST /v1/security/tokens/detokenize": (
        "same reasoning as token revoke above; see detokenization_api.py's module docstring "
        "(QG-6) for why this mirrors token_revocation_api.py's shape deliberately"
    ),
    "GET /api/v1/organizations/{organization_id}/consumption-lineage/by-resource": (
        "any authenticated, tenant-scoped principal may read consumption lineage for their "
        "own organization; enforce_organization gates tenancy, there is no role restriction "
        "by design (CX-4)"
    ),
    "GET /api/v1/organizations/{organization_id}/consumption-lineage/by-consumer": (
        "same as consumption-lineage/by-resource above (CX-4)"
    ),
    "GET /api/v1/organizations/{organization_id}/consumption-lineage/graph": (
        "same as consumption-lineage/by-resource above (CX-4)"
    ),
    "POST /v1/access-review/entitlements/generate": (
        "self-service by design (OB-7): any authenticated principal may pull their own "
        "entitlement report with no role restriction; pulling a *different* principal's "
        "report is manually role-checked inside the handler body against "
        "access_review_api._ON_BEHALF_OF_ROLES, mirroring the audited-before-403 shape "
        "token revoke/detokenize already use above"
    ),
    "GET /v1/access-review/reports": (
        "same self-service-by-default shape as entitlements/generate above (OB-7): a "
        "principal with no elevated role only ever sees their own report history, "
        "enforced manually via access_review_api._ON_BEHALF_OF_ROLES, not require_roles"
    ),
    "GET /v1/access-review/reports/{report_id}": (
        "same self-service-by-default shape as entitlements/generate above (OB-7); "
        "enforce_organization gates tenancy and the handler manually checks "
        "_ON_BEHALF_OF_ROLES before returning another principal's report"
    ),
}


def test_the_role_gated_route_set_is_not_empty() -> None:
    """Tripwire for the enumeration itself: if `require_roles_gate` or `iter_api_routes`
    ever silently returned nothing, every test below would pass by parametrizing over an
    empty list, checking nothing at all.
    """
    assert len(_ROLE_GATED_ROUTES) >= 300


def test_every_gate_declares_at_least_one_role() -> None:
    """`require_roles()` called with no arguments is a distinct bug shape from a merely
    wrong role set: `frozenset(...).isdisjoint(())` is always `True`, so an empty allowed
    tuple denies every principal unconditionally, including a legitimate `PlatformAdmin`.
    Nothing today calls `require_roles()` bare, but the property is cheap to hold structurally
    rather than trust by inspection.
    """
    empty = [route_id(route) for route, _call, allowed in _ROLE_GATED_ROUTES if not allowed]
    assert empty == [], f"these routes declare require_roles() with no roles at all: {empty}"


def test_the_not_role_gated_route_list_stays_closed() -> None:
    """The exclusion list above is the only thing standing between this suite and a false
    pass for those ten routes, so it is itself asserted against the live app -- both that
    every entry still names a mounted route, and that the set of routes actually missing a
    `require_roles` gate is exactly this list, no more and no fewer.
    """
    mounted = {_route_key(route) for route in iter_api_routes()}
    stale = sorted(set(_NOT_ROLE_GATED_ROUTES) - mounted)
    assert stale == [], f"_NOT_ROLE_GATED_ROUTES names routes that no longer exist: {stale}"

    actual = {
        _route_key(route) for route in iter_api_routes() if require_roles_gate(route) is None
    }
    assert actual == set(_NOT_ROLE_GATED_ROUTES), (
        "the set of routes with no require_roles gate has changed; add a reason to "
        "_NOT_ROLE_GATED_ROUTES for each newly-unguarded route, or remove an entry that is "
        f"now gated -- added={sorted(actual - set(_NOT_ROLE_GATED_ROUTES))} "
        f"removed={sorted(set(_NOT_ROLE_GATED_ROUTES) - actual)}"
    )


@pytest.mark.parametrize(
    "call,allowed",
    [(call, allowed) for _route, call, allowed in _ROLE_GATED_ROUTES],
    ids=_ROLE_GATED_IDS,
)
async def test_wrong_role_is_denied(call: object, allowed: tuple[str, ...]) -> None:
    """AU-7's core assertion: a principal whose only role is not in `allowed` is refused by
    the gate FastAPI actually wired into this route, with 403 -- not 401, not a silent pass.

    `_UNKNOWN_ROLE` is disjoint from every route's `allowed` tuple by construction (see its
    definition above), so this single probe is valid unmodified across a route declaring one
    role and a route declaring nine: the property under test is "a role outside the declared
    set is rejected", not "this specific other role is rejected".
    """
    with pytest.raises(HTTPException) as denied:
        await call(context=_context(frozenset({_UNKNOWN_ROLE})))  # type: ignore[operator]

    assert denied.value.status_code == 403, (
        f"a principal with no allowed role was answered {denied.value.status_code} "
        f"instead of 403: {denied.value.detail}"
    )
    assert _UNKNOWN_ROLE not in str(allowed)


@pytest.mark.parametrize(
    "call,allowed",
    [(call, allowed) for _route, call, allowed in _ROLE_GATED_ROUTES],
    ids=_ROLE_GATED_IDS,
)
async def test_an_allowed_role_passes_the_role_gate(
    call: object, allowed: tuple[str, ...]
) -> None:
    """The positive half of AU-7: a principal holding one of the declared roles is not
    rejected by the role gate itself.

    Deliberately scoped to the gate alone, per the tracker's exit criterion and the module
    docstring above -- the returned `SecurityContext` proves `require_roles` let this
    principal through, not that the full request would succeed; tenancy and any other
    downstream check are out of scope for this assertion and covered elsewhere (INV-5).
    """
    principal_role = sorted(allowed)[0]
    result = await call(context=_context(frozenset({principal_role})))  # type: ignore[operator]

    assert isinstance(result, SecurityContext)
    assert principal_role in result.roles
