"""RT-2 graph-edge extension + RT-9 genuine cross-source retrieval.

`Docs/60-delivery/03-tracker.md`'s RT-2 row named two follow-ups as explicitly
not attempted by the original landing: dbt `depends_on` edges and governed-tool
`referenced_tables` edges (only `MetadataConstraint` foreign keys were loaded).
Its RT-9 row separately noted that `hybrid_retrieve`/`hybrid_retrieve_enhanced`
each take a single `DataSource` and never search across sources -- the only
genuinely cross-source surface was `search_api.py`'s lexical-only
`/v1/search`. This file proves both gaps are closed in `aida.retrieval`.

Real in-memory SQLite DB (same pattern as
`test_agent_orchestrator_retrieval_wiring.py`), not a hand-mocked session:
the new code issues real joined queries (`DbtLineageEdge` joined to
`DbtResource` twice, `resolve_table_ids`) that a call-order mock would make
brittle to verify honestly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    DbtArtifactImport,
    DbtLineageEdge,
    DbtProject,
    DbtResource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.retrieval import hybrid_retrieve_cross_source, hybrid_retrieve_enhanced


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _base_org(db: AsyncSession) -> tuple[Organization, LineOfBusiness, DataDomain, Project]:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(organization)
    await db.flush()
    lob = LineOfBusiness(organization_id=organization.id, name="Retail", code="RETAIL")
    db.add(lob)
    await db.flush()
    domain = DataDomain(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Commerce",
        code="COMMERCE",
    )
    db.add(domain)
    await db.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core Commerce",
        slug=f"core-commerce-{uuid4().hex[:8]}",
    )
    db.add(project)
    await db.flush()
    return organization, lob, domain, project


async def _datasource(
    db: AsyncSession,
    *,
    organization: Organization,
    lob: LineOfBusiness,
    domain: DataDomain,
    project: Project,
    name: str,
) -> DataSource:
    ds = DataSource(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=name,
        connector_type="POSTGRES",
        dialect="postgres",
        environment="PRODUCTION",
        credential_reference=f"vault://{name}",
    )
    db.add(ds)
    await db.flush()
    return ds


async def _schema(
    db: AsyncSession, *, organization: Organization, datasource: DataSource, name: str = "public"
) -> MetadataSchema:
    catalog = MetadataCatalog(
        organization_id=organization.id,
        datasource_id=datasource.id,
        name=f"catalog-{uuid4().hex[:6]}",
        fingerprint=f"fp-catalog-{uuid4().hex[:6]}",
    )
    db.add(catalog)
    await db.flush()
    schema = MetadataSchema(
        organization_id=organization.id,
        catalog_id=catalog.id,
        name=name,
        fingerprint=f"fp-schema-{uuid4().hex[:6]}",
    )
    db.add(schema)
    await db.flush()
    return schema


async def _table(
    db: AsyncSession,
    *,
    organization: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    name: str,
    description: str = "",
) -> MetadataTable:
    table = MetadataTable(
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="TABLE",
        status="ACTIVE",
        fingerprint=f"fp-{name}-{uuid4().hex[:6]}",
        source_description=description,
    )
    db.add(table)
    await db.flush()
    return table


# ---------------------------------------------------------------------------
# RT-2: dbt `depends_on` graph edges
# ---------------------------------------------------------------------------


async def test_hybrid_retrieve_enhanced_expands_via_dbt_depends_on_edge(
    db: AsyncSession,
) -> None:
    organization, lob, domain, project = await _base_org(db)
    datasource = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse",
    )
    schema = await _schema(db, organization=organization, datasource=datasource)

    # `fact_orders` matches the question lexically ("orders"). `stg_snapshot`
    # shares no token with the question or with `fact_orders`'s own text, and
    # there is no `MetadataConstraint` FK between them -- the *only* path to
    # it is the dbt `DEPENDS_ON` edge below.
    fact_orders = await _table(
        db, organization=organization, datasource=datasource, schema=schema,
        name="fact_orders", description="Order fact table",
    )
    stg_snapshot = await _table(
        db, organization=organization, datasource=datasource, schema=schema,
        name="stg_zzz_snapshot", description="",
    )

    dbt_project = DbtProject(
        organization_id=organization.id,
        project_id=project.id,
        datasource_id=datasource.id,
        project_key="core",
        display_name="Core",
        target_name="prod",
        status="ACTIVE",
        created_by="dbt-bot",
    )
    db.add(dbt_project)
    await db.flush()

    artifact = DbtArtifactImport(
        organization_id=organization.id,
        dbt_project_id=dbt_project.id,
        manifest_fingerprint="fp-manifest-1",
        dbt_schema_version="v10",
        status="IMPORTED",
        resource_count=2,
        model_count=2,
        source_count=0,
        test_count=0,
        lineage_edge_count=1,
        matched_resource_count=2,
        unmatched_resource_count=0,
        imported_by="dbt-bot",
    )
    db.add(artifact)
    await db.flush()

    source_resource = DbtResource(
        organization_id=organization.id,
        artifact_import_id=artifact.id,
        unique_id="model.core.fact_orders",
        resource_type="model",
        package_name="core",
        name="fact_orders",
        sql_parse_status="PARSED",
        matched_table_id=fact_orders.id,
    )
    target_resource = DbtResource(
        organization_id=organization.id,
        artifact_import_id=artifact.id,
        unique_id="model.core.stg_zzz_snapshot",
        resource_type="model",
        package_name="core",
        name="stg_zzz_snapshot",
        sql_parse_status="PARSED",
        matched_table_id=stg_snapshot.id,
    )
    db.add_all([source_resource, target_resource])
    await db.flush()

    edge = DbtLineageEdge(
        organization_id=organization.id,
        artifact_import_id=artifact.id,
        source_resource_id=source_resource.id,
        target_resource_id=target_resource.id,
        edge_type="DEPENDS_ON",
    )
    db.add(edge)
    await db.commit()

    hits = await hybrid_retrieve_enhanced(
        db,
        datasource=datasource,
        question="orders",
        settings=Settings(_env_file=None),
        include_vector=False,
        include_graph=True,
    )

    by_id = {hit.object_id: hit for hit in hits}
    assert str(fact_orders.id) in by_id
    assert str(stg_snapshot.id) in by_id, "dbt DEPENDS_ON edge did not expand to the target table"

    snapshot_hit = by_id[str(stg_snapshot.id)]
    assert "graph" in snapshot_hit.reason_codes
    # Reached only through expansion from the fact_orders seed -- proves the
    # dbt edge, not lexical/vector overlap, produced this hit.
    assert f"TABLE:{fact_orders.id}" in snapshot_hit.metadata["graph_expansion_path"]
    assert snapshot_hit.metadata["retrieval_evidence"]["factors"]


async def test_hybrid_retrieve_enhanced_ignores_dbt_edges_with_no_matched_table(
    db: AsyncSession,
) -> None:
    """An edge where one side has no `matched_table_id` contributes no graph
    edge -- it is an unmatched dbt node, not yet a table-level relationship.
    """
    organization, lob, domain, project = await _base_org(db)
    datasource = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse",
    )
    schema = await _schema(db, organization=organization, datasource=datasource)
    fact_orders = await _table(
        db, organization=organization, datasource=datasource, schema=schema,
        name="fact_orders", description="Order fact table",
    )

    dbt_project = DbtProject(
        organization_id=organization.id,
        project_id=project.id,
        datasource_id=datasource.id,
        project_key="core",
        display_name="Core",
        target_name="prod",
        status="ACTIVE",
        created_by="dbt-bot",
    )
    db.add(dbt_project)
    await db.flush()
    artifact = DbtArtifactImport(
        organization_id=organization.id,
        dbt_project_id=dbt_project.id,
        manifest_fingerprint="fp-manifest-2",
        dbt_schema_version="v10",
        status="IMPORTED",
        resource_count=2,
        model_count=2,
        source_count=0,
        test_count=0,
        lineage_edge_count=1,
        matched_resource_count=1,
        unmatched_resource_count=1,
        imported_by="dbt-bot",
    )
    db.add(artifact)
    await db.flush()
    source_resource = DbtResource(
        organization_id=organization.id,
        artifact_import_id=artifact.id,
        unique_id="model.core.fact_orders",
        resource_type="model",
        package_name="core",
        name="fact_orders",
        sql_parse_status="PARSED",
        matched_table_id=fact_orders.id,
    )
    unmatched_resource = DbtResource(
        organization_id=organization.id,
        artifact_import_id=artifact.id,
        unique_id="model.core.unmatched_model",
        resource_type="model",
        package_name="core",
        name="unmatched_model",
        sql_parse_status="PARSED",
        matched_table_id=None,
    )
    db.add_all([source_resource, unmatched_resource])
    await db.flush()
    db.add(DbtLineageEdge(
        organization_id=organization.id,
        artifact_import_id=artifact.id,
        source_resource_id=source_resource.id,
        target_resource_id=unmatched_resource.id,
        edge_type="DEPENDS_ON",
    ))
    await db.commit()

    hits = await hybrid_retrieve_enhanced(
        db,
        datasource=datasource,
        question="orders",
        settings=Settings(_env_file=None),
        include_vector=False,
        include_graph=True,
    )

    # Only the seed table itself -- no phantom TABLE node/edge was fabricated
    # for the unmatched dbt resource. (`fact_orders`'s own `DbtResource` name
    # also matches "orders" lexically and legitimately surfaces as its own
    # separate `DBT_RESOURCE` hit via `hybrid_retrieve`'s stage 5 -- unrelated
    # to the graph stage this test targets.)
    table_hits = [hit for hit in hits if hit.object_type == "TABLE"]
    assert [hit.object_id for hit in table_hits] == [str(fact_orders.id)]


# ---------------------------------------------------------------------------
# RT-2: governed-tool `referenced_tables` graph edges
# ---------------------------------------------------------------------------


async def test_hybrid_retrieve_enhanced_expands_via_governed_tool_referenced_table(
    db: AsyncSession,
) -> None:
    organization, lob, domain, project = await _base_org(db)
    datasource = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse",
    )
    schema = await _schema(db, organization=organization, datasource=datasource)

    # `zzz_unrelated_dim` shares no token with the query and has no FK/dbt
    # edge to anything -- the only path to it is the governed tool's own
    # declared `referenced_tables`.
    target_table = await _table(
        db, organization=organization, datasource=datasource, schema=schema,
        name="zzz_unrelated_dim", description="",
    )

    tool = GovernedTool(
        organization_id=organization.id, project_id=project.id, slug="customer-lookup"
    )
    db.add(tool)
    await db.flush()
    version = GovernedToolVersion(
        organization_id=organization.id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Customer Lookup",
        description="Look up customer records",
        datasource_id=datasource.id,
        sql_template="SELECT 1",
        referenced_tables=["zzz_unrelated_dim"],
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint="fp-tool-1",
        created_by="tool-dev",
    )
    db.add(version)
    await db.commit()

    hits = await hybrid_retrieve_enhanced(
        db,
        datasource=datasource,
        question="customer lookup",
        settings=Settings(_env_file=None),
        include_vector=False,
        include_graph=True,
    )

    by_id = {hit.object_id: hit for hit in hits}
    assert str(target_table.id) in by_id, "tool referenced_tables edge did not expand to the table"
    target_hit = by_id[str(target_table.id)]
    assert "graph" in target_hit.reason_codes
    assert f"GOVERNED_TOOL:{version.id}" in target_hit.metadata["graph_expansion_path"]


# ---------------------------------------------------------------------------
# RT-9: genuine cross-source retrieval
# ---------------------------------------------------------------------------


async def test_hybrid_retrieve_cross_source_returns_empty_for_no_datasources(
    db: AsyncSession,
) -> None:
    hits = await hybrid_retrieve_cross_source(
        db,
        organization_id=uuid4(),
        datasources=[],
        question="orders",
        settings=Settings(_env_file=None),
    )
    assert hits == []


async def test_hybrid_retrieve_cross_source_merges_hits_from_every_datasource(
    db: AsyncSession,
) -> None:
    organization, lob, domain, project = await _base_org(db)
    ds_a = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse-a",
    )
    ds_b = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse-b",
    )
    schema_a = await _schema(db, organization=organization, datasource=ds_a)
    schema_b = await _schema(db, organization=organization, datasource=ds_b)

    table_a = await _table(
        db, organization=organization, datasource=ds_a, schema=schema_a,
        name="fact_orders_alpha", description="Order fact table, source alpha",
    )
    table_b = await _table(
        db, organization=organization, datasource=ds_b, schema=schema_b,
        name="fact_orders_beta", description="Order fact table, source beta",
    )
    await db.commit()

    hits = await hybrid_retrieve_cross_source(
        db,
        organization_id=organization.id,
        datasources=[ds_a, ds_b],
        question="orders",
        settings=Settings(_env_file=None),
        include_vector=False,
        include_graph=False,
    )

    by_id = {hit.object_id: hit for hit in hits}
    assert str(table_a.id) in by_id, "cross-source retrieval missed datasource A's own hit"
    assert str(table_b.id) in by_id, "cross-source retrieval missed datasource B's own hit"
    assert by_id[str(table_a.id)].metadata["datasource_id"] == str(ds_a.id)
    assert by_id[str(table_b.id)].metadata["datasource_id"] == str(ds_b.id)

    # RT-3: every merged hit still carries its full per-signal ranking
    # evidence, not a stripped-down cross-source summary.
    for hit in hits:
        evidence = hit.metadata["retrieval_evidence"]
        assert evidence["factors"]
        assert evidence["fusion_method"] == "rrf"

    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


async def test_hybrid_retrieve_cross_source_caps_the_merged_result_at_the_limit(
    db: AsyncSession,
) -> None:
    organization, lob, domain, project = await _base_org(db)
    ds_a = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse-a",
    )
    ds_b = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse-b",
    )
    schema_a = await _schema(db, organization=organization, datasource=ds_a)
    schema_b = await _schema(db, organization=organization, datasource=ds_b)
    await _table(
        db, organization=organization, datasource=ds_a, schema=schema_a,
        name="fact_orders_alpha", description="Order fact table, source alpha",
    )
    await _table(
        db, organization=organization, datasource=ds_b, schema=schema_b,
        name="fact_orders_beta", description="Order fact table, source beta",
    )
    await db.commit()

    hits = await hybrid_retrieve_cross_source(
        db,
        organization_id=organization.id,
        datasources=[ds_a, ds_b],
        question="orders",
        settings=Settings(agent_retrieval_limit=1, _env_file=None),
        include_vector=False,
        include_graph=False,
    )

    # Both sources independently found a match; the merged, re-sorted result
    # is still capped at the configured limit -- not "limit per source".
    assert len(hits) == 1


async def test_hybrid_retrieve_cross_source_is_sequential_and_never_shares_a_stale_result(
    db: AsyncSession,
) -> None:
    """A datasource with no matching rows contributes nothing, and does not
    poison or short-circuit retrieval for the other datasources in the call.
    """
    organization, lob, domain, project = await _base_org(db)
    ds_a = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse-a",
    )
    ds_b = await _datasource(
        db, organization=organization, lob=lob, domain=domain, project=project,
        name="core-warehouse-b",
    )
    schema_b = await _schema(db, organization=organization, datasource=ds_b)
    table_b = await _table(
        db, organization=organization, datasource=ds_b, schema=schema_b,
        name="fact_orders_beta", description="Order fact table, source beta",
    )
    await db.commit()

    hits = await hybrid_retrieve_cross_source(
        db,
        organization_id=organization.id,
        datasources=[ds_a, ds_b],
        question="orders",
        settings=Settings(_env_file=None),
        include_vector=False,
        include_graph=False,
    )

    assert [hit.object_id for hit in hits] == [str(table_b.id)]
