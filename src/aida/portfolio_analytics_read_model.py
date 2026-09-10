"""Portfolio analytics read model: the aggregate queries behind one summary.

R02 named `product_marketplace_api.portfolio_analytics_summary` (407 lines) as
a hotspot whose useful boundary is "aggregate queries and portfolio read
model". This module is that read model. The router keeps exactly two jobs --
authorize the organization, and translate query parameters into
`PortfolioAnalyticsWindow` -- while every SQL aggregate and every derivation
from it lives here.

Two invariants shape the split, and each has exactly one implementation:

**What the window covers.** `PortfolioAnalyticsWindow.window_start` is the only
place the reporting window is computed, and it is applied to *activity* only
(access requests created, context/MCP consumption, agent runs, query and tool
executions). Inventory tallies -- how many products, versions, contracts and
context products exist in each state -- are current-state counts and are never
windowed. Grant liveness (`active_grants`,
`grants_expiring_within_30_days`) is evaluated against `now`, which is passed
in rather than read from the clock here so one summary reports one instant
across every section.

**What "published portfolio" means.** Quality averages, certification tallies
and `top_products` all read the same set: PUBLISHED versions of ACTIVE
products, loaded once by `_load_published_portfolio`. A product with no
`quality_score` counts as published but not as scored, so an average over
nothing is `None` rather than zero -- "no data" and "zero" are different
answers and this read model keeps them different.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AgentRun,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataContractVersion,
    DataProduct,
    DataProductAccessRequest,
    DataProductVersion,
    McpConsumptionEvidence,
    QueryExecution,
    ToolExecution,
)
from aida.platform_schemas import (
    PortfolioAccessRead,
    PortfolioAnalyticsSummaryRead,
    PortfolioLifecycleRead,
    PortfolioQualityRead,
    PortfolioQueueRead,
    PortfolioTopProductRead,
    PortfolioUsageRead,
)

#: How far ahead `grants_expiring_within_30_days` looks. Fixed by the field's
#: own name, so it is not a caller-tunable window.
GRANT_EXPIRY_HORIZON = timedelta(days=30)


def count_map(rows: Sequence[Any]) -> dict[str, int]:
    """Fold a `(group_key, count)` result set into a plain tally."""
    return {str(key): int(value) for key, value in rows}


def count_value(counts: dict[str, int], key: str) -> int:
    """Read one tally, treating an absent group as zero rather than missing."""
    return int(counts.get(key, 0))


@dataclass(frozen=True, slots=True)
class PortfolioAnalyticsWindow:
    """One summary's reporting parameters, resolved to a single instant.

    `now` is supplied by the caller rather than read here so every section of
    one response agrees on when "now" was.
    """

    organization_id: UUID
    now: datetime
    window_days: int
    low_quality_threshold: int
    top_products_limit: int

    @property
    def window_start(self) -> datetime:
        return self.now - timedelta(days=self.window_days)


@dataclass(frozen=True, slots=True)
class _InventoryTallies:
    """Current-state counts. Deliberately not windowed: a draft written last
    year is still a draft today."""

    product_lifecycle: dict[str, int]
    product_version: dict[str, int]
    contract_version: dict[str, int]
    context_product_total: int
    context_version: dict[str, int]


@dataclass(frozen=True, slots=True)
class _AccessTallies:
    """Access-request activity inside the window, plus grant liveness at `now`."""

    status: dict[str, int]
    fulfillment: dict[str, int]
    active_grants: int
    grants_expiring: int


@dataclass(frozen=True, slots=True)
class _PublishedPortfolio:
    """PUBLISHED versions of ACTIVE products -- the one definition of
    "the published portfolio" that quality and top_products both read."""

    rows: Sequence[tuple[DataProduct, DataProductVersion]]

    @property
    def scores(self) -> list[int]:
        return [
            version.quality_score for _, version in self.rows if version.quality_score is not None
        ]


async def _load_inventory(
    session: AsyncSession, window: PortfolioAnalyticsWindow
) -> _InventoryTallies:
    organization_id = window.organization_id
    product_lifecycle = count_map(
        (
            await session.execute(
                select(DataProduct.lifecycle_status, func.count())
                .where(DataProduct.organization_id == organization_id)
                .group_by(DataProduct.lifecycle_status)
            )
        ).all()
    )
    product_version = count_map(
        (
            await session.execute(
                select(DataProductVersion.status, func.count())
                .where(DataProductVersion.organization_id == organization_id)
                .group_by(DataProductVersion.status)
            )
        ).all()
    )
    contract_version = count_map(
        (
            await session.execute(
                select(DataContractVersion.status, func.count())
                .where(DataContractVersion.organization_id == organization_id)
                .group_by(DataContractVersion.status)
            )
        ).all()
    )
    context_product_total = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ContextProduct)
                .where(ContextProduct.organization_id == organization_id)
            )
        )
        or 0
    )
    context_version = count_map(
        (
            await session.execute(
                select(ContextProductVersion.status, func.count())
                .where(ContextProductVersion.organization_id == organization_id)
                .group_by(ContextProductVersion.status)
            )
        ).all()
    )
    return _InventoryTallies(
        product_lifecycle=product_lifecycle,
        product_version=product_version,
        contract_version=contract_version,
        context_product_total=context_product_total,
        context_version=context_version,
    )


def _lifecycle_read(inventory: _InventoryTallies) -> PortfolioLifecycleRead:
    return PortfolioLifecycleRead(
        data_products_total=sum(inventory.product_lifecycle.values()),
        data_products_active=count_value(inventory.product_lifecycle, "ACTIVE"),
        data_products_candidate=count_value(inventory.product_lifecycle, "CANDIDATE"),
        data_products_retired=count_value(inventory.product_lifecycle, "RETIRED"),
        data_product_versions_draft=count_value(inventory.product_version, "DRAFT"),
        data_product_versions_review_required=count_value(
            inventory.product_version, "REVIEW_REQUIRED"
        ),
        data_product_versions_published=count_value(inventory.product_version, "PUBLISHED"),
        data_product_versions_retired=count_value(inventory.product_version, "RETIRED"),
        data_contract_versions_draft=count_value(inventory.contract_version, "DRAFT"),
        data_contract_versions_review_required=count_value(
            inventory.contract_version, "REVIEW_REQUIRED"
        ),
        data_contract_versions_published=count_value(inventory.contract_version, "PUBLISHED"),
        context_products_total=inventory.context_product_total,
        context_product_versions_draft=count_value(inventory.context_version, "DRAFT"),
        context_product_versions_review_required=count_value(
            inventory.context_version, "REVIEW_REQUIRED"
        ),
        context_product_versions_published=count_value(inventory.context_version, "PUBLISHED"),
        context_product_versions_deprecated=count_value(inventory.context_version, "DEPRECATED"),
    )


async def _load_access(session: AsyncSession, window: PortfolioAnalyticsWindow) -> _AccessTallies:
    organization_id = window.organization_id
    window_start = window.window_start
    now = window.now
    status = count_map(
        (
            await session.execute(
                select(DataProductAccessRequest.status, func.count())
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.created_at >= window_start,
                )
                .group_by(DataProductAccessRequest.status)
            )
        ).all()
    )
    fulfillment = count_map(
        (
            await session.execute(
                select(DataProductAccessRequest.fulfillment_status, func.count())
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.created_at >= window_start,
                )
                .group_by(DataProductAccessRequest.fulfillment_status)
            )
        ).all()
    )
    # Grant liveness is a statement about `now`, not about the window: a grant
    # approved before the window opened is still an active grant today.
    active_grants = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(DataProductAccessRequest)
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.status == "APPROVED",
                    or_(
                        DataProductAccessRequest.expires_at.is_(None),
                        DataProductAccessRequest.expires_at > now,
                    ),
                )
            )
        )
        or 0
    )
    grants_expiring = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(DataProductAccessRequest)
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.status == "APPROVED",
                    DataProductAccessRequest.expires_at.is_not(None),
                    and_(
                        DataProductAccessRequest.expires_at >= now,
                        DataProductAccessRequest.expires_at <= now + GRANT_EXPIRY_HORIZON,
                    ),
                )
            )
        )
        or 0
    )
    return _AccessTallies(
        status=status,
        fulfillment=fulfillment,
        active_grants=active_grants,
        grants_expiring=grants_expiring,
    )


def _access_read(access: _AccessTallies) -> PortfolioAccessRead:
    return PortfolioAccessRead(
        requests_created=sum(access.status.values()),
        requests_pending=count_value(access.status, "PENDING"),
        requests_approved=count_value(access.status, "APPROVED"),
        requests_rejected=count_value(access.status, "REJECTED"),
        requests_revoked=count_value(access.status, "REVOKED"),
        requests_expired=count_value(access.status, "EXPIRED"),
        active_grants=access.active_grants,
        grants_expiring_within_30_days=access.grants_expiring,
        fulfillment_pending=count_value(access.fulfillment, "PENDING"),
        fulfillment_provisioned=count_value(access.fulfillment, "PROVISIONED"),
        fulfillment_failed=count_value(access.fulfillment, "FAILED"),
        fulfillment_revoked=count_value(access.fulfillment, "REVOKED"),
    )


async def _load_usage(
    session: AsyncSession, window: PortfolioAnalyticsWindow
) -> PortfolioUsageRead:
    """Consumption evidence inside the window, counted per channel.

    Every count here is windowed; the distinct-principal counts are distinct
    *within* the window too, so a consumer who was active only before it does
    not inflate reach.
    """
    organization_id = window.organization_id
    window_start = window.window_start
    context_product_reads = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ContextProductConsumptionEdge)
                .where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
            )
        )
        or 0
    )
    unique_context_consumers = int(
        (
            await session.scalar(
                select(func.count(func.distinct(ContextProductConsumptionEdge.principal_id))).where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
            )
        )
        or 0
    )
    mcp_operation_counts = count_map(
        (
            await session.execute(
                select(McpConsumptionEvidence.operation_kind, func.count())
                .where(
                    McpConsumptionEvidence.organization_id == organization_id,
                    McpConsumptionEvidence.consumed_at >= window_start,
                )
                .group_by(McpConsumptionEvidence.operation_kind)
            )
        ).all()
    )
    unique_mcp_consumers = int(
        (
            await session.scalar(
                select(func.count(func.distinct(McpConsumptionEvidence.principal_id))).where(
                    McpConsumptionEvidence.organization_id == organization_id,
                    McpConsumptionEvidence.consumed_at >= window_start,
                )
            )
        )
        or 0
    )
    agent_generation_counts = count_map(
        (
            await session.execute(
                select(AgentRun.generation_source, func.count())
                .where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.created_at >= window_start,
                )
                .group_by(AgentRun.generation_source)
            )
        ).all()
    )
    unique_agent_principals = int(
        (
            await session.scalar(
                select(func.count(func.distinct(AgentRun.principal_id))).where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.created_at >= window_start,
                )
            )
        )
        or 0
    )
    query_executions = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(QueryExecution)
                .where(
                    QueryExecution.organization_id == organization_id,
                    QueryExecution.created_at >= window_start,
                )
            )
        )
        or 0
    )
    governed_tool_executions = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(
                    ToolExecution.organization_id == organization_id,
                    ToolExecution.created_at >= window_start,
                )
            )
        )
        or 0
    )
    return PortfolioUsageRead(
        unique_context_consumers=unique_context_consumers,
        unique_mcp_consumers=unique_mcp_consumers,
        unique_agent_principals=unique_agent_principals,
        context_product_reads=context_product_reads,
        mcp_operations=sum(mcp_operation_counts.values()),
        mcp_resource_reads=count_value(mcp_operation_counts, "RESOURCE"),
        mcp_prompt_reads=count_value(mcp_operation_counts, "PROMPT"),
        mcp_tool_calls=count_value(mcp_operation_counts, "TOOL"),
        mcp_control_operations=count_value(mcp_operation_counts, "CONTROL"),
        agent_runs=sum(agent_generation_counts.values()),
        governed_tool_agent_runs=count_value(agent_generation_counts, "GOVERNED_TOOL"),
        model_gateway_agent_runs=count_value(agent_generation_counts, "MODEL_GATEWAY"),
        development_override_agent_runs=count_value(
            agent_generation_counts, "DEVELOPMENT_OVERRIDE"
        ),
        policy_blocked_agent_runs=count_value(agent_generation_counts, "POLICY_BLOCK"),
        query_executions=query_executions,
        governed_tool_executions=governed_tool_executions,
    )


async def _load_published_portfolio(
    session: AsyncSession, window: PortfolioAnalyticsWindow
) -> _PublishedPortfolio:
    rows = (
        await session.execute(
            select(DataProduct, DataProductVersion)
            .join(DataProductVersion, DataProductVersion.product_id == DataProduct.id)
            .where(
                DataProduct.organization_id == window.organization_id,
                DataProduct.lifecycle_status == "ACTIVE",
                DataProductVersion.status == "PUBLISHED",
            )
            .order_by(DataProduct.product_key)
        )
    ).all()
    return _PublishedPortfolio(rows=[(product, version) for product, version in rows])


def _quality_read(
    portfolio: _PublishedPortfolio, window: PortfolioAnalyticsWindow
) -> PortfolioQualityRead:
    scores = portfolio.scores
    return PortfolioQualityRead(
        published_products=len(portfolio.rows),
        scored_products=len(scores),
        # An average over an empty set is None, not 0 -- an unscored portfolio
        # is not a badly scored one.
        average_quality_score=round(sum(scores) / len(scores), 2) if scores else None,
        low_quality_products=sum(
            1 for score in scores if int(score) < window.low_quality_threshold
        ),
        certified_products=sum(
            1 for _, version in portfolio.rows if version.certification_status == "CERTIFIED"
        ),
        uncertified_products=sum(
            1 for _, version in portfolio.rows if version.certification_status != "CERTIFIED"
        ),
        average_lineage_coverage=(
            round(
                sum(version.lineage_coverage for _, version in portfolio.rows)
                / len(portfolio.rows),
                2,
            )
            if portfolio.rows
            else None
        ),
    )


async def _load_top_products(
    session: AsyncSession, window: PortfolioAnalyticsWindow, portfolio: _PublishedPortfolio
) -> list[PortfolioTopProductRead]:
    """Rank the published portfolio by demand inside the window.

    Ordering is total demand (access requests + context reads) descending, then
    approvals, then quality score, then `product_key` ascending -- the last term
    makes the order total, so equally-quiet products do not shuffle between
    requests.
    """
    organization_id = window.organization_id
    window_start = window.window_start
    access_by_version = {
        version_id: {"requests": int(total), "approved": int(approved)}
        for version_id, total, approved in (
            await session.execute(
                select(
                    DataProductAccessRequest.data_product_version_id,
                    func.count(),
                    func.count().filter(DataProductAccessRequest.status == "APPROVED"),
                )
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.created_at >= window_start,
                )
                .group_by(DataProductAccessRequest.data_product_version_id)
            )
        ).all()
    }
    context_reads_by_version = {
        version_id: int(total)
        for version_id, total in (
            await session.execute(
                select(
                    ContextProductConsumptionEdge.context_product_version_id,
                    func.count(),
                )
                .where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
                .group_by(ContextProductConsumptionEdge.context_product_version_id)
            )
        ).all()
    }
    return sorted(
        [
            PortfolioTopProductRead(
                data_product_version_id=version.id,
                product_key=product.product_key,
                name=version.name,
                domain_name=version.domain_name,
                certification_status=version.certification_status,
                quality_score=version.quality_score,
                lineage_coverage=version.lineage_coverage,
                access_request_count=access_by_version.get(version.id, {}).get("requests", 0),
                approved_access_count=access_by_version.get(version.id, {}).get("approved", 0),
                context_read_count=context_reads_by_version.get(
                    version.context_product_version_id, 0
                )
                if version.context_product_version_id is not None
                else 0,
            )
            for product, version in portfolio.rows
        ],
        key=lambda item: (
            -(item.access_request_count + item.context_read_count),
            -item.approved_access_count,
            -(item.quality_score or -1),
            item.product_key,
        ),
    )[: window.top_products_limit]


def _queue_read(inventory: _InventoryTallies, access: _AccessTallies) -> PortfolioQueueRead:
    """Restates review-required tallies as a work queue.

    Deliberately derived from the same tallies the lifecycle block reports
    rather than re-queried: "how many versions await review" has one
    authoritative count, reported in two shapes.
    """
    return PortfolioQueueRead(
        review_required_data_product_versions=count_value(
            inventory.product_version, "REVIEW_REQUIRED"
        ),
        review_required_data_contract_versions=count_value(
            inventory.contract_version, "REVIEW_REQUIRED"
        ),
        review_required_context_product_versions=count_value(
            inventory.context_version, "REVIEW_REQUIRED"
        ),
        pending_marketplace_access_requests=count_value(access.status, "PENDING"),
    )


async def build_portfolio_analytics_summary(
    session: AsyncSession, window: PortfolioAnalyticsWindow
) -> PortfolioAnalyticsSummaryRead:
    """Compose one organization's portfolio summary.

    Every section is scoped to `window.organization_id`; nothing here decides
    whether the caller may see that organization, which stays the router's job.
    """
    inventory = await _load_inventory(session, window)
    access = await _load_access(session, window)
    usage = await _load_usage(session, window)
    portfolio = await _load_published_portfolio(session, window)
    top_products = await _load_top_products(session, window, portfolio)
    return PortfolioAnalyticsSummaryRead(
        generated_at=window.now,
        window_days=window.window_days,
        low_quality_threshold=window.low_quality_threshold,
        lifecycle=_lifecycle_read(inventory),
        access=_access_read(access),
        usage=usage,
        quality=_quality_read(portfolio, window),
        queues=_queue_read(inventory, access),
        top_products=top_products,
    )
