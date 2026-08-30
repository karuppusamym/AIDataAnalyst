"""Coverage for ID-4: token revocation and replay policy (module 01 identity-and-tenancy).

Two layers are exercised:

* `aida.token_revocation` -- the durable revocation record and the fail-closed check
  (`enforce_not_revoked`, `prune_expired_revocations`) against a real SQLite-backed
  session, following the pattern in `tests/test_workspace_authorization.py`.
* `aida.security.get_security_context` and `aida.token_revocation_api.revoke_token`
  -- the actual verification path a request goes through, and the admin/self-service
  surface that writes a revocation, using the RSA/JWKS fixture from `tests/test_oidc.py`.

"Replay" in this platform means presenting a bearer token again after it has been
revoked; there is no separate single-use token type, so the fail-closed revocation
check *is* the replay defense (see `aida.token_revocation`'s module docstring).
"""

import itertools
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.models import AuditEvent, RevokedToken
from aida.oidc import OidcVerifier, token_identifier
from aida.security import get_security_context
from aida.security_types import SecurityContext
from aida.token_revocation import (
    TokenRevokedError,
    enforce_not_revoked,
    prune_expired_revocations,
)
from aida.token_revocation_api import TokenRevocationRequest, revoke_token

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

# `AuditEvent.id` is a `BigInteger` autoincrement primary key, relying in production
# on Postgres's own identity/sequence generation. sqlite only auto-populates a bare
# `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger` compiles to `BIGINT`, which
# sqlite does not treat as that alias -- so an in-memory sqlite session (as used
# below) leaves `id` NULL and violates the NOT NULL constraint on insert. Assign ids
# by hand for this test module's sqlite engine only; nothing about the production
# model changes. (Same workaround as `tests/test_relationship_intelligence_review.py`.)
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


def _oidc_fixture() -> tuple[Settings, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "bank-key-1", "use": "sig", "alg": "RS256"})
    settings = Settings(
        identity_provider="oidc",
        oidc_issuer="https://identity.bank.example",
        oidc_audience="atlas",
        oidc_jwks_json=json.dumps({"keys": [jwk]}),
    )
    return settings, private_key


def _sign(
    settings: Settings,
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = "bank-user-123",
    jti: str | None = "token-abc",
    org: str | None = None,
    minutes_valid: int = 5,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    claims = {
        "sub": sub,
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_audience,
        "iat": issued,
        "exp": issued + timedelta(minutes=minutes_valid),
    }
    if jti is not None:
        claims["jti"] = jti
    if org is not None:
        claims["organization_id"] = org
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "bank-key-1"})


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _RaisingSession:
    """Stands in for a session whose revocation lookup fails -- e.g. the database is
    unreachable -- to prove the check denies rather than silently passing (INV-4)."""

    async def execute(self, _statement: object) -> object:
        raise SQLAlchemyError("connection reset")


# --- token_identifier ---------------------------------------------------------------


def test_token_identifier_prefers_jti() -> None:
    identifier = token_identifier({"jti": "abc-123", "sub": "s", "iat": 1, "exp": 2})
    assert identifier == "jti:abc-123"


def test_token_identifier_falls_back_to_a_fingerprint_when_jti_is_absent() -> None:
    claims = {"sub": "bank-user-123", "iat": 1000, "exp": 1300}
    identifier = token_identifier(claims)
    assert identifier.startswith("fp:")
    # Deterministic: the same (sub, iat, exp) triple always yields the same identifier.
    assert identifier == token_identifier(dict(claims))
    # A different subject is a different token.
    assert identifier != token_identifier({**claims, "sub": "someone-else"})


# --- enforce_not_revoked: the fail-closed check -----------------------------------


@pytest.mark.asyncio
async def test_a_valid_unrevoked_token_passes(session: AsyncSession) -> None:
    claims = {"sub": "bank-user-123", "jti": "token-1", "iat": 1, "exp": 2}
    await enforce_not_revoked(session, claims)  # no exception


@pytest.mark.asyncio
async def test_a_revoked_token_is_rejected_on_its_next_use(session: AsyncSession) -> None:
    claims = {"sub": "bank-user-123", "jti": "token-2", "iat": 1, "exp": 2}
    identifier = token_identifier(claims)
    session.add(
        RevokedToken(
            token_identifier=identifier,
            subject="bank-user-123",
            token_expires_at=_NOW + timedelta(minutes=5),
            revoked_by="platform-admin",
            reason="compromised credential",
        )
    )
    await session.flush()

    with pytest.raises(TokenRevokedError, match="revoked"):
        await enforce_not_revoked(session, claims)


@pytest.mark.asyncio
async def test_a_lookup_failure_denies_rather_than_passes() -> None:
    """A DB error checking revocation status must not be treated as 'not revoked'."""
    with pytest.raises(TokenRevokedError, match="could not be verified"):
        await enforce_not_revoked(_RaisingSession(), {"sub": "x", "jti": "y", "iat": 1, "exp": 2})


# --- pruning: bounded, never un-revokes a still-live token ------------------------


@pytest.mark.asyncio
async def test_pruning_removes_only_records_past_their_tokens_own_expiry(
    session: AsyncSession,
) -> None:
    expired_claims = {"sub": "bank-user-123", "jti": "expired-token", "iat": 1, "exp": 2}
    live_claims = {"sub": "bank-user-123", "jti": "live-token", "iat": 1, "exp": 2}
    session.add(
        RevokedToken(
            token_identifier=token_identifier(expired_claims),
            subject="bank-user-123",
            token_expires_at=_NOW - timedelta(days=1),
            revoked_by="platform-admin",
            reason="stale",
        )
    )
    session.add(
        RevokedToken(
            token_identifier=token_identifier(live_claims),
            subject="bank-user-123",
            token_expires_at=_NOW + timedelta(days=1),
            revoked_by="platform-admin",
            reason="compromised credential",
        )
    )
    await session.flush()

    removed = await prune_expired_revocations(session, now=_NOW)
    await session.flush()

    assert removed == 1
    remaining = (await session.execute(select(RevokedToken.token_identifier))).scalars().all()
    assert remaining == [token_identifier(live_claims)]

    # The still-live token must still be rejected -- pruning never un-revokes a
    # token that is still inside its own expiry window.
    with pytest.raises(TokenRevokedError):
        await enforce_not_revoked(session, live_claims)
    # The pruned one is gone from the revocation list (though it can never be
    # replayed anyway, since it has already failed the verifier's own exp check).
    await enforce_not_revoked(session, expired_claims)


# --- wired into the actual verification path (aida.security) ---------------------


@pytest.mark.asyncio
async def test_get_security_context_rejects_a_revoked_bearer_token(
    session: AsyncSession,
) -> None:
    settings, private_key = _oidc_fixture()
    token = _sign(settings, private_key, jti="live-session-token")
    claims = await OidcVerifier(settings).verify(token)

    # Passes before revocation.
    context = await get_security_context(
        settings=settings,
        session=session,
        principal_id=None,
        principal_type="USER",
        organization_header=None,
        roles="Viewer",
        authorization=f"Bearer {token}",
        business_purpose=None,
    )
    assert context.principal_id == "bank-user-123"

    session.add(
        RevokedToken(
            token_identifier=token_identifier(claims),
            subject="bank-user-123",
            token_expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
            revoked_by="platform-admin",
            reason="logout",
        )
    )
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await get_security_context(
            settings=settings,
            session=session,
            principal_id=None,
            principal_type="USER",
            organization_header=None,
            roles="Viewer",
            authorization=f"Bearer {token}",
            business_purpose=None,
        )
    assert excinfo.value.status_code == 401
    # INV-4: does not leak which check failed (signature vs. revocation).
    assert excinfo.value.detail == "bearer token verification failed"


# --- the admin/self-service revoke endpoint: audited, role-gated -----------------


def _admin_context(org: object) -> SecurityContext:
    return SecurityContext(
        principal_id="security-admin",
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"PlatformAdmin"}),
    )


@pytest.mark.asyncio
async def test_self_revocation_logs_out_the_callers_own_token(session: AsyncSession) -> None:
    settings, private_key = _oidc_fixture()
    org_id = uuid4()
    token = _sign(settings, private_key, sub="alice", jti="alices-token", org=str(org_id))

    caller = SecurityContext(
        principal_id="alice",
        principal_type="USER",
        organization_id=org_id,
        roles=frozenset({"Analyst"}),
    )

    result = await revoke_token(
        TokenRevocationRequest(token=token, reason="user-initiated logout"),
        context=caller,
        settings=settings,
        session=session,
    )
    await session.flush()

    assert result.self_revocation is True
    assert result.subject == "alice"

    stored = (
        await session.execute(select(RevokedToken).where(RevokedToken.subject == "alice"))
    ).scalar_one()
    assert stored.reason == "user-initiated logout"


@pytest.mark.asyncio
async def test_revoking_a_token_writes_an_audit_event(session: AsyncSession) -> None:
    settings, private_key = _oidc_fixture()
    org_id = uuid4()
    token = _sign(settings, private_key, sub="bob", jti="bobs-token", org=str(org_id))
    admin = _admin_context(org_id)

    await revoke_token(
        TokenRevocationRequest(token=token, reason="compromised credential"),
        context=admin,
        settings=settings,
        session=session,
    )
    await session.flush()

    audited = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "token.revoked")
        )
    ).scalar_one()
    assert audited.outcome == "SUCCESS"
    assert audited.principal_id == "security-admin"
    assert audited.details["subject"] == "bob"
    assert audited.details["self_revocation"] is False


@pytest.mark.asyncio
async def test_a_non_admin_cannot_revoke_someone_elses_token(session: AsyncSession) -> None:
    settings, private_key = _oidc_fixture()
    org_id = uuid4()
    token = _sign(settings, private_key, sub="carol", jti="carols-token", org=str(org_id))
    non_admin = SecurityContext(
        principal_id="dave",
        principal_type="USER",
        organization_id=org_id,
        roles=frozenset({"Analyst"}),
    )

    with pytest.raises(HTTPException) as excinfo:
        await revoke_token(
            TokenRevocationRequest(token=token, reason="not my call"),
            context=non_admin,
            settings=settings,
            session=session,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_revoking_the_same_token_twice_conflicts(session: AsyncSession) -> None:
    settings, private_key = _oidc_fixture()
    org_id = uuid4()
    token = _sign(settings, private_key, sub="erin", jti="erins-token", org=str(org_id))
    admin = _admin_context(org_id)

    await revoke_token(
        TokenRevocationRequest(token=token, reason="first revocation"),
        context=admin,
        settings=settings,
        session=session,
    )
    await session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await revoke_token(
            TokenRevocationRequest(token=token, reason="second revocation"),
            context=admin,
            settings=settings,
            session=session,
        )
    assert excinfo.value.status_code == 409
