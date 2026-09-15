"""R11-FP16: rebuild what a source change made stale, into review queues, and release the hold.

The same chain `test_footprint_journey` walks against live sources, driven here on
in-memory SQLite through the real draft, review and decision routes, so it runs without a server:

* a redefined view's tool, description and context product are rebuilt as drafts in their review
  queues; the pass publishes nothing and does not draft twice while a review waits;
* the hold is released only once every rebuilt artifact is approved;
* a hand-written tool reading the view keeps the hold until a version is approved after the change;
* a view no longer eligible for a tool blocks that rebuild with its eligibility code;
* a table the source no longer has is retired through review: DEPRECATE reviews for the tools
  reading it and a context product version without it; its hold is released once approved,
  and a rejected proposal keeps the hold and is not proposed again;
* a table back unchanged is released; a column a tool names leaving proposes retiring the tool;
  a retyped column waits for re-approval and an added one does not;
* a table description written against other columns is redrafted, and never approved stale;
* a routine tool is regenerated bound to the routine's new definition;
* the scheduler pass is off by default and opens no session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import HTTPException
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
from aida.context_rebuild import (
    CONTEXT_REBUILD_PRINCIPAL,
    RESHAPE_RELEASE_REASON,
    RETIREMENT_RELEASE_REASON,
    RebuildOutcome,
    organizations_needing_rebuild,
    run_context_rebuild,
)
from aida.document_ingestion import (
    create_document_from_csv,
    extract_description_claims,
    resolve_structural_mappings,
)
from aida.envelope_models import MetadataViewDefinition
from aida.lineage_agent import as_create_view
from aida.models import (
    AnalysisRun,
    AssetDescriptionDraft,
    ContextProduct,
    ContextProductVersion,
    DataQualityIncident,
    DataSource,
    DocumentClaim,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    GovernedToolVersion,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
    SemanticModelVersion,
    ViewLineageEdge,
)
from aida.ontology_models import OntologyHead, OntologyVersion
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


async def _orders_tool(
    estate: Estate,
    sql: str = "SELECT order_id, amount FROM public.orders",
    *,
    slug: str = "orders_report",
) -> GovernedToolVersion:
    """A tool a developer wrote by hand over the orders table, reviewed and published."""
    drafted = await create_tool_version(
        estate.project.id,
        GovernedToolVersionCreate(
            slug=slug,
            name="Orders report",
            description="Order amounts, written by hand over the orders table.",
            datasource_id=estate.datasource.id,
            sql_template=sql,
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
    version = await estate.session.get(GovernedToolVersion, drafted.id, populate_existing=True)
    assert version is not None and version.status == "PUBLISHED"
    return version


async def _table_signal(
    estate: Estate, table: MetadataTable, signal_type: str, change_class: str | None = None
) -> None:
    """A rescan's signal about a table, processed into its hold."""
    estate.session.add(
        MetadataChangeSignal(
            organization_id=estate.org.id,
            datasource_id=estate.datasource.id,
            subject_kind="TABLE",
            subject_id=table.id,
            signal_type=signal_type,
            change_class=change_class,
        )
    )
    await estate.session.flush()
    await process_change_signals(estate.session, organization_id=estate.org.id, limit=100)
    await estate.session.commit()


async def _orders_column(estate: Estate, name: str) -> MetadataColumn:
    column = await estate.session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.table_id == estate.orders.id, MetadataColumn.name == name
        )
    )
    assert column is not None
    return column


async def _add_orders_column(estate: Estate, name: str = "channel") -> None:
    estate.session.add(
        MetadataColumn(
            organization_id=estate.org.id,
            table_id=estate.orders.id,
            name=name,
            ordinal_position=5,
            physical_type="varchar",
            nullable=True,
            fingerprint="fp",
        )
    )
    await estate.session.flush()


async def _reject_rebuilt(estate: Estate, object_type: str) -> int:
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
        await decide_governance_review(
            review_id,
            GovernanceDecisionRequest(decision="REJECT", reason="Kept while checked."),
            context=estate.reviewer,
            session=estate.session,
        )
    return len(reviews)


async def test_a_retired_table_is_retired_through_review_and_its_hold_released(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    customers = await session.scalar(
        select(MetadataTable).where(
            MetadataTable.datasource_id == estate.datasource.id, MetadataTable.name == "customers"
        )
    )
    assert customers is not None
    tool = await _orders_tool(estate)
    await create_context_product(
        estate.project.id,
        ContextProductCreate(
            product_key="orders",
            name="Orders",
            description="Orders and customers, for agents answering order questions.",
            purpose="Answer questions about order amounts per customer.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            table_ids=[estate.orders.id, customers.id],
            eligible_tool_version_ids=[tool.id],
            allowed_consumer_roles=["Analyst"],
        ),
        context=estate.steward,
        session=session,
    )
    product = await session.scalar(
        select(ContextProductVersion)
        .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
        .where(ContextProduct.product_key == "orders")
    )
    assert product is not None
    review = await submit_context_product_version(
        product.id, context=estate.steward, session=session
    )
    await _approve(estate, review.id)

    # The source no longer has the orders table: the rescan retires it and it is held.
    estate.orders.status = "DEPRECATED"
    await _table_signal(estate, estate.orders, "DEPRECATED")
    assert (await _hold(estate, estate.orders)).severity == "CRITICAL"

    first = await _rebuild(estate)
    assert (first.deprecations_proposed, first.products_drafted, first.holds_released) == (
        1,
        1,
        0,
    ), first.as_details()
    assert first.waiting == {"RETIRED_TABLE_IN_USE": 1}
    assert await _approve_rebuilt(estate, "GOVERNED_TOOL_VERSION") == 1
    assert await _approve_rebuilt(estate, "CONTEXT_PRODUCT_VERSION") == 1

    second = await _rebuild(estate)
    assert (second.deprecations_proposed, second.products_drafted, second.holds_released) == (
        0,
        0,
        1,
    ), second.as_details()
    hold = await _hold(estate, estate.orders)
    assert (hold.status, hold.resolved_by, hold.resolution_reason) == (
        "RESOLVED",
        CONTEXT_REBUILD_PRINCIPAL,
        RETIREMENT_RELEASE_REASON,
    )
    await session.refresh(tool)
    assert tool.status == "DEPRECATED"
    current = await session.scalar(
        select(ContextProductVersion).where(
            ContextProductVersion.product_id == product.product_id,
            ContextProductVersion.status == "PUBLISHED",
        )
    )
    assert current is not None
    assert (current.table_ids, current.eligible_tool_version_ids) == ([str(customers.id)], [])
    assert not (await _rebuild(estate)).acted


async def test_a_rejected_retirement_keeps_the_hold_and_is_not_proposed_again(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    await _orders_tool(estate)
    estate.orders.status = "DEPRECATED"
    await _table_signal(estate, estate.orders, "DEPRECATED")

    assert (await _rebuild(estate)).deprecations_proposed == 1
    assert await _reject_rebuilt(estate, "GOVERNED_TOOL_VERSION") == 1

    again = await _rebuild(estate)
    assert (again.deprecations_proposed, again.holds_released) == (0, 0), again.as_details()
    assert again.waiting == {"RETIRED_TABLE_IN_USE": 1}
    assert (await _hold(estate, estate.orders)).status == "OPEN"


async def test_a_table_back_in_the_source_unchanged_is_released_without_retiring_anything(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    tool = await _orders_tool(estate)
    estate.orders.status = "DEPRECATED"
    await _table_signal(estate, estate.orders, "DEPRECATED")
    estate.orders.status = "ACTIVE"
    await _table_signal(estate, estate.orders, "REACTIVATED")
    await _table_signal(estate, estate.orders, "STRUCTURE_CHANGED", "COLUMNS_RETURNED")

    outcome = await _rebuild(estate)

    assert (outcome.deprecations_proposed, outcome.holds_released) == (0, 1), outcome.as_details()
    hold = await _hold(estate, estate.orders)
    assert (hold.status, hold.resolution_reason) == ("RESOLVED", RESHAPE_RELEASE_REASON)
    await session.refresh(tool)
    assert tool.status == "PUBLISHED"


async def test_a_column_a_tool_names_leaving_its_table_proposes_retiring_that_tool(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    discount_tool = await _orders_tool(estate, "SELECT order_id, discount FROM public.orders")
    await _orders_tool(estate, slug="order_amounts")
    (await _orders_column(estate, "discount")).status = "DEPRECATED"
    await _table_signal(estate, estate.orders, "STRUCTURE_CHANGED", "COLUMNS_REMOVED")
    assert (await _hold(estate, estate.orders)).severity == "WARNING"

    first = await _rebuild(estate)
    assert (first.deprecations_proposed, first.holds_released) == (1, 0), first.as_details()
    assert first.waiting == {"TOOL_COLUMNS_MISSING": 1}
    proposal = await session.scalar(
        select(GovernanceReview).where(GovernanceReview.requested_action == "DEPRECATE")
    )
    assert proposal is not None and proposal.object_id == str(discount_tool.id)
    assert await _approve_rebuilt(estate, "GOVERNED_TOOL_VERSION") == 1

    second = await _rebuild(estate)
    assert second.holds_released == 1, second.as_details()
    assert (await _hold(estate, estate.orders)).resolution_reason == RESHAPE_RELEASE_REASON


@pytest.mark.parametrize(
    ("change_class", "answers_can_move"), [("COLUMNS_ADDED", False), ("COLUMNS_RETYPED", True)]
)
async def test_a_reshape_that_can_change_an_answer_waits_for_re_approval_and_others_do_not(
    session: AsyncSession, change_class: str, answers_can_move: bool
) -> None:
    estate = await _estate(session)
    await _orders_tool(estate)
    if answers_can_move:
        (await _orders_column(estate, "amount")).physical_type = "varchar"
    else:
        await _add_orders_column(estate)
    await _table_signal(estate, estate.orders, "STRUCTURE_CHANGED", change_class)

    first = await _rebuild(estate)

    if not answers_can_move:
        assert first.holds_released == 1, first.as_details()
        return
    assert (first.deprecations_proposed, first.holds_released) == (0, 0), first.as_details()
    assert first.waiting == {"TOOL_NOT_REVERIFIED": 1}
    # The developer re-approves the tool against the retyped column: a new version, reviewed.
    await _orders_tool(estate)
    assert (await _rebuild(estate)).holds_released == 1


async def _key_orders(estate: Estate) -> None:
    """A declared key: the structural evidence a table description needs to reach review."""
    estate.session.add(
        MetadataConstraint(
            organization_id=estate.org.id,
            datasource_id=estate.datasource.id,
            table_id=estate.orders.id,
            name="orders_pkey",
            constraint_type="PRIMARY_KEY",
            columns=["order_id"],
            fingerprint="fp",
        )
    )
    await estate.session.flush()


async def test_a_table_description_written_against_other_columns_is_redrafted_into_review(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    await _key_orders(estate)
    await generate_asset_description_drafts(
        estate.org.id,
        AssetDescriptionDraftGenerate(table_ids=[estate.orders.id]),
        context=estate.steward,
        session=session,
    )
    draft = await session.scalar(
        select(AssetDescriptionDraft).where(AssetDescriptionDraft.table_id == estate.orders.id)
    )
    assert draft is not None and draft.evidence["column_digest"]
    review = await submit_asset_description_draft(draft.id, context=estate.steward, session=session)
    await _approve(estate, review.id)

    await _add_orders_column(estate)
    await _table_signal(estate, estate.orders, "STRUCTURE_CHANGED", "COLUMNS_ADDED")

    first = await _rebuild(estate)
    assert (first.descriptions_drafted, first.holds_released) == (1, 0), first.as_details()
    assert first.waiting == {"DESCRIPTION_STALE": 1}
    assert await _approve_rebuilt(estate, "ASSET_DESCRIPTION_DRAFT") == 1
    assert (await _rebuild(estate)).holds_released == 1


async def test_a_table_description_is_not_approved_once_its_columns_moved(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    await _key_orders(estate)
    await generate_asset_description_drafts(
        estate.org.id,
        AssetDescriptionDraftGenerate(table_ids=[estate.orders.id]),
        context=estate.steward,
        session=session,
    )
    draft = await session.scalar(
        select(AssetDescriptionDraft).where(AssetDescriptionDraft.table_id == estate.orders.id)
    )
    assert draft is not None
    review = await submit_asset_description_draft(draft.id, context=estate.steward, session=session)
    await _add_orders_column(estate)

    with pytest.raises(HTTPException) as refused:
        await _approve(estate, review.id)

    assert refused.value.status_code == 409
    detail = refused.value.detail
    assert isinstance(detail, dict) and detail["code"] == "COLUMNS_MOVED"


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


async def test_a_proposed_document_is_mapped_again_after_a_scan_and_new_matches_go_to_review(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    document = await create_document_from_csv(
        session,
        organization_id=estate.org.id,
        project_id=estate.project.id,
        filename="dictionary.csv",
        content=(
            "schema,table,column,description\n"
            "public,orders,amount,gross amount of the order\n"
            "public,orders,channel,how the order was placed\n"
        ),
        uploaded_by="maker@example.com",
    )
    await resolve_structural_mappings(session, document)
    assert len(await extract_description_claims(session, document, requested_by="steward-1")) == 1
    await session.commit()
    assert estate.org.id not in await organizations_needing_rebuild(session)

    # The source adds the column the dictionary already describes, and a scan reads it.
    await _add_orders_column(estate)
    session.add(
        AnalysisRun(
            organization_id=estate.org.id,
            datasource_id=estate.datasource.id,
            mode="FULL",
            trigger_type="MANUAL",
            status="COMPLETED",
        )
    )
    await session.commit()
    assert estate.org.id in await organizations_needing_rebuild(session)

    first = await _rebuild(estate)
    assert (first.sections_remapped, first.claims_proposed) == (1, 1), first.as_details()
    assert await _approve_rebuilt(estate, "DOCUMENT_CLAIM") == 1
    claim = await session.scalar(
        select(DocumentClaim).where(DocumentClaim.created_by == "maker@example.com")
    )
    assert claim is not None and claim.status == "APPROVED"
    assert not (await _rebuild(estate)).acted
    assert estate.org.id not in await organizations_needing_rebuild(session)


async def test_a_context_product_pinning_an_earlier_ontology_version_is_re_pinned_through_review(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    head = OntologyHead(
        organization_id=estate.org.id, ontology_key="commerce", last_version=1, published_version=1
    )
    session.add(head)
    await session.flush()
    first = OntologyVersion(
        organization_id=estate.org.id,
        ontology_id=head.id,
        version=1,
        base_version=0,
        status="APPROVED",
        definition={"name": "Commerce", "concepts": []},
        created_by="author",
    )
    session.add(first)
    await session.flush()
    await create_context_product(
        estate.project.id,
        ContextProductCreate(
            product_key="orders-meaning",
            name="Orders",
            description="Orders, read in the commerce ontology's terms.",
            purpose="Answer order questions in the commerce ontology's terms.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            table_ids=[estate.orders.id],
            ontology_version_ids=[first.id],
            allowed_consumer_roles=["Analyst"],
        ),
        context=estate.steward,
        session=session,
    )
    product = await session.scalar(
        select(ContextProductVersion)
        .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
        .where(ContextProduct.product_key == "orders-meaning")
    )
    assert product is not None
    review = await submit_context_product_version(
        product.id, context=estate.steward, session=session
    )
    await _approve(estate, review.id)
    assert not (await _rebuild(estate)).acted

    # The ontology publishes a second version; the product still pins the first.
    second = OntologyVersion(
        organization_id=estate.org.id,
        ontology_id=head.id,
        version=2,
        base_version=1,
        status="APPROVED",
        definition={"name": "Commerce", "concepts": []},
        created_by="author",
    )
    session.add(second)
    head.last_version = 2
    head.published_version = 2
    await session.commit()
    assert estate.org.id in await organizations_needing_rebuild(session)

    outcome = await _rebuild(estate)
    assert outcome.products_drafted == 1, outcome.as_details()
    assert await _approve_rebuilt(estate, "CONTEXT_PRODUCT_VERSION") == 1
    current = await session.scalar(
        select(ContextProductVersion).where(
            ContextProductVersion.product_id == product.product_id,
            ContextProductVersion.status == "PUBLISHED",
        )
    )
    assert current is not None and current.ontology_version_ids == [str(second.id)]
    assert not (await _rebuild(estate)).acted


async def test_a_product_pinning_superseded_semantic_and_glossary_versions_is_re_pinned(
    session: AsyncSession,
) -> None:
    """A product pinning a superseded semantic model or glossary term version serves meaning its
    project and term have moved past, and no re-pin draft of it could pass validation, which
    accepts only the current versions. The rebuild drafts one version pinned to both."""
    estate = await _estate(session)
    term = GlossaryTerm(
        organization_id=estate.org.id, term_key="net_revenue", lifecycle_status="ACTIVE"
    )
    session.add(term)
    await session.flush()

    def model(version: int, status: str) -> SemanticModelVersion:
        return SemanticModelVersion(
            organization_id=estate.org.id,
            project_id=estate.project.id,
            version=version,
            name="Revenue model",
            change_summary=f"Version {version}.",
            status=status,
            created_by="modeller",
        )

    def definition(version: int, status: str) -> GlossaryTermVersion:
        return GlossaryTermVersion(
            organization_id=estate.org.id,
            term_id=term.id,
            version=version,
            status=status,
            display_name="Net revenue",
            definition=f"Revenue after discounts, as defined in version {version}.",
            synonyms=[],
            created_by="steward-2",
        )

    first_model, first_term = model(1, "PUBLISHED"), definition(1, "APPROVED")
    session.add_all([first_model, first_term])
    await session.flush()
    await create_context_product(
        estate.project.id,
        ContextProductCreate(
            product_key="orders-semantics",
            name="Orders",
            description="Orders, in the revenue model's and glossary's terms.",
            purpose="Answer order questions with the approved revenue definitions.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            table_ids=[estate.orders.id],
            semantic_model_version_ids=[first_model.id],
            glossary_term_version_ids=[first_term.id],
            allowed_consumer_roles=["Analyst"],
        ),
        context=estate.steward,
        session=session,
    )
    product = await session.scalar(
        select(ContextProductVersion)
        .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
        .where(ContextProduct.product_key == "orders-semantics")
    )
    assert product is not None
    review = await submit_context_product_version(
        product.id, context=estate.steward, session=session
    )
    await _approve(estate, review.id)
    assert not (await _rebuild(estate)).acted

    # The project publishes a second model and the term a second definition.
    second_model, second_term = model(2, "PUBLISHED"), definition(2, "APPROVED")
    first_model.status, first_term.status = "SUPERSEDED", "SUPERSEDED"
    session.add_all([second_model, second_term])
    await session.commit()

    outcome = await _rebuild(estate)
    assert (outcome.products_drafted, outcome.blocked) == (1, {}), outcome.as_details()
    assert await _approve_rebuilt(estate, "CONTEXT_PRODUCT_VERSION") == 1
    current = await session.scalar(
        select(ContextProductVersion).where(
            ContextProductVersion.product_id == product.product_id,
            ContextProductVersion.status == "PUBLISHED",
        )
    )
    assert current is not None
    assert (current.semantic_model_version_ids, current.glossary_term_version_ids) == (
        [str(second_model.id)],
        [str(second_term.id)],
    )
    assert not (await _rebuild(estate)).acted
