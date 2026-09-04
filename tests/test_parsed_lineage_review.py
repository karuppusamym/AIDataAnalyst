"""P1-05 / ADR-0026: coverage for the parsed-lineage-edge review lifecycle.

Uses the same in-memory-SQLite + real-ORM pattern as
`tests/test_view_lineage_api.py`. No mocked persistence -- the point of
these tests is to exercise the actual `_persist_edges` decision
(delete-only-PROPOSED-on-re-parse, existing-ACTIVE-idempotency), the
actual review endpoint (maker-checker + status flip + audit + outbox),
and the actual unified-lineage-read filter.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    ProcedureLineageEdge,
    Project,
    ViewLineageEdge,
)
from aida.parsed_lineage_review_api import (
    bulk_decide_parsed_lineage_edges,
    decide_parsed_lineage_edge,
    get_parsed_lineage_review_queue,
)
from aida.parsed_lineage_review_service import resolve_review_status_for_new_edge
from aida.schemas import (
    ParsedLineageEdgeBulkDecisionItem,
    ParsedLineageEdgeBulkDecisionRequest,
    ParsedLineageEdgeDecisionRequest,
    ViewLineageParseRequest,
)
from aida.security_types import SecurityContext
from aida.view_lineage_api import parse_view_lineage_endpoint
from atlas.platform.config import Settings, get_settings

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


def _request(sql: str) -> ViewLineageParseRequest:
    return ViewLineageParseRequest(sql=sql, dialect="postgres")


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


class TestAutoActiveMode:
    """Backward-compat: the default `auto_active` config MUST land every
    parsed edge as ACTIVE, so nothing about an existing deployment
    changes on the P1-05 code being present."""

    async def test_view_parse_lands_active(self, session, monkeypatch):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        # No env override for review mode -> the default "auto_active".
        datasource, _ = await _seed(session, table_names=["source_table", "my_view"])
        context = _context(datasource)
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(
                    ViewLineageEdge.datasource_id == datasource.id
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].review_status == "ACTIVE"
        # No PROPOSED rows exist -> the queue is empty.
        proposed = (
            await session.scalars(
                select(ViewLineageEdge).where(
                    ViewLineageEdge.review_status == "PROPOSED"
                )
            )
        ).all()
        assert proposed == []


class TestRequireReviewMode:
    async def test_low_confidence_parse_lands_proposed(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        # threshold at 1.01 -> even FULL (=1.0) is below it, so
        # everything the parser emits lands PROPOSED regardless of its
        # confidence value.
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, _ = await _seed(session, table_names=["source_table", "my_view"])
        context = _context(datasource)
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(
                    ViewLineageEdge.datasource_id == datasource.id
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].review_status == "PROPOSED"
        assert rows[0].created_by == "author"

    async def test_full_confidence_edge_lands_active_via_threshold(
        self, session, monkeypatch
    ):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        # Default threshold 0.9 -> a FULL (1.0) parse still lands ACTIVE
        # even under require_review; mirrors ADR-0025's spirit.
        datasource, _ = await _seed(session, table_names=["source_table", "my_view"])
        context = _context(datasource)
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(
                    ViewLineageEdge.datasource_id == datasource.id
                )
            )
        ).all()
        assert len(rows) == 1
        # A FULL-confidence view parse maps to 1.0 >= threshold -> ACTIVE.
        assert rows[0].review_status == "ACTIVE"


class TestDecideParsedLineageEdge:
    async def _seed_proposed_edge(self, session, monkeypatch):
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, _ = await _seed(session, table_names=["source_table", "my_view"])
        author_context = _context(datasource, principal_id="author")
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"),
            context=author_context,
            session=session,
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
        author_context = _context(datasource, principal_id="author")
        for src, view in [
            ("src_a", "view_a"),
            ("src_b", "view_b"),
            ("src_c", "view_c"),
            ("src_d", "view_d"),
            ("src_e", "view_e"),
        ]:
            await parse_view_lineage_endpoint(
                datasource.id,
                _request(f"CREATE VIEW {view} AS SELECT a.col_a FROM {src} a"),
                context=author_context,
                session=session,
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


class TestReparseIdempotency:
    async def test_reparse_does_not_delete_active_approved_edge(
        self, session, monkeypatch
    ):
        # Mode 1: parse under require_review, get a PROPOSED edge, then
        # a reviewer approves it -> ACTIVE with reviewed_by set.
        # Mode 2: re-parse the same view definition. The new parse
        # produces an edge with the SAME natural key. The ACTIVE edge
        # must stay untouched (idempotency); no new PROPOSED duplicate
        # is added.
        monkeypatch.setenv("AIDA_ENVIRONMENT", "test")
        monkeypatch.setenv("AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE", "require_review")
        monkeypatch.setenv(
            "AIDA_LINEAGE_HIGH_CONFIDENCE_AUTO_ACTIVE_THRESHOLD", "1.01"
        )
        datasource, _ = await _seed(session, table_names=["source_table", "my_view"])
        author = _context(datasource, principal_id="author")
        sql = "CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"
        await parse_view_lineage_endpoint(
            datasource.id, _request(sql), context=author, session=session
        )
        edge = (
            await session.scalars(select(ViewLineageEdge))
        ).one()
        # Reviewer approves.
        reviewer = _context(datasource, principal_id="reviewer")
        await decide_parsed_lineage_edge(
            edge.id,
            ParsedLineageEdgeDecisionRequest(
                edge_type="VIEW", decision="APPROVED", reason="ok"
            ),
            context=reviewer,
            session=session,
        )
        # Re-parse.
        await parse_view_lineage_endpoint(
            datasource.id, _request(sql), context=author, session=session
        )
        rows = (
            await session.scalars(select(ViewLineageEdge))
        ).all()
        # Exactly one edge, still ACTIVE, reviewer trail intact.
        assert len(rows) == 1
        assert rows[0].review_status == "ACTIVE"
        assert rows[0].reviewed_by == "reviewer"


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
