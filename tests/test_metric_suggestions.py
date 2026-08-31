"""SM-4 exit-condition tests: metric suggestions from approved annotations.

Two things must be true no matter how the scoring logic evolves:

(a) a low-evidence proposal can never reach a state a non-reviewer could
    mistake for published -- the minimum-evidence gate (`ensure_reviewable`)
    blocks it before a `governance_review` row is ever created, and

(b) a high-confidence proposal still requires independent approval -- there
    is no code path, however high the score, that publishes a metric
    without going through `semantic_api.decide_governance_review`, and that
    function's shared maker-checker guard (self-approval denied) runs
    before the SEMANTIC_METRIC_PROPOSAL branch is ever reached.

The first half of the file exercises the deterministic scoring and
composition pure functions directly -- no database, no network, no external
model call -- mirroring `tests/test_asset_description.py` (GL-9). The second
half proves the full generate -> submit -> approve flow against a real
(in-memory sqlite) database, seeding a real approved `MetadataBusinessAnnotation`
and real columns/constraints, mirroring the real-engine half of
`tests/test_semantic_glossary_binding.py` (SM-2).
"""

import inspect
import itertools
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.metric_suggestion_api as metric_suggestion_api
import aida.metric_suggestion_service as metric_suggestion_service
import aida.semantic_api as semantic_api
from aida.db import Base
from aida.main import app
from aida.metric_suggestion_service import (
    MINIMUM_EVIDENCE_FOR_METRIC_REVIEW,
    MetricEvidence,
    compose_metric_definition,
    ensure_reviewable,
    evidence_payload,
    match_measure_keyword,
    score_evidence,
)
from aida.models import (
    AssetTermLink,
    AuditEvent,
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricProposal,
    SemanticMetricVersion,
)
from aida.schemas import GovernanceDecisionRequest, MetricSuggestionProposalGenerate
from aida.semantic_api import decide_governance_review
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# Pure functions: keyword matching, scoring, composition (no DB)
# ---------------------------------------------------------------------------


def test_metric_suggestion_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/metric-suggestions/generate" in paths
    assert "/v1/organizations/{organization_id}/metric-suggestions" in paths
    assert "/v1/metric-suggestions/{proposal_id}/submit" in paths


def test_match_measure_keyword_prefers_exact_over_suffix_over_contains() -> None:
    assert match_measure_keyword("balance") == ("balance", "SUM", "EXACT")
    assert match_measure_keyword("account_balance") == ("balance", "SUM", "SUFFIX")
    assert match_measure_keyword("balance_transfer_flag") == ("balance", "SUM", "SUFFIX")
    assert match_measure_keyword("rebalance_note") == ("balance", "SUM", "CONTAINS")
    assert match_measure_keyword("customer_id") is None


def _evidence(**overrides: object) -> MetricEvidence:
    defaults: dict[str, object] = {
        "table_id": uuid4(),
        "table_name": "fact_accounts",
        "project_id": uuid4(),
        "business_annotation_id": uuid4(),
        "business_name": "Customer Accounts",
        "business_description": "Approved deposit account records.",
        "table_role": "DIMENSION",
        "grain_statement": "One row per account_id.",
        "column_id": uuid4(),
        "column_name": "account_balance",
        "physical_type": "INTEGER",
        "nullable": True,
        "matched_keyword": "balance",
        "suggested_aggregation": "SUM",
        "match_kind": "SUFFIX",
        "bound_term_names": (),
    }
    defaults.update(overrides)
    return MetricEvidence(**defaults)  # type: ignore[arg-type]


def _well_evidenced_evidence() -> MetricEvidence:
    return _evidence(
        table_name="fact_accounts",
        business_name="Customer Accounts",
        business_description="Approved deposit account records tracking account balance.",
        table_role="FACT",
        column_name="balance",
        physical_type="NUMERIC(18,2)",
        nullable=False,
        matched_keyword="balance",
        match_kind="EXACT",
        bound_term_names=("Account Balance",),
    )


def test_score_evidence_is_a_deterministic_pure_function() -> None:
    evidence = _well_evidenced_evidence()
    first = score_evidence(evidence)
    second = score_evidence(evidence)
    assert first == second


def test_score_evidence_rewards_corroborating_evidence() -> None:
    bare = score_evidence(_evidence())
    partial = score_evidence(
        _evidence(table_role="FACT", bound_term_names=("Account Balance",))
    )
    rich = score_evidence(_well_evidenced_evidence())

    assert bare.overall < partial.overall < rich.overall
    for dimension in ("accuracy", "clarity", "style", "completeness"):
        assert getattr(bare, dimension) <= getattr(rich, dimension)


def test_weak_evidence_scores_below_the_review_threshold() -> None:
    weak = score_evidence(_evidence())
    assert weak.overall < MINIMUM_EVIDENCE_FOR_METRIC_REVIEW


def test_well_evidenced_candidate_scores_at_or_above_the_review_threshold() -> None:
    rich = score_evidence(_well_evidenced_evidence())
    assert rich.overall >= MINIMUM_EVIDENCE_FOR_METRIC_REVIEW
    assert rich.overall == 1.0


def test_compose_metric_definition_uses_only_evidence_fields_no_model_call() -> None:
    slug, name, description = compose_metric_definition(_well_evidenced_evidence())
    assert slug == "fact_accounts_balance"
    assert name == "Customer Accounts Balance"
    assert "Sum of fact_accounts.balance" in description
    assert "exactly matches" in description
    assert "FACT table" in description
    assert "Account Balance" in description
    assert "Approved deposit account records tracking account balance." in description


def test_evidence_payload_is_json_safe() -> None:
    evidence = _well_evidenced_evidence()
    payload = evidence_payload(evidence)
    assert payload["column_id"] == str(evidence.column_id)
    assert payload["is_monetary_type"] is True
    assert isinstance(payload["nullable"], bool)


# --- exit condition (a): a low-evidence proposal never reaches PENDING_APPROVAL ---


def test_ensure_reviewable_blocks_low_evidence_proposals() -> None:
    weak_score = score_evidence(_evidence()).overall
    with pytest.raises(HTTPException) as exc_info:
        ensure_reviewable(weak_score)
    assert exc_info.value.status_code == 422


def test_ensure_reviewable_allows_well_evidenced_proposals() -> None:
    rich_score = score_evidence(_well_evidenced_evidence()).overall
    ensure_reviewable(rich_score)  # must not raise


def test_submit_endpoint_calls_the_evidence_gate_before_creating_a_review() -> None:
    source = inspect.getsource(metric_suggestion_api.submit_metric_suggestion_proposal)
    gate_at = source.index("ensure_reviewable(")
    review_construction_at = source.index("GovernanceReview(\n")
    assert gate_at < review_construction_at


# --- exit condition (b): no path publishes without decide_governance_review ---


def test_apply_metric_suggestion_proposal_has_exactly_one_call_site() -> None:
    """The only function that can move a proposal to APPROVED and publish a
    real `SemanticMetricVersion` is `apply_metric_suggestion_proposal`. It
    must be called from nowhere but the shared governance-review dispatch
    (`semantic_api._apply_governance_review_decision`, invoked only from
    `decide_governance_review`) -- i.e. there is no bypass, however high a
    proposal's confidence score."""
    source = inspect.getsource(semantic_api)
    call_pattern = re.compile(r"apply_metric_suggestion_proposal\(")
    matches = call_pattern.findall(source)
    # one import + one call site inside _apply_governance_review_decision
    assert source.count("apply_metric_suggestion_proposal") == 2
    assert len(matches) == 1

    dispatch_source = inspect.getsource(semantic_api._apply_governance_review_decision)
    assert "apply_metric_suggestion_proposal(" in dispatch_source
    assert "reject_metric_suggestion_proposal(" in dispatch_source

    decide_source = inspect.getsource(semantic_api.decide_governance_review)
    assert "apply_metric_suggestion_proposal(" not in decide_source
    assert "_apply_governance_review_decision(" in decide_source


def test_decide_governance_review_checks_self_approval_before_publishing() -> None:
    decide_source = inspect.getsource(semantic_api.decide_governance_review)
    guard_at = decide_source.index("maker-checker separation is required")
    dispatch_call_at = decide_source.index("_apply_governance_review_decision(")
    assert guard_at < dispatch_call_at

    dispatch_source = inspect.getsource(semantic_api._apply_governance_review_decision)
    branch_at = dispatch_source.index('review.object_type == "SEMANTIC_METRIC_PROPOSAL"')
    publish_at = dispatch_source.index("apply_metric_suggestion_proposal(")
    assert branch_at < publish_at


def test_apply_metric_suggestion_proposal_refuses_a_non_pending_proposal() -> None:
    source = inspect.getsource(metric_suggestion_service.apply_metric_suggestion_proposal)
    assert 'proposal.status != "PENDING_APPROVAL"' in source
    assert "raise HTTPException" in source


# ---------------------------------------------------------------------------
# Real (in-memory sqlite) end-to-end: generate -> submit -> approve/reject
# ---------------------------------------------------------------------------

# `AuditEvent.id` is a `BigInteger` autoincrement primary key that relies, in
# production, on Postgres's own identity/sequence generation. sqlite only
# auto-populates a bare `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger`
# compiles to `BIGINT`, which sqlite does not treat as that alias -- so an
# in-memory sqlite session (as every test below uses) leaves `id` NULL and
# violates the NOT NULL constraint on insert (`generate`/`submit` both call
# `record_audit`). Assign ids by hand for this test module's sqlite engine
# only; nothing about the production model changes. Same workaround as
# `tests/test_catalog_bulk_actions_endpoints.py` / `tests/test_bulk_governance_decisions.py`.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


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
    """One organization with a real catalog chain (org -> LOB -> domain ->
    project -> datasource -> catalog -> schema -> table -> columns), a real
    approved `MetadataBusinessAnnotation`, and a real bound glossary term --
    the minimum evidence SM-4's generation pass reads to prove the actual SQL
    joins run, not a hand-simulated approximation of them."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Scenario":
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
            name="fact_accounts",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()

        self.pk_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="account_id",
            ordinal_position=1,
            physical_type="INTEGER",
            nullable=False,
            fingerprint="fp-pk-column",
        )
        db.add(self.pk_column)
        await db.flush()

        db.add(
            MetadataConstraint(
                organization_id=self.organization.id,
                datasource_id=self.datasource.id,
                table_id=self.table.id,
                name="pk_fact_accounts",
                constraint_type="PRIMARY_KEY",
                columns=["account_id"],
                fingerprint="fp-pk-constraint",
            )
        )
        await db.flush()

        # Rich evidence: an exact-match, non-nullable, monetary column.
        self.balance_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="balance",
            ordinal_position=2,
            physical_type="NUMERIC(18,2)",
            nullable=False,
            fingerprint="fp-balance-column",
        )
        db.add(self.balance_column)

        # Weak evidence lives on a second, deliberately unremarkable table: a
        # non-fact-shaped annotation, no bound glossary term, and a column
        # that only suffix-matches a keyword the annotation's own prose never
        # mentions -- isolated from the rich table so its score is not
        # inflated by evidence (table role, glossary binding) that belongs to
        # a different table's approval.
        self.weak_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="ref_codes",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-weak-table",
        )
        db.add(self.weak_table)
        await db.flush()

        self.weak_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.weak_table.id,
            name="reference_total",
            ordinal_position=1,
            physical_type="INTEGER",
            nullable=True,
            fingerprint="fp-weak-column",
        )
        db.add(self.weak_column)
        await db.flush()

        self.weak_annotation = MetadataBusinessAnnotation(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=self.weak_table.id,
            domain_id=uuid4(),
            entity_id=uuid4(),
            source_proposal_id=uuid4(),
            version=1,
            business_name="Reference Codes",
            business_description="Approved static reference code list.",
            table_role="REFERENCE",
            grain_statement="One row per code.",
            synonyms=[],
            suggested_questions=[],
            tags=[],
            confidence=0.8,
            approved_by="steward",
            approved_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        db.add(self.weak_annotation)
        await db.flush()

        self.annotation = MetadataBusinessAnnotation(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=self.table.id,
            domain_id=uuid4(),
            entity_id=uuid4(),
            source_proposal_id=uuid4(),
            version=1,
            business_name="Customer Accounts",
            business_description=(
                "Approved deposit account records; tracks the current account balance "
                "for each customer."
            ),
            table_role="FACT",
            grain_statement="One row per account_id.",
            synonyms=[],
            suggested_questions=[],
            tags=[],
            confidence=0.95,
            approved_by="steward",
            approved_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        db.add(self.annotation)
        await db.flush()

        term = GlossaryTerm(organization_id=self.organization.id, term_key="account-balance")
        db.add(term)
        await db.flush()
        term_version = GlossaryTermVersion(
            organization_id=self.organization.id,
            term_id=term.id,
            version=1,
            status="APPROVED",
            display_name="Account Balance",
            definition="The current balance held in a customer deposit account.",
            synonyms=[],
            created_by="term-maker",
            approved_by="term-checker",
            approved_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        db.add(term_version)
        await db.flush()
        db.add(
            AssetTermLink(
                organization_id=self.organization.id,
                table_id=self.table.id,
                term_id=term.id,
                linked_by="steward",
                link_type="MANUAL",
                confidence=1.0,
            )
        )
        await db.flush()
        return self

    def maker(self) -> object:
        return security_context(
            organization_id=self.organization.id,
            principal_id="metric-maker",
            roles=frozenset({"DataSteward"}),
        )

    def checker(self) -> object:
        return security_context(
            organization_id=self.organization.id,
            principal_id="metric-checker",
            roles=frozenset({"Reviewer"}),
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_generate_creates_proposals_for_exact_and_suffix_matches_only(
    scenario: _Scenario,
) -> None:
    page = await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    proposed_columns = {item.measure_column_id for item in page.items}
    # account_id is a primary key -> excluded. balance (EXACT) and
    # reference_total (SUFFIX, matches "total") both create a proposal.
    assert scenario.pk_column.id not in proposed_columns
    assert scenario.balance_column.id in proposed_columns
    assert scenario.weak_column.id in proposed_columns

    balance_proposal = next(
        item for item in page.items if item.measure_column_id == scenario.balance_column.id
    )
    assert balance_proposal.overall_score == 1.0
    assert balance_proposal.status == "DRAFT"
    assert balance_proposal.proposed_aggregation == "SUM"

    weak_proposal = next(
        item for item in page.items if item.measure_column_id == scenario.weak_column.id
    )
    assert weak_proposal.overall_score < MINIMUM_EVIDENCE_FOR_METRIC_REVIEW


async def test_generate_is_idempotent_and_bounded_by_limit(scenario: _Scenario) -> None:
    first = await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    second = await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    assert first.total == 2
    assert second.total == 0  # same evidence tuples already have proposals


async def test_submit_refuses_a_weak_proposal_before_creating_a_review(
    scenario: _Scenario,
) -> None:
    await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    weak_proposal = await scenario.db.scalar(
        select(SemanticMetricProposal).where(
            SemanticMetricProposal.measure_column_id == scenario.weak_column.id
        )
    )
    assert weak_proposal is not None

    with pytest.raises(HTTPException) as denied:
        await metric_suggestion_api.submit_metric_suggestion_proposal(
            weak_proposal.id, scenario.maker(), scenario.db
        )
    assert denied.value.status_code == 422

    # No GovernanceReview row was created, and the proposal is still DRAFT.
    reviews = (
        await scenario.db.scalars(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "SEMANTIC_METRIC_PROPOSAL"
            )
        )
    ).all()
    assert reviews == []
    await scenario.db.refresh(weak_proposal)
    assert weak_proposal.status == "DRAFT"


async def test_full_flow_independent_approval_publishes_a_real_metric_version(
    scenario: _Scenario,
) -> None:
    await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    rich_proposal = await scenario.db.scalar(
        select(SemanticMetricProposal).where(
            SemanticMetricProposal.measure_column_id == scenario.balance_column.id
        )
    )
    assert rich_proposal is not None
    assert rich_proposal.overall_score == 1.0

    review = await metric_suggestion_api.submit_metric_suggestion_proposal(
        rich_proposal.id, scenario.maker(), scenario.db
    )
    assert review.status == "PENDING"
    assert review.object_type == "SEMANTIC_METRIC_PROPOSAL"
    await scenario.db.refresh(rich_proposal)
    assert rich_proposal.status == "PENDING_APPROVAL"

    decided = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        scenario.checker(),
        scenario.db,
    )
    assert decided.status == "APPROVED"

    await scenario.db.refresh(rich_proposal)
    assert rich_proposal.status == "APPROVED"
    assert rich_proposal.published_metric_version_id is not None

    published_version = await scenario.db.get(
        SemanticMetricVersion, rich_proposal.published_metric_version_id
    )
    assert published_version is not None
    assert published_version.status == "PUBLISHED"
    assert published_version.measure_column_id == scenario.balance_column.id
    assert published_version.source_table_id == scenario.table.id
    assert published_version.aggregation == "SUM"

    metric = await scenario.db.get(SemanticMetric, published_version.metric_id)
    assert metric is not None
    assert metric.slug == rich_proposal.proposed_slug
    assert metric.project_id == scenario.project.id


async def test_self_approval_of_a_metric_proposal_is_refused(scenario: _Scenario) -> None:
    await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    rich_proposal = await scenario.db.scalar(
        select(SemanticMetricProposal).where(
            SemanticMetricProposal.measure_column_id == scenario.balance_column.id
        )
    )
    assert rich_proposal is not None

    maker = scenario.maker()
    review = await metric_suggestion_api.submit_metric_suggestion_proposal(
        rich_proposal.id, maker, scenario.db
    )

    with pytest.raises(HTTPException) as self_approval:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            maker,
            scenario.db,
        )
    assert self_approval.value.status_code == 409

    await scenario.db.refresh(rich_proposal)
    assert rich_proposal.status == "PENDING_APPROVAL"  # unchanged: never published


async def test_rejected_proposal_is_retained_not_deleted(scenario: _Scenario) -> None:
    await metric_suggestion_api.generate_metric_suggestion_proposals(
        scenario.organization.id,
        MetricSuggestionProposalGenerate(limit=100),
        scenario.maker(),
        scenario.db,
    )
    rich_proposal = await scenario.db.scalar(
        select(SemanticMetricProposal).where(
            SemanticMetricProposal.measure_column_id == scenario.balance_column.id
        )
    )
    assert rich_proposal is not None
    review = await metric_suggestion_api.submit_metric_suggestion_proposal(
        rich_proposal.id, scenario.maker(), scenario.db
    )

    decided = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="REJECT", reason="Duplicate of an existing metric."),
        scenario.checker(),
        scenario.db,
    )
    assert decided.status == "REJECTED"

    still_there = await scenario.db.get(SemanticMetricProposal, rich_proposal.id)
    assert still_there is not None
    assert still_there.status == "REJECTED"
    assert still_there.published_metric_version_id is None
