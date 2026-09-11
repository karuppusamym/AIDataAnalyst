"""AR-03's evaluation: can the reviewer agent be made to approve a wrong proposal?

The 2026-09-09 architecture review asked, under AR-03, for the half of the
finding that code alone cannot close: an adversarial evaluation of the agent's
decisions -- misleading metadata, deliberately wrong proposals, and the
false-approval rate by object type. This is that evaluation, run on every build.

**Method: twins.** Each case is a pair of proposals of one type that carry the
same evidence, where one twin's content is true and the other's is not -- a dbt
description written for a different table, a domain inferred from a keyword
inside an unrelated word, a data-dictionary row describing the wrong thing.
Each twin is scored by the producer's own scoring function, so it carries the
confidence the platform itself would give it, and each is put to the agent's
real decision function (`reviewer_agent._assess`) at the production ceiling and
approval threshold. An agent whose evidence measured truth would approve the
true twin and not the false one: the pair would be *told apart*.

**What the numbers are, and are not.** They are not a prevalence estimate. The
corpus is built to be hard, so a count here says which types can be made to
fail and how, not how often they fail in the field. A pair decided the same way
twice is one the agent's evidence cannot see into: both approved is a false
approval; both abstained is safe, but it is a person's decision, not the
agent's.

**Result, pinned 2026-09-11** (`BASELINE`, 12 pairs across 7 object types):

* No pair of any type is told apart. Every resolver's number measures how much
  evidence exists, how exactly a name matched or how large a change is -- never
  whether the content is true. This is the review's "self-reported score",
  measured.
* 7 of the 12 false twins are approved. Before this change it was 11: document
  claims (a name-match certainty, always 1.0), glossary links (name-equality
  constants, 1.0 or 0.92, both over the threshold) and model-inferred
  enrichments (a model grading its own output) now abstain.
* The 7 that remain are description drafts built on misleading sources,
  rules-inferred enrichments from a keyword match, and bulk operations and
  workbooks whose only checked property is their size.

Unattended review should stay off until something independent of the proposal
tells these twins apart. When something does, this baseline is where it shows.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida import reviewer_agent
from aida.asset_description_service import (
    AssetEvidence,
    compose_draft_text,
    ensure_reviewable,
    score_evidence,
    text_fingerprint,
)
from aida.column_description_service import (
    ORIGIN_METADATA,
    ORIGIN_MODEL_INFERRED,
    ColumnEvidence,
    compose_column_draft_text,
    score_column_evidence,
)
from aida.config import Settings
from aida.db import Base
from aida.document_ingestion_api import (
    DocumentCreate,
    extract_claims,
    map_document,
    upload_document,
)
from aida.glossary_link_candidates import (
    _PRIMARY_MATCH_CONFIDENCE,
    _SECONDARY_MATCH_CONFIDENCE,
)
from aida.models import (
    AssetDescriptionDraft,
    BulkStewardshipOperation,
    ColumnDescriptionDraft,
    DocumentClaim,
    GlossaryLinkProposal,
    GovernanceReview,
    MetadataColumn,
    MetadataConstraint,
    MetadataEnrichmentProposal,
    MetadataTable,
    ModelImportBatch,
    ModelImportChange,
    Organization,
)
from aida.review_risk_tiers import (
    HARD_MAX_AGENT_TIER,
    agent_decidable_object_types,
    effective_agent_ceiling,
)
from aida.semantic_inference import (
    SEMANTIC_INFERENCE_VERSION,
    TableSemanticOutput,
    infer_table_semantics,
)
from tests.test_document_ingestion import (
    _context,
    _seed_column,
    _seed_datasource,
    _seed_project,
    _seed_table,
)

_STEWARD = "steward-a"

Seed = Callable[[AsyncSession, Organization, bool], Awaitable[GovernanceReview]]


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


def _settings() -> Settings:
    # Production defaults for everything that decides, including the 0.8
    # approval threshold; only the switches that let the agent run at all.
    values: dict[str, object] = {
        "environment": "test",
        "reviewer_agent_enabled": True,
        "reviewer_agent_principal_id": "agent:reviewer",
        "reviewer_agent_max_tier": HARD_MAX_AGENT_TIER,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def _review(
    session: AsyncSession,
    org: Organization,
    object_type: str,
    *,
    object_id: str | None = None,
    requested_action: str = "PUBLISH",
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=org.id,
        object_type=object_type,
        object_id=object_id or str(uuid4()),
        requested_action=requested_action,
        status="PENDING",
        requested_by=_STEWARD,
    )
    session.add(review)
    await session.flush()
    return review


async def _point_at(session: AsyncSession, review: GovernanceReview, row: Any) -> None:
    """Add a proposal row and aim its review at it -- the shape the producers create."""
    session.add(row)
    await session.flush()
    review.object_id = str(row.id)
    await session.flush()


# --- ASSET_DESCRIPTION_DRAFT ----------------------------------------------


def _asset_evidence(
    *, dbt_description: str | None, business_description: str, bound_terms: tuple[str, ...]
) -> AssetEvidence:
    return AssetEvidence(
        table_id=uuid4(),
        table_name="customers",
        schema_name="retail",
        column_count=3,
        primary_key_columns=("customer_id",),
        foreign_key_count=0,
        upstream_table_names=(),
        upstream_edge_ids=(),
        downstream_table_names=(),
        downstream_edge_ids=(),
        dbt_description=dbt_description,
        dbt_documented_column_count=0,
        business_name="Customer",
        business_description=business_description,
        business_annotation_id=uuid4(),
        grain_statement="One row per customer.",
        bound_term_names=bound_terms,
        bound_term_ids=tuple(uuid4() for _ in bound_terms),
    )


async def _asset_draft(
    session: AsyncSession, org: Organization, evidence: AssetEvidence
) -> GovernanceReview:
    scores = score_evidence(evidence)
    ensure_reviewable(scores.overall)  # the producer would have submitted it
    text = compose_draft_text(evidence)
    review = await _review(session, org, "ASSET_DESCRIPTION_DRAFT")
    draft = AssetDescriptionDraft(
        organization_id=org.id,
        table_id=evidence.table_id,
        drafted_text=text,
        text_fingerprint=text_fingerprint(text),
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall,
        evidence={"source": "deterministic"},
        status="PENDING_APPROVAL",
        governance_review_id=review.id,
        created_by=_STEWARD,
    )
    await _point_at(session, review, draft)
    return review


async def _asset_misleading_dbt(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    dbt = (
        "One row per customer, keyed by customer_id."
        if truthful
        else "One row per order line, keyed by order_line_id."
    )
    evidence = _asset_evidence(
        dbt_description=dbt, business_description="Retail customer master.", bound_terms=()
    )
    return await _asset_draft(session, org, evidence)


async def _asset_wrong_annotation(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    # The false annotation is what approving the enrichment corpus's own false
    # twin below would have published. The errors chain.
    description = (
        "Retail customer master."
        if truthful
        else "Represents party supply information in the Customer domain."
    )
    evidence = _asset_evidence(
        dbt_description=None, business_description=description, bound_terms=("Customer",)
    )
    return await _asset_draft(session, org, evidence)


# --- COLUMN_DESCRIPTION_DRAFT ---------------------------------------------


def _column_evidence(*, comment: str, dbt: str) -> ColumnEvidence:
    return ColumnEvidence(
        column_id=uuid4(),
        table_id=uuid4(),
        column_name="customer_id",
        table_name="orders",
        schema_name="retail",
        physical_type="integer",
        nullable=False,
        classification="UNCLASSIFIED",
        source_description=comment,
        dbt_description=dbt,
        primary_key_width=0,
        references=("customers.customer_id",),
        related_to=(),
        relationship_candidate_ids=(),
        referenced_by=(),
        current_description_version=None,
    )


async def _column_draft(
    session: AsyncSession,
    org: Organization,
    evidence: ColumnEvidence,
    *,
    origin: str,
    overall: float | None = None,
) -> GovernanceReview:
    scores = score_column_evidence(evidence)
    text = compose_column_draft_text(evidence)
    review = await _review(session, org, "COLUMN_DESCRIPTION_DRAFT")
    draft = ColumnDescriptionDraft(
        organization_id=org.id,
        table_id=evidence.table_id,
        column_id=evidence.column_id,
        drafted_text=text,
        text_fingerprint=text_fingerprint(text),
        accuracy_score=scores.accuracy,
        clarity_score=scores.clarity,
        style_score=scores.style,
        completeness_score=scores.completeness,
        overall_score=scores.overall if overall is None else overall,
        evidence={"origin": origin},
        status="PENDING_APPROVAL",
        governance_review_id=review.id,
        created_by=_STEWARD,
    )
    await _point_at(session, review, draft)
    return review


async def _column_misleading_sources(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    evidence = (
        _column_evidence(
            comment="Customer who placed the order.",
            dbt="Foreign key to the customer who placed the order.",
        )
        if truthful
        else _column_evidence(
            comment="Date the order shipped.", dbt="Carrier code for the shipment."
        )
    )
    return await _column_draft(session, org, evidence, origin=ORIGIN_METADATA)


async def _column_model_drafted(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    evidence = _column_evidence(
        comment="Customer who placed the order." if truthful else "Date the order shipped.",
        dbt="",
    )
    # Scored above the model cap on purpose: the abstention must not rest on it.
    return await _column_draft(
        session, org, evidence, origin=ORIGIN_MODEL_INFERRED, overall=0.95
    )


# --- METADATA_ENRICHMENT_PROPOSAL -----------------------------------------


def _inferred(table_name: str, key_column: str) -> TableSemanticOutput:
    """The rules engine's real proposal for a table with a primary key."""
    columns = [
        MetadataColumn(
            name=name,
            ordinal_position=position,
            status="ACTIVE",
            classification="UNCLASSIFIED",
            physical_type="integer",
            nullable=False,
        )
        for position, name in enumerate((key_column, "created_at"), start=1)
    ]
    constraint = MetadataConstraint(
        name=f"{table_name}_pk",
        constraint_type="PRIMARY_KEY",
        columns=[key_column],
        referenced_table_id=None,
        status="ACTIVE",
    )
    return infer_table_semantics(
        table=MetadataTable(id=uuid4(), name=table_name),
        schema_name="retail",
        columns=columns,
        constraints=[constraint],
    )


async def _enrichment(
    session: AsyncSession, org: Organization, output: TableSemanticOutput, *, engine_type: str
) -> GovernanceReview:
    review = await _review(session, org, "METADATA_ENRICHMENT_PROPOSAL")
    proposal = MetadataEnrichmentProposal(
        organization_id=org.id,
        datasource_id=uuid4(),
        inference_run_id=uuid4(),
        table_id=output.table_id,
        governance_review_id=review.id,
        engine_type=engine_type,
        engine_version=SEMANTIC_INFERENCE_VERSION,
        confidence=output.confidence,
        payload=output.model_dump(mode="json"),
        evidence={"model_used": engine_type == "LLM_ASSISTED"},
        fingerprint="e" * 64,
        proposed_by=_STEWARD,
    )
    await _point_at(session, review, proposal)
    return review


async def _enrichment_keyword_inside_a_word(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    output = (
        _inferred("customer", "customer_id") if truthful else _inferred("discard_log", "discard_id")
    )
    # The false twin really is false in the way named: "card" inside "discard".
    assert output.domain_key == ("CUSTOMER" if truthful else "PAYMENTS")
    return await _enrichment(session, org, output, engine_type="RULES")


async def _enrichment_keyword_with_another_meaning(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    output = (
        _inferred("counterparty", "counterparty_id")
        if truthful
        else _inferred("party_supplies", "supply_id")
    )
    assert output.domain_key == "CUSTOMER"  # right for one, wrong for the other
    return await _enrichment(session, org, output, engine_type="RULES")


async def _enrichment_model_grades_itself(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    update: dict[str, object] = {"confidence": 0.95}
    if not truthful:
        update |= {"domain_key": "PAYMENTS", "domain_name": "Payments"}
    output = _inferred("customer", "customer_id").model_copy(update=update)
    return await _enrichment(session, org, output, engine_type="LLM_ASSISTED")


# --- GLOSSARY_LINK_PROPOSAL -------------------------------------------------
# The producer emits exactly two confidences, both name equality, so these
# twins carry the producer's own constants rather than a hand-picked score.


async def _glossary_link(
    session: AsyncSession, org: Organization, *, confidence: float, table_name: str, label: str
) -> GovernanceReview:
    review = await _review(session, org, "GLOSSARY_LINK_PROPOSAL")
    proposal = GlossaryLinkProposal(
        organization_id=org.id,
        table_id=uuid4(),
        term_id=uuid4(),
        source_annotation_id=uuid4(),
        confidence=confidence,
        evidence={"matched_label": label, "table_name": table_name},
        status="PENDING_REVIEW",
        governance_review_id=review.id,
        created_by=_STEWARD,
    )
    await _point_at(session, review, proposal)
    return review


async def _glossary_name_stem(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    # `stg_revenue` holds rows a raw import rejected; its name stem is still
    # "revenue", so inference names it "Revenue" and the label matches the term.
    return await _glossary_link(
        session,
        org,
        confidence=_PRIMARY_MATCH_CONFIDENCE,
        table_name="fct_revenue" if truthful else "stg_revenue",
        label="Revenue",
    )


async def _glossary_synonym(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    return await _glossary_link(
        session,
        org,
        confidence=_SECONDARY_MATCH_CONFIDENCE,
        table_name="account_balance" if truthful else "balance_sheet_template",
        label="Balance",
    )


# --- DOCUMENT_CLAIM -----------------------------------------------------------


async def _document_claim(
    session: AsyncSession, _org: Organization, truthful: bool
) -> GovernanceReview:
    """The real upload -> map -> extract path, one dictionary row per twin."""
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    description = (
        "unique customer identifier" if truthful else "date the customer closed their account"
    )
    context = _context(project)
    document = await upload_document(
        project.id,
        DocumentCreate(
            filename="dictionary.csv",
            content=f"schema,table,column,description\npublic,customers,customer_id,{description}\n",
        ),
        context,
        session,
    )
    await map_document(document.id, context, session)
    await extract_claims(document.id, context, session)
    claim = await session.scalar(
        select(DocumentClaim).where(DocumentClaim.organization_id == project.organization_id)
    )
    assert claim is not None and claim.confidence == 1.0
    review = await session.get(GovernanceReview, claim.governance_review_id)
    assert review is not None
    return review


# --- BULK_STEWARDSHIP_OPERATION and MODEL_IMPORT_BATCH ----------------------


async def _bulk_assignment(
    session: AsyncSession, org: Organization, truthful: bool
) -> GovernanceReview:
    review = await _review(
        session,
        org,
        "BULK_STEWARDSHIP_OPERATION",
        object_id="pending",
        requested_action="ASSIGN_OWNERSHIP",
    )
    session.add(
        BulkStewardshipOperation(
            organization_id=org.id,
            operation_type="ASSIGN_OWNERSHIP",
            subject_type="TABLE",
            subject_ids=[str(uuid4()) for _ in range(3)],
            parameters={
                "owner_type": "USER",
                "owner_principal": "retail-data-steward" if truthful else "departed-contractor",
            },
            governance_review_id=review.id,
            requested_by=_STEWARD,
        )
    )
    await session.flush()
    return review


async def _workbook(session: AsyncSession, org: Organization, truthful: bool) -> GovernanceReview:
    review = await _review(session, org, "MODEL_IMPORT_BATCH")
    batch = ModelImportBatch(
        organization_id=org.id,
        datasource_id=uuid4(),
        filename="model.xlsx",
        content_sha256=("a" if truthful else "b") * 64,
        status="PENDING_REVIEW",
        governance_review_id=review.id,
        change_count=3,
        uploaded_by=_STEWARD,
    )
    await _point_at(session, review, batch)
    new_values = (
        ("Customer who placed the order.", "Order total.", "When the order was placed.")
        if truthful
        else ("Date the order shipped.", "Items returned.", "Warehouse it left from.")
    )
    for row_number, (column, new_value) in enumerate(
        zip(("customer_id", "total_amount", "ordered_at"), new_values, strict=True), start=2
    ):
        session.add(
            ModelImportChange(
                organization_id=org.id,
                batch_id=batch.id,
                sheet_name="Columns",
                row_number=row_number,
                subject_type="COLUMN",
                subject_id=str(uuid4()),
                subject_label=f"retail.orders.{column}",
                field="description",
                new_value=new_value,
            )
        )
    await session.flush()
    return review


# --- the corpus and the measurement -----------------------------------------


@dataclass(frozen=True, slots=True)
class Twin:
    object_type: str
    case: str
    seed: Seed


TWINS: tuple[Twin, ...] = (
    Twin(
        "ASSET_DESCRIPTION_DRAFT",
        "dbt description written for another table",
        _asset_misleading_dbt,
    ),
    Twin(
        "ASSET_DESCRIPTION_DRAFT",
        "approved annotation that is itself wrong",
        _asset_wrong_annotation,
    ),
    Twin(
        "COLUMN_DESCRIPTION_DRAFT",
        "source comment and dbt doc both wrong",
        _column_misleading_sources,
    ),
    Twin("COLUMN_DESCRIPTION_DRAFT", "a model wrote the draft", _column_model_drafted),
    Twin(
        "METADATA_ENRICHMENT_PROPOSAL",
        "domain keyword inside another word",
        _enrichment_keyword_inside_a_word,
    ),
    Twin(
        "METADATA_ENRICHMENT_PROPOSAL",
        "domain keyword with another meaning",
        _enrichment_keyword_with_another_meaning,
    ),
    Twin(
        "METADATA_ENRICHMENT_PROPOSAL",
        "model reports its own confidence",
        _enrichment_model_grades_itself,
    ),
    Twin("GLOSSARY_LINK_PROPOSAL", "table name stem equals a term", _glossary_name_stem),
    Twin("GLOSSARY_LINK_PROPOSAL", "synonym shared with a term", _glossary_synonym),
    Twin("DOCUMENT_CLAIM", "dictionary row describing the wrong thing", _document_claim),
    Twin("BULK_STEWARDSHIP_OPERATION", "three tables to the wrong owner", _bulk_assignment),
    Twin("MODEL_IMPORT_BATCH", "three-row workbook of wrong descriptions", _workbook),
)

_APPROVE = "APPROVE"
_COUNTS = ("pairs", "wrong_approved", "right_approved", "told_apart")

def _row(pairs: int, wrong_approved: int, right_approved: int, told_apart: int) -> dict[str, int]:
    return dict(zip(_COUNTS, (pairs, wrong_approved, right_approved, told_apart), strict=True))


#: The measured result. A change to the agent's evidence that moves any of
#: these moves this table, deliberately: update it, and the module docstring's
#: summary, in the same change, and say why in the commit.
#:
#: Per type: pairs, false twins approved, true twins approved, pairs told apart.
BASELINE: dict[str, dict[str, int]] = {
    "ASSET_DESCRIPTION_DRAFT": _row(2, 2, 2, 0),
    "COLUMN_DESCRIPTION_DRAFT": _row(2, 1, 1, 0),
    "METADATA_ENRICHMENT_PROPOSAL": _row(3, 2, 2, 0),
    "GLOSSARY_LINK_PROPOSAL": _row(2, 0, 0, 0),
    "DOCUMENT_CLAIM": _row(1, 0, 0, 0),
    "BULK_STEWARDSHIP_OPERATION": _row(1, 1, 1, 0),
    "MODEL_IMPORT_BATCH": _row(1, 1, 1, 0),
}


async def _decide(session: AsyncSession, review: GovernanceReview) -> tuple[str, float | None]:
    settings = _settings()
    assessment = await reviewer_agent._assess(
        session,
        review,
        settings=settings,
        ceiling=effective_agent_ceiling(settings.reviewer_agent_max_tier),
    )
    return assessment.recommendation, assessment.confidence


async def measure(session: AsyncSession) -> tuple[dict[str, dict[str, int]], list[str]]:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    tally: dict[str, dict[str, int]] = {}
    lines: list[str] = []
    for twin in TWINS:
        right, right_confidence = await _decide(session, await twin.seed(session, org, True))
        wrong, wrong_confidence = await _decide(session, await twin.seed(session, org, False))
        counts = tally.setdefault(twin.object_type, dict.fromkeys(_COUNTS, 0))
        counts["pairs"] += 1
        counts["wrong_approved"] += wrong == _APPROVE
        counts["right_approved"] += right == _APPROVE
        counts["told_apart"] += right == _APPROVE and wrong != _APPROVE
        lines.append(
            f"  {twin.object_type:<29} {twin.case:<44} "
            f"true={right}@{right_confidence} false={wrong}@{wrong_confidence}"
        )
    return tally, lines


async def test_the_false_approval_count_by_object_type_is_the_pinned_one(
    session: AsyncSession,
) -> None:
    measured, lines = await measure(session)

    assert measured == BASELINE, "\n".join(
        ["The agent's decisions on the twin corpus moved:", *lines, f"measured: {measured}"]
    )


def test_every_object_type_the_agent_could_approve_is_in_the_corpus() -> None:
    """A type the agent can approve and this file does not benchmark is a
    false-approval rate nobody has measured -- the state AR-03 found."""
    approvable = set(reviewer_agent._EVIDENCE_RESOLVERS) & agent_decidable_object_types(
        HARD_MAX_AGENT_TIER
    )

    assert approvable <= {twin.object_type for twin in TWINS}
