"""OB-2 / F04: security events reach a durable delivery ledger, not a log line.

The original audit finding was "Zero call sites. No security event reaches a
SOC." (`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` Sec.2). Call sites
were then added -- but they called a function that formatted a message, logged
it and returned ``True`` without opening a socket
(`Docs/review-2026-09-05/REVIEW.md` F04), so "wired" still did not mean
"delivered".

This module tests the wiring only: that both real call sites produce a
committed `DeliveryIntent`, that an unconfigured deployment produces none, and
that the auth-failure path -- whose session is about to be thrown away by an
`HTTPException` -- still leaves a durable row. Nothing here is mocked; the
actual `route_to_siem` runs. Whether a destination then receives the bytes is
`tests/test_siem_delivery.py`'s job, against real local servers.

The two call sites:

- `aida.events.record_audit` -- the single funnel every audit event passes
  through, including policy denials, kill-switch engagement and token
  revocation. Stages the intent in the caller's transaction.
- `aida.security.get_security_context` -- OIDC bearer verification, which runs
  before a `SecurityContext` exists and ends in a 401, so it commits its own.
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
from aida.config import Settings
from aida.db import Base
from aida.delivery_intents import KIND_SIEM, STATE_PENDING
from aida.events import record_audit
from aida.models import AuditEvent, DeliveryIntent
from aida.security import get_security_context
from aida.security_types import SecurityContext

# Same sqlite `AuditEvent.id` workaround as `tests/test_token_revocation.py` /
# `tests/test_detokenization_api.py`.
_audit_event_ids = itertools.count(1)

_COLLECTOR = "https://siem.example.com/events"


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
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _configured(**overrides: object) -> Settings:
    """Settings that actually name a SIEM destination."""
    values: dict[str, object] = {
        "siem_enabled": True,
        "siem_transport": "webhook",
        "siem_endpoint": _COLLECTOR,
    }
    values.update(overrides)
    return _settings(**values)


def _context(*, source_ip: str | None = "10.1.2.3") -> SecurityContext:
    return SecurityContext(
        principal_id="analyst-1",
        principal_type="USER",
        organization_id=None,
        roles=frozenset({"Analyst"}),
        source_ip=source_ip,
    )


async def _last_audit_event(session: AsyncSession) -> AuditEvent:
    rows = (await session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()))).scalars()
    event_row = rows.first()
    assert event_row is not None, "no audit event was recorded"
    return event_row


async def _intents(session: AsyncSession) -> list[DeliveryIntent]:
    rows = await session.scalars(
        select(DeliveryIntent)
        .where(DeliveryIntent.kind == KIND_SIEM)
        .order_by(DeliveryIntent.requested_at)
    )
    return list(rows.all())


@pytest.fixture
def configured_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`record_audit` reads settings through `get_settings`, so point that at a
    configured SIEM rather than reaching into the call site."""
    settings = _configured()
    monkeypatch.setattr("aida.events.get_settings", lambda: settings)
    return settings


# --- record_audit funnel: policy denials -----------------------------------


async def test_a_policy_denial_is_audited_for_real(session: AsyncSession) -> None:
    """`record_audit`'s primary job -- writing the `AuditEvent` row -- keeps
    working unchanged; the SIEM intent staged alongside it is below."""
    record_audit(
        session,
        _context(),
        action="mcp.tool_call.role_binding_denied",
        resource_type="governed_tool_version",
        resource_id="tool-1",
        outcome="DENIED",
        correlation_id="corr-siem-1",
    )
    await session.flush()

    audited = await _last_audit_event(session)
    assert audited.outcome == "DENIED"


async def test_a_policy_denial_stages_a_delivery_intent(
    session: AsyncSession, configured_settings: Settings
) -> None:
    record_audit(
        session,
        _context(),
        action="query.detokenize",
        resource_type="query",
        resource_id=None,
        outcome="DENIED",
        correlation_id="corr-siem-2",
        details={"reason": "ROLE_NOT_AUTHORIZED"},
    )
    await session.flush()

    intents = await _intents(session)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.state == STATE_PENDING
    assert intent.payload["event_type"] == "POLICY_VIOLATION"
    assert intent.payload["severity"] == "MEDIUM"
    assert intent.payload["correlation_id"] == "corr-siem-2"
    assert intent.payload["source"] == "10.1.2.3"
    assert intent.payload["principal_id"] == "analyst-1"
    # Not yet attempted, and emphatically not delivered.
    assert intent.attempted_at is None
    assert intent.delivered_at is None


async def test_the_intent_and_the_audit_event_commit_together(
    session: AsyncSession, configured_settings: Settings
) -> None:
    """The intent is staged in the caller's transaction, so a rolled-back
    audited operation leaves no obligation to forward an event that did not
    happen -- and a committed one cannot lose its obligation."""
    record_audit(
        session,
        _context(),
        action="query.detokenize",
        resource_type="query",
        resource_id=None,
        outcome="DENIED",
        correlation_id="corr-siem-rollback",
    )
    await session.flush()
    await session.rollback()

    assert await _intents(session) == []


# --- record_audit funnel: kill-switch / token revocation (SUCCESS outcome) --


@pytest.mark.parametrize(
    "action",
    ["model.kill_switch_engage", "model.kill_switch_release", "token.revoked"],
)
async def test_security_control_changes_are_queued_even_on_success(
    session: AsyncSession, configured_settings: Settings, action: str
) -> None:
    record_audit(
        session,
        _context(),
        action=action,
        resource_type="kill_switch_state",
        resource_id="scope-1",
        outcome="SUCCESS",
        correlation_id="corr-siem-3",
    )
    await session.flush()

    intents = await _intents(session)
    assert len(intents) == 1
    assert intents[0].payload["event_type"] == "SECURITY_CONTROL_CHANGE"


async def test_routine_success_audit_events_are_not_queued(
    session: AsyncSession, configured_settings: Settings
) -> None:
    record_audit(
        session,
        _context(),
        action="observability.slo.create",
        resource_type="slo_definition",
        resource_id="slo-1",
        outcome="SUCCESS",
        correlation_id="corr-siem-4",
    )
    await session.flush()

    assert await _intents(session) == []


async def test_the_shipped_default_endpoint_queues_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F04, and the "do not start traffic by surprise" constraint.

    `siem_enabled` ships True with `siem_endpoint` set to
    `internal://security-log-pipeline`, which was a label for a structlog line
    rather than an address. A deployment carrying that forward has not chosen a
    SOC collector, so it must queue nothing -- upgrading to this code cannot
    start sending anywhere.
    """
    monkeypatch.setattr("aida.events.get_settings", _settings)
    assert _settings().siem_endpoint == "internal://security-log-pipeline"

    record_audit(
        session,
        _context(),
        action="query.detokenize",
        resource_type="query",
        resource_id=None,
        outcome="DENIED",
        correlation_id="corr-siem-default",
    )
    await session.flush()

    assert await _intents(session) == []


# --- get_security_context: real auth failures ------------------------------


async def _call_auth(settings: Settings, session: AsyncSession) -> HTTPException:
    with pytest.raises(HTTPException) as excinfo:
        await get_security_context(
            settings=settings,
            session=session,
            principal_id=None,
            principal_type="USER",
            organization_header=None,
            roles="Viewer",
            authorization=None,
            business_purpose=None,
        )
    return excinfo.value


def _oidc(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "identity_provider": "oidc",
        "oidc_issuer": "https://issuer.example",
        "oidc_audience": "aida",
        "oidc_jwks_url": "https://issuer.example/.well-known/jwks.json",
    }
    values.update(overrides)
    return values


async def test_a_missing_bearer_token_leaves_a_durable_intent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request's own session is discarded by the 401, so this path commits
    its own transaction. Without that, the one security event a SOC most needs
    would be staged into a session that never commits -- F12's defect on the
    authentication path."""
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr("aida.db.session_factory", maker)

    error = await _call_auth(_configured(**_oidc()), session)
    assert error.status_code == 401

    async with maker() as reader:
        intents = await _intents(reader)
    assert len(intents) == 1
    assert intents[0].payload["event_type"] == "AUTH_FAILURE"
    assert intents[0].payload["severity"] == "HIGH"
    assert intents[0].payload["details"]["reason"] == "missing bearer token"


async def test_an_unconfigured_siem_opens_no_session_on_auth_failure(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An INSERT per refused authentication is affordable; one against a
    database nobody configured is not. The unusable-destination check runs
    before any session is opened."""

    def _explode() -> AsyncSession:  # pragma: no cover - must not be reached
        raise AssertionError("a session was opened for an unconfigured SIEM")

    monkeypatch.setattr("aida.db.session_factory", _explode)

    error = await _call_auth(_settings(**_oidc()), session)
    assert error.status_code == 401
