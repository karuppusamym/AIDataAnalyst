"""R11-REV01: a batch decision's progress is durable per member, and resumable.

The first slice decided a batch in one transaction: a failure at member 900 of 1,000 threw
away the 899 decisions before it, and nothing told the reviewer where it had got to. Now the
decision commits per chunk, each member's outcome row is written inside the savepoint that
decides it, and calling the decision again resumes at the first undecided member. What these
tests pin, on SQLite (the concurrent shapes are raced on PostgreSQL in
`tests/test_review_batches_postgres.py`):

* an interruption keeps every chunk committed before it -- decisions, outcome rows, audit
  and outbox -- and nothing of the chunk it interrupted;
* the batch then reads `resumable`, with the undecided members counted as PENDING;
* resuming decides only what is left: no member is decided twice, audited twice or
  published twice, and members recorded earlier are reported as carried over;
* the decision is fixed by the first call -- resuming with the other one is refused;
* an interruption after the last member but before the batch closed resumes to a close
  that decides nothing.

The engine uses SQLAlchemy's aiosqlite BEGIN recipe: pysqlite otherwise defers BEGIN, and a
member savepoint opened first would be the outermost transaction, committing on release --
which would make "the interrupted chunk left nothing behind" untestable here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.review_batches as review_batches_module
from aida import review_batch_models  # noqa: F401 -- registers the batch tables
from aida.db import Base
from aida.governance_decision_service import decide_review as real_decide_review
from aida.models import AuditEvent, GovernanceReview, OutboxEvent
from aida.review_batch_api import decide_frozen_review_batch, read_review_batch
from aida.review_batch_models import ReviewBatch, ReviewBatchItem
from aida.review_batch_schemas import ReviewBatchDecisionCreate
from aida.review_batches import (
    ReviewBatchError,
    Selection,
    decide_review_batch,
    freeze_review_batch,
)
from tests.support.review_batch_estate import (
    add_bare_review,
    add_table_draft_reviews,
    add_tables,
    build_estate,
    reviewer,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _explicit_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _WorkerDied(RuntimeError):
    """Stands in for anything that ends a decision part-way: a dropped connection, a killed
    worker, a request timeout, an unexpected error in one member's target."""


def _dies_at(review_id: UUID) -> Any:
    async def dying_decide_review(session_, review, **kwargs):  # type: ignore[no-untyped-def]
        if review.id == review_id:
            raise _WorkerDied("the worker died mid-chunk")
        return await real_decide_review(session_, review, **kwargs)

    return dying_decide_review


async def _statuses(session: AsyncSession, review_ids: list[UUID]) -> list[str]:
    rows = dict(
        (
            await session.execute(
                select(GovernanceReview.id, GovernanceReview.status).where(
                    GovernanceReview.id.in_(review_ids)
                )
            )
        ).all()
    )
    return [rows[review_id] for review_id in review_ids]


async def _member_outcomes(session: AsyncSession, batch_id: UUID) -> list[str]:
    return list(
        (
            await session.scalars(
                select(ReviewBatchItem.outcome)
                .where(ReviewBatchItem.batch_id == batch_id)
                .order_by(ReviewBatchItem.position)
            )
        ).all()
    )


async def _decision_audits(session: AsyncSession, review_ids: list[UUID]) -> dict[str, int]:
    rows = (
        await session.execute(
            select(AuditEvent.resource_id, func.count())
            .where(
                AuditEvent.action == "governance.review.batch_decide",
                AuditEvent.resource_id.in_([str(value) for value in review_ids]),
            )
            .group_by(AuditEvent.resource_id)
        )
    ).all()
    return {str(resource_id): int(count) for resource_id, count in rows}


async def test_an_interrupted_decision_keeps_its_committed_chunks_and_resumes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await build_estate(session)
    reviews = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 7))
    review_ids = [review.id for review in reviews]
    object_ids = [review.object_id for review in reviews]
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(rid) for rid in review_ids], filt=None
    )
    batch_id = batch.id
    await session.commit()

    # Chunks of three: members 0-2 commit; the worker dies deciding member 5, after members 3
    # and 4 were decided in their own savepoints -- inside the chunk that never commits.
    monkeypatch.setattr(review_batches_module, "decide_review", _dies_at(review_ids[5]))
    with pytest.raises(_WorkerDied):
        await decide_review_batch(
            session,
            context=context,
            batch_id=batch_id,
            decision="APPROVE",
            reason="evidence read",
            chunk_size=3,
        )
    await session.rollback()

    assert await _statuses(session, review_ids) == ["APPROVED"] * 3 + ["PENDING"] * 4
    assert await _member_outcomes(session, batch_id) == ["APPLIED"] * 3 + ["PENDING"] * 4
    assert await _decision_audits(session, review_ids) == {str(rid): 1 for rid in review_ids[:3]}
    published = await session.scalar(
        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.aggregate_id.in_(object_ids))
    )
    assert published == 3
    interrupted = await read_review_batch(batch_id, context=context, session=session)
    assert (interrupted.status, interrupted.decision, interrupted.resumable) == (
        "FROZEN",
        "APPROVE",
        True,
    )
    assert interrupted.outcome_counts == {"APPLIED": 3, "PENDING": 4}

    # Resume: the same request again, with the worker healthy.
    monkeypatch.setattr(review_batches_module, "decide_review", real_decide_review)
    resumed = await decide_frozen_review_batch(
        batch_id,
        ReviewBatchDecisionCreate(decision="APPROVE", reason="evidence read"),
        context=context,
        session=session,
    )
    assert resumed.resumed is True
    assert resumed.overall == "SUCCESS"
    assert resumed.applied_count == 7
    assert resumed.decided_in_this_call_count == 4
    assert [m.decided_in_this_call for m in resumed.members] == [False] * 3 + [True] * 4
    assert resumed.batch.status == "DECIDED"
    assert resumed.batch.resumable is False
    assert resumed.batch.outcome_counts == {"APPLIED": 7}

    # Nothing was decided twice: one decision audit and one outbox event per member.
    assert await _statuses(session, review_ids) == ["APPROVED"] * 7
    assert await _decision_audits(session, review_ids) == {str(rid): 1 for rid in review_ids}
    published = await session.scalar(
        select(func.count()).select_from(OutboxEvent).where(OutboxEvent.aggregate_id.in_(object_ids))
    )
    assert published == 7
    batch_audits = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "governance_review.batch_decide",
                AuditEvent.resource_id == str(batch_id),
            )
        )
    ).all()
    [closing] = batch_audits  # the batch is audited once, when it closes
    assert closing.details["resumed"] is True
    assert closing.details["decided_in_this_call"] == 4
    assert closing.details["applied_count"] == 7


async def test_resuming_with_the_other_decision_is_refused(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await build_estate(session)
    reviews = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 4))
    review_ids = [review.id for review in reviews]
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(rid) for rid in review_ids], filt=None
    )
    batch_id = batch.id
    await session.commit()
    monkeypatch.setattr(review_batches_module, "decide_review", _dies_at(review_ids[2]))
    with pytest.raises(_WorkerDied):
        await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
            chunk_size=2,
        )
    await session.rollback()
    monkeypatch.setattr(review_batches_module, "decide_review", real_decide_review)

    with pytest.raises(ReviewBatchError) as mismatch:
        await decide_review_batch(
            session,
            context=context,
            batch_id=batch_id,
            decision="REJECT",
            reason="changed my mind half-way",
        )
    assert (mismatch.value.code, mismatch.value.http_status) == (
        "REVIEW_BATCH_DECISION_MISMATCH",
        409,
    )
    await session.rollback()
    # The refused call changed nothing: two approved, two still pending, still resumable.
    assert await _statuses(session, review_ids) == ["APPROVED"] * 2 + ["PENDING"] * 2
    state = await read_review_batch(batch_id, context=context, session=session)
    assert (state.decision, state.resumable) == ("APPROVE", True)

    finished = await decide_review_batch(
        session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
        chunk_size=2,
    )
    assert finished.applied_count == 4
    with pytest.raises(ReviewBatchError) as again:
        await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None
        )
    assert again.value.code == "REVIEW_BATCH_ALREADY_DECIDED"


async def test_a_member_that_fails_the_same_way_every_time_is_reported_stuck_not_repeated(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first failure at a member is an ordinary, unlabelled interruption -- indistinguishable
    from a crash, exactly as `test_an_interrupted_decision_keeps_its_committed_chunks_and_resumes`
    pins. But a *second* resume that fails unhandled at that *same* member (the target is
    genuinely broken, not merely a one-off crash) is reported as `REVIEW_BATCH_STUCK_AT_MEMBER`
    instead of the same raw failure repeated with no way to tell it apart from the first -- and
    once the underlying problem is fixed, the very next resume decides the batch normally, so the
    label never blocks a real retry from succeeding.
    """
    estate = await build_estate(session)
    reviews = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 4))
    review_ids = [review.id for review in reviews]
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(rid) for rid in review_ids], filt=None
    )
    batch_id = batch.id
    await session.commit()

    # Every attempt dies at the same member: a permanently broken target, not a one-off crash.
    monkeypatch.setattr(review_batches_module, "decide_review", _dies_at(review_ids[2]))

    with pytest.raises(_WorkerDied):
        await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
            chunk_size=2,
        )
    await session.rollback()
    assert await _statuses(session, review_ids) == ["APPROVED"] * 2 + ["PENDING"] * 2

    # Resuming hits the identical failure again -- reported distinctly, not repeated raw, and
    # nothing new is recorded (the attempt still failed, just labelled differently).
    with pytest.raises(ReviewBatchError) as stuck:
        await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
            chunk_size=2,
        )
    assert (stuck.value.code, stuck.value.http_status) == ("REVIEW_BATCH_STUCK_AT_MEMBER", 409)
    await session.rollback()
    assert await _statuses(session, review_ids) == ["APPROVED"] * 2 + ["PENDING"] * 2
    still_stuck = await read_review_batch(batch_id, context=context, session=session)
    assert (still_stuck.status, still_stuck.resumable) == ("FROZEN", True)

    # A second consecutive stuck report, still without ever attempting a third identical crash
    # being any different -- the label persists while the failure keeps recurring.
    with pytest.raises(ReviewBatchError) as still_stuck_error:
        await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
            chunk_size=2,
        )
    assert still_stuck_error.value.code == "REVIEW_BATCH_STUCK_AT_MEMBER"
    await session.rollback()

    # The underlying problem is fixed: the very next resume decides the batch normally, proving
    # the label never blocks a real retry from succeeding once the cause is gone.
    monkeypatch.setattr(review_batches_module, "decide_review", real_decide_review)
    healed = await decide_review_batch(
        session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
        chunk_size=2,
    )
    assert healed.applied_count == 4
    assert healed.batch.status == "DECIDED"
    assert await _statuses(session, review_ids) == ["APPROVED"] * 4


async def test_an_interruption_before_the_close_resumes_to_a_close_that_decides_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every member recorded, the batch not yet closed: resuming decides no member again, marks
    the excluded member SKIPPED, and closes the batch."""
    estate = await build_estate(session)
    reviews = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 3))
    gone = await add_bare_review(session, estate, "GLOSSARY_CONFLICT")
    gone.status = "APPROVED"  # excluded at freeze as NOT_PENDING
    await session.flush()
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session,
        context=context,
        selections=[*(Selection(r.id) for r in reviews), Selection(gone.id)],
        filt=None,
    )
    batch_id = batch.id
    review_ids = [r.id for r in reviews]
    await session.commit()

    real_close = review_batches_module._close_batch

    async def dying_close(*args: Any, **kwargs: Any) -> None:
        raise _WorkerDied("died closing the batch")

    monkeypatch.setattr(review_batches_module, "_close_batch", dying_close)
    with pytest.raises(_WorkerDied):
        await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None
        )
    await session.rollback()
    assert await _member_outcomes(session, batch_id) == ["APPLIED"] * 3 + ["PENDING"]
    status = await session.scalar(select(ReviewBatch.status).where(ReviewBatch.id == batch_id))
    assert status == "FROZEN"

    monkeypatch.setattr(review_batches_module, "_close_batch", real_close)
    closed = await decide_review_batch(
        session, context=context, batch_id=batch_id, decision="APPROVE", reason=None
    )
    assert closed.resumed is True
    assert [o.outcome for o in closed.outcomes] == ["APPLIED"] * 3 + ["SKIPPED"]
    assert [o.decided_in_this_call for o in closed.outcomes] == [False] * 3 + [True]
    assert closed.outcomes[-1].reason_code == "NOT_PENDING"
    assert closed.batch.status == "DECIDED"
    assert await _decision_audits(session, review_ids) == {str(rid): 1 for rid in review_ids}
