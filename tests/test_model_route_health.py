"""R11-B16: an approved route whose model the provider retired must be noticed.

This happened here. A route was approved for `gemini-2.0-flash`, Google had
retired it, and the route read as entirely healthy -- APPROVED, credentialed,
selected -- while every generated answer failed with a 404 nothing connected
back to it. Registration-time validation was added the same day and says
nothing about the months afterwards; the fallback was fixed too and still
leaves a dead primary nobody has been told about.

What these pin is mostly the *restraint*, because that is where a health check
of a governed object goes wrong: it must not revoke an approval, must not spend
money, and must not call a briefly-unreachable provider dead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.model_route_health import (
    REACHABLE,
    UNKNOWN,
    UNREACHABLE,
    check_route,
    run_model_route_reachability_pass,
    unreachable_route_summary,
)
from aida.models import AuditEvent, ModelRouteConfiguration, Organization

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {"_env_file": None, "environment": "test"}
    defaults.update(overrides)
    return Settings(**defaults)


@pytest_asyncio.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # The pass opens its own session through `aida.db`, imported inside the
    # function, so the module attribute is what has to be replaced.
    from aida import db as aida_db

    monkeypatch.setattr(aida_db, "session_factory", maker)
    from aida import model_route_health

    model_route_health._last_run_at = None
    async with maker() as active:
        yield active
    await engine.dispose()


async def _route(
    session: AsyncSession,
    *,
    route_key: str,
    model_id: str,
    provider: str = "GOOGLE_GEMINI",
    status: str = "APPROVED",
) -> ModelRouteConfiguration:
    organization = await session.scalar(select(Organization))
    if organization is None:
        organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        session.add(organization)
        await session.flush()
    route = ModelRouteConfiguration(
        organization_id=organization.id,
        route_key=route_key,
        version=1,
        status=status,
        display_name=route_key,
        provider_type=provider,
        model_id=model_id,
        endpoint_alias="provider-default",
        credential_reference="env://GEMINI_API_KEY",
        data_residency="test",
        retention_policy="PROVIDER_CONTRACT",
        capabilities=["SQL_GENERATION"],
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
        fingerprint="f",
        created_by="test",
    )
    session.add(route)
    await session.flush()
    return route


def _serving(*models: str) -> Any:
    async def _served(provider_type: str, settings: Settings) -> set[str]:
        return set(models)

    return _served


# --------------------------------------------------------------------------- #
# The three verdicts
# --------------------------------------------------------------------------- #


async def test_a_served_model_is_reachable(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aida import model_route_health

    monkeypatch.setattr(model_route_health, "_served_models", _serving("gemini-3.6-flash"))
    route = await _route(session, route_key="live", model_id="gemini-3.6-flash")

    result = await check_route(route, _settings())

    assert result.status == REACHABLE


async def test_a_retired_model_is_unreachable_and_says_why(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detail has to be actionable: the name of the model that is gone, and
    the consequence. "UNREACHABLE" alone tells an operator nothing about what
    to do next."""
    from aida import model_route_health

    monkeypatch.setattr(model_route_health, "_served_models", _serving("gemini-3.6-flash"))
    route = await _route(session, route_key="retired", model_id="gemini-2.0-flash")

    result = await check_route(route, _settings())

    assert result.status == UNREACHABLE
    assert "gemini-2.0-flash" in result.detail


async def test_a_provider_that_cannot_be_listed_is_unknown_not_unreachable(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction that keeps the signal worth reading.

    A network failure, an expired credential, a rate limit, or an OpenAI
    account whose billing is inactive and therefore cannot list models at all,
    each mean "we could not tell". Reporting those as a retired model would
    train an operator to ignore this, and then the one real retirement goes
    unnoticed too.
    """
    from aida import model_route_health

    async def _unknown(provider_type: str, settings: Settings) -> None:
        return None

    monkeypatch.setattr(model_route_health, "_served_models", _unknown)
    route = await _route(session, route_key="offline", model_id="gemini-3.6-flash")

    result = await check_route(route, _settings())

    assert result.status == UNKNOWN
    assert result.status != UNREACHABLE


# --------------------------------------------------------------------------- #
# The sweep, and what it must not do
# --------------------------------------------------------------------------- #


async def test_the_sweep_never_revokes_an_approval(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restraint that matters most.

    An approval is a human decision taken through maker-checker. A background
    sweep that flipped `status` would be the platform quietly overruling its
    own governance -- so reachability is recorded *beside* the approval and the
    approval is left exactly as it was.
    """
    from aida import model_route_health

    monkeypatch.setattr(model_route_health, "_served_models", _serving("something-else"))
    route = await _route(session, route_key="retired", model_id="gemini-2.0-flash")
    await session.commit()

    checked = await run_model_route_reachability_pass(_settings(), now=_NOW)

    assert checked == 1
    # `populate_existing`, because the pass commits in its **own** session and
    # this fixture's maker has `expire_on_commit=False` -- so a plain `get`
    # returns the identity map's stale copy and the assertion below would read
    # None however correctly the sweep had written.
    refreshed = await session.scalar(
        select(ModelRouteConfiguration)
        .where(ModelRouteConfiguration.id == route.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed is not None
    assert refreshed.status == "APPROVED", "the sweep revoked a human approval"
    assert refreshed.reachability_status == UNREACHABLE
    assert refreshed.reachability_checked_at is not None


async def test_only_a_change_of_status_is_audited(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit trail that repeats itself every sweep is one nobody reads."""
    from aida import model_route_health

    monkeypatch.setattr(model_route_health, "_served_models", _serving("something-else"))
    await _route(session, route_key="retired", model_id="gemini-2.0-flash")
    await session.commit()

    await run_model_route_reachability_pass(_settings(), now=_NOW)
    model_route_health._last_run_at = None
    await run_model_route_reachability_pass(_settings(), now=_NOW + timedelta(days=1))

    events = list(
        (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "model_route.reachability_changed"
                )
            )
        ).all()
    )
    assert len(events) == 1, "the unchanged second sweep audited itself again"


async def test_a_draft_route_is_not_checked(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an APPROVED route can serve traffic, so only one is worth a call to
    a provider -- and a draft naming a model nobody has approved yet is not a
    finding."""
    from aida import model_route_health

    monkeypatch.setattr(model_route_health, "_served_models", _serving("gemini-3.6-flash"))
    await _route(session, route_key="draft", model_id="anything", status="DRAFT")
    await session.commit()

    assert await run_model_route_reachability_pass(_settings(), now=_NOW) == 0


async def test_a_disabled_sweep_makes_no_provider_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aida import model_route_health

    called = False

    async def _tripwire(provider_type: str, settings: Settings) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(model_route_health, "_served_models", _tripwire)
    await _route(session, route_key="live", model_id="gemini-3.6-flash")
    await session.commit()

    result = await run_model_route_reachability_pass(
        _settings(model_route_health_enabled=False), now=_NOW
    )

    assert result is None
    assert called is False


async def test_the_summary_separates_unreachable_from_never_checked(
    session: AsyncSession,
) -> None:
    """"Nothing is unreachable" and "nothing has looked yet" must not read the
    same, which is the whole reason the column is nullable."""
    await _route(session, route_key="never-looked", model_id="gemini-3.6-flash")
    gone = await _route(session, route_key="gone", model_id="gemini-2.0-flash")
    gone.reachability_status = UNREACHABLE
    await session.commit()

    summary = await unreachable_route_summary(_settings())

    assert summary["approved"] == 2
    assert summary["unreachable"] == ["gone"]
    assert summary["never_checked"] == 1
