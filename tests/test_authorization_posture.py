"""F11 -- workspace authorization can no longer be silently non-enforcing.

The finding was not "authorization is off". It was that nothing said whether it
was on: an unresolved workspace defaulted to SHADOW, workspaces defaulted to
SHADOW, production settings validation checked neither, and no operator-facing
signal reported the difference. These tests pin the three things the remediation
added, and one thing it deliberately did *not* change.

1. the default is unchanged (tightening is an intentional migration, not an
   upgrade side effect);
2. a deployment that *claims* enforcement cannot be wrong about it -- the claim
   fails settings validation if unresolved scope is allowed through, and fails
   process startup if any ACTIVE workspace is still observing;
3. "enforcing", "observing" and "could not resolve its scope" are three distinct
   reported states, not two.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.authorization_posture import (
    CONTROL_NAME,
    ENFORCING,
    OBSERVING,
    SCOPE_UNRESOLVED,
    PostureViolation,
    assert_startup_posture,
    describe_configured_posture,
    evaluate_posture,
    unresolvable_posture,
)
from aida.db import Base
from aida.models import Organization, Workspace
from atlas.platform.config import Settings


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _enforcing_settings() -> Settings:
    return Settings(
        workspace_authorization_posture="ENFORCING",
        unresolved_workspace_posture="DENY",
    )


async def _workspaces(session: AsyncSession, *modes: str) -> None:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    for index, mode in enumerate(modes):
        session.add(
            Workspace(
                organization_id=organization.id,
                name=f"Workspace {index}",
                slug=f"ws-{index}-{uuid4().hex[:6]}",
                authorization_mode=mode,
            )
        )
    await session.flush()


# ---------------------------------------------------------------------------
# 1. The default is deliberately unchanged
# ---------------------------------------------------------------------------


def test_default_posture_is_still_observing_and_shadow() -> None:
    """The review is explicit that tightening must be an intentional migration.
    This change makes the posture explicit and checkable; it must not flip a
    default and start denying traffic in a deployment that merely upgraded.
    """
    settings = Settings()
    assert settings.workspace_authorization_posture == "OBSERVING"
    assert settings.unresolved_workspace_posture == "SHADOW"
    assert describe_configured_posture(settings).state == OBSERVING


# ---------------------------------------------------------------------------
# 2. A claim of enforcement is checked, not trusted
# ---------------------------------------------------------------------------


def test_enforcing_claim_rejects_configuration_that_lets_unresolved_scope_through() -> None:
    """The exact hole F11 named: settings validation passed while a request
    whose workspace could not be resolved proceeded undecided.
    """
    with pytest.raises(ValidationError) as exc:
        Settings(
            workspace_authorization_posture="ENFORCING",
            unresolved_workspace_posture="SHADOW",
        )
    assert "unresolved_workspace_posture=DENY" in str(exc.value)


def test_enforcing_claim_is_constructible_once_unresolved_scope_is_denied() -> None:
    settings = _enforcing_settings()
    assert describe_configured_posture(settings).state == ENFORCING
    assert describe_configured_posture(settings).consistent


async def test_enforcing_claim_fails_startup_when_a_workspace_is_still_observing(
    session: AsyncSession,
) -> None:
    """Configuration alone cannot prove enforcement -- the workspace inventory
    is the other half of the claim, and it is checked at startup.
    """
    await _workspaces(session, "ENFORCE", "SHADOW", "ENFORCE")
    report = await evaluate_posture(session, _enforcing_settings())

    assert report.state == OBSERVING
    assert report.workspaces_total == 3
    assert report.workspaces_enforcing == 2
    assert report.workspaces_observing == 1
    assert not report.consistent
    with pytest.raises(PostureViolation) as exc:
        assert_startup_posture(report)
    assert "1 of 3 ACTIVE workspaces" in str(exc.value)


async def test_enforcing_claim_starts_when_every_active_workspace_enforces(
    session: AsyncSession,
) -> None:
    await _workspaces(session, "ENFORCE", "ENFORCE")
    report = await evaluate_posture(session, _enforcing_settings())

    assert report.state == ENFORCING
    assert report.consistent
    assert_startup_posture(report)  # must not raise


async def test_observing_deployment_never_fails_startup(session: AsyncSession) -> None:
    """A deployment that makes no enforcement claim cannot fail this check --
    that is what keeps the default posture a no-op for existing deployments.
    """
    await _workspaces(session, "SHADOW", "SHADOW")
    report = await evaluate_posture(session, Settings())

    assert report.state == OBSERVING
    assert report.consistent
    assert_startup_posture(report)


async def test_archived_workspaces_do_not_count_against_an_enforcement_claim(
    session: AsyncSession,
) -> None:
    """A workspace that serves no traffic is not an enforcement hole. Counting
    it would make the claim impossible to satisfy for any long-lived estate,
    which is how a check gets disabled.
    """
    await _workspaces(session, "ENFORCE")
    organization = Organization(name="Bank2", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    session.add(
        Workspace(
            organization_id=organization.id,
            name="Retired",
            slug=f"ws-retired-{uuid4().hex[:6]}",
            status="ARCHIVED",
            authorization_mode="SHADOW",
        )
    )
    await session.flush()

    report = await evaluate_posture(session, _enforcing_settings())
    assert report.workspaces_total == 1
    assert report.state == ENFORCING


# ---------------------------------------------------------------------------
# 3. Three states, not two
# ---------------------------------------------------------------------------


def test_unreadable_inventory_is_scope_unresolved_not_observing() -> None:
    """ "not enforcing" and "cannot tell whether it is enforcing" are different
    operational facts. Collapsing them is how the original finding stayed
    invisible, so they must not share a value.
    """
    report = unresolvable_posture(_enforcing_settings(), detail="db down")
    assert report.state == SCOPE_UNRESOLVED
    assert report.state != OBSERVING
    assert report.workspaces_total is None
    assert "db down" in report.problems[0]


def test_scope_unresolved_does_not_crash_a_starting_process() -> None:
    """A database outage at boot is already reported by readiness; turning it
    into a crash-loop would make the stricter posture an availability
    regression, which is the fastest way to get it turned back off.
    """
    assert_startup_posture(unresolvable_posture(_enforcing_settings(), detail="timeout"))


def test_reported_signals_name_the_control_and_carry_the_counts() -> None:
    """An operator has to be able to answer "is this enforcing" from the
    signal, without reading source -- and see why when the answer is no.
    """
    report = unresolvable_posture(Settings(), detail="timeout after 2.0s")
    signals = report.as_signals()
    assert signals[f"{CONTROL_NAME}.declared"] == "OBSERVING"
    assert signals[f"{CONTROL_NAME}.unresolved_scope"] == "PROCEEDS_UNDECIDED"
    assert "timeout" in signals[f"{CONTROL_NAME}.problems"]
