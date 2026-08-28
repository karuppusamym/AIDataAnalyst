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
