"""R11-REV01 exit fixtures: a 1,000-table estate and a 1,000-column table, with the
performance budgets stated *before* measuring, as the design asks ("set and record performance
budgets before implementation benchmarking; no unmeasured latency promise").

**Where these numbers come from.** In-memory SQLite (aiosqlite, StaticPool) on the developer
machine that runs the suite. They are a local measurement of this code's own cost, not a
PostgreSQL latency claim and not a production SLO. The *statement counts* are the portable
claim: they are counted at the DBAPI cursor, so nothing above it can hide a round trip, and a
page that costs the same number of statements at 10 reviews as at 2,000 does not grow with the
queue on any engine. Wall-clock budgets are generous ceilings that catch a regression to
per-row querying, which on this fixture would cost thousands of statements.

Budgets (declared, then asserted):

* B1 queue page (100 rows): <= QUEUE_PAGE_STATEMENTS statements, the same count at 10 or 2,000
  queued reviews and on page 1 or page 10; <= QUEUE_PAGE_SECONDS wall per page.
* B2 table-scoped page over the 1,000-column table: the same statement count on every page.
* B3 freeze of 1,000 members selected across 10 pages: <= FREEZE_STATEMENTS statements (not
  one per member); <= FREEZE_SECONDS wall.
* B4 member listing page: <= MEMBER_PAGE_STATEMENTS statements.
* B5 deciding 1,000 members: per-member statements at 1,000 no more than 1.5x the per-member
  cost at 100 (linear, not quadratic); <= DECIDE_1000_SECONDS wall. Each member is a real
  approval: claim, publish the column description, outbox and audit, in its own savepoint.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida import review_batch_models  # noqa: F401 -- registers the batch tables
from aida.db import Base
from aida.review_batch_api import (
    create_review_batch,
    decide_frozen_review_batch,
    get_change_queue,
    read_review_batch_items,
)
from aida.review_batch_schemas import (
    ReviewBatchCreate,
    ReviewBatchDecisionCreate,
    ReviewBatchSelectionWrite,
)
from tests.support.review_batch_estate import (
    Estate,
    add_column_draft_reviews,
    add_columns,
    add_table_draft_reviews,
    add_tables,
    build_estate,
    reviewer,
)

QUEUE_PAGE_STATEMENTS = 12
QUEUE_PAGE_SECONDS = 1.0
FREEZE_STATEMENTS = 20
FREEZE_SECONDS = 5.0
MEMBER_PAGE_STATEMENTS = 3
DECIDE_1000_SECONDS = 60.0

#: Printed at the end of each test (`pytest -s`) so the measured values can be recorded.
MEASURED: dict[str, float] = {}


class _Counter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def reset(self) -> None:
        self.statements.clear()

    def __len__(self) -> int:
        return len(self.statements)


@pytest.fixture
async def engine() -> AsyncIterator[Any]:
    created = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with created.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest.fixture
async def session(engine: Any) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active


@pytest.fixture
def counted(engine: Any) -> Iterator[_Counter]:
    counter = _Counter()

    def _record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        counter.statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield counter
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _queue(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "review_status": "PENDING",
        "object_type": [],
        "family": [],
        "change_kind": [],
        "object_id": None,
        "table_id": None,
        "decidable_only": False,
        "cursor": None,
        "limit": 100,
        "include_total": True,
    }
    base.update(overrides)
    return base


async def _estate_1000_tables_and_1000_columns(session: AsyncSession) -> tuple[Estate, Any]:
    """1,000 tables each with a pending table-description review, plus one table of 1,000
    columns each with a pending column-description review: 2,000 queued reviews."""
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 1000)
    await add_table_draft_reviews(session, estate, tables)
    [wide] = await add_tables(session, estate, 1, prefix="wide")
    columns = await add_columns(session, wide, 1000)
    await add_column_draft_reviews(session, estate, wide, columns)
    await session.commit()
    # Measured calls read from the database, as separate requests would, not from the
    # identity map the seeding filled.
    session.expunge_all()
    return estate, wide.id


async def test_b1_queue_page_cost_does_not_grow_with_the_queue_or_the_page(
    engine: Any, session: AsyncSession, counted: _Counter
) -> None:
    # Small queue first, in its own organization of the same database.
    small = await build_estate(session, name="Small")
    await add_table_draft_reviews(session, small, await add_tables(session, small, 10))
    await session.commit()
    session.expunge_all()
    counted.reset()
    await get_change_queue(**_queue(), context=reviewer(small.organization_id), session=session)
    small_statements = len(counted)

    estate, _ = await _estate_1000_tables_and_1000_columns(session)
    context = reviewer(estate.organization_id)
    per_page: list[int] = []
    slowest = 0.0
    cursor: str | None = None
    for _ in range(10):
        session.expunge_all()
        counted.reset()
        started = time.perf_counter()
        page = await get_change_queue(**_queue(cursor=cursor), context=context, session=session)
        slowest = max(slowest, time.perf_counter() - started)
        per_page.append(len(counted))
        assert len(page.items) == 100
        assert page.total == 2000
        cursor = page.next_cursor

    MEASURED.update(
        b1_small_queue_statements=small_statements,
        b1_statements_per_page=max(per_page),
        b1_slowest_page_seconds=round(slowest, 3),
    )
    print("R11-REV01 B1", MEASURED)
    assert set(per_page) == {small_statements}, per_page
    assert small_statements <= QUEUE_PAGE_STATEMENTS
    assert slowest <= QUEUE_PAGE_SECONDS


async def test_b2_b3_b4_b5_the_1000_column_table_end_to_end(
    engine: Any, session: AsyncSession, counted: _Counter
) -> None:
    estate, wide_id = await _estate_1000_tables_and_1000_columns(session)
    context = reviewer(estate.organization_id)

    # B2: page the table scope, selecting across all ten pages.
    selected: list[ReviewBatchSelectionWrite] = []
    per_page: list[int] = []
    cursor: str | None = None
    while True:
        session.expunge_all()
        counted.reset()
        page = await get_change_queue(
            **_queue(table_id=wide_id, cursor=cursor), context=context, session=session
        )
        per_page.append(len(counted))
        selected.extend(
            ReviewBatchSelectionWrite(
                review_id=item.review_id, evidence_fingerprint=item.evidence_fingerprint
            )
            for item in page.items
        )
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(per_page) == 10
    assert len(set(per_page)) == 1, per_page
    assert len(selected) == 1000

    # B3: freeze all 1,000, bound to the versions seen on each page.
    session.expunge_all()
    counted.reset()
    started = time.perf_counter()
    batch = await create_review_batch(
        ReviewBatchCreate(items=selected), context=context, session=session
    )
    freeze_seconds = time.perf_counter() - started
    freeze_statements = len(counted)
    assert (batch.item_count, batch.eligible_count) == (1000, 1000)

    # B4: members are paged, never loaded whole.
    session.expunge_all()
    counted.reset()
    members = await read_review_batch_items(
        batch.id, cursor=None, limit=100, outcome=None, eligibility=None,
        context=context, session=session,
    )
    member_statements = len(counted)
    assert len(members.items) == 100 and members.next_cursor is not None

    # B5: decide all 1,000 -- each one a real publish through the shared decision service.
    session.expunge_all()
    counted.reset()
    started = time.perf_counter()
    result = await decide_frozen_review_batch(
        batch.id,
        ReviewBatchDecisionCreate(decision="APPROVE", reason="column evidence reviewed"),
        context=context,
        session=session,
    )
    decide_seconds = time.perf_counter() - started
    decide_statements = len(counted)
    assert result.overall == "SUCCESS"
    assert result.applied_count == 1000

    MEASURED.update(
        b2_statements_per_scoped_page=per_page[0],
        b3_freeze_1000_statements=freeze_statements,
        b3_freeze_1000_seconds=round(freeze_seconds, 3),
        b4_member_page_statements=member_statements,
        b5_decide_1000_statements=decide_statements,
        b5_decide_1000_seconds=round(decide_seconds, 3),
        b5_statements_per_member=round(decide_statements / 1000, 2),
    )
    print("R11-REV01 B2-B5", MEASURED)
    assert freeze_statements <= FREEZE_STATEMENTS
    assert freeze_seconds <= FREEZE_SECONDS
    assert member_statements <= MEMBER_PAGE_STATEMENTS
    assert decide_seconds <= DECIDE_1000_SECONDS


async def test_b5_deciding_is_linear_in_members(engine: Any, session: AsyncSession) -> None:
    """Per-member statement cost at 1,000 is within 1.5x of the cost at 100."""

    async def _decide(size: int) -> int:
        estate = await build_estate(session, name=f"Size{size}")
        [wide] = await add_tables(session, estate, 1, prefix=f"wide{size}")
        columns = await add_columns(session, wide, size)
        reviews = await add_column_draft_reviews(session, estate, wide, columns)
        await session.commit()
        context = reviewer(estate.organization_id)
        batch = await create_review_batch(
            ReviewBatchCreate(
                items=[ReviewBatchSelectionWrite(review_id=r.id) for r in reviews]
            ),
            context=context,
            session=session,
        )
        session.expunge_all()
        counter = _Counter()

        def _record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            counter.statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            result = await decide_frozen_review_batch(
                batch.id,
                ReviewBatchDecisionCreate(decision="APPROVE"),
                context=context,
                session=session,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record)
        assert result.applied_count == size
        return len(counter)

    small = await _decide(100)
    large = await _decide(1000)
    MEASURED.update(
        b5_per_member_at_100=round(small / 100, 2), b5_per_member_at_1000=round(large / 1000, 2)
    )
    print("R11-REV01 B5 linearity", MEASURED)
    assert large / 1000 <= (small / 100) * 1.5
