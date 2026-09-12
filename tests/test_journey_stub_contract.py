"""The browser journey suite's least-privilege model cannot drift from the app's.

`scripts/journey_stub_api.py` seats an identity per journey step and refuses
the calls that identity is not entitled to make, so that the browser suite
(tracker R11-B11) can check the one thing no backend test can: that a 403 is
**visible on the screen** rather than swallowed into an empty state.

That is only worth anything if the roles the stub demands are the roles the
application actually demands. A stub with an invented permission model would
produce a green browser suite that proves the UI renders refusals the real
deployment would never send -- or, worse, lets a step through that the real
deployment refuses.

So every rule in the stub cites a surface in
`Docs/50-security/surface-control-matrix.md` -- a generated file, derived from
the live FastAPI application's own `require_roles` dependencies -- and this
test asserts the two still agree. When someone changes a route's role tuple in
`src/`, the matrix is regenerated, and this test fails until the stub is
updated to match.

It reads the matrix rather than importing the app: the matrix is the
already-audited projection of those dependencies, and parsing it keeps this
gate stdlib-only and fast, exactly like the other configuration gates.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from journey_stub_api import IDENTITIES, ROUTE_RULES  # noqa: E402

MATRIX = REPO_ROOT / "Docs" / "50-security" / "surface-control-matrix.md"


def _matrix_roles() -> dict[str, tuple[str, ...]]:
    """`Surface` -> the `Required roles` tuple, from the generated matrix."""
    rows: dict[str, tuple[str, ...]] = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        surface = cells[0].strip("`")
        roles_cell = cells[3]
        if roles_cell in {"none declared", "unknown"}:
            rows[surface] = ()
            continue
        rows[surface] = tuple(
            sorted(role.strip() for role in roles_cell.split(",") if role.strip())
        )
    return rows


@pytest.fixture(scope="module")
def matrix() -> dict[str, tuple[str, ...]]:
    assert MATRIX.exists(), f"the surface-control matrix is missing at {MATRIX}"
    rows = _matrix_roles()
    assert rows, "no surface rows parsed out of the matrix -- has its table format changed?"
    return rows


def test_every_stub_rule_names_a_real_surface(matrix: dict[str, tuple[str, ...]]) -> None:
    """A rule citing a surface that no longer exists is a rule nobody can check."""
    unknown = sorted({rule.surface for rule in ROUTE_RULES if rule.surface not in matrix})
    assert not unknown, (
        "these surfaces are cited by scripts/journey_stub_api.py but are not in the "
        f"surface-control matrix (renamed or removed routes?): {unknown}"
    )


def test_stub_role_tuples_match_the_application(matrix: dict[str, tuple[str, ...]]) -> None:
    """The seat the browser suite tests is the seat the application enforces."""
    drifted: list[str] = []
    for rule in ROUTE_RULES:
        expected = matrix.get(rule.surface)
        if expected is None:
            continue  # reported by the test above
        actual = tuple(sorted(rule.roles))
        if actual != expected:
            drifted.append(
                f"  {rule.surface}\n"
                f"    stub says:   {actual or '()'}\n"
                f"    matrix says: {expected or '()'}"
            )
    assert not drifted, (
        "scripts/journey_stub_api.py no longer agrees with the application's required "
        "roles. Update the stub's ROUTE_RULES to match, then re-check whether the "
        "identities in IDENTITIES can still perform their journey step:\n"
        + "\n".join(drifted)
    )


def test_no_identity_is_an_administrator() -> None:
    """The point of the suite is least privilege; a broad seat would void it.

    `PlatformAdmin` (and the other blanket roles) would satisfy nearly every
    rule in the matrix, so an identity holding one would make the browser
    journey pass regardless of whether the application authorized anything.
    """
    forbidden = {"PlatformAdmin", "OrganizationAdmin", "ProjectAdmin"}
    offenders = {
        name: sorted(roles & forbidden) for name, roles in IDENTITIES.items() if roles & forbidden
    }
    assert not offenders, (
        "these journey identities hold an administrator role, which would let the "
        f"browser suite pass without proving anything about authorization: {offenders}"
    )


def test_each_identity_is_actually_restricted() -> None:
    """Every seat must be refused something, or it is not a least-privilege seat."""
    unrestricted: list[str] = []
    for name, roles in IDENTITIES.items():
        refused = [
            rule.surface for rule in ROUTE_RULES if rule.roles and roles.isdisjoint(rule.roles)
        ]
        if not refused:
            unrestricted.append(name)
    assert not unrestricted, (
        "these identities are entitled to every surface the stub knows about, so no "
        f"denial can be observed through them: {unrestricted}"
    )
