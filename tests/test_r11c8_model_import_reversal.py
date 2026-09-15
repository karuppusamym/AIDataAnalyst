"""R11-C8: an agent-approved workbook import can be reversed from its sample.

The reviewer agent may approve a `MODEL_IMPORT_BATCH` of ten changes or fewer,
and approving one publishes descriptions and business-annotation fields. A human
who disputed that decision had no correction to file from the sample: asking was
refused by name, and the remedy was to re-export, fix and re-import by hand,
with no edge back to the sample.

A reversal is an ordinary batch that puts back what the disputed one replaced.
These pin what makes it correct rather than merely available:

* applying an import records the version each change published -- the record a
  reversal is bounded by;
* disputing the sample files a reversal naming it, which no agent may decide,
  restoring each field from the version it replaced;
* approving it publishes the replaced text again, and withdraws a description
  the import added where there was none;
* a field someone changed since is skipped, not overwritten;
* a description withdrawn before the import applied is not put back, though the
  diff recorded it as the old value;
* an import that did not record its versions, a second reversal and a reversal
  of a reversal are refused;
* while the reversal waits, the disputed sample still counts as unresolved.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contract_api import ResolveSampleRequest, resolve_sample
from aida.catalog_read_model import _business_annotations, _latest_approved_documentation
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.model_export import COLUMN_SHEET, TABLE_SHEET
from aida.model_import import (
    REVERSAL_ACTION,
    request_model_import_reversal,
    submit_batch_for_review,
)
from aida.models import (
    ColumnDocumentation,
    ColumnDocumentationVersion,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
    ModelImportBatch,
    ModelImportChange,
    ReviewAuditSample,
)
from aida.reviewer_agent import _sized_risk_tier, unresolved_audit_samples
from aida.schemas import GovernanceDecisionRequest
from aida.semantic_api import decide_governance_review
from tests import test_model_import as mi

# The model-import suite's database, bound as a module attribute for pytest.
session = mi.session

AGENT = "agent:reviewer"
ORIGINAL_KEY = "The customer's original key."


@dataclass
class AppliedImport:
    table: MetadataTable
    described: MetadataColumn
    undescribed: MetadataColumn
    batch: ModelImportBatch
    sample: ReviewAuditSample

    @property
    def org_id(self) -> UUID:
        return self.table.organization_id


async def _decide(
    db: AsyncSession,
    review_id: UUID | None,
    org_id: UUID,
    principal: str,
    decision: str = "APPROVE",
) -> None:
    assert review_id is not None
    request = (
        GovernanceDecisionRequest(decision="APPROVE")
        if decision == "APPROVE"
        else GovernanceDecisionRequest(decision=decision, reason="the agent was right after all")
    )
    await decide_governance_review(review_id, request, mi._context(org_id, principal), db)


async def _agent_applies(
    db: AsyncSession, batch: ModelImportBatch, org_id: UUID
) -> ReviewAuditSample:
    """The agent approves `batch`, and its decision is sampled."""
    await submit_batch_for_review(db, batch, requested_by=mi._MAKER)
    await db.commit()
    await _decide(db, batch.governance_review_id, org_id, AGENT)
    sample = ReviewAuditSample(
        organization_id=org_id,
        governance_review_id=batch.governance_review_id,
        agent_principal_id=AGENT,
        object_type="MODEL_IMPORT_BATCH",
        risk_tier="T1",
        decision="APPROVED",
        sampled_at=datetime.now(UTC) - timedelta(hours=2),
        human_outcome="PENDING",
    )
    db.add(sample)
    await db.commit()
    return sample


async def _agent_applied_import(db: AsyncSession) -> AppliedImport:
    """An import the agent approved that overwrote one column's description,
    described a column that had none, renamed the table's business annotation
    and gave the table a readme it did not have."""
    datasource, table, described = await mi._seed(db)
    await mi._seed_annotation(db, datasource, table)
    await publish_column_description(
        db,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=described.id,
        description=ORIGINAL_KEY,
        created_by="steward-a",
        approved_by="steward-b",
        approved_at=datetime.now(UTC) - timedelta(days=1),
    )
    undescribed = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name="segment_code",
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        status="ACTIVE",
        fingerprint="fp",
    )
    db.add(undescribed)
    await db.flush()

    def edit(name: str, headers: list[str], rows: list[list[object]]) -> None:
        if name == COLUMN_SHEET:
            for row in rows:
                column = row[headers.index("column")]
                row[headers.index("business_description")] = f"Agent wording for {column}."
        elif name == TABLE_SHEET:
            rows[0][headers.index("business_name")] = "Clients"
            rows[0][headers.index("readme")] = "An agent-approved readme."

    batch = await mi._upload(db, datasource, mi._rewrite(await mi._export(db, datasource), edit))
    sample = await _agent_applies(db, batch, table.organization_id)
    return AppliedImport(table, described, undescribed, batch, sample)


async def _dispute(db: AsyncSession, org_id: UUID, sample: ReviewAuditSample) -> None:
    await resolve_sample(
        org_id,
        sample.id,
        ResolveSampleRequest(
            human_outcome="DISAGREED",
            rationale="the workbook renamed the wrong table",
            reverse_applied_changes=True,
        ),
        context=mi._context(org_id, "reviewer-h"),
        session=db,
    )


async def _reversal(db: AsyncSession, batch: ModelImportBatch) -> ModelImportBatch:
    reversal = await db.scalar(
        select(ModelImportBatch).where(ModelImportBatch.reverses_batch_id == batch.id)
    )
    assert reversal is not None
    return reversal


async def _by_field(db: AsyncSession, batch_id: UUID) -> dict[tuple[str, str], ModelImportChange]:
    return {(change.subject_id, change.field): change for change in await mi._changes(db, batch_id)}


async def _description(
    db: AsyncSession, column: MetadataColumn
) -> ColumnDocumentationVersion | None:
    return (await current_descriptions_by_column_id(db, [column.id])).get(column.id)


# --- the record a reversal is bounded by ------------------------------------


async def test_applying_an_import_records_the_version_each_change_published(
    session: AsyncSession,
) -> None:
    applied = await _agent_applied_import(session)
    changes = await _by_field(session, applied.batch.id)
    described = await _description(session, applied.described)
    undescribed = await _description(session, applied.undescribed)
    readme = (await _latest_approved_documentation(session, [applied.table.id]))[applied.table.id]
    annotation = (await _business_annotations(session, [applied.table.id]))[applied.table.id]
    assert described is not None and undescribed is not None

    assert {change.status for change in changes.values()} == {"APPLIED"}
    published = {key: change.published_version for key, change in changes.items()}
    assert published == {
        (str(applied.described.id), "business_description"): described.version,
        (str(applied.undescribed.id), "business_description"): undescribed.version,
        (str(applied.table.id), "readme"): readme.version,
        (str(applied.table.id), "business_name"): annotation.version,
    }
    assert (described.version, undescribed.version, readme.version, annotation.version) == (
        2,
        1,
        1,
        2,
    )


# --- disputing the decision files the reversal ------------------------------


async def test_disputing_the_decision_files_a_reversal_that_names_its_sample(
    session: AsyncSession,
) -> None:
    applied = await _agent_applied_import(session)

    await _dispute(session, applied.org_id, applied.sample)

    reversal = await _reversal(session, applied.batch)
    assert (reversal.status, reversal.review_audit_sample_id, reversal.uploaded_by) == (
        "PENDING_REVIEW",
        applied.sample.id,
        "reviewer-h",
    )
    review = await session.get(GovernanceReview, reversal.governance_review_id)
    assert review is not None
    assert (review.object_type, review.requested_action) == ("MODEL_IMPORT_BATCH", REVERSAL_ACTION)

    restores = await _by_field(session, reversal.id)
    assert reversal.change_count == len(restores) == 4
    overwritten = restores[(str(applied.described.id), "business_description")]
    assert (overwritten.old_value, overwritten.new_value, overwritten.expected_version) == (
        "Agent wording for customer_id.",
        ORIGINAL_KEY,
        2,
    )
    assert restores[(str(applied.table.id), "business_name")].new_value == "Customers"
    # Nothing was there before, so nothing is put back.
    assert restores[(str(applied.undescribed.id), "business_description")].new_value is None
    assert restores[(str(applied.table.id), "readme")].new_value is None


async def test_the_reversal_is_t2_so_no_agent_can_decide_it(session: AsyncSession) -> None:
    """Four changes is inside the agent's reach as an import and outside it as a
    reversal: the disputed party must not decide the dispute."""
    applied = await _agent_applied_import(session)
    await _dispute(session, applied.org_id, applied.sample)
    reversal = await _reversal(session, applied.batch)
    original_review = await session.get(GovernanceReview, applied.batch.governance_review_id)
    reversal_review = await session.get(GovernanceReview, reversal.governance_review_id)
    assert original_review is not None and reversal_review is not None

    original_tier, _ = await _sized_risk_tier(session, original_review, governance_threshold=10)
    reversal_tier, evidence = await _sized_risk_tier(
        session, reversal_review, governance_threshold=10
    )

    assert (original_tier, reversal_tier) == ("T1", "T2")
    assert evidence["reverses_batch_id"] == str(applied.batch.id)


# --- approving it ------------------------------------------------------------


async def test_approving_the_reversal_puts_back_what_the_import_replaced(
    session: AsyncSession,
) -> None:
    applied = await _agent_applied_import(session)
    await _dispute(session, applied.org_id, applied.sample)
    reversal = await _reversal(session, applied.batch)

    await _decide(session, reversal.governance_review_id, applied.org_id, "steward-c")

    await session.refresh(reversal)
    assert (reversal.status, reversal.applied_count) == ("APPLIED", 4)
    # Overwritten text is published again, as a new version.
    described = await _description(session, applied.described)
    assert described is not None
    assert (described.description, described.version) == (ORIGINAL_KEY, 3)
    # A description the import added is withdrawn, keeping its text.
    assert await _description(session, applied.undescribed) is None
    added = await session.scalar(
        select(ColumnDocumentationVersion)
        .join(
            ColumnDocumentation,
            ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
        )
        .where(ColumnDocumentation.column_id == applied.undescribed.id)
    )
    assert added is not None
    assert (added.status, added.description) == ("WITHDRAWN", "Agent wording for segment_code.")
    assert applied.table.id not in await _latest_approved_documentation(session, [applied.table.id])
    # The annotation's renamed field is put back; the fields it did not touch stay.
    annotation = (await _business_annotations(session, [applied.table.id]))[applied.table.id]
    assert (annotation.business_name, annotation.grain_statement, annotation.version) == (
        "Customers",
        "One row per customer.",
        3,
    )


async def test_a_field_someone_changed_since_is_skipped_not_overwritten(
    session: AsyncSession,
) -> None:
    applied = await _agent_applied_import(session)
    await publish_column_description(
        session,
        organization_id=applied.org_id,
        table_id=applied.table.id,
        column_id=applied.described.id,
        description="A steward's later wording.",
        created_by="steward-a",
        approved_by="steward-b",
        approved_at=datetime.now(UTC),
    )
    await session.commit()
    await _dispute(session, applied.org_id, applied.sample)
    reversal = await _reversal(session, applied.batch)

    await _decide(session, reversal.governance_review_id, applied.org_id, "steward-c")

    restores = await _by_field(session, reversal.id)
    assert restores[(str(applied.described.id), "business_description")].status == "SKIPPED_STALE"
    described = await _description(session, applied.described)
    assert described is not None and described.description == "A steward's later wording."
    assert restores[(str(applied.table.id), "business_name")].status == "APPLIED"


async def test_a_description_withdrawn_before_the_import_applied_is_not_put_back(
    session: AsyncSession,
) -> None:
    """The diff recorded the interim description as the old value, but it was
    withdrawn before approval, so the import replaced nothing. Restoring from
    `old_value` would revive text a reviewer had already retired."""
    datasource, table, column = await mi._seed(session)
    exported = await mi._export(session, datasource)
    interim = await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="An interim description.",
        created_by="steward-a",
        approved_by="steward-b",
        approved_at=datetime.now(UTC),
    )
    batch = await mi._upload(
        session,
        datasource,
        mi._rewrite(exported, mi._set_cell(COLUMN_SHEET, "business_description", "Agent wording.")),
    )
    [change] = await mi._changes(session, batch.id)
    assert (change.old_value, change.expected_version) == ("An interim description.", None)
    interim.status = "WITHDRAWN"
    await session.flush()
    sample = await _agent_applies(session, batch, table.organization_id)

    await _dispute(session, table.organization_id, sample)

    [restore] = await mi._changes(session, (await _reversal(session, batch)).id)
    assert restore.new_value is None


# --- what is refused ----------------------------------------------------------


async def test_an_import_that_did_not_record_its_versions_is_refused(
    session: AsyncSession,
) -> None:
    """How an import applied before `published_version` existed presents."""
    applied = await _agent_applied_import(session)
    for change in await mi._changes(session, applied.batch.id):
        change.published_version = None
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await _dispute(session, applied.org_id, applied.sample)

    assert refused.value.status_code == 409
    assert "did not record" in str(refused.value.detail)
    assert (
        await session.scalar(
            select(ModelImportBatch).where(ModelImportBatch.reverses_batch_id == applied.batch.id)
        )
        is None
    )


async def test_a_second_reversal_is_refused_while_one_waits(session: AsyncSession) -> None:
    applied = await _agent_applied_import(session)
    await _dispute(session, applied.org_id, applied.sample)

    with pytest.raises(HTTPException) as refused:
        await request_model_import_reversal(session, applied.batch, requested_by="steward-d")

    assert refused.value.status_code == 409
    assert "already pending" in str(refused.value.detail)


async def test_a_reversal_is_not_itself_reversed(session: AsyncSession) -> None:
    applied = await _agent_applied_import(session)
    await _dispute(session, applied.org_id, applied.sample)
    reversal = await _reversal(session, applied.batch)
    await _decide(session, reversal.governance_review_id, applied.org_id, "steward-c")

    with pytest.raises(HTTPException) as refused:
        await request_model_import_reversal(session, reversal, requested_by="steward-d")

    assert refused.value.status_code == 422


async def test_the_person_who_asked_for_the_reversal_cannot_approve_it(
    session: AsyncSession,
) -> None:
    applied = await _agent_applied_import(session)
    await _dispute(session, applied.org_id, applied.sample)
    reversal = await _reversal(session, applied.batch)

    with pytest.raises(HTTPException) as refused:
        await _decide(session, reversal.governance_review_id, applied.org_id, "reviewer-h")

    assert refused.value.status_code == 409
    await session.refresh(reversal)
    assert reversal.status == "PENDING_REVIEW"


async def test_the_disputed_sample_stays_unresolved_until_the_reversal_is_decided(
    session: AsyncSession,
) -> None:
    applied = await _agent_applied_import(session)
    await _dispute(session, applied.org_id, applied.sample)
    reversal = await _reversal(session, applied.batch)

    assert await unresolved_audit_samples(session, applied.org_id) == 1

    await _decide(session, reversal.governance_review_id, applied.org_id, "steward-c", "REJECT")

    assert await unresolved_audit_samples(session, applied.org_id) == 0
