"""AR-05: the agent budget reservation, raced on a real PostgreSQL.

`tests/test_agent_budget_and_envelope.py` proved the reservation arithmetic
on SQLite, and said in its own docstring that this was not enough: SQLite
serializes writers, so a test there exercises the cap *predicate* but never
two genuinely simultaneous reservations. The 2026-09-09 architecture review
(`Docs/10-architecture/15-agent-architecture-critical-review.md`, AR-05) asked
for exactly the reproduction F05 got. This file is it.

Every test below issues its reservations with `asyncio.gather` against
**separate `AsyncSession`s on separate connections** to a real PostgreSQL
server, released from a shared `asyncio.Barrier` so all of them are in flight
before any of them writes. Nothing is sequenced.

**The property.** `aida.agent_budget.reserve_run_budget` moves
`agent_budget_window.reserved_tokens` with a conditional UPDATE that carries
the contract's daily cap in its own `WHERE`:

    UPDATE agent_budget_window
       SET reserved_tokens = reserved_tokens + :amount
     WHERE id = :id
       AND reserved_tokens + :amount <= :daily_cap

The claim under test is that the database, not the application, decides who
fits. On PostgreSQL a losing racer's `UPDATE` blocks on the winner's
uncommitted row; at READ COMMITTED it then re-evaluates the predicate against
the newly committed version, matches nothing, and `rowcount` is 0 -- so it is
refused `AgentBudgetExceeded`. That is the shape a read-then-check
implementation cannot produce: five concurrent readers of "0 spent" would all
proceed.

**What makes this falsifiable, and where it is not.** Dropping the
`reserved_tokens + :amount <= :daily_cap` clause from the `WHERE` was tried
before this file was committed. At READ COMMITTED three tests go red -- ten
racers all win, and a 1000-token day holds 2000 -- so the predicate is
demonstrably the thing doing the bounding, not some other guard catching it
downstream. There is no other guard: this clause is all that stands between a
contracted agent and an unbounded day.

At REPEATABLE READ, removing it changes nothing: PostgreSQL aborts the blocked
writers with 40001 before they can double-count, so the isolation level
bounds the day by itself. That is worth stating rather than hiding, because it
means the REPEATABLE READ cases here confirm the invariant without testing the
guard. The READ COMMITTED cases are the ones that hold the guard honest, and
READ COMMITTED is what the application connects at.

**Isolation levels, and a real difference between them.** Every shape runs at
READ COMMITTED (PostgreSQL's default, and what the application connects at)
and at REPEATABLE READ. The *invariant* holds at both: the window never
exceeds its cap. The *refusal shape* does not, and that is a finding rather
than something to paper over -- at REPEATABLE READ a blocked writer cannot
re-read the newer row version, so PostgreSQL aborts it with SQLSTATE 40001
instead of letting it observe the fuller window. The caller is still refused
and still reserves nothing, but it is refused as a serialization failure, not
as `AgentBudgetExceeded`, and an operator who raises the isolation level needs
a retry loop that the application does not supply. `_expected_refusals` pins
that difference per isolation level rather than accepting "either" everywhere.

**Skipping.** Same idiom as `test_governance_decision_postgres_concurrency.py`:
only a failure to *reach* PostgreSQL is a skip. Anything after that -- an
overshot cap, a duplicated window row -- must fail. Point
`AIDA_BUDGET_CONCURRENCY_TEST_DATABASE_URL` at a scratch database, or run a
PostgreSQL matching `Settings.database_url`'s default and this file derives
`.../<dbname>_budget_concurrency_test` on the same server. It never reuses
another suite's database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from aida import (  # noqa: F401 -- registers every ORM table on Base.metadata
    envelope_models,
    graph_store,
    models,
    procedure_lineage_models,
)
from aida.agent_budget import (
    AgentBudgetExceeded,
    daily_reserved_tokens,
    reconcile_run_budget,
    reserve_run_budget,
)
from aida.db import Base
from aida.models import (
    AgentBudgetWindow,
    AgentContract,
    AiAsset,
    AiAssetVersion,
    Organization,
)
from atlas.platform.config import get_settings

#: PostgreSQL's SQLSTATE for "could not serialize access due to concurrent
#: update" -- what a blocked writer gets at REPEATABLE READ or stricter, where
#: READ COMMITTED would instead let it re-read and be refused by the predicate.
SERIALIZATION_FAILURE = "40001"

ISOLATION_LEVELS = ("READ COMMITTED", "REPEATABLE READ")

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


# --- reaching (or skipping) a real PostgreSQL --------------------------------


def _test_database_url() -> str:
    override = os.environ.get("AIDA_BUDGET_CONCURRENCY_TEST_DATABASE_URL")
    if override:
        return override
    default_url = get_settings().database_url
    root, _, dbname = default_url.rpartition("/")
    if not root or not dbname:
        raise AssertionError(
            f"Settings.database_url {default_url!r} doesn't look like a "
            "'.../<dbname>' URL; cannot derive a scratch database name from it."
        )
    return f"{root}/{dbname}_budget_concurrency_test"


def _maintenance_url(db_url: str) -> str:
    root, _, _ = db_url.rpartition("/")
    _, _, app_dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{app_dbname}"


async def _probe_reachable(db_url: str) -> None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _prepare_database(db_url: str) -> None:
    try:
        await _probe_reachable(db_url)
    except Exception:
        admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
        _, _, dbname = db_url.rpartition("/")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        finally:
            await admin.dispose()
        await _probe_reachable(db_url)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """A prepared scratch database, or a clean skip naming why not."""
    db_url = _test_database_url()
    try:
        asyncio.run(_prepare_database(db_url))
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip", not "fail"
        pytest.skip(
            f"PostgreSQL is not reachable at {db_url!r} ({type(exc).__name__}: {exc}); "
            "AR-05's concurrent reproduction needs a real PostgreSQL -- see this "
            "file's module docstring for how to point it at one. The SQLite "
            "reproduction in tests/test_agent_budget_and_envelope.py still runs."
        )
    yield db_url


@pytest_asyncio.fixture(params=ISOLATION_LEVELS)
async def engine(postgres_url: str, request: pytest.FixtureRequest) -> AsyncIterator[AsyncEngine]:
    """One engine per isolation level, with no shared connections.

    `NullPool` is deliberate: pooled connections would let two "concurrent"
    sessions be handed the same physical connection back-to-back, which is the
    thing this file exists to rule out. `lock_timeout` turns a genuine lock
    cycle into a failure in seconds rather than a hung suite.
    """
    created = create_async_engine(
        postgres_url,
        isolation_level=request.param,
        poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "15s"}},
    )
    created.echo = False
    yield created
    await created.dispose()


@pytest.fixture
def isolation_level(request: pytest.FixtureRequest) -> str:
    return str(request.node.callspec.params["engine"])


# --- seeding -----------------------------------------------------------------


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    engine: AsyncEngine,
    *,
    daily_token_cap: int,
    per_run_token_cap: int | None,
) -> tuple[UUID, UUID]:
    """A fresh organization, agent version and contract.

    Returns `(organization_id, ai_asset_version_id)`. A fresh organization per
    test is what lets this file build the schema once per module and still
    count reservations exactly: nothing here is shared with another test.
    """
    async with _sessions(engine)() as setup:
        org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:12]}")
        setup.add(org)
        await setup.flush()
        asset = AiAsset(
            organization_id=org.id,
            asset_key=f"agent-{uuid4().hex[:12]}",
            asset_kind="AGENT",
            created_by="human-author",
        )
        setup.add(asset)
        await setup.flush()
        version = AiAssetVersion(
            organization_id=org.id,
            asset_id=asset.id,
            version=1,
            status="APPROVED",
            name="Steward agent",
            description="Drafts descriptions.",
            intended_use="Steward assistance.",
            owner_principal="steward-team",
            provider_type="INTERNAL",
            risk_tier="LOW",
            context_product_version_ids=[],
            model_route_ids=[],
            policy_control_ids=[],
            evaluation_evidence={},
            runtime_evidence={},
            fingerprint=uuid4().hex,
            created_by="human-author",
        )
        setup.add(version)
        await setup.flush()
        setup.add(
            AgentContract(
                organization_id=org.id,
                ai_asset_version_id=version.id,
                agent_principal_id=f"agent:steward-{uuid4().hex[:8]}",
                capability_envelope={
                    "tool_slugs": [],
                    "context_product_ids": [],
                    "write_lanes": [],
                },
                autonomy_tier="T1",
                supervisor_persona="STEWARD",
                kill_scope="AGENT",
                sampling_rate=0.05,
                daily_token_cap=daily_token_cap,
                per_run_token_cap=per_run_token_cap,
                created_by="human-author",
            )
        )
        await setup.commit()
        return org.id, version.id


async def _contract(session: AsyncSession, organization_id: UUID) -> AgentContract:
    contract = await session.scalar(
        select(AgentContract).where(AgentContract.organization_id == organization_id)
    )
    assert contract is not None
    return contract


# --- racing ------------------------------------------------------------------


def _reserver(
    maker: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    barrier: asyncio.Barrier,
) -> Callable[[], Awaitable[Any]]:
    """One `reserve_run_budget` call on its own session and connection.

    Each racer opens its transaction and loads its own copy of the contract
    *before* waiting on the barrier, so every one of them is holding a live
    transaction with a stale view of the window when the first write lands.
    That is the interleaving a read-then-check implementation gets wrong.
    """

    async def run() -> Any:
        async with maker() as session:
            await session.execute(select(func.now()))  # open the transaction first
            contract = await _contract(session, organization_id)
            await barrier.wait()
            reservation = await reserve_run_budget(
                session, contract, estimated_input_tokens=1, now=_NOW
            )
            await session.commit()
            return reservation

    return run


async def _race(*racers: Callable[[], Awaitable[Any]]) -> list[Any]:
    """Run every racer truly concurrently, releasing them from one barrier."""
    return await asyncio.gather(*(racer() for racer in racers), return_exceptions=True)


def _partition(results: list[Any]) -> tuple[list[Any], list[BaseException]]:
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    return winners, losers


def _classify(error: BaseException) -> str:
    """How a losing racer was refused, as a stable label."""
    if isinstance(error, AgentBudgetExceeded):
        return "budget_exceeded"
    if isinstance(error, DBAPIError):
        sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None) or getattr(
            getattr(error, "orig", None), "pgcode", None
        )
        if sqlstate == SERIALIZATION_FAILURE:
            return "serialization_failure"
        return f"dbapi:{sqlstate}"
    return type(error).__name__


def _expected_refusals(isolation_level: str) -> set[str]:
    """How a losing reserver is refused, per isolation level.

    At READ COMMITTED this must be the conditional UPDATE itself: anything
    else would mean the predicate let a second caller through and something
    downstream caught it. At REPEATABLE READ the blocked writer is aborted
    before it can re-read, so a serialization failure is the correct -- and
    documented -- shape there.
    """
    if isolation_level == "READ COMMITTED":
        return {"budget_exceeded"}
    return {"budget_exceeded", "serialization_failure"}


async def _window_state(engine: AsyncEngine, version_id: UUID) -> tuple[int, int, int]:
    """`(row count, reserved_tokens, run_count)` on a fresh connection."""
    async with _sessions(engine)() as reader:
        rows = (
            await reader.scalars(
                select(AgentBudgetWindow).where(
                    AgentBudgetWindow.ai_asset_version_id == version_id
                )
            )
        ).all()
        if not rows:
            return 0, 0, 0
        assert len(rows) == 1, "one agent version, one UTC day, one window row"
        return 1, int(rows[0].reserved_tokens), int(rows[0].run_count)


# ---------------------------------------------------------------------------
# 1. the cap holds under genuine contention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ten_simultaneous_reservations_never_exceed_the_cap(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """The invariant, at both isolation levels.

    Ten racers each want a fifth of the day, released together. Whatever the
    refusal shape, the window must never hold more than the cap -- this is the
    assertion that goes red if the predicate is dropped from the `WHERE`.
    """
    organization_id, version_id = await _seed(
        engine, daily_token_cap=1_000, per_run_token_cap=200
    )
    maker = _sessions(engine)
    barrier = asyncio.Barrier(10)

    results = await _race(*(_reserver(maker, organization_id, barrier) for _ in range(10)))
    _winners, losers = _partition(results)

    _rows, reserved, _runs = await _window_state(engine, version_id)
    assert reserved <= 1_000, (
        f"the daily cap was exceeded ({reserved} > 1000) -- the conditional "
        "UPDATE's predicate is not bounding concurrent reservations"
    )
    assert {_classify(error) for error in losers} <= _expected_refusals(isolation_level)


@pytest.mark.asyncio
async def test_read_committed_grants_exactly_the_number_that_fit(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """At the isolation level the application actually runs at, the outcome is
    exact: five of ten racers fit a 1000-token day at 200 tokens each, and the
    other five are refused by the predicate itself.

    Asserted only at READ COMMITTED. At REPEATABLE READ a blocked writer is
    aborted rather than re-reading, so the *count* that gets through depends on
    how many serialization failures PostgreSQL raises -- the invariant still
    holds there and the test above asserts it, but a precise count would be
    asserting the scheduler, not the guard.
    """
    if isolation_level != "READ COMMITTED":
        pytest.skip("exact grant count is a READ COMMITTED property; see the docstring")
    organization_id, version_id = await _seed(
        engine, daily_token_cap=1_000, per_run_token_cap=200
    )
    maker = _sessions(engine)
    barrier = asyncio.Barrier(10)

    results = await _race(*(_reserver(maker, organization_id, barrier) for _ in range(10)))
    winners, losers = _partition(results)

    assert len(winners) == 5
    assert len(losers) == 5
    assert {_classify(error) for error in losers} == {"budget_exceeded"}
    _rows, reserved, runs = await _window_state(engine, version_id)
    assert reserved == 1_000
    assert runs == 5, "run_count counts granted reservations, not attempts"


# ---------------------------------------------------------------------------
# 2. the window row itself is created exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simultaneous_first_runs_create_one_window_row(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """The day's first runs race to `INSERT` the window row.

    `_ensure_window` reads, inserts inside a savepoint, and re-reads when the
    unique constraint refuses it. On SQLite that path is unreachable -- writers
    serialize, so the second racer's read already sees the first's row. Here it
    is the normal case, and the assertion is that it produces one row and no
    unhandled `IntegrityError`.
    """
    organization_id, version_id = await _seed(
        engine, daily_token_cap=10_000, per_run_token_cap=100
    )
    maker = _sessions(engine)
    barrier = asyncio.Barrier(6)

    results = await _race(*(_reserver(maker, organization_id, barrier) for _ in range(6)))
    _winners, losers = _partition(results)

    rows, reserved, _runs = await _window_state(engine, version_id)
    assert rows == 1, "the unique constraint must collapse the insert race to one row"
    assert reserved <= 10_000
    assert {_classify(error) for error in losers} <= _expected_refusals(isolation_level)


# ---------------------------------------------------------------------------
# 3. reconciliation under contention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_reconciliation_does_not_lose_or_invent_tokens(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """Four runs reserve, then all four reconcile down at once.

    Reconciliation is a relative `UPDATE` (`reserved + delta`), so lost updates
    are the failure mode: two reconciliations reading the same value and both
    writing their own absolute result would leave the window overstating or
    understating the day. PostgreSQL serializes the row writes; this asserts
    the arithmetic survives that.
    """
    if isolation_level != "READ COMMITTED":
        pytest.skip("reconciliation retry semantics at REPEATABLE READ are AR-05 open work")
    organization_id, version_id = await _seed(
        engine, daily_token_cap=10_000, per_run_token_cap=1_000
    )
    maker = _sessions(engine)
    barrier = asyncio.Barrier(4)

    reservations = await _race(*(_reserver(maker, organization_id, barrier) for _ in range(4)))
    granted, losers = _partition(reservations)
    assert losers == []
    assert len(granted) == 4

    _rows, reserved_before, _runs = await _window_state(engine, version_id)
    assert reserved_before == 4_000

    reconcile_barrier = asyncio.Barrier(4)

    def reconciler(reservation: Any) -> Callable[[], Awaitable[Any]]:
        async def run() -> Any:
            async with maker() as session:
                await session.execute(select(func.now()))
                await reconcile_barrier.wait()
                await reconcile_run_budget(session, reservation, actual_tokens=250)
                await session.commit()

        return run

    outcomes = await _race(*(reconciler(reservation) for reservation in granted))
    assert _partition(outcomes)[1] == []

    _rows, reserved_after, _runs = await _window_state(engine, version_id)
    assert reserved_after == 1_000, (
        "four runs reserving 1000 each and reconciling to 250 each must leave "
        f"exactly 1000 reserved, not {reserved_after} -- a relative UPDATE that "
        "lost an update would drift"
    )


# ---------------------------------------------------------------------------
# 4. the read-only helper agrees with what was raced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_reserved_tokens_reports_what_the_race_left(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """`daily_reserved_tokens` exists for operators and tests, never for
    enforcement -- reading it and then deciding is precisely the race the
    conditional UPDATE avoids. This asserts it reports the truth, so an
    operator dashboard built on it is not lying."""
    organization_id, version_id = await _seed(
        engine, daily_token_cap=600, per_run_token_cap=200
    )
    maker = _sessions(engine)
    barrier = asyncio.Barrier(5)

    await _race(*(_reserver(maker, organization_id, barrier) for _ in range(5)))

    _rows, reserved, _runs = await _window_state(engine, version_id)
    async with _sessions(engine)() as reader:
        reported = await daily_reserved_tokens(
            reader,
            organization_id=organization_id,
            ai_asset_version_id=version_id,
            window_date=_NOW.date(),
        )
    assert reported == reserved
    assert reported <= 600
