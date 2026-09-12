"""AR-11: a reviewer's correction is linked to its sample, and reversible.

Row R11-C8. Three properties, in the order they depend on each other:

* **What the decision did is recorded.** `apply_bulk_operation` reported
  `applied_count` and nothing else, and every branch of it skips subjects
  already in the requested state. A LINK_TERM over three tables reporting two
  applied did not say *which* two, so no compensating action could be bounded
  to them. `applied_subject_ids` is that record.
* **The correction is bounded to it.** A reversal acts on
  `applied_subject_ids`, never on `subject_ids` -- so a link that existed
  before the operation ran is not collateral damage of undoing it. This is
  the property that makes the whole thing worth having: a correction that
  removes more than the decision added is a second wrong change.
* **The correction names its sample.** `review_audit_sample_id` carries the
  edge from the sampled agent decision to the correction raised against it,
  in both directions, without a free-text field or a timestamp join.

And the bound that keeps the loop honest: a reversal is T2 whatever its size,
so the agent whose decision is being disputed cannot wave the correction
through.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.main  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_contract_api import ResolveSampleRequest, resolve_sample
from aida.db import Base
from aida.models import (
    AssetCertification,
    AssetTermLink,
    BulkStewardshipOperation,
    GlossaryTerm,
    GovernanceReview,
    MetadataTable,
    Organization,
    ReviewAuditSample,
)
from aida.review_risk_tiers import (
    agent_decidable_object_types,
    effective_agent_ceiling,
    risk_tier_for,
    tier_at_or_below,
)
from aida.security import SecurityContext
from aida.stewardship_service import (
    apply_bulk_operation,
    request_bulk_operation_reversal,
)
from atlas.modules.catalog.service import _certification_state
from tests.support.doubles import security_context

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _tables(session: AsyncSession, org: Organization, count: int) -> list[MetadataTable]:
    rows = [
        MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=uuid4(),
            schema_id=uuid4(),
            name=f"tbl_{index}",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp",
        )
        for index in range(count)
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def _term(session: AsyncSession, org: Organization) -> GlossaryTerm:
    term = GlossaryTerm(organization_id=org.id, term_key=f"term-{uuid4().hex[:8]}")
    session.add(term)
    await session.flush()
    return term


def _context(org: Organization, principal_id: str = "steward-a") -> SecurityContext:
    return security_context(
        organization_id=org.id,
        principal_id=principal_id,
        roles=frozenset({"Reviewer", "PlatformAdmin"}),
    )


async def _operation(
    session: AsyncSession,
    org: Organization,
    *,
    operation_type: str,
    subject_ids: list[UUID],
    parameters: dict[str, object],
    requested_by: str = "agent:reviewer",
) -> BulkStewardshipOperation:
    review = GovernanceReview(
        organization_id=org.id,
        object_type="BULK_STEWARDSHIP_OPERATION",
        object_id=str(uuid4()),
        requested_action=operation_type,
        requested_by=requested_by,
    )
    session.add(review)
    await session.flush()
    operation = BulkStewardshipOperation(
        organization_id=org.id,
        operation_type=operation_type,
        subject_type="TABLE",
        subject_ids=[str(value) for value in subject_ids],
        parameters=parameters,
        status="REVIEW_REQUIRED",
        governance_review_id=review.id,
        requested_by=requested_by,
    )
    session.add(operation)
    await session.flush()
    review.object_id = str(operation.id)
    await session.flush()
    return operation


# --- 1. what the decision did is recorded -----------------------------------


async def test_the_operation_records_which_subjects_it_changed_not_which_it_was_given(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    tables = await _tables(session, org, 3)
    term = await _term(session, org)
    # One table is already linked, by a steward, long before this operation.
    session.add(
        AssetTermLink(
            organization_id=org.id,
            table_id=tables[0].id,
            term_id=term.id,
            linked_by="steward-old",
            link_type="MANUAL",
        )
    )
    await session.flush()

    operation = await _operation(
        session,
        org,
        operation_type="LINK_TERM",
        subject_ids=[table.id for table in tables],
        parameters={"term_id": str(term.id)},
    )
    _event, applied = await apply_bulk_operation(
        session, operation, reviewer="agent:reviewer", now=NOW
    )
    await session.flush()

    assert applied == 2
    # Asked for three, changed two, and says which two. The pre-existing link
    # is not in the ledger, because this operation did not create it.
    assert len(operation.subject_ids) == 3
    assert set(operation.applied_subject_ids) == {str(tables[1].id), str(tables[2].id)}
    assert operation.applied_count == len(operation.applied_subject_ids)


# --- 2. the correction is bounded to that record ----------------------------


async def test_reversing_removes_what_the_operation_added_and_nothing_else(
    session: AsyncSession,
) -> None:
    """The discriminating test for the whole change.

    If the reversal were built from `subject_ids` -- what was requested --
    it would delete the steward's own pre-existing link too, and a correction
    would have destroyed something the decision it corrects never touched.
    """
    org = await _org(session)
    tables = await _tables(session, org, 3)
    term = await _term(session, org)
    session.add(
        AssetTermLink(
            organization_id=org.id,
            table_id=tables[0].id,
            term_id=term.id,
            linked_by="steward-old",
            link_type="MANUAL",
        )
    )
    await session.flush()
    operation = await _operation(
        session,
        org,
        operation_type="LINK_TERM",
        subject_ids=[table.id for table in tables],
        parameters={"term_id": str(term.id)},
    )
    await apply_bulk_operation(session, operation, reviewer="agent:reviewer", now=NOW)
    await session.flush()

    reversal, review = await request_bulk_operation_reversal(
        session,
        operation,
        reason="the agent linked two staging tables to the Revenue term",
        requested_by="steward-a",
    )
    assert reversal.operation_type == "UNLINK_TERM"
    assert set(reversal.subject_ids) == {str(tables[1].id), str(tables[2].id)}
    assert review.object_type == "BULK_STEWARDSHIP_OPERATION"
    assert review.object_id == str(reversal.id)

    event_type, undone = await apply_bulk_operation(
        session, reversal, reviewer="reviewer-b", now=NOW + timedelta(hours=1)
    )
    await session.flush()

    assert (event_type, undone) == ("glossary.term_unlinked_bulk.v1", 2)
    surviving = (
        await session.scalars(select(AssetTermLink).where(AssetTermLink.term_id == term.id))
    ).all()
    # Exactly the steward's link, untouched -- same linker, same link type.
    assert [(row.table_id, row.linked_by, row.link_type) for row in surviving] == [
        (tables[0].id, "steward-old", "MANUAL")
    ]


async def test_withdrawing_a_certification_leaves_the_asset_uncertified_not_refused(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    [table] = await _tables(session, org, 1)
    operation = await _operation(
        session,
        org,
        operation_type="CERTIFY_ASSET",
        subject_ids=[table.id],
        parameters={
            "rationale": "looks fine",
            "expires_at": (NOW + timedelta(days=365)).isoformat(),
        },
    )
    await apply_bulk_operation(session, operation, reviewer="agent:reviewer", now=NOW)
    await session.flush()
    granted = await session.scalar(
        select(AssetCertification).where(AssetCertification.table_id == table.id)
    )
    assert granted is not None
    assert _certification_state(granted, now=NOW)[0] == "CERTIFIED"

    reversal, _review = await request_bulk_operation_reversal(
        session, operation, reason="not a certified asset", requested_by="steward-a"
    )
    event_type, undone = await apply_bulk_operation(
        session, reversal, reviewer="reviewer-b", now=NOW + timedelta(hours=1)
    )
    await session.flush()
    await session.refresh(granted)

    assert (event_type, undone) == ("certification.withdrawn_bulk.v1", 1)
    # WITHDRAWN, not REVOKED: `asset_usage_decision` reads REVOKED as a
    # standing refusal (BLOCKED), which is a stronger claim than the platform
    # is entitled to make about an attestation it merely retracted.
    assert granted.status == "WITHDRAWN"
    # And the catalog says NONE -- no claim -- rather than EXPIRED, which
    # would say the attestation ran its course, or REVOKED, which would say
    # the asset is refused.
    assert _certification_state(granted, now=NOW + timedelta(hours=2))[0] == "NONE"


@pytest.mark.parametrize(
    ("operation_type", "status", "record_effect", "expected_status", "expected_detail"),
    [
        # No sound compensating action: reversing these needs a before-image
        # of what they overwrote, which nothing captures.
        ("TAG", "APPLIED", True, 422, "no compensating action"),
        ("CLASSIFY", "APPLIED", True, 422, "no compensating action"),
        ("ASSIGN_OWNERSHIP", "APPLIED", True, 422, "no compensating action"),
        ("DEPRECATE_TERM", "APPLIED", True, 422, "no compensating action"),
        ("REASSIGN_LEAVER", "APPLIED", True, 422, "no compensating action"),
        # Never applied: there is nothing to compensate.
        ("LINK_TERM", "REJECTED", True, 409, "only an applied bulk operation"),
        # Applied before the effect ledger existed. An empty list means "not
        # recorded", never "changed nothing" -- falling back to `subject_ids`
        # would let the reversal exceed the original's blast radius.
        ("LINK_TERM", "APPLIED", False, 409, "did not record which subjects"),
    ],
)
async def test_what_cannot_be_reversed_is_refused_by_name(
    session: AsyncSession,
    operation_type: str,
    status: str,
    record_effect: bool,
    expected_status: int,
    expected_detail: str,
) -> None:
    org = await _org(session)
    [table] = await _tables(session, org, 1)
    operation = await _operation(
        session,
        org,
        operation_type=operation_type,
        subject_ids=[table.id],
        parameters={},
    )
    operation.status = status
    operation.applied_subject_ids = [str(table.id)] if record_effect else []
    await session.flush()

    with pytest.raises(HTTPException) as refused:
        await request_bulk_operation_reversal(
            session, operation, reason="wrong", requested_by="steward-a"
        )

    assert refused.value.status_code == expected_status
    assert expected_detail in str(refused.value.detail)


async def test_one_operation_cannot_be_reversed_twice(session: AsyncSession) -> None:
    org = await _org(session)
    tables = await _tables(session, org, 2)
    term = await _term(session, org)
    operation = await _operation(
        session,
        org,
        operation_type="LINK_TERM",
        subject_ids=[table.id for table in tables],
        parameters={"term_id": str(term.id)},
    )
    await apply_bulk_operation(session, operation, reviewer="agent:reviewer", now=NOW)
    await session.flush()
    await request_bulk_operation_reversal(
        session, operation, reason="first", requested_by="steward-a"
    )

    with pytest.raises(HTTPException) as refused:
        await request_bulk_operation_reversal(
            session, operation, reason="second", requested_by="steward-b"
        )

    assert refused.value.status_code == 409
    assert "already pending or applied" in str(refused.value.detail)


# --- 3. the correction names its sample -------------------------------------


async def _sampled_bulk_decision(
    session: AsyncSession, org: Organization
) -> tuple[BulkStewardshipOperation, ReviewAuditSample, list[MetadataTable]]:
    """An agent-approved bulk term-link, applied, and sampled for audit."""
    tables = await _tables(session, org, 2)
    term = await _term(session, org)
    operation = await _operation(
        session,
        org,
        operation_type="LINK_TERM",
        subject_ids=[table.id for table in tables],
        parameters={"term_id": str(term.id)},
    )
    await apply_bulk_operation(session, operation, reviewer="agent:reviewer", now=NOW)
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=operation.governance_review_id,
        agent_principal_id="agent:reviewer",
        object_type="BULK_STEWARDSHIP_OPERATION",
        risk_tier="T1",
        decision="APPROVED",
        sampled_at=datetime.now(UTC) - timedelta(hours=2),
        human_outcome="PENDING",
    )
    session.add(sample)
    await session.flush()
    return operation, sample, tables


async def test_disputing_a_sample_can_raise_a_correction_that_names_it(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    operation, sample, tables = await _sampled_bulk_decision(session, org)
    await session.commit()

    await resolve_sample(
        org.id,
        sample.id,
        ResolveSampleRequest(
            human_outcome="DISAGREED",
            rationale="both tables are staging copies, not the revenue book",
            reverse_applied_changes=True,
        ),
        context=_context(org, "reviewer-h"),
        session=session,
    )

    reversal = await session.scalar(
        select(BulkStewardshipOperation).where(
            BulkStewardshipOperation.reverses_operation_id == operation.id
        )
    )
    assert reversal is not None
    # Forward: from the sample to every correction raised against it.
    by_sample = (
        await session.scalars(
            select(BulkStewardshipOperation).where(
                BulkStewardshipOperation.review_audit_sample_id == sample.id
            )
        )
    ).all()
    assert [row.id for row in by_sample] == [reversal.id]
    # Back: from the correction to the sampled decision, its review, and the
    # agent that made it -- by foreign key, not by timestamp.
    assert reversal.review_audit_sample_id == sample.id
    assert reversal.reverses_operation_id == operation.id
    linked_sample = await session.get(ReviewAuditSample, reversal.review_audit_sample_id)
    assert linked_sample is not None
    assert linked_sample.governance_review_id == operation.governance_review_id
    assert linked_sample.agent_principal_id == "agent:reviewer"
    # And the correction is bounded to what the disputed decision applied.
    assert set(reversal.subject_ids) == {str(table.id) for table in tables}
    assert reversal.status == "REVIEW_REQUIRED"


async def test_a_correction_is_not_raised_without_being_asked_for(
    session: AsyncSession,
) -> None:
    """Disagreeing and undoing are two judgements, so the second is opt-in."""
    org = await _org(session)
    operation, sample, _tables = await _sampled_bulk_decision(session, org)
    await session.commit()

    await resolve_sample(
        org.id,
        sample.id,
        ResolveSampleRequest(human_outcome="DISAGREED", rationale="wrong, but leave it"),
        context=_context(org, "reviewer-h"),
        session=session,
    )

    assert (
        await session.scalar(
            select(BulkStewardshipOperation).where(
                BulkStewardshipOperation.reverses_operation_id == operation.id
            )
        )
        is None
    )


async def test_an_object_type_with_no_compensating_action_says_so(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    review = GovernanceReview(
        organization_id=org.id,
        object_type="METADATA_ENRICHMENT_PROPOSAL",
        object_id=str(uuid4()),
        requested_action="APPLY",
        requested_by="agent:reviewer",
    )
    session.add(review)
    await session.flush()
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=review.id,
        agent_principal_id="agent:reviewer",
        object_type="METADATA_ENRICHMENT_PROPOSAL",
        risk_tier="T0",
        decision="APPROVED",
        sampled_at=datetime.now(UTC) - timedelta(hours=2),
        human_outcome="PENDING",
    )
    session.add(sample)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await resolve_sample(
            org.id,
            sample.id,
            ResolveSampleRequest(
                human_outcome="DISAGREED",
                rationale="the annotation invented a system of record",
                reverse_applied_changes=True,
            ),
            context=_context(org, "reviewer-h"),
            session=session,
        )

    assert refused.value.status_code == 422
    assert "METADATA_ENRICHMENT_PROPOSAL" in str(refused.value.detail)


async def test_a_reversal_cannot_accompany_an_agreement(session: AsyncSession) -> None:
    org = await _org(session)
    _operation, sample, _tables = await _sampled_bulk_decision(session, org)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await resolve_sample(
            org.id,
            sample.id,
            ResolveSampleRequest(
                human_outcome="AGREED",
                rationale="checked it",
                reverse_applied_changes=True,
            ),
            context=_context(org, "reviewer-h"),
            session=session,
        )

    assert refused.value.status_code == 422
    assert "DISAGREED" in str(refused.value.detail)


# --- the bound that keeps the loop honest -----------------------------------


@pytest.mark.parametrize("configured_ceiling", ["T0", "T1", "T2", "T3"])
def test_a_reversal_is_never_agent_decidable_whatever_its_size(
    configured_ceiling: str,
) -> None:
    small = {"item_count": 2, "governance_threshold": 10}
    ceiling = effective_agent_ceiling(configured_ceiling)
    # The allowlist is per *type*, and BULK_STEWARDSHIP_OPERATION is a T1
    # type -- so it is on the list at every ceiling that admits T1 at all,
    # reversal or not. What excludes a reversal is the per-item tier the agent
    # recomputes, checked against the same ceiling below.
    assert ("BULK_STEWARDSHIP_OPERATION" in agent_decidable_object_types(ceiling)) is (
        configured_ceiling != "T0"
    )

    ordinary = risk_tier_for("BULK_STEWARDSHIP_OPERATION", small)
    reversal = risk_tier_for(
        "BULK_STEWARDSHIP_OPERATION", {**small, "reverses_operation_id": str(uuid4())}
    )
    assert (ordinary, reversal) == ("T1", "T2")
    # Two subjects either way. The ordinary one is decidable at any ceiling
    # from T1 up; the reversal is decidable at none of them, because the
    # ceiling in force clamps to T1 and T2 is never at or below T1.
    assert tier_at_or_below(ordinary, ceiling) is (configured_ceiling != "T0")
    assert tier_at_or_below(reversal, ceiling) is False


async def test_the_agent_recomputes_the_reversal_tier_from_the_row(
    session: AsyncSession,
) -> None:
    """AR-02's rule applied to this flag: the tier is read from the row the
    platform wrote, never from anything a caller supplied."""
    from aida.reviewer_agent import _sized_risk_tier

    org = await _org(session)
    tables = await _tables(session, org, 2)
    term = await _term(session, org)
    original = await _operation(
        session,
        org,
        operation_type="LINK_TERM",
        subject_ids=[table.id for table in tables],
        parameters={"term_id": str(term.id)},
    )
    await apply_bulk_operation(session, original, reviewer="agent:reviewer", now=NOW)
    await session.flush()
    ordinary_review = await session.get(GovernanceReview, original.governance_review_id)
    assert ordinary_review is not None

    reversal, reversal_review = await request_bulk_operation_reversal(
        session, original, reason="wrong", requested_by="steward-a"
    )
    await session.flush()

    ordinary_tier, _ = await _sized_risk_tier(
        session, ordinary_review, governance_threshold=10
    )
    reversal_tier, evidence = await _sized_risk_tier(
        session, reversal_review, governance_threshold=10
    )

    # Same object type, same size, different tier -- and the reason is in the
    # evidence an auditor reads back.
    assert (ordinary_tier, reversal_tier) == ("T1", "T2")
    assert evidence["reverses_operation_id"] == str(reversal.reverses_operation_id)
