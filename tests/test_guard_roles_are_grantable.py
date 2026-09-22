"""R11-AUD01: every role a guard names is a role a token can carry.

**The defect this pins.** Nine role names sat in route guards without being in
`aida.oidc.PLATFORM_ROLES`, and `context_from_claims` drops any mapped name outside that set, so
no OIDC token could ever hold them. The development identity provider accepts any `X-Roles`
string, so nothing in the local stack or the test suite showed it; a live probe did: on
`GET /v1/organizations/{id}/business-nodes` a `DataSteward` was refused (403) while the
ungrantable `Steward` was admitted. The compliance-pack routes admitted `ComplianceOfficer` and
refused the `Auditor`, whose job is reading that kind of evidence. Nothing compared the guards
with the catalog, because the two were written in different places at different times.

**What this proves.** (1) For every route the live app gates with `require_roles(...)` -- read
back out of the app object by `tests.support.app_surface.require_roles_gate`, the same way the
AU-7 suite does, not re-derived from source -- every declared role is in `PLATFORM_ROLES`.
(2) Every module-level role list in `src` (a constant whose name says it is a set of roles or of
the people who may do something) names only catalog roles, because several such lists are used
outside a `require_roles` call: the inbox and contract readers, the detokenization roles, the
marketplace ranking classes.

**What it does not prove.** It says nothing about whether a route admits the *right* roles, only
that the roles it names exist. A role list built dynamically, or written inline in a handler body
(for instance `context.roles.isdisjoint({...})`), is outside (2); (1) still covers the routes.
"""

import ast
from pathlib import Path

from aida.oidc import PLATFORM_ROLES
from tests.support.app_surface import iter_api_routes, require_roles_gate, route_id

_SRC = Path(__file__).resolve().parents[1] / "src"

# A constant is a role list when its name says so. Suffixes, not a hand-kept list of names, so a
# new `*_READERS` constant is checked the day it is written.
_ROLE_LIST_SUFFIXES = ("_ROLES", "_AUTHORS", "_READERS", "_ASSESSORS", "_USERS", "_ADMIN")

# Constants that end in one of those suffixes and are not lists of platform roles, each with the
# reason. An entry here is an exclusion a reader can disagree with.
_NOT_ROLE_LISTS: dict[str, str] = {
    "_FACT_LIKE_TABLE_ROLES": (
        "the roles a TABLE plays in a dimensional model (FACT, EVENT, SNAPSHOT, TRANSACTION), "
        "matched against `table_role`; no relation to who may call a route"
    ),
}


def test_every_role_a_route_guard_names_is_in_the_platform_catalog() -> None:
    offenders: dict[str, list[str]] = {}
    checked = 0
    for route in iter_api_routes():
        gate = require_roles_gate(route)
        if gate is None:
            continue
        checked += 1
        unknown = sorted(set(gate[1]) - PLATFORM_ROLES)
        if unknown:
            offenders[route_id(route)] = unknown

    assert checked > 300, f"only {checked} role-gated routes were found; the walk is broken"
    assert not offenders, (
        "these routes name a role that is not in `aida.oidc.PLATFORM_ROLES`, so no OIDC token can "
        f"carry it and it grants nothing in a real deployment: {offenders}"
    )


def _string_members(node: ast.expr) -> list[str]:
    """The string literals a role-list expression is made of; a starred name contributes none."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return [name for element in node.elts for name in _string_members(element)]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "tuple", "list"}
        and len(node.args) == 1
    ):
        return _string_members(node.args[0])
    return []


def _module_level_role_lists() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                targets, value = [statement.target], statement.value
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if not target.id.endswith(_ROLE_LIST_SUFFIXES) or target.id in _NOT_ROLE_LISTS:
                    continue
                members = _string_members(value)
                if members:
                    key = f"{path.relative_to(_SRC).as_posix()}::{target.id}"
                    found[key] = members
    return found


def test_every_module_level_role_list_names_only_catalog_roles() -> None:
    lists = _module_level_role_lists()
    offenders = {
        where: sorted(set(members) - PLATFORM_ROLES)
        for where, members in lists.items()
        if set(members) - PLATFORM_ROLES
    }

    assert len(lists) > 20, f"only {len(lists)} role lists were found; the scan is broken"
    assert not offenders, (
        "these module-level role lists name a role that is not in `aida.oidc.PLATFORM_ROLES`, so "
        f"no OIDC token can carry it: {offenders}"
    )


def test_the_scan_would_notice_an_ungrantable_name() -> None:
    """The scan is only worth trusting if it fails when it should."""
    tree = ast.parse('SOME_READERS = ("PlatformAdmin", "NotARealRole", *OTHER_READERS)')
    (statement,) = tree.body
    assert isinstance(statement, ast.Assign)
    members = _string_members(statement.value)

    assert set(members) - PLATFORM_ROLES == {"NotARealRole"}
