"""`scripts/verify_oidc_bundles.py`: the parts that must hold without an issuer or an API.

The script signs in as each OIDC bundle through the mock issuer's login form (R11-AUD07). That needs
a running issuer and is not run here; `tests/test_oidc_demo_bundles.py` proves the same claim sets
in process. What this holds is the script's own logic, and one tie that keeps it honest: every
bundle the overlay defines is one the script knows how to sign in as, so a bundle added to
`compose.oidc.yaml` cannot be left out of the live check without a test noticing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_oidc_bundles as harness  # noqa: E402


def test_help_prints_usage_and_exits_without_touching_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        harness.main(["--help"])

    assert stopped.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_it_needs_exactly_one_way_to_reach_an_api() -> None:
    with pytest.raises(SystemExit) as neither:
        harness.main([])
    with pytest.raises(SystemExit) as both:
        harness.main(["--api", "http://127.0.0.1:1", "--spawn-api", "8100"])

    assert neither.value.code == 2
    assert both.value.code == 2


def test_it_refuses_a_target_that_is_not_on_loopback() -> None:
    """A mock issuer mints a token for anyone who asks: it must never be pointed at a real host."""
    with pytest.raises(SystemExit) as stopped:
        harness.main(["--api", "http://api.example.com:8000"])

    assert "loopback" in str(stopped.value.code)


def test_every_bundle_the_overlay_defines_is_one_the_script_signs_in_as() -> None:
    overlay = harness.load_overlay(harness.OVERLAY)

    assert set(overlay["role_mappings"]) <= set(harness.BUNDLE_GROUPS)
    # ... and every group it signs in with is one the overlay maps to a persona.
    for bundle, group in harness.BUNDLE_GROUPS.items():
        assert group is None or group in overlay["persona_mappings"], bundle


def test_the_first_mapped_group_picks_the_persona_and_none_means_the_default() -> None:
    overlay = {
        "persona_mappings": {"a-group": "Steward", "b-group": "Auditor"},
        "default_persona": "Analyst",
    }

    assert harness.expected_persona(overlay, ["nobody", "b-group", "a-group"]) == "Auditor"
    assert harness.expected_persona(overlay, []) == "Analyst"
    assert harness.expected_persona(overlay, ["nobody"]) == "Analyst"


def test_a_route_template_gets_the_organization_only_where_it_asks_for_one() -> None:
    organization = "11111111-1111-1111-1111-111111111111"

    filled = harness.fill("/v1/organizations/{organization_id}/things/{thing_id}", organization)

    assert filled.startswith(f"/v1/organizations/{organization}/things/")
    assert "{" not in filled
    assert organization not in filled.split("/things/")[1]


def test_the_admitted_and_refused_picks_never_overlap() -> None:
    routes = [
        {"method": "GET", "path": "/a", "roles": {"Viewer", "Analyst"}},
        {"method": "GET", "path": "/b/{id}", "roles": {"Auditor"}},
        {"method": "GET", "path": "/c", "roles": {"PlatformAdmin"}},
        {"method": "POST", "path": "/d", "roles": {"Analyst"}},
    ]
    roles = {"Viewer"}

    refused = harness.pick(routes, "GET", roles, admitted=False)
    admitted = harness.pick(routes, "GET", roles, admitted=True)

    assert admitted is not None and admitted["path"] == "/a"
    # the simplest refused route: fewest path parameters, then alphabetical
    assert refused is not None and refused["path"] == "/c"
    assert refused["roles"].isdisjoint(roles)
    assert harness.pick(routes, "POST", roles, admitted=True) is None
