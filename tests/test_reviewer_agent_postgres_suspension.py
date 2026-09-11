"""AR-04: how long a suspension takes to stop concurrent reviewer agents.

`tests/test_reviewer_agent.py::test_ar04_a_suspension_raised_mid_batch_stops_the_batch`
proves the guard bounds the stop at one item -- but it does so inside a single
SQLite session, where the suspension is written by the same session that reads
it. That arrangement cannot fail: the writer always sees its own write. The
2026-09-09 architecture review (AR-04) asked for the experiment this file is:
concurrent workers, on separate connections to a real PostgreSQL, stopped by a
suspension committed by *someone else*, with the stop delay measured rather
than asserted.

**The property.** `reviewer_agent.auto_decide_tier0_tier1` re-reads
`ReviewerAgentState.suspended` before each item (Guard 0), with
`populate_existing=True` so the read goes to the database rather than the
identity map. The claim in that guard's own comment is:

    "Bound on the stop: one item, and only under an isolation level where a
     statement sees rows committed since the transaction began -- READ
     COMMITTED, which is this platform's default. Under REPEATABLE READ the
     re-read returns the snapshot and the batch runs to its limit; that is a
     property of the isolation level, not something a check here can defeat."

Both halves of that are measured here, at both isolation levels, and the
second half is the reason `REPEATABLE_READ_IS_UNBOUNDED` below is an assertion
rather than a caveat in prose: the comment claims a *known* weakness, and a
known weakness that nothing exercises is how a regression gets called a fix.

**What "stop delay" means here.** Each worker records the monotonic time at
which each of its decisions was made. The controller records the monotonic
time at which the suspension's COMMIT returned. A worker's stop delay is the
number of its own decisions made strictly after that instant.

Made, not committed, and the difference is itself a finding: `reviewer_agent`
never commits. It leaves transaction control to its caller, so a batch is one
transaction and its decisions land or roll back as a unit. Forcing a commit
per item inside this test would have measured a boundary the application does
not have. The measurement is deliberately in *items*, not seconds: the operator-
visible question is "how many more things can this agent decide after I hit
suspend", and seconds are a property of the machine the test ran on.

**Scope.** In the suspension experiment each worker gets its own
organization, and the suspension is committed for every organization from one
connection at one moment. That isolates the property under test -- a re-read
seeing another connection's committed write -- from row contention between
workers competing for the *same* organization's queue. Contention is measured
separately, at the end of this file: several committing workers on one queue,
whether every item is decided exactly once, and whether a lost race skips an
item or aborts a batch.

Pointing this at a PostgreSQL: `AIDA_REVIEWER_SUSPENSION_TEST_DATABASE_URL`,
or let it derive a scratch database from `Settings.database_url`. It uses its
own scratch database name, distinct from the AR-05 budget race's, because both
files drop and rebuild the schema they own.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
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

import aida.models  # noqa: F401 -- registers every table on the metadata
import aida.semantic_api  # noqa: F401 -- registers the decision target adapters
from aida.config import Settings, get_settings
from aida.db import Base
from aida.models import (
    AssetDescriptionDraft,
    AuditEvent,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.reviewer_agent import (
    REASON_SUSPENDED,
    ReviewerAgentUnavailable,
    auto_decide_tier0_tier1,
    pre_review_pending,
    set_suspended,
)
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

ISOLATION_LEVELS = ("READ COMMITTED", "REPEATABLE READ")

#: Workers racing one suspension. Four is enough to show a per-worker maximum
#: rather than a single sample, and small enough that a lock cycle surfaces as
#: a timeout in seconds rather than a hung suite.
WORKERS = 4

#: Items seeded per worker. Comfortably more than any worker should reach
#: after the suspension commits, so "stopped within one item" is a real bound
#: and not an artefact of running out of work.
ITEMS_PER_WORKER = 8

#: The bound the guard claims at READ COMMITTED, in items.
MAX_STOP_DELAY_ITEMS = 1

#: At REPEATABLE READ the re-read is served from the transaction's snapshot,
#: so the suspension is invisible and the batch runs to its limit. Asserted,
#: not excused: see the module docstring.
REPEATABLE_READ_IS_UNBOUNDED = True


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "reviewer_agent_enabled": True,
        "reviewer_agent_principal_id": "agent:reviewer",
        "reviewer_agent_max_tier": "T1",
        "reviewer_agent_sampling_rate": 0.05,
        "reviewer_agent_suspended": False,
        # This experiment is about Guard 0. The audit-backlog precondition
        # (AR-11) and evidence staleness (Guard 1) have their own tests, and
        # leaving them live here would stop batches for reasons that are not
        # the suspension being measured.
        "reviewer_agent_max_unresolved_samples": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# --- reaching (or skipping) a real PostgreSQL --------------------------------


def _test_database_url() -> str:
    override = os.environ.get("AIDA_REVIEWER_SUSPENSION_TEST_DATABASE_URL")
    if override:
        return override
    default_url = get_settings().database_url
    root, _, dbname = default_url.rpartition("/")
    if not root or not dbname:
        raise AssertionError(
            f"Settings.database_url {default_url!r} doesn't look like a "
            "'.../<dbname>' URL; cannot derive a scratch database name from it."
        )
    return f"{root}/{dbname}_reviewer_suspension_test"


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
    db_url = _test_database_url()
    try:
        asyncio.run(_prepare_database(db_url))
    except Exception as exc:  # noqa: BLE001 -- a connection failure means "skip", not "fail"
        pytest.skip(
            f"PostgreSQL is not reachable at {db_url!r} ({type(exc).__name__}: {exc}); "
            "AR-04's multi-worker suspension experiment needs a real PostgreSQL -- "
            "see this file's module docstring. The single-session reproduction in "
            "tests/test_reviewer_agent.py still runs."
        )
    yield db_url


@pytest_asyncio.fixture(params=ISOLATION_LEVELS)
async def engine(postgres_url: str, request: pytest.FixtureRequest) -> AsyncIterator[AsyncEngine]:
    """One engine per isolation level, with no shared connections.

    `NullPool` is deliberate, for the same reason the AR-05 race uses it:
    pooled connections would let two "concurrent" workers be handed the same
    physical connection, which is the thing this file exists to rule out.
    """
    created = create_async_engine(
        postgres_url,
        isolation_level=request.param,
        poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "15s"}},
    )
    yield created
    await created.dispose()


@pytest.fixture
def isolation_level(request: pytest.FixtureRequest) -> str:
    return str(request.node.callspec.params["engine"])


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# --- seeding -----------------------------------------------------------------


async def _seed_worker_org(
    engine: AsyncEngine, items: int, *, table_per_item: bool = False
) -> UUID:
    """One organization with `items` genuinely pre-reviewed APPROVE rows.

    With `table_per_item`, each draft describes its own table. The contention
    experiment needs that: drafts sharing a table would also collide on that
    table's documentation row as each approval publishes, which is a different
    race from the one it measures.

    The full catalog chain (organization -> LOB -> domain -> project ->
    datasource -> catalog -> schema -> table) is built because
    `AssetDescriptionDraft.table_id` carries a real foreign key. SQLite does
    not enforce it, so `tests/test_reviewer_agent.py` seeds a bare `uuid4()`
    there and passes; on PostgreSQL that is a `ForeignKeyViolationError`. The
    draft rows this experiment decides are therefore attached to a table that
    actually exists, which is also the shape the API creates.
    """
    async with _sessions(engine)() as setup:
        org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:12]}")
        lob = LineOfBusiness(
            id=uuid4(),
            organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
        )
        domain = DataDomain(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            name="Retail Banking",
            code=f"RB{uuid4().hex[:6]}",
        )
        project = Project(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            data_domain_id=domain.id,
            name="Core Banking",
            slug=f"core-banking-{uuid4().hex[:8]}",
        )
        datasource = DataSource(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            data_domain_id=domain.id,
            project_id=project.id,
            name="core-warehouse",
            connector_type="postgres",
            dialect="postgres",
            environment="TEST",
            credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
            status="ACTIVE",
        )
        # Added and flushed one at a time, in dependency order. These models
        # carry plain FK columns with no `relationship()`, so SQLAlchemy's unit
        # of work has nothing to topologically sort by and emits INSERTs in
        # whatever order `add_all` received them -- which SQLite tolerates and
        # PostgreSQL refuses.
        for row in (org, lob, domain, project, datasource):
            setup.add(row)
            await setup.flush()

        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            name="core",
            fingerprint=f"fp-catalog-{uuid4().hex[:8]}",
        )
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=org.id,
            catalog_id=catalog.id,
            name="retail",
            fingerprint=f"fp-schema-{uuid4().hex[:8]}",
        )
        for row in (catalog, schema):
            setup.add(row)
            await setup.flush()
        async def new_table() -> MetadataTable:
            table = MetadataTable(
                id=uuid4(),
                organization_id=org.id,
                datasource_id=datasource.id,
                schema_id=schema.id,
                name=f"customer_master_{uuid4().hex[:8]}",
                object_type="TABLE",
                fingerprint=f"fp-table-{uuid4().hex[:8]}",
            )
            setup.add(table)
            await setup.flush()
            return table

        shared_table = None if table_per_item else await new_table()

        for _ in range(items):
            table = shared_table if shared_table is not None else await new_table()
            review = GovernanceReview(
                organization_id=org.id,
                object_type="ASSET_DESCRIPTION_DRAFT",
                object_id=str(uuid4()),
                requested_action="PUBLISH",
                status="PENDING",
                requested_by="steward-a",
            )
            setup.add(review)
            await setup.flush()
            draft = AssetDescriptionDraft(
                organization_id=org.id,
                table_id=table.id,
                drafted_text="Customer master, one row per customer.",
                text_fingerprint="f" * 64,
                accuracy_score=0.95,
                clarity_score=0.95,
                style_score=0.95,
                completeness_score=0.95,
                overall_score=0.95,
                evidence={"source": "deterministic"},
                status="PENDING_APPROVAL",
                governance_review_id=review.id,
                created_by="steward-a",
            )
            setup.add(draft)
            await setup.flush()
            review.object_id = str(draft.id)
        await setup.flush()
        await pre_review_pending(setup, org.id, settings=_settings())
        await setup.commit()
        return org.id


# --- the experiment ----------------------------------------------------------


class _Recorder:
    """Decision timestamps, and the instant the suspension became visible.

    Keyed by the worker's own `AsyncSession`, which is the only thing the
    patched `decide_review` is handed that identifies who is calling.
    """

    def __init__(self) -> None:
        self.decisions: list[tuple[UUID, float]] = []
        self.suspended_at: float | None = None
        self.first_decision = asyncio.Event()
        self._org_of_session: dict[int, UUID] = {}

    def register(self, session: AsyncSession, org_id: UUID) -> None:
        self._org_of_session[id(session)] = org_id

    def record_for(self, session: AsyncSession) -> None:
        org_id = self._org_of_session[id(session)]
        self.decisions.append((org_id, time.monotonic()))
        self.first_decision.set()

    def decided(self, org_id: UUID) -> int:
        return sum(1 for (o, _) in self.decisions if o == org_id)

    def stop_delay(self, org_id: UUID) -> int:
        """Decisions this organization made after the suspension committed."""
        assert self.suspended_at is not None
        return sum(1 for (o, t) in self.decisions if o == org_id and t > self.suspended_at)


async def _run_worker(
    engine: AsyncEngine, org_id: UUID, recorder: _Recorder
) -> str:
    """One reviewer-agent batch on its own connection.

    Returns why it ended: `"suspended"` if the guard stopped it, `"exhausted"`
    if it ran out of work first (which at READ COMMITTED would mean the
    suspension never reached it -- a failure of the property, reported as a
    distinct outcome rather than as a passing test).

    Attribution is by session, registered with the recorder here. An earlier
    version of this file patched `reviewer.decide_review` per worker from
    inside the worker, which is wrong in a way that produced a *passing* test:
    concurrent workers each captured the previous worker's already-patched
    function as their `original`, so the wrappers nested and one real decision
    was recorded up to four times, against whichever `org_id` each layer's
    closure held. It reported 128 decisions from 32 seeded rows. The patch is
    now installed once, by `_recording`, and reads the session it was handed.
    """
    async with _sessions(engine)() as session:
        recorder.register(session, org_id)
        try:
            await auto_decide_tier0_tier1(session, org_id, settings=_settings())
        except ReviewerAgentUnavailable as exc:
            assert exc.reason_code == REASON_SUSPENDED
            return "suspended"
        return "exhausted"


@asynccontextmanager
async def _recording(recorder: _Recorder) -> AsyncIterator[None]:
    """Patch `decide_review` once, for the whole fleet."""
    import aida.reviewer_agent as reviewer

    original = reviewer.decide_review

    async def recording_decide(*args: object, **kwargs: object) -> object:
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        # Recorded when the decision is MADE, not when it commits.
        # `reviewer_agent` never commits -- it leaves transaction control to
        # its caller, so the whole batch is one transaction and forcing a
        # commit here would fabricate a boundary the application does not
        # have. "How many more items did it decide after I hit suspend" is
        # answered by decisions made, and every one of them is staged in the
        # same transaction that will or will not be committed as a unit.
        session = kwargs.get("session", args[0] if args else None)
        assert isinstance(session, AsyncSession)
        recorder.record_for(session)
        return result

    reviewer.decide_review = recording_decide  # type: ignore[assignment]
    try:
        yield
    finally:
        reviewer.decide_review = original  # type: ignore[assignment]


async def _suspend_everything(
    engine: AsyncEngine, org_ids: list[UUID], recorder: _Recorder
) -> None:
    """Wait for the fleet to be genuinely working, then commit one suspension.

    On its own connection, which is the whole point: a worker must learn about
    a write it did not make.
    """
    await asyncio.wait_for(recorder.first_decision.wait(), timeout=30)
    async with _sessions(engine)() as suspender:
        for org_id in org_ids:
            await set_suspended(
                suspender,
                org_id,
                suspended=True,
                context=security_context(organization_id=org_id, principal_id="risk-officer"),
                reason="spike",
            )
        await suspender.commit()
    recorder.suspended_at = time.monotonic()


async def test_ar04_a_committed_suspension_stops_concurrent_workers_within_one_item(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """The measurement AR-04 asked for, at both isolation levels."""
    org_ids = [await _seed_worker_org(engine, ITEMS_PER_WORKER) for _ in range(WORKERS)]
    recorder = _Recorder()

    async with _recording(recorder):
        outcomes = await asyncio.gather(
            *[_run_worker(engine, org_id, recorder) for org_id in org_ids],
            _suspend_everything(engine, org_ids, recorder),
        )
    worker_outcomes = list(outcomes[:WORKERS])
    delays = {org_id: recorder.stop_delay(org_id) for org_id in org_ids}
    # An experiment reports its measurement. `pytest -s` shows it; the
    # assertions below are the pass/fail, this is the number they are about.
    print(
        f"\n[AR-04] {isolation_level}: {WORKERS} workers, "
        f"{ITEMS_PER_WORKER} items each, {len(recorder.decisions)} decisions total "
        f"(per worker {sorted(recorder.decided(o) for o in org_ids)}); "
        f"per-worker stop delay (items decided after the suspension committed) = "
        f"{sorted(delays.values())}; outcomes = {worker_outcomes}"
    )

    if isolation_level == "READ COMMITTED":
        # The bound the guard claims. Every worker must have been stopped BY
        # the suspension -- a worker that merely ran out of work would give a
        # delay of 0 and hide a guard that never fired.
        assert worker_outcomes == ["suspended"] * WORKERS, (
            f"not every worker was stopped by the suspension: {worker_outcomes}"
        )
        worst = max(delays.values())
        assert worst <= MAX_STOP_DELAY_ITEMS, (
            f"maximum stop delay was {worst} items across {WORKERS} workers "
            f"(per-worker: {sorted(delays.values())}); AR-04 claims a bound of "
            f"{MAX_STOP_DELAY_ITEMS}"
        )
    else:
        # REPEATABLE READ: each worker's re-read is served from the snapshot
        # its transaction opened with, so the suspension is invisible and the
        # batch runs to its limit. This is the documented weakness; asserting
        # it means a change that silently "fixed" or worsened it is visible.
        assert REPEATABLE_READ_IS_UNBOUNDED
        assert worker_outcomes == ["exhausted"] * WORKERS, (
            "REPEATABLE READ is expected to hide the suspension for the life of "
            f"the transaction, but workers reported {worker_outcomes}"
        )


async def test_ar04_a_suspension_committed_before_the_batch_stops_it_at_entry(
    engine: AsyncEngine,
) -> None:
    """The control for the experiment above.

    If a suspension committed *before* a worker's transaction begins did not
    stop it, a zero stop delay would prove nothing about the guard -- the
    entry check in `auto_decide_tier0_tier1` would be doing the work. This
    holds at both isolation levels, because the snapshot a transaction opens
    with already contains the committed suspension.
    """
    org_id = await _seed_worker_org(engine, ITEMS_PER_WORKER)
    async with _sessions(engine)() as suspender:
        await set_suspended(
            suspender,
            org_id,
            suspended=True,
            context=security_context(organization_id=org_id, principal_id="risk-officer"),
            reason="pre-existing",
        )
        await suspender.commit()

    recorder = _Recorder()
    async with _recording(recorder):
        assert await _run_worker(engine, org_id, recorder) == "suspended"
    assert recorder.decisions == []


# --- contention: several workers on one organization's queue ------------------

#: Workers racing for one organization's queue.
CONTENDING_WORKERS = 3

#: Items in that queue.
CONTENDED_ITEMS = 12

#: PostgreSQL's SQLSTATE for "could not serialize access due to concurrent
#: update".
SERIALIZATION_FAILURE = "40001"


def _sqlstate(error: BaseException) -> str | None:
    """The SQLSTATE behind a driver error, wherever the driver put it."""
    origin = getattr(error, "orig", None)
    for candidate in (error, origin, getattr(origin, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


async def _run_committing_worker(engine: AsyncEngine, org_id: UUID) -> tuple[str, int]:
    """One reviewer-agent batch that commits, as a real caller does.

    `_run_worker` above never commits, which is right for measuring when a
    worker stops; contention only exists once decisions land. Returns how the
    batch ended -- `"committed"`, or `"aborted:<SQLSTATE>"` -- and how many
    decisions it committed.
    """
    async with _sessions(engine)() as session:
        try:
            outcomes = await auto_decide_tier0_tier1(session, org_id, settings=_settings())
            await session.commit()
        except DBAPIError as exc:
            await session.rollback()
            return (f"aborted:{_sqlstate(exc)}", 0)
        return ("committed", len(outcomes))


async def _decided(engine: AsyncEngine, org_id: UUID) -> tuple[int, int]:
    """Reviews in the terminal state, and the reviewer agent's audit rows."""
    async with _sessions(engine)() as reader:
        approved = await reader.scalar(
            select(func.count())
            .select_from(GovernanceReview)
            .where(
                GovernanceReview.organization_id == org_id,
                GovernanceReview.status == "APPROVED",
            )
        )
        audited = await reader.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.organization_id == org_id,
                AuditEvent.action == "reviewer_agent.decide",
            )
        )
    return int(approved or 0), int(audited or 0)


async def test_ar04_workers_racing_one_organization_decide_each_item_exactly_once(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """AR-04's contention half: several committing workers on one queue.

    `auto_decide_tier0_tier1` takes no row locks and claims each review with a
    compare-and-set, each item in its own savepoint, catching only the decision
    service's own refusal. This measures what that does under a real race:
    whether every item is still decided exactly once, and whether a worker that
    loses a race skips the item or loses its whole batch.
    """
    org_id = await _seed_worker_org(engine, CONTENDED_ITEMS, table_per_item=True)
    started = time.monotonic()
    results = await asyncio.gather(
        *[_run_committing_worker(engine, org_id) for _ in range(CONTENDING_WORKERS)]
    )
    elapsed = time.monotonic() - started
    approved, audited = await _decided(engine, org_id)
    outcomes = [outcome for outcome, _count in results]
    committed = [count for _outcome, count in results]
    print(
        f"\n[AR-04 contention] {isolation_level}: {CONTENDING_WORKERS} workers on one "
        f"organization's {CONTENDED_ITEMS} items in {elapsed:.2f}s; outcomes = {outcomes}; "
        f"decisions committed per worker = {committed}; "
        f"reviews approved = {approved}, reviewer-agent audit rows = {audited}"
    )

    # Exactly once, at both isolation levels: every item decided, none twice,
    # and one audit row per decision.
    assert approved == CONTENDED_ITEMS
    assert audited == CONTENDED_ITEMS
    assert sum(committed) == CONTENDED_ITEMS

    if isolation_level == "READ COMMITTED":
        # A lost race is a skipped item, never a lost batch: once the winner
        # commits, the loser's compare-and-set finds the row no longer PENDING
        # and the decision service refuses it inside the item's savepoint.
        assert outcomes == ["committed"] * CONTENDING_WORKERS, outcomes
    else:
        # REPEATABLE READ: the loser's claim meets a row a concurrent
        # transaction changed, which PostgreSQL refuses outright. Only the
        # service's own refusal is caught, so the loser's whole batch rolls
        # back; nothing it decided lands, and the winner's decisions stand.
        assert "committed" in outcomes, outcomes
        assert all(
            outcome in ("committed", f"aborted:{SERIALIZATION_FAILURE}") for outcome in outcomes
        ), outcomes
