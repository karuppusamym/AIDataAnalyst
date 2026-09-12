"""R11-D11: `business_node_rollup` has a writer, and the writer is actually called.

The defect this closes is narrow and was invisible from either end on its own.
`aida.business_graph.rebuild_rollup` and `rebuild_closure` existed, were correct and
were measured (ADR-0020: a subtree roll-up costs 3,147 ms computed on read against
0.4 ms read from the materialisation, at 13,548 nodes and 5,000,000 assignments) --
and nothing ever called either of them. So `business_node_rollup` stayed empty
forever, `rollup()` took its authoritative fallback on every call, and
`GET /v1/business-nodes/{node_id}/rollup` returned `computed_at: null` while
answering from the ~3 s query the projection was built to avoid.

Two halves are therefore tested, because either half alone would pass against the
bug: that a rebuild really populates what `rollup()` reads (behavioural), and that
the scheduler really calls the rebuild (structural -- an AST check, since "the
function exists and works but has no caller" is precisely the state being fixed).
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.business_graph as business_graph
import aida.db
import aida.models  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.business_graph import (
    assign,
    organizations_by_rollup_staleness,
    rebuild_projections,
    rollup,
    rollup_freshness,
    run_rollup_rebuild_pass,
)
from aida.db import Base
from aida.models import BusinessNode, BusinessNodeRollup, Organization
from atlas.platform.config import Settings

#: Deliberately in the past. `rollup()` without an explicit `as_of` resolves against
#: `datetime.now(UTC)`, so an assignment seeded with a future `effective_from` is not
#: yet live and every count would be zero for reasons that have nothing to do with
#: the projection under test.
_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
_SCHEDULER = Path(business_graph.__file__).with_name("workflows") / "scheduler.py"


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with maker() as active:
        yield active


async def _seed_tree(session: AsyncSession) -> tuple[UUID, UUID, UUID]:
    """One organization, a parent LOB with a child domain, one table assigned to each.

    Returns `(organization_id, lob_id, domain_id)`. The roll-up at the LOB must count
    both tables; the roll-up at the domain must count only its own.
    """
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = BusinessNode(organization_id=org.id, kind="LOB", name="Retail", code="LOB:RTL")
    session.add(lob)
    await session.flush()
    domain = BusinessNode(
        organization_id=org.id,
        kind="DOMAIN",
        name="Deposits",
        code="DOM:DEP",
        parent_id=lob.id,
    )
    session.add(domain)
    await session.flush()
    for node_id in (lob.id, domain.id):
        await assign(
            session,
            organization_id=org.id,
            business_node_id=node_id,
            target_type="TABLE",
            target_id=uuid4().hex,
            assigned_by="seed",
            as_of=_NOW,
        )
    await session.flush()
    return org.id, lob.id, domain.id


# --- the projection now has a writer, and it is the one `rollup()` reads ----


async def test_rebuild_projections_populates_what_rollup_reads(session: AsyncSession) -> None:
    """Before the rebuild the roll-up is unmaterialised; after it, it is on disk.

    `rollup_freshness` is the discriminator that makes this test mean something: it
    reads `computed_at`, which only ever exists on a materialised row. `None` before
    and a real timestamp after is proof the projection was written, not merely that
    two computations of the same number agreed.
    """
    organization_id, lob_id, _ = await _seed_tree(session)

    assert await rollup_freshness(session, organization_id, lob_id) is None
    assert await rollup(session, organization_id, lob_id) == {"TABLE": 2}

    closure_rows, rollup_rows = await rebuild_projections(session, organization_id, now=_NOW)

    assert closure_rows > 0, "closure projection stayed empty"
    assert rollup_rows > 0, "roll-up projection stayed empty"
    assert await rollup_freshness(session, organization_id, lob_id) is not None
    assert await rollup(session, organization_id, lob_id) == {"TABLE": 2}


async def test_rollup_reads_the_materialisation_rather_than_recomputing(
    session: AsyncSession,
) -> None:
    """Prove the read path actually consults the table.

    Both paths return the same number when the projection is honest, so agreement
    proves nothing. Deliberately corrupting one stored count to a sentinel and seeing
    the sentinel come back is the only way to show `rollup()` read the projection
    instead of quietly recomputing it -- which is exactly what it did for as long as
    the table had no writer.
    """
    organization_id, lob_id, _ = await _seed_tree(session)
    await rebuild_projections(session, organization_id, now=_NOW)

    await session.execute(
        update(BusinessNodeRollup)
        .where(
            BusinessNodeRollup.organization_id == organization_id,
            BusinessNodeRollup.business_node_id == lob_id,
        )
        .values(distinct_targets=4_242)
    )
    await session.flush()

    assert await rollup(session, organization_id, lob_id) == {"TABLE": 4_242}


async def test_the_closure_is_rebuilt_before_the_rollup_that_aggregates_through_it(
    session: AsyncSession,
) -> None:
    """Re-parenting is the case that pairs the two rebuilds.

    `_REBUILD_ROLLUP` aggregates *through* `business_node_closure`, and
    `extend_closure_for_new_node` only maintains the closure for newly created nodes --
    it does not handle a node moving. So a roll-up rebuilt against a stale closure
    would reproduce the old tree's answer and stamp a fresh `computed_at` on it. This
    detaches the domain from the LOB and asserts the LOB's count drops, which can only
    happen if the closure was rebuilt first.
    """
    organization_id, lob_id, domain_id = await _seed_tree(session)
    await rebuild_projections(session, organization_id, now=_NOW)
    assert await rollup(session, organization_id, lob_id) == {"TABLE": 2}

    await session.execute(
        update(BusinessNode).where(BusinessNode.id == domain_id).values(parent_id=None)
    )
    await session.flush()

    await rebuild_projections(session, organization_id, now=_NOW + timedelta(hours=1))

    assert await rollup(session, organization_id, lob_id) == {"TABLE": 1}
    assert await rollup(session, organization_id, domain_id) == {"TABLE": 1}


# --- ordering: a bounded sweep must not starve the organizations it skips ---


async def test_never_built_organizations_are_rebuilt_before_merely_stale_ones(
    session: AsyncSession,
) -> None:
    """The batch bound is only safe if the order rotates.

    A plain `LIMIT` over an unordered organization list rebuilds the same first N
    organizations forever, which on a multi-tenant estate is indistinguishable from
    having no writer at all for everyone after N. Never-built first, then oldest.
    """
    built_recently, _, _ = await _seed_tree(session)
    built_long_ago, _, _ = await _seed_tree(session)
    never_built, _, _ = await _seed_tree(session)

    await rebuild_projections(session, built_recently, now=_NOW)
    await rebuild_projections(session, built_long_ago, now=_NOW - timedelta(days=30))
    await session.flush()

    ordered = await organizations_by_rollup_staleness(session, limit=10)

    assert ordered.index(never_built) < ordered.index(built_long_ago)
    assert ordered.index(built_long_ago) < ordered.index(built_recently)


async def test_the_staleness_sweep_is_bounded_by_the_batch_size(session: AsyncSession) -> None:
    for _ in range(3):
        await _seed_tree(session)
    assert len(await organizations_by_rollup_staleness(session, limit=2)) == 2


async def test_organizations_without_a_classification_tree_are_not_swept(
    session: AsyncSession,
) -> None:
    session.add(Organization(name="Empty", slug=f"empty-{uuid4().hex[:8]}"))
    await session.flush()
    assert await organizations_by_rollup_staleness(session, limit=10) == ()


# --- the scheduled pass: kill switch, cadence, and a real sweep -------------


def _settings(**kwargs: object) -> Settings:
    """`_env_file=None` keeps a developer's local .env out of the harness, matching
    `tests/test_reaper_service.py`."""
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_cadence() -> AsyncIterator[None]:
    business_graph._rollup_rebuild_last_run_at = None
    yield
    business_graph._rollup_rebuild_last_run_at = None


async def test_the_kill_switch_skips_the_pass_without_touching_the_database() -> None:
    settings = _settings(business_rollup_rebuild_enabled=False)
    assert await run_rollup_rebuild_pass(settings, now=_NOW) is None


async def test_the_pass_is_a_no_op_between_cadence_windows() -> None:
    business_graph._rollup_rebuild_last_run_at = _NOW
    settings = _settings(business_rollup_rebuild_interval_seconds=86_400)
    assert await run_rollup_rebuild_pass(settings, now=_NOW + timedelta(hours=1)) is None


async def test_the_pass_rebuilds_every_due_organization(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: the scheduler-facing entry point writes the projection.

    `aida.db.session_factory` is resolved lazily through a module `__getattr__`, so
    setting the attribute directly is what the pass's own local import picks up.
    """
    async with maker() as seeding:
        organization_id, lob_id, _ = await _seed_tree(seeding)
        await seeding.commit()

    monkeypatch.setattr(aida.db, "session_factory", maker, raising=False)

    rebuilt = await run_rollup_rebuild_pass(_settings(), now=_NOW)

    assert rebuilt == 1
    async with maker() as reading:
        assert await rollup_freshness(reading, organization_id, lob_id) is not None
        assert await rollup(reading, organization_id, lob_id) == {"TABLE": 2}


async def test_one_organizations_failure_does_not_abort_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A failed rebuild must cost that organization its freshness, nothing more --
    its roll-up stays as stale as it was and `rollup()` keeps answering correctly by
    the slow path."""
    async with maker() as seeding:
        first, first_lob, _ = await _seed_tree(seeding)
        second, second_lob, _ = await _seed_tree(seeding)
        await seeding.commit()

    monkeypatch.setattr(aida.db, "session_factory", maker, raising=False)

    failed: list[UUID] = []
    real_rebuild = business_graph.rebuild_projections

    async def _explode_once(
        session: AsyncSession, organization_id: UUID, *, now: datetime | None = None
    ) -> tuple[int, int]:
        if not failed:
            failed.append(organization_id)
            raise RuntimeError("projection rebuild failed")
        return await real_rebuild(session, organization_id, now=now)

    monkeypatch.setattr(business_graph, "rebuild_projections", _explode_once)

    rebuilt = await run_rollup_rebuild_pass(_settings(), now=_NOW)

    assert rebuilt == 1, "the sweep aborted instead of skipping the failed organization"
    survivor = second if failed[0] == first else first
    survivor_lob = second_lob if failed[0] == first else first_lob
    async with maker() as reading:
        assert await rollup_freshness(reading, survivor, survivor_lob) is not None
        # The failed organization still answers correctly, just not from the cache.
        assert await rollup(reading, failed[0], first_lob if failed[0] == first else second_lob)


# --- the wiring itself ------------------------------------------------------


def test_the_scheduler_iteration_actually_calls_the_rebuild_pass() -> None:
    """The regression R11-D11 is about is a correct function with no caller.

    A behavioural test of `run_scheduler_iteration` needs a live Temporal client, so
    this reads the call out of the source instead. It fails the moment the call is
    dropped from the iteration -- which is the exact state this row found the
    projection rebuild in.
    """
    tree = ast.parse(_SCHEDULER.read_text(encoding="utf-8"), filename=str(_SCHEDULER))
    iteration = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_scheduler_iteration"
    )
    called = {
        node.func.id
        for node in ast.walk(iteration)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_rollup_rebuild_pass" in called, (
        "run_scheduler_iteration no longer calls run_rollup_rebuild_pass -- "
        "business_node_rollup is back to having no writer"
    )
