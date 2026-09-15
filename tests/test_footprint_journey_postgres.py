"""R11-FP16 milestone: the database footprint journey against a real PostgreSQL source.

Discover the source; investigate it (the lineage agent reads the view, a reviewer decides each
edge); review its meaning (a description drafted from evidence, approved by someone else); publish
context (a governed tool over the view, and a context product pinning it); answer a question
through the query gateway against the source. Then change the source's logic, read it again, and
watch the platform hold what the change can have broken, rebuild each affected artifact into its
review queue, release the hold once reviewers approve the rebuilt context, and answer correctly
again.

The source is a private database created on the configured PostgreSQL server from the footprint
sample pack (`tests/fixtures/database_footprint/postgres`) and dropped afterwards. The platform runs
on in-memory SQLite. Skipped when no PostgreSQL server is reachable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_api import (
    generate_asset_description_drafts,
    submit_asset_description_draft,
)
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import SOURCE_CHANGE_ANOMALY_TYPE, process_change_signals
from aida.config import Settings, get_settings
from aida.connectors.postgres import PostgresConnector
from aida.context_product_api import create_context_product, submit_context_product_version
from aida.context_rebuild import CONTEXT_REBUILD_PRINCIPAL, run_context_rebuild
from aida.envelope_models import MetadataViewDefinition
from aida.ingestion import persist_envelope_extensions
from aida.lineage_agent import run_lineage_agent
from aida.models import (
    AnalysisRun,
    AssetDescriptionDraft,
    ContextProduct,
    ContextProductVersion,
    DataQualityIncident,
    DataSource,
    GovernanceReview,
    GovernedToolVersion,
    MetadataTable,
    Organization,
    Project,
    ViewLineageEdge,
)
from aida.parsed_lineage_review_api import decide_parsed_lineage_edge
from aida.schemas import (
    AssetDescriptionDraftGenerate,
    ContextProductCreate,
    GovernanceDecisionRequest,
    ParsedLineageEdgeDecisionRequest,
    ToolExecutionRequest,
)
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.task_agent import TaskAgentRunRequest
from aida.tool_api import (
    ViewToolBlueprintRequest,
    create_view_tool_blueprint,
    execute_tool,
    submit_tool_for_review,
)
from aida.workflows.activities import persist_discovery_snapshot
from tests.support.task_agents import (
    agent_settings,
    human,
    register_agent,
    seed_estate,
    task_agent_session,
)

SCHEMA = "footprint_context_sample"
FIXTURES = Path(__file__).parent / "fixtures" / "database_footprint" / "postgres"
#: The source's new logic: revenue no longer nets off discounts.
REDEFINED_VIEW = (
    "CREATE OR REPLACE VIEW footprint_context_sample.customer_revenue AS "
    "SELECT c.customer_id, c.region, SUM(o.amount) AS net_revenue "
    "FROM footprint_context_sample.customers c "
    "JOIN footprint_context_sample.orders o ON o.customer_id = c.customer_id "
    "GROUP BY c.customer_id, c.region"
)


async def _execute(url: URL, sql: str) -> None:
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def source() -> AsyncIterator[URL]:
    """A private PostgreSQL database holding the footprint sample, dropped afterwards."""
    server = make_url(get_settings().database_url)
    maintenance = server.set(database="postgres")
    name = f"aida_footprint_journey_{os.getpid()}"
    try:
        await _execute(maintenance, f"DROP DATABASE IF EXISTS {name}")
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable PostgreSQL server for the journey: {type(exc).__name__}")
    await _execute(maintenance, f"CREATE DATABASE {name}")
    database = server.set(database=name)
    try:
        for fixture in ("setup.sql", "view.sql"):
            await _execute(database, (FIXTURES / fixture).read_text(encoding="utf-8"))
        yield database
    finally:
        await _execute(maintenance, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def _scan(session: AsyncSession, datasource: DataSource, source: URL) -> AnalysisRun:
    """One full discovery of the sample schema, persisted through the real ingestion halves."""
    catalogs = await PostgresConnector(_dsn(source)).discover()
    selected = tuple(
        replace(catalog, schemas=tuple(s for s in catalog.schemas if s.name == SCHEMA))
        for catalog in catalogs
    )
    run = AnalysisRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    await persist_discovery_snapshot(
        session, run, datasource, selected, deprecate_missing=True, connector_capabilities={}
    )
    await persist_envelope_extensions(
        session, datasource, selected, deprecate_missing=True, analysis_run_id=run.id
    )
    run.status = "COMPLETED"
    await session.commit()
    return run


async def _table(session: AsyncSession, datasource: DataSource, name: str) -> MetadataTable:
    table = await session.scalar(
        select(MetadataTable).where(
            MetadataTable.datasource_id == datasource.id, MetadataTable.name == name
        )
    )
    assert table is not None, name
    return table


async def _investigate(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    reviewer: SecurityContext,
    settings: Settings,
) -> int:
    """The lineage agent reads the view; a reviewer decides every edge it proposed."""
    await run_lineage_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(capabilities=("VIEW_LINEAGE",), datasource_id=datasource.id),
        settings=settings,
        triggered_by=human(org),
    )
    await session.commit()
    proposed = list(
        await session.scalars(
            select(ViewLineageEdge.id).where(
                ViewLineageEdge.datasource_id == datasource.id,
                ViewLineageEdge.review_status == "PROPOSED",
            )
        )
    )
    for edge_id in proposed:
        await decide_parsed_lineage_edge(
            edge_id,
            ParsedLineageEdgeDecisionRequest(
                edge_type="VIEW", decision="APPROVED", reason="Matches the view definition."
            ),
            context=reviewer,
            session=session,
        )
    return len(proposed)


async def _approve(session: AsyncSession, review_id: UUID, reviewer: SecurityContext) -> None:
    await decide_governance_review(
        review_id, GovernanceDecisionRequest(decision="APPROVE"), context=reviewer, session=session
    )


async def _approve_rebuilt(
    session: AsyncSession, reviewer: SecurityContext, object_type: str
) -> int:
    """A reviewer approves every review the rebuild pass opened for one kind of artifact."""
    reviews = list(
        await session.scalars(
            select(GovernanceReview.id).where(
                GovernanceReview.requested_by == CONTEXT_REBUILD_PRINCIPAL,
                GovernanceReview.object_type == object_type,
                GovernanceReview.status == "PENDING",
            )
        )
    )
    for review_id in reviews:
        await _approve(session, review_id, reviewer)
    return len(reviews)


async def _revenue(
    session: AsyncSession,
    tool_version_id: UUID,
    analyst: SecurityContext,
    settings: Settings,
    customer_id: int = 1,
) -> float:
    """Ask the governed tool for one customer's revenue; the gateway runs it on the source."""
    response = await execute_tool(
        tool_version_id,
        ToolExecutionRequest(parameters={"customer_id": customer_id}),
        context=analyst,
        session=session,
        settings=settings,
    )
    rows = response.execution.rows
    assert len(rows) == 1, rows
    return float(rows[0]["net_revenue"])


async def _hold(session: AsyncSession, view: MetadataTable) -> DataQualityIncident | None:
    return await session.scalar(
        select(DataQualityIncident)
        .where(
            DataQualityIncident.table_id == view.id,
            DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
        )
        .execution_options(populate_existing=True)
    )


async def test_the_footprint_journey_on_postgres(
    source: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DSN", _dsn(source))
    settings = agent_settings()
    async with task_agent_session() as session:
        org, datasource, _ = await seed_estate(session)
        project = await session.get(Project, datasource.project_id)
        assert project is not None
        steward = human(org, "steward-1", frozenset({"DataSteward", "MetadataAdmin"}))
        reviewer = human(
            org, "reviewer-1", frozenset({"PlatformAdmin", "DataSteward", "MetadataReviewer"})
        )
        tool_developer = human(org, "tool-developer-1", frozenset({"ToolDeveloper"}))
        analyst = human(org, "analyst-1", frozenset({"Analyst"}))
        await register_agent(session, org, principal="agent:lineage")

        # 1. Discover.
        await _scan(session, datasource, source)
        view = await _table(session, datasource, "customer_revenue")
        customers = await _table(session, datasource, "customers")
        orders = await _table(session, datasource, "orders")
        assert view.object_type == "VIEW"
        definition = await session.scalar(
            select(MetadataViewDefinition).where(MetadataViewDefinition.table_id == view.id)
        )
        assert definition is not None and definition.availability == "AVAILABLE"
        original_fingerprint = definition.definition_fingerprint

        # 2. Investigate.
        assert await _investigate(session, org, datasource, reviewer, settings) > 0
        lineage = set(
            await session.scalars(
                select(ViewLineageEdge.source_column).where(
                    ViewLineageEdge.target_table_id == view.id,
                    ViewLineageEdge.review_status == "ACTIVE",
                )
            )
        )
        assert {"amount", "discount"} <= lineage

        # 3. Review meaning.
        await generate_asset_description_drafts(
            org.id,
            AssetDescriptionDraftGenerate(table_ids=[view.id]),
            context=steward,
            session=session,
        )
        draft = await session.scalar(
            select(AssetDescriptionDraft).where(AssetDescriptionDraft.table_id == view.id)
        )
        assert draft is not None
        description_review = await submit_asset_description_draft(
            draft.id, context=steward, session=session
        )
        await _approve(session, description_review.id, reviewer)

        # 4. Publish context: a governed tool over the view, and a product pinning it.
        drafted_tool = await create_view_tool_blueprint(
            project.id,
            ViewToolBlueprintRequest(
                slug="customer_revenue",
                name="Customer revenue",
                description="Net revenue per customer and region, from the customer_revenue view.",
                datasource_id=datasource.id,
                table_id=view.id,
                allowed_roles=["Analyst"],
            ),
            context=tool_developer,
            session=session,
            settings=settings,
        )
        tool_review = await submit_tool_for_review(
            drafted_tool.id, context=tool_developer, session=session
        )
        await _approve(session, tool_review.id, reviewer)
        first_tool = await session.get(GovernedToolVersion, drafted_tool.id)
        assert first_tool is not None
        assert (first_tool.status, first_tool.source_view_table_id) == ("PUBLISHED", view.id)
        assert first_tool.source_definition_fingerprint == original_fingerprint

        await create_context_product(
            project.id,
            ContextProductCreate(
                product_key="customer-revenue",
                name="Customer revenue",
                description="Revenue per customer, for agents answering revenue questions.",
                purpose="Answer questions about net revenue per customer and region.",
                owner_type="INDIVIDUAL",
                owner_principal="steward-1",
                table_ids=[view.id, customers.id, orders.id],
                eligible_tool_version_ids=[first_tool.id],
                allowed_consumer_roles=["Analyst"],
            ),
            context=steward,
            session=session,
        )
        first_product = await session.scalar(
            select(ContextProductVersion)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(ContextProduct.product_key == "customer-revenue")
        )
        assert first_product is not None
        product_review = await submit_context_product_version(
            first_product.id, context=steward, session=session
        )
        await _approve(session, product_review.id, reviewer)

        # 5. Answer: customer 1's orders are 100 - 10 and 60 - 0.
        assert await _revenue(session, first_tool.id, analyst, settings) == 150.0

        # 6. Change the source's logic, and read the source again.
        await _execute(source, REDEFINED_VIEW)
        rescan = await _scan(session, datasource, source)
        signals = list(
            await session.scalars(
                select(MetadataChangeSignal).where(
                    MetadataChangeSignal.analysis_run_id == rescan.id,
                    MetadataChangeSignal.subject_id == view.id,
                )
            )
        )
        assert [(s.signal_type, s.change_class) for s in signals] == [
            ("DEFINITION_CHANGED", "STRUCTURAL")
        ]
        await session.refresh(definition)
        assert definition.definition_fingerprint != original_fingerprint

        # 7. The change holds what it can have broken: no stale answer is served.
        await process_change_signals(session, organization_id=org.id, limit=100)
        await session.commit()
        hold = await _hold(session, view)
        assert hold is not None and (hold.severity, hold.status) == ("CRITICAL", "OPEN")
        with pytest.raises(HTTPException) as held:
            await _revenue(session, first_tool.id, analyst, settings)
        assert held.value.status_code == 409

        # 8. Rebuild. The lineage agent reads the new definition; the rebuild retires the edge
        # it no longer produces and drafts a regenerated tool and description into review.
        await _investigate(session, org, datasource, reviewer, settings)
        first_pass = await run_context_rebuild(session, org.id, settings=settings)
        await session.commit()
        assert first_pass.lineage_edges_superseded >= 1
        assert (
            first_pass.tools_drafted,
            first_pass.descriptions_drafted,
            first_pass.products_drafted,
            first_pass.holds_released,
        ) == (1, 1, 0, 0), first_pass.as_details()
        current_lineage = set(
            await session.scalars(
                select(ViewLineageEdge.source_column).where(
                    ViewLineageEdge.target_table_id == view.id,
                    ViewLineageEdge.review_status == "ACTIVE",
                )
            )
        )
        assert "discount" not in current_lineage and "amount" in current_lineage

        # Reviewers approve the rebuilt tool and description; the product follows the tool.
        assert await _approve_rebuilt(session, reviewer, "GOVERNED_TOOL_VERSION") == 1
        assert await _approve_rebuilt(session, reviewer, "ASSET_DESCRIPTION_DRAFT") == 1
        second_pass = await run_context_rebuild(session, org.id, settings=settings)
        await session.commit()
        assert (second_pass.products_drafted, second_pass.holds_released) == (1, 0)
        assert second_pass.waiting == {"PRODUCT_STALE": 1}
        assert await _approve_rebuilt(session, reviewer, "CONTEXT_PRODUCT_VERSION") == 1

        # Nothing standing on the view is stale any more: the hold is released.
        third_pass = await run_context_rebuild(session, org.id, settings=settings)
        await session.commit()
        assert third_pass.holds_released == 1
        hold = await _hold(session, view)
        assert hold is not None and (hold.status, hold.resolved_by) == (
            "RESOLVED",
            CONTEXT_REBUILD_PRINCIPAL,
        )

        # 9. Answer correctly again, through the rebuilt, re-approved context.
        rebuilt_tool = await session.scalar(
            select(GovernedToolVersion).where(
                GovernedToolVersion.tool_id == first_tool.tool_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
        )
        assert rebuilt_tool is not None and rebuilt_tool.version == 2
        assert rebuilt_tool.source_definition_fingerprint == definition.definition_fingerprint
        current_product = await session.scalar(
            select(ContextProductVersion).where(
                ContextProductVersion.product_id == first_product.product_id,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
        assert current_product is not None
        assert current_product.eligible_tool_version_ids == [str(rebuilt_tool.id)]
        assert await _revenue(session, rebuilt_tool.id, analyst, settings) == 160.0
        with pytest.raises(HTTPException):
            await _revenue(session, first_tool.id, analyst, settings)
