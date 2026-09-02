"""RT-5 (API half) / RT-9: `GET /v1/organizations/{organization_id}/global-search`.

`aida.retrieval.hybrid_retrieve_cross_source` (see
`tests/test_retrieval_cross_source_and_graph.py` for its own behavioral
coverage) is exercised there directly; this file proves its API surface in
`semantic_api.py` -- route registration, organization policy filtering
before ranking, and the response shape a caller actually receives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.main import app
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
from aida.semantic_api import global_semantic_search
from tests.support.doubles import security_context


def test_global_search_route_is_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/global-search" in paths


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed(db: AsyncSession) -> tuple[Organization, DataSource, DataSource, MetadataTable]:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(organization)
    await db.flush()
    lob = LineOfBusiness(organization_id=organization.id, name="Retail", code="RETAIL")
    db.add(lob)
    await db.flush()
    domain = DataDomain(
        organization_id=organization.id, line_of_business_id=lob.id,
        name="Commerce", code="COMMERCE",
    )
    db.add(domain)
    await db.flush()
    project = Project(
        organization_id=organization.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, name="Core Commerce", slug=f"core-{uuid4().hex[:8]}",
    )
    db.add(project)
    await db.flush()

    ds_a = DataSource(
        organization_id=organization.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, project_id=project.id, name="warehouse-a",
        connector_type="POSTGRES", dialect="postgres", environment="PRODUCTION",
        credential_reference="vault://warehouse-a",
    )
    ds_b = DataSource(
        organization_id=organization.id, line_of_business_id=lob.id,
        data_domain_id=domain.id, project_id=project.id, name="warehouse-b",
        connector_type="POSTGRES", dialect="postgres", environment="PRODUCTION",
        credential_reference="vault://warehouse-b",
    )
    db.add_all([ds_a, ds_b])
    await db.flush()

    catalog = MetadataCatalog(
        organization_id=organization.id, datasource_id=ds_a.id,
        name="catalog-a", fingerprint="fp-catalog-a",
    )
    db.add(catalog)
    await db.flush()
    schema = MetadataSchema(
        organization_id=organization.id, catalog_id=catalog.id,
        name="public", fingerprint="fp-schema-a",
    )
    db.add(schema)
    await db.flush()
    table = MetadataTable(
        organization_id=organization.id, datasource_id=ds_a.id, schema_id=schema.id,
        name="fact_orders", object_type="TABLE", status="ACTIVE",
        fingerprint="fp-fact-orders", source_description="Order fact table",
    )
    db.add(table)
    await db.commit()
    return organization, ds_a, ds_b, table


async def test_global_search_returns_policy_filtered_hits_with_inspectable_evidence(
    db: AsyncSession,
) -> None:
    organization, ds_a, ds_b, table = await _seed(db)

    response = await global_semantic_search(
        organization.id,
        q="orders",
        project_id=None,
        datasource_ids=None,
        limit=25,
        fusion_method="rrf",
        include_vector=False,
        include_graph=False,
        context=security_context(organization_id=organization.id, roles=frozenset({"Analyst"})),
        session=db,
        settings=Settings(_env_file=None),
    )

    assert response.datasource_count == 2  # both org datasources considered, even ds_b (no rows)
    assert response.total == 1
    hit = response.items[0]
    assert hit.object_id == str(table.id)
    assert hit.datasource_id == ds_a.id
    assert hit.evidence["factors"]
    assert hit.evidence["fusion_method"] == "rrf"


async def test_global_search_denies_cross_organization_access(db: AsyncSession) -> None:
    organization, _ds_a, _ds_b, _table = await _seed(db)
    other_org_id = uuid4()

    with pytest.raises(HTTPException) as exc_info:
        await global_semantic_search(
            organization.id,
            q="orders",
            project_id=None,
            datasource_ids=None,
            limit=25,
            fusion_method="rrf",
            include_vector=False,
            include_graph=False,
            context=security_context(organization_id=other_org_id, roles=frozenset({"Analyst"})),
            session=db,
            settings=Settings(_env_file=None),
        )

    assert exc_info.value.status_code == 403


async def test_global_search_narrows_to_an_explicit_datasource_id(db: AsyncSession) -> None:
    organization, ds_a, ds_b, table = await _seed(db)

    response = await global_semantic_search(
        organization.id,
        q="orders",
        project_id=None,
        datasource_ids=[ds_b.id],
        limit=25,
        fusion_method="rrf",
        include_vector=False,
        include_graph=False,
        context=security_context(organization_id=organization.id, roles=frozenset({"Analyst"})),
        session=db,
        settings=Settings(_env_file=None),
    )

    # ds_b has no tables at all -- narrowing to it excludes ds_a's real hit.
    assert response.datasource_count == 1
    assert response.total == 0
