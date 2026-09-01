"""OB-2: the audit's finding was "Zero call sites. No security event reaches
a SOC." (`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` Sec.2).

Two real call sites are exercised here, both against `aida.siem_routing`'s
actual `route_to_siem` (never mocked -- the point is that the real function
runs and returns True, formatting a real CEF/webhook payload):

- `aida.events.record_audit` -- the single funnel every audit event in the
  platform passes through, including policy denials, kill-switch
  engagement, and token revocation.
- `aida.security.get_security_context` -- the OIDC bearer-token
  verification path, which runs *before* a `SecurityContext` (and hence
  `record_audit`) exists, so it calls `route_to_siem` directly on rejection.
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
from aida.events import record_audit
from aida.models import AuditEvent
from aida.security import get_security_context
from aida.security_types import SecurityContext

# Same sqlite `AuditEvent.id` workaround as `tests/test_token_revocation.py` /
# `tests/test_detokenization_api.py`.
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
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


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


# --- record_audit funnel: policy denials -----------------------------------


async def test_a_policy_denial_is_audited_for_real(session: AsyncSession) -> None:
    """`record_audit`'s primary job -- writing the `AuditEvent` row -- keeps
    working unchanged; the SIEM call added alongside it is exercised (with
    the real, unmocked `route_to_siem`) below.
    """
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


async def test_a_policy_denial_actually_calls_route_to_siem_with_policy_violation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        "aida.events.route_to_siem",
        lambda security_event, siem_config: calls.append((security_event, siem_config))
        or True,
    )

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

    assert len(calls) == 1
    security_event, siem_config = calls[0]
    assert security_event.event_type == "POLICY_VIOLATION"
    assert security_event.severity == "MEDIUM"
    assert security_event.correlation_id == "corr-siem-2"
    assert security_event.source == "10.1.2.3"
    assert security_event.principal_id == "analyst-1"
    assert siem_config.enabled is True  # genuinely wired on by default (OB-2)


# --- record_audit funnel: kill-switch / token revocation (SUCCESS outcome) --


@pytest.mark.parametrize(
    "action",
    ["model.kill_switch_engage", "model.kill_switch_release", "token.revoked"],
)
async def test_security_control_changes_reach_the_siem_router_even_on_success(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        "aida.events.route_to_siem",
        lambda security_event, siem_config: calls.append((security_event, siem_config))
        or True,
    )

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

    assert len(calls) == 1
    security_event, _siem_config = calls[0]
    assert security_event.event_type == "SECURITY_CONTROL_CHANGE"


async def test_routine_success_audit_events_are_not_routed_to_siem(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "aida.events.route_to_siem", lambda *a, **k: calls.append(a) or True
    )

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

    assert calls == []


# --- get_security_context: real auth failures ------------------------------


async def test_a_missing_bearer_token_is_denied_and_reaches_the_siem_router(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        "aida.security.route_to_siem",
        lambda security_event, siem_config: calls.append((security_event, siem_config))
        or True,
    )

    settings = _settings(
        identity_provider="oidc",
        oidc_issuer="https://issuer.example",
        oidc_audience="aida",
        oidc_jwks_url="https://issuer.example/.well-known/jwks.json",
    )

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
    assert excinfo.value.status_code == 401

    assert len(calls) == 1
    security_event, siem_config = calls[0]
    assert security_event.event_type == "AUTH_FAILURE"
    assert security_event.severity == "HIGH"
    assert security_event.details["reason"] == "missing bearer token"
    assert siem_config.enabled is True
