"""N3 endpoint coverage: routine-identity-aware procedure lineage parse and
persistence (`procedure_lineage_api.py`).

Mirrors `tests/test_view_lineage_api.py`'s pattern (real ORM session backed
by in-memory SQLite, no mocking of the persistence layer) but for the new,
routine-identity-aware `DeepProcedureLineageEdge` table -- proving the
eligibility gate (missing/unavailable/unparsed/quarantined routine body all
refuse outright), `source_table_id`/`target_table_id` resolution, and that a
re-parse replaces exactly this routine's own rows.
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
    Project,
)
from aida.procedure_lineage_api import (
    RoutineNotEligibleError,
    get_procedure_lineage_capability_matrix,
    list_deep_procedure_lineage,
    parse_deep_procedure_lineage_endpoint,
    require_eligible_routine_body,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.security_types import SecurityContext


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
) -> tuple[DataSource, dict[str, MetadataTable], MetadataSchema]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        name="Ungoverned", code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, name="Warehouse", slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, project_id=project.id, name="primary",
        connector_type="mssql", dialect="tsql", environment="PROD", network_zone="default",
        credential_reference="env://TEST_DSN", capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id,
        name="bank", fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="dbo", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()

    tables: dict[str, MetadataTable] = {}
    for name in table_names:
        table = MetadataTable(
            id=uuid4(), organization_id=org.id, datasource_id=datasource.id, schema_id=schema.id,
            name=name, object_type="BASE_TABLE", fingerprint="fp",
        )
        session.add(table)
        tables[name] = table
    if table_names:
        await session.flush()
    return datasource, tables, schema


def _routine(
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    body: str | None,
    availability: str = "AVAILABLE",
    redaction_status: str = "PARSED",
    screening_status: str = "CLEAN",
    status: str = "ACTIVE",
    name: str = "usp_test",
) -> MetadataRoutine:
    return MetadataRoutine(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        routine_type="PROCEDURE",
        body_sql_redacted=body,
        redaction_status=redaction_status,
        screening_status=screening_status,
        availability=availability,
        status=status,
        fingerprint="fp",
    )


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="tester", principal_type="USER",
        organization_id=datasource.organization_id, roles=frozenset({"PlatformAdmin"}),
    )


# ---------------------------------------------------------------------------
# Eligibility gate.
# ---------------------------------------------------------------------------


class TestEligibilityGate:
    def test_unavailable_body_is_refused(self) -> None:
        datasource = DataSource(
            id=uuid4(), organization_id=uuid4(), line_of_business_id=uuid4(),
            data_domain_id=uuid4(), project_id=uuid4(), name="x", connector_type="mssql",
            dialect="tsql", environment="PROD", network_zone="default",
            credential_reference="env://X", capabilities={},
        )
        schema = MetadataSchema(
            id=uuid4(), organization_id=uuid4(), catalog_id=uuid4(), name="dbo", fingerprint="fp"
        )
        routine = _routine(datasource, schema, body=None, availability="UNAVAILABLE")
        with pytest.raises(RoutineNotEligibleError, match="UNAVAILABLE"):
            require_eligible_routine_body(routine)

    def test_quarantined_body_is_refused(self) -> None:
        datasource = DataSource(
            id=uuid4(), organization_id=uuid4(), line_of_business_id=uuid4(),
            data_domain_id=uuid4(), project_id=uuid4(), name="x", connector_type="mssql",
            dialect="tsql", environment="PROD", network_zone="default",
            credential_reference="env://X", capabilities={},
        )
        schema = MetadataSchema(
            id=uuid4(), organization_id=uuid4(), catalog_id=uuid4(), name="dbo", fingerprint="fp"
        )
        routine = _routine(datasource, schema, body="SELECT 1", screening_status="QUARANTINED")
        with pytest.raises(RoutineNotEligibleError, match="quarantined"):
            require_eligible_routine_body(routine)

    def test_missing_routine_is_refused(self) -> None:
        with pytest.raises(RoutineNotEligibleError, match="no captured routine"):
            require_eligible_routine_body(None)


# ---------------------------------------------------------------------------
# Real parse + persist, through the actual endpoint.
# ---------------------------------------------------------------------------


class TestParseAndPersist:
    async def test_eligible_routine_persists_edges_with_table_ids_resolved(self, session) -> None:
        datasource, tables, schema = await _seed_datasource(
            session, table_names=["orders", "customers"]
        )
        routine = _routine(
            datasource, schema,
            body=(
                "SELECT c.customer_id, SUM(o.amount) AS total_amount "
                "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
                "GROUP BY c.customer_id"
            ),
        )
        session.add(routine)
        await session.flush()
        context = _context(datasource)

        response = await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=context, session=session
        )
        assert response.is_fully_parsed is True
        assert response.is_read_only is True
        assert response.persisted_edge_count == 2
        await session.commit()

        rows = (
            await session.scalars(
                select(DeepProcedureLineageEdge).where(
                    DeepProcedureLineageEdge.routine_id == routine.id
                )
            )
        ).all()
        assert len(rows) == 2
        by_column = {row.source_column: row for row in rows}
        assert by_column["customer_id"].source_table_id == tables["customers"].id
        assert by_column["amount"].source_table_id == tables["orders"].id
        assert all(row.is_write is False for row in rows)

    async def test_ineligible_routine_raises_before_touching_the_database(self, session) -> None:
        datasource, _tables, schema = await _seed_datasource(session)
        routine = _routine(datasource, schema, body=None, availability="UNAVAILABLE")
        session.add(routine)
        await session.flush()
        context = _context(datasource)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await parse_deep_procedure_lineage_endpoint(
                datasource.id, routine.id, context=context, session=session
            )
        assert exc_info.value.status_code == 422
        assert "UNAVAILABLE" in exc_info.value.detail

    async def test_reparse_replaces_only_this_routines_own_rows(self, session) -> None:
        datasource, _tables, schema = await _seed_datasource(session, table_names=["t", "s"])
        routine_a = _routine(
            datasource, schema, body="SELECT a.id FROM t a", name="usp_a"
        )
        routine_b = _routine(
            datasource, schema, body="SELECT b.id FROM s b", name="usp_b"
        )
        session.add_all([routine_a, routine_b])
        await session.flush()
        context = _context(datasource)

        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine_a.id, context=context, session=session
        )
        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine_b.id, context=context, session=session
        )
        await session.commit()

        # Re-parse routine_a only.
        second = await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine_a.id, context=context, session=session
        )
        await session.commit()
        assert second.persisted_edge_count == 1

        rows_a = (
            await session.scalars(
                select(DeepProcedureLineageEdge).where(
                    DeepProcedureLineageEdge.routine_id == routine_a.id
                )
            )
        ).all()
        rows_b = (
            await session.scalars(
                select(DeepProcedureLineageEdge).where(
                    DeepProcedureLineageEdge.routine_id == routine_b.id
                )
            )
        ).all()
        assert len(rows_a) == 1  # not doubled
        assert len(rows_b) == 1  # untouched by routine_a's re-parse

    async def test_unparsed_construct_persists_an_explicit_marker_row(self, session) -> None:
        datasource, _tables, schema = await _seed_datasource(session)
        routine = _routine(
            datasource, schema,
            body="CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT 1; EXEC(@dynamic_sql); END",
        )
        session.add(routine)
        await session.flush()
        context = _context(datasource)

        response = await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=context, session=session
        )
        assert response.is_fully_parsed is False
        assert any(e.transformation_type == "UNPARSED" for e in response.edges)
        await session.commit()

        rows = (
            await session.scalars(
                select(DeepProcedureLineageEdge).where(
                    DeepProcedureLineageEdge.routine_id == routine.id
                )
            )
        ).all()
        assert any(row.transformation_type == "UNPARSED" and row.unparsed_reason for row in rows)

    async def test_list_endpoint_returns_persisted_edges_in_statement_order(self, session) -> None:
        datasource, _tables, schema = await _seed_datasource(session, table_names=["t"])
        routine = _routine(
            datasource, schema,
            body=(
                "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a.id FROM t a; "
                "SELECT a.id FROM t a WHERE a.id > 1; END"
            ),
        )
        session.add(routine)
        await session.flush()
        context = _context(datasource)

        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, context=context, session=session
        )
        await session.commit()

        listed = await list_deep_procedure_lineage(
            datasource.id, routine.id, limit=200, offset=0, context=context, session=session
        )
        assert len(listed) >= 2
        assert [row.statement_ordinal for row in listed] == sorted(
            row.statement_ordinal for row in listed
        )


class TestCapabilityMatrixRoute:
    """AT-22: the matrix is also served live (not just published as a
    generated doc), so it is reachable from a real entry point
    (`aida.main`) and verifiably backed by the same source the publishing
    script uses."""

    async def test_route_returns_the_same_matrix_the_generator_produces(self) -> None:
        from aida.procedure_capability_matrix import build_capability_matrix

        expected = build_capability_matrix()
        datasource = DataSource(
            id=uuid4(), organization_id=uuid4(), line_of_business_id=uuid4(),
            data_domain_id=uuid4(), project_id=uuid4(), name="x", connector_type="mssql",
            dialect="tsql", environment="PROD", network_zone="default",
            credential_reference="env://X", capabilities={},
        )
        context = _context(datasource)

        response = await get_procedure_lineage_capability_matrix(context=context)

        assert response.dialects == list(expected.dialects)
        assert response.unparsed_reasons == list(expected.unparsed_reasons)
        assert [c.construct_name for c in response.constructs] == [
            row.construct for row in expected.constructs
        ]
        assert any(c.procedure_parser_status == "EXPLICIT_UNPARSED" for c in response.constructs)
