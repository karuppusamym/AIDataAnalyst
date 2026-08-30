"""The ADR-0018 rollout must not be able to lock the platform out.

These tests exist because of a defect the migration rehearsal missed: the ADR-0018
migration creates one workspace per project and zero memberships, because this codebase
has no persisted principal table to backfill them from. Wiring `authorize` into a read
path at that point would have denied every request in the platform.

Two mechanisms make the rollout safe, and both are tested here for the property that
matters rather than for their implementation: rule-derived membership, so a migrated
workspace is reachable without inventing an access grant; and shadow mode, so enforcement
becomes a measurement rather than a leap.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401
from aida.business_graph import assign
from aida.db import Base
from aida.models import (
    AccessPolicy,
    AuthorizationShadowRecord,
    BusinessNode,
    Organization,
    Workspace,
    WorkspaceAccessRule,
)
from aida.security_types import SecurityContext
from aida.workspace_access import ENFORCE, enforcement_readiness, rule_derived_roles
from aida.workspace_service import authorize, authorize_enforced, create_workspace

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    session.add(
        AccessPolicy(
            organization_id=org.id, code="rbac-parity", name="parity", effect="ALLOW",
            subject_match={"roles": ["Analyst", "Steward"]}, action_match=[],
            created_by="seed",
        )
    )
    await session.flush()
    return org


async def _memberless_workspace(session: AsyncSession, org: Organization) -> Workspace:
    """A workspace shaped exactly as the ADR-0018 migration leaves them: no members."""
    workspace = Workspace(
        organization_id=org.id, name="Migrated", slug=f"w-{uuid4().hex[:6]}", purpose="p"
    )
    session.add(workspace)
    await session.flush()
    return workspace


def _analyst(org: Organization) -> SecurityContext:
    return SecurityContext(
        principal_id="alice", principal_type="USER",
        organization_id=org.id, roles=frozenset({"Analyst"}),
    )


# --- the lockout the rehearsal missed ---------------------------------------


async def test_a_migrated_workspace_has_no_members_and_would_deny_everyone(
    session: AsyncSession,
) -> None:
    """The defect, pinned so it cannot be reintroduced silently.

    This asserts the *problem*, not a fix: a workspace created the way the migration
    creates them denies a legitimate analyst, because membership cannot be backfilled from
    a principal table that does not exist.
    """
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    result = await authorize(
        session, _analyst(org), workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE", now=_NOW,
    )
    assert result.allowed is False
    assert result.reason_code == "NO_WORKSPACE_MEMBERSHIP"


async def test_an_access_rule_makes_a_memberless_workspace_reachable(
    session: AsyncSession,
) -> None:
    """One rule covers every migrated workspace, without inventing a membership row."""
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    session.add(
        WorkspaceAccessRule(
            organization_id=org.id, code="seed-analyst", subject_role="Analyst",
            workspace_role="analyst", created_by="migration",
        )
    )
    await session.flush()
    result = await authorize(
        session, _analyst(org), workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE", now=_NOW,
    )
    assert result.allowed is True


async def test_revoking_the_rule_revokes_the_access(session: AsyncSession) -> None:
    """The property that makes a rule better than seeded owners: it is reversible."""
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    rule = WorkspaceAccessRule(
        organization_id=org.id, code="seed-analyst", subject_role="Analyst",
        workspace_role="analyst", created_by="migration",
    )
    session.add(rule)
    await session.flush()
    rule.status = "REVOKED"
    await session.flush()
    result = await authorize(
        session, _analyst(org), workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE", now=_NOW,
    )
    assert result.allowed is False
    assert result.reason_code == "NO_WORKSPACE_MEMBERSHIP"


async def test_an_expired_rule_grants_nothing(session: AsyncSession) -> None:
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    session.add(
        WorkspaceAccessRule(
            organization_id=org.id, code="temp", subject_role="Analyst",
            workspace_role="analyst", created_by="x",
            expires_at=_NOW - timedelta(days=1),
        )
    )
    await session.flush()
    derived = await rule_derived_roles(
        session, workspace, frozenset({"Analyst"}), now=_NOW
    )
    assert derived == frozenset()


async def test_a_rule_does_not_leak_across_organizations(session: AsyncSession) -> None:
    org_a = await _org(session)
    org_b = await _org(session)
    workspace = await _memberless_workspace(session, org_b)
    session.add(
        WorkspaceAccessRule(
            organization_id=org_a.id, code="seed-analyst", subject_role="Analyst",
            workspace_role="analyst", created_by="x",
        )
    )
    await session.flush()
    derived = await rule_derived_roles(
        session, workspace, frozenset({"Analyst"}), now=_NOW
    )
    assert derived == frozenset()


async def test_a_node_scoped_rule_only_covers_workspaces_under_that_node(
    session: AsyncSession,
) -> None:
    """"Analysts get read access to every workspace under Retail Banking" -- one rule."""
    org = await _org(session)
    retail = BusinessNode(organization_id=org.id, kind="LOB", name="Retail", code="LOB:R")
    markets = BusinessNode(organization_id=org.id, kind="LOB", name="Markets", code="LOB:M")
    session.add_all([retail, markets])
    await session.flush()
    cards = BusinessNode(
        organization_id=org.id, parent_id=retail.id, kind="DOMAIN", name="Cards", code="DOM:C"
    )
    session.add(cards)
    await session.flush()

    inside = await _memberless_workspace(session, org)
    outside = await _memberless_workspace(session, org)
    await assign(session, organization_id=org.id, business_node_id=cards.id,
                 target_type="WORKSPACE", target_id=str(inside.id), assigned_by="s")
    await assign(session, organization_id=org.id, business_node_id=markets.id,
                 target_type="WORKSPACE", target_id=str(outside.id), assigned_by="s")

    session.add(
        WorkspaceAccessRule(
            organization_id=org.id, code="retail-analysts", business_node_id=retail.id,
            subject_role="Analyst", workspace_role="analyst", created_by="x",
        )
    )
    await session.flush()

    # `inside` is classified under a sub-domain of Retail, so the rule reaches it.
    assert await rule_derived_roles(session, inside, frozenset({"Analyst"}), now=_NOW) == {
        "analyst"
    }
    assert (
        await rule_derived_roles(session, outside, frozenset({"Analyst"}), now=_NOW)
        == frozenset()
    )


# --- shadow mode -------------------------------------------------------------


async def test_shadow_mode_never_denies_but_records_what_it_would_have(
    session: AsyncSession,
) -> None:
    """Introducing authorization in enforcing mode is how you cause an outage.

    In SHADOW the correct decision is still computed -- the divergence is written down --
    but the request proceeds.
    """
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)  # SHADOW by default
    assert workspace.authorization_mode == "SHADOW"

    result = await authorize_enforced(
        session, _analyst(org), workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE", resource_id="tbl_1", now=_NOW,
    )
    assert result.allowed is True
    assert result.reason_code == "SHADOW_MODE_NOT_ENFORCING"

    recorded = (await session.scalars(select(AuthorizationShadowRecord))).all()
    assert len(recorded) == 1
    assert recorded[0].shadow_allowed is False
    assert recorded[0].reason_code == "NO_WORKSPACE_MEMBERSHIP"


async def test_enforce_mode_actually_denies(session: AsyncSession) -> None:
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    workspace.authorization_mode = ENFORCE
    await session.flush()
    result = await authorize_enforced(
        session, _analyst(org), workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE", now=_NOW,
    )
    assert result.allowed is False
    assert result.reason_code == "NO_WORKSPACE_MEMBERSHIP"


async def test_shadow_mode_records_nothing_when_the_engine_agrees(
    session: AsyncSession,
) -> None:
    """Agreements are counted, not stored. Otherwise this is a second access log."""
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    session.add(
        WorkspaceAccessRule(
            organization_id=org.id, code="seed-analyst", subject_role="Analyst",
            workspace_role="analyst", created_by="x",
        )
    )
    await session.flush()
    result = await authorize_enforced(
        session, _analyst(org), workspace_id=workspace.id,
        action="READ_METADATA", resource_type="TABLE", now=_NOW,
    )
    assert result.allowed is True
    assert (await session.scalars(select(AuthorizationShadowRecord))).all() == []


async def test_readiness_reports_what_would_break_before_enforcing(
    session: AsyncSession,
) -> None:
    """Flipping to ENFORCE should be a measurement, not a leap."""
    org = await _org(session)
    workspace = await _memberless_workspace(session, org)
    for principal in ("alice", "bob", "alice"):
        await authorize_enforced(
            session,
            SecurityContext(principal_id=principal, principal_type="USER",
                            organization_id=org.id, roles=frozenset({"Analyst"})),
            workspace_id=workspace.id, action="READ_METADATA",
            resource_type="TABLE", now=_NOW,
        )
    report = await enforcement_readiness(session, workspace)
    assert report.would_be_denials == 3
    assert report.distinct_principals_affected == 2
    assert report.top_reason_codes[0][0] == "NO_WORKSPACE_MEMBERSHIP"
    assert report.ready is False


async def test_a_workspace_with_no_divergences_reports_ready(session: AsyncSession) -> None:
    org = await _org(session)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p",
        owner_principal="alice",
    )
    report = await enforcement_readiness(session, workspace)
    assert report.would_be_denials == 0
    assert report.ready is True


# --- the bug class, pinned ---------------------------------------------------


def test_timestamp_comparison_survives_a_naive_stored_value() -> None:
    """The defect that was written three times in one day, pinned once.

    PostgreSQL returns aware timestamps, SQLite returns naive ones. A bare comparison
    either raises TypeError or -- worse -- silently answers wrongly on one backend. These
    are expiry checks on access grants, so a backend-dependent answer is the worst
    available failure shape: no single test environment reveals it.
    """
    from datetime import datetime as _dt

    from aida.timeutil import is_expired, is_live, same_instant

    naive_past = _dt(2026, 1, 1)                       # as SQLite hands it back
    aware_now = _dt(2026, 8, 30, tzinfo=UTC)           # as the application produces it

    assert is_expired(naive_past, aware_now) is True
    assert is_live(naive_past, aware_now) is False
    assert is_expired(None, aware_now) is False
    assert same_instant(_dt(2026, 8, 30), aware_now) is True
    assert same_instant(None, aware_now) is False
