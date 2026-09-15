"""R11-FP11: a stored procedure or function is reachable by the words of a question.

Before this, `hybrid_retrieve` had seven candidate kinds and none was a routine, so "which
procedure rebuilds the revenue rollup" found nothing however well the routine was named and
however much reviewed lineage it had. These tests drive the real retrieval against in-memory
SQLite and pin the three rules that make the new candidate safe to hand an agent:

* it is found by name, parameters and description -- never ranked by its body;
* what it stands on comes only from ACTIVE lineage -- an undecided proposal steers nothing;
* the SQL model is given the tables it reads and writes, never the routine as a callable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter
from aida.mcp_server import _resolve_governed_entities
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
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.retrieval import HybridRetrievalHit, hybrid_retrieve, hybrid_retrieve_enhanced

QUESTION = "rebuild revenue rollup"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


def _datasource(
    org: Organization, lob: LineOfBusiness, domain: DataDomain, project: Project, name: str
) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )


async def _routine(
    session: AsyncSession, datasource: DataSource, schema: MetadataSchema, name: str, status: str
) -> MetadataRoutine:
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        signature="(date)",
        routine_type="PROCEDURE",
        language="plpgsql",
        body_sql_redacted="CREATE PROCEDURE p() LANGUAGE plpgsql AS $$ BEGIN NULL; END; $$",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        source_description="Recomputes net revenue per customer",
        status=status,
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    return routine


async def _seed(session: AsyncSession) -> dict[str, object]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Finance",
        code=f"F{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = _datasource(org, lob, domain, project, "primary")
    elsewhere = _datasource(org, lob, domain, project, "elsewhere")
    session.add_all([org, lob, domain, project, datasource, elsewhere])
    await session.flush()

    schemas = {}
    for source in (datasource, elsewhere):
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=source.id,
            name="bank",
            fingerprint="fp",
        )
        session.add(catalog)
        await session.flush()
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=org.id,
            catalog_id=catalog.id,
            name="sales",
            fingerprint="fp",
        )
        session.add(schema)
        await session.flush()
        schemas[source.id] = schema
    schema = schemas[datasource.id]

    tables = {}
    for name in ("orders", "cust_net_amounts", "unrelated_audit"):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint="fp",
        )
        session.add(table)
        tables[name] = table
    await session.flush()

    routine = await _routine(session, datasource, schema, "rebuild_revenue_rollup", "ACTIVE")
    await _routine(session, datasource, schema, "rebuild_revenue_rollup_v0", "DEPRECATED")
    await _routine(session, elsewhere, schemas[elsewhere.id], "rebuild_revenue_rollup", "ACTIVE")
    session.add(
        MetadataRoutineParameter(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            name="p_business_date",
            ordinal_position=1,
            physical_type="date",
            fingerprint="fp",
        )
    )

    def edge(
        target: str, *, review_status: str, intermediate: bool = False
    ) -> DeepProcedureLineageEdge:
        target_table = tables.get(target)
        return DeepProcedureLineageEdge(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            statement_ordinal=1,
            source_table="sales.orders",
            source_column="amount",
            target_table=f"sales.{target}",
            target_column="amount",
            source_resolved=True,
            source_table_id=tables["orders"].id,
            target_table_id=None if intermediate or target_table is None else target_table.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            is_write=True,
            is_intermediate=intermediate,
            sql_hash="h",
            review_status=review_status,
        )

    session.add_all(
        [
            edge("cust_net_amounts", review_status="ACTIVE"),
            # An agent's proposal nobody has decided must not steer an answer.
            edge("unrelated_audit", review_status="PROPOSED"),
            edge("totals_tmp", review_status="ACTIVE", intermediate=True),
        ]
    )
    await session.commit()
    return {"datasource": datasource, "routine": routine, "tables": tables}


def _settings() -> Settings:
    return Settings(_env_file=None)


@pytest.mark.asyncio
async def test_a_routine_is_found_by_its_words_with_only_reviewed_lineage(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    tables = seeded["tables"]

    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    routines = [hit for hit in hits if hit.object_type == "ROUTINE"]
    # The DEPRECATED copy and the other datasource's routine share the words; neither returns.
    assert [hit.object_id for hit in routines] == [str(seeded["routine"].id)]
    (hit,) = routines
    assert hit.display_name == "sales.rebuild_revenue_rollup"
    assert hit.metadata["reads_table_ids"] == [str(tables["orders"].id)]
    assert hit.metadata["writes_table_ids"] == [str(tables["cust_net_amounts"].id)]
    assert hit.metadata["body_available"] is True
    assert "GOVERNED_TOOL_BOOST" not in hit.reason_codes


@pytest.mark.asyncio
async def test_graph_expansion_reaches_the_table_a_routine_writes(session: AsyncSession) -> None:
    """No word of the question names `cust_net_amounts`; only the routine's lineage does."""
    seeded = await _seed(session)

    hits = await hybrid_retrieve_enhanced(
        session,
        datasource=seeded["datasource"],
        question=QUESTION,
        settings=_settings(),
        include_vector=False,
    )

    table_names = {hit.display_name for hit in hits if hit.object_type == "TABLE"}
    assert "cust_net_amounts" in table_names
    assert "unrelated_audit" not in table_names


@pytest.mark.asyncio
async def test_the_sql_model_gets_the_routine_tables_never_the_routine(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    tables = seeded["tables"]
    hit = HybridRetrievalHit(
        object_type="ROUTINE",
        object_id=str(seeded["routine"].id),
        display_name="sales.rebuild_revenue_rollup",
        score=0.9,
        reason_codes=["BM25_ROUTINE_NAME"],
        metadata={
            "reads_table_ids": [str(tables["orders"].id)],
            "writes_table_ids": [str(tables["cust_net_amounts"].id)],
        },
    )

    context = await GovernedAgentOrchestrator._model_context(  # type: ignore[arg-type]
        None, session, datasource=seeded["datasource"], retrieval_hits=[hit]
    )

    assert {table["qualified_name"] for table in context["tables"]} == {
        "sales.orders",
        "sales.cust_net_amounts",
    }
    assert set(context) == {"dialect", "tables", "constraints"}


@pytest.mark.asyncio
async def test_a_question_that_names_no_routine_returns_none(session: AsyncSession) -> None:
    seeded = await _seed(session)

    hits = await hybrid_retrieve(
        session,
        datasource=seeded["datasource"],
        question="customer address history",
        settings=_settings(),
    )

    assert not [hit for hit in hits if hit.object_type == "ROUTINE"]


@pytest.mark.asyncio
async def test_mcp_resolve_entity_names_a_routine_get_transformation_detail_accepts(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)

    payload = await _resolve_governed_entities(
        session, seeded["datasource"], "rebuild_revenue_rollup", "ROUTINE", 5
    )

    matches = payload["matches"]
    assert [match["entity_id"] for match in matches] == [str(seeded["routine"].id)]
    assert matches[0]["entity_type"] == "ROUTINE"
    assert matches[0]["qualified_name"] == "bank.sales.rebuild_revenue_rollup"
    tables_only = await _resolve_governed_entities(
        session, seeded["datasource"], "rebuild_revenue_rollup", "TABLE", 5
    )
    assert tables_only["matches"] == []
