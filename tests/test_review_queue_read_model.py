"""UX-17: review-queue read model -- run summary plus each proposal's diff,
confidence and evidence.

`GET /v1/governance/reviews/queue` (`aida.review_queue_api.get_review_queue`)
composes `aida.review_queue_read_model.compose_review_queue` -- one row per
selected `GovernanceReview`, each carrying its own confidence, evidence
(`aida.asset_evidence`'s `EvidenceItemRead` shape) and a structured diff that
reuses SM-7's `compose_governance_review_diff` (`aida.semantic_api`) directly.

Sections:

1. route is registered;
2. composition shape across two different proposal types -- one diffable
   (`SEMANTIC_MODEL_VERSION`, via SM-7) and one not (`METADATA_ENRICHMENT_
   PROPOSAL`, confidence + evidence composed from `aida.review_queue_read_
   model`'s own dispatch);
3. counts (`total_proposals`, `by_status`, `by_object_type`,
   `diffable_count`) are `computed_field`s mathematically derived from
   `proposals` -- never independently settable, and always consistent with
   the list actually returned;
4. shared-behavior: the diff embedded in a queue row for a given review is
   byte-for-byte what SM-7's own `GET /v1/governance/reviews/{id}/diff`
   returns for that same review, because both call the same
   `compose_governance_review_diff` function.

Runs the real endpoint bodies against an in-memory SQLite database, the same
pattern `test_semantic_diff_endpoint.py` (SM-7) and `test_asset_evidence.py`
(UX-13) already use for this module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.main import app
from aida.models import (
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataEnrichmentProposal,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticInferenceRun,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.review_queue_api import get_review_queue
from aida.review_queue_read_model import compose_review_queue
from aida.review_queue_schemas import ReviewQueueProposalRead, ReviewQueueRead
from aida.semantic_api import GovernanceReviewDiffRead, get_governance_review_diff
from tests.support.doubles import security_context

# `asyncio_mode = "auto"` (pyproject.toml) runs every `async def test_*` as a
# test on its own -- no `pytestmark`/per-test marker needed, and this file
# mixes async (DB-backed) and sync (pure-model) tests.


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_review_queue_route_is_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/governance/reviews/queue" in paths


# ---------------------------------------------------------------------------
# Fixtures / seeding
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


class _Scenario:
    """Minimal org/project/datasource/table skeleton, following
    `test_semantic_diff_endpoint.py::_Scenario` and
    `test_asset_evidence.py::_seed_datasource`.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
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
            slug=f"core-banking-{uuid4().hex[:8]}",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name=f"core-warehouse-{uuid4().hex[:8]}",
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
        return self

    async def semantic_model_review(
        self, *, published_aggregation: str, draft_aggregation: str
    ) -> GovernanceReview:
        """A `SEMANTIC_MODEL_VERSION` review with a real published predecessor,
        so the embedded diff carries at least one entry -- exercising the
        genuinely diffable path SM-7 built.
        """
        db = self.db
        metric = SemanticMetric(
            organization_id=self.organization.id, project_id=self.project.id, slug="revenue"
        )
        db.add(metric)
        await db.flush()

        published_model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=1,
            name="Sales Model",
            change_summary="metric maintenance",
            status="PUBLISHED",
            created_by="metric-maker",
        )
        db.add(published_model)
        await db.flush()
        db.add(
            SemanticMetricVersion(
                organization_id=self.organization.id,
                semantic_model_version_id=published_model.id,
                metric_id=metric.id,
                version=1,
                status="PUBLISHED",
                name="Revenue",
                description="Revenue description",
                aggregation=published_aggregation,
                grain="daily",
                source_table_id=self.table.id,
                fingerprint="fp-revenue-1",
                created_by="metric-maker",
            )
        )

        draft_model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=2,
            name="Sales Model",
            change_summary="metric maintenance",
            status="REVIEW_REQUIRED",
            created_by="metric-maker",
        )
        db.add(draft_model)
        await db.flush()
        db.add(
            SemanticMetricVersion(
                organization_id=self.organization.id,
                semantic_model_version_id=draft_model.id,
                metric_id=metric.id,
                version=2,
                status="REVIEW_REQUIRED",
                name="Revenue",
                description="Revenue description",
                aggregation=draft_aggregation,
                grain="daily",
                source_table_id=self.table.id,
                fingerprint="fp-revenue-2",
                created_by="metric-maker",
            )
        )
        await db.flush()

        review = GovernanceReview(
            organization_id=self.organization.id,
            object_type="SEMANTIC_MODEL_VERSION",
            object_id=str(draft_model.id),
            requested_action="PUBLISH",
            requested_by="metric-maker",
        )
        db.add(review)
        await db.flush()
        return review

    async def metadata_enrichment_review(
        self, *, confidence: float = 0.74
    ) -> tuple[GovernanceReview, MetadataEnrichmentProposal, SemanticInferenceRun]:
        """A `METADATA_ENRICHMENT_PROPOSAL` review -- the one proposal type
        with a real persisted "run" (`inference_run_id` ->
        `SemanticInferenceRun`), and one SM-7 never diffs (its object_type is
        not `SEMANTIC_MODEL_VERSION`/`GLOSSARY_TERM_VERSION`).
        """
        db = self.db
        run = SemanticInferenceRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            status="COMPLETED",
            engine_mode="RULES_ONLY",
            engine_version="v1",
            table_count=1,
            proposal_count=1,
            model_enriched_count=0,
            rule_only_count=1,
            created_by="inference-engine",
            completed_at=datetime.now(UTC),
        )
        db.add(run)
        await db.flush()

        review = GovernanceReview(
            organization_id=self.organization.id,
            object_type="METADATA_ENRICHMENT_PROPOSAL",
            object_id=str(uuid4()),
            requested_action="APPLY_BUSINESS_SEMANTICS",
            requested_by="inference-engine",
        )
        db.add(review)
        await db.flush()

        proposal = MetadataEnrichmentProposal(
            id=UUID(review.object_id),
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            inference_run_id=run.id,
            table_id=self.table.id,
            governance_review_id=review.id,
            proposal_type="TABLE_BUSINESS_SEMANTICS",
            engine_type="RULES",
            engine_version="v1",
            confidence=confidence,
            payload={"business_name": "Sales Fact"},
            evidence={
                "value_scope": "METADATA_ONLY",
                "rules_version": "v1",
                "model_used": False,
                "evidence_ids": [f"table:{self.table.id}", "rule:GRAIN_PRIMARY_KEY"],
            },
            fingerprint="fp-enrichment-1",
            proposed_by="inference-engine",
        )
        db.add(proposal)
        await db.flush()
        return review, proposal, run


# ---------------------------------------------------------------------------
# Composition shape across two proposal types
# ---------------------------------------------------------------------------


async def test_composed_queue_covers_a_diffable_and_a_non_diffable_proposal(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    model_review = await scenario.semantic_model_review(
        published_aggregation="SUM", draft_aggregation="AVG"
    )
    enrichment_review, proposal, _run = await scenario.metadata_enrichment_review(confidence=0.81)

    result = await get_review_queue(
        review_status="PENDING",
        object_type=None,
        inference_run_id=None,
        limit=1000,
        context=security_context(
            organization_id=scenario.organization.id, roles=frozenset({"Reviewer"})
        ),
        session=session,
    )

    by_review_id = {row.review_id: row for row in result.proposals}
    assert set(by_review_id) == {model_review.id, enrichment_review.id}

    model_row = by_review_id[model_review.id]
    assert model_row.object_type == "SEMANTIC_MODEL_VERSION"
    assert model_row.confidence is None
    assert model_row.diff.diffable is True
    assert [entry.field for entry in model_row.diff.entries] == ["metrics.revenue.aggregation"]

    enrichment_row = by_review_id[enrichment_review.id]
    assert enrichment_row.object_type == "METADATA_ENRICHMENT_PROPOSAL"
    assert enrichment_row.confidence == pytest.approx(0.81)
    assert enrichment_row.diff.diffable is False
    assert enrichment_row.diff.message is not None
    # Evidence is composed, not empty, and every item carries a source --
    # the same discipline UX-13's `AssetEvidenceRead` items follow.
    assert enrichment_row.evidence
    assert all(item.source for item in enrichment_row.evidence)
    evidence_claims = {item.claim for item in enrichment_row.evidence}
    assert any(f"table:{proposal.table_id}" in claim for claim in evidence_claims)
    assert any(str(proposal.inference_run_id) in claim for claim in evidence_claims)


async def test_inference_run_id_filter_scopes_to_that_run(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    await scenario.semantic_model_review(published_aggregation="SUM", draft_aggregation="AVG")
    enrichment_review, _proposal, run = await scenario.metadata_enrichment_review()

    result = await get_review_queue(
        review_status="PENDING",
        object_type=None,
        inference_run_id=run.id,
        limit=1000,
        context=security_context(
            organization_id=scenario.organization.id, roles=frozenset({"Reviewer"})
        ),
        session=session,
    )

    assert [row.review_id for row in result.proposals] == [enrichment_review.id]
    assert result.inference_run_id_filter == run.id


# ---------------------------------------------------------------------------
# Counts are derived from the returned list, never independently settable
# ---------------------------------------------------------------------------


def _stub_diff(review_id: UUID, *, diffable: bool) -> GovernanceReviewDiffRead:
    return GovernanceReviewDiffRead(
        review_id=review_id,
        object_type="SEMANTIC_MODEL_VERSION" if diffable else "METADATA_ENRICHMENT_PROPOSAL",
        object_id=str(uuid4()),
        diffable=diffable,
        before={} if diffable else None,
        after={"x": 1} if diffable else None,
        entries=[],
        message=None if diffable else "structured diffs are not yet available",
    )


def _stub_proposal(*, status: str, object_type: str, diffable: bool) -> ReviewQueueProposalRead:
    review_id = uuid4()
    return ReviewQueueProposalRead(
        review_id=review_id,
        organization_id=uuid4(),
        object_type=object_type,
        object_id=str(uuid4()),
        requested_action="PUBLISH",
        status=status,
        requested_by="maker",
        decided_by=None,
        decision_reason=None,
        decided_at=None,
        created_at=datetime.now(UTC),
        confidence=None,
        evidence=[],
        diff=_stub_diff(review_id, diffable=diffable),
    )


def test_counts_are_mathematically_derived_from_the_proposals_list() -> None:
    proposals = [
        _stub_proposal(status="PENDING", object_type="SEMANTIC_MODEL_VERSION", diffable=True),
        _stub_proposal(
            status="PENDING", object_type="METADATA_ENRICHMENT_PROPOSAL", diffable=False
        ),
        _stub_proposal(
            status="APPROVED", object_type="METADATA_ENRICHMENT_PROPOSAL", diffable=False
        ),
    ]
    queue = ReviewQueueRead(
        organization_id=uuid4(),
        status_filter=None,
        object_type_filter=None,
        inference_run_id_filter=None,
        generated_at=datetime.now(UTC),
        proposals=proposals,
    )

    assert queue.total_proposals == 3 == len(queue.proposals)
    assert queue.by_status == {"PENDING": 2, "APPROVED": 1}
    assert sum(queue.by_status.values()) == queue.total_proposals
    assert queue.by_object_type == {"SEMANTIC_MODEL_VERSION": 1, "METADATA_ENRICHMENT_PROPOSAL": 2}
    assert sum(queue.by_object_type.values()) == queue.total_proposals
    assert queue.diffable_count == 1

    # Removing an item from the source list changes every derived count --
    # there is no cached/independent count to go stale.
    trimmed = ReviewQueueRead(
        organization_id=queue.organization_id,
        status_filter=None,
        object_type_filter=None,
        inference_run_id_filter=None,
        generated_at=queue.generated_at,
        proposals=proposals[:1],
    )
    assert trimmed.total_proposals == 1
    assert trimmed.by_status == {"PENDING": 1}


def test_counts_are_not_independently_settable() -> None:
    """`total_proposals`/`by_status`/`by_object_type`/`diffable_count` are
    Pydantic `computed_field`s, not real `__init__` parameters -- passing one
    explicitly is rejected by `ApiModel`'s `extra="forbid"` rather than
    silently accepted and left to disagree with `proposals`.
    """
    with pytest.raises(ValidationError):
        ReviewQueueRead(
            organization_id=uuid4(),
            status_filter=None,
            object_type_filter=None,
            inference_run_id_filter=None,
            generated_at=datetime.now(UTC),
            proposals=[],
            total_proposals=99,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Shared behavior: the embedded diff matches SM-7's own endpoint exactly
# ---------------------------------------------------------------------------


async def test_embedded_diff_matches_sm7_endpoint_directly(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    model_review = await scenario.semantic_model_review(
        published_aggregation="SUM", draft_aggregation="AVG"
    )

    direct = await get_governance_review_diff(
        model_review.id,
        context=security_context(organization_id=scenario.organization.id),
        session=session,
    )
    [composed] = await compose_review_queue(session, [model_review])

    assert composed.diff == direct
    assert composed.diff.entries == direct.entries
    assert composed.diff.before == direct.before
    assert composed.diff.after == direct.after
    assert composed.diff.diffable == direct.diffable is True
