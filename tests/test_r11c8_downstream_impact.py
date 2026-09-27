"""R11-C8: the answers that relied on a disputed agent decision while it stood.

A correction undoes the catalog change. Until this existed nothing found the
answers produced in the meantime -- the last open clause of the row. These pin
what makes the finding trustworthy rather than merely present:

* an annotation counts only by the exact version the decision published, and
  only in the window it stood -- a run before the decision, or after the
  correction took effect, is not listed, and neither is a run that merely
  retrieved the annotated table;
* a column change reaches every run that hydrated the column's table, however
  that table got into context -- a table hit, or a hit naming it as its source;
* a correction still waiting leaves the window open;
* an ownership change is reported as unable to reach an answer, not as a clean
  search;
* the scan is bounded and says when it stopped early;
* the endpoint walks the sample and refuses another organization's.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contract_api import get_sample_downstream_impact
from aida.correction_impact import ASSET_IN_CONTEXT, EXACT_VERSION, downstream_impact
from aida.models import (
    AgentRun,
    GovernanceReview,
    MetadataColumn,
    Organization,
    ReviewAuditSample,
)
from aida.semantic_api import GovernanceDecisionRequest, decide_governance_review
from aida.stewardship_service import apply_bulk_operation, request_bulk_operation_reversal
from tests import test_ar11_correction_traceability as ar11
from tests import test_r11c8_annotation_withdrawal as annotations

# The AR-11 suite's in-memory database, bound as a module attribute for pytest.
session = ar11.session

AGENT = "agent:reviewer"
NOW = ar11.NOW


def make_agent_run(
    organization_id: UUID,
    *,
    created_at: datetime,
    retrieval: Sequence[dict[str, Any]] = (),
    grounding: Sequence[dict[str, Any]] = (),
    principal_id: str = "analyst-1",
) -> AgentRun:
    """An answer, as the ledger keeps it: identifiers of what it touched, no text."""
    return AgentRun(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=uuid4(),
        principal_id=principal_id,
        status="COMPLETED",
        question_hash="q" * 64,
        generation_source="TEST",
        retrieval_evidence=list(retrieval),
        grounding_fragment_digests=list(grounding),
        created_at=created_at,
    )


def hit(object_type: str, object_id: object, **metadata: object) -> dict[str, Any]:
    return {
        "object_type": object_type,
        "object_id": str(object_id),
        "display_name": "x",
        "score": 0.5,
        "reason_codes": [],
        "metadata": {key: str(value) for key, value in metadata.items()},
    }


async def _sampled_operation(
    db: AsyncSession,
    org: Organization,
    *,
    operation_type: str,
    subject_ids: list[UUID],
    parameters: dict[str, object] | None = None,
) -> ReviewAuditSample:
    """A bulk operation the agent decided at `NOW`, applied, and sampled."""
    operation = await ar11._operation(
        db,
        org,
        operation_type=operation_type,
        subject_ids=subject_ids,
        parameters=parameters or {},
    )
    operation.status = "APPLIED"
    operation.applied_subject_ids = [str(subject) for subject in subject_ids]
    operation.applied_at = NOW
    review = await db.get(GovernanceReview, operation.governance_review_id)
    assert review is not None
    review.status = "APPROVED"
    review.decided_at = NOW
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=review.id,
        agent_principal_id=AGENT,
        object_type="BULK_STEWARDSHIP_OPERATION",
        risk_tier="T1",
        decision="APPROVED",
        sampled_at=NOW,
        human_outcome="PENDING",
    )
    db.add(sample)
    await db.commit()
    return sample


async def test_an_annotation_counts_by_the_exact_version_and_only_while_it_stood(
    session: AsyncSession,
) -> None:
    org, annotation, version, sample = await annotations._agent_approved_annotation(session)
    review = await session.get(GovernanceReview, sample.governance_review_id)
    assert review is not None
    review.decided_at = NOW
    cites = {
        "object_type": "BUSINESS_ANNOTATION",
        "object_id": str(annotation.id),
        "fragment_digest": "sha256:x",
        "annotation_version_id": str(version.id),
    }
    before = make_agent_run(org.id, created_at=NOW - timedelta(hours=3), grounding=[cites])
    cited = make_agent_run(org.id, created_at=NOW + timedelta(hours=1), grounding=[cites])
    table_only = make_agent_run(
        org.id,
        created_at=NOW + timedelta(hours=1),
        retrieval=[hit("TABLE", annotation.table_id, table_id=annotation.table_id)],
    )
    session.add_all([before, cited, table_only])
    await session.commit()

    await annotations._dispute(session, org, sample)
    withdrawal = await annotations._withdrawal(session, annotation)
    assert withdrawal is not None and withdrawal.governance_review_id is not None
    await decide_governance_review(
        withdrawal.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=ar11._context(org, "steward-c"),
        session=session,
    )
    after = make_agent_run(
        org.id, created_at=datetime.now(UTC) + timedelta(days=1), grounding=[cites]
    )
    session.add(after)
    await session.commit()

    impact = await downstream_impact(session, sample)

    assert impact.correction is not None
    assert (impact.correction.kind, impact.correction.status) == (
        "DESCRIPTION_WITHDRAWAL",
        "APPLIED",
    )
    assert impact.window_start == NOW and impact.window_end is not None
    assert [run.agent_run_id for run in impact.affected_runs] == [cited.id]
    assert impact.affected_runs[0].bases == (EXACT_VERSION,)
    assert impact.scanned_runs == 2  # the cited and table-only runs; not before, not after


async def test_a_column_change_reaches_every_run_that_hydrated_its_table(
    session: AsyncSession,
) -> None:
    org = await ar11._org(session)
    table, other = await ar11._tables(session, org, 2)
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name="national_id",
        ordinal_position=0,
        physical_type="varchar",
        nullable=True,
        status="ACTIVE",
        fingerprint="f",
    )
    session.add(column)
    await session.flush()
    sample = await _sampled_operation(
        session, org, operation_type="CLASSIFY", subject_ids=[column.id]
    )
    sibling_column = make_agent_run(
        org.id,
        created_at=NOW + timedelta(hours=1),
        retrieval=[hit("COLUMN", uuid4(), column_id=uuid4(), table_id=table.id)],
    )
    elsewhere = make_agent_run(
        org.id, created_at=NOW + timedelta(hours=1), retrieval=[hit("TABLE", other.id)]
    )
    via_source = make_agent_run(
        org.id,
        created_at=NOW + timedelta(hours=2),
        retrieval=[hit("BUSINESS_ANNOTATION", uuid4(), source_table_id=table.id)],
    )
    session.add_all([sibling_column, elsewhere, via_source])
    await session.commit()

    impact = await downstream_impact(session, sample)

    assert impact.correction is None and impact.window_end is None
    assert {run.agent_run_id for run in impact.affected_runs} == {
        sibling_column.id,
        via_source.id,
    }
    assert {run.bases for run in impact.affected_runs} == {(ASSET_IN_CONTEXT,)}
    assert impact.affected_runs[0].matched_object_ids == (str(column.id),)


async def test_a_waiting_correction_leaves_the_window_open(session: AsyncSession) -> None:
    org = await ar11._org(session)
    tables = await ar11._tables(session, org, 1)
    term = await ar11._term(session, org)
    operation = await ar11._operation(
        session,
        org,
        operation_type="LINK_TERM",
        subject_ids=[tables[0].id],
        parameters={"term_id": str(term.id)},
    )
    await apply_bulk_operation(session, operation, reviewer=AGENT, now=NOW)
    review = await session.get(GovernanceReview, operation.governance_review_id)
    assert review is not None
    review.decided_at = NOW
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=review.id,
        agent_principal_id=AGENT,
        object_type="BULK_STEWARDSHIP_OPERATION",
        risk_tier="T1",
        decision="APPROVED",
        sampled_at=NOW,
        human_outcome="DISAGREED",
    )
    session.add(sample)
    await session.flush()
    await request_bulk_operation_reversal(
        session, operation, reason="wrong term", requested_by="steward-a", sample=sample
    )
    later = make_agent_run(
        org.id,
        created_at=datetime.now(UTC) + timedelta(days=3),
        retrieval=[hit("SEMANTIC_METRIC", uuid4(), bound_term_ids=term.id)],
    )
    session.add(later)
    await session.commit()
    # `bound_term_ids` is a list on a real hit.
    later.retrieval_evidence = [
        {**later.retrieval_evidence[0], "metadata": {"bound_term_ids": [str(term.id)]}}
    ]
    await session.commit()

    impact = await downstream_impact(session, sample)

    assert impact.correction is not None and impact.correction.status == "PENDING"
    assert impact.window_end is None
    assert [run.agent_run_id for run in impact.affected_runs] == [later.id]


async def test_an_ownership_change_is_reported_as_unable_to_reach_an_answer(
    session: AsyncSession,
) -> None:
    org = await ar11._org(session)
    [table] = await ar11._tables(session, org, 1)
    sample = await _sampled_operation(
        session,
        org,
        operation_type="ASSIGN_OWNERSHIP",
        subject_ids=[table.id],
        parameters={"principal_id": "owner-1"},
    )
    session.add(
        make_agent_run(
            org.id, created_at=NOW + timedelta(hours=1), retrieval=[hit("TABLE", table.id)]
        )
    )
    await session.commit()

    impact = await downstream_impact(session, sample)

    assert impact.reaches_answers is False
    assert (impact.scanned_runs, impact.affected_runs) == (0, ())


async def test_the_scan_is_bounded_and_says_when_it_stopped(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aida.correction_impact.MAX_SCANNED_RUNS", 2)
    org = await ar11._org(session)
    [table] = await ar11._tables(session, org, 1)
    sample = await _sampled_operation(session, org, operation_type="TAG", subject_ids=[table.id])
    runs = [
        make_agent_run(
            org.id, created_at=NOW + timedelta(hours=hours), retrieval=[hit("TABLE", table.id)]
        )
        for hours in (1, 2, 3)
    ]
    session.add_all(runs)
    await session.commit()

    impact = await downstream_impact(session, sample)

    assert (impact.scanned_runs, impact.truncated) == (2, True)
    assert [run.agent_run_id for run in impact.affected_runs] == [runs[0].id, runs[1].id]


async def test_the_endpoint_reads_the_sample_and_refuses_another_organization(
    session: AsyncSession,
) -> None:
    org = await ar11._org(session)
    other = await ar11._org(session)
    [table] = await ar11._tables(session, org, 1)
    sample = await _sampled_operation(
        session, org, operation_type="CERTIFY_ASSET", subject_ids=[table.id]
    )
    run = make_agent_run(
        org.id, created_at=NOW + timedelta(hours=1), retrieval=[hit("TABLE", table.id)]
    )
    session.add(run)
    await session.commit()

    read = await get_sample_downstream_impact(
        org.id, sample.id, context=ar11._context(org, "auditor-1"), session=session
    )

    assert read.reaches_answers is True
    assert [affected.agent_run_id for affected in read.affected_runs] == [run.id]
    assert read.subjects[0].object_type == "TABLE"

    with pytest.raises(HTTPException) as unknown:
        await get_sample_downstream_impact(
            org.id, uuid4(), context=ar11._context(org, "auditor-1"), session=session
        )
    assert unknown.value.status_code == 404
    with pytest.raises(HTTPException) as foreign:
        await get_sample_downstream_impact(
            other.id, sample.id, context=ar11._context(other, "auditor-1"), session=session
        )
    assert foreign.value.status_code == 404
