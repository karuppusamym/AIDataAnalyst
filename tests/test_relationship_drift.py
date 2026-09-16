"""R11-FP06: an approved join whose evidence is gone stops being used until a person decides again.

Drift used to be reported and nothing more, so a join approved on a key the source has since
dropped kept feeding every consumer of approved joins. These tests pin:

* nothing is checked until a scan of the join's datasource finishes after its approval, and a
  join is checked once per scan;
* a join whose key is dropped is suspended to PENDING, keeping the approval it held, and is
  restored under its original approver once the key is back exactly as approved;
* a join whose evidence only changed stays approved, and one whose column left is suspended;
* a composite join is suspended the same way;
* the rebuild pass runs the check and brings the organization into the scheduled pass.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.context_rebuild import organizations_needing_rebuild, run_context_rebuild
from aida.db import Base
from aida.intelligence_api import (
    decide_composite_relationship_candidate,
    decide_relationship_candidate,
)
from aida.models import (
    AnalysisRun,
    DataSource,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    RelationshipCandidateGroup,
    RelationshipCandidateGroupMember,
)
from aida.relationship_drift import (
    DRIFT_COLUMNS_MISSING,
    DRIFT_CORROBORATION_LOST,
    RELATIONSHIP_DRIFT_PRINCIPAL,
    check_relationship_drift,
    relationship_drift_pending,
)
from aida.schemas import RelationshipCandidateDecision
from tests.test_relationship_intelligence_review import _context, _table_with_column
from tests.test_relationship_validation import _candidate, _column, _key, _source


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _scan(session: AsyncSession, datasource: DataSource, finished_at: datetime) -> None:
    session.add(
        AnalysisRun(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            mode="FULL",
            trigger_type="MANUAL",
            status="COMPLETED",
            created_at=finished_at,
            updated_at=finished_at,
        )
    )
    await session.flush()


async def _target_key(session: AsyncSession, table: MetadataTable) -> MetadataConstraint:
    key = await session.scalar(
        select(MetadataConstraint).where(MetadataConstraint.table_id == table.id)
    )
    assert key is not None
    return key


async def test_a_join_whose_key_is_dropped_is_suspended_and_restored_when_the_key_returns(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, target_table = await _candidate(session, org, datasource, target_key=True)
    reviewer = _context(org, "reviewer")
    await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="APPROVE", reason="The customer key."),
        context=reviewer,
        session=session,
        settings=Settings(),
    )
    start = datetime.now(UTC) + timedelta(minutes=1)

    # No scan has finished since the approval: nothing to check.
    assert org.id not in await relationship_drift_pending(session)
    assert (await check_relationship_drift(session, org.id, now=start)).checked == 0

    (await _target_key(session, target_table)).status = "DEPRECATED"
    await _scan(session, datasource, start + timedelta(minutes=1))
    assert org.id in await relationship_drift_pending(session)
    lost = await check_relationship_drift(session, org.id, now=start + timedelta(minutes=2))

    assert (lost.checked, lost.suspended, lost.restored) == (1, 1, 0)
    await session.refresh(candidate)
    assert (candidate.status, candidate.reviewed_by) == ("PENDING", RELATIONSHIP_DRIFT_PRINCIPAL)
    drift = candidate.evidence["drift"]
    assert (drift["state"], drift["approved_by"]) == (
        DRIFT_CORROBORATION_LOST,
        reviewer.principal_id,
    )
    # Checked once per scan.
    assert (
        await check_relationship_drift(session, org.id, now=start + timedelta(minutes=3))
    ).checked == 0

    (await _target_key(session, target_table)).status = "ACTIVE"
    await _scan(session, datasource, start + timedelta(minutes=4))
    back = await check_relationship_drift(session, org.id, now=start + timedelta(minutes=5))

    assert (back.checked, back.suspended, back.restored) == (1, 0, 1)
    await session.refresh(candidate)
    assert (candidate.status, candidate.reviewed_by, candidate.review_reason) == (
        "APPROVED",
        reviewer.principal_id,
        "The customer key.",
    )
    assert "drift" not in candidate.evidence


async def test_a_changed_join_stays_approved_and_one_whose_column_left_is_suspended(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    changed, _ = await _candidate(session, org, datasource, target_key=True)
    gone, _ = await _candidate(session, org, datasource, target_key=True)
    reviewer = _context(org, "reviewer")
    for candidate in (changed, gone):
        await decide_relationship_candidate(
            candidate.id,
            RelationshipCandidateDecision(decision="APPROVE"),
            context=reviewer,
            session=session,
            settings=Settings(),
        )
    referencing = await session.get(MetadataColumn, changed.source_column_id)
    departed = await session.get(MetadataColumn, gone.target_column_id)
    assert referencing is not None and departed is not None
    referencing.nullable = True
    departed.status = "DEPRECATED"
    start = datetime.now(UTC) + timedelta(minutes=1)
    await _scan(session, datasource, start)

    outcome = await check_relationship_drift(session, org.id, now=start + timedelta(minutes=1))

    assert (outcome.checked, outcome.suspended, outcome.restored) == (2, 1, 0)
    await session.refresh(changed)
    await session.refresh(gone)
    assert changed.status == "APPROVED"
    assert (gone.status, gone.evidence["drift"]["state"]) == ("PENDING", DRIFT_COLUMNS_MISSING)


async def test_a_composite_join_is_suspended_when_the_key_covering_it_is_dropped(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    source_table, source_branch = await _table_with_column(
        session,
        org,
        datasource,
        table_name="loan",
        column_name="branch_code",
        physical_type="INTEGER",
    )
    target_table, target_branch = await _table_with_column(
        session,
        org,
        datasource,
        table_name="account",
        column_name="branch_code",
        physical_type="INTEGER",
    )
    source_account = await _column(session, org, source_table, "account_no", 2)
    target_account = await _column(session, org, target_table, "account_no", 2)
    group = RelationshipCandidateGroup(
        organization_id=org.id,
        datasource_id=datasource.id,
        source_table_id=source_table.id,
        target_table_id=target_table.id,
        member_fingerprint="c" * 64,
        member_count=2,
        detection_rule="COMPOSITE_EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.7,
        evidence={},
        created_by="maker",
    )
    session.add(group)
    await session.flush()
    pairs = ((source_branch, target_branch), (source_account, target_account))
    for ordinal, (source, target) in enumerate(pairs):
        session.add(
            RelationshipCandidateGroupMember(
                group_id=group.id,
                ordinal=ordinal,
                source_column_id=source.id,
                target_column_id=target.id,
            )
        )
    await _key(session, org, datasource, target_table, "branch_code", "account_no")
    await decide_composite_relationship_candidate(
        group.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=_context(org, "reviewer"),
        session=session,
        settings=Settings(),
    )
    (await _target_key(session, target_table)).status = "DEPRECATED"
    start = datetime.now(UTC) + timedelta(minutes=1)
    await _scan(session, datasource, start)

    outcome = await check_relationship_drift(session, org.id, now=start + timedelta(minutes=1))

    assert (outcome.checked, outcome.suspended) == (1, 1)
    await session.refresh(group)
    assert (group.status, group.evidence["drift"]["state"]) == ("PENDING", DRIFT_CORROBORATION_LOST)


async def test_the_rebuild_pass_checks_joins_and_brings_their_organization_in(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, target_table = await _candidate(session, org, datasource, target_key=True)
    await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=_context(org, "reviewer"),
        session=session,
        settings=Settings(),
    )
    (await _target_key(session, target_table)).status = "DEPRECATED"
    start = datetime.now(UTC) + timedelta(minutes=1)
    await _scan(session, datasource, start)
    assert org.id in await organizations_needing_rebuild(session)

    outcome = await run_context_rebuild(
        session, org.id, settings=Settings(), now=start + timedelta(minutes=1)
    )

    assert (outcome.joins_suspended, outcome.joins_restored) == (1, 0), outcome.as_details()
    assert org.id not in await organizations_needing_rebuild(session)
