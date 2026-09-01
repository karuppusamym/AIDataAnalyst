"""QG-6: the gated, audited detokenize endpoint.

`aida.detokenization_api.detokenize_value` is the one place a tokenized
column's original value can come back. Three properties matter, exercised
here against a real SQLite-backed session (the same pattern
`tests/test_token_revocation.py` uses for `revoke_token`):

- An authorized caller with a stated purpose gets the original value back,
  and the call is audited (INV-7: attributable).
- An unauthorized caller is denied *and audited* -- denial is not a quiet
  403, it is its own evidence trail (mirrors `query_gateway.py`'s
  `AuthorizationDenied` -> `REJECTED` bookkeeping, one HTTP layer up).
- A misconfigured/unavailable provider fails the call closed (503), never
  returning the token unchanged or a best-effort guess, and that failure is
  audited too.
"""

import itertools
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.db import Base
from aida.detokenization_api import DetokenizeRequest, detokenize_value
from aida.models import AuditEvent
from aida.security_types import SecurityContext
from atlas.platform.config import Settings

# Same sqlite `AuditEvent.id` workaround as `tests/test_token_revocation.py`.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "tokenization_key": "k" * 32}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _context(*, roles: frozenset[str]) -> SecurityContext:
    return SecurityContext(
        principal_id="analyst-1",
        principal_type="USER",
        organization_id=None,
        roles=roles,
    )


async def _last_audit_event(session: AsyncSession) -> AuditEvent:
    rows = (await session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()))).scalars()
    event_row = rows.first()
    assert event_row is not None, "no audit event was recorded"
    return event_row


# --- authorized path -----------------------------------------------------------


async def test_an_authorized_caller_recovers_the_original_value(
    session: AsyncSession,
) -> None:
    from aida.tokenization import LocalFpeTokenizationProvider

    settings = _settings()
    original = "4111111111111111"
    token = await LocalFpeTokenizationProvider(settings.tokenization_key).tokenize(original)

    result = await detokenize_value(
        DetokenizeRequest(token=token, purpose="fraud dispute case #4471"),
        context=_context(roles=frozenset({"ComplianceOfficer"})),
        settings=settings,
        session=session,
    )

    assert result.value == original

    audited = await _last_audit_event(session)
    assert audited.action == "query.detokenize"
    assert audited.outcome == "SUCCESS"
    assert audited.principal_id == "analyst-1"
    assert audited.details["purpose"] == "fraud dispute case #4471"
    # INV-6: the resolved value never lands in the audit trail.
    assert original not in str(audited.details)
    assert token not in str(audited.details)


# --- unauthorized path: denied AND audited --------------------------------------


async def test_an_unauthorized_caller_is_denied_and_the_denial_is_audited(
    session: AsyncSession,
) -> None:
    settings = _settings()

    with pytest.raises(HTTPException) as excinfo:
        await detokenize_value(
            DetokenizeRequest(token="4111-1111-1111-1111", purpose="curiosity"),  # noqa: S106
            context=_context(roles=frozenset({"Analyst"})),
            settings=settings,
            session=session,
        )

    assert excinfo.value.status_code == 403

    audited = await _last_audit_event(session)
    assert audited.action == "query.detokenize"
    assert audited.outcome == "DENIED"
    assert audited.details["reason"] == "ROLE_NOT_AUTHORIZED"
    assert audited.principal_id == "analyst-1"


@pytest.mark.parametrize(
    "roles",
    [
        frozenset({"PlatformAdmin"}),
        frozenset({"OrganizationAdmin"}),
        frozenset({"ComplianceOfficer"}),
        frozenset({"DataSteward"}),
    ],
)
async def test_every_authorized_role_is_accepted(
    session: AsyncSession, roles: frozenset[str]
) -> None:
    result = await detokenize_value(
        DetokenizeRequest(token="99", purpose="authorized-role coverage"),  # noqa: S106
        context=_context(roles=roles),
        settings=_settings(),
        session=session,
    )
    assert result.value  # the local provider always returns something


# --- fail closed: an unavailable provider never returns the token unchanged ----


async def test_an_unavailable_provider_fails_closed_and_is_audited(
    session: AsyncSession,
) -> None:
    settings = _settings(tokenization_provider="vault_transform")  # no Vault URL configured

    with pytest.raises(HTTPException) as excinfo:
        await detokenize_value(
            DetokenizeRequest(
                token="4111-1111-1111-1111",  # noqa: S106
                purpose="fraud dispute case #4472",
            ),
            context=_context(roles=frozenset({"ComplianceOfficer"})),
            settings=settings,
            session=session,
        )

    assert excinfo.value.status_code == 503

    audited = await _last_audit_event(session)
    assert audited.outcome == "DENIED"
    assert audited.details["reason"] == "TOKENIZATION_PROVIDER_UNAVAILABLE"
