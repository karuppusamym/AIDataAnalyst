import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from aida.config import Settings
from aida.oidc import OidcVerificationError, OidcVerifier, context_from_claims


def oidc_fixture() -> tuple[Settings, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "bank-key-1", "use": "sig", "alg": "RS256"})
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json=json.dumps({"keys": [jwk]}),
        oidc_role_mappings={"BANK_ANALYST": ["Analyst", "Viewer"]},
    )
    return settings, private_key


@pytest.mark.asyncio
async def test_oidc_verifies_signature_issuer_audience_and_claim_mapping() -> None:
    settings, private_key = oidc_fixture()
    organization_id = uuid4()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "roles": ["BANK_ANALYST", "UNRECOGNIZED_ROLE"],
            "organization_id": str(organization_id),
            "principal_type": "USER",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    claims = await OidcVerifier(settings).verify(token)
    context = context_from_claims(claims, settings)

    assert context.principal_id == "bank-user-123"
    assert context.organization_id == organization_id
    assert context.roles == frozenset({"Analyst", "Viewer"})


@pytest.mark.asyncio
async def test_oidc_rejects_wrong_audience() -> None:
    settings, private_key = oidc_fixture()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": "different-product",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(token)


@pytest.mark.asyncio
async def test_oidc_rejects_malformed_matching_jwk_without_internal_error() -> None:
    settings, private_key = oidc_fixture()
    settings = settings.model_copy(
        update={
            "oidc_jwks_json": json.dumps(
                {"keys": [{"kid": "bank-key-1", "kty": "RSA", "alg": "RS256"}]}
            )
        }
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "bank-user-123",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "bank-key-1"},
    )

    with pytest.raises(OidcVerificationError, match="verification failed"):
        await OidcVerifier(settings).verify(token)


def test_oidc_rejects_invalid_organization_claim() -> None:
    settings, _ = oidc_fixture()

    with pytest.raises(OidcVerificationError, match="not a UUID"):
        context_from_claims({"sub": "bank-user-123", "organization_id": "not-a-uuid"}, settings)


# ---------------------------------------------------------------------------
# The role mapping is a closed contract
# ---------------------------------------------------------------------------
#
# `context_from_claims` read `oidc_role_mappings.get(role, [role])` and kept
# whatever survived `in PLATFORM_ROLES`. Together those two lines let a token
# grant itself any platform role by naming it: the fallback handed the role's
# own string through, and the filter -- which reads like a guard -- was what
# admitted it, because `PlatformAdmin` is of course in `PLATFORM_ROLES`.
#
# Verified end to end against a real OIDC issuer before the fix: a token
# claiming `["totally-unmapped-group", "PlatformAdmin"]`, against a deployment
# whose mapping named only `atlas-admin`/`atlas-steward`/`atlas-viewer`, came
# back from `/v1/me` as `roles: ["PlatformAdmin"]`.
#
# The reason it survived this file is worth recording: the existing mapping
# test used `"UNRECOGNIZED_ROLE"`, a name that does not collide with the
# platform's vocabulary, so it proved only that *unknown* names are dropped --
# never that a *known* name cannot be claimed. These close that gap.


def _claims(roles: list[str]) -> dict[str, object]:
    return {
        "sub": "bank-user-123",
        "roles": roles,
        "organization_id": str(uuid4()),
        "principal_type": "USER",
    }


def test_a_token_cannot_grant_itself_a_platform_role_by_naming_it() -> None:
    settings, _ = oidc_fixture()  # maps BANK_ANALYST only

    context = context_from_claims(_claims(["PlatformAdmin"]), settings)

    assert context.roles == frozenset(), (
        "a token named a platform role and was granted it; the external role "
        "mapping must be a closed contract, not a passthrough with a filter"
    )


def test_a_platform_role_named_alongside_a_mapped_one_is_still_refused() -> None:
    """The escalation is most dangerous in this shape: a legitimate group plus
    an appended platform role name, where the principal genuinely has *some*
    access and the reviewer sees a plausible claim."""
    settings, _ = oidc_fixture()

    context = context_from_claims(_claims(["BANK_ANALYST", "PlatformAdmin"]), settings)

    assert context.roles == frozenset({"Analyst", "Viewer"})
    assert "PlatformAdmin" not in context.roles


def test_a_deployment_with_no_mapping_grants_nothing() -> None:
    """`oidc_role_mappings` defaults to `{}`, so this was the shipped default:
    with no mapping configured at all, every deployment accepted whatever its
    IdP called a role. Granting nothing is loudly wrong rather than quietly
    dangerous (INV-4)."""
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json='{"keys": []}',
    )

    context = context_from_claims(_claims(["PlatformAdmin", "Analyst"]), settings)

    assert context.roles == frozenset()


def test_a_name_can_be_accepted_as_is_by_mapping_it_to_itself() -> None:
    """The documented escape, so closing the default costs no capability.

    A deployment whose IdP deliberately emits platform role names says so
    explicitly. That is self-documenting in the configuration a reviewer reads,
    which is the whole difference from an implicit passthrough.
    """
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json='{"keys": []}',
        oidc_role_mappings={"Analyst": ["Analyst"]},
    )

    context = context_from_claims(_claims(["Analyst", "PlatformAdmin"]), settings)

    assert context.roles == frozenset({"Analyst"})
