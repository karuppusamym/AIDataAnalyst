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
identity map. The bound that re-read buys is **one further item per worker**,
and because workers share no transaction it does not degrade with the number
of workers: N workers on one queue can decide at most N more items between
the suspension's COMMIT and their stop.

**2026-09-12 (R11-C4).** That bound holds only where a statement sees rows
committed since its transaction began -- READ COMMITTED. Under REPEATABLE
READ and SERIALIZABLE the re-read is served from the batch's own snapshot,
the suspension stays invisible for the life of the transaction, and the batch
runs to its `limit`. This file used to *assert* that weakness
(`REPEATABLE_READ_IS_UNBOUNDED`) against a comment in Guard 0 that said the
code should be run at READ COMMITTED. Nothing enforced it, so a deployment
that set a stricter default -- the direction an operator reaches for when
they want more safety -- silently bought an unbounded agent.

`reviewer_agent.refuse_unsupported_isolation` now makes that a precondition:
the agent refuses to start at any level but READ COMMITTED, with a named
reason code. So the assertions below changed shape. At READ COMMITTED the
bound is measured, both on separate queues and -- the case R11-C4 was
raised for -- on one shared queue. At REPEATABLE READ the measurement is
that the batch never starts. `test_ar04_snapshot_isolation_hides_a_committed_
suspension` keeps the *reason* for that refusal under test at the SQL level,
so a later reader cannot mistake the enforcement for paranoia and relax it.

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

**Scope.** The first experiment gives each worker its own organization, and
commits the suspension for every organization from one connection at one
moment. That isolates the property -- a re-read seeing another connection's
committed write -- from row contention between workers competing for the
*same* organization's queue. Contention is measured separately: several
committing workers on one queue, whether every item is decided exactly once,
and whether a lost race skips an item or aborts a batch.

Separate organizations are not enough on their own, and R11-C4 is the row
that said so: two workers on two organizations never contend for a row, so
that experiment proves tenant isolation and says nothing about whether a
shared queue lets each worker independently conclude it is still fine and
race past the limit. The last experiment in this file closes that: N workers,
one organization, one queue, one suspension, with the number of decisions
made past the suspension counted per worker.

Pointing this at a PostgreSQL: `AIDA_REVIEWER_SUSPENSION_TEST_DATABASE_URL`,
or let it derive a scratch database from `Settings.database_url`. It uses its
own scratch database name, distinct from the AR-05 budget race's, because both
files drop and rebuild the schema they own.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Hashable, Iterator
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
    REASON_UNSUPPORTED_ISOLATION,
    SUPPORTED_ISOLATION_LEVEL,
    ReviewerAgentUnavailable,
    auto_decide_tier0_tier1,
    organization_suspended,
    pre_review_pending,
    set_suspended,
    transaction_isolation_level,
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

#: The bound the guard claims at READ COMMITTED: items decided by any one
#: worker after the suspension's COMMIT returned. Per *worker*, so the fleet
#: bound is this times the number of workers.
MAX_STOP_DELAY_ITEMS = 1


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
    patched `decide_review` is handed that identifies who is calling. The
    label a session is registered under is opaque: the separate-organization
    experiments use the `organization_id`, and the shared-queue experiment
    uses a worker number, because there every worker has the same
    organization and attributing by it would collapse the fleet into one
    series and hide the per-worker bound.
    """

    def __init__(self, fleet_size: int = 1) -> None:
        self.decisions: list[tuple[Hashable, float]] = []
        self.suspended_at: float | None = None
        self.first_decision = asyncio.Event()
        #: Set once every worker has returned. The suspending controller waits
        #: on whichever of these comes first: at an isolation level the agent
        #: refuses, no decision is ever made and waiting only on
        #: `first_decision` would stall the experiment until its timeout.
        self.fleet_idle = asyncio.Event()
        self._fleet_size = fleet_size
        self._finished = 0
        self._label_of_session: dict[int, Hashable] = {}

    def register(self, session: AsyncSession, label: Hashable) -> None:
        self._label_of_session[id(session)] = label

    def record_for(self, session: AsyncSession) -> None:
        label = self._label_of_session[id(session)]
        self.decisions.append((label, time.monotonic()))
        self.first_decision.set()

    def worker_finished(self) -> None:
        self._finished += 1
        if self._finished >= self._fleet_size:
            self.fleet_idle.set()

    def decided(self, label: Hashable) -> int:
        return sum(1 for (own, _) in self.decisions if own == label)

    def stop_delay(self, label: Hashable) -> int:
        """Decisions this worker made after the suspension committed."""
        assert self.suspended_at is not None
        return sum(1 for (own, t) in self.decisions if own == label and t > self.suspended_at)


async def _run_worker(
    engine: AsyncEngine, org_id: UUID, recorder: _Recorder
) -> str:
    """One reviewer-agent batch on its own connection.

    Returns why it ended: `"suspended"` if the guard stopped it,
    `"refused:isolation"` if the batch never started because the isolation
    level cannot support the stop bound, `"exhausted"` if it ran out of work
    first (which at READ COMMITTED would mean the suspension never reached it
    -- a failure of the property, reported as a distinct outcome rather than
    as a passing test).

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
            if exc.reason_code == REASON_UNSUPPORTED_ISOLATION:
                return "refused:isolation"
            assert exc.reason_code == REASON_SUSPENDED
            return "suspended"
        finally:
            recorder.worker_finished()
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


#: Safety valve for `_wait_for_fleet`. Never reached in a healthy run: one of
#: the two events it waits on fires in milliseconds. It exists so a fleet that
#: deadlocks fails the run in seconds instead of hanging the suite.
FLEET_WAIT_SECONDS = 30


async def _wait_for_fleet(recorder: _Recorder) -> None:
    """Block until the fleet has decided something, or has all finished."""
    waiters = [
        asyncio.create_task(recorder.first_decision.wait()),
        asyncio.create_task(recorder.fleet_idle.wait()),
    ]
    try:
        await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED, timeout=FLEET_WAIT_SECONDS
        )
    finally:
        for waiter in waiters:
            waiter.cancel()


async def _suspend_everything(
    engine: AsyncEngine, org_ids: list[UUID], recorder: _Recorder
) -> None:
    """Wait for the fleet to be genuinely working, then commit one suspension.

    On its own connection, which is the whole point: a worker must learn about
    a write it did not make.

    "Genuinely working" is the first decision, or the whole fleet returning --
    whichever comes first. At an isolation level the agent refuses outright no
    decision is ever made, and waiting only on the first would hold the
    experiment open until its timeout for a fleet that has already gone home.
    """
    await _wait_for_fleet(recorder)
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
    recorder = _Recorder(fleet_size=WORKERS)

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
        # REPEATABLE READ: the batch never starts. Before 2026-09-12 every
        # worker here ran to exhaustion, deciding its whole queue with the
        # suspension committed and invisible -- measured at 6 items past the
        # suspension per worker, bounded only by how much work was seeded.
        # The stop bound is now a precondition rather than a hope, so the
        # unbounded path is unreachable instead of merely documented.
        assert worker_outcomes == ["refused:isolation"] * WORKERS, (
            f"{isolation_level} cannot support the stop bound and must be "
            f"refused, but workers reported {worker_outcomes}"
        )
        assert recorder.decisions == [], (
            "a refused batch must decide nothing, but "
            f"{len(recorder.decisions)} decisions were made"
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
    batch ended -- `"committed"`, `"aborted:<SQLSTATE>"`, or one of the
    agent's own refusals -- and how many decisions it committed.

    A `ReviewerAgentUnavailable` is rolled back rather than committed, which
    is what `agent_contract_api.run_reviewer_agent` does with it. That is the
    reason a suspended worker's committed count is zero and not "everything it
    decided before the stop": the batch is one transaction, so a mid-batch
    refusal discards the lot.
    """
    async with _sessions(engine)() as session:
        try:
            outcomes = await auto_decide_tier0_tier1(session, org_id, settings=_settings())
            await session.commit()
        except ReviewerAgentUnavailable as exc:
            await session.rollback()
            return (f"refused:{exc.reason_code}", 0)
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

    if isolation_level == "READ COMMITTED":
        # Exactly once: every item decided, none twice, one audit row each.
        assert approved == CONTENDED_ITEMS
        assert audited == CONTENDED_ITEMS
        assert sum(committed) == CONTENDED_ITEMS
        # A lost race is a skipped item, never a lost batch: once the winner
        # commits, the loser's compare-and-set finds the row no longer PENDING
        # and the decision service refuses it inside the item's savepoint.
        assert outcomes == ["committed"] * CONTENDING_WORKERS, outcomes
    else:
        # REPEATABLE READ is refused before any row is read, so contention at
        # this level is now unreachable through the agent rather than merely
        # survivable. What it used to measure -- the loser's claim meeting a
        # row a concurrent transaction had changed, PostgreSQL refusing it
        # with 40001, and the loser's whole batch rolling back while the
        # winner's stood -- is kept as a property of the database itself by
        # `test_ar04_snapshot_isolation_hides_a_committed_suspension`, which
        # is the mechanism the refusal exists for.
        assert outcomes == [f"refused:{REASON_UNSUPPORTED_ISOLATION}"] * CONTENDING_WORKERS, (
            outcomes
        )
        assert (approved, audited, sum(committed)) == (0, 0, 0)


# --- why REPEATABLE READ is refused rather than tolerated ---------------------


async def test_ar04_snapshot_isolation_hides_a_committed_suspension(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """The mechanism `refuse_unsupported_isolation` exists for, at SQL level.

    The enforcement is only defensible if the thing it refuses is real, and
    the tests that used to demonstrate it can no longer reach the agent. This
    reproduces it underneath the agent instead, with the same re-read Guard 0
    performs: a reader opens a transaction and reads the state row, a second
    connection commits a suspension, and the reader repeats its read.

    At READ COMMITTED the second read sees the suspension, which is exactly
    what bounds the stop at one item. At REPEATABLE READ it does not, for the
    life of the transaction, however many times it is repeated -- so a batch
    running at that level would decide its whole queue with the kill switch
    already thrown. Without this, a later reader could mistake the refusal for
    paranoia and relax it.
    """
    org_id = await _seed_worker_org(engine, 0)

    async with _sessions(engine)() as reader:
        # Takes the reader's snapshot: under REPEATABLE READ the snapshot is
        # fixed by the transaction's *first* statement, so the suspension has
        # to be committed after this read, not before, for the test to mean
        # anything. The already-suspended case is the control test above.
        assert await organization_suspended(reader, org_id) is False

        async with _sessions(engine)() as suspender:
            await set_suspended(
                suspender,
                org_id,
                suspended=True,
                context=security_context(organization_id=org_id, principal_id="risk-officer"),
                reason="mid-transaction",
            )
            await suspender.commit()

        # The re-read Guard 0 makes, in a transaction that began before the
        # suspension committed.
        seen = await organization_suspended(reader, org_id)
        level_in_force = await transaction_isolation_level(reader)

    print(
        f"\n[AR-04 snapshot] {isolation_level}: transaction_isolation reports "
        f"{level_in_force!r}; a re-read inside a transaction older than the "
        f"suspension's COMMIT sees suspended={seen}"
    )
    assert level_in_force == isolation_level

    if isolation_level == SUPPORTED_ISOLATION_LEVEL:
        assert seen is True, (
            "at READ COMMITTED the per-item re-read must see a suspension "
            "committed after the batch began -- that is the whole stop bound"
        )
    else:
        assert seen is False, (
            f"{isolation_level} is refused because its snapshot hides the "
            "suspension; if this now sees it, the refusal can be revisited"
        )


# --- R11-C4: N workers, ONE queue, ONE agent, ONE suspension ------------------

#: Workers drawing on a single organization's queue. Four is enough that the
#: per-worker bound and the fleet bound are different numbers.
SHARED_QUEUE_WORKERS = 4

#: Items in that one shared queue. Deep enough that the fleet is still working
#: when the suspension commits: a worker that merely ran out of items would
#: report a stop delay of zero and prove nothing.
SHARED_QUEUE_ITEMS = 48


async def _run_shared_queue_worker(
    engine: AsyncEngine, org_id: UUID, worker: int, recorder: _Recorder
) -> tuple[str, int]:
    """One committing worker on the shared queue, attributed by worker number.

    Every worker here has the same `organization_id`, so the recorder is keyed
    by worker instead -- attributing by organization would collapse the fleet
    into one series and hide the per-worker bound this test is about.
    """
    async with _sessions(engine)() as session:
        recorder.register(session, worker)
        try:
            outcomes = await auto_decide_tier0_tier1(session, org_id, settings=_settings())
            await session.commit()
        except ReviewerAgentUnavailable as exc:
            await session.rollback()
            if exc.reason_code == REASON_UNSUPPORTED_ISOLATION:
                return ("refused:isolation", 0)
            assert exc.reason_code == REASON_SUSPENDED
            return ("suspended", 0)
        except DBAPIError as exc:
            await session.rollback()
            return (f"aborted:{_sqlstate(exc)}", 0)
        finally:
            recorder.worker_finished()
        return ("committed", len(outcomes))


async def test_ar04_suspension_binds_on_one_shared_queue_within_one_item_per_worker(
    engine: AsyncEngine, isolation_level: str
) -> None:
    """R11-C4: the stop bound under same-queue contention.

    The separate-organization experiment at the top of this file proves that a
    re-read sees another connection's committed write. It cannot prove what
    R11-C4 asked about, because two workers on two organizations never contend
    for a row: nothing there rules out several workers on *one* queue each
    independently concluding it is still fine and racing past the limit.

    So: one organization, one queue, one agent, one suspension, and
    `SHARED_QUEUE_WORKERS` workers that commit. The measurement is the number
    of decisions each worker made strictly after the suspension's COMMIT
    returned.

    The bound is **one item per worker**, and therefore `SHARED_QUEUE_WORKERS`
    for the fleet. It is per worker because workers share no transaction: each
    can be inside `decide_review` when the suspension commits, and none can
    begin a second item after it. It does not grow with queue depth, and it
    does not grow with how many workers lose the claim race for a given row --
    a lost race is a skip, and Guard 0 runs before the skip as well.

    Committed decisions past the suspension are a second, tighter number:
    zero. `auto_decide_tier0_tier1` raises out of the batch and its caller
    rolls back, so a worker stopped mid-batch lands nothing at all.
    """
    org_id = await _seed_worker_org(engine, SHARED_QUEUE_ITEMS, table_per_item=True)
    recorder = _Recorder(fleet_size=SHARED_QUEUE_WORKERS)

    started = time.monotonic()
    async with _recording(recorder):
        results = await asyncio.gather(
            *[
                _run_shared_queue_worker(engine, org_id, worker, recorder)
                for worker in range(SHARED_QUEUE_WORKERS)
            ],
            _suspend_everything(engine, [org_id], recorder),
        )
    elapsed = time.monotonic() - started

    worker_results = list(results[:SHARED_QUEUE_WORKERS])
    outcomes = [outcome for outcome, _count in worker_results]
    committed = [count for _outcome, count in worker_results]
    delays = {worker: recorder.stop_delay(worker) for worker in range(SHARED_QUEUE_WORKERS)}
    approved, audited = await _decided(engine, org_id)
    print(
        f"\n[AR-04 shared queue] {isolation_level}: {SHARED_QUEUE_WORKERS} workers on ONE "
        f"organization's {SHARED_QUEUE_ITEMS}-item queue in {elapsed:.2f}s; "
        f"{len(recorder.decisions)} decisions made "
        f"(per worker {[recorder.decided(w) for w in range(SHARED_QUEUE_WORKERS)]}); "
        f"per-worker stop delay = {sorted(delays.values())} "
        f"(fleet total {sum(delays.values())}, bound {SHARED_QUEUE_WORKERS}); "
        f"outcomes = {outcomes}; committed per worker = {committed}; "
        f"reviews approved = {approved}, reviewer-agent audit rows = {audited}"
    )

    if isolation_level != SUPPORTED_ISOLATION_LEVEL:
        # Refused before a row is read: the shared queue is untouched.
        #
        # What the refusal is worth, measured on 2026-09-12 by disabling
        # `refuse_unsupported_isolation` and running exactly this experiment
        # at REPEATABLE READ: 47 decisions made past the suspension against a
        # bound of 4, and -- because these workers commit -- 48 reviews
        # approved with 48 audit rows. Not a rolled-back near-miss: the agent
        # approved the whole queue after the kill switch was thrown, and the
        # approvals were durable. That is the regression this branch pins.
        assert outcomes == ["refused:isolation"] * SHARED_QUEUE_WORKERS, outcomes
        assert recorder.decisions == []
        assert (approved, audited) == (0, 0)
        return

    # The suspension must actually have bitten. Every worker running out of
    # work instead would give delays of zero and measure nothing.
    assert "suspended" in outcomes, (
        f"no worker was stopped by the suspension ({outcomes}); the queue was "
        f"too shallow or the fleet too slow for the experiment to mean anything"
    )
    assert all(outcome in ("suspended", "committed") for outcome in outcomes), outcomes
    assert recorder.decisions, "the fleet decided nothing; nothing was measured"

    # The bound, per worker and for the fleet.
    worst = max(delays.values())
    assert worst <= MAX_STOP_DELAY_ITEMS, (
        f"a worker decided {worst} items after the suspension committed "
        f"(per worker: {sorted(delays.values())}); the bound is "
        f"{MAX_STOP_DELAY_ITEMS} item per worker"
    )
    fleet_delay = sum(delays.values())
    assert fleet_delay <= SHARED_QUEUE_WORKERS * MAX_STOP_DELAY_ITEMS, (
        f"the fleet decided {fleet_delay} items after the suspension committed; "
        f"the bound for {SHARED_QUEUE_WORKERS} workers is "
        f"{SHARED_QUEUE_WORKERS * MAX_STOP_DELAY_ITEMS}"
    )

    # Nothing was decided twice, and only committed batches landed: a worker
    # stopped mid-batch rolls back everything it had decided, so no decision
    # made after the suspension survives at all.
    assert approved == sum(committed), (approved, committed)
    assert audited == approved, (audited, approved)
    assert approved <= SHARED_QUEUE_ITEMS
