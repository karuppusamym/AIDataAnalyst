"""Integration coverage for the ADR-0018 access and classification axes.

These are the first tests in this repository that run against a real database
rather than a hand-rolled fake. That is deliberate: the behaviour under test is
recursive CTE traversal, effective-dated history and multi-step authorization, and
faking a session would assert that the fake behaves, not that the SQL does.

SQLite in memory is enough here -- every construct used (recursive CTEs, JSON
columns, UUID keys, timestamp comparison) is portable, and the schema creates
cleanly. Anything genuinely PostgreSQL-specific stays out of this file.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.business_graph import (
    ancestor_closure,
    assign,
    classification_scope,
    descendant_ids,
    load_policies,
    nodes_for_target,
    rollup,
)
from aida.db import Base
from aida.models import (
    AccessPolicy,
    BusinessNode,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
    WorkspaceMembership,
)
from aida.security_types import SecurityContext
from aida.workspace_service import (
    BindingApprovalError,
    approve_binding,
    authorize,
    create_workspace,
    request_binding,
)

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


async def _organization(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _datasource(session: AsyncSession, org: Organization) -> DataSource:
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:4]}")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Ungoverned", code="UNGOVERNED"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()
    return datasource


async def _allow_everything(session: AsyncSession, org: Organization) -> None:
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="rbac-parity",
            name="RBAC parity",
            effect="ALLOW",
            subject_match={"roles": ["analyst", "workspace_owner"]},
            action_match=[],
            created_by="test",
        )
    )
    await session.flush()


def _context(org: Organization, principal: str = "alice") -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Analyst"}),
    )


# --- workspace lifecycle ----------------------------------------------------


async def test_creating_a_workspace_seats_its_owner(session: AsyncSession) -> None:
    """A workspace with no owner is one nobody can administer, so the two happen together."""
    org = await _organization(session)
    workspace = await create_workspace(
        session,
        organization_id=org.id,
        name="Risk Analytics",
        slug="risk-analytics",
        purpose="model validation",
        owner_principal="alice",
    )
    membership = await session.scalar(
        select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id)
    )
    assert membership is not None
    assert membership.role == "workspace_owner"
    assert membership.principal_id == "alice"


async def test_authorization_denies_a_principal_with_no_membership(session: AsyncSession) -> None:
    org = await _organization(session)
    await _allow_everything(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="owner"
    )
    result = await authorize(
        session,
        _context(org, "stranger"),
        workspace_id=workspace.id,
        action="READ_METADATA",
        resource_type="TABLE",
    )
    assert result.allowed is False
    assert result.reason_code == "NO_WORKSPACE_MEMBERSHIP"


async def test_authorization_denies_across_organizations(session: AsyncSession) -> None:
    """403-shaped refusal, so "exists elsewhere" and "does not exist" look identical."""
    org_a = await _organization(session)
    org_b = await _organization(session)
    workspace = await create_workspace(
        session, organization_id=org_a.id, name="W", slug="w", purpose="p", owner_principal="alice"
    )
    result = await authorize(
        session,
        _context(org_b, "alice"),
        workspace_id=workspace.id,
        action="READ_METADATA",
        resource_type="TABLE",
    )
    assert result.allowed is False
    assert result.reason_code == "CROSS_ORGANIZATION_DENIED"


async def test_a_role_that_does_not_permit_the_action_is_refused(session: AsyncSession) -> None:
    org = await _organization(session)
    await _allow_everything(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="owner"
    )
    session.add(
        WorkspaceMembership(
            organization_id=org.id,
            workspace_id=workspace.id,
            principal_id="viewer-only",
            role="viewer",
            granted_by="owner",
        )
    )
    await session.flush()
    result = await authorize(
        session,
        _context(org, "viewer-only"),
        workspace_id=workspace.id,
        action="APPROVE",
        resource_type="TABLE",
    )
    assert result.allowed is False
    assert result.reason_code == "ROLE_DOES_NOT_PERMIT_ACTION"


# --- source bindings --------------------------------------------------------


async def test_reaching_a_datasource_requires_an_active_binding(session: AsyncSession) -> None:
    org = await _organization(session)
    await _allow_everything(session, org)
    datasource = await _datasource(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="alice"
    )
    result = await authorize(
        session,
        _context(org),
        workspace_id=workspace.id,
        action="READ_DATA",
        resource_type="TABLE",
        datasource_id=datasource.id,
    )
    assert result.allowed is False
    assert result.reason_code == "NO_ACTIVE_SOURCE_BINDING"


async def test_an_approved_binding_permits_access_and_an_expired_one_does_not(
    session: AsyncSession,
) -> None:
    """Expiry is the mechanism that stops entitlement creep, so it must actually bite."""
    org = await _organization(session)
    await _allow_everything(session, org)
    datasource = await _datasource(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="alice"
    )
    binding = await request_binding(
        session,
        organization_id=org.id,
        workspace_id=workspace.id,
        datasource_id=datasource.id,
        purpose="reporting",
        requested_by="alice",
    )
    await approve_binding(session, binding, approver_principal="bob", valid_for_days=30, now=_NOW)

    inside = await authorize(
        session,
        _context(org),
        workspace_id=workspace.id,
        action="READ_DATA",
        resource_type="TABLE",
        datasource_id=datasource.id,
        now=_NOW + timedelta(days=1),
    )
    assert inside.allowed is True

    after = await authorize(
        session,
        _context(org),
        workspace_id=workspace.id,
        action="READ_DATA",
        resource_type="TABLE",
        datasource_id=datasource.id,
        now=_NOW + timedelta(days=31),
    )
    assert after.allowed is False
    assert after.reason_code == "NO_ACTIVE_SOURCE_BINDING"


async def test_a_binding_is_confined_to_its_schema_scope(session: AsyncSession) -> None:
    org = await _organization(session)
    await _allow_everything(session, org)
    datasource = await _datasource(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="alice"
    )
    binding = await request_binding(
        session,
        organization_id=org.id,
        workspace_id=workspace.id,
        datasource_id=datasource.id,
        purpose="reporting",
        requested_by="alice",
        schema_scope=["rtl"],
    )
    await approve_binding(session, binding, approver_principal="bob", now=_NOW)

    permitted = await authorize(
        session, _context(org), workspace_id=workspace.id, action="READ_DATA",
        resource_type="TABLE", datasource_id=datasource.id, schema_name="rtl", now=_NOW,
    )
    refused = await authorize(
        session, _context(org), workspace_id=workspace.id, action="READ_DATA",
        resource_type="TABLE", datasource_id=datasource.id, schema_name="fin", now=_NOW,
    )
    assert permitted.allowed is True
    assert refused.allowed is False
    assert refused.reason_code == "OUTSIDE_BINDING_SCHEMA_SCOPE"


async def test_the_requester_of_a_binding_cannot_approve_it(session: AsyncSession) -> None:
    """INV-8. A binding is a grant of source reach; self-approval must not exist."""
    org = await _organization(session)
    datasource = await _datasource(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="alice"
    )
    binding = await request_binding(
        session,
        organization_id=org.id,
        workspace_id=workspace.id,
        datasource_id=datasource.id,
        purpose="reporting",
        requested_by="alice",
    )
    with pytest.raises(BindingApprovalError) as denied:
        await approve_binding(session, binding, approver_principal="alice")
    assert denied.value.reason_code == "MAKER_CHECKER_SEPARATION_REQUIRED"
    assert binding.status == "PENDING_APPROVAL"


# --- classification axis ----------------------------------------------------


async def _tree(session: AsyncSession, org: Organization) -> tuple[BusinessNode, ...]:
    lob = BusinessNode(organization_id=org.id, kind="LOB", name="Retail", code="LOB:RTL")
    session.add(lob)
    await session.flush()
    domain = BusinessNode(
        organization_id=org.id, parent_id=lob.id, kind="DOMAIN", name="Cards", code="DOM:CARDS"
    )
    session.add(domain)
    await session.flush()
    sub = BusinessNode(
        organization_id=org.id, parent_id=domain.id, kind="SUB_DOMAIN", name="Credit",
        code="DOM:CARDS:CREDIT",
    )
    other = BusinessNode(organization_id=org.id, kind="LOB", name="Markets", code="LOB:MKT")
    session.add_all([sub, other])
    await session.flush()
    return lob, domain, sub, other


async def test_descendants_and_ancestors_walk_the_tree(session: AsyncSession) -> None:
    org = await _organization(session)
    lob, domain, sub, other = await _tree(session, org)

    below = await descendant_ids(session, org.id, lob.id)
    assert below == {lob.id, domain.id, sub.id}
    assert other.id not in below

    above = await ancestor_closure(session, org.id, [sub.id])
    assert above == {sub.id, domain.id, lob.id}


async def test_an_asset_can_belong_to_two_sibling_domains(session: AsyncSession) -> None:
    """The exact case that superseded ADR-0017.

    ADR-0017 recorded its own reversal condition as "a table genuinely needs two
    sibling domains". A containment hierarchy cannot express it; this can.
    """
    org = await _organization(session)
    retail = BusinessNode(organization_id=org.id, kind="LOB", name="Retail", code="LOB:RTL")
    crime = BusinessNode(organization_id=org.id, kind="DOMAIN", name="FinCrime", code="DOM:FC")
    session.add_all([retail, crime])
    await session.flush()

    for node in (retail, crime):
        await assign(
            session,
            organization_id=org.id,
            business_node_id=node.id,
            target_type="TABLE",
            target_id="customer",
            assigned_by="steward",
        )

    nodes = await nodes_for_target(session, org.id, "TABLE", "customer")
    assert set(nodes) == {retail.id, crime.id}


async def test_classification_scope_includes_ancestors(session: AsyncSession) -> None:
    """A policy written against a parent must cover an asset assigned only to a child."""
    org = await _organization(session)
    lob, domain, sub, _ = await _tree(session, org)
    await assign(
        session,
        organization_id=org.id,
        business_node_id=sub.id,
        target_type="TABLE",
        target_id="card_txn",
        assigned_by="steward",
    )
    scope = await classification_scope(session, org.id, "TABLE", "card_txn")
    assert scope == {sub.id, domain.id, lob.id}


async def test_reassignment_supersedes_rather_than_overwrites(session: AsyncSession) -> None:
    """History stays queryable, which is what lets a past decision be replayed."""
    org = await _organization(session)
    lob, _, _, _ = await _tree(session, org)
    first = await assign(
        session, organization_id=org.id, business_node_id=lob.id, target_type="TABLE",
        target_id="t", assigned_by="steward", as_of=_NOW,
    )
    second = await assign(
        session, organization_id=org.id, business_node_id=lob.id, target_type="TABLE",
        target_id="t", assigned_by="steward", as_of=_NOW + timedelta(days=1),
    )
    assert first.status == "SUPERSEDED"
    assert first.effective_to == _NOW + timedelta(days=1)
    assert second.status == "ACTIVE"

    live_now = await nodes_for_target(
        session, org.id, "TABLE", "t", as_of=_NOW + timedelta(days=2)
    )
    assert live_now == (lob.id,)


async def test_the_tree_is_queryable_as_of_a_past_moment(session: AsyncSession) -> None:
    """A reorganisation must not rewrite what an audit record meant at the time."""
    org = await _organization(session)
    lob, _, _, _ = await _tree(session, org)
    await assign(
        session, organization_id=org.id, business_node_id=lob.id, target_type="TABLE",
        target_id="t", assigned_by="steward", as_of=_NOW,
    )
    before = await nodes_for_target(session, org.id, "TABLE", "t", as_of=_NOW - timedelta(days=1))
    after = await nodes_for_target(session, org.id, "TABLE", "t", as_of=_NOW + timedelta(days=1))
    assert before == ()
    assert after == (lob.id,)


async def test_rollup_counts_everything_under_a_node(session: AsyncSession) -> None:
    """"Show me everything under Retail Banking" is a query, not a subsystem."""
    org = await _organization(session)
    lob, domain, sub, other = await _tree(session, org)
    await assign(session, organization_id=org.id, business_node_id=domain.id,
                 target_type="TABLE", target_id="a", assigned_by="s")
    await assign(session, organization_id=org.id, business_node_id=sub.id,
                 target_type="TABLE", target_id="b", assigned_by="s")
    await assign(session, organization_id=org.id, business_node_id=sub.id,
                 target_type="DATASOURCE", target_id="ds", assigned_by="s")
    await assign(session, organization_id=org.id, business_node_id=other.id,
                 target_type="TABLE", target_id="elsewhere", assigned_by="s")

    counts = await rollup(session, org.id, lob.id)
    assert counts == {"TABLE": 2, "DATASOURCE": 1}


# --- policy loading ---------------------------------------------------------


async def test_draft_policies_are_loaded_by_nobody(session: AsyncSession) -> None:
    """A seeded-but-inactive policy must be reviewable without being enforced.

    The ADR-0018 migration seeds the agent sensitive-data DENY as DRAFT precisely
    so that migration day changes no behaviour.
    """
    org = await _organization(session)
    session.add_all(
        [
            AccessPolicy(
                organization_id=org.id, code="live", name="live", effect="ALLOW",
                created_by="t", status="ACTIVE",
            ),
            AccessPolicy(
                organization_id=org.id, code="seeded", name="seeded", effect="DENY",
                created_by="t", status="DRAFT",
            ),
        ]
    )
    await session.flush()
    loaded = await load_policies(session, org.id)
    assert [policy.code for policy in loaded] == ["live"]


async def test_an_active_deny_policy_reaches_the_authorization_path(
    session: AsyncSession,
) -> None:
    """End to end: membership + binding present, and a DENY still refuses."""
    org = await _organization(session)
    await _allow_everything(session, org)
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="agents-no-pii",
            name="Agents may not read PII",
            effect="DENY",
            priority=1000,
            subject_match={"principal_kind": "AGENT"},
            resource_match={"classifications": ["PII"]},
            action_match=["READ_DATA"],
            created_by="t",
        )
    )
    datasource = await _datasource(session, org)
    workspace = await create_workspace(
        session, organization_id=org.id, name="W", slug="w", purpose="p", owner_principal="alice"
    )
    binding = await request_binding(
        session, organization_id=org.id, workspace_id=workspace.id,
        datasource_id=datasource.id, purpose="p", requested_by="alice",
    )
    await approve_binding(session, binding, approver_principal="bob", now=_NOW)

    common = {
        "workspace_id": workspace.id,
        "action": "READ_DATA",
        "resource_type": "TABLE",
        "datasource_id": datasource.id,
        "classifications": frozenset({"PII"}),
        "now": _NOW,
    }
    human = await authorize(session, _context(org), principal_kind="HUMAN", **common)  # type: ignore[arg-type]
    agent = await authorize(session, _context(org), principal_kind="AGENT", **common)  # type: ignore[arg-type]
    assert human.allowed is True
    assert agent.allowed is False
    assert agent.reason_code == "DENIED_BY_POLICY"
