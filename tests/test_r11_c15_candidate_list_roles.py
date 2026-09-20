"""R11-C15: DataSteward and MetadataReviewer can list what they can already decide.

`decide_relationship_candidate`, `decide_rename_candidate`,
`decide_cross_source_object_resolution_candidate` and
`decide_composite_relationship_candidate` (`intelligence_api.py`) all accept
`PlatformAdmin`, `MetadataReviewer` and `DataSteward`. Their matching list routes, and
`list_organization_data_domains` (`operational_api.py`) which Cross-source needs to offer a
domain to look at, did not: a DataSteward or MetadataReviewer could decide a candidate but
never see the queue it came from, and the domain picker read as empty rather than refused.
`ui-next`'s Cross-source screen turned that 403 into an empty page (`test_okf_source_bundles`'s
sibling report noted the symptom), so the gap looked like "no candidates" rather than "no
access".

Verified against the live FastAPI application object and its real dependency closures
(`tests/support/app_surface.require_roles_gate`), not by re-parsing source text, so an aliased
role-set constant would still be caught correctly.
"""

from __future__ import annotations

import pytest

from tests.support.app_surface import iter_api_routes, require_roles_gate, route_id

#: (method, path) -> the extra roles this row must now include, matching that resource's own
#: decide route.
_LIST_ROUTES = {
    ("GET", "/v1/datasources/{datasource_id}/relationship-candidates"),
    ("GET", "/v1/datasources/{datasource_id}/relationship-candidates/composite"),
    ("GET", "/v1/datasources/{datasource_id}/rename-candidates"),
    ("GET", "/v1/datasources/{datasource_id}/cross-source-object-resolution-candidates"),
    ("GET", "/v1/organizations/{organization_id}/data-domains"),
}

#: The matching decide routes (none scoped under `/datasources/{id}` -- they resolve the
#: datasource from the candidate id itself), whose existing role set every list route above
#: must now equal on these two roles -- proving this closes the asymmetry rather than opening
#: a new one.
_DECIDE_ROUTES = {
    "/v1/relationship-candidates/{candidate_id}/decision",
    "/v1/composite-relationship-candidates/{group_id}/decision",
    "/v1/rename-candidates/{candidate_id}/decision",
    "/v1/cross-source-object-resolution-candidates/{candidate_id}/decision",
}


def _routes_by_path() -> dict[tuple[str, str], object]:
    found = {}
    for route in iter_api_routes():
        for method in route.methods or ():
            found[(method, route.path)] = route
    return found


@pytest.mark.parametrize("method_path", sorted(_LIST_ROUTES))
def test_a_steward_or_reviewer_can_list_what_they_can_decide(
    method_path: tuple[str, str],
) -> None:
    routes = _routes_by_path()
    route = routes.get(method_path)
    assert route is not None, f"route not found: {method_path}"
    gate = require_roles_gate(route)
    assert gate is not None, f"{route_id(route)} carries no require_roles gate"
    _, roles = gate
    assert "DataSteward" in roles, f"{route_id(route)} still refuses DataSteward"
    assert "MetadataReviewer" in roles, f"{route_id(route)} still refuses MetadataReviewer"
    # Widened, not narrowed: every role the route accepted before is still accepted.
    assert "PlatformAdmin" in roles and "Viewer" in roles


def test_the_decide_routes_are_unchanged() -> None:
    """This fix widens the list routes to match the decide routes -- it must not also touch
    the decide routes themselves, which is the surface that already worked."""
    routes = _routes_by_path()
    decide = [route for route in routes.values() if route.path in _DECIDE_ROUTES]
    assert len(decide) == len(_DECIDE_ROUTES), sorted(r.path for r in decide)
    for route in decide:
        gate = require_roles_gate(route)
        assert gate is not None
        _, roles = gate
        assert set(roles) == {"PlatformAdmin", "MetadataReviewer", "DataSteward"}, (
            route_id(route),
            roles,
        )


def test_the_confidence_calibration_route_is_unchanged() -> None:
    """Not part of this fix: reading calibration statistics isn't needed to decide a
    candidate, so it keeps its original, narrower role set."""
    routes = _routes_by_path()
    route = routes[("GET", "/v1/relationship-candidates/confidence-calibration")]
    gate = require_roles_gate(route)
    assert gate is not None
    _, roles = gate
    assert "DataSteward" not in roles
    assert "MetadataReviewer" not in roles
