"""R11-FP06 -- the bulk decision's validation cost no longer depends on how many joins you decide.

The tracker row's remainder named "batching the bulk path's per-item reads".
The bulk decision already loaded its candidates in one read, and then called a
validation that issued six reads plus one per table *for each candidate*, so a
reviewer approving a page of joins paid a round trip per candidate per catalog
table for questions about a set of tables the batch mostly shared.

Two claims, and they are deliberately the strong ones.

**The verdicts are unchanged.** Not "similar" -- every field of the recorded
evidence, including the fingerprint an approval is compared against for drift,
is identical between the per-item path and the batched one, across the shapes
that exercise every dict the batched read fills: a declared key, a unique
index, an approved composite key, profile statistics, a bare name match, and a
retired column. A performance change that moved one verdict would silently
re-decide which joins are approvable, so this is checked before anything about
speed.

**The read count is constant, not merely smaller.** Any cache produces "fewer
reads"; only a batched read produces "the same number for four candidates as
for one". The statements are counted at the DBAPI cursor, where nothing above
it can hide a round trip, and filtered to the six catalog tables validation
reads -- the per-item authorization this change deliberately leaves alone reads
none of them, so the count isolates validation rather than the endpoint.

One read moved the other way, and it is recorded here rather than left to be
found: the per-item path used to check for a retired column *before* it read
constraints, indexes, keys and profiles, so a candidate naming a retired column
cost one read. The batched loader reads the batch and then refuses per
candidate, so that candidate now costs the batch's reads like any other. It is
a rare shape, it is a read fewer only in the case that refuses, and the single
candidate's ordinary path still went from seven reads to six (the per-table
profile loop is gone), so the trade is taken deliberately.

What this does NOT cover, because R11-FP06's remainder has a second half that
is not this one: the inclusion check (does every referencing value exist on the
key side?) is a query against the source, still gated on R11-FP04's
sample-access policy, and is recorded as NOT_RUN with its reason. Nothing here
changes that. The drift pass (`aida.relationship_drift`) also validates one row
at a time, and is left alone on purpose: each of its rows is checked inside its
own `begin_nested()` savepoint, so hoisting a read in front of them would
change what each check sees, which is a semantics change and not this one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.intelligence_api import bulk_decide_relationship_candidates
from aida.models import (
    ColumnProfile,
    CompositeKeyCandidate,
    DataSource,
    MetadataColumn,
    MetadataIndex,
    Organization,
    RelationshipCandidate,
    TableProfile,
)
from aida.relationship_validation import (
    RelationshipColumnsMissingError,
    load_relationship_catalog_facts,
    validate_relationship_candidate,
    validate_relationship_candidate_from,
)
from aida.schemas import RelationshipCandidateBulkDecisionRequest
from tests.test_relationship_intelligence_review import (
    _context,
    _datasource,
    _domain,
    _lob,
    _org,
    _project,
    _seed_candidate,
)

#: The tables `load_catalog_facts` reads. The per-item authorization the bulk
#: decision still performs per candidate (`load_datasource_in_scope`,
#: `gate_read`, `check_cross_boundary_grant`) touches `datasource`, `workspace`
#: and `cross_boundary_grant` and none of these, so counting only these
#: isolates the validation reads from the rest of the endpoint.
VALIDATION_TABLES = (
    "metadata_column",
    "metadata_constraint",
    "metadata_index",
    "composite_key_candidate",
    "table_profile",
    "column_profile",
)


class _Counter:
    """Counts statements at the DBAPI cursor, where nothing can be elided."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __len__(self) -> int:
        return len(self.statements)

    def reset(self) -> None:
        self.statements.clear()

    def validation_reads(self) -> list[str]:
        """The SELECTs against the six tables validation reads."""
        return [
            statement
            for statement in self.statements
            if statement.lstrip().upper().startswith("SELECT")
            and any(
                f'"{table}"' in statement or f" {table} " in statement
                for table in VALIDATION_TABLES
            )
        ]


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    created = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with created.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest_asyncio.fixture
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


async def _estate(session: AsyncSession) -> tuple[Organization, DataSource]:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name=f"ds-{uuid4().hex[:6]}")
    return org, datasource


async def _profiled(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    candidate: RelationshipCandidate,
) -> None:
    """A completed profile on the target side, with statistics for both columns.

    Fills `profiles` and `column_stats` -- the two dicts whose batched filters
    are wider than the per-item ones, and therefore the two whose equivalence
    is worth pinning rather than reasoning about.
    """
    profile = TableProfile(
        organization_id=org.id,
        analysis_run_id=uuid4(),
        datasource_id=datasource.id,
        table_id=candidate.target_table_id,
        sampled_row_count=1_000,
        row_count_estimate=1_000,
        observation_scope="FULL",
        status="COMPLETED",
    )
    session.add(profile)
    await session.flush()
    session.add(
        ColumnProfile(
            organization_id=org.id,
            table_profile_id=profile.id,
            column_id=candidate.target_column_id,
            null_count=0,
            non_null_count=1_000,
            approximate_distinct_count=1_000,
            effectively_unique=True,
        )
    )
    await session.flush()


async def _superseded_profile(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    candidate: RelationshipCandidate,
) -> None:
    """An older COMPLETED profile for the same table, so "the latest" has to choose.

    The per-item read chose with `ORDER BY created_at DESC LIMIT 1` per table;
    the batched read chooses with a window function over the whole batch. Two
    profiles for one table is the only shape where those two can disagree.
    """
    session.add(
        TableProfile(
            organization_id=org.id,
            analysis_run_id=uuid4(),
            datasource_id=datasource.id,
            table_id=candidate.target_table_id,
            sampled_row_count=10,
            row_count_estimate=1_000_000,
            observation_scope="SAMPLE",
            status="COMPLETED",
        )
    )
    await session.flush()
    # `created_at` defaults to now for both rows, so the older one is dated
    # explicitly rather than left to the order they happened to be inserted in.
    oldest = (
        await session.scalars(
            select(TableProfile.id)
            .where(TableProfile.table_id == candidate.target_table_id)
            .order_by(TableProfile.sampled_row_count)
            .limit(1)
        )
    ).one()
    await session.execute(
        update(TableProfile)
        .where(TableProfile.id == oldest)
        .values(created_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    await session.flush()


async def _indexed(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    candidate: RelationshipCandidate,
) -> None:
    """A unique index on the target side -- fills `unique_indexes`."""
    name = (
        await session.scalars(
            select(MetadataColumn.name).where(MetadataColumn.id == candidate.target_column_id)
        )
    ).one()
    session.add(
        MetadataIndex(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=candidate.target_table_id,
            name=f"ux_{uuid4().hex[:6]}",
            columns=[name],
            is_unique=True,
            fingerprint="f" * 8,
        )
    )
    await session.flush()


async def _approved_key(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    candidate: RelationshipCandidate,
) -> None:
    """An APPROVED composite-key candidate on the target side -- fills `approved_keys`."""
    session.add(
        CompositeKeyCandidate(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=candidate.target_table_id,
            column_ids=[str(candidate.target_column_id)],
            column_names=["customer_id"],
            column_count=1,
            key_fingerprint=uuid4().hex,
            detection_rule="PROFILED_DISTINCTNESS_V1",
            confidence=0.95,
            estimated_distinctness_ratio=1.0,
            status="APPROVED",
            created_by="profiler",
        )
    )
    await session.flush()


async def _retire_source_column(session: AsyncSession, candidate: RelationshipCandidate) -> None:
    await session.execute(
        update(MetadataColumn)
        .where(MetadataColumn.id == candidate.source_column_id)
        .values(status="DEPRECATED")
    )
    await session.flush()


async def _mixed_batch(
    session: AsyncSession, org: Organization, datasource: DataSource
) -> list[RelationshipCandidate]:
    """One candidate per shape that reaches a different part of the batched read."""
    declared = await _seed_candidate(session, org, datasource)

    profiled = await _seed_candidate(session, org, datasource)
    await _profiled(session, org, datasource, profiled)

    superseded = await _seed_candidate(session, org, datasource)
    await _profiled(session, org, datasource, superseded)
    await _superseded_profile(session, org, datasource, superseded)

    indexed = await _seed_candidate(session, org, datasource)
    await _indexed(session, org, datasource, indexed)

    keyed = await _seed_candidate(session, org, datasource)
    await _approved_key(session, org, datasource, keyed)

    retired = await _seed_candidate(session, org, datasource)
    await _retire_source_column(session, retired)

    return [declared, profiled, superseded, indexed, keyed, retired]


# ---------------------------------------------------------------------------
# Claim 1: identical verdicts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_validation_returns_the_same_verdict_as_the_per_item_path() -> None:
    """Every field of the evidence, for every shape, byte-for-byte.

    Including `fingerprint`: an approval records it and the drift pass compares
    today's against it, so a batching change that perturbed a fingerprint would
    make every already-approved join read as drifted.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        org, datasource = await _estate(session)
        candidates = await _mixed_batch(session, org, datasource)

        per_item: dict[str, Any] = {}
        for candidate in candidates:
            try:
                per_item[str(candidate.id)] = (
                    await validate_relationship_candidate(session, candidate)
                ).as_evidence()
            except RelationshipColumnsMissingError as exc:
                per_item[str(candidate.id)] = f"missing:{exc}"

        facts = await load_relationship_catalog_facts(session, candidates)
        batched: dict[str, Any] = {}
        for candidate in candidates:
            try:
                batched[str(candidate.id)] = validate_relationship_candidate_from(
                    facts, candidate
                ).as_evidence()
            except RelationshipColumnsMissingError as exc:
                batched[str(candidate.id)] = f"missing:{exc}"

        assert batched == per_item
        # The batch really did contain both outcomes, so an all-refusing or
        # all-approving batch cannot pass this by agreeing trivially.
        assert any(
            isinstance(value, dict) and value["outcome"] == "CORROBORATED"
            for value in batched.values()
        )
        assert any(
            isinstance(value, str) and value.startswith("missing:") for value in batched.values()
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_one_candidates_retired_column_does_not_affect_another_verdict() -> None:
    """The missing-column refusal stays per candidate.

    The column read is now shared across the batch, so this is the property
    most at risk from sharing it: a retired column in candidate A must refuse A
    and leave B's verdict exactly as it would have been alone.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        org, datasource = await _estate(session)
        healthy = await _seed_candidate(session, org, datasource)
        retired = await _seed_candidate(session, org, datasource)
        await _retire_source_column(session, retired)

        alone = (await validate_relationship_candidate(session, healthy)).as_evidence()

        facts = await load_relationship_catalog_facts(session, [healthy, retired])
        assert validate_relationship_candidate_from(facts, healthy).as_evidence() == alone
        with pytest.raises(RelationshipColumnsMissingError):
            validate_relationship_candidate_from(facts, retired)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Claim 2: the read count does not depend on the batch size.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_load_reads_the_same_number_of_times_for_six_candidates_as_for_one(
    session: AsyncSession, counted: _Counter
) -> None:
    """The claim a cache cannot make.

    Six candidates over twelve distinct tables -- `_seed_candidate` shares no
    table between any two candidates, which is the worst case for batching, and
    it still holds. The per-item path is counted in the same test from the same
    data, so the saving is measured rather than asserted.

    `one` is measured on a candidate that *has* a profile so that it is the same
    shape as the batch: the column-statistics read is conditional on some table
    in the scope having a completed profile, which is asserted separately below
    rather than left to make the comparison look worse or better than it is.
    """
    org, datasource = await _estate(session)
    candidates = await _mixed_batch(session, org, datasource)
    declared, profiled = candidates[0], candidates[1]

    counted.reset()
    await load_relationship_catalog_facts(session, [profiled])
    one = len(counted.validation_reads())

    counted.reset()
    await load_relationship_catalog_facts(session, [declared])
    unprofiled = len(counted.validation_reads())

    counted.reset()
    facts = await load_relationship_catalog_facts(session, candidates)
    many = len(counted.validation_reads())

    counted.reset()
    for candidate in candidates:
        try:
            validate_relationship_candidate_from(facts, candidate)
        except RelationshipColumnsMissingError:
            pass
    assessing = len(counted.statements)

    counted.reset()
    for candidate in candidates:
        try:
            await validate_relationship_candidate(session, candidate)
        except RelationshipColumnsMissingError:
            pass
    per_item = len(counted.validation_reads())

    assert many == one, "the batched read's cost must not grow with the batch"
    assert many == 6, f"expected six batched reads, one per catalog table; got {many}"
    assert unprofiled == 5, (
        "a scope in which no table has a completed profile should skip the "
        f"column-statistics read; got {unprofiled}"
    )
    assert assessing == 0, "assessing a loaded batch must issue no statement at all"
    # The saving, measured on this shape: 32 reads one candidate at a time --
    # six each for the two whose target table carries a profile and five each
    # for the other four -- against 6 for the batch. Pinned as a number rather
    # than an inequality so that a change which quietly reintroduces a
    # per-candidate read fails here with the arithmetic visible.
    assert per_item == 32, f"per-item reads changed shape: {per_item}"
    assert per_item > many * 5


@pytest.mark.asyncio
async def test_bulk_approval_validation_reads_do_not_grow_with_the_batch(
    session: AsyncSession, counted: _Counter
) -> None:
    """Measured through the real endpoint, not the helper.

    `bulk_decide_relationship_candidates` still authorizes every candidate
    individually -- that is deliberate and unchanged -- so this counts only the
    six catalog tables validation reads. Those are now flat: approving four
    joins reads them exactly as many times as approving one.
    """
    org, datasource = await _estate(session)
    settings = Settings(environment="development")
    context = _context(org, principal="reviewer")

    single = [await _seed_candidate(session, org, datasource)]
    batch = [await _seed_candidate(session, org, datasource) for _ in range(4)]
    await session.commit()

    counted.reset()
    one_result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[c.id for c in single], decision="APPROVE", reason="evidence checked"
        ),
        context=context,
        session=session,
        settings=settings,
    )
    one = len(counted.validation_reads())

    counted.reset()
    four_result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[c.id for c in batch], decision="APPROVE", reason="evidence checked"
        ),
        context=context,
        session=session,
        settings=settings,
    )
    four = len(counted.validation_reads())

    assert one_result.succeeded_count == 1
    assert four_result.succeeded_count == 4, [item.reason for item in four_result.results]
    assert four == one, (
        f"validation reads grew with the batch: {one} for one candidate, {four} for four"
    )


@pytest.mark.asyncio
async def test_bulk_rejection_reads_no_validation_at_all(
    session: AsyncSession, counted: _Counter
) -> None:
    """A rejection decides no evidence, so it must not pay for a batched load.

    Worth pinning because the batched load is hoisted above the loop: hoisting
    a read out of a branch is how an unconditional cost appears where there was
    none.
    """
    org, datasource = await _estate(session)
    settings = Settings(environment="development")
    candidates = [await _seed_candidate(session, org, datasource) for _ in range(3)]
    await session.commit()

    counted.reset()
    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[c.id for c in candidates], decision="REJECT", reason="not supported"
        ),
        context=_context(org, principal="reviewer"),
        session=session,
        settings=settings,
    )

    assert result.succeeded_count == 3, [item.reason for item in result.results]
    assert counted.validation_reads() == []
