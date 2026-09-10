"""Characterization suite for the knowledge-graph neighborhood read path.

R02 names `intelligence_api.get_knowledge_graph_neighborhood` as a refactoring
hotspot. Before this file the endpoint had *no* behavioural coverage at all:
`tests/test_knowledge_graph.py` exercises the pure frontier algorithm
(`expand_frontier` / `expand_cross_source_frontier`) and asserts the OpenAPI
path exists, but nothing pinned the handler's own rules -- bounds, ordering,
policy filtering, or refusals.

These tests are the contract the extraction in
`aida.knowledge_graph_neighborhood` must not change. They deliberately assert
observable response content (node order, edge ids, truncation reasons, totals)
rather than internal structure, so they stay meaningful on either side of the
move.

The invariant that matters most here is ADR-0017's per-read grant rule: a
`RelationshipCandidate` crossing into another datasource may only render when
that datasource's data_domain still holds an ACTIVE `CrossBoundaryGrant` *and*
the caller's own RBAC gate allows `READ_METADATA` on it. A refusal is
indistinguishable from absence (INV-6) -- no truncation reason, no field.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.intelligence_api import get_knowledge_graph_neighborhood
from aida.models import (
    CrossBoundaryGrant,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    RelationshipCandidate,
)
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as session:
        yield session
    await engine.dispose()


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]


def _context(organization_id: UUID, *, roles: tuple[str, ...] = ("Analyst",)) -> SecurityContext:
    return SecurityContext(
        principal_id="analyst@example.com",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles),
    )


class _World:
    """One organization, one line of business, and however many datasources a
    test needs -- each pinned to a named data_domain so cross-domain grants can
    be granted or withheld explicitly."""

    def __init__(self, session: AsyncSession, organization: Organization, lob: LineOfBusiness):
        self.session = session
        self.organization = organization
        self.lob = lob
        self.domains: dict[str, DataDomain] = {}

    async def domain(self, name: str) -> DataDomain:
        existing = self.domains.get(name)
        if existing is not None:
            return existing
        domain = DataDomain(
            id=uuid4(),
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name=name,
            code=f"{name[:3].upper()}{uuid4().hex[:6]}",
        )
        self.session.add(domain)
        await self.session.flush()
        self.domains[name] = domain
        return domain

    async def datasource(self, name: str, *, domain_name: str = "core") -> DataSource:
        domain = await self.domain(domain_name)
        project = Project(
            id=uuid4(),
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=domain.id,
            name=f"project-{name}",
            slug=f"prj-{uuid4().hex[:8]}",
        )
        datasource = DataSource(
            id=uuid4(),
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=domain.id,
            project_id=project.id,
            name=name,
            connector_type="postgres",
            dialect="postgres",
            environment="PROD",
            network_zone="default",
            credential_reference="env://TEST_DSN",
            capabilities={},
        )
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=self.organization.id,
            datasource_id=datasource.id,
            name="bank",
            fingerprint="fp",
        )
        self.session.add_all([project, datasource, catalog])
        await self.session.flush()
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="public",
            fingerprint="fp",
        )
        self.session.add(schema)
        await self.session.flush()
        self._schemas[datasource.id] = schema
        return datasource

    _schemas: dict[UUID, MetadataSchema] = {}

    async def table(
        self, datasource: DataSource, name: str, *, status: str = "ACTIVE"
    ) -> MetadataTable:
        table = MetadataTable(
            id=uuid4(),
            organization_id=self.organization.id,
            datasource_id=datasource.id,
            schema_id=self._schemas[datasource.id].id,
            name=name,
            object_type="BASE_TABLE",
            status=status,
            fingerprint="fp",
        )
        self.session.add(table)
        await self.session.flush()
        return table

    async def column(
        self,
        table: MetadataTable,
        name: str,
        *,
        classification: str = "UNCLASSIFIED",
        ordinal: int = 1,
    ) -> MetadataColumn:
        column = MetadataColumn(
            id=uuid4(),
            organization_id=self.organization.id,
            table_id=table.id,
            name=name,
            ordinal_position=ordinal,
            physical_type="text",
            nullable=True,
            classification=classification,
            fingerprint="fp",
        )
        self.session.add(column)
        await self.session.flush()
        return column

    async def foreign_key(
        self, source: MetadataTable, target: MetadataTable, *, name: str
    ) -> MetadataConstraint:
        constraint = MetadataConstraint(
            id=uuid4(),
            organization_id=self.organization.id,
            datasource_id=source.datasource_id,
            table_id=source.id,
            name=name,
            constraint_type="FOREIGN_KEY",
            columns=["ref_id"],
            referenced_table_id=target.id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp",
        )
        self.session.add(constraint)
        await self.session.flush()
        return constraint

    async def candidate(
        self,
        source_column: MetadataColumn,
        target_column: MetadataColumn,
        *,
        source_table: MetadataTable,
        target_table: MetadataTable,
        status: str = "PENDING",
        confidence: float = 0.8,
    ) -> RelationshipCandidate:
        candidate = RelationshipCandidate(
            id=uuid4(),
            organization_id=self.organization.id,
            datasource_id=source_table.datasource_id,
            target_datasource_id=target_table.datasource_id,
            source_table_id=source_table.id,
            source_column_id=source_column.id,
            target_table_id=target_table.id,
            target_column_id=target_column.id,
            detection_rule="NAME_MATCH",
            confidence=confidence,
            evidence={"rule": "NAME_MATCH"},
            status=status,
            created_by="inference",
        )
        self.session.add(candidate)
        await self.session.flush()
        return candidate

    async def grant(
        self,
        *,
        source_domain: DataDomain,
        target_domain: DataDomain,
        status: str = "ACTIVE",
        expires_at: datetime | None = None,
    ) -> CrossBoundaryGrant:
        grant = CrossBoundaryGrant(
            id=uuid4(),
            organization_id=self.organization.id,
            source_data_domain_id=source_domain.id,
            target_data_domain_id=target_domain.id,
            edge_kinds=[],
            reason="characterization",
            status=status,
            requested_by="steward@example.com",
            approved_by="approver@example.com",
            approved_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self.session.add(grant)
        await self.session.flush()
        return grant


async def _world(session: AsyncSession) -> _World:
    org = Organization(id=uuid4(), name=f"org-{uuid4().hex[:8]}", slug=f"org-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    session.add_all([org, lob])
    await session.flush()
    world = _World(session, org, lob)
    world._schemas = {}
    return world


# ---------------------------------------------------------------------------
# Shape, ordering and totals
# ---------------------------------------------------------------------------


async def test_neighborhood_projects_focus_and_declared_foreign_key_neighbors(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    orders = await world.table(source, "orders")
    customers = await world.table(source, "customers")
    await world.column(orders, "customer_id", classification="PII")
    await world.column(orders, "amount", ordinal=2)
    await world.foreign_key(orders, customers, name="fk_orders_customers")

    result = await get_knowledge_graph_neighborhood(
        source.id,
        orders.id,
        depth=1,
        direction="BOTH",
        suggestion_status="ALL",
        node_limit=100,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )

    assert result.focus_node_id == orders.id
    assert result.direction == "BOTH"
    assert result.requested_depth == 1
    # Nodes are ordered by (depth, qualified_name) -- focus first at depth 0.
    assert [node.qualified_name for node in result.nodes] == [
        "bank.public.orders",
        "bank.public.customers",
    ]
    assert [node.depth for node in result.nodes] == [0, 1]
    assert result.nodes[0].column_count == 2
    assert result.nodes[0].sensitive_column_count == 1
    assert [edge.id for edge in result.edges] == [
        f"constraint:{(await _only_constraint(db_session)).id}"
    ]
    edge = result.edges[0]
    assert edge.edge_type == "DECLARED_FOREIGN_KEY"
    assert edge.status == "DECLARED"
    assert edge.confidence == 1.0
    assert edge.evidence == {"source": "DATABASE_CONSTRAINT", "source_values_inspected": False}
    assert edge.source_label == "bank.public.orders"
    assert edge.target_label == "bank.public.customers"
    assert result.total_tables == 2
    assert result.total_declared_edges == 1
    assert result.total_suggested_edges == 0
    assert result.pending_suggestions == 0
    assert result.returned_node_count == 2
    assert result.returned_edge_count == 1
    assert result.truncated is False
    assert result.truncation_reasons == []
    # Edge counts are computed from the *returned* edges, not the totals.
    assert result.nodes[0].outbound_edge_count == 1
    assert result.nodes[1].inbound_edge_count == 1


async def _only_constraint(session: AsyncSession) -> MetadataConstraint:
    from sqlalchemy import select

    return (await session.scalars(select(MetadataConstraint))).one()


async def test_neighborhood_of_an_isolated_table_returns_only_the_focus(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    lonely = await world.table(source, "lonely")

    result = await get_knowledge_graph_neighborhood(
        source.id,
        lonely.id,
        depth=3,
        direction="BOTH",
        suggestion_status="ALL",
        node_limit=100,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )

    assert [node.id for node in result.nodes] == [lonely.id]
    assert result.edges == []
    assert result.truncation_reasons == []
    assert result.nodes[0].column_count == 0
    assert result.nodes[0].sensitive_column_count == 0


async def test_neighborhood_direction_selects_which_side_of_a_foreign_key_expands(
    db_session,
) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    orders = await world.table(source, "orders")
    customers = await world.table(source, "customers")
    await world.foreign_key(orders, customers, name="fk_orders_customers")

    common = dict(
        depth=1,
        suggestion_status="ALL",
        node_limit=100,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )
    references = await get_knowledge_graph_neighborhood(
        source.id, orders.id, direction="REFERENCES", **common
    )
    referenced_by = await get_knowledge_graph_neighborhood(
        source.id, orders.id, direction="REFERENCED_BY", **common
    )

    # orders --FK--> customers. From orders, REFERENCES reaches customers;
    # REFERENCED_BY reaches nothing (nothing points at orders).
    assert {node.id for node in references.nodes} == {orders.id, customers.id}
    assert {node.id for node in referenced_by.nodes} == {orders.id}


async def test_neighborhood_depth_bounds_how_far_the_chain_is_followed(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    a = await world.table(source, "a_table")
    b = await world.table(source, "b_table")
    c = await world.table(source, "c_table")
    await world.foreign_key(a, b, name="fk_a_b")
    await world.foreign_key(b, c, name="fk_b_c")

    common = dict(
        direction="REFERENCES",
        suggestion_status="ALL",
        node_limit=100,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )
    one_hop = await get_knowledge_graph_neighborhood(source.id, a.id, depth=1, **common)
    two_hops = await get_knowledge_graph_neighborhood(source.id, a.id, depth=2, **common)

    assert {node.qualified_name for node in one_hop.nodes} == {
        "bank.public.a_table",
        "bank.public.b_table",
    }
    assert {node.qualified_name for node in two_hops.nodes} == {
        "bank.public.a_table",
        "bank.public.b_table",
        "bank.public.c_table",
    }
    assert {node.qualified_name: node.depth for node in two_hops.nodes}["bank.public.c_table"] == 2


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


async def test_neighborhood_node_limit_truncates_and_names_the_reason(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    hub = await world.table(source, "hub")
    for index in range(5):
        spoke = await world.table(source, f"spoke_{index}")
        await world.foreign_key(hub, spoke, name=f"fk_hub_spoke_{index}")

    result = await get_knowledge_graph_neighborhood(
        source.id,
        hub.id,
        depth=1,
        direction="REFERENCES",
        suggestion_status="ALL",
        node_limit=5,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )

    assert "NODE_LIMIT" in result.truncation_reasons
    assert result.truncated is True
    assert result.returned_node_count <= 5
    # The bound is on the traversal, not on the honest totals.
    assert result.total_tables == 6


async def test_neighborhood_edge_limit_truncates_and_names_the_reason(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    hub = await world.table(source, "hub")
    for index in range(4):
        spoke = await world.table(source, f"spoke_{index}")
        await world.foreign_key(hub, spoke, name=f"fk_hub_spoke_{index}")

    result = await get_knowledge_graph_neighborhood(
        source.id,
        hub.id,
        depth=1,
        direction="REFERENCES",
        suggestion_status="ALL",
        node_limit=100,
        edge_limit=2,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )

    assert result.returned_edge_count == 2
    assert result.truncated is True
    assert set(result.truncation_reasons) & {"EDGE_LIMIT", "EDGE_SCAN_LIMIT"}


async def test_neighborhood_refuses_bounds_above_configured_policy(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    focus = await world.table(source, "orders")
    settings = _settings(
        knowledge_graph_max_depth=2,
        knowledge_graph_max_nodes=50,
        knowledge_graph_max_edges=100,
    )
    common = dict(
        direction="BOTH",
        suggestion_status="ALL",
        context=_context(world.organization.id),
        session=db_session,
        settings=settings,
    )

    for kwargs, detail in (
        (dict(depth=3, node_limit=10, edge_limit=10), "requested graph depth exceeds policy"),
        (
            dict(depth=1, node_limit=51, edge_limit=10),
            "requested graph node limit exceeds policy",
        ),
        (
            dict(depth=1, node_limit=10, edge_limit=101),
            "requested graph edge limit exceeds policy",
        ),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await get_knowledge_graph_neighborhood(source.id, focus.id, **kwargs, **common)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == detail


# ---------------------------------------------------------------------------
# Suggestion status filtering
# ---------------------------------------------------------------------------


async def test_neighborhood_suggestion_status_filters_candidate_edges(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    orders = await world.table(source, "orders")
    customers = await world.table(source, "customers")
    accounts = await world.table(source, "accounts")
    orders_customer = await world.column(orders, "customer_id")
    orders_account = await world.column(orders, "account_id", ordinal=2)
    customers_id = await world.column(customers, "id")
    accounts_id = await world.column(accounts, "id")
    pending = await world.candidate(
        orders_customer,
        customers_id,
        source_table=orders,
        target_table=customers,
        status="PENDING",
    )
    approved = await world.candidate(
        orders_account,
        accounts_id,
        source_table=orders,
        target_table=accounts,
        status="APPROVED",
    )

    common = dict(
        depth=1,
        direction="BOTH",
        node_limit=100,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )
    everything = await get_knowledge_graph_neighborhood(
        source.id, orders.id, suggestion_status="ALL", **common
    )
    only_approved = await get_knowledge_graph_neighborhood(
        source.id, orders.id, suggestion_status="APPROVED", **common
    )

    assert {edge.id for edge in everything.edges} == {
        f"candidate:{pending.id}",
        f"candidate:{approved.id}",
    }
    assert {edge.id for edge in only_approved.edges} == {f"candidate:{approved.id}"}
    # Totals are status-independent and datasource-scoped, always.
    assert everything.total_suggested_edges == 2
    assert everything.pending_suggestions == 1
    assert only_approved.total_suggested_edges == 2
    assert only_approved.pending_suggestions == 1
    candidate_edge = next(edge for edge in only_approved.edges if edge.candidate_id is not None)
    assert candidate_edge.candidate_id == approved.id
    assert candidate_edge.edge_type == "SUGGESTED_RELATIONSHIP"
    assert candidate_edge.source_columns == ["account_id"]
    assert candidate_edge.target_columns == ["id"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_neighborhood_refuses_an_unknown_datasource(db_session) -> None:
    world = await _world(db_session)
    with pytest.raises(HTTPException) as excinfo:
        await get_knowledge_graph_neighborhood(
            uuid4(),
            uuid4(),
            depth=1,
            direction="BOTH",
            suggestion_status="ALL",
            node_limit=10,
            edge_limit=10,
            context=_context(world.organization.id),
            session=db_session,
            settings=_settings(),
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "datasource not found"


async def test_neighborhood_refuses_a_caller_from_another_organization(db_session) -> None:
    world = await _world(db_session)
    source = await world.datasource("primary")
    focus = await world.table(source, "orders")

    with pytest.raises(HTTPException) as excinfo:
        await get_knowledge_graph_neighborhood(
            source.id,
            focus.id,
            depth=1,
            direction="BOTH",
            suggestion_status="ALL",
            node_limit=10,
            edge_limit=10,
            context=_context(uuid4()),
            session=db_session,
            settings=_settings(),
        )
    assert excinfo.value.status_code == 403


async def test_neighborhood_refuses_a_focus_table_outside_the_datasource(db_session) -> None:
    world = await _world(db_session)
    primary = await world.datasource("primary")
    other = await world.datasource("other")
    foreign_focus = await world.table(other, "orders")
    retired = await world.table(primary, "retired", status="DEPRECATED")

    for focus_id in (uuid4(), foreign_focus.id, retired.id):
        with pytest.raises(HTTPException) as excinfo:
            await get_knowledge_graph_neighborhood(
                primary.id,
                focus_id,
                depth=1,
                direction="BOTH",
                suggestion_status="ALL",
                node_limit=10,
                edge_limit=10,
                context=_context(world.organization.id),
                session=db_session,
                settings=_settings(),
            )
        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "active graph focus table not found"


# ---------------------------------------------------------------------------
# ADR-0017 cross-source policy -- the invariant this refactor must not widen
# ---------------------------------------------------------------------------


async def _cross_source_fixture(db_session) -> tuple[_World, DataSource, MetadataTable, UUID]:
    """orders (datasource `primary`, domain `core`) --candidate--> partners.leads
    (datasource `partner`, domain `partner`). Rendering that edge requires a
    grant letting `core` see into `partner`."""
    world = await _world(db_session)
    primary = await world.datasource("primary", domain_name="core")
    partner = await world.datasource("partner", domain_name="partner")
    orders = await world.table(primary, "orders")
    leads = await world.table(partner, "leads")
    orders_key = await world.column(orders, "lead_id")
    leads_key = await world.column(leads, "id")
    candidate = await world.candidate(
        orders_key,
        leads_key,
        source_table=orders,
        target_table=leads,
        status="APPROVED",
    )
    return world, primary, orders, candidate.id


async def _neighborhood(
    db_session, world: _World, datasource: DataSource, focus: MetadataTable, **overrides
):
    kwargs = dict(
        depth=2,
        direction="BOTH",
        suggestion_status="ALL",
        node_limit=100,
        edge_limit=500,
        context=_context(world.organization.id),
        session=db_session,
        settings=_settings(),
    )
    kwargs.update(overrides)
    return await get_knowledge_graph_neighborhood(datasource.id, focus.id, **kwargs)


async def test_cross_source_edge_is_withheld_without_an_active_grant(db_session) -> None:
    world, primary, orders, candidate_id = await _cross_source_fixture(db_session)

    result = await _neighborhood(db_session, world, primary, orders)

    # INV-6: the refusal is indistinguishable from absence. No node from the
    # other datasource, no edge, and no truncation reason naming the refusal.
    assert [node.qualified_name for node in result.nodes] == ["bank.public.orders"]
    assert result.edges == []
    assert result.truncation_reasons == []


async def test_cross_source_edge_renders_once_a_grant_is_active(db_session) -> None:
    world, primary, orders, candidate_id = await _cross_source_fixture(db_session)
    await world.grant(source_domain=world.domains["partner"], target_domain=world.domains["core"])

    result = await _neighborhood(db_session, world, primary, orders)

    assert {node.qualified_name for node in result.nodes} == {
        "bank.public.orders",
        "bank.public.leads",
    }
    assert [edge.id for edge in result.edges] == [f"candidate:{candidate_id}"]


async def test_cross_source_edge_is_withheld_when_the_grant_is_not_active(db_session) -> None:
    world, primary, orders, _ = await _cross_source_fixture(db_session)
    await world.grant(
        source_domain=world.domains["partner"],
        target_domain=world.domains["core"],
        status="PENDING_APPROVAL",
    )

    result = await _neighborhood(db_session, world, primary, orders)

    assert [node.qualified_name for node in result.nodes] == ["bank.public.orders"]
    assert result.edges == []


# NOTE: the "grant has expired" case is deliberately absent. It cannot be
# characterized here: SQLite stores `DateTime(timezone=True)` naive, so
# `domain_service.check_cross_boundary_grant`'s `grant.expires_at <= now`
# raises `TypeError: can't compare offset-naive and offset-aware datetimes`
# against the in-memory database these tests use. That is a real (Postgres-only
# green) fragility in `domain_service.py` -- a file this lane does not own --
# not a property of the neighborhood path, so it is recorded rather than
# papered over with a test that would pass for the wrong reason.


async def test_cross_source_edge_is_withheld_when_the_grant_points_the_wrong_way(
    db_session,
) -> None:
    """A grant letting `partner` see into `core` does not let `core` see into
    `partner`. Direction is part of the grant, not a formality."""
    world, primary, orders, _ = await _cross_source_fixture(db_session)
    await world.grant(source_domain=world.domains["core"], target_domain=world.domains["partner"])

    result = await _neighborhood(db_session, world, primary, orders)

    assert [node.qualified_name for node in result.nodes] == ["bank.public.orders"]
    assert result.edges == []


async def test_cross_source_edge_is_withheld_when_the_grant_excludes_this_edge_kind(
    db_session,
) -> None:
    world, primary, orders, _ = await _cross_source_fixture(db_session)
    grant = await world.grant(
        source_domain=world.domains["partner"], target_domain=world.domains["core"]
    )
    grant.edge_kinds = ["FOREIGN_KEY_INFERRED"]
    await db_session.flush()

    result = await _neighborhood(db_session, world, primary, orders)

    assert [node.qualified_name for node in result.nodes] == ["bank.public.orders"]
    assert result.edges == []


async def test_cross_source_edge_is_withheld_when_the_rbac_gate_refuses(db_session) -> None:
    """The grant is a domain-level decision; `gate()` is the caller's own RBAC.
    Both must pass. With `unresolved_workspace_posture=DENY` and no workspace to
    resolve, the gate refuses `READ_METADATA` on the other datasource -- and the
    edge disappears even though the grant is ACTIVE."""
    world, primary, orders, _ = await _cross_source_fixture(db_session)
    await world.grant(source_domain=world.domains["partner"], target_domain=world.domains["core"])

    result = await _neighborhood(
        db_session,
        world,
        primary,
        orders,
        settings=_settings(unresolved_workspace_posture="DENY"),
    )

    assert [node.qualified_name for node in result.nodes] == ["bank.public.orders"]
    assert result.edges == []


async def test_same_domain_cross_source_edge_needs_no_grant(db_session) -> None:
    """ADR-0017 SS4/SS8: two datasources inside one data_domain are not a
    boundary crossing, so `check_cross_boundary_grant` is not consulted."""
    world = await _world(db_session)
    primary = await world.datasource("primary", domain_name="core")
    secondary = await world.datasource("secondary", domain_name="core")
    orders = await world.table(primary, "orders")
    leads = await world.table(secondary, "leads")
    orders_key = await world.column(orders, "lead_id")
    leads_key = await world.column(leads, "id")
    candidate = await world.candidate(
        orders_key, leads_key, source_table=orders, target_table=leads, status="APPROVED"
    )

    result = await _neighborhood(db_session, world, primary, orders)

    assert {node.qualified_name for node in result.nodes} == {
        "bank.public.orders",
        "bank.public.leads",
    }
    assert [edge.id for edge in result.edges] == [f"candidate:{candidate.id}"]


async def test_cross_organization_candidate_never_crosses_even_with_a_grant(db_session) -> None:
    """A candidate row naming a datasource in another organization fails closed
    regardless of grants -- INV-4/INV-5 are not overridable by row content."""
    world = await _world(db_session)
    primary = await world.datasource("primary", domain_name="core")
    orders = await world.table(primary, "orders")
    orders_key = await world.column(orders, "lead_id")

    other_org = Organization(
        id=uuid4(), name=f"org-{uuid4().hex[:8]}", slug=f"org-{uuid4().hex[:8]}"
    )
    db_session.add(other_org)
    await db_session.flush()
    foreign_world = _World(db_session, other_org, world.lob)
    foreign_world._schemas = {}
    foreign_lob = LineOfBusiness(
        id=uuid4(), organization_id=other_org.id, name="Other", code=f"OTH{uuid4().hex[:6]}"
    )
    db_session.add(foreign_lob)
    await db_session.flush()
    foreign_world.lob = foreign_lob
    foreign_source = await foreign_world.datasource("foreign", domain_name="foreign")
    foreign_table = await foreign_world.table(foreign_source, "leads")
    foreign_key_column = await foreign_world.column(foreign_table, "id")

    await world.candidate(
        orders_key,
        foreign_key_column,
        source_table=orders,
        target_table=foreign_table,
        status="APPROVED",
    )

    result = await _neighborhood(db_session, world, primary, orders)

    assert [node.qualified_name for node in result.nodes] == ["bank.public.orders"]
    assert result.edges == []
