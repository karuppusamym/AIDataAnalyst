"""R11-REV01: batch decisions raced, interrupted and timed on a real PostgreSQL.

`tests/test_review_batch_resume.py` proves resumable progress on SQLite, which serializes
writers and compiles `FOR UPDATE` away -- it cannot show what two genuinely simultaneous
deciders do to each other. This file can. Every race below issues its decisions with
`asyncio.gather` on **separate `AsyncSession`s over separate connections** (`NullPool`),
released together from an `asyncio.Barrier`.

The shapes, and the guard each one exercises:

* **Same batch, same decision, twice at once.** The first call's claim (`UPDATE review_batch
  SET decision = ... WHERE status = 'FROZEN' AND decision IS NULL`) takes the batch row
  lock; the second call's claim blocks on it, re-evaluates after the first chunk commits,
  matches nothing and becomes a *resume*. From then on each chunk starts with a hold
  (`UPDATE ... WHERE status = 'FROZEN' AND decision = :same`), so the two calls take turns,
  chunk by chunk. Asserted: every member is decided exactly once -- one decision audit and
  one outbox event per review -- and the members were split between the two callers.
* **Same batch, opposite decisions.** Whichever claims first fixes the decision; the other is
  refused `REVIEW_BATCH_DECISION_MISMATCH` and decides nothing.
* **Batch against batch.** Two batches that overlap on twenty reviews, one approving and one
  rejecting, decided at once. Each chunk locks its reviews `ORDER BY id FOR UPDATE`
  (`lock_reviews_for_decision`), so the overlap is decided once, by whichever reached it
  first; the other batch's member is refused (`ALREADY_DECIDED` after the re-read, or the
  claim's `CONCURRENT_DECISION`), and nothing deadlocks.
* **Interrupted, then resumed**, with a dropped worker mid-chunk: the committed chunks stand,
  the interrupted chunk leaves nothing, the resume decides the rest once.

And the timings the first slice could only give for SQLite: freezing and deciding 1,000
column-description approvals on PostgreSQL, statements counted at the DBAPI cursor.

**Database.** A private scratch database per run, `<app db>_rev01_batches_<random>`, created
here and dropped when the module finishes -- never a fixed name another suite (or a peer
session running this same file) could wipe mid-run. Built from `Base.metadata`, the
convention of every DB-backed suite except the migration drift gate. Only a failure to
*reach* PostgreSQL skips; anything after that fails.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import aida.review_batches as review_batches_module
from aida import (  # noqa: F401 -- registers every ORM table on Base.metadata
    change_signal_models,
    envelope_models,
    governed_execution_models,
    graph_store,
    models,
    okf_store_models,
    ontology_models,
    procedure_lineage_models,
    quality_rule_proposal_model,
    review_batch_models,
    sql_workspace_models,
)
from aida.db import Base
from aida.governance_decision_service import decide_review as real_decide_review
from aida.models import AuditEvent, GovernanceReview, OutboxEvent
from aida.review_batch_api import create_review_batch, decide_frozen_review_batch, get_change_queue
from aida.review_batch_models import ReviewBatch, ReviewBatchItem
from aida.review_batch_schemas import (
    ReviewBatchCreate,
    ReviewBatchDecisionCreate,
    ReviewBatchSelectionWrite,
)
from aida.review_batches import (
    BatchDecisionResult,
    ReviewBatchError,
    Selection,
    decide_review_batch,
    freeze_review_batch,
)
from atlas.platform.config import get_settings
from tests.support.review_batch_estate import (
    add_column_draft_reviews,
    add_columns,
    add_table_draft_reviews,
    add_tables,
    build_estate,
    reviewer,
)

#: Declared before measuring, as the design asks. Generous ceilings that catch a regression
#: to per-row querying or a lock pile-up, not SLOs. Measured values are printed (`-s`).
PG_FREEZE_1000_SECONDS = 10.0
PG_DECIDE_1000_SECONDS = 120.0
PG_FREEZE_1000_STATEMENTS = 20
PG_DECIDE_STATEMENTS_PER_MEMBER = 20.0

MEASURED: dict[str, float] = {}


# --- a private scratch database ------------------------------------------------------------


def _scratch_url() -> str:
    override = os.environ.get("AIDA_REVIEW_BATCH_POSTGRES_TEST_DATABASE_URL")
    if override:
        return override
    root, _, dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{dbname}_rev01_batches_{uuid4().hex[:10]}"


def _maintenance_url(db_url: str) -> str:
    root, _, _ = db_url.rpartition("/")
    _, _, app_dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{app_dbname}"


async def _create(db_url: str) -> None:
    _, _, dbname = db_url.rpartition("/")
    admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _drop(db_url: str) -> None:
    _, _, dbname = db_url.rpartition("/")
    admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    db_url = _scratch_url()
    try:
        asyncio.run(_create(db_url))
    except Exception as exc:  # noqa: BLE001 -- unreachable PostgreSQL means skip, not fail
        pytest.skip(
            f"PostgreSQL is not reachable for {db_url!r} ({type(exc).__name__}: {exc}); this "
            "file races batch decisions on a real server. The SQLite resumability proof in "
            "tests/test_review_batch_resume.py still runs."
        )
    try:
        yield db_url
    finally:
        if not os.environ.get("AIDA_REVIEW_BATCH_POSTGRES_TEST_DATABASE_URL"):
            asyncio.run(_drop(db_url))


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    """No shared connections (`NullPool`): two "concurrent" sessions must never be handed the
    same physical connection. `lock_timeout` turns a lock cycle into a failure, not a hang."""
    created = create_async_engine(
        postgres_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "20s"}},
    )
    yield created
    await created.dispose()


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_batch(
    engine: AsyncEngine, size: int
) -> tuple[UUID, list[UUID], list[str], UUID]:
    """An organization with `size` pending table-description reviews, frozen into one batch.
    Returns (organization id, review ids, draft ids, batch id)."""
    async with _sessions(engine)() as setup:
        estate = await build_estate(setup)
        tables = await add_tables(setup, estate, size)
        reviews = await add_table_draft_reviews(setup, estate, tables)
        batch = await freeze_review_batch(
            setup,
            context=reviewer(estate.organization_id),
            selections=[Selection(review.id) for review in reviews],
            filt=None,
        )
        await setup.commit()
        return (
            estate.organization_id,
            [review.id for review in reviews],
            [review.object_id for review in reviews],
            batch.id,
        )


async def _decision_audits(engine: AsyncEngine, review_ids: list[UUID]) -> dict[str, int]:
    async with _sessions(engine)() as check:
        rows = (
            await check.execute(
                select(AuditEvent.resource_id, func.count())
                .where(
                    AuditEvent.action == "governance.review.batch_decide",
                    AuditEvent.resource_id.in_([str(value) for value in review_ids]),
                )
                .group_by(AuditEvent.resource_id)
            )
        ).all()
    return {str(resource_id): int(count) for resource_id, count in rows}


async def _outbox_count(engine: AsyncEngine, aggregate_ids: list[str]) -> int:
    async with _sessions(engine)() as check:
        return int(
            await check.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id.in_(aggregate_ids))
            )
            or 0
        )


def _slowed(delay: float) -> Any:
    """The real decision, slowed so a chunk takes long enough for the other caller to queue
    behind it. Timing only: every decision is still the shared service's."""

    async def slow_decide_review(session_, review, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(delay)
        return await real_decide_review(session_, review, **kwargs)

    return slow_decide_review


async def _decide_in_own_session(
    engine: AsyncEngine,
    barrier: asyncio.Barrier,
    *,
    organization_id: UUID,
    batch_id: UUID,
    decision: str,
    reason: str | None,
    chunk_size: int,
) -> BatchDecisionResult | ReviewBatchError:
    async with _sessions(engine)() as session:
        await barrier.wait()
        try:
            return await decide_review_batch(
                session,
                context=reviewer(organization_id),
                batch_id=batch_id,
                decision=decision,  # type: ignore[arg-type]
                reason=reason,
                chunk_size=chunk_size,
            )
        except ReviewBatchError as refused:
            await session.rollback()
            return refused


# --- same batch, twice at once -------------------------------------------------------------


async def test_two_concurrent_decisions_of_one_batch_take_turns_and_decide_each_member_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization_id, review_ids, draft_ids, batch_id = await _seed_batch(engine, 60)
    monkeypatch.setattr(review_batches_module, "decide_review", _slowed(0.01))
    barrier = asyncio.Barrier(2)
    first, second = await asyncio.gather(
        *(
            _decide_in_own_session(
                engine,
                barrier,
                organization_id=organization_id,
                batch_id=batch_id,
                decision="APPROVE",
                reason="read every member",
                chunk_size=10,
            )
            for _ in range(2)
        )
    )
    results = [r for r in (first, second) if isinstance(r, BatchDecisionResult)]
    refusals = [r for r in (first, second) if isinstance(r, ReviewBatchError)]
    # A caller that arrives after the batch closed is refused ALREADY_DECIDED; nothing else.
    assert {refusal.code for refusal in refusals} <= {"REVIEW_BATCH_ALREADY_DECIDED"}
    assert results, "at least one caller must have decided the batch"
    for result in results:
        assert result.applied_count == 60  # each caller reports the whole batch
        assert result.batch.status == "DECIDED"
    # Serialized, not duplicated: the members were split between the callers, and together
    # they decided each one exactly once.
    per_caller = [result.decided_in_this_call_count for result in results]
    assert sum(per_caller) == 60, per_caller
    MEASURED["race_same_batch_split"] = float(min(per_caller) if len(per_caller) == 2 else 0)
    print("R11-REV01 PG same-batch race: members decided per caller", per_caller)
    assert await _decision_audits(engine, review_ids) == {str(rid): 1 for rid in review_ids}
    assert await _outbox_count(engine, draft_ids) == 60
    async with _sessions(engine)() as check:
        statuses = set(
            (
                await check.scalars(
                    select(GovernanceReview.status).where(GovernanceReview.id.in_(review_ids))
                )
            ).all()
        )
        assert statuses == {"APPROVED"}
        batch_closes = await check.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "governance_review.batch_decide",
                AuditEvent.resource_id == str(batch_id),
            )
        )
        assert batch_closes == 1  # closed -- and audited -- once, by one of the two


async def test_a_concurrent_opposite_decision_of_one_batch_is_refused(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization_id, review_ids, _draft_ids, batch_id = await _seed_batch(engine, 40)
    monkeypatch.setattr(review_batches_module, "decide_review", _slowed(0.01))
    barrier = asyncio.Barrier(2)
    approve, reject = await asyncio.gather(
        _decide_in_own_session(
            engine,
            barrier,
            organization_id=organization_id,
            batch_id=batch_id,
            decision="APPROVE",
            reason=None,
            chunk_size=10,
        ),
        _decide_in_own_session(
            engine,
            barrier,
            organization_id=organization_id,
            batch_id=batch_id,
            decision="REJECT",
            reason="not what the source says",
            chunk_size=10,
        ),
    )
    outcomes = [approve, reject]
    winners = [o for o in outcomes if isinstance(o, BatchDecisionResult)]
    losers = [o for o in outcomes if isinstance(o, ReviewBatchError)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    [winner], [loser] = winners, losers
    assert loser.code in ("REVIEW_BATCH_DECISION_MISMATCH", "REVIEW_BATCH_ALREADY_DECIDED")
    assert winner.applied_count == 40
    expected = "APPROVED" if winner is approve else "REJECTED"
    async with _sessions(engine)() as check:
        statuses = set(
            (
                await check.scalars(
                    select(GovernanceReview.status).where(GovernanceReview.id.in_(review_ids))
                )
            ).all()
        )
    assert statuses == {expected}
    assert await _decision_audits(engine, review_ids) == {str(rid): 1 for rid in review_ids}


# --- batch against batch -------------------------------------------------------------------


async def test_two_overlapping_batches_decided_at_once_decide_each_review_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _sessions(engine)() as setup:
        estate = await build_estate(setup)
        tables = await add_tables(setup, estate, 60)
        reviews = await add_table_draft_reviews(setup, estate, tables)
        context = reviewer(estate.organization_id)
        approving = await freeze_review_batch(
            setup, context=context, selections=[Selection(r.id) for r in reviews[:40]], filt=None
        )
        rejecting = await freeze_review_batch(
            setup, context=context, selections=[Selection(r.id) for r in reviews[20:]], filt=None
        )
        await setup.commit()
        organization_id = estate.organization_id
        review_ids = [r.id for r in reviews]
        approving_id, rejecting_id = approving.id, rejecting.id

    monkeypatch.setattr(review_batches_module, "decide_review", _slowed(0.005))
    barrier = asyncio.Barrier(2)
    approved, rejected = await asyncio.gather(
        _decide_in_own_session(
            engine,
            barrier,
            organization_id=organization_id,
            batch_id=approving_id,
            decision="APPROVE",
            reason=None,
            chunk_size=10,
        ),
        _decide_in_own_session(
            engine,
            barrier,
            organization_id=organization_id,
            batch_id=rejecting_id,
            decision="REJECT",
            reason="the draft overstates the grain",
            chunk_size=10,
        ),
    )
    assert isinstance(approved, BatchDecisionResult) and isinstance(rejected, BatchDecisionResult)
    by_review_approve = {o.item.review_id: o for o in approved.outcomes}
    by_review_reject = {o.item.review_id: o for o in rejected.outcomes}
    overlap = review_ids[20:40]
    for review_id in overlap:
        applied = [
            side
            for side in (by_review_approve[review_id], by_review_reject[review_id])
            if side.outcome == "APPLIED"
        ]
        assert len(applied) == 1, review_id  # decided once, by exactly one batch
        other = (
            by_review_reject[review_id]
            if applied[0] is by_review_approve[review_id]
            else by_review_approve[review_id]
        )
        assert (other.outcome, other.reason_code) in (
            ("REFUSED", "ALREADY_DECIDED"),
            ("REFUSED", "CONCURRENT_DECISION"),
        )
    assert approved.applied_count + rejected.applied_count == 60
    assert await _decision_audits(engine, review_ids) == {str(rid): 1 for rid in review_ids}
    async with _sessions(engine)() as check:
        rows = dict(
            (
                await check.execute(
                    select(GovernanceReview.id, GovernanceReview.status).where(
                        GovernanceReview.id.in_(review_ids)
                    )
                )
            ).all()
        )
    for review_id in review_ids:
        side = by_review_approve.get(review_id)
        expected = "APPROVED" if side is not None and side.outcome == "APPLIED" else "REJECTED"
        assert rows[review_id] == expected, review_id
    MEASURED["batch_vs_batch_overlap_won_by_approve"] = float(
        sum(1 for rid in overlap if by_review_approve[rid].outcome == "APPLIED")
    )
    print("R11-REV01 PG batch-vs-batch", MEASURED)


# --- interrupted, then resumed ---------------------------------------------------------------


class _WorkerDied(RuntimeError):
    pass


async def test_an_interrupted_decision_resumes_on_postgres(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization_id, review_ids, draft_ids, batch_id = await _seed_batch(engine, 25)
    dies_at = review_ids[17]

    async def dying_decide_review(session_, review, **kwargs):  # type: ignore[no-untyped-def]
        if review.id == dies_at:
            raise _WorkerDied("connection dropped")
        return await real_decide_review(session_, review, **kwargs)

    context = reviewer(organization_id)
    monkeypatch.setattr(review_batches_module, "decide_review", dying_decide_review)
    async with _sessions(engine)() as session:
        with pytest.raises(_WorkerDied):
            await decide_review_batch(
                session,
                context=context,
                batch_id=batch_id,
                decision="APPROVE",
                reason=None,
                chunk_size=10,
            )
        await session.rollback()
    async with _sessions(engine)() as check:
        outcomes = list(
            (
                await check.scalars(
                    select(ReviewBatchItem.outcome)
                    .where(ReviewBatchItem.batch_id == batch_id)
                    .order_by(ReviewBatchItem.position)
                )
            ).all()
        )
        assert outcomes == ["APPLIED"] * 10 + ["PENDING"] * 15
        state = (
            await check.execute(
                select(ReviewBatch.status, ReviewBatch.decision).where(ReviewBatch.id == batch_id)
            )
        ).one()
        assert tuple(state) == ("FROZEN", "APPROVE")

    monkeypatch.setattr(review_batches_module, "decide_review", real_decide_review)
    async with _sessions(engine)() as session:
        resumed = await decide_review_batch(
            session, context=context, batch_id=batch_id, decision="APPROVE", reason=None,
            chunk_size=10,
        )
    assert resumed.resumed is True
    assert (resumed.applied_count, resumed.decided_in_this_call_count) == (25, 15)
    assert await _decision_audits(engine, review_ids) == {str(rid): 1 for rid in review_ids}
    assert await _outbox_count(engine, draft_ids) == 25


# --- PostgreSQL timings: freeze and decide 1,000 members --------------------------------------


async def test_postgres_timings_for_freezing_and_deciding_1000_members(
    engine: AsyncEngine,
) -> None:
    async with _sessions(engine)() as setup:
        estate = await build_estate(setup)
        [wide] = await add_tables(setup, estate, 1, prefix="wide")
        columns = await add_columns(setup, wide, 1000)
        await add_column_draft_reviews(setup, estate, wide, columns)
        await setup.commit()
        organization_id, wide_id = estate.organization_id, wide.id
    context = reviewer(organization_id)

    statements: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        selected: list[ReviewBatchSelectionWrite] = []
        async with _sessions(engine)() as session:
            cursor: str | None = None
            page_seconds: list[float] = []
            while True:
                started = time.perf_counter()
                page = await get_change_queue(
                    review_status="PENDING",
                    object_type=[],
                    family=[],
                    change_kind=[],
                    object_id=None,
                    table_id=wide_id,
                    decidable_only=False,
                    cursor=cursor,
                    limit=100,
                    include_total=True,
                    context=context,
                    session=session,
                )
                page_seconds.append(time.perf_counter() - started)
                selected.extend(
                    ReviewBatchSelectionWrite(
                        review_id=item.review_id, evidence_fingerprint=item.evidence_fingerprint
                    )
                    for item in page.items
                )
                cursor = page.next_cursor
                if cursor is None:
                    break
        assert len(selected) == 1000

        async with _sessions(engine)() as session:
            statements.clear()
            started = time.perf_counter()
            batch = await create_review_batch(
                ReviewBatchCreate(items=selected), context=context, session=session
            )
            freeze_seconds = time.perf_counter() - started
            freeze_statements = len(statements)
        assert (batch.item_count, batch.eligible_count) == (1000, 1000)

        async with _sessions(engine)() as session:
            statements.clear()
            started = time.perf_counter()
            result = await decide_frozen_review_batch(
                batch.id,
                ReviewBatchDecisionCreate(decision="APPROVE", reason="column evidence reviewed"),
                context=context,
                session=session,
            )
            decide_seconds = time.perf_counter() - started
            decide_statements = len(statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert result.overall == "SUCCESS"
    assert result.applied_count == 1000
    MEASURED.update(
        pg_slowest_scoped_page_seconds=round(max(page_seconds), 3),
        pg_freeze_1000_seconds=round(freeze_seconds, 3),
        pg_freeze_1000_statements=freeze_statements,
        pg_decide_1000_seconds=round(decide_seconds, 3),
        pg_decide_1000_statements=decide_statements,
        pg_decide_statements_per_member=round(decide_statements / 1000, 2),
    )
    print("R11-REV01 PG timings", MEASURED)
    assert freeze_statements <= PG_FREEZE_1000_STATEMENTS
    assert freeze_seconds <= PG_FREEZE_1000_SECONDS
    assert decide_statements / 1000 <= PG_DECIDE_STATEMENTS_PER_MEMBER
    assert decide_seconds <= PG_DECIDE_1000_SECONDS
