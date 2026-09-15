"""R11-FP09: the published ontology is read when a question is answered.

Before this nothing outside the ontology routes read an `OntologyVersion`: an approved concept
with an agreed name, aliases and mappings could not steer a single answer. These tests drive the
real retrieval against in-memory SQLite and pin the rules that make a concept safe to use:

* only each ontology's *published* version counts -- not a draft, not a deprecated concept;
* a concept stands only on mappings still valid in this datasource: ACTIVE, of the declared
  kind, and not another datasource's object;
* the approved version's id travels in the hit, and the SQL model is given the mapped tables.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4, uuid5

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataRoutine
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.retrieval import hybrid_retrieve, hybrid_retrieve_enhanced

QUESTION = "gross bookings"


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


def _settings() -> Settings:
    return Settings(_env_file=None)


def _concept(key: str, name: str, *, deprecated: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "description": "Value of orders placed, before cancellations.",
        "aliases": [],
        "deprecated": deprecated,
    }


def _mapping(concept: str, subject_type: str, subject: Any) -> dict[str, str]:
    return {"concept": concept, "subject_type": subject_type, "subject_id": str(subject.id)}


async def _seed(session: AsyncSession) -> dict[str, Any]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(id=uuid4(), organization_id=org.id, name="R", code=f"R{uuid4().hex[:6]}")
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Commerce",
        code=f"C{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    session.add_all([org, lob, domain, project])
    await session.flush()

    schemas: dict[str, MetadataSchema] = {}
    sources: dict[str, DataSource] = {}
    for name in ("primary", "elsewhere"):
        source = DataSource(
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
        session.add(source)
        await session.flush()
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=source.id,
            name="bank",
            fingerprint="f",
        )
        session.add(catalog)
        await session.flush()
        schema = MetadataSchema(
            id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="sales", fingerprint="f"
        )
        session.add(schema)
        await session.flush()
        sources[name], schemas[name] = source, schema

    def table(
        name: str, *, where: str = "primary", kind: str = "BASE_TABLE", status: str = "ACTIVE"
    ) -> MetadataTable:
        row = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=sources[where].id,
            schema_id=schemas[where].id,
            name=name,
            object_type=kind,
            status=status,
            fingerprint="f",
        )
        session.add(row)
        return row

    rows = {
        "fct": table("fct_orders_daily"),
        "view": table("stg_orders_v", kind="VIEW"),
        "legacy": table("legacy_orders", status="DEPRECATED"),
        "foreign": table("elsewhere_orders", where="elsewhere"),
    }
    await session.flush()
    rows["column"] = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=rows["fct"].id,
        name="amount",
        ordinal_position=1,
        physical_type="numeric",
        nullable=False,
        fingerprint="f",
    )
    rows["routine"] = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=sources["primary"].id,
        schema_id=schemas["primary"].id,
        name="rebuild_orders_daily",
        signature="()",
        routine_type="PROCEDURE",
        body_sql_redacted="CREATE PROCEDURE p() AS $$ BEGIN NULL; END; $$",
        redaction_status="LEXICAL",
        status="ACTIVE",
        fingerprint="f",
    )
    session.add_all([rows["column"], rows["routine"]])

    head = OntologyHead(
        id=uuid4(),
        organization_id=org.id,
        ontology_key="commerce",
        last_version=2,
        published_version=1,
    )
    session.add(head)
    await session.flush()
    published = OntologyVersion(
        id=uuid4(),
        organization_id=org.id,
        ontology_id=head.id,
        version=1,
        base_version=0,
        status="APPROVED",
        created_by="author",
        approved_by="reviewer",
        definition={
            "name": "Commerce",
            "owner": "commerce-governance",
            "provenance": "Council",
            "lifecycle": "ACTIVE",
            "concepts": [
                _concept("gross_bookings", "Gross bookings"),
                _concept("gross_bookings_old", "Gross bookings (old)", deprecated=True),
            ],
            "relations": [],
            "mappings": [
                _mapping("gross_bookings", "TABLE", rows["fct"]),
                _mapping("gross_bookings", "COLUMN", rows["column"]),
                _mapping("gross_bookings", "ROUTINE", rows["routine"]),
                # Each of these is invalid here and must stand on nothing:
                _mapping("gross_bookings", "TABLE", rows["view"]),  # a view mapped as TABLE
                _mapping("gross_bookings", "TABLE", rows["legacy"]),  # deprecated
                _mapping("gross_bookings", "TABLE", rows["foreign"]),  # another datasource
                _mapping("gross_bookings_old", "TABLE", rows["legacy"]),
            ],
        },
    )
    draft = OntologyVersion(
        id=uuid4(),
        organization_id=org.id,
        ontology_id=head.id,
        version=2,
        base_version=1,
        status="DRAFT",
        created_by="author",
        definition={
            "name": "Commerce",
            "owner": "commerce-governance",
            "provenance": "Council",
            "concepts": [_concept("gross_bookings_draft", "Gross bookings draft")],
            "mappings": [_mapping("gross_bookings_draft", "TABLE", rows["view"])],
        },
    )
    session.add_all([published, draft])
    await session.commit()
    return {"datasource": sources["primary"], "published": published, **rows}


@pytest.mark.asyncio
async def test_a_published_concept_is_found_and_stands_only_on_valid_mappings_here(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)

    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    concepts = [hit for hit in hits if hit.object_type == "ONTOLOGY_CONCEPT"]
    # Not the deprecated concept, not the draft version's concept.
    assert len(concepts) == 1
    (concept,) = concepts
    published = seeded["published"]
    assert concept.object_id == str(uuid5(published.id, "gross_bookings"))
    assert concept.display_name == "Gross bookings"
    assert concept.reason_codes == ["BM25_ONTOLOGY_CONCEPT", "ONTOLOGY_VERSION_APPROVED"]
    assert concept.metadata["ontology_version_id"] == str(published.id)
    assert concept.metadata["mapped_table_ids"] == [str(seeded["fct"].id)]
    assert concept.metadata["mapped_column_ids"] == [str(seeded["column"].id)]
    assert concept.metadata["mapped_routine_ids"] == [str(seeded["routine"].id)]
    assert not [hit for hit in hits if hit.object_type == "TABLE"], "no table name matches"


@pytest.mark.asyncio
async def test_graph_expansion_reaches_the_table_the_concept_is_mapped_to(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)

    hits = await hybrid_retrieve_enhanced(
        session,
        datasource=seeded["datasource"],
        question=QUESTION,
        settings=_settings(),
        include_vector=False,
    )

    table_names = {hit.display_name for hit in hits if hit.object_type == "TABLE"}
    assert "fct_orders_daily" in table_names
    assert table_names.isdisjoint({"stg_orders_v", "legacy_orders", "elsewhere_orders"})


@pytest.mark.asyncio
async def test_the_sql_model_gets_the_mapped_tables(session: AsyncSession) -> None:
    seeded = await _seed(session)
    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    context = await GovernedAgentOrchestrator._model_context(  # type: ignore[arg-type]
        None, session, datasource=seeded["datasource"], retrieval_hits=hits
    )

    assert {table["qualified_name"] for table in context["tables"]} == {"sales.fct_orders_daily"}


@pytest.mark.asyncio
async def test_moving_the_published_pointer_changes_the_meaning_used(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    published = seeded["published"]
    head = await session.get(OntologyHead, published.ontology_id)
    assert head is not None
    head.published_version = 2
    await session.commit()

    hits = await hybrid_retrieve(
        session, datasource=seeded["datasource"], question=QUESTION, settings=_settings()
    )

    # Version 2 is not APPROVED, so nothing is published at the pointer: no concept at all.
    assert not [hit for hit in hits if hit.object_type == "ONTOLOGY_CONCEPT"]
