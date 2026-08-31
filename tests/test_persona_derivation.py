"""UX-1: persona navigation bound to the bank OIDC group contract (module 21 SS5).

`oidc.context_from_claims` already turns verified OIDC claims into roles via a
configurable claim path (`oidc_roles_claim`) plus a mapping dict (`oidc_role_mappings`).
This extends that exact mechanism -- not a parallel one -- to derive the shell's
persona from the verified groups claim, so persona is server-derived in production and
never a value the browser can pick for itself.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from aida.config import Settings
from aida.oidc import OidcVerificationError, OidcVerifier, context_from_claims
from aida.persona_api import get_me
from aida.security_types import SecurityContext


def _bank_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json=json.dumps({"keys": [_JWK]}),
        oidc_groups_claim="groups",
        oidc_persona_mappings={
            "BANK_DATA_STEWARDS": "Steward",
            "BANK_RISK_ANALYSTS": "Analyst",
            "BANK_REVIEWERS": "Reviewer",
        },
    )
    defaults.update(overrides)
    return Settings(**defaults)


_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key()))
_JWK.update({"kid": "bank-key-1", "use": "sig", "alg": "RS256"})


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "sub": "bank-user-123",
        "iss": "https://identity.bank.example",
        "aud": "atlas",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Group -> persona derivation via the configurable claim-path mechanism
# ---------------------------------------------------------------------------


def test_principal_with_mapped_group_derives_the_configured_persona() -> None:
    settings = _bank_settings()

    context = context_from_claims(
        _claims(groups=["BANK_DATA_STEWARDS"]),
        settings,
    )

    assert context.persona == "Steward"


def test_a_different_group_derives_a_different_persona() -> None:
    settings = _bank_settings()

    context = context_from_claims(_claims(groups=["BANK_RISK_ANALYSTS"]), settings)

    assert context.persona == "Analyst"


def test_first_mapped_group_in_claim_order_wins_when_several_groups_are_present() -> None:
    settings = _bank_settings()

    context = context_from_claims(
        _claims(groups=["UNMAPPED_GROUP", "BANK_REVIEWERS", "BANK_DATA_STEWARDS"]),
        settings,
    )

    assert context.persona == "Reviewer"


def test_unmapped_groups_with_no_default_derive_no_persona() -> None:
    settings = _bank_settings()

    context = context_from_claims(_claims(groups=["SOME_OTHER_GROUP"]), settings)

    assert context.persona is None


def test_unmapped_groups_fall_back_to_the_configured_default_persona() -> None:
    settings = _bank_settings(oidc_default_persona="Analyst")

    context = context_from_claims(_claims(groups=["SOME_OTHER_GROUP"]), settings)

    assert context.persona == "Analyst"


def test_a_default_persona_outside_the_recognized_set_is_ignored() -> None:
    settings = _bank_settings(oidc_default_persona="NotARealPersona")

    context = context_from_claims(_claims(groups=[]), settings)

    assert context.persona is None


def test_a_group_mapped_to_an_unrecognized_persona_name_is_ignored() -> None:
    settings = _bank_settings(
        oidc_persona_mappings={"BANK_TYPO_GROUP": "NotAPersona"},
    )

    context = context_from_claims(_claims(groups=["BANK_TYPO_GROUP"]), settings)

    assert context.persona is None


def test_groups_claim_honors_a_configurable_claim_path_like_the_roles_claim_does() -> None:
    settings = _bank_settings(
        oidc_groups_claim="bank.entitlements.groups",
        oidc_persona_mappings={"BANK_DATA_STEWARDS": "Steward"},
    )

    context = context_from_claims(
        _claims(bank={"entitlements": {"groups": ["BANK_DATA_STEWARDS"]}}),
        settings,
    )

    assert context.persona == "Steward"


def test_comma_separated_groups_claim_is_accepted_like_the_roles_claim() -> None:
    settings = _bank_settings()

    context = context_from_claims(
        _claims(groups="BANK_RISK_ANALYSTS, BANK_DATA_STEWARDS"),
        settings,
    )

    assert context.persona == "Analyst"


def test_malformed_groups_claim_is_rejected() -> None:
    settings = _bank_settings()

    with pytest.raises(OidcVerificationError, match="groups claim has an invalid shape"):
        context_from_claims(_claims(groups={"not": "a list"}), settings)


@pytest.mark.asyncio
async def test_persona_survives_full_signature_and_claim_verification() -> None:
    settings = _bank_settings()
    organization_id = uuid4()
    token = jwt.encode(
        _claims(
            groups=["BANK_DATA_STEWARDS"],
            organization_id=str(organization_id),
            principal_type="USER",
        ),
        _PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    claims = await OidcVerifier(settings).verify(token)
    context = context_from_claims(claims, settings)

    assert context.persona == "Steward"
    assert context.roles == frozenset()  # no roles claim/mapping configured here


# ---------------------------------------------------------------------------
# GET /v1/me: persona and identity-provider mode surfaced to the shell
# ---------------------------------------------------------------------------


def test_me_endpoint_reports_oidc_derived_persona_and_provider_mode() -> None:
    settings = _bank_settings()
    context = SecurityContext(
        principal_id="bank-user-123",
        principal_type="USER",
        organization_id=None,
        roles=frozenset({"Analyst"}),
        persona="Steward",
    )

    result = asyncio.run(get_me(context=context, settings=settings))

    assert result.identity_provider == "OIDC"
    assert result.persona == "Steward"
    assert result.roles == ["Analyst"]


def test_me_endpoint_reports_no_persona_in_development_mode() -> None:
    settings = Settings(identity_provider="development")
    context = SecurityContext(
        principal_id="dev-user",
        principal_type="USER",
        organization_id=None,
        roles=frozenset({"Analyst"}),
        persona=None,
    )

    result = asyncio.run(get_me(context=context, settings=settings))

    assert result.identity_provider == "DEVELOPMENT"
    assert result.persona is None
