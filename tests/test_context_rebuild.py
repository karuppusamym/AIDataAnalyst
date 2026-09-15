"""R11-FP16: rebuild what a source change made stale, into review queues, and release the hold.

The same chain `test_footprint_journey` walks against live sources, driven here on
in-memory SQLite through the real draft, review and decision routes, so it runs without a server:

* a redefined view's tool, description and context product are rebuilt as drafts in their review
  queues; the pass publishes nothing and does not draft twice while a review waits;
* the hold is released only once every rebuilt artifact is approved;
* a hand-written tool reading the view keeps the hold until a version is approved after the change;
* a view no longer eligible for a tool blocks that rebuild with its eligibility code;
* a retired table's hold is never released by the pass;
* a routine tool is regenerated bound to the routine's new definition;
* the scheduler pass is off by default and opens no session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_api import (
    generate_asset_description_drafts,
    submit_asset_description_draft,
)
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import SOURCE_CHANGE_ANOMALY_TYPE, process_change_signals
from aida.config import Settings
from aida.context_product_api import create_context_product, submit_context_product_version
from aida.context_rebuild import CONTEXT_REBUILD_PRINCIPAL, RebuildOutcome, run_context_rebuild
from aida.envelope_models import MetadataViewDefinition
from aida.lineage_agent import as_create_view
from aida.models import (
    AssetDescriptionDraft,
    ContextProduct,
    ContextProductVersion,
    DataQualityIncident,
    DataSource,
    GovernanceReview,
    GovernedToolVersion,
    MetadataColumn,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
    ViewLineageEdge,
)
from aida.procedure_tool_api import ProcedureToolBlueprintRequest, create_procedure_tool_blueprint
from aida.schemas import (
    AssetDescriptionDraftGenerate,
    ContextProductCreate,
    GovernanceDecisionRequest,
    GovernedToolVersionCreate,
)
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.sql_lineage_parser import parse_view_lineage
from aida.tool_api import (
    ViewToolBlueprintRequest,
    create_tool_version,
    create_view_tool_blueprint,
    submit_tool_for_review,
)
from aida.workflows import scheduler
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    seed_table,
    task_agent_session,
)
from tests.test_procedure_tool_blueprint import _Scenario as ProcedureScenario
from tests.test_tool_source_binding import _REPORT_BODY

VIEW_SQL = (
    "SELECT c.customer_id, c.region, SUM(o.amount - o.discount) AS net_revenue "
    "FROM public.customers c JOIN public.orders o ON o.customer_id = c.customer_id "
    "GROUP BY c.customer_id, c.region"
)
REDEFINED_SQL = VIEW_SQL.replace("o.amount - o.discount", "o.amount")
REVIEWER_ROLES = frozenset({"PlatformAdmin", "DataSteward", "MetadataReviewer"})


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with task_agent_session() as active:
        yield active


@dataclass
class Estate:
    session: AsyncSession
    org: Organization
    datasource: DataSource
    project: Project
    orders: MetadataTable
    view: MetadataTable
    definition: MetadataViewDefinition
    settings: Settings

    @property
    def reviewer(self) -> SecurityContext:
        return human(self.org, "reviewer-1", REVIEWER_ROLES)

    @property
    def steward(self) -> SecurityContext:
        return human(self.org, "steward-1", frozenset({"DataSteward", "MetadataAdmin"}))

    @property
    def developer(self) -> SecurityContext:
        return human(self.org, "tool-developer-1", frozenset({"ToolDeveloper"}))


async def _columns(
    session: AsyncSession, org: Organization, table: MetadataTable, *specs: tuple[str, str]
) -> None:
    for position, (name, physical_type) in enumerate(specs, start=1):
        session.add(
            MetadataColumn(
                organization_id=org.id,
                table_id=table.id,
                name=name,
                ordinal_position=position,
                physical_type=physical_type,
                nullable=False,
                fingerprint="fp",
            )
        )
    await session.flush()


async def _approve(estate: Estate, review_id: object) -> None:
    await decide_governance_review(
        review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=estate.reviewer,
        session=estate.session,
    )


async def _estate(
    session: AsyncSession, *, tool: bool = False, description: bool = False, product: bool = False
) -> Estate:
    org, datasource, schema = await seed_estate(session)
    project = await session.get(Project, datasource.project_id)
    assert project is not None
    customers = await seed_table(session, org, datasource, schema, name="customers")
    orders = await seed_table(session, org, datasource, schema, name="orders")
    view = await seed_table(
        session, org, datasource, schema, name="customer_revenue", object_type="VIEW"
    )
    await _columns(session, org, customers, ("customer_id", "integer"), ("region", "varchar"))
    await _columns(
        session,
        org,
        orders,
        ("order_id", "integer"),
        ("customer_id", "integer"),
        ("amount", "numeric"),
        ("discount", "numeric"),
    )
    await _columns(
        session,
        org,
        view,
        ("customer_id", "integer"),
        ("region", "varchar"),
        ("net_revenue", "numeric"),
    )
    definition = MetadataViewDefinition(
        organization_id=org.id,
        datasource_id=datasource.id,
        table_id=view.id,
        definition_sql_redacted=VIEW_SQL,
        definition_fingerprint="a" * 64,
        redaction_status="PARSED",
        fingerprint="fp",
    )
    session.add(definition)
    parsed = parse_view_lineage(as_create_view("public.customer_revenue", VIEW_SQL))
    for edge in parsed.edges:
        session.add(
            ViewLineageEdge(
                organization_id=org.id,
                datasource_id=datasource.id,
                source_table=edge.source_table,
                source_column=edge.source_column,
                target_table=edge.target_table,
                target_column=edge.target_column,
                source_table_id={"customers": customers.id, "orders": orders.id}.get(
                    edge.source_table.rsplit(".", 1)[-1]
                ),
                target_table_id=view.id,
                transformation_type=edge.transformation_type,
                confidence=edge.confidence,
                dialect=edge.dialect,
                sql_hash=parsed.sql_hash,
                review_status="ACTIVE",
                created_by="agent:lineage",
            )
        )
    await session.commit()
    estate = Estate(session, org, datasource, project, orders, view, definition, agent_settings())

    tool_version_id = None
    if description:
        await generate_asset_description_drafts(
            org.id,
            AssetDescriptionDraftGenerate(table_ids=[view.id]),
            context=estate.steward,
            session=session,
        )
        draft = await session.scalar(
            select(AssetDescriptionDraft).where(AssetDescriptionDraft.table_id == view.id)
        )
        assert draft is not None
        review = await submit_asset_description_draft(
            draft.id, context=estate.steward, session=session
        )
        await _approve(estate, review.id)
    if tool:
        drafted = await create_view_tool_blueprint(
            project.id,
            ViewToolBlueprintRequest(
                slug="customer_revenue",
                name="Customer revenue",
                description="Net revenue per customer and region.",
                datasource_id=datasource.id,
                table_id=view.id,
                allowed_roles=["Analyst"],
            ),
            context=estate.developer,
            session=session,
            settings=estate.settings,
        )
        review = await submit_tool_for_review(drafted.id, context=estate.developer, session=session)
        await _approve(estate, review.id)
        tool_version_id = drafted.id
    if product:
        assert tool_version_id is not None
        await create_context_product(
            project.id,
            ContextProductCreate(
                product_key="customer-revenue",
                name="Customer revenue",
                description="Revenue per customer, for agents answering revenue questions.",
                purpose="Answer questions about net revenue per customer and region.",
                owner_type="INDIVIDUAL",
                owner_principal="steward-1",
                table_ids=[view.id],
                eligible_tool_version_ids=[tool_version_id],
                allowed_consumer_roles=["Analyst"],
            ),
            context=estate.steward,
            session=session,
        )
        version = await session.scalar(
            select(ContextProductVersion)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(ContextProduct.product_key == "customer-revenue")
        )
        assert version is not None
        review = await submit_context_product_version(
            version.id, context=estate.steward, session=session
        )
        await _approve(estate, review.id)
    return estate


async def _redefine(estate: Estate, *, screening_status: str = "CLEAN") -> None:
    """The source redefines the view; a rescan's signal is processed into a hold."""
    estate.definition.definition_sql_redacted = REDEFINED_SQL
    estate.definition.definition_fingerprint = "b" * 64
    estate.definition.screening_status = screening_status
    estate.session.add(
        MetadataChangeSignal(
            organization_id=estate.org.id,
            datasource_id=estate.datasource.id,
            subject_kind="VIEW",
            subject_id=estate.view.id,
            signal_type="DEFINITION_CHANGED",
            change_class="STRUCTURAL",
        )
    )
    await estate.session.flush()
    await process_change_signals(estate.session, organization_id=estate.org.id, limit=100)
    await estate.session.commit()


async def _rebuild(estate: Estate) -> RebuildOutcome:
    outcome = await run_context_rebuild(estate.session, estate.org.id, settings=estate.settings)
    await estate.session.commit()
    return outcome


async def _approve_rebuilt(estate: Estate, object_type: str) -> int:
    reviews = list(
        await estate.session.scalars(
            select(GovernanceReview.id).where(
                GovernanceReview.requested_by == CONTEXT_REBUILD_PRINCIPAL,
                GovernanceReview.object_type == object_type,
                GovernanceReview.status == "PENDING",
            )
        )
    )
    for review_id in reviews:
        await _approve(estate, review_id)
    return len(reviews)


async def _hold(estate: Estate, table: MetadataTable) -> DataQualityIncident:
    hold = await estate.session.scalar(
        select(DataQualityIncident)
        .where(
            DataQualityIncident.table_id == table.id,
            DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
        )
        .execution_options(populate_existing=True)
    )
    assert hold is not None
    return hold


async def _hand_written_tool(estate: Estate) -> None:
    drafted = await create_tool_version(
        estate.project.id,
        GovernedToolVersionCreate(
            slug="revenue_report",
            name="Revenue report",
            description="Revenue per customer, written by hand over the view.",
            datasource_id=estate.datasource.id,
            sql_template="SELECT customer_id, net_revenue FROM public.customer_revenue",
            allowed_roles=["Analyst"],
        ),
        context=estate.developer,
        session=estate.session,
        settings=estate.settings,
    )
    review = await submit_tool_for_review(
        drafted.id, context=estate.developer, session=estate.session
    )
    await _approve(estate, review.id)


async def test_a_redefined_view_is_rebuilt_into_review_and_its_hold_released_once_approved(
    session: AsyncSession,
) -> None:
    estate = await _estate(session, tool=True, description=True, product=True)
    await _redefine(estate)

    first = await _rebuild(estate)
    assert (
        first.tools_drafted,
        first.descriptions_drafted,
        first.products_drafted,
        first.holds_released,
    ) == (1, 1, 0, 0), first.as_details()
    assert first.lineage_edges_superseded >= 1
    active_sources = set(
        await session.scalars(
            select(ViewLineageEdge.source_column).where(
                ViewLineageEdge.target_table_id == estate.view.id,
                ViewLineageEdge.review_status == "ACTIVE",
            )
        )
    )
    assert "discount" not in active_sources

    # The pass publishes nothing, and drafts nothing again while its reviews wait.
    again = await _rebuild(estate)
    assert (again.tools_drafted, again.descriptions_drafted, again.products_drafted) == (0, 0, 0)
    assert again.waiting == {"TOOL_STALE": 1}
    published_tools = await session.scalar(
        select(func.count())
        .select_from(GovernedToolVersion)
        .where(GovernedToolVersion.status == "PUBLISHED")
    )
    assert published_tools == 1

    assert await _approve_rebuilt(estate, "GOVERNED_TOOL_VERSION") == 1
    assert await _approve_rebuilt(estate, "ASSET_DESCRIPTION_DRAFT") == 1
    second = await _rebuild(estate)
    assert (second.products_drafted, second.holds_released) == (1, 0)
    assert second.waiting == {"PRODUCT_STALE": 1}
    assert (await _hold(estate, estate.view)).status == "OPEN"

    assert await _approve_rebuilt(estate, "CONTEXT_PRODUCT_VERSION") == 1
    third = await _rebuild(estate)
    assert third.holds_released == 1
    hold = await _hold(estate, estate.view)
    assert (hold.status, hold.resolved_by) == ("RESOLVED", CONTEXT_REBUILD_PRINCIPAL)
    resolved = await session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "data_quality.incident_resolved",
            OutboxEvent.aggregate_id == str(hold.id),
        )
    )
    assert resolved is not None


async def test_a_hand_written_tool_reading_the_view_keeps_the_hold_until_re_approved(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    await _hand_written_tool(estate)
    await _redefine(estate)

    first = await _rebuild(estate)
    assert first.holds_released == 0
    assert first.waiting == {"TOOL_NOT_REVERIFIED": 1}

    # A person reviews the tool against the new definition and approves a version of it.
    await _hand_written_tool(estate)
    second = await _rebuild(estate)
    assert second.holds_released == 1


async def test_a_view_no_longer_eligible_for_a_tool_blocks_its_rebuild_and_keeps_the_hold(
    session: AsyncSession,
) -> None:
    estate = await _estate(session, tool=True)
    await _redefine(estate, screening_status="QUARANTINED")

    outcome = await _rebuild(estate)

    assert outcome.tools_drafted == 0
    assert outcome.blocked == {"VIEW_DEFINITION_QUARANTINED": 1}
    assert outcome.waiting == {"TOOL_STALE": 1}
    assert outcome.lineage_edges_superseded == 0, "quarantined text is not read"
    assert (await _hold(estate, estate.view)).status == "OPEN"


async def test_a_retired_table_hold_is_never_released_by_the_pass(session: AsyncSession) -> None:
    estate = await _estate(session)
    session.add(
        MetadataChangeSignal(
            organization_id=estate.org.id,
            datasource_id=estate.datasource.id,
            subject_kind="TABLE",
            subject_id=estate.orders.id,
            signal_type="DEPRECATED",
        )
    )
    await session.flush()
    await process_change_signals(session, organization_id=estate.org.id, limit=100)
    await session.commit()

    outcome = await _rebuild(estate)

    assert outcome.holds_released == 0
    assert (await _hold(estate, estate.orders)).status == "OPEN"


async def test_a_routine_tool_is_regenerated_bound_to_the_routine_new_definition(
    session: AsyncSession,
) -> None:
    scenario = await ProcedureScenario(session).build()
    routine = scenario.routine(body=_REPORT_BODY)
    routine.body_fingerprint = "a" * 64
    session.add(routine)
    await session.flush()
    created = await create_procedure_tool_blueprint(
        scenario.project.id,
        ProcedureToolBlueprintRequest(
            slug="customer_order_totals",
            name="Customer order totals",
            description="Read surface from a proven read-only procedure.",
            datasource_id=scenario.datasource.id,
            routine_id=routine.id,
            allowed_roles=["Analyst"],
        ),
        context=scenario.maker(),
        session=session,
        settings=Settings(),
    )
    review = await submit_tool_for_review(created.id, context=scenario.maker(), session=session)
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=human(scenario.organization, "reviewer-1", REVIEWER_ROLES),
        session=session,
    )
    routine.body_sql_redacted = _REPORT_BODY.replace("SUM(o.amount)", "MAX(o.amount)")
    routine.body_fingerprint = "b" * 64
    await session.commit()

    outcome = await run_context_rebuild(session, scenario.organization.id, settings=Settings())
    await session.commit()

    assert outcome.tools_drafted == 1, outcome.as_details()
    rebuilt = await session.scalar(
        select(GovernedToolVersion).where(GovernedToolVersion.status == "REVIEW_REQUIRED")
    )
    assert rebuilt is not None
    assert (rebuilt.source_routine_id, rebuilt.source_definition_fingerprint, rebuilt.version) == (
        routine.id,
        "b" * 64,
        2,
    )
    assert rebuilt.created_by == CONTEXT_REBUILD_PRINCIPAL
    pending = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_id == str(rebuilt.id),
            GovernanceReview.requested_by == CONTEXT_REBUILD_PRINCIPAL,
            GovernanceReview.status == "PENDING",
        )
    )
    assert pending is not None


async def test_the_scheduler_pass_is_off_by_default_and_opens_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> None:
        raise AssertionError("the pass opened a session while off")

    monkeypatch.setattr(scheduler, "session_factory", refuse)
    assert await scheduler.run_context_rebuild_pass(Settings(_env_file=None)) == 0
