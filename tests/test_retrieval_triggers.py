"""R11-FP01: a SQL Server or Oracle trigger is reachable by the words of a question.

Before this, `hybrid_retrieve` had no TRIGGER candidate at all: a PostgreSQL trigger reaches
retrieval through the function `action_routine` names (a ROUTINE candidate, see
`test_retrieval_routines.py`), but SQL Server and Oracle keep the body on the trigger row itself,
and nothing fetched it. That was deliberate -- R11-D23 made `ContextProductScope.admits` refuse
any retrieval hit kind it does not recognise, so a TRIGGER candidate could not be added by itself
without being silently admitted past every product's boundary. This file drives the real
retrieval against in-memory SQLite and pins the rules that make the new candidate safe:

* it is found by its own name and its firing table's name -- never its body;
* a trigger whose body lives in a named routine (PostgreSQL) is not fetched again here, because
  the ROUTINE candidate already carries it;
* the hit is decided through a context product on its one firing table
  (`ContextProductScope._owning_table`, `tests/test_context_product_hit_types.py`), and a trigger
  whose firing table this datasource's catalog does not hold is an unresolved reference,
  `table_id: None`, refused rather than guessed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.agent_orchestrator import ContextProductScope
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataTrigger
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
from aida.retrieval import hybrid_retrieve

QUESTION = "orders audit trigger"


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
        connector_type="mssql",
        dialect="tsql",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )


#: A stored, never-executed trigger body used only as fixture text -- not a query this test
#: runs, so the shape a real query-injection rule cares about doesn't apply.
_TRIGGER_BODY_TEMPLATE = (
    "CREATE TRIGGER {name} ON {table_name} AFTER INSERT AS BEGIN INSERT INTO "
    "{table_name}_audit SELECT * FROM inserted END"
)


def _trigger(
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    table_name: str,
    action_routine: str | None = None,
    status: str = "ACTIVE",
) -> MetadataTrigger:
    return MetadataTrigger(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        table_name=table_name,
        timing="AFTER",
        events=["INSERT"],
        orientation="ROW",
        is_enabled=True,
        action_routine=action_routine,
        body_sql_redacted=(
            None
            if action_routine
            else _TRIGGER_BODY_TEMPLATE.format(name=name, table_name=table_name)
        ),
        body_fingerprint=None if action_routine else "body-fp",
        redaction_status="UNAVAILABLE" if action_routine else "LEXICAL",
        screening_status="CLEAN",
        availability="UNAVAILABLE" if action_routine else "AVAILABLE",
        unavailable_reason="ACTION_ROUTINE" if action_routine else None,
        status=status,
        fingerprint="fp",
    )


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
    session.add_all([org, lob, domain, project, datasource])
    await session.flush()

    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="sales", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()

    orders = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="orders",
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(orders)
    await session.flush()

    own_body_trigger = _trigger(datasource, schema, name="orders_audit_trg", table_name="orders")
    postgres_style_trigger = _trigger(
        datasource,
        schema,
        name="orders_audit_via_fn",
        table_name="orders",
        action_routine="sales.orders_audit_fn",
    )
    unresolved_table_trigger = _trigger(
        datasource, schema, name="orders_ghost_trg", table_name="ghost_table"
    )
    session.add_all([own_body_trigger, postgres_style_trigger, unresolved_table_trigger])
    await session.commit()
    return {
        "datasource": datasource,
        "orders": orders,
        "own_body_trigger": own_body_trigger,
        "postgres_style_trigger": postgres_style_trigger,
        "unresolved_table_trigger": unresolved_table_trigger,
    }


def _settings() -> Settings:
    return Settings(_env_file=None)


@pytest.mark.asyncio
async def test_a_trigger_with_its_own_body_is_found_by_name_and_decided_on_its_firing_table(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)

    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    triggers = [hit for hit in hits if hit.object_type == "TRIGGER"]
    ids = {hit.object_id for hit in triggers}
    assert str(seeded["own_body_trigger"].id) in ids
    (hit,) = [h for h in triggers if h.object_id == str(seeded["own_body_trigger"].id)]
    assert hit.display_name == "sales.orders.orders_audit_trg"
    assert hit.metadata["table_id"] == str(seeded["orders"].id)
    assert hit.metadata["body_available"] is True
    assert "BM25_TRIGGER_NAME" in hit.reason_codes

    scope = ContextProductScope(
        version_id=uuid4(),
        version=1,
        table_ids=frozenset({str(seeded["orders"].id)}),
        tool_version_ids=frozenset(),
        routine_ids=frozenset(),
        ontology_version_ids=frozenset(),
        glossary_term_version_ids=frozenset(),
        semantic_model_version_ids=frozenset(),
    )
    assert scope.admits(hit)
    outside_scope = ContextProductScope(
        version_id=uuid4(),
        version=1,
        table_ids=frozenset({str(uuid4())}),
        tool_version_ids=frozenset(),
        routine_ids=frozenset(),
        ontology_version_ids=frozenset(),
        glossary_term_version_ids=frozenset(),
        semantic_model_version_ids=frozenset(),
    )
    assert not outside_scope.admits(hit)


@pytest.mark.asyncio
async def test_a_trigger_whose_body_lives_in_a_named_routine_is_not_fetched_again(
    session: AsyncSession,
) -> None:
    """PostgreSQL: the trigger names an `action_routine`, so its body is a ROUTINE candidate's
    business (`test_retrieval_routines.py`), and fetching it again here would be a second,
    conflicting way for the same code to reach the model."""
    seeded = await _seed(session)

    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    triggers = {hit.object_id for hit in hits if hit.object_type == "TRIGGER"}
    assert str(seeded["postgres_style_trigger"].id) not in triggers


@pytest.mark.asyncio
async def test_a_trigger_whose_firing_table_is_not_in_the_catalog_is_an_unresolved_reference(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)

    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    (hit,) = [
        h
        for h in hits
        if h.object_type == "TRIGGER" and h.object_id == str(seeded["unresolved_table_trigger"].id)
    ]
    assert hit.metadata["table_id"] is None

    scope = ContextProductScope(
        version_id=uuid4(),
        version=1,
        table_ids=frozenset({str(seeded["orders"].id)}),
        tool_version_ids=frozenset(),
        routine_ids=frozenset(),
        ontology_version_ids=frozenset(),
        glossary_term_version_ids=frozenset(),
        semantic_model_version_ids=frozenset(),
    )
    assert not scope.admits(hit)


@pytest.mark.asyncio
async def test_a_question_that_names_no_trigger_returns_none(session: AsyncSession) -> None:
    seeded = await _seed(session)

    hits = await hybrid_retrieve(
        session,
        datasource=seeded["datasource"],
        question="customer address history",
        settings=_settings(),
    )

    assert not [hit for hit in hits if hit.object_type == "TRIGGER"]
