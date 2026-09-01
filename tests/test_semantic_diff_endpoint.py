"""SM-7: reviewers see version deltas on the governance review queue.

`GET /v1/governance/reviews/{review_id}/diff` (aida.semantic_api,
`get_governance_review_diff`) returns the structured field-level delta
(`aida.semantic_diff.diff_semantic_object`, unit-tested in isolation in
`tests/test_semantic_diff.py`) between a pending review's proposed content
and the currently published version, alongside the raw before/after
snapshots -- not instead of them.

These are integration tests against a real (in-memory sqlite) database with
rows seeded directly through the ORM, the same pattern
`test_semantic_glossary_binding.py` (SM-2) and
`test_bulk_governance_decisions.py` (PG-3) already use for this module, so
the actual SQL joins that assemble a semantic model version's metrics run
for real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.main import app
from aida.models import (
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
    TermSemanticBinding,
)
from aida.security_types import SecurityContext
from aida.semantic_api import get_governance_review_diff

# ---------------------------------------------------------------------------
# Contract: the route is registered
# ---------------------------------------------------------------------------


def test_diff_route_is_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/governance/reviews/{review_id}/diff" in paths


# ---------------------------------------------------------------------------
# Fixtures / seeding helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _context(
    org_id: UUID, *, principal: str = "reviewer", roles: frozenset[str] | None = None
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org_id,
        roles=roles or frozenset({"Reviewer"}),
    )


class _Scenario:
    """Minimal org/project/table skeleton a `SemanticMetricVersion` needs for
    its `source_table_id`/`measure_column_id` foreign keys -- mirrors
    `test_semantic_glossary_binding.py`'s own `_Scenario.build`.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
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

        self.measure_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="sale_amount",
            ordinal_position=1,
            physical_type="NUMERIC",
            nullable=False,
            fingerprint="fp-column",
        )
        db.add(self.measure_column)
        await db.flush()
        return self

    async def model_version(
        self, *, version: int, status: str, slug: str, name: str, aggregation: str, grain: str
    ) -> tuple[SemanticModelVersion, SemanticMetricVersion]:
        db = self.db
        model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=version,
            # Held constant across versions so a test that only varies a
            # metric field (e.g. `aggregation`) doesn't also pick up
            # incidental model-level noise in the diff.
            name="Sales Model",
            change_summary="metric maintenance",
            status=status,
            created_by="metric-maker",
        )
        db.add(model)
        await db.flush()
        metric = await db.scalar(
            select(SemanticMetric).where(
                SemanticMetric.project_id == self.project.id, SemanticMetric.slug == slug
            )
        )
        if metric is None:
            metric_row = SemanticMetric(
                organization_id=self.organization.id, project_id=self.project.id, slug=slug
            )
            db.add(metric_row)
            await db.flush()
            metric_id = metric_row.id
        else:
            metric_id = metric.id
        metric_version = SemanticMetricVersion(
            organization_id=self.organization.id,
            semantic_model_version_id=model.id,
            metric_id=metric_id,
            version=version,
            status=status,
            name=name,
            description=f"{name} description",
            aggregation=aggregation,
            grain=grain,
            source_table_id=self.table.id,
            measure_column_id=self.measure_column.id,
            fingerprint=f"fp-{slug}-{version}",
            created_by="metric-maker",
        )
        db.add(metric_version)
        await db.flush()
        return model, metric_version


async def _review(
    db: AsyncSession, *, org_id: UUID, object_type: str, object_id: str, status: str = "PENDING"
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=org_id,
        object_type=object_type,
        object_id=object_id,
        requested_action="PUBLISH",
        requested_by="maker",
        status=status,
    )
    db.add(review)
    await db.flush()
    return review


# ---------------------------------------------------------------------------
# SEMANTIC_MODEL_VERSION: no published predecessor yet
# ---------------------------------------------------------------------------


async def test_first_submission_diffs_against_empty_before(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    draft_model, _draft_metric = await scenario.model_version(
        version=1,
        status="REVIEW_REQUIRED",
        slug="revenue",
        name="Revenue",
        aggregation="SUM",
        grain="daily",
    )
    review = await _review(
        session,
        org_id=scenario.organization.id,
        object_type="SEMANTIC_MODEL_VERSION",
        object_id=str(draft_model.id),
    )

    result = await get_governance_review_diff(
        review.id, context=_context(scenario.organization.id), session=session
    )

    assert result.diffable is True
    assert result.before == {}
    assert result.after is not None
    assert "revenue" in result.after["metrics"]
    # `before` has no "metrics" key at all (not even `{}`) when the object has
    # never been published, so the whole field -- not a per-metric entry --
    # is reported as one `added` delta; contrast with
    # `test_diff_against_published_predecessor_reports_changed_metric_field`,
    # where a real predecessor lets per-metric nesting kick in.
    added = {entry.field: entry for entry in result.entries}
    assert added["metrics"].change == "added"
    assert added["metrics"].before is None
    assert "revenue" in added["metrics"].after


# ---------------------------------------------------------------------------
# SEMANTIC_MODEL_VERSION: diffed against a real published predecessor
# ---------------------------------------------------------------------------


async def test_diff_against_published_predecessor_reports_changed_metric_field(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    await scenario.model_version(
        version=1,
        status="PUBLISHED",
        slug="revenue",
        name="Revenue",
        aggregation="SUM",
        grain="daily",
    )
    draft_model, _draft_metric = await scenario.model_version(
        version=2,
        status="REVIEW_REQUIRED",
        slug="revenue",
        name="Revenue",
        aggregation="AVG",
        grain="daily",
    )
    review = await _review(
        session,
        org_id=scenario.organization.id,
        object_type="SEMANTIC_MODEL_VERSION",
        object_id=str(draft_model.id),
    )

    result = await get_governance_review_diff(
        review.id, context=_context(scenario.organization.id), session=session
    )

    assert result.diffable is True
    assert result.before is not None
    assert result.before["metrics"]["revenue"]["aggregation"] == "SUM"
    assert result.after is not None
    assert result.after["metrics"]["revenue"]["aggregation"] == "AVG"
    assert [entry.field for entry in result.entries] == ["metrics.revenue.aggregation"]


async def test_diff_entries_shape_matches_field_level_delta(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    await scenario.model_version(
        version=1,
        status="PUBLISHED",
        slug="revenue",
        name="Revenue",
        aggregation="SUM",
        grain="daily",
    )
    draft_model, _draft_metric = await scenario.model_version(
        version=2,
        status="REVIEW_REQUIRED",
        slug="revenue",
        name="Revenue",
        aggregation="AVG",
        grain="daily",
    )
    review = await _review(
        session,
        org_id=scenario.organization.id,
        object_type="SEMANTIC_MODEL_VERSION",
        object_id=str(draft_model.id),
    )

    result = await get_governance_review_diff(
        review.id, context=_context(scenario.organization.id), session=session
    )

    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.field == "metrics.revenue.aggregation"
    assert entry.change == "changed"
    assert entry.before == "SUM"
    assert entry.after == "AVG"


async def test_unchanged_metric_produces_no_diff(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    await scenario.model_version(
        version=1,
        status="PUBLISHED",
        slug="revenue",
        name="Revenue",
        aggregation="SUM",
        grain="daily",
    )
    draft_model, _draft_metric = await scenario.model_version(
        version=2,
        status="REVIEW_REQUIRED",
        slug="revenue",
        name="Revenue",
        aggregation="SUM",
        grain="daily",
    )
    review = await _review(
        session,
        org_id=scenario.organization.id,
        object_type="SEMANTIC_MODEL_VERSION",
        object_id=str(draft_model.id),
    )

    result = await get_governance_review_diff(
        review.id, context=_context(scenario.organization.id), session=session
    )

    assert result.entries == []


# ---------------------------------------------------------------------------
# GLOSSARY_TERM_VERSION
# ---------------------------------------------------------------------------


async def test_glossary_term_diff_reports_synonym_change(session: AsyncSession) -> None:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    term = GlossaryTerm(organization_id=org.id, term_key="net-sales")
    session.add(term)
    await session.flush()
    published = GlossaryTermVersion(
        organization_id=org.id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net Sales",
        definition="Total sale value after adjustments.",
        synonyms=["revenue"],
        created_by="term-maker",
        approved_by="term-checker",
        approved_at=datetime.now(UTC),
    )
    session.add(published)
    await session.flush()
    draft = GlossaryTermVersion(
        organization_id=org.id,
        term_id=term.id,
        version=2,
        status="REVIEW_REQUIRED",
        display_name="Net Sales",
        definition="Total sale value after adjustments.",
        synonyms=["revenue", "net revenue"],
        created_by="term-maker",
    )
    session.add(draft)
    await session.flush()
    review = await _review(
        session,
        org_id=org.id,
        object_type="GLOSSARY_TERM_VERSION",
        object_id=str(draft.id),
    )

    result = await get_governance_review_diff(review.id, context=_context(org.id), session=session)

    assert result.diffable is True
    assert len(result.entries) == 1
    assert result.entries[0].field == "synonyms"
    assert result.entries[0].change == "changed"
    assert result.entries[0].before == ["revenue"]
    assert result.entries[0].after == ["net revenue", "revenue"]


# ---------------------------------------------------------------------------
# Unsupported object types: diffable=False with a message, never an error
# ---------------------------------------------------------------------------


async def test_unsupported_object_type_returns_diffable_false_with_message(
    session: AsyncSession,
) -> None:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    term = GlossaryTerm(organization_id=org.id, term_key="net-sales")
    session.add(term)
    await session.flush()
    binding = TermSemanticBinding(
        organization_id=org.id,
        term_id=term.id,
        semantic_object_type="SEMANTIC_METRIC",
        semantic_object_id=uuid4(),
        status="PENDING_APPROVAL",
        requested_by="maker",
    )
    session.add(binding)
    await session.flush()
    review = await _review(
        session,
        org_id=org.id,
        object_type="TERM_SEMANTIC_BINDING",
        object_id=str(binding.id),
    )

    result = await get_governance_review_diff(review.id, context=_context(org.id), session=session)

    assert result.diffable is False
    assert result.entries == []
    assert result.before is None
    assert result.after is None
    assert result.message is not None
    assert "TERM_SEMANTIC_BINDING" in result.message


# ---------------------------------------------------------------------------
# Errors and boundaries
# ---------------------------------------------------------------------------


async def test_diff_for_missing_review_is_404(session: AsyncSession) -> None:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await get_governance_review_diff(uuid4(), context=_context(org.id), session=session)
    assert exc_info.value.status_code == 404


async def test_diff_enforces_organization_boundary(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    draft_model, _ = await scenario.model_version(
        version=1,
        status="REVIEW_REQUIRED",
        slug="revenue",
        name="Revenue",
        aggregation="SUM",
        grain="daily",
    )
    review = await _review(
        session,
        org_id=scenario.organization.id,
        object_type="SEMANTIC_MODEL_VERSION",
        object_id=str(draft_model.id),
    )
    other_org_id = uuid4()

    with pytest.raises(HTTPException) as exc_info:
        await get_governance_review_diff(review.id, context=_context(other_org_id), session=session)
    assert exc_info.value.status_code == 403
