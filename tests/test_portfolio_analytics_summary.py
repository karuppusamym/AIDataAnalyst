"""Characterization suite for the portfolio analytics summary read model.

R02 names `product_marketplace_api.portfolio_analytics_summary` as a hotspot.
Before this file the endpoint's only coverage was
`tests/test_agentic_platform.py::test_agentic_platform_contracts_are_published`
asserting its OpenAPI path exists -- nothing pinned a single number it returns.

Every assertion below is on the response object, so the suite stays valid across
the extraction into `aida.portfolio_analytics_read_model`. The behaviours that
matter and are easy to break by accident:

* the reporting window applies to *activity* counts (access requests,
  consumption, agent runs, executions) but never to *inventory* counts
  (lifecycle, contract and context-product status tallies);
* `active_grants` and `grants_expiring_within_30_days` are evaluated against
  `now`, independently of the window;
* averages are computed over PUBLISHED versions of ACTIVE products only, with
  `quality_score=None` excluded from the quality average but still counted as a
  published product;
* `top_products` ordering is total-demand desc, approved desc, quality desc,
  then product_key asc, truncated to `top_products_limit`;
* the organization filter is absolute -- another organization's rows never
  contribute to any field.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
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
from aida.product_marketplace_api import portfolio_analytics_summary
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)
INSIDE_WINDOW = NOW - timedelta(days=3)
OUTSIDE_WINDOW = NOW - timedelta(days=120)


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as session:
        yield session
    await engine.dispose()


def _context(organization_id: UUID, *, role: str = "Operations") -> SecurityContext:
    return SecurityContext(
        principal_id="operator@example.com",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({role}),
    )


def _product(organization_id: UUID, key: str, *, lifecycle: str) -> DataProduct:
    return DataProduct(
        id=uuid4(),
        organization_id=organization_id,
        project_id=uuid4(),
        product_key=key,
        lifecycle_status=lifecycle,
        created_by="owner@example.com",
    )


def _version(
    product: DataProduct,
    *,
    status: str,
    version: int = 1,
    quality_score: int | None = None,
    lineage_coverage: int = 0,
    certification_status: str = "UNCERTIFIED",
    context_product_version_id: UUID | None = None,
) -> DataProductVersion:
    return DataProductVersion(
        id=uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version=version,
        status=status,
        name=f"{product.product_key} v{version}",
        description="Characterization fixture.",
        domain_name="Customer",
        owner_principal="owner@example.com",
        usage_terms="Approved use only.",
        classification="INTERNAL",
        certification_status=certification_status,
        quality_score=quality_score,
        lineage_coverage=lineage_coverage,
        context_product_version_id=context_product_version_id,
        discoverable_roles=["Analyst"],
        consumer_roles=["DataConsumer"],
        fingerprint=f"fp-{product.product_key}",
        created_by="owner@example.com",
    )


def _access_request(
    version: DataProductVersion,
    *,
    status: str,
    created_at: datetime,
    requested_by: str | None = None,
    fulfillment_status: str = "PENDING",
    expires_at: datetime | None = None,
) -> DataProductAccessRequest:
    return DataProductAccessRequest(
        id=uuid4(),
        organization_id=version.organization_id,
        data_product_version_id=version.id,
        requested_by=requested_by or f"analyst-{uuid4().hex[:8]}@example.com",
        purpose="Characterization fixture.",
        duration_days=30,
        status=status,
        governance_review_id=uuid4(),
        fulfillment_status=fulfillment_status,
        expires_at=expires_at,
        created_at=created_at,
        updated_at=created_at,
    )


def _context_version(
    organization_id: UUID, *, status: str
) -> tuple[ContextProduct, ContextProductVersion]:
    product = ContextProduct(
        id=uuid4(),
        organization_id=organization_id,
        project_id=uuid4(),
        product_key=f"ctx-{uuid4().hex[:8]}",
        lifecycle_status="ACTIVE",
        created_by="owner@example.com",
    )
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=organization_id,
        product_id=product.id,
        version=1,
        status=status,
        name="Context product",
        description="Characterization fixture.",
        purpose="Support bounded characterization.",
        owner_principal="owner@example.com",
        table_ids=[],
        allowed_consumer_roles=["Analyst"],
        fingerprint=f"fp-{uuid4().hex[:8]}",
        created_by="owner@example.com",
    )
    return product, version


def _contract(product: DataProduct, *, status: str) -> DataContractVersion:
    return DataContractVersion(
        id=uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version=1,
        status=status,
        compatibility_mode="FULL",
        compatibility_status="COMPATIBLE",
        schema_definition=[],
        producer_principal="owner@example.com",
        fingerprint=f"fp-contract-{uuid4().hex[:8]}",
        created_by="owner@example.com",
    )


def _consumption(version_id: UUID, organization_id: UUID, *, principal: str, at: datetime):
    return ContextProductConsumptionEdge(
        id=uuid4(),
        organization_id=organization_id,
        context_product_version_id=version_id,
        principal_id=principal,
        principal_type="USER",
        channel="MCP",
        correlation_id=uuid4().hex,
        product_fingerprint="fp",
        policy_decision="ALLOW",
        consumed_at=at,
    )


def _mcp(organization_id: UUID, *, kind: str, principal: str, at: datetime):
    return McpConsumptionEvidence(
        id=uuid4(),
        organization_id=organization_id,
        principal_id=principal,
        principal_type="USER",
        operation_kind=kind,
        method="tools/call",
        correlation_id=uuid4().hex,
        policy_decision="ALLOW",
        consumed_at=at,
    )


def _agent_run(organization_id: UUID, *, source: str, principal: str, at: datetime):
    return AgentRun(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        principal_id=principal,
        status="COMPLETED",
        question_hash=uuid4().hex,
        generation_source=source,
        created_at=at,
        updated_at=at,
    )


def _query_execution(organization_id: UUID, *, at: datetime):
    return QueryExecution(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        principal_id="analyst@example.com",
        status="SUCCEEDED",
        dialect="postgres",
        sql_hash=uuid4().hex,
        created_at=at,
        updated_at=at,
    )


def _tool_execution(organization_id: UUID, *, at: datetime):
    return ToolExecution(
        id=uuid4(),
        organization_id=organization_id,
        tool_version_id=uuid4(),
        principal_id="analyst@example.com",
        parameter_fingerprint=uuid4().hex,
        status="SUCCEEDED",
        created_at=at,
        updated_at=at,
    )


async def _summary(session: AsyncSession, organization_id: UUID, **overrides):
    kwargs = dict(window_days=30, low_quality_threshold=80, top_products_limit=10)
    kwargs.update(overrides)
    return await portfolio_analytics_summary(
        organization_id,
        context=_context(organization_id),
        session=session,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Empty organization -- every field must be present and zero-valued
# ---------------------------------------------------------------------------


async def test_summary_of_an_empty_organization_is_all_zeros(db_session) -> None:
    organization_id = uuid4()

    result = await _summary(db_session, organization_id)

    assert result.window_days == 30
    assert result.low_quality_threshold == 80
    assert result.lifecycle.data_products_total == 0
    assert result.lifecycle.context_products_total == 0
    assert result.access.requests_created == 0
    assert result.access.active_grants == 0
    assert result.access.grants_expiring_within_30_days == 0
    assert result.usage.mcp_operations == 0
    assert result.usage.agent_runs == 0
    assert result.usage.query_executions == 0
    assert result.usage.governed_tool_executions == 0
    assert result.quality.published_products == 0
    assert result.quality.scored_products == 0
    # An average over nothing is None, not 0 -- "no data" and "zero" differ.
    assert result.quality.average_quality_score is None
    assert result.quality.average_lineage_coverage is None
    assert result.queues.pending_marketplace_access_requests == 0
    assert result.top_products == []


# ---------------------------------------------------------------------------
# Inventory counts ignore the window; activity counts respect it
# ---------------------------------------------------------------------------


async def test_lifecycle_and_queue_counts_are_inventory_not_windowed(db_session) -> None:
    organization_id = uuid4()
    active = _product(organization_id, "active_product", lifecycle="ACTIVE")
    candidate = _product(organization_id, "candidate_product", lifecycle="CANDIDATE")
    retired = _product(organization_id, "retired_product", lifecycle="RETIRED")
    db_session.add_all([active, candidate, retired])
    db_session.add_all(
        [
            _version(active, status="PUBLISHED", quality_score=90),
            _version(candidate, status="DRAFT"),
            _version(retired, status="REVIEW_REQUIRED"),
        ]
    )
    db_session.add_all(
        [_contract(active, status="PUBLISHED"), _contract(candidate, status="REVIEW_REQUIRED")]
    )
    ctx_product, ctx_version = _context_version(organization_id, status="REVIEW_REQUIRED")
    db_session.add_all([ctx_product, ctx_version])
    await db_session.flush()

    result = await _summary(db_session, organization_id, window_days=1)

    assert result.lifecycle.data_products_total == 3
    assert result.lifecycle.data_products_active == 1
    assert result.lifecycle.data_products_candidate == 1
    assert result.lifecycle.data_products_retired == 1
    assert result.lifecycle.data_product_versions_published == 1
    assert result.lifecycle.data_product_versions_draft == 1
    assert result.lifecycle.data_product_versions_review_required == 1
    assert result.lifecycle.data_contract_versions_published == 1
    assert result.lifecycle.data_contract_versions_review_required == 1
    assert result.lifecycle.context_products_total == 1
    assert result.lifecycle.context_product_versions_review_required == 1
    # The queue block restates the same review-required tallies -- one
    # authoritative count each, reported twice.
    assert result.queues.review_required_data_product_versions == 1
    assert result.queues.review_required_data_contract_versions == 1
    assert result.queues.review_required_context_product_versions == 1


async def test_access_activity_is_confined_to_the_window(db_session) -> None:
    organization_id = uuid4()
    product = _product(organization_id, "product", lifecycle="ACTIVE")
    version = _version(product, status="PUBLISHED", quality_score=90)
    db_session.add_all([product, version])
    db_session.add_all(
        [
            _access_request(version, status="PENDING", created_at=INSIDE_WINDOW),
            _access_request(
                version,
                status="APPROVED",
                created_at=INSIDE_WINDOW,
                fulfillment_status="PROVISIONED",
            ),
            _access_request(version, status="REJECTED", created_at=OUTSIDE_WINDOW),
        ]
    )
    await db_session.flush()

    result = await _summary(db_session, organization_id, window_days=30)

    assert result.access.requests_created == 2
    assert result.access.requests_pending == 1
    assert result.access.requests_approved == 1
    assert result.access.requests_rejected == 0
    assert result.access.fulfillment_pending == 1
    assert result.access.fulfillment_provisioned == 1
    assert result.queues.pending_marketplace_access_requests == 1


async def test_active_and_expiring_grants_are_evaluated_against_now_not_the_window(
    db_session,
) -> None:
    organization_id = uuid4()
    product = _product(organization_id, "product", lifecycle="ACTIVE")
    version = _version(product, status="PUBLISHED", quality_score=90)
    db_session.add_all([product, version])
    db_session.add_all(
        [
            # Approved long before the window, never expiring: still an active grant.
            _access_request(version, status="APPROVED", created_at=OUTSIDE_WINDOW, expires_at=None),
            # Approved inside the window, expiring in 10 days: active AND expiring.
            _access_request(
                version,
                status="APPROVED",
                created_at=INSIDE_WINDOW,
                expires_at=NOW + timedelta(days=10),
            ),
            # Already expired: neither active nor expiring.
            _access_request(
                version,
                status="APPROVED",
                created_at=INSIDE_WINDOW,
                expires_at=NOW - timedelta(days=1),
            ),
            # Expiring beyond the fixed 30-day horizon: active, not expiring.
            _access_request(
                version,
                status="APPROVED",
                created_at=INSIDE_WINDOW,
                expires_at=NOW + timedelta(days=90),
            ),
            # Not approved at all.
            _access_request(
                version,
                status="PENDING",
                created_at=INSIDE_WINDOW,
                expires_at=NOW + timedelta(days=5),
            ),
        ]
    )
    await db_session.flush()

    result = await _summary(db_session, organization_id, window_days=1)

    assert result.access.active_grants == 3
    assert result.access.grants_expiring_within_30_days == 1


async def test_usage_counts_distinct_principals_within_the_window(db_session) -> None:
    organization_id = uuid4()
    ctx_product, ctx_version = _context_version(organization_id, status="PUBLISHED")
    db_session.add_all([ctx_product, ctx_version])
    await db_session.flush()
    db_session.add_all(
        [
            _consumption(ctx_version.id, organization_id, principal="a", at=INSIDE_WINDOW),
            _consumption(ctx_version.id, organization_id, principal="a", at=INSIDE_WINDOW),
            _consumption(ctx_version.id, organization_id, principal="b", at=INSIDE_WINDOW),
            _consumption(ctx_version.id, organization_id, principal="c", at=OUTSIDE_WINDOW),
            _mcp(organization_id, kind="TOOL", principal="a", at=INSIDE_WINDOW),
            _mcp(organization_id, kind="TOOL", principal="b", at=INSIDE_WINDOW),
            _mcp(organization_id, kind="RESOURCE", principal="a", at=INSIDE_WINDOW),
            _mcp(organization_id, kind="PROMPT", principal="a", at=INSIDE_WINDOW),
            _mcp(organization_id, kind="CONTROL", principal="a", at=INSIDE_WINDOW),
            _mcp(organization_id, kind="TOOL", principal="z", at=OUTSIDE_WINDOW),
            _agent_run(organization_id, source="GOVERNED_TOOL", principal="a", at=INSIDE_WINDOW),
            _agent_run(organization_id, source="MODEL_GATEWAY", principal="b", at=INSIDE_WINDOW),
            _agent_run(
                organization_id,
                source="DEVELOPMENT_OVERRIDE",
                principal="b",
                at=INSIDE_WINDOW,
            ),
            _agent_run(organization_id, source="POLICY_BLOCK", principal="b", at=INSIDE_WINDOW),
            _agent_run(organization_id, source="GOVERNED_TOOL", principal="z", at=OUTSIDE_WINDOW),
            _query_execution(organization_id, at=INSIDE_WINDOW),
            _query_execution(organization_id, at=OUTSIDE_WINDOW),
            _tool_execution(organization_id, at=INSIDE_WINDOW),
            _tool_execution(organization_id, at=INSIDE_WINDOW),
            _tool_execution(organization_id, at=OUTSIDE_WINDOW),
        ]
    )
    await db_session.flush()

    result = await _summary(db_session, organization_id, window_days=30)

    assert result.usage.context_product_reads == 3
    assert result.usage.unique_context_consumers == 2
    assert result.usage.mcp_operations == 5
    assert result.usage.mcp_tool_calls == 2
    assert result.usage.mcp_resource_reads == 1
    assert result.usage.mcp_prompt_reads == 1
    assert result.usage.mcp_control_operations == 1
    assert result.usage.unique_mcp_consumers == 2
    assert result.usage.agent_runs == 4
    assert result.usage.governed_tool_agent_runs == 1
    assert result.usage.model_gateway_agent_runs == 1
    assert result.usage.development_override_agent_runs == 1
    assert result.usage.policy_blocked_agent_runs == 1
    assert result.usage.unique_agent_principals == 2
    assert result.usage.query_executions == 1
    assert result.usage.governed_tool_executions == 2


# ---------------------------------------------------------------------------
# Quality aggregates
# ---------------------------------------------------------------------------


async def test_quality_averages_cover_published_versions_of_active_products_only(
    db_session,
) -> None:
    organization_id = uuid4()
    certified = _product(organization_id, "certified_product", lifecycle="ACTIVE")
    scoreless = _product(organization_id, "scoreless_product", lifecycle="ACTIVE")
    low = _product(organization_id, "low_product", lifecycle="ACTIVE")
    draft_only = _product(organization_id, "draft_product", lifecycle="ACTIVE")
    retired = _product(organization_id, "retired_product", lifecycle="RETIRED")
    db_session.add_all([certified, scoreless, low, draft_only, retired])
    db_session.add_all(
        [
            _version(
                certified,
                status="PUBLISHED",
                quality_score=90,
                lineage_coverage=60,
                certification_status="CERTIFIED",
            ),
            # Scoreless: a published product, but not a scored one.
            _version(scoreless, status="PUBLISHED", quality_score=None, lineage_coverage=40),
            # Below threshold.
            _version(low, status="PUBLISHED", quality_score=71, lineage_coverage=20),
            # Not published: excluded entirely.
            _version(draft_only, status="DRAFT", quality_score=10, lineage_coverage=0),
            # Published, but the product is retired: excluded entirely.
            _version(retired, status="PUBLISHED", quality_score=10, lineage_coverage=0),
        ]
    )
    await db_session.flush()

    result = await _summary(db_session, organization_id, low_quality_threshold=80)

    assert result.quality.published_products == 3
    assert result.quality.scored_products == 2
    assert result.quality.average_quality_score == 80.5
    # Lineage coverage averages over every published version, scored or not.
    assert result.quality.average_lineage_coverage == 40.0
    assert result.quality.low_quality_products == 1
    assert result.quality.certified_products == 1
    assert result.quality.uncertified_products == 2


async def test_low_quality_threshold_is_a_strict_less_than(db_session) -> None:
    organization_id = uuid4()
    at_threshold = _product(organization_id, "at_threshold", lifecycle="ACTIVE")
    below_threshold = _product(organization_id, "below_threshold", lifecycle="ACTIVE")
    db_session.add_all([at_threshold, below_threshold])
    db_session.add_all(
        [
            _version(at_threshold, status="PUBLISHED", quality_score=80),
            _version(below_threshold, status="PUBLISHED", quality_score=79),
        ]
    )
    await db_session.flush()

    result = await _summary(db_session, organization_id, low_quality_threshold=80)

    assert result.quality.low_quality_products == 1


# ---------------------------------------------------------------------------
# Top products
# ---------------------------------------------------------------------------


async def test_top_products_rank_by_demand_then_approvals_then_quality_then_key(
    db_session,
) -> None:
    organization_id = uuid4()
    ctx_product, ctx_version = _context_version(organization_id, status="PUBLISHED")
    db_session.add_all([ctx_product, ctx_version])
    await db_session.flush()

    product = _product(organization_id, "aaa_product", lifecycle="ACTIVE")
    busy = _product(organization_id, "bbb_busy", lifecycle="ACTIVE")
    read_only = _product(organization_id, "ccc_reads", lifecycle="ACTIVE")
    db_session.add_all([product, busy, read_only])
    quiet_version = _version(product, status="PUBLISHED", quality_score=99)
    busy_version = _version(busy, status="PUBLISHED", quality_score=10)
    reads_version = _version(
        read_only,
        status="PUBLISHED",
        quality_score=50,
        context_product_version_id=ctx_version.id,
    )
    db_session.add_all([quiet_version, busy_version, reads_version])
    db_session.add_all(
        [
            _access_request(busy_version, status="APPROVED", created_at=INSIDE_WINDOW),
            _access_request(busy_version, status="PENDING", created_at=INSIDE_WINDOW),
            # Outside the window: contributes to no product's demand.
            _access_request(quiet_version, status="APPROVED", created_at=OUTSIDE_WINDOW),
            _consumption(ctx_version.id, organization_id, principal="a", at=INSIDE_WINDOW),
        ]
    )
    await db_session.flush()

    result = await _summary(db_session, organization_id)

    assert [row.product_key for row in result.top_products] == [
        "bbb_busy",  # 2 requests + 0 reads
        "ccc_reads",  # 0 requests + 1 read
        "aaa_product",  # nothing inside the window
    ]
    busy_row = result.top_products[0]
    assert busy_row.access_request_count == 2
    assert busy_row.approved_access_count == 1
    assert busy_row.context_read_count == 0
    assert result.top_products[1].context_read_count == 1
    assert result.top_products[2].access_request_count == 0


async def test_top_products_respect_the_requested_limit(db_session) -> None:
    organization_id = uuid4()
    for index in range(5):
        product = _product(organization_id, f"product_{index}", lifecycle="ACTIVE")
        db_session.add(product)
        db_session.add(_version(product, status="PUBLISHED", quality_score=50))
    await db_session.flush()

    result = await _summary(db_session, organization_id, top_products_limit=2)

    assert len(result.top_products) == 2
    # `published_products` reports the honest total, not the truncated list.
    assert result.quality.published_products == 5


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_another_organizations_rows_never_contribute(db_session) -> None:
    organization_id = uuid4()
    other_organization_id = uuid4()
    for owner in (organization_id, other_organization_id):
        product = _product(owner, "shared_key", lifecycle="ACTIVE")
        version = _version(product, status="PUBLISHED", quality_score=90)
        db_session.add_all([product, version])
        db_session.add(_access_request(version, status="PENDING", created_at=INSIDE_WINDOW))
        db_session.add(_mcp(owner, kind="TOOL", principal="a", at=INSIDE_WINDOW))
        db_session.add(_agent_run(owner, source="GOVERNED_TOOL", principal="a", at=INSIDE_WINDOW))
        db_session.add(_query_execution(owner, at=INSIDE_WINDOW))
        db_session.add(_tool_execution(owner, at=INSIDE_WINDOW))
    await db_session.flush()

    result = await _summary(db_session, organization_id)

    assert result.lifecycle.data_products_total == 1
    assert result.access.requests_created == 1
    assert result.usage.mcp_operations == 1
    assert result.usage.agent_runs == 1
    assert result.usage.query_executions == 1
    assert result.usage.governed_tool_executions == 1
    assert result.quality.published_products == 1
    assert len(result.top_products) == 1


async def test_summary_refuses_a_caller_from_another_organization(db_session) -> None:
    organization_id = uuid4()

    with pytest.raises(HTTPException) as excinfo:
        await portfolio_analytics_summary(
            organization_id,
            window_days=30,
            low_quality_threshold=80,
            top_products_limit=10,
            context=_context(uuid4()),
            session=db_session,
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "cross-organization access denied"


async def test_platform_admin_is_exempt_from_the_organization_check(db_session) -> None:
    """`enforce_organization` returns early for PlatformAdmin. That is the
    platform-wide operator escape hatch, not an accident of this endpoint --
    pinned here so an extraction cannot quietly change which principals it
    applies to."""
    organization_id = uuid4()
    product = _product(organization_id, "product", lifecycle="ACTIVE")
    db_session.add(product)
    db_session.add(_version(product, status="PUBLISHED", quality_score=90))
    await db_session.flush()

    result = await portfolio_analytics_summary(
        organization_id,
        window_days=30,
        low_quality_threshold=80,
        top_products_limit=10,
        context=_context(uuid4(), role="PlatformAdmin"),
        session=db_session,
    )

    assert result.lifecycle.data_products_total == 1
