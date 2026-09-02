"""AT-12 -- semantic mining of warehouse query history.

Three layers, matching the tracker exit criterion:

1. Pure structure extraction and frequency mining
   (`test_extract_query_structure_*`, `test_mine_join_pair_frequencies_*`,
   `test_mine_metric_shape_frequencies_*`): value-free (no literal from a
   `WHERE`/filter predicate ever appears in the extracted structure or its
   string representation -- `test_extract_query_structure_never_captures_literal_values`),
   bounded (`min_occurrences` drops noise, `max_candidates` caps the ranked
   survivors -- `test_mine_join_pair_frequencies_is_bounded_and_ranked`), and
   the AT-C2 lane-3 confidence cap holds regardless of occurrence count
   (`test_lane3_confidence_never_exceeds_the_cap`).
2. Landing candidates in the real review queue against real seeded data
   (`test_mine_and_land_query_history_candidates_*`): a recurring join shape
   lands as a `PENDING` `RelationshipCandidate` (the existing maker-checker
   queue, reused unmodified -- only a new `detection_rule`); a recurring
   metric shape lands as a `PENDING` `QueryHistoryMetricCandidate` with an
   open `PENDING` `GovernanceReview`. Neither is ever auto-approved
   (`test_mined_candidates_are_never_auto_approved`), and re-mining the same
   log does not create duplicates (`test_mining_is_idempotent_against_the_same_log`).
3. The review gate itself, dispatched exactly the way AT-11's
   `COLUMN_CLASSIFICATION_PROMOTION` is: an independent APPROVE publishes a
   real `SemanticMetric` + `SemanticMetricVersion`
   (`test_approving_a_metric_candidate_publishes_a_real_metric`); REJECT
   publishes nothing and retains the candidate as negative evidence
   (`test_rejecting_a_metric_candidate_publishes_nothing`); deciding an
   already-decided candidate conflicts
   (`test_deciding_an_already_decided_candidate_conflicts`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QueryHistoryMetricCandidate,
    RelationshipCandidate,
    SemanticMetricVersion,
)
from aida.query_history_miner import (
    QUERY_HISTORY_CONFIDENCE_CAP,
    AggregateMeasure,
    ColumnRef,
    JoinColumnPair,
    QueryStructure,
    WarehouseQueryLogEntry,
    _lane3_confidence,
    apply_query_history_metric_candidate_decision,
    extract_query_structure,
    grain_fingerprint,
    mine_and_land_query_history_candidates,
    mine_join_pair_frequencies,
    mine_metric_shape_frequencies,
)
from aida.semantic_api import _apply_governance_review_decision
from tests.support.doubles import security_context

_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

_JOIN_SQL = """
SELECT o.customer_id, SUM(o.amount) AS total
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.status = 'COMPLETED' AND o.amount > 100
GROUP BY o.customer_id
"""


# ---------------------------------------------------------------------------
# 1. Pure structure extraction and frequency mining
# ---------------------------------------------------------------------------


def test_extract_query_structure_never_captures_literal_values() -> None:
    entry = WarehouseQueryLogEntry(query_id="q1", sql_text=_JOIN_SQL)
    structure = extract_query_structure(entry, dialect="postgres")
    assert structure is not None
    rendered = repr(structure)
    assert "COMPLETED" not in rendered
    assert "100" not in rendered
    assert structure.join_pairs == (
        JoinColumnPair("customers", "id", "orders", "customer_id"),
    )
    assert structure.group_by_columns == (ColumnRef("orders", "customer_id"),)
    assert structure.aggregate_measures == (AggregateMeasure("SUM", "orders", "amount"),)
    assert ColumnRef("orders", "status") in structure.filter_columns
    assert ColumnRef("orders", "amount") in structure.filter_columns


def test_extract_query_structure_degrades_gracefully_on_unparseable_sql() -> None:
    entry = WarehouseQueryLogEntry(query_id="bad", sql_text="SELEKT !!! not valid")
    assert extract_query_structure(entry) is None


def test_extract_query_structure_handles_no_join_no_group_by() -> None:
    entry = WarehouseQueryLogEntry(query_id="simple", sql_text="SELECT id FROM customers")
    structure = extract_query_structure(entry)
    assert structure is not None
    assert structure.referenced_tables == frozenset({"customers"})
    assert structure.join_pairs == ()
    assert structure.aggregate_measures == ()


def test_join_pair_normalized_is_order_independent() -> None:
    forward = JoinColumnPair("orders", "customer_id", "customers", "id")
    reverse = JoinColumnPair("customers", "id", "orders", "customer_id")
    assert forward.normalized() == reverse.normalized()


def test_mine_join_pair_frequencies_is_bounded_and_ranked() -> None:
    frequent_pair = JoinColumnPair("orders", "customer_id", "customers", "id")
    rare_pair = JoinColumnPair("orders", "product_id", "products", "id")
    structures = [
        QueryStructure(f"q{i}", frozenset(), join_pairs=(frequent_pair,)) for i in range(5)
    ] + [QueryStructure("q-rare", frozenset(), join_pairs=(rare_pair,))]

    survivors = mine_join_pair_frequencies(structures, min_occurrences=3, max_candidates=10)
    assert len(survivors) == 1
    assert survivors[0].pair == frequent_pair
    assert survivors[0].occurrence_count == 5

    capped = mine_join_pair_frequencies(structures, min_occurrences=1, max_candidates=1)
    assert len(capped) == 1
    assert capped[0].occurrence_count == 5, "the higher-occurrence pair must rank first"


def test_mine_metric_shape_frequencies_groups_by_measure_and_grain() -> None:
    measure = AggregateMeasure("SUM", "orders", "amount")
    grain = (ColumnRef("orders", "customer_id"),)
    structures = [
        QueryStructure(
            f"q{i}", frozenset(), aggregate_measures=(measure,), group_by_columns=grain
        )
        for i in range(4)
    ]
    survivors = mine_metric_shape_frequencies(structures, min_occurrences=3)
    assert len(survivors) == 1
    assert survivors[0].measure == measure
    assert survivors[0].grain == grain
    assert survivors[0].occurrence_count == 4


def test_grain_fingerprint_is_order_independent() -> None:
    a = (ColumnRef("orders", "customer_id"), ColumnRef("orders", "region"))
    b = (ColumnRef("orders", "region"), ColumnRef("orders", "customer_id"))
    assert grain_fingerprint(a) == grain_fingerprint(b)


def test_lane3_confidence_never_exceeds_the_cap() -> None:
    for occurrences in (0, 1, 5, 20, 1000, 1_000_000):
        assert _lane3_confidence(occurrences) <= QUERY_HISTORY_CONFIDENCE_CAP
    assert _lane3_confidence(1_000_000) == QUERY_HISTORY_CONFIDENCE_CAP


# ---------------------------------------------------------------------------
# 2 & 3. Landing candidates against real seeded data, and the review gate
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()
        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Commerce", code="COMMERCE"
        )
        db.add(self.lob)
        await db.flush()
        self.data_domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Orders",
            code="ORDERS",
        )
        db.add(self.data_domain)
        await db.flush()
        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.data_domain.id,
            name="Core Commerce",
            slug="core-commerce",
        )
        db.add(self.project)
        await db.flush()
        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.data_domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="warehouse",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()
        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="public",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.orders_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="orders",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-orders",
        )
        self.customers_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="customers",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-customers",
        )
        db.add_all([self.orders_table, self.customers_table])
        await db.flush()

        self.orders_customer_id = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.orders_table.id,
            name="customer_id",
            ordinal_position=1,
            physical_type="INTEGER",
            nullable=False,
            fingerprint="fp-orders-customer-id",
        )
        self.orders_amount = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.orders_table.id,
            name="amount",
            ordinal_position=2,
            physical_type="NUMERIC",
            nullable=False,
            fingerprint="fp-orders-amount",
        )
        self.orders_status = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.orders_table.id,
            name="status",
            ordinal_position=3,
            physical_type="TEXT",
            nullable=False,
            fingerprint="fp-orders-status",
        )
        self.customers_id = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.customers_table.id,
            name="id",
            ordinal_position=1,
            physical_type="INTEGER",
            nullable=False,
            fingerprint="fp-customers-id",
        )
        db.add_all(
            [
                self.orders_customer_id,
                self.orders_amount,
                self.orders_status,
                self.customers_id,
            ]
        )
        await db.flush()
        return self

    def context(self, principal_id: str = "svc-query-history-miner"):
        return security_context(
            organization_id=self.organization.id,
            principal_id=principal_id,
            roles=frozenset({"PlatformAdmin"}),
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


def _repeated_join_and_metric_entries(count: int = 5) -> list[WarehouseQueryLogEntry]:
    return [
        WarehouseQueryLogEntry(query_id=f"q{i}", sql_text=_JOIN_SQL) for i in range(count)
    ]


async def test_mine_and_land_query_history_candidates_creates_join_and_metric_candidates(
    scenario: _Scenario,
) -> None:
    result = await mine_and_land_query_history_candidates(
        scenario.db,
        scenario.context(),
        datasource=scenario.datasource,
        project_id=scenario.project.id,
        entries=_repeated_join_and_metric_entries(),
        correlation_id="corr-at12",
    )
    await scenario.db.commit()

    assert len(result.relationship_candidates) == 1
    join_candidate = result.relationship_candidates[0]
    assert join_candidate.detection_rule == "QUERY_LOG_JOIN_V1"
    assert join_candidate.source_column_id == scenario.customers_id.id
    assert join_candidate.target_column_id == scenario.orders_customer_id.id
    assert join_candidate.confidence <= QUERY_HISTORY_CONFIDENCE_CAP
    assert join_candidate.evidence["value_free"] is True
    assert join_candidate.evidence["occurrence_count"] == 5

    assert len(result.metric_candidates) == 1
    metric_candidate = result.metric_candidates[0]
    assert metric_candidate.detection_rule == "QUERY_LOG_METRIC_V1"
    assert metric_candidate.table_id == scenario.orders_table.id
    assert metric_candidate.measure_column_id == scenario.orders_amount.id
    assert metric_candidate.aggregation == "SUM"
    assert metric_candidate.grain_column_ids == [str(scenario.orders_customer_id.id)]
    assert metric_candidate.governance_review_id is not None

    review = await scenario.db.get(GovernanceReview, metric_candidate.governance_review_id)
    assert review is not None
    assert review.status == "PENDING"
    assert review.object_type == "QUERY_HISTORY_METRIC_CANDIDATE"
    assert review.object_id == str(metric_candidate.id)


async def test_mined_candidates_are_never_auto_approved(scenario: _Scenario) -> None:
    result = await mine_and_land_query_history_candidates(
        scenario.db,
        scenario.context(),
        datasource=scenario.datasource,
        project_id=scenario.project.id,
        entries=_repeated_join_and_metric_entries(),
        correlation_id="corr-at12",
    )
    await scenario.db.commit()

    for candidate in result.relationship_candidates:
        assert candidate.status == "PENDING"
    for candidate in result.metric_candidates:
        assert candidate.status == "PENDING"

    relationship_rows = (
        await scenario.db.scalars(
            select(RelationshipCandidate).where(
                RelationshipCandidate.organization_id == scenario.organization.id
            )
        )
    ).all()
    assert all(row.status == "PENDING" for row in relationship_rows)
    metric_rows = (
        await scenario.db.scalars(
            select(QueryHistoryMetricCandidate).where(
                QueryHistoryMetricCandidate.organization_id == scenario.organization.id
            )
        )
    ).all()
    assert all(row.status == "PENDING" for row in metric_rows)


async def test_mining_is_idempotent_against_the_same_log(scenario: _Scenario) -> None:
    entries = _repeated_join_and_metric_entries()
    await mine_and_land_query_history_candidates(
        scenario.db,
        scenario.context(),
        datasource=scenario.datasource,
        project_id=scenario.project.id,
        entries=entries,
        correlation_id="corr-at12-first",
    )
    await scenario.db.commit()

    second = await mine_and_land_query_history_candidates(
        scenario.db,
        scenario.context(),
        datasource=scenario.datasource,
        project_id=scenario.project.id,
        entries=entries,
        correlation_id="corr-at12-second",
    )
    await scenario.db.commit()

    assert second.relationship_candidates == ()
    assert second.metric_candidates == ()
    relationship_rows = (
        await scenario.db.scalars(
            select(RelationshipCandidate).where(
                RelationshipCandidate.organization_id == scenario.organization.id
            )
        )
    ).all()
    assert len(relationship_rows) == 1
    metric_rows = (
        await scenario.db.scalars(
            select(QueryHistoryMetricCandidate).where(
                QueryHistoryMetricCandidate.organization_id == scenario.organization.id
            )
        )
    ).all()
    assert len(metric_rows) == 1


async def test_mining_below_min_occurrences_produces_nothing(scenario: _Scenario) -> None:
    result = await mine_and_land_query_history_candidates(
        scenario.db,
        scenario.context(),
        datasource=scenario.datasource,
        project_id=scenario.project.id,
        entries=_repeated_join_and_metric_entries(count=2),
        correlation_id="corr-at12-sparse",
    )
    assert result.relationship_candidates == ()
    assert result.metric_candidates == ()


async def _mine_one_metric_candidate(scenario: _Scenario) -> QueryHistoryMetricCandidate:
    result = await mine_and_land_query_history_candidates(
        scenario.db,
        scenario.context(),
        datasource=scenario.datasource,
        project_id=scenario.project.id,
        entries=_repeated_join_and_metric_entries(),
        correlation_id="corr-at12",
    )
    await scenario.db.commit()
    return result.metric_candidates[0]


async def test_approving_a_metric_candidate_publishes_a_real_metric(scenario: _Scenario) -> None:
    candidate = await _mine_one_metric_candidate(scenario)
    review = await scenario.db.get(GovernanceReview, candidate.governance_review_id)
    assert review is not None

    reviewer = scenario.context(principal_id="reviewer-1")
    event_type, aggregate_type, aggregate_id, payload = await _apply_governance_review_decision(
        scenario.db,
        review,
        decision="APPROVE",
        reason=None,
        context=reviewer,
        now=_NOW,
    )
    await scenario.db.commit()

    assert event_type == "query_history_metric_candidate.approved.v1"
    assert aggregate_type == "query_history_metric_candidate"
    assert payload["candidate_id"] == str(candidate.id)

    refreshed = await scenario.db.get(QueryHistoryMetricCandidate, candidate.id)
    assert refreshed is not None
    assert refreshed.status == "APPROVED"
    assert refreshed.reviewed_by == "reviewer-1"
    assert refreshed.published_metric_version_id is not None

    version = await scenario.db.get(SemanticMetricVersion, refreshed.published_metric_version_id)
    assert version is not None
    assert version.status == "PUBLISHED"
    assert version.source_table_id == scenario.orders_table.id
    assert version.measure_column_id == scenario.orders_amount.id
    assert version.aggregation == "SUM"
    assert version.allowed_dimension_column_ids == [str(scenario.orders_customer_id.id)]


async def test_rejecting_a_metric_candidate_publishes_nothing(scenario: _Scenario) -> None:
    candidate = await _mine_one_metric_candidate(scenario)
    review = await scenario.db.get(GovernanceReview, candidate.governance_review_id)
    assert review is not None

    reviewer = scenario.context(principal_id="reviewer-1")
    event_type, _, _, _ = await _apply_governance_review_decision(
        scenario.db,
        review,
        decision="REJECT",
        reason="not a real business metric",
        context=reviewer,
        now=_NOW,
    )
    await scenario.db.commit()

    assert event_type == "query_history_metric_candidate.rejected.v1"
    refreshed = await scenario.db.get(QueryHistoryMetricCandidate, candidate.id)
    assert refreshed is not None
    assert refreshed.status == "REJECTED"
    assert refreshed.published_metric_version_id is None

    versions = (await scenario.db.scalars(select(SemanticMetricVersion))).all()
    assert versions == []


async def test_deciding_an_already_decided_candidate_conflicts(scenario: _Scenario) -> None:
    from fastapi import HTTPException

    candidate = await _mine_one_metric_candidate(scenario)
    review = await scenario.db.get(GovernanceReview, candidate.governance_review_id)
    assert review is not None
    reviewer = scenario.context(principal_id="reviewer-1")
    await apply_query_history_metric_candidate_decision(
        scenario.db, review, decision="APPROVE", context=reviewer, now=_NOW
    )
    await scenario.db.commit()

    with pytest.raises(HTTPException) as excinfo:
        await apply_query_history_metric_candidate_decision(
            scenario.db, review, decision="APPROVE", context=reviewer, now=_NOW
        )
    assert excinfo.value.status_code == 409
