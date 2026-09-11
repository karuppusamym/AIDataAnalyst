"""Owner routing read `tags` off an annotation row that no longer has them.

GL-6's `_unowned_asset_table_facts` was written while annotation content still
lived on `MetadataBusinessAnnotation`. AT-6 then moved content, tags included,
onto the append-only `MetadataBusinessAnnotationVersion`, and the routing pass
kept reading `annotation.tags`. Its only tests used a fake session that handed
back `None` for the annotation, so nothing noticed. In the dev stack every
organization with one annotated table failed its routing pass, every ten
seconds, from the moment the scheduler started. This runs the real query.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import MetadataBusinessAnnotation, MetadataBusinessAnnotationVersion
from aida.stewardship_api import _unowned_asset_table_facts
from tests.test_document_ingestion import _seed_datasource, _seed_project, _seed_table


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def test_table_facts_carry_the_approved_annotation_versions_tags(session) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    annotated = await _seed_table(session, datasource, name="payments")
    bare = await _seed_table(session, datasource, name="ledger")
    annotation = MetadataBusinessAnnotation(
        id=uuid4(),
        organization_id=project.organization_id,
        datasource_id=datasource.id,
        table_id=annotated.id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=uuid4(),
    )
    session.add(annotation)
    await session.flush()
    approved_at = datetime.now(UTC)
    for version, status, tags in ((1, "SUPERSEDED", ["stale"]), (2, "APPROVED", ["pii", "finance"])):
        session.add(
            MetadataBusinessAnnotationVersion(
                id=uuid4(),
                organization_id=project.organization_id,
                annotation_id=annotation.id,
                version=version,
                status=status,
                business_name="Payments",
                business_description="One row per settled payment.",
                table_role="FACT",
                grain_statement="one row per payment",
                tags=tags,
                confidence=0.9,
                approved_by="reviewer@bank",
                approved_at=approved_at,
            )
        )
    await session.flush()

    facts = await _unowned_asset_table_facts(
        session, organization_id=project.organization_id, table_ids=[annotated.id, bare.id]
    )

    assert facts[annotated.id].tags == ("pii", "finance")
    assert facts[bare.id].tags == ()
