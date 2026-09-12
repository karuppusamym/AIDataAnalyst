"""P1-05 / ADR-0026: coverage for the parsed-lineage-edge review lifecycle.

Uses an in-memory-SQLite + real-ORM pattern. No mocked persistence -- the
point of these tests is to exercise the actual review endpoint
(maker-checker + status flip + audit + outbox), the actual routine-parse
write path under each review mode, and the actual unified-lineage-read
filter.

R11-X5 (2026-09-11) removed `view_lineage_api` and with it the
`_persist_edges` helper these tests once drove for their view-edge setup:
the router had no caller outside this repository's tests, and the
lineage agent (`aida.lineage_agent`) now owns proposing view edges. The
view-edge cases that only existed to pin `_persist_edges`' own re-parse
semantics went with it; the equivalent guarantees on the surviving write
paths are pinned by `TestRoutineEdgesUnderReview` below and by
`tests/test_lineage_agent.py`. Everything else here seeds its view edges
directly through the ORM.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.envelope_models import MetadataRoutine
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
    ViewLineageEdge,
)
from aida.parsed_lineage_review_api import (
    bulk_decide_parsed_lineage_edges,
    decide_parsed_lineage_edge,
)
from aida.parsed_lineage_review_service import (
    list_parsed_lineage_review_queue,
    resolve_review_status_for_new_edge,
)
from aida.procedure_lineage_api import parse_deep_procedure_lineage_endpoint
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.schemas import (
    ParsedLineageEdgeBulkDecisionItem,
    ParsedLineageEdgeBulkDecisionRequest,
    ParsedLineageEdgeDecisionRequest,
)
from aida.security_types import SecurityContext
from atlas.platform.config import get_settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Every test starts from a fresh Settings() so overrides applied
    with monkeypatch.setenv actually take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed(session: AsyncSession, *, table_names: list[str]):
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    tables: dict[str, MetadataTable] = {}
    for name in table_names:
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
        session.add(table)
        tables[name] = table
    await session.flush()
    return datasource, tables


def _context(datasource, principal_id: str = "author") -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


async def _add_view_edge(
    session: AsyncSession,
    datasource,
    tables: dict[str, MetadataTable],
    *,
    source_table: str,
    target_table: str,
    review_status: str = "PROPOSED",
    created_by: str = "author",
    sql_hash: str = "h1",
) -> ViewLineageEdge:
    """One stored view-lineage edge, shaped exactly as a parse would have
    written it. Seeding through the ORM rather than through a parse keeps
    these review-lifecycle tests independent of whichever component
    proposed the edge."""
    edge = ViewLineageEdge(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        source_table=source_table,
        source_column="col_a",
        target_table=target_table,
        target_column="col_a",
        source_table_id=tables[source_table].id,
        target_table_id=tables[target_table].id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="postgres",
        sql_hash=sql_hash,
        review_status=review_status,
        created_by=created_by,
    )
    session.add(edge)
    await session.flush()
    return edge


class TestResolveReviewStatusForNewEdge:
    """The pure decision function is the single hinge -- test it in
    isolation before wiring it into a full parse round-trip."""

    def test_auto_active_always_active(self):
        assert (
            resolve_review_status_for_new_edge(
                review_mode="auto_active",
                confidence="LOW",
                threshold=0.9,
                source_trusted=False,
            )
            == "ACTIVE"
        )

    def test_require_review_low_confidence_lands_proposed(self):
        assert (
            resolve_review_status_for_new_edge(
                review_mode="require_review",
                confidence="LOW",
                threshold=0.9,
                source_trusted=None,
            )
            == "PROPOSED"
        )

    def test_require_review_high_confidence_string_lands_active(self):
        # FULL -> 1.0 >= 0.9 threshold -> auto-active
        assert (
            resolve_review_status_for_new_edge(
                review_mode="require_review",
                confidence="FULL",
                threshold=0.9,
                source_trusted=None,
            )
            == "ACTIVE"
        )

    def test_require_review_trusted_source_bypasses_review(self):
        assert (
            resolve_review_status_for_new_edge(
                review_mode="require_review",
                confidence="LOW",
                threshold=0.9,
                source_trusted=True,
            )
            == "ACTIVE"
        )


class TestDecideParsedLineageEdge:
    async def _seed_proposed_edge(self, session, monkeypatch):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, tables = await _seed(
            session, table_names=["source_table", "my_view"]
        )
        await _add_view_edge(
            session,
            datasource,
            tables,
            source_table="source_table",
            target_table="my_view",
        )
        edge = (
            await session.scalars(
                select(ViewLineageEdge).where(
                    ViewLineageEdge.datasource_id == datasource.id
                )
            )
        ).one()
        return datasource, edge

    async def test_approved_flips_active_and_records_reviewer(
        self, session, monkeypatch
    ):
        datasource, edge = await self._seed_proposed_edge(session, monkeypatch)
        reviewer = _context(datasource, principal_id="reviewer")
        result = await decide_parsed_lineage_edge(
            edge.id,
            ParsedLineageEdgeDecisionRequest(
                edge_type="VIEW", decision="APPROVED", reason="looks right"
            ),
            context=reviewer,
            session=session,
        )
        assert result.review_status == "ACTIVE"
        assert result.reviewed_by == "reviewer"
        # Outbox event emitted.
        outbox = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == str(edge.id))
            )
        ).all()
        assert len(outbox) == 1
        assert outbox[0].event_type == "lineage.parsed_edge.approved.v1"

    async def test_rejected_flips_rejected_and_records_reason(
        self, session, monkeypatch
    ):
        datasource, edge = await self._seed_proposed_edge(session, monkeypatch)
        reviewer = _context(datasource, principal_id="reviewer")
        result = await decide_parsed_lineage_edge(
            edge.id,
            ParsedLineageEdgeDecisionRequest(
                edge_type="VIEW",
                decision="REJECTED",
                reason="not what the view actually does",
            ),
            context=reviewer,
            session=session,
        )
        assert result.review_status == "REJECTED"
        assert result.reviewed_by == "reviewer"
        assert result.review_reason == "not what the view actually does"

    async def test_maker_cannot_review_own_edge(self, session, monkeypatch):
        datasource, edge = await self._seed_proposed_edge(session, monkeypatch)
        maker = _context(datasource, principal_id="author")
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            await decide_parsed_lineage_edge(
                edge.id,
                ParsedLineageEdgeDecisionRequest(
                    edge_type="VIEW", decision="APPROVED", reason="looks right"
                ),
                context=maker,
                session=session,
            )
        assert excinfo.value.status_code == 409


class TestBulkDecide:
    async def test_partial_success_when_one_item_fails(self, session, monkeypatch):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, tables = await _seed(
            session,
            table_names=[
                "src_a",
                "src_b",
                "src_c",
                "src_d",
                "src_e",
                "view_a",
                "view_b",
                "view_c",
                "view_d",
                "view_e",
            ],
        )
        for src, view in [
            ("src_a", "view_a"),
            ("src_b", "view_b"),
            ("src_c", "view_c"),
            ("src_d", "view_d"),
            ("src_e", "view_e"),
        ]:
            await _add_view_edge(
                session,
                datasource,
                tables,
                source_table=src,
                target_table=view,
            )
        edges = (
            await session.scalars(
                select(ViewLineageEdge).where(
                    ViewLineageEdge.datasource_id == datasource.id
                )
            )
        ).all()
        assert len(edges) == 5
        # Poison one row: manually flip it to REJECTED so the bulk
        # decision on it fails with "already decided" and the other four
        # still commit.
        edges[2].review_status = "REJECTED"
        await session.flush()

        reviewer = _context(datasource, principal_id="reviewer")
        result = await bulk_decide_parsed_lineage_edges(
            ParsedLineageEdgeBulkDecisionRequest(
                items=[
                    ParsedLineageEdgeBulkDecisionItem(
                        edge_id=edge.id, edge_type="VIEW"
                    )
                    for edge in edges
                ],
                decision="APPROVED",
                reason="looks right",
            ),
            context=reviewer,
            session=session,
        )
        assert result.requested_count == 5
        assert result.succeeded_count == 4
        assert result.failed_count == 1
        failed = [r for r in result.results if r.status == "FAILED"]
        assert len(failed) == 1
        assert failed[0].edge_id == edges[2].id


_ROUTINE_INSERT = (
    "INSERT INTO public.order_totals (customer_id, total) "
    "SELECT o.customer_id, o.amount FROM public.orders o"
)


async def _seed_routine(
    session: AsyncSession, *, body: str = _ROUTINE_INSERT, dialect: str = "postgres"
):
    datasource, tables = await _seed(session, table_names=["orders", "order_totals"])
    datasource.dialect = dialect
    routine = MetadataRoutine(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=tables["orders"].schema_id,
        name="load_totals",
        routine_type="PROCEDURE",
        body_sql_redacted=body,
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return datasource, routine


async def _routine_edges(session: AsyncSession) -> list[DeepProcedureLineageEdge]:
    return list(
        (
            await session.scalars(
                select(DeepProcedureLineageEdge).order_by(
                    DeepProcedureLineageEdge.transformation_type,
                    DeepProcedureLineageEdge.source_column,
                )
            )
        ).all()
    )


class TestRoutineEdgesUnderReview:
    """The routine-aware procedure table joined ADR-0026's review on
    2026-09-11: a person's parse of a captured routine is written under the
    review mode, and its edges are decided in the same queue as the rest."""

    async def test_auto_active_lands_active_and_records_its_author(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        datasource, routine = await _seed_routine(session)

        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=_context(datasource), session=session
        )

        rows = await _routine_edges(session)
        assert [(row.review_status, row.created_by) for row in rows] == [
            ("ACTIVE", "author"),
            ("ACTIVE", "author"),
        ]

    async def test_require_review_queues_the_edge_for_someone_else(
        self, session, monkeypatch
    ):
        from fastapi import HTTPException

        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, routine = await _seed_routine(session)
        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=_context(datasource), session=session
        )

        items, total = await list_parsed_lineage_review_queue(
            session, datasource.organization_id, edge_type="ROUTINE"
        )

        assert total == 2
        assert {item.source_sql_reference["routine_id"] for item in items} == {
            str(routine.id)
        }
        decision = ParsedLineageEdgeDecisionRequest(
            edge_type="ROUTINE", decision="APPROVED", reason="matches the body"
        )
        with pytest.raises(HTTPException) as excinfo:
            await decide_parsed_lineage_edge(
                items[0].edge_id,
                decision,
                context=_context(datasource, principal_id="author"),
                session=session,
            )
        assert excinfo.value.status_code == 409
        result = await decide_parsed_lineage_edge(
            items[0].edge_id,
            decision,
            context=_context(datasource, principal_id="reviewer"),
            session=session,
        )
        assert result.review_status == "ACTIVE"

    async def test_an_unparsed_marker_is_never_queued(self, session, monkeypatch):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, routine = await _seed_routine(
            session,
            body=(
                "CREATE PROCEDURE dbo.load_totals AS BEGIN "
                + _ROUTINE_INSERT
                + "; EXEC(@dynamic_sql); END"
            ),
            dialect="tsql",
        )

        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=_context(datasource), session=session
        )

        rows = await _routine_edges(session)
        assert [(row.transformation_type, row.review_status) for row in rows] == [
            ("DIRECT", "PROPOSED"),
            ("DIRECT", "PROPOSED"),
            ("UNPARSED", "ACTIVE"),
        ]

    async def test_a_reparse_keeps_every_decision_and_writes_nothing_twice(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, routine = await _seed_routine(session)
        author = _context(datasource)
        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=author, session=session
        )
        amount, customer = await _routine_edges(session)
        reviewer = _context(datasource, principal_id="reviewer")
        for edge, verdict in ((amount, "APPROVED"), (customer, "REJECTED")):
            await decide_parsed_lineage_edge(
                edge.id,
                ParsedLineageEdgeDecisionRequest(
                    edge_type="ROUTINE", decision=verdict, reason="checked"
                ),
                context=reviewer,
                session=session,
            )

        response = await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=author, session=session
        )
        await session.flush()

        assert response.persisted_edge_count == 0
        assert [row.review_status for row in await _routine_edges(session)] == [
            "ACTIVE",
            "REJECTED",
        ]


class TestUnifiedReadFilter:
    async def test_default_read_excludes_proposed(self, session, monkeypatch):
        # Seed one ACTIVE edge and one PROPOSED edge on the same target
        # table but different source tables; the default read must show
        # only the ACTIVE one.
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        datasource, tables = await _seed(
            session, table_names=["src_active", "src_pending", "my_view"]
        )
        session.add_all(
            [
                ViewLineageEdge(
                    organization_id=datasource.organization_id,
                    datasource_id=datasource.id,
                    source_table="src_active",
                    source_column="col_a",
                    target_table="my_view",
                    target_column="col_a",
                    source_table_id=tables["src_active"].id,
                    target_table_id=tables["my_view"].id,
                    transformation_type="DIRECT",
                    confidence="FULL",
                    dialect="postgres",
                    sql_hash="h1",
                    review_status="ACTIVE",
                ),
                ViewLineageEdge(
                    organization_id=datasource.organization_id,
                    datasource_id=datasource.id,
                    source_table="src_pending",
                    source_column="col_a",
                    target_table="my_view",
                    target_column="col_a",
                    source_table_id=tables["src_pending"].id,
                    target_table_id=tables["my_view"].id,
                    transformation_type="DIRECT",
                    confidence="LOW",
                    dialect="postgres",
                    sql_hash="h2",
                    review_status="PROPOSED",
                ),
            ]
        )
        await session.flush()

        from aida.unified_lineage_api import build_unified_lineage_graph_payload

        payload_default = await build_unified_lineage_graph_payload(
            session, datasource, node_limit=50, edge_limit=100
        )
        edge_evidences = [e.evidence.get("source") for e in payload_default.edges]
        # Exactly one VIEW_DEFINITION edge in the default read.
        assert edge_evidences.count("VIEW_DEFINITION") == 1

        payload_pending = await build_unified_lineage_graph_payload(
            session,
            datasource,
            node_limit=50,
            edge_limit=100,
            include_pending_edges=True,
        )
        edge_evidences_pending = [
            e.evidence.get("source") for e in payload_pending.edges
        ]
        assert edge_evidences_pending.count("VIEW_DEFINITION") == 2


class TestGraphProjectorFiltersPending:
    """Unit test on the payload the projector will hand to Neo4j -- the
    payload builder itself decides which edges make the projection, so
    filtering there is what keeps a PROPOSED edge out of the shared
    graph. No live Neo4j connection touched."""

    async def test_projection_payload_excludes_proposed_view_edge(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        datasource, tables = await _seed(
            session, table_names=["src", "my_view"]
        )
        session.add(
            ViewLineageEdge(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                source_table="src",
                source_column="col_a",
                target_table="my_view",
                target_column="col_a",
                source_table_id=tables["src"].id,
                target_table_id=tables["my_view"].id,
                transformation_type="DIRECT",
                confidence="LOW",
                dialect="postgres",
                sql_hash="h1",
                review_status="PROPOSED",
            )
        )
        await session.flush()
        from aida.unified_lineage_api import build_unified_lineage_graph_payload

        # This is the exact call graph_projector.py makes for its Neo4j
        # rebuild -- include_pending_edges is False.
        payload = await build_unified_lineage_graph_payload(
            session,
            datasource,
            node_limit=50,
            edge_limit=100,
            suggestion_status="ALL",
            settings=None,
            include_pending_edges=False,
        )
        assert not any(
            e.evidence.get("source") == "VIEW_DEFINITION" for e in payload.edges
        )
