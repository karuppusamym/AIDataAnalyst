"""A table's tags live on its approved annotation version, not the annotation row.

AT-6 moved annotation content, tags included, onto the append-only
`MetadataBusinessAnnotationVersion`. Two stewardship readers kept reading
`annotation.tags` off the identity row: GL-6's owner routing
(`_unowned_asset_table_facts`) and tag-matched ownership rules
(`apply_ownership_rule`). Their tests used fake sessions that handed back `None`
for the annotation, so nothing noticed until the dev stack. There, the
scheduler's routing pass failed for every organization with an annotated table,
every ten seconds from the moment it started, and a TAG rule would have raised a
500. These run the real queries.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataTable,
    OwnershipRule,
    Project,
)
from aida.security import SecurityContext
from aida.stewardship_api import _unowned_asset_table_facts, apply_ownership_rule
from tests.test_document_ingestion import _seed_datasource, _seed_project, _seed_table


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def _annotated_estate(
    session: AsyncSession,
) -> tuple[Project, MetadataTable, MetadataTable]:
    """Two tables; one annotated, whose tags changed from `stale` to `pii, finance`."""
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
    for version, status, tags in (
        (1, "SUPERSEDED", ["stale"]),
        (2, "APPROVED", ["pii", "finance"]),
    ):
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
    return project, annotated, bare


def _tag_rule(project: Project, pattern: str) -> OwnershipRule:
    return OwnershipRule(
        id=uuid4(),
        organization_id=project.organization_id,
        rule_key=f"tag-{pattern}",
        display_name=f"Tagged {pattern}",
        match_field="TAG",
        match_pattern=pattern,
        owner_type="INDIVIDUAL",
        owner_principal="jane@bank.example",
        status="ACTIVE",
        created_by="steward@bank",
    )


def _steward(project: Project) -> SecurityContext:
    return SecurityContext(
        principal_id="steward@bank",
        principal_type="USER",
        organization_id=project.organization_id,
        roles=frozenset({"DataSteward"}),
    )


async def test_owner_routing_reads_the_approved_versions_tags(session) -> None:
    project, annotated, bare = await _annotated_estate(session)

    facts = await _unowned_asset_table_facts(
        session, organization_id=project.organization_id, table_ids=[annotated.id, bare.id]
    )

    assert facts[annotated.id].tags == ("pii", "finance")
    assert facts[bare.id].tags == ()


async def test_a_tag_ownership_rule_matches_only_the_approved_versions_tags(session) -> None:
    project, annotated, _bare = await _annotated_estate(session)
    current = _tag_rule(project, "pii")
    superseded = _tag_rule(project, "stale")
    session.add_all([current, superseded])
    await session.flush()

    operation = await apply_ownership_rule(current.id, _steward(project), session)

    assert operation.subject_ids == [str(annotated.id)]
    with pytest.raises(HTTPException) as refused:
        await apply_ownership_rule(superseded.id, _steward(project), session)
    assert refused.value.status_code == 409
