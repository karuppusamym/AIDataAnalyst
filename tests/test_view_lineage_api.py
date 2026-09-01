"""Endpoint coverage for view/procedure lineage persistence (AT-D2 defects 5, 6).

No test file existed for `view_lineage_api.py` before AT-D2. PostgreSQL is
reachable in this sandbox for the migration-drift test, but this module's
persistence logic (a `DELETE ... WHERE datasource_id = ... AND target_table
IN (...)` followed by plain inserts, plus a handful of equality-joined
`SELECT`s) uses no Postgres-only construct, so it runs against a real ORM
session backed by in-memory SQLite -- the same pattern
`tests/test_catalog_pagination.py` uses -- exercising genuine query
execution (the unique constraint included) rather than a mock.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    ProcedureLineageEdge,
    Project,
    ViewLineageEdge,
)
from aida.schemas import ViewLineageParseRequest
from aida.security_types import SecurityContext
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET
from aida.view_lineage_api import (
    parse_procedure_lineage_endpoint,
    parse_view_lineage_endpoint,
)

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


async def _seed_datasource(
    session: AsyncSession, *, table_names: list[str] = ()
) -> tuple[DataSource, dict[str, MetadataTable]]:
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
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
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
    if table_names:
        await session.flush()
    return datasource, tables


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="tester",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


class TestTableIdPopulation:
    """AT-D2 defect 6: source_table_id / target_table_id were never set."""

    async def test_resolved_source_and_target_get_real_table_ids(self, session) -> None:
        datasource, tables = await _seed_datasource(
            session, table_names=["source_table", "my_view"]
        )
        context = _context(datasource)

        response = await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        assert response.persisted_edge_count == 1

        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(ViewLineageEdge.datasource_id == datasource.id)
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].source_table_id == tables["source_table"].id
        assert rows[0].target_table_id == tables["my_view"].id

    async def test_unresolved_source_reference_leaves_source_table_id_null(self, session) -> None:
        # Unqualified column, so sql_lineage_parser cannot attribute it to
        # source_table even though that table exists in the catalog -- a
        # NULL source_table_id (never a guessed FK) is the honest result.
        datasource, tables = await _seed_datasource(
            session, table_names=["source_table", "my_view"]
        )
        context = _context(datasource)

        response = await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT col_a FROM source_table"),
            context=context,
            session=session,
        )
        assert response.persisted_edge_count == 1

        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(ViewLineageEdge.datasource_id == datasource.id)
            )
        ).all()
        assert rows[0].source_table_id is None
        assert rows[0].target_table_id == tables["my_view"].id

    async def test_target_not_yet_catalogued_leaves_target_table_id_null(self, session) -> None:
        # The view being defined does not exist in the catalog yet (it has
        # not been crawled) -- target_table_id honestly stays NULL rather
        # than raising or fabricating a row.
        datasource, _tables = await _seed_datasource(session, table_names=["source_table"])
        context = _context(datasource)

        response = await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW uncatalogued_view AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        assert response.persisted_edge_count == 1
        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(ViewLineageEdge.datasource_id == datasource.id)
            )
        ).all()
        assert rows[0].target_table_id is None


class TestReparseDoesNotDoubleTheGraph:
    """AT-D2 defect 5: no unique constraint, blind insert doubled the graph."""

    async def test_identical_reparse_leaves_edge_count_unchanged(self, session) -> None:
        datasource, _tables = await _seed_datasource(
            session, table_names=["source_table", "my_view"]
        )
        context = _context(datasource)
        sql = "CREATE VIEW my_view AS SELECT a.col_a, a.col_b FROM source_table a"

        first = await parse_view_lineage_endpoint(
            datasource.id, _request(sql), context=context, session=session
        )
        await session.commit()
        second = await parse_view_lineage_endpoint(
            datasource.id, _request(sql), context=context, session=session
        )
        await session.commit()

        assert first.persisted_edge_count == 2
        assert second.persisted_edge_count == 2
        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(ViewLineageEdge.datasource_id == datasource.id)
            )
        ).all()
        assert len(rows) == 2  # not 4 -- the old blind-insert bug would double this

    async def test_reparse_after_dropping_a_column_removes_the_stale_edge(self, session) -> None:
        datasource, _tables = await _seed_datasource(
            session, table_names=["source_table", "my_view"]
        )
        context = _context(datasource)

        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a, a.col_b FROM source_table a"),
            context=context,
            session=session,
        )
        await session.commit()
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW my_view AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        await session.commit()

        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(ViewLineageEdge.datasource_id == datasource.id)
            )
        ).all()
        assert [row.source_column for row in rows] == ["col_a"]

    async def test_reparsing_one_view_does_not_touch_an_unrelated_views_edges(
        self, session
    ) -> None:
        datasource, _tables = await _seed_datasource(
            session, table_names=["source_table", "view_a", "view_b"]
        )
        context = _context(datasource)

        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW view_a AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW view_b AS SELECT a.col_b FROM source_table a"),
            context=context,
            session=session,
        )
        await session.commit()

        # Re-parse view_a only.
        await parse_view_lineage_endpoint(
            datasource.id,
            _request("CREATE VIEW view_a AS SELECT a.col_a FROM source_table a"),
            context=context,
            session=session,
        )
        await session.commit()

        rows = (
            await session.scalars(
                select(ViewLineageEdge).where(ViewLineageEdge.datasource_id == datasource.id)
            )
        ).all()
        targets = sorted(row.target_table for row in rows)
        assert targets == ["view_a", "view_b"]

    async def test_unique_constraint_is_enforced_at_the_database_level(self, session) -> None:
        # Defence in depth: even bypassing the endpoint's own delete-then-
        # insert, the database itself refuses a literal duplicate edge.
        datasource, _tables = await _seed_datasource(session)
        edge_kwargs = dict(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="source_table",
            source_column="col_a",
            target_table="my_view",
            target_column="col_a",
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="a" * 64,
        )
        session.add(ViewLineageEdge(id=uuid4(), **edge_kwargs))
        await session.flush()
        session.add(ViewLineageEdge(id=uuid4(), **edge_kwargs))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_procedure_reparse_with_a_standalone_select_does_not_double(
        self, session
    ) -> None:
        datasource, _tables = await _seed_datasource(session, table_names=["t"])
        context = _context(datasource)
        sql = "SELECT a.id FROM t a"

        first = await parse_procedure_lineage_endpoint(
            datasource.id, _request(sql), context=context, session=session
        )
        await session.commit()
        second = await parse_procedure_lineage_endpoint(
            datasource.id, _request(sql), context=context, session=session
        )
        await session.commit()

        assert first.persisted_edge_count == second.persisted_edge_count == 1
        rows = (
            await session.scalars(
                select(ProcedureLineageEdge).where(
                    ProcedureLineageEdge.datasource_id == datasource.id
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].target_table == PROCEDURE_RESULT_TARGET


def _request(sql: str, dialect: str = "postgres") -> ViewLineageParseRequest:
    return ViewLineageParseRequest(sql=sql, dialect=dialect)
