"""The compose OIDC overlay's role bundles and persona groups, and the sign-in claims the
walkthrough tells a presenter to type.

`compose.oidc.yaml` is the only place the demo's OIDC identities are defined: nine role
bundles in `AIDA_OIDC_ROLE_MAPPINGS`, five persona groups in `AIDA_OIDC_PERSONA_MAPPINGS`, and
`AIDA_OIDC_DEFAULT_PERSONA`. `Docs/walkthrough/roles-and-users.html#signin` lists, per demo
user, the claims to type into the mock issuer's login form, and `scripts/demo-users.ps1` holds
the same eight users' roles for the development identity provider. Nothing checked that the
three agree, and a mistake in any of them fails quietly: `context_from_claims` keeps only names
in `PLATFORM_ROLES`, so a misspelt role in a bundle is dropped without an error and the user
signs in with less than the page promises. No docker and no network here: the overlay is read
with PyYAML, and each claim set is signed with a freshly generated RSA key and sent through
`OidcVerifier.verify` and `context_from_claims` exactly as `get_security_context` does.

Held to account:

* **Every role a bundle grants is in `PLATFORM_ROLES`**, and each bundle grants exactly what
  the walkthrough says it does (`BUNDLES` below, written out rather than read back from the
  file under test).
* **All five personas can sign in as a least-privilege identity.** The overlay's own comment
  says the six added bundles exist so a Reviewer, Auditor, Operations user or Analyst can be
  minted without `PlatformAdmin`; before them a persona of Auditor with the role Viewer landed
  on a ledger it may not read. `LEAST_PRIVILEGE` names the bundle per persona, and each is
  signed in through a group that maps to that persona.
* **The page's claims, verbatim, sign in as the documented bundle and persona**, and the
  development roster in `scripts/demo-users.ps1` agrees with the OIDC bundles user by user.
* **The mapping is closed.** A token that names a platform role gets nothing, whichever of
  the fifteen it names, alone or beside a mapped bundle; a group never grants a role and a
  bundle name in `groups` never picks a persona; an unknown group gets the default persona
  and no more.

Not shown: what the mock issuer actually puts in a token it mints (its `aud`, its `sub`), that
the browser flow completes, or that the container reads the overlay as the YAML says. The
overlay's own note says the sign-in flow was not driven in the pass that added the bundles.
"""

from __future__ import annotations

import html
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa

from aida.config import Settings
from aida.oidc import (
    PERSONAS,
    PLATFORM_ROLES,
    OidcTokenExpired,
    OidcVerificationError,
    OidcVerifier,
    context_from_claims,
)
from aida.security_types import SecurityContext

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE_OIDC = REPO_ROOT / "compose.oidc.yaml"
ROLES_PAGE = REPO_ROOT / "Docs" / "walkthrough" / "roles-and-users.html"
DEMO_USERS = REPO_ROOT / "scripts" / "demo-users.ps1"
UI_TYPES = REPO_ROOT / "ui-next" / "src" / "lib" / "ui-types.ts"

#: The Northwind organization every claim set on the page carries.
ORG_ID = UUID("9b90b35f-dcf5-49d3-8f0e-2f269987ae87")
KID = "atlas-demo-key"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key()))
_JWK.update({"kid": KID, "use": "sig", "alg": "RS256"})

# ---------------------------------------------------------------------------
# What the walkthrough says each bundle and user is
# ---------------------------------------------------------------------------

#: bundle -> the roles a token signing in with it must end up holding. Roles are additive sets,
#: not a hierarchy, so every working bundle is the working role plus `Viewer`.
BUNDLES: dict[str, frozenset[str]] = {
    "atlas-admin": frozenset(
        {
            "PlatformAdmin",
            "OrganizationAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "SemanticAdmin",
            "DataSteward",
            "Reviewer",
            "MetadataReviewer",
            "Auditor",
            "Operations",
            "Analyst",
            "Viewer",
            "ToolDeveloper",
            "ToolConsumer",
            "AgentDeveloper",
        }
    ),
    "atlas-steward": frozenset({"DataSteward", "MetadataReviewer", "Analyst", "Viewer"}),
    "atlas-viewer": frozenset({"Viewer"}),
    "atlas-reviewer": frozenset({"Reviewer", "Viewer"}),
    "atlas-auditor": frozenset({"Auditor", "Viewer"}),
    "atlas-analyst": frozenset({"Analyst", "Viewer"}),
    "atlas-operations": frozenset({"Operations", "Viewer"}),
    "atlas-dataadmin": frozenset({"DataAdmin", "Viewer"}),
    "atlas-agentdev": frozenset({"AgentDeveloper", "ToolDeveloper", "Analyst", "Viewer"}),
}


@dataclass(frozen=True)
class SignIn:
    """One row of the page's 'Claims to type at sign-in' table."""

    bundle: str
    group: str | None
    persona: str


#: user -> bundle, group and the persona the shell must open as. `vic.viewer` has no group, so
#: he gets the default persona.
SIGN_INS: dict[str, SignIn] = {
    "alex.operator": SignIn("atlas-admin", "atlas-admins", "Operator"),
    "dana.steward": SignIn("atlas-steward", "atlas-stewards", "Steward"),
    "riya.reviewer": SignIn("atlas-reviewer", "atlas-reviewers", "Reviewer"),
    "omar.auditor": SignIn("atlas-auditor", "atlas-auditors", "Auditor"),
    "ana.analyst": SignIn("atlas-analyst", "atlas-analysts", "Analyst"),
    "ravi.dataadmin": SignIn("atlas-dataadmin", "atlas-admins", "Operator"),
    "sam.agentdev": SignIn("atlas-agentdev", "atlas-analysts", "Analyst"),
    "vic.viewer": SignIn("atlas-viewer", None, "Analyst"),
}

#: persona -> the role its own screens are for.
WORKING_ROLE = {
    "Analyst": "Analyst",
    "Steward": "DataSteward",
    "Reviewer": "Reviewer",
    "Auditor": "Auditor",
    "Operator": "Operations",
}

#: persona -> the bundle a presenter signs in with to see that persona without administrator
#: rights. `atlas-operations` has no row on the page (no demo user holds it), so the Operator
#: persona's least-privilege sign-in is exercised here from the overlay alone.
LEAST_PRIVILEGE = {
    "Analyst": "atlas-analyst",
    "Steward": "atlas-steward",
    "Reviewer": "atlas-reviewer",
    "Auditor": "atlas-auditor",
    "Operator": "atlas-operations",
}

#: The two roles only the administrator bundle may carry.
ADMINISTRATOR_ROLES = frozenset({"PlatformAdmin", "OrganizationAdmin"})


# ---------------------------------------------------------------------------
# Reading the three artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Overlay:
    environment: dict[str, str]
    role_mappings: dict[str, list[str]]
    persona_mappings: dict[str, str]
    default_persona: str
    issuer: str
    audience: str


@pytest.fixture(scope="module")
def overlay() -> Overlay:
    document = yaml.safe_load(COMPOSE_OIDC.read_text(encoding="utf-8"))
    environment = document["services"]["api"]["environment"]
    assert isinstance(environment, dict), "services.api.environment is no longer a mapping"
    values = {str(name): str(value) for name, value in environment.items()}
    return Overlay(
        environment=values,
        role_mappings=json.loads(values["AIDA_OIDC_ROLE_MAPPINGS"]),
        persona_mappings=json.loads(values["AIDA_OIDC_PERSONA_MAPPINGS"]),
        default_persona=values["AIDA_OIDC_DEFAULT_PERSONA"],
        issuer=values["AIDA_OIDC_ISSUER"],
        audience=values["AIDA_OIDC_AUDIENCE"],
    )


@pytest.fixture(scope="module")
def page_claims() -> dict[str, dict[str, Any]]:
    """user -> the claims object the page tells a presenter to type, parsed as typed."""
    text = html.unescape(ROLES_PAGE.read_text(encoding="utf-8"))
    section = re.search(r'<h2 id="signin">(.*?)(?=<h2 )', text, re.S)
    assert section, "roles-and-users.html no longer has a section with id=signin"
    rows = re.findall(
        r'<tr><td><b>([\w.]+)</b></td><td class="mono">(\{[^<]*\})</td></tr>', section.group(1)
    )
    assert rows, "the sign-in table was not recognised; the page changed shape"
    claims = {user: json.loads(typed) for user, typed in rows}
    assert len(claims) == len(rows), "a user appears twice in the sign-in table"
    return claims


@pytest.fixture(scope="module")
def roster() -> dict[str, tuple[str, frozenset[str]]]:
    """user -> (persona, roles) from the development roster in scripts/demo-users.ps1."""
    text = DEMO_USERS.read_text(encoding="utf-8")
    entries = re.findall(
        r'Name\s*=\s*"([\w.]+)";\s*Port\s*=\s*\d+;\s*Persona\s*=\s*"(\w+)";\s*Roles\s*=\s*"([\w,]+)"',
        text,
    )
    assert entries, "the roster in demo-users.ps1 was not recognised; the script changed shape"
    return {name: (persona, frozenset(roles.split(","))) for name, persona, roles in entries}


# ---------------------------------------------------------------------------
# Signing a claim set in
# ---------------------------------------------------------------------------


def _settings(overlay: Overlay) -> Settings:
    """The overlay's own values, configured as `test_oidc.py` and `test_persona_derivation.py`
    do, with the JWKS pinned so nothing is fetched."""
    return Settings(
        _env_file=None,
        identity_provider="oidc",
        oidc_issuer=overlay.issuer,
        oidc_audience=overlay.audience,
        oidc_jwks_json=json.dumps({"keys": [_JWK]}),
        oidc_subject_claim="sub",
        oidc_roles_claim="roles",
        oidc_groups_claim="groups",
        oidc_organization_claim="organization_id",
        oidc_role_mappings=overlay.role_mappings,
        oidc_persona_mappings=overlay.persona_mappings,
        oidc_default_persona=overlay.default_persona,
    )


def _token(settings: Settings, claims: dict[str, Any], **registered: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": "someone",
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    payload.update(claims)
    payload.update(registered)
    return jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})


async def _sign_in(settings: Settings, token: str) -> SecurityContext:
    claims = await OidcVerifier(settings).verify(token)
    return context_from_claims(claims, settings)


def _typed(bundle: str, *, group: str | None = None) -> dict[str, Any]:
    typed: dict[str, Any] = {"roles": [bundle], "organization_id": str(ORG_ID)}
    if group is not None:
        typed["groups"] = [group]
    return typed


# ---------------------------------------------------------------------------
# The overlay is well-formed
# ---------------------------------------------------------------------------


def test_the_overlay_switches_the_api_to_oidc_with_an_issuer_and_audience(
    overlay: Overlay,
) -> None:
    assert overlay.environment["AIDA_IDENTITY_PROVIDER"] == "oidc"
    assert overlay.issuer == "http://localhost:8090/atlas"
    assert overlay.audience == "default"
    assert overlay.environment["AIDA_OIDC_JWKS_URL"].startswith("http://mock-oidc:8080/")


def test_the_folded_json_values_have_the_shapes_settings_expects(overlay: Overlay) -> None:
    """`>-` folds the JSON over several lines; it must still be one JSON object of the type
    the `Settings` field declares (`dict[str, list[str]]`, `dict[str, str]`)."""
    assert overlay.role_mappings
    assert all(
        isinstance(bundle, str)
        and isinstance(roles, list)
        and all(isinstance(role, str) for role in roles)
        for bundle, roles in overlay.role_mappings.items()
    )
    assert overlay.persona_mappings
    assert all(
        isinstance(group, str) and isinstance(persona, str)
        for group, persona in overlay.persona_mappings.items()
    )
    assert isinstance(overlay.default_persona, str)


def test_settings_read_the_overlays_raw_strings_from_the_environment(
    overlay: Overlay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container receives the folded strings as environment variables and pydantic-settings
    decodes them. Set them as the container would and compare with what was parsed here."""
    # The overlay does not set the environment name, and a production value would (rightly)
    # refuse its plain-HTTP JWKS URL; this is about decoding, so start from a clean slate.
    monkeypatch.delenv("AIDA_ENVIRONMENT", raising=False)
    for name, value in overlay.environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.identity_provider == "oidc"
    assert settings.oidc_issuer == overlay.issuer
    assert settings.oidc_audience == overlay.audience
    assert settings.oidc_role_mappings == overlay.role_mappings
    assert settings.oidc_persona_mappings == overlay.persona_mappings
    assert settings.oidc_default_persona == overlay.default_persona


# ---------------------------------------------------------------------------
# Roles: every one is a platform role, and each bundle is what the walkthrough says
# ---------------------------------------------------------------------------


def test_every_role_a_bundle_grants_is_a_platform_role(overlay: Overlay) -> None:
    """`context_from_claims` drops any mapped name outside `PLATFORM_ROLES` without an error, so
    a typo here would sign a user in with less than the page promises."""
    outside = {
        bundle: sorted(set(roles) - PLATFORM_ROLES)
        for bundle, roles in overlay.role_mappings.items()
        if set(roles) - PLATFORM_ROLES
    }

    assert outside == {}


def test_the_overlay_defines_exactly_the_documented_bundles(overlay: Overlay) -> None:
    assert set(overlay.role_mappings) == set(BUNDLES)


@pytest.mark.parametrize("bundle", sorted(BUNDLES))
def test_each_bundle_grants_exactly_its_documented_roles(overlay: Overlay, bundle: str) -> None:
    granted = overlay.role_mappings[bundle]

    assert len(granted) == len(set(granted)), "a role is listed twice"
    assert frozenset(granted) == BUNDLES[bundle]


def test_the_admin_bundle_is_the_whole_catalog(overlay: Overlay) -> None:
    """alex.operator holds all fifteen roles, so a role added to the catalog and not to the
    overlay (or the roster) shows up here."""
    assert frozenset(overlay.role_mappings["atlas-admin"]) == PLATFORM_ROLES


def test_only_the_admin_bundle_grants_platform_or_organization_admin(overlay: Overlay) -> None:
    holders = sorted(
        bundle
        for bundle, roles in overlay.role_mappings.items()
        if ADMINISTRATOR_ROLES & set(roles)
    )

    assert holders == ["atlas-admin"]


def test_every_bundle_carries_the_viewer_read_baseline(overlay: Overlay) -> None:
    """Roles are additive, not a hierarchy: `Analyst` does not imply `Viewer`, so each working
    bundle names `Viewer` itself (the overlay's comment says so)."""
    assert [b for b, roles in overlay.role_mappings.items() if "Viewer" not in roles] == []


# ---------------------------------------------------------------------------
# Personas: five groups, five least-privilege bundles
# ---------------------------------------------------------------------------


def test_every_persona_value_and_the_default_are_recognised_personas(overlay: Overlay) -> None:
    """`_persona_from_groups` ignores a group mapped to a name outside `PERSONAS`, and a default
    outside it, without an error."""
    assert set(overlay.persona_mappings.values()) <= PERSONAS
    assert overlay.default_persona in PERSONAS


def test_all_five_personas_are_reachable_from_a_group(overlay: Overlay) -> None:
    assert set(overlay.persona_mappings.values()) == PERSONAS == set(WORKING_ROLE)


def test_the_intended_bundle_and_group_pairs_exist_and_map_to_the_intended_persona(
    overlay: Overlay,
) -> None:
    for user, sign_in in SIGN_INS.items():
        assert sign_in.bundle in overlay.role_mappings, user
        if sign_in.group is None:
            # No group: the persona is the default, and no group may quietly override it.
            assert sign_in.persona == overlay.default_persona, user
        else:
            assert overlay.persona_mappings.get(sign_in.group) == sign_in.persona, user


def test_the_shell_knows_every_persona_the_overlay_can_hand_it(overlay: Overlay) -> None:
    """`aida.oidc.PERSONAS` is 'kept in sync by hand' with the UI's `Persona` union, and the shell
    narrows anything else to no persona. A name the overlay maps that the UI does not declare
    would open the shell with no persona at all."""
    declared = re.search(r"export type Persona\s*=\s*([^;]+);", UI_TYPES.read_text("utf-8"))
    assert declared, "ui-types.ts no longer declares `export type Persona`"
    names = set(re.findall(r'"(\w+)"', declared.group(1)))

    assert names == PERSONAS
    assert set(overlay.persona_mappings.values()) | {overlay.default_persona} <= names


@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_every_persona_has_a_least_privilege_bundle(overlay: Overlay, persona: str) -> None:
    bundle = LEAST_PRIVILEGE[persona]
    roles = frozenset(overlay.role_mappings[bundle])

    assert WORKING_ROLE[persona] in roles
    assert not roles & ADMINISTRATOR_ROLES
    assert roles < frozenset(overlay.role_mappings["atlas-admin"]), "not a strict subset"


@pytest.mark.parametrize("persona", sorted(PERSONAS))
async def test_every_persona_can_sign_in_as_its_least_privilege_bundle(
    overlay: Overlay, persona: str
) -> None:
    """Through the real verifier: the persona's own group plus its least-privilege bundle
    opens as that persona holding that bundle's roles and no administrator role."""
    settings = _settings(overlay)
    [group] = [g for g, mapped in overlay.persona_mappings.items() if mapped == persona]
    bundle = LEAST_PRIVILEGE[persona]

    context = await _sign_in(
        settings, _token(settings, _typed(bundle, group=group), sub=f"least.{persona.lower()}")
    )

    assert context.persona == persona
    assert context.roles == BUNDLES[bundle]
    assert WORKING_ROLE[persona] in context.roles
    assert not context.roles & ADMINISTRATOR_ROLES
    assert context.organization_id == ORG_ID


# ---------------------------------------------------------------------------
# The page's claims, verbatim
# ---------------------------------------------------------------------------


def test_the_page_lists_the_eight_users_with_the_claims_this_test_expects(
    page_claims: dict[str, dict[str, Any]],
) -> None:
    assert set(page_claims) == set(SIGN_INS)
    for user, sign_in in SIGN_INS.items():
        typed = page_claims[user]
        assert typed["roles"] == [sign_in.bundle], user
        assert typed.get("groups") == ([sign_in.group] if sign_in.group else None), user
        assert typed["organization_id"] == str(ORG_ID), user
        assert set(typed) <= {"roles", "groups", "organization_id"}, user


@pytest.mark.parametrize("user", sorted(SIGN_INS))
async def test_the_claims_the_page_says_to_type_sign_in_as_the_documented_user(
    overlay: Overlay, page_claims: dict[str, dict[str, Any]], user: str
) -> None:
    settings = _settings(overlay)
    expected = SIGN_INS[user]

    context = await _sign_in(settings, _token(settings, page_claims[user], sub=user))

    assert context.principal_id == user
    assert context.principal_type == "USER"
    assert context.roles == BUNDLES[expected.bundle]
    assert context.persona == expected.persona
    assert context.organization_id == ORG_ID


@pytest.mark.parametrize("user", sorted(SIGN_INS))
def test_the_development_roster_agrees_with_the_oidc_bundles(
    roster: dict[str, tuple[str, frozenset[str]]], user: str
) -> None:
    """`scripts/demo-users.ps1` sends these roles as headers; the overlay grants them from a
    token. The two ways of being the same user must not drift apart."""
    persona, roles = roster[user]
    expected = SIGN_INS[user]

    assert roles == BUNDLES[expected.bundle]
    assert persona == expected.persona


def test_the_roster_and_the_page_name_the_same_users(
    roster: dict[str, tuple[str, frozenset[str]]], page_claims: dict[str, dict[str, Any]]
) -> None:
    assert set(roster) == set(page_claims) == set(SIGN_INS)


# ---------------------------------------------------------------------------
# The mapping is closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(PLATFORM_ROLES))
async def test_a_token_that_names_a_platform_role_is_granted_nothing(
    overlay: Overlay, role: str
) -> None:
    """The escalation fixed on 2026-09-12, against this overlay's own mapping rather than a
    fixture's: `"roles": ["PlatformAdmin"]` must not be a way in, and neither may any other
    of the fifteen."""
    settings = _settings(overlay)

    context = await _sign_in(
        settings, _token(settings, {"roles": [role], "organization_id": str(ORG_ID)})
    )

    assert context.roles == frozenset()


async def test_a_platform_role_beside_a_mapped_bundle_adds_nothing(overlay: Overlay) -> None:
    settings = _settings(overlay)
    typed = {"roles": ["atlas-steward", "PlatformAdmin", "OrganizationAdmin"]}

    context = await _sign_in(settings, _token(settings, typed, sub="dana.steward"))

    assert context.roles == BUNDLES["atlas-steward"]
    assert not context.roles & ADMINISTRATOR_ROLES


async def test_an_unmapped_platform_role_leaves_no_roles_and_the_default_persona(
    overlay: Overlay,
) -> None:
    settings = _settings(overlay)

    context = await _sign_in(settings, _token(settings, {"roles": ["PlatformAdmin"]}))

    assert context.roles == frozenset()
    assert context.persona == overlay.default_persona


async def test_an_unknown_group_gets_the_default_persona_and_no_more(overlay: Overlay) -> None:
    settings = _settings(overlay)

    context = await _sign_in(
        settings,
        _token(settings, _typed("atlas-viewer", group="no-such-group"), sub="vic.viewer"),
    )

    assert context.persona == overlay.default_persona == "Analyst"
    assert context.roles == BUNDLES["atlas-viewer"]


async def test_an_unknown_group_alone_grants_no_role(overlay: Overlay) -> None:
    settings = _settings(overlay)

    context = await _sign_in(settings, _token(settings, {"groups": ["no-such-group"]}))

    assert context.roles == frozenset()
    assert context.persona == overlay.default_persona


async def test_a_group_name_in_the_roles_claim_grants_nothing(overlay: Overlay) -> None:
    """Every group that picks a persona has a plural name; none is a bundle, so naming one in
    `roles` is not a way to a role."""
    settings = _settings(overlay)

    context = await _sign_in(
        settings, _token(settings, {"roles": sorted(overlay.persona_mappings)})
    )

    assert context.roles == frozenset()


async def test_a_bundle_name_in_the_groups_claim_picks_no_persona(overlay: Overlay) -> None:
    settings = _settings(overlay)

    context = await _sign_in(
        settings, _token(settings, {"roles": ["atlas-viewer"], "groups": sorted(BUNDLES)})
    )

    assert context.persona == overlay.default_persona
    assert context.roles == BUNDLES["atlas-viewer"]


async def test_a_persona_group_selects_navigation_and_grants_no_role(overlay: Overlay) -> None:
    """'Persona comes from the groups claim and only picks navigation.' A Viewer who claims
    the operator group opens the Operator shell holding only Viewer."""
    settings = _settings(overlay)

    context = await _sign_in(
        settings, _token(settings, _typed("atlas-viewer", group="atlas-admins"), sub="vic.viewer")
    )

    assert context.persona == "Operator"
    assert context.roles == BUNDLES["atlas-viewer"]


async def test_the_first_mapped_group_in_claim_order_wins(overlay: Overlay) -> None:
    settings = _settings(overlay)
    typed = {
        "roles": ["atlas-auditor"],
        "groups": ["not-mapped", "atlas-auditors", "atlas-admins"],
    }

    context = await _sign_in(settings, _token(settings, typed))

    assert context.persona == "Auditor"


async def test_the_operations_bundle_signs_in_through_the_operator_group(
    overlay: Overlay,
) -> None:
    """The one bundle with no row on the page: the Operator persona's least-privilege
    sign-in."""
    settings = _settings(overlay)

    context = await _sign_in(
        settings,
        _token(settings, _typed("atlas-operations", group="atlas-admins"), sub="ops.operator"),
    )

    assert context.roles == frozenset({"Operations", "Viewer"})
    assert context.persona == "Operator"


# ---------------------------------------------------------------------------
# The overlay's issuer and audience are enforced
# ---------------------------------------------------------------------------


async def test_a_token_minted_for_another_audience_is_refused(overlay: Overlay) -> None:
    settings = _settings(overlay)
    token = _token(settings, _typed("atlas-admin"), aud="atlas")

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(token)


async def test_the_issuer_is_the_browsers_url_not_the_containers(overlay: Overlay) -> None:
    """The overlay's comment: `iss` is what the browser saw (`localhost:8090`); the JWKS is
    fetched from `mock-oidc:8080`. A token whose `iss` is the container's URL is refused."""
    settings = _settings(overlay)
    token = _token(settings, _typed("atlas-admin"), iss="http://mock-oidc:8080/atlas")

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(token)


async def test_an_expired_token_is_reported_as_expired(overlay: Overlay) -> None:
    settings = _settings(overlay)
    then = datetime.now(UTC) - timedelta(hours=1)
    token = _token(settings, _typed("atlas-admin"), iat=then, exp=then + timedelta(minutes=5))

    with pytest.raises(OidcTokenExpired):
        await OidcVerifier(settings).verify(token)


async def test_a_token_signed_with_another_key_is_refused(overlay: Overlay) -> None:
    settings = _settings(overlay)
    forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": "alex.operator",
            "iss": overlay.issuer,
            "aud": overlay.audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            **_typed("atlas-admin", group="atlas-admins"),
        },
        forger,
        algorithm="RS256",
        headers={"kid": KID},
    )

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(forged)
