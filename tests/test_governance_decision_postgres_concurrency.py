"""F05 / T05: the governance claim, raced on a real PostgreSQL.

`tests/test_governance_decision_concurrency.py` proved the invariant on
file-backed SQLite with two real connections. The review
(`Docs/review-2026-09-05/REVIEW.md` F05, tracker row T05) recorded that this
was not enough: SQLite compiles `SELECT ... FOR UPDATE` away and has no
notion of a blocking row lock, so a test there can show that a *later*
caller is refused but can never show what two genuinely simultaneous
deciders do to each other. This file is the missing half.

Everything below issues its two (or more) decisions with `asyncio.gather`
against **separate `AsyncSession`s on separate connections** to a real
PostgreSQL server, released from a shared `asyncio.Barrier` so both are
in-flight before either writes. Nothing is sequenced.

**What contention actually looks like on PostgreSQL, and why the shapes
differ from the SQLite file.** Two guards stand in front of a decision:

1. the routers' `FOR UPDATE` read (`semantic_api.decide_governance_review`
   and `governance_decision_service.lock_reviews_for_decision`), and
2. the compare-and-set in `governance_decision_service.claim_review`,
   `UPDATE ... WHERE id = :id AND status = 'PENDING'`.

On PostgreSQL the first guard is real: the loser's `FOR UPDATE` *blocks* on
the winner's row lock, and at READ COMMITTED re-reads the row after the
winner commits -- so the loser is refused by the router's own already-decided
check before it ever reaches the claim. That is a stronger outcome than
SQLite could demonstrate, and it is what `test_single_vs_single`,
`test_single_vs_bulk` and `test_bulk_vs_bulk` assert.

It also means those three shapes do not, on PostgreSQL, exercise the
compare-and-set itself. `test_service_claim_race` and
`test_service_claim_race_three_way` do: they race
`governance_decision_service.decide_review` directly, with no `FOR UPDATE`
anywhere, which is the shape the reviewer agent (ADR-0027) and any future
lock-free caller actually run in. There the loser's `UPDATE` blocks on the
winner's uncommitted row, PostgreSQL re-evaluates `status = 'PENDING'`
against the newly committed version, `rowcount` is 0, and the caller is
refused `CONFLICT` carrying the review's refreshed state.

**Isolation levels.** Every test runs twice, at READ COMMITTED (PostgreSQL's
default and the one the application runs at) and at REPEATABLE READ. The
invariant -- one terminal decision, one set of side effects -- holds at both.
The *refusal shape* does not, and that is a finding rather than something to
paper over: at REPEATABLE READ a blocked writer cannot re-read the newer row
version, so PostgreSQL aborts it with SQLSTATE 40001 ("could not serialize
access due to concurrent update") instead of letting it observe the decided
status. The loser is still refused and still writes nothing, but it is
refused as a serialization failure, not as a 409 CONFLICT. An operator who
raises the isolation level therefore needs a retry loop; nothing in the
application supplies one today. `_expected_refusal` below pins that
difference per isolation level rather than accepting "either" everywhere.

**Skipping.** Same idiom as `tests/test_migration_orm_drift.py`: only a
failure to *reach* PostgreSQL is a skip. Anything after that -- a broken
claim, a double decision -- must fail. Point
`AIDA_DECISION_CONCURRENCY_TEST_DATABASE_URL` at a scratch database, or run
a PostgreSQL matching `Settings.database_url`'s default and this file
derives `.../<dbname>_decision_concurrency_test` on the same server. It
never reuses the drift gate's database.

**Schema.** Built from `Base.metadata`, not from Alembic. That is the
convention of every other DB-backed test file; the one file that applies the
real migrations, `test_migration_orm_drift.py`, exists precisely so that
"the ORM and the migrations agree" is proven once, in one place, rather than
paid for by every suite that only needs tables to exist.
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
from fastapi import HTTPException
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
from aida import governance_decision_service
from aida.db import Base
from aida.governance_decision_contracts import GovernanceDecisionRefused
from aida.governance_decision_service import decide_review, record_decision_outbox
from aida.models import (
    AuditEvent,
    GlossaryTerm,
    GovernanceReview,
    Organization,
    OutboxEvent,
    TermSemanticBinding,
)
from aida.schemas import (
    GovernanceDecisionRequest,
    GovernanceReviewBulkDecisionRequest,
)
from aida.security_types import SecurityContext
from aida.semantic_api import bulk_decide_governance_reviews, decide_governance_review
from atlas.platform.config import get_settings

#: PostgreSQL's SQLSTATE for "could not serialize access due to concurrent
#: update" -- what a blocked writer gets at REPEATABLE READ or stricter,
#: where READ COMMITTED would instead let it re-read and be refused CONFLICT.
SERIALIZATION_FAILURE = "40001"

#: The isolation levels every shape below is raced at. READ COMMITTED is
#: PostgreSQL's default and what the application connects at; REPEATABLE READ
#: is included because a compare-and-set that only holds at one isolation
#: level is worth knowing about.
ISOLATION_LEVELS = ("READ COMMITTED", "REPEATABLE READ")


# --- reaching (or skipping) a real PostgreSQL --------------------------------


def _test_database_url() -> str:
    """The scratch PostgreSQL database this file builds and races against.

    An explicit override always wins; otherwise a dedicated database name is
    derived from the application's own default connection, so this file
    duplicates no host/port/credential defaults and never touches either the
    database an app instance would use or the drift gate's own.
    """
    override = os.environ.get("AIDA_DECISION_CONCURRENCY_TEST_DATABASE_URL")
    if override:
        return override
    default_url = get_settings().database_url
    root, _, dbname = default_url.rpartition("/")
    if not root or not dbname:
        raise AssertionError(
            f"Settings.database_url {default_url!r} doesn't look like a "
            "'.../<dbname>' URL; cannot derive a scratch database name from it."
        )
    return f"{root}/{dbname}_decision_concurrency_test"


def _maintenance_url(db_url: str) -> str:
    """The same server, but the application's own database, used only to
    `CREATE DATABASE` the scratch one when it does not exist yet."""
    root, _, _ = db_url.rpartition("/")
    _, _, app_dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{app_dbname}"


async def _prepare_database(db_url: str) -> None:
    """Reach the scratch database, creating it if needed, and reset it to a
    schema built from `Base.metadata`.

    Creating the database is attempted only when connecting to it fails; a
    role without `CREATEDB` simply propagates that failure, which the caller
    turns into a skip with the real reason attached.
    """
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


async def _probe_reachable(db_url: str) -> None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """A prepared scratch database, or a clean skip naming why not.

    Deliberately a *sync* fixture driving its own `asyncio.run`, the same way
    `test_migration_orm_drift.py` does: only a failure to reach PostgreSQL is
    a skip, and it must be decided before any event loop this file's async
    tests run in exists.
    """
    db_url = _test_database_url()
    try:
        asyncio.run(_prepare_database(db_url))
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip", not "fail"
        pytest.skip(
            f"PostgreSQL is not reachable at {db_url!r} ({type(exc).__name__}: {exc}); "
            "F05's concurrent reproduction needs a real PostgreSQL -- see this "
            "file's module docstring for how to point it at one. The SQLite "
            "reproduction in tests/test_governance_decision_concurrency.py "
            "still runs."
        )
    yield db_url


@pytest_asyncio.fixture(params=ISOLATION_LEVELS)
async def engine(postgres_url: str, request: pytest.FixtureRequest) -> AsyncIterator[AsyncEngine]:
    """One engine per isolation level, with no shared connections.

    `NullPool` is deliberate: pooled connections would let two "concurrent"
    sessions be handed the same physical connection back-to-back, which is
    exactly the thing this file exists to rule out. `lock_timeout` is a
    safety net so a genuine lock cycle fails the test in seconds instead of
    hanging the suite -- PostgreSQL's own deadlock detector handles real
    deadlocks, but a bug that parks a coroutine on a lock forever would
    otherwise never report.
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
    """The isolation level the `engine` fixture was parametrized with."""
    return str(request.node.callspec.params["engine"])


class _AdapterCalls:
    """How many times a review's target adapter actually ran."""

    def __init__(self) -> None:
        self.count = 0


@pytest.fixture
def adapter_calls(monkeypatch: pytest.MonkeyPatch) -> _AdapterCalls:
    """Count invocations of the TERM_SEMANTIC_BINDING target adapter.

    This is the assertion that makes the compare-and-set falsifiable, and it
    was added because the obvious assertions were **not** enough. Removing
    `AND status = 'PENDING'` from the claim leaves most outcomes here
    unchanged: the loser's `UPDATE` then succeeds, it proceeds into the
    adapter, the adapter finds the binding no longer pending and raises its
    own 409, and the caller's rollback erases the bogus claim -- so the row
    still ends up decided once and the outbox still holds one event. The
    invariant survives, but it survives by accident, held up by a
    target-specific precondition instead of by the guard the service says
    owns it. An object type whose adapter is more permissive would not be so
    lucky.

    Counting adapter entries states the real invariant directly: a contended
    review runs its target's side-effect path **once**, because the loser is
    stopped before it. With the guard removed this count is 2 and every test
    that asserts it fails.
    """
    adapters = governance_decision_service._ADAPTERS
    real = adapters["TERM_SEMANTIC_BINDING"]
    calls = _AdapterCalls()

    async def counting(*args: Any, **kwargs: Any) -> Any:
        calls.count += 1
        return await real(*args, **kwargs)

    monkeypatch.setitem(adapters, "TERM_SEMANTIC_BINDING", counting)
    return calls


# --- seeding -----------------------------------------------------------------


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _context(organization_id: UUID, principal: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )


async def _seed(engine: AsyncEngine, *, count: int = 1) -> tuple[UUID, list[UUID], list[UUID]]:
    """A fresh organization and `count` pending TERM_SEMANTIC_BINDING reviews.

    Returns `(organization_id, review_ids, binding_ids)` -- ids, not
    instances, because every racer below loads its own copy in its own
    session on purpose. A fresh organization per test is what lets this file
    build the schema once per module and still count side effects exactly:
    nothing here is shared with another test.
    """
    async with _sessions(engine)() as setup:
        org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:12]}")
        setup.add(org)
        await setup.flush()
        term = GlossaryTerm(organization_id=org.id, term_key=f"term-{uuid4().hex[:12]}")
        setup.add(term)
        await setup.flush()
        review_ids: list[UUID] = []
        binding_ids: list[UUID] = []
        for _ in range(count):
            binding = TermSemanticBinding(
                organization_id=org.id,
                term_id=term.id,
                semantic_object_type="SEMANTIC_METRIC",
                semantic_object_id=uuid4(),
                status="PENDING_APPROVAL",
                requested_by="maker",
            )
            setup.add(binding)
            await setup.flush()
            review = GovernanceReview(
                organization_id=org.id,
                object_type="TERM_SEMANTIC_BINDING",
                object_id=str(binding.id),
                requested_action="APPROVE",
                requested_by="maker",
                status="PENDING",
            )
            setup.add(review)
            await setup.flush()
            review_ids.append(review.id)
            binding_ids.append(binding.id)
        await setup.commit()
        return org.id, review_ids, binding_ids


async def _load(session: AsyncSession, review_id: UUID) -> GovernanceReview:
    review = await session.get(GovernanceReview, review_id)
    assert review is not None
    return review


# --- observing the outcome ---------------------------------------------------


async def _final_state(
    engine: AsyncEngine, review_id: UUID, binding_id: UUID
) -> tuple[str, str | None, str, int, int]:
    """`(review status, decided_by, binding status, audit rows, outbox events)`
    read back on a fresh connection after every racer has finished."""
    async with _sessions(engine)() as reader:
        review = await _load(reader, review_id)
        binding = await reader.get(TermSemanticBinding, binding_id)
        assert binding is not None
        audits = await reader.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.resource_id == str(review_id))
        )
        events = await reader.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == str(binding_id))
        )
        return (
            review.status,
            review.decided_by,
            binding.status,
            int(audits or 0),
            int(events or 0),
        )


def _sqlstate(exc: BaseException) -> str | None:
    """The PostgreSQL SQLSTATE behind a driver error, if there is one."""
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def _refusal_kind(exc: BaseException) -> str:
    """Classify *which guard* refused a losing decider.

    Kept apart rather than collapsed into "it raised something", because
    which guard fired is the whole subject of this file:

    * ``claim_conflict`` -- `governance_decision_service.claim_review`'s
      compare-and-set saw `rowcount == 0` and refused with the review's
      refreshed state. This is the guard F05 added;
    * ``route_conflict`` -- a router's own already-decided check refused it
      with a plain 409 after its `FOR UPDATE` read unblocked. Correct, but a
      different guard, and on PostgreSQL it usually gets there first;
    * ``serialization_failure`` -- PostgreSQL aborted it at REPEATABLE READ
      because it could not re-read the row the winner had just replaced;
    * anything else is not a refusal and fails the caller's assertion.
    """
    if isinstance(exc, GovernanceDecisionRefused):
        return "claim_conflict" if exc.outcome == "CONFLICT" else f"refused:{exc.outcome}"
    if isinstance(exc, HTTPException):
        return "route_conflict" if exc.status_code == 409 else f"http:{exc.status_code}"
    if isinstance(exc, DBAPIError) and _sqlstate(exc) == SERIALIZATION_FAILURE:
        return "serialization_failure"
    return f"unexpected:{type(exc).__name__}"


def _expected_router_refusal(isolation_level: str) -> str:
    """How a router's losing caller is refused at this isolation level.

    Pinned, not widened. At READ COMMITTED the loser's `FOR UPDATE` unblocks,
    re-reads the decided row and is refused 409 by the route's own check. At
    REPEATABLE READ it cannot re-read: PostgreSQL aborts it with SQLSTATE
    40001 instead. Both keep the invariant; only one produces an error a
    caller can act on without a retry loop, which is the finding this
    distinction records.
    """
    return "route_conflict" if isolation_level == "READ COMMITTED" else "serialization_failure"


def _expected_claim_refusal(isolation_level: str) -> str:
    """How a lock-free (agent-shaped) losing decider is refused.

    At READ COMMITTED this must be the compare-and-set itself -- a
    `route_conflict` or an adapter's 409 here would mean the claim let a
    second caller through and something downstream caught it.
    """
    return "claim_conflict" if isolation_level == "READ COMMITTED" else "serialization_failure"


async def _race(*racers: Callable[[], Awaitable[Any]]) -> list[Any]:
    """Run every racer truly concurrently, releasing them from one barrier.

    Each racer opens its own session, does its reads, then waits here; only
    once *all* of them have a live transaction holding a PENDING read does
    any of them write. `return_exceptions=True` because losing is the
    expected outcome for all but one of them, and the caller asserts on the
    exception rather than on its absence.
    """
    return await asyncio.gather(*(racer() for racer in racers), return_exceptions=True)


def _partition(results: list[Any]) -> tuple[list[Any], list[BaseException]]:
    """Split gathered results into winners and raised refusals."""
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    return winners, losers


# ---------------------------------------------------------------------------
# 1. router shapes: single vs single, single vs bulk, bulk vs bulk
# ---------------------------------------------------------------------------


def _single(
    maker: async_sessionmaker[AsyncSession],
    *,
    organization_id: UUID,
    review_id: UUID,
    barrier: asyncio.Barrier,
    decision: str,
    principal: str,
) -> Callable[[], Awaitable[Any]]:
    """One `decide_governance_review` call on its own session and connection."""

    async def run() -> Any:
        async with maker() as session:
            await session.execute(select(func.now()))  # open the transaction first
            await barrier.wait()
            return await decide_governance_review(
                review_id,
                GovernanceDecisionRequest(decision=decision, reason=f"{principal} decided"),
                context=_context(organization_id, principal),
                session=session,
            )

    return run


def _bulk(
    maker: async_sessionmaker[AsyncSession],
    *,
    organization_id: UUID,
    review_ids: list[UUID],
    barrier: asyncio.Barrier,
    decision: str,
    principal: str,
) -> Callable[[], Awaitable[Any]]:
    """One `bulk_decide_governance_reviews` batch on its own session."""

    async def run() -> Any:
        async with maker() as session:
            await session.execute(select(func.now()))
            await barrier.wait()
            return await bulk_decide_governance_reviews(
                GovernanceReviewBulkDecisionRequest(
                    review_ids=review_ids, decision=decision, reason=f"{principal} swept"
                ),
                context=_context(organization_id, principal),
                session=session,
            )

    return run


async def test_single_vs_single_produces_one_terminal_decision(
    engine: AsyncEngine, isolation_level: str, adapter_calls: _AdapterCalls
) -> None:
    """Two checkers press Approve and Reject on the same review at the same
    instant, on two connections, released together.

    Exactly one decision stands. The target binding transitions once, one
    outbox event exists and one audit row names the review -- the loser
    contributes none of them, which is the F05 defect stated as a count
    rather than as "no exception was raised".
    """
    organization_id, review_ids, binding_ids = await _seed(engine)
    review_id, binding_id = review_ids[0], binding_ids[0]
    maker = _sessions(engine)
    barrier = asyncio.Barrier(2)

    results = await _race(
        _single(
            maker,
            organization_id=organization_id,
            review_id=review_id,
            barrier=barrier,
            decision="APPROVE",
            principal="checker-approve",
        ),
        _single(
            maker,
            organization_id=organization_id,
            review_id=review_id,
            barrier=barrier,
            decision="REJECT",
            principal="checker-reject",
        ),
    )

    winners, losers = _partition(results)
    assert len(winners) == 1, f"exactly one decider may win; got {results!r}"
    assert len(losers) == 1
    assert _refusal_kind(losers[0]) == _expected_router_refusal(isolation_level)
    assert adapter_calls.count == 1, "the loser must never reach the target adapter"

    status, decided_by, binding_status, audits, events = await _final_state(
        engine, review_id, binding_id
    )
    winner = winners[0]
    assert status == winner.status
    assert decided_by == winner.decided_by
    assert status in {"APPROVED", "REJECTED"}
    assert binding_status == ("ACTIVE" if status == "APPROVED" else "REJECTED")
    assert events == 1, "one terminal decision, one outbox event"
    assert audits == 1, "one terminal decision, one audit row naming the review"


async def test_single_vs_bulk_produces_one_terminal_decision(
    engine: AsyncEngine, isolation_level: str, adapter_calls: _AdapterCalls
) -> None:
    """A single-item Approve and a bulk Reject sweep hit the same review
    simultaneously. One applies; the other is refused, and the review's
    target object transitions exactly once."""
    organization_id, review_ids, binding_ids = await _seed(engine)
    review_id, binding_id = review_ids[0], binding_ids[0]
    maker = _sessions(engine)
    barrier = asyncio.Barrier(2)

    results = await _race(
        _single(
            maker,
            organization_id=organization_id,
            review_id=review_id,
            barrier=barrier,
            decision="APPROVE",
            principal="checker-single",
        ),
        _bulk(
            maker,
            organization_id=organization_id,
            review_ids=[review_id],
            barrier=barrier,
            decision="REJECT",
            principal="checker-bulk",
        ),
    )

    applied, refused = _bulk_aware_partition(results, isolation_level)
    assert applied == 1, f"exactly one decider may win; got {results!r}"
    assert refused == 1
    assert adapter_calls.count == 1, "the loser must never reach the target adapter"

    status, decided_by, binding_status, _, events = await _final_state(
        engine, review_id, binding_id
    )
    assert status in {"APPROVED", "REJECTED"}
    assert decided_by == ("checker-single" if status == "APPROVED" else "checker-bulk")
    assert binding_status == ("ACTIVE" if status == "APPROVED" else "REJECTED")
    assert events == 1, "the loser must not add a second outbox event"


async def test_bulk_vs_bulk_produces_one_terminal_decision(
    engine: AsyncEngine, isolation_level: str, adapter_calls: _AdapterCalls
) -> None:
    """Two bulk sweeps overlap on one review and each also carries a review
    only it selected.

    The overlapping review is decided once. Both batches' private items are
    unaffected by the contention on the shared one -- PG-3's per-item
    savepoints, verified while a real row lock is in play rather than on an
    engine where `FOR UPDATE` compiles away.
    """
    organization_id, review_ids, binding_ids = await _seed(engine, count=3)
    shared, first_only, second_only = review_ids
    shared_binding = binding_ids[0]
    maker = _sessions(engine)
    barrier = asyncio.Barrier(2)

    results = await _race(
        _bulk(
            maker,
            organization_id=organization_id,
            review_ids=[shared, first_only],
            barrier=barrier,
            decision="APPROVE",
            principal="checker-one",
        ),
        _bulk(
            maker,
            organization_id=organization_id,
            review_ids=[shared, second_only],
            barrier=barrier,
            decision="REJECT",
            principal="checker-two",
        ),
    )

    applied, refused = _bulk_aware_partition(results, isolation_level, review_id=shared)
    assert applied == 1, f"the shared review may be applied once; got {results!r}"
    assert refused == 1
    # The adapter ran once per *applied* item and not once more. How many
    # items get applied in total is isolation-dependent -- at REPEATABLE READ
    # the losing batch aborts whole, taking its own uncontended item with it
    # -- so this is stated as an equality against what the batches reported
    # rather than as a fixed number.
    assert adapter_calls.count == _total_applied(results)

    status, _, binding_status, _, events = await _final_state(engine, shared, shared_binding)
    assert status in {"APPROVED", "REJECTED"}
    assert binding_status == ("ACTIVE" if status == "APPROVED" else "REJECTED")
    assert events == 1, "one terminal decision on the shared review, one outbox event"


def _total_applied(results: list[Any]) -> int:
    """How many decisions the racers between them reported as applied.

    Three racer shapes report success three ways: the single-item route
    returns the decided `GovernanceReview`, a lock-free claimer returns its
    principal, and a bulk batch returns per-item outcomes.
    """
    total = 0
    for item in results:
        if isinstance(item, BaseException):
            continue
        if isinstance(item, GovernanceReview | str):
            total += 1
            continue
        total += sum(1 for entry in item.results if entry.outcome == "APPLIED")
    return total


def _refusal_labels(results: list[Any]) -> set[str]:
    """Every way a racer in `results` was told it did not decide the review:
    the guard that raised, or a bulk item's own non-APPLIED outcome."""
    labels: set[str] = set()
    for item in results:
        if isinstance(item, BaseException):
            labels.add(_refusal_kind(item))
        elif not isinstance(item, GovernanceReview | str):
            labels.update(entry.outcome for entry in item.results if entry.outcome != "APPLIED")
    return labels


def _bulk_aware_partition(
    results: list[Any], isolation_level: str, *, review_id: UUID | None = None
) -> tuple[int, int]:
    """Count `(applied, refused)` across mixed single/bulk racer results.

    A bulk batch reports per-item outcomes instead of raising, so a losing
    batch is a *successful* call carrying a CONFLICT item. Collapsing the two
    shapes here keeps every caller asserting the same invariant -- exactly one
    application, exactly one refusal -- rather than each re-deriving it.
    """
    applied = 0
    refused = 0
    for item in results:
        if isinstance(item, BaseException):
            assert _refusal_kind(item) == _expected_router_refusal(isolation_level), (
                f"a losing decider must be refused, not {item!r}"
            )
            refused += 1
            continue
        if isinstance(item, GovernanceReview):  # the single-item route's return
            applied += 1
            continue
        outcomes = {
            entry.review_id: entry.outcome
            for entry in item.results
            if review_id is None or entry.review_id == str(review_id)
        }
        for outcome in outcomes.values():
            if outcome == "APPLIED":
                applied += 1
            else:
                assert outcome == "CONFLICT", f"a losing batch item must be CONFLICT, not {item!r}"
                refused += 1
    return applied, refused


# ---------------------------------------------------------------------------
# 2. the compare-and-set itself, with no row lock in front of it
# ---------------------------------------------------------------------------


def _claimer(
    maker: async_sessionmaker[AsyncSession],
    *,
    organization_id: UUID,
    review_id: UUID,
    barrier: asyncio.Barrier,
    decision: str,
    principal: str,
) -> Callable[[], Awaitable[str]]:
    """A lock-free decider: read the review, wait for everyone else to have
    read it too, then run `decide_review` -- which is only the compare-and-set
    plus the target adapter. This is the reviewer agent's shape (ADR-0027)
    and the one the routers' `FOR UPDATE` would otherwise hide.
    """

    async def run() -> str:
        async with maker() as session:
            review = await _load(session, review_id)
            assert review.status == "PENDING", "every racer must start from a PENDING read"
            await barrier.wait()
            effect = await decide_review(
                session,
                review,
                decision=decision,
                reason=f"{principal} decided",
                context=_context(organization_id, principal),
                now=datetime.now(UTC),
            )
            record_decision_outbox(session, review, effect)
            await session.commit()
            return principal

    return run


async def test_service_claim_race_refuses_the_loser_with_refreshed_state(
    engine: AsyncEngine, isolation_level: str, adapter_calls: _AdapterCalls
) -> None:
    """The compare-and-set, raced directly on PostgreSQL.

    Both sessions read the review while it is PENDING, then issue their
    `UPDATE ... WHERE status = 'PENDING'` simultaneously. The loser's
    statement blocks on the winner's row lock; at READ COMMITTED it then
    re-evaluates the predicate against the winner's committed row, matches
    nothing, and is refused `CONFLICT` carrying the *refreshed* state -- the
    winner's status, principal and reason, not the stale PENDING it read.

    This is the assertion the review asked for and the one the SQLite
    reproduction could not make: there, the loser saw a row that had already
    changed before it started, never a row that changed underneath a
    statement it had already issued.
    """
    organization_id, review_ids, binding_ids = await _seed(engine)
    review_id, binding_id = review_ids[0], binding_ids[0]
    maker = _sessions(engine)
    barrier = asyncio.Barrier(2)

    results = await _race(
        _claimer(
            maker,
            organization_id=organization_id,
            review_id=review_id,
            barrier=barrier,
            decision="APPROVE",
            principal="claimer-approve",
        ),
        _claimer(
            maker,
            organization_id=organization_id,
            review_id=review_id,
            barrier=barrier,
            decision="REJECT",
            principal="claimer-reject",
        ),
    )

    winners, losers = _partition(results)
    assert len(winners) == 1, f"the claim must admit exactly one caller; got {results!r}"
    assert len(losers) == 1
    loser = losers[0]
    assert adapter_calls.count == 1, (
        "the claim precedes the target adapter, so the loser must never enter it"
    )
    assert _refusal_kind(loser) == _expected_claim_refusal(isolation_level), (
        f"the compare-and-set must be the guard that refuses here, not {loser!r}"
    )

    status, decided_by, binding_status, _, events = await _final_state(
        engine, review_id, binding_id
    )
    assert decided_by == winners[0]
    assert status == ("APPROVED" if decided_by == "claimer-approve" else "REJECTED")
    assert binding_status == ("ACTIVE" if status == "APPROVED" else "REJECTED")
    assert events == 1, "the loser never reached the target adapter or the outbox"

    if isolation_level == "READ COMMITTED":
        assert isinstance(loser, GovernanceDecisionRefused)
        assert loser.http_status == 409
        state = loser.review_state
        assert state is not None, "the loser must be handed the review's refreshed state"
        assert state.status == status, "refreshed state, not the PENDING the loser read"
        assert state.decided_by == decided_by
        assert state.decision_reason == f"{decided_by} decided"


async def test_three_way_claim_race_admits_exactly_one(
    engine: AsyncEngine, isolation_level: str, adapter_calls: _AdapterCalls
) -> None:
    """Three simultaneous deciders, one review. The invariant is not "the
    second caller loses" but "all but one lose", so it is worth stating with
    more than two."""
    organization_id, review_ids, binding_ids = await _seed(engine)
    review_id, binding_id = review_ids[0], binding_ids[0]
    maker = _sessions(engine)
    barrier = asyncio.Barrier(3)

    results = await _race(
        *(
            _claimer(
                maker,
                organization_id=organization_id,
                review_id=review_id,
                barrier=barrier,
                decision="APPROVE" if index else "REJECT",
                principal=f"claimer-{index}",
            )
            for index in range(3)
        )
    )

    winners, losers = _partition(results)
    assert len(winners) == 1, f"one review, one decision, however many callers; got {results!r}"
    assert adapter_calls.count == 1, "two losers, zero extra trips through the adapter"
    assert {_refusal_kind(loser) for loser in losers} == {_expected_claim_refusal(isolation_level)}

    status, decided_by, _, _, events = await _final_state(engine, review_id, binding_id)
    assert decided_by == winners[0]
    assert status in {"APPROVED", "REJECTED"}
    assert events == 1, "two losers, zero extra outbox events"


async def test_bulk_sweep_vs_lock_free_decider_produces_one_terminal_decision(
    engine: AsyncEngine, isolation_level: str, adapter_calls: _AdapterCalls
) -> None:
    """A human bulk sweep and a lock-free automated decider, at once.

    This is the production shape the compare-and-set exists for: the batch
    takes `FOR UPDATE` row locks, the agent-shaped caller takes none, and
    nothing about their ordering is arranged. Whichever arrives second is
    refused, and only one of the two possible refusals runs through the
    claim -- so this test asserts the invariant and reports which guard
    caught it, rather than assuming.
    """
    organization_id, review_ids, binding_ids = await _seed(engine)
    review_id, binding_id = review_ids[0], binding_ids[0]
    maker = _sessions(engine)
    barrier = asyncio.Barrier(2)

    results = await _race(
        _bulk(
            maker,
            organization_id=organization_id,
            review_ids=[review_id],
            barrier=barrier,
            decision="REJECT",
            principal="checker-bulk",
        ),
        _claimer(
            maker,
            organization_id=organization_id,
            review_id=review_id,
            barrier=barrier,
            decision="APPROVE",
            principal="agent-claimer",
        ),
    )

    assert _total_applied(results) == 1, f"exactly one decider may win; got {results!r}"
    assert adapter_calls.count == 1, "the loser must never reach the target adapter"
    assert _refusal_labels(results) <= {
        _expected_claim_refusal(isolation_level),
        _expected_router_refusal(isolation_level),
        "CONFLICT",
    }, f"the loser must be refused as a conflict; got {results!r}"

    status, _, binding_status, _, events = await _final_state(engine, review_id, binding_id)
    assert status in {"APPROVED", "REJECTED"}
    assert binding_status == ("ACTIVE" if status == "APPROVED" else "REJECTED")
    assert events == 1, "one terminal decision, one outbox event"


async def test_disjoint_reviews_do_not_block_each_other(
    engine: AsyncEngine, adapter_calls: _AdapterCalls
) -> None:
    """The control. Two simultaneous deciders on two *different* reviews both
    succeed -- so the single-winner results above are contention on a shared
    row, not this file accidentally serializing everything it does.
    """
    organization_id, review_ids, binding_ids = await _seed(engine, count=2)
    maker = _sessions(engine)
    barrier = asyncio.Barrier(2)

    results = await _race(
        *(
            _claimer(
                maker,
                organization_id=organization_id,
                review_id=review_id,
                barrier=barrier,
                decision="APPROVE",
                principal=f"claimer-{index}",
            )
            for index, review_id in enumerate(review_ids)
        )
    )

    winners, losers = _partition(results)
    assert losers == [], f"uncontended decisions must not be refused; got {results!r}"
    assert sorted(winners) == ["claimer-0", "claimer-1"]
    assert adapter_calls.count == 2, "two uncontended reviews, two adapter runs"

    for index, (review_id, binding_id) in enumerate(zip(review_ids, binding_ids, strict=True)):
        status, decided_by, binding_status, _, events = await _final_state(
            engine, review_id, binding_id
        )
        assert status == "APPROVED"
        assert decided_by == f"claimer-{index}"
        assert binding_status == "ACTIVE"
        assert events == 1
