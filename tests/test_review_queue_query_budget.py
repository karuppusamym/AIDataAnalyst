"""F16 -- the review queue's cost no longer depends on how many rows you ask for.

The finding: confidence and evidence were already batched, but every review
still called a database-backed diff composer, so a page of N reviews issued
O(N) statements; and the Overview screen requested a page of up to 1,000 fully
composed reviews in order to display a count.

The claim these tests make is deliberately the strong one -- not "fewer
queries", which any caching change can produce, but "the same number of queries
for one row as for many", which only a batched read model can. The statements
are counted at the DBAPI cursor, so nothing above it can hide a round trip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.review_queue_read_model import compose_review_queue, summarize_review_queue


class _Counter:
    """Counts statements at the DBAPI cursor, where nothing can be elided."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __len__(self) -> int:
        return len(self.statements)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    created = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with created.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def session(engine: Any) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active


@pytest.fixture
def counted(engine: Any) -> Iterator[_Counter]:
    counter = _Counter()

    def _record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        counter.statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield counter
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


class _Estate:
    """The smallest org/project/table skeleton the diffable review types need,
    plus builders for the two types SM-7 can actually diff -- those are the two
    that used to issue per-row queries.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Estate:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()
        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
        )
        db.add(self.lob)
        await db.flush()
        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code=f"FIN{uuid4().hex[:6]}",
        )
        db.add(self.domain)
        await db.flush()
        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug=f"core-{uuid4().hex[:8]}",
        )
        db.add(self.project)
        await db.flush()
        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name=f"warehouse-{uuid4().hex[:8]}",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://warehouse",
        )
        db.add(self.datasource)
        await db.flush()
        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()
        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()
        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()
        return self

    async def semantic_model_review(self, index: int) -> GovernanceReview:
        """One project per review, each with a PUBLISHED predecessor and a draft
        under review -- the shape that made the old composer issue up to five
        statements per row.
        """
        db = self.db
        project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name=f"Project {index}",
            slug=f"project-{index}-{uuid4().hex[:8]}",
        )
        db.add(project)
        await db.flush()
        metric = SemanticMetric(
            organization_id=self.organization.id, project_id=project.id, slug=f"revenue-{index}"
        )
        db.add(metric)
        await db.flush()
        for version, status, aggregation in (
            (1, "PUBLISHED", "SUM"),
            (2, "REVIEW_REQUIRED", "AVG"),
        ):
            model = SemanticModelVersion(
                organization_id=self.organization.id,
                project_id=project.id,
                version=version,
                name=f"Model {index}",
                change_summary="metric maintenance",
                status=status,
                created_by="metric-maker",
            )
            db.add(model)
            await db.flush()
            db.add(
                SemanticMetricVersion(
                    organization_id=self.organization.id,
                    semantic_model_version_id=model.id,
                    metric_id=metric.id,
                    version=version,
                    status=status,
                    name="Revenue",
                    description="Revenue description",
                    aggregation=aggregation,
                    grain="daily",
                    source_table_id=self.table.id,
                    fingerprint=f"fp-{index}-{version}",
                    created_by="metric-maker",
                )
            )
            await db.flush()
            if status != "PUBLISHED":
                draft_id = model.id
        review = GovernanceReview(
            organization_id=self.organization.id,
            object_type="SEMANTIC_MODEL_VERSION",
            object_id=str(draft_id),
            requested_action="PUBLISH",
            requested_by="metric-maker",
        )
        db.add(review)
        await db.flush()
        return review

    async def glossary_term_review(self, index: int) -> GovernanceReview:
        db = self.db
        term = GlossaryTerm(
            organization_id=self.organization.id,
            term_key=f"net-revenue-{index}-{uuid4().hex[:6]}",
        )
        db.add(term)
        await db.flush()
        for version, status in ((1, "APPROVED"), (2, "REVIEW_REQUIRED")):
            term_version = GlossaryTermVersion(
                organization_id=self.organization.id,
                term_id=term.id,
                version=version,
                status=status,
                display_name=f"Net Revenue {index}",
                definition=f"definition v{version}",
                synonyms=["NR"],
                created_by="steward",
            )
            db.add(term_version)
            await db.flush()
            if status != "APPROVED":
                draft_id = term_version.id
        review = GovernanceReview(
            organization_id=self.organization.id,
            object_type="GLOSSARY_TERM_VERSION",
            object_id=str(draft_id),
            requested_action="APPROVE",
            requested_by="steward",
        )
        db.add(review)
        await db.flush()
        return review


async def test_composition_query_count_does_not_grow_with_page_size(
    session: AsyncSession, counted: _Counter
) -> None:
    """The property F16 asks to be proven: same statement count for a one-row
    page and a sixteen-row page of the same, genuinely diffable, type.
    """
    estate = await _Estate(session).build()
    reviews = [await estate.semantic_model_review(index) for index in range(16)]

    counted.statements.clear()
    await compose_review_queue(session, reviews[:1])
    one_row = len(counted)

    counted.statements.clear()
    await compose_review_queue(session, reviews)
    sixteen_rows = len(counted)

    assert one_row == sixteen_rows, (
        f"one review cost {one_row} statements, sixteen cost {sixteen_rows}. "
        "The queue endpoint's cost must not scale with the requested page size."
    )


async def test_composition_query_count_is_bounded_across_mixed_types(
    session: AsyncSession, counted: _Counter
) -> None:
    """Mixed pages are the real case. The budget is per *type present*, never
    per row, so adding rows of the same types must not add statements.
    """
    estate = await _Estate(session).build()
    small = [await estate.semantic_model_review(100), await estate.glossary_term_review(100)]
    large = small + [
        review
        for index in range(101, 111)
        for review in (
            await estate.semantic_model_review(index),
            await estate.glossary_term_review(index),
        )
    ]

    counted.statements.clear()
    await compose_review_queue(session, small)
    small_cost = len(counted)

    counted.statements.clear()
    await compose_review_queue(session, large)
    large_cost = len(counted)

    assert small_cost == large_cost, (
        f"2 reviews cost {small_cost} statements, {len(large)} cost {large_cost}"
    )
    # The absolute ceiling matters too: "constant" at 500 statements would still
    # be a bad endpoint. Five per diffable type is the batched shape.
    assert large_cost <= 15


async def test_summary_answers_a_count_in_one_statement_composing_nothing(
    session: AsyncSession, counted: _Counter
) -> None:
    """The Overview screen's actual need. Previously it fetched a page of up to
    1,000 composed reviews to take its length.
    """
    estate = await _Estate(session).build()
    for index in range(12):
        await estate.semantic_model_review(index)
    for index in range(5):
        await estate.glossary_term_review(index)

    counted.statements.clear()
    summary = await summarize_review_queue(
        session, organization_id=estate.organization.id, status="PENDING"
    )

    assert len(counted) == 1, f"the summary issued {len(counted)} statements: {counted.statements}"
    assert summary.total == 17
    assert summary.by_status == {"PENDING": 17}
    assert summary.by_object_type == {"SEMANTIC_MODEL_VERSION": 12, "GLOSSARY_TERM_VERSION": 5}
    assert summary.by_queue == {"PUBLISH": 12, "APPROVE": 5}


async def test_summary_cost_is_flat_as_the_queue_grows(
    session: AsyncSession, counted: _Counter
) -> None:
    estate = await _Estate(session).build()
    await estate.semantic_model_review(500)

    counted.statements.clear()
    await summarize_review_queue(session, organization_id=estate.organization.id, status="PENDING")
    with_one = len(counted)

    for index in range(501, 521):
        await estate.semantic_model_review(index)

    counted.statements.clear()
    await summarize_review_queue(session, organization_id=estate.organization.id, status="PENDING")
    with_twenty_one = len(counted)

    assert with_one == with_twenty_one == 1


async def test_summary_is_scoped_to_the_callers_organization(
    session: AsyncSession,
) -> None:
    """The summary is an aggregate over rows the caller never sees, so its
    tenant scope is the only thing standing between a count and a cross-tenant
    disclosure -- small, but exactly the kind of leak an aggregate hides.
    """
    mine = await _Estate(session).build()
    theirs = await _Estate(session).build()
    await mine.semantic_model_review(900)
    await theirs.semantic_model_review(901)
    await theirs.semantic_model_review(902)

    summary = await summarize_review_queue(session, organization_id=mine.organization.id)
    assert summary.total == 1
