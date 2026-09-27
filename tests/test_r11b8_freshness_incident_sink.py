"""R11-B8: scheduled freshness evaluation files into the quality incident sink.

DQ-2 could evaluate a watermark contract on request and nothing ever asked:
`evaluate_freshness` was reached only from `quality_api.get_freshness_status`
and the ABAC attribute resolver, both of which answer one caller and persist
nothing. So a table could be hours late and the platform would say so only to
whoever happened to open that one screen.

These tests run against a real (in-memory sqlite) database and prove the
lifecycle the tracker row asks for, through the scheduler's own entry point:

1. a scheduled sweep over an approved contract whose watermark is past its
   threshold OPENS a `DataQualityIncident`;
2. a second sweep while it is still stale UPDATES that same row -- one row per
   table, occurrence counted, severity re-derived -- and never opens a second;
3. recovery RESOLVES it. This is the half a naive "alert on violation" pass
   gets wrong, and the half the acceptance condition names explicitly;
4. an unapproved (PENDING_APPROVAL) contract opens nothing, ever -- which is
   what keeps maker-checker meaningful rather than decorative;
5. a contract edited back to pending resolves the incident it left behind,
   rather than stranding it open with nothing left able to close it;
6. the sweep is off by default, proven by refusing to open a session at all;
7. the bound truncates and says so; and
8. `quality_summary.source_freshness_status`, which was the hardcoded string
   "NOT_CONFIGURED", now reports the datasource's real rolled-up state.

(8) is the one seam this row cannot close on its own: the DTO field is typed
`Literal["NOT_CONFIGURED"]` in `schemas.py`, which another session owns this
round, so the endpoint-level assertion is xfailed and the roll-up it depends
on is asserted directly instead. The Literal must carry FRESH / STALE /
AWAITING_APPROVAL / NOT_CONFIGURED.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.freshness import (
    FRESHNESS_ANOMALY_TYPE,
    FRESHNESS_SCHEDULER_PRINCIPAL,
    evaluate_freshness_for_datasource,
    load_freshness_states,
    worst_freshness_state,
)
from aida.models import (
    DataDomain,
    DataQualityIncident,
    DataSource,
    FreshnessObservation,
    FreshnessWatermarkConfig,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.quality_api import (
    approve_freshness_config,
    quality_summary,
    upsert_freshness_config,
)
from aida.schemas import FreshnessConfigUpsert
from tests.support.doubles import security_context

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        yield session


class _Scenario:
    """One organization, one datasource, two tables -- the smallest estate in
    which "this table is late and that one is not" is a real distinction."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Scenario":
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug=f"core-banking-{uuid4().hex[:8]}",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="transactions",
            object_type="BASE_TABLE",
            fingerprint="fp-transactions",
        )
        self.other_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="accounts",
            object_type="BASE_TABLE",
            fingerprint="fp-accounts",
        )
        db.add_all([self.table, self.other_table])
        await db.flush()
        await db.commit()
        return self

    def maker_context(self):
        return security_context(
            organization_id=self.organization.id,
            principal_id="steward-maker",
            roles=frozenset({"DataSteward"}),
        )

    def checker_context(self):
        return security_context(
            organization_id=self.organization.id,
            principal_id="steward-checker",
            roles=frozenset({"DataSteward"}),
        )

    def worker_context(self):
        return security_context(
            organization_id=self.organization.id,
            principal_id=FRESHNESS_SCHEDULER_PRINCIPAL,
            roles=frozenset({"SchedulerWorker"}),
        )

    async def approved_contract(self, table_id, *, threshold_minutes: int = 60) -> None:
        """The whole maker-checker dance, since an unapproved contract is
        inert by design and would make every assertion below vacuous."""
        await upsert_freshness_config(
            self.datasource.id,
            table_id,
            FreshnessConfigUpsert(
                watermark_column="updated_at", threshold_minutes=threshold_minutes
            ),
            context=self.maker_context(),
            session=self.db,
        )
        await approve_freshness_config(
            self.datasource.id,
            table_id,
            context=self.checker_context(),
            session=self.db,
        )
        await self.db.commit()

    async def observe(self, table_id, watermark: datetime) -> None:
        self.db.add(
            FreshnessObservation(
                organization_id=self.organization.id,
                datasource_id=self.datasource.id,
                table_id=table_id,
                watermark_value=watermark,
                observed_at=watermark,
            )
        )
        await self.db.commit()

    async def sweep(self, *, now: datetime, max_tables: int = 500):
        result = await evaluate_freshness_for_datasource(
            self.db,
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            context=self.worker_context(),
            now=now,
            max_tables=max_tables,
        )
        await self.db.commit()
        return result


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def _incidents(db: AsyncSession) -> list[DataQualityIncident]:
    return list(
        (
            await db.scalars(
                select(DataQualityIncident).where(
                    DataQualityIncident.anomaly_type == FRESHNESS_ANOMALY_TYPE
                )
            )
        ).all()
    )


async def test_violation_opens_then_updates_and_recovery_resolves_the_same_incident(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """The tracker row's acceptance condition, end to end on one incident row."""
    await scenario.approved_contract(scenario.table.id, threshold_minutes=60)
    await scenario.observe(scenario.table.id, _NOW - timedelta(minutes=90))

    first = await scenario.sweep(now=_NOW)
    assert (first.incidents_opened, first.incidents_updated, first.incidents_resolved) == (1, 0, 0)

    opened = await _incidents(db)
    assert len(opened) == 1
    incident = opened[0]
    assert incident.status == "OPEN"
    assert incident.table_id == scenario.table.id
    assert incident.anomaly_type == FRESHNESS_ANOMALY_TYPE
    assert incident.severity == "WARNING"  # 90 minutes is under twice the threshold
    assert incident.occurrence_count == 1
    assert incident.evidence["age_minutes"] == pytest.approx(90.0)
    assert incident.evidence["threshold_minutes"] == 60
    fingerprint = incident.fingerprint

    # Still stale, and further past the threshold: the same row is updated in
    # place and its severity re-derived. A second row here would mean a table
    # accumulating one incident per sweep.
    second = await scenario.sweep(now=_NOW + timedelta(minutes=60))
    assert (second.incidents_opened, second.incidents_updated, second.incidents_resolved) == (
        0,
        1,
        0,
    )
    still_open = await _incidents(db)
    assert len(still_open) == 1
    assert still_open[0].id == incident.id
    assert still_open[0].fingerprint == fingerprint
    assert still_open[0].occurrence_count == 2
    assert still_open[0].severity == "CRITICAL"  # 150 minutes is past 2x60
    assert still_open[0].status == "OPEN"

    # Recovery: a newer watermark lands inside the threshold.
    recovery_at = _NOW + timedelta(minutes=120)
    await scenario.observe(scenario.table.id, recovery_at - timedelta(minutes=5))

    third = await scenario.sweep(now=recovery_at)
    assert (third.incidents_opened, third.incidents_updated, third.incidents_resolved) == (0, 0, 1)

    resolved = await _incidents(db)
    assert len(resolved) == 1
    assert resolved[0].id == incident.id
    assert resolved[0].status == "RESOLVED"
    assert resolved[0].resolved_by == FRESHNESS_SCHEDULER_PRINCIPAL
    assert resolved[0].resolved_at == recovery_at
    assert "within the configured freshness threshold" in (resolved[0].resolution_reason or "")

    # And a relapse reopens the very same row rather than colliding with the
    # sink's UNIQUE(fingerprint).
    relapse_at = _NOW + timedelta(minutes=300)
    fourth = await scenario.sweep(now=relapse_at)
    assert fourth.incidents_opened == 1
    relapsed = await _incidents(db)
    assert len(relapsed) == 1
    assert relapsed[0].id == incident.id
    assert relapsed[0].status == "OPEN"
    assert relapsed[0].resolved_at is None


async def test_an_approved_contract_with_no_watermark_at_all_opens_a_warning(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """Nothing in the platform writes `FreshnessObservation` rows yet, so this
    is the state a real estate lands in first. It is a violation -- the
    contract is approved and nothing is proving it -- but a WARNING, not a
    CRITICAL: there is no age to judge, and overstating it would fail governed
    tools closed on a table that may be perfectly current."""
    await scenario.approved_contract(scenario.table.id)

    sweep = await scenario.sweep(now=_NOW)
    assert sweep.incidents_opened == 1

    incidents = await _incidents(db)
    assert incidents[0].severity == "WARNING"
    assert incidents[0].evidence["age_minutes"] is None
    assert "no watermark has ever been observed" in incidents[0].summary


async def test_an_unapproved_contract_never_opens_an_incident(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """PENDING_APPROVAL is not a control. If a sweep could open incidents off
    an unapproved contract, maker-checker would gate nothing that matters."""
    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=60),
        context=scenario.maker_context(),
        session=db,
    )
    await db.commit()
    await scenario.observe(scenario.table.id, _NOW - timedelta(days=30))

    sweep = await scenario.sweep(now=_NOW)

    assert sweep.contracts_evaluated == 1
    assert sweep.incidents_opened == 0
    assert await _incidents(db) == []


async def test_editing_an_approved_contract_resolves_the_incident_it_left_behind(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """`upsert_freshness_config` resets approval on every edit. A sweep that
    only read ACTIVE contracts would never see this table again, stranding its
    incident open with nothing left able to close it."""
    await scenario.approved_contract(scenario.table.id)
    await scenario.observe(scenario.table.id, _NOW - timedelta(minutes=600))
    await scenario.sweep(now=_NOW)
    assert (await _incidents(db))[0].status == "OPEN"

    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="ingested_at", threshold_minutes=30),
        context=scenario.maker_context(),
        session=db,
    )
    await db.commit()

    sweep = await scenario.sweep(now=_NOW + timedelta(minutes=10))

    assert sweep.incidents_resolved == 1
    incidents = await _incidents(db)
    assert incidents[0].status == "RESOLVED"
    assert "no longer active" in (incidents[0].resolution_reason or "")


async def test_the_sweep_is_bounded_and_reports_truncation(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await scenario.approved_contract(scenario.table.id)
    await scenario.approved_contract(scenario.other_table.id)

    sweep = await scenario.sweep(now=_NOW, max_tables=1)

    assert sweep.truncated is True
    assert sweep.contracts_evaluated == 1
    assert len(await _incidents(db)) == 1


def test_a_datasource_is_only_as_fresh_as_its_worst_table() -> None:
    """The roll-up itself, which needs no database and no DTO.

    Worst-first, and an estate with no contracts at all is NOT_CONFIGURED
    rather than FRESH -- a summary that read FRESH because nothing was
    measured is the reassuring-number mistake ADR-0016 exists to prevent.
    """
    assert worst_freshness_state([]) == "NOT_CONFIGURED"
    assert worst_freshness_state(["FRESH", "FRESH"]) == "FRESH"
    assert worst_freshness_state(["FRESH", "STALE"]) == "STALE"
    assert worst_freshness_state(["FRESH", "AWAITING_APPROVAL"]) == "AWAITING_APPROVAL"
    assert worst_freshness_state(["AWAITING_APPROVAL", "STALE"]) == "STALE"
    assert worst_freshness_state(["FRESH", "NOT_CONFIGURED"]) == "NOT_CONFIGURED"


async def test_freshness_states_roll_up_to_the_datasources_worst_table(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """The value `quality_summary` reports, read at the seam below the DTO.

    Asserted here rather than only through the endpoint because
    `DataQualitySummaryRead.source_freshness_status` is still
    `Literal["NOT_CONFIGURED"]` -- see the xfail below. This half is provable
    today and stays provable after the Literal widens.
    """
    empty = await load_freshness_states(db, datasource_id=scenario.datasource.id, now=_NOW)
    assert empty.rolled_up_status == "NOT_CONFIGURED"

    await scenario.approved_contract(scenario.table.id, threshold_minutes=60)
    await scenario.observe(scenario.table.id, _NOW - timedelta(minutes=5))
    fresh = await load_freshness_states(db, datasource_id=scenario.datasource.id, now=_NOW)
    assert fresh.rolled_up_status == "FRESH"

    # A second, later table drags the answer to its worst member -- a
    # datasource is not fresh because one of its tables is.
    await scenario.approved_contract(scenario.other_table.id, threshold_minutes=60)
    await scenario.observe(scenario.other_table.id, _NOW - timedelta(hours=9))
    stale = await load_freshness_states(db, datasource_id=scenario.datasource.id, now=_NOW)
    assert stale.rolled_up_status == "STALE"


async def test_quality_summary_reports_the_real_rolled_up_freshness_state(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """The endpoint half of the roll-up above: the seam this row cannot close.

    `quality_summary` hardcoded "NOT_CONFIGURED" for every datasource in the
    product, configured or not, and the DTO's own type is why -- the field
    cannot express any other value, so the symptom was pinned by the type
    rather than by a missing query.
    """
    before = await quality_summary(
        scenario.datasource.id, context=scenario.checker_context(), session=db
    )
    assert before.source_freshness_status == "NOT_CONFIGURED"

    await scenario.approved_contract(scenario.table.id, threshold_minutes=60)
    await scenario.observe(scenario.table.id, datetime.now(UTC) - timedelta(minutes=5))

    fresh = await quality_summary(
        scenario.datasource.id, context=scenario.checker_context(), session=db
    )
    assert fresh.source_freshness_status == "FRESH"

    await scenario.approved_contract(scenario.other_table.id, threshold_minutes=60)
    await scenario.observe(scenario.other_table.id, datetime.now(UTC) - timedelta(hours=9))

    stale = await quality_summary(
        scenario.datasource.id, context=scenario.checker_context(), session=db
    )
    assert stale.source_freshness_status == "STALE"


async def test_load_freshness_states_reads_the_latest_observation_not_the_first(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """Freshness is judged on the newest watermark on file. Reading any other
    row would make a recovery invisible and the incident unresolvable."""
    await scenario.approved_contract(scenario.table.id, threshold_minutes=60)
    await scenario.observe(scenario.table.id, _NOW - timedelta(hours=10))
    await scenario.observe(scenario.table.id, _NOW - timedelta(minutes=5))

    states = await load_freshness_states(db, datasource_id=scenario.datasource.id, now=_NOW)

    assert [result.status for _, result in states.results] == ["FRESH"]


async def test_the_scheduler_pass_opens_no_session_when_the_interval_is_zero() -> None:
    """Default 0 means never, the same convention the task agents and the
    classification propagation pass use. Asserted by proving it opens no
    session at all: a pass that returned 0 after querying every datasource
    would still cost every deployment that never asked for it."""
    from aida.config import Settings
    from aida.workflows import scheduler

    def fail_session() -> object:
        raise AssertionError("the freshness pass must not open a session when off")

    original = scheduler.session_factory
    scheduler.session_factory = fail_session  # type: ignore[assignment]
    try:
        settings = Settings(_env_file=None)
        assert settings.freshness_evaluation_interval_minutes == 0
        swept = await scheduler.run_freshness_evaluation_pass(settings)
    finally:
        scheduler.session_factory = original  # type: ignore[assignment]

    assert swept == 0


async def test_the_scheduler_pass_sweeps_every_datasource_and_isolates_failures(
    scenario: _Scenario, maker: async_sessionmaker[AsyncSession], db: AsyncSession
) -> None:
    """The scheduled entry point itself -- interval honoured, one session per
    datasource, and a per-datasource failure logged and skipped rather than
    aborting the iteration (the convention every pass in that module keeps)."""
    from aida.config import Settings
    from aida.workflows import scheduler

    await scenario.approved_contract(scenario.table.id, threshold_minutes=60)
    await scenario.observe(scenario.table.id, _NOW - timedelta(hours=4))

    settings = Settings(_env_file=None, freshness_evaluation_interval_minutes=15)
    original = scheduler.session_factory
    scheduler.session_factory = maker  # type: ignore[assignment]
    try:
        swept = await scheduler.run_freshness_evaluation_pass(settings, now=_NOW)
        assert swept == 1

        # Inside the interval: the datasource is skipped, so nothing is
        # re-evaluated and the incident's occurrence count does not move.
        again = await scheduler.run_freshness_evaluation_pass(
            settings, now=_NOW + timedelta(minutes=5)
        )
        assert again == 0

        # One datasource raising must not abort the sweep.
        boom_calls = {"n": 0}
        real_evaluate = scheduler.evaluate_freshness_for_datasource

        async def boom(*args, **kwargs):
            boom_calls["n"] += 1
            raise RuntimeError("connector exploded")

        scheduler.evaluate_freshness_for_datasource = boom  # type: ignore[assignment]
        try:
            survived = await scheduler.run_freshness_evaluation_pass(
                settings, now=_NOW + timedelta(minutes=30)
            )
        finally:
            scheduler.evaluate_freshness_for_datasource = real_evaluate  # type: ignore[assignment]
        assert boom_calls["n"] == 1
        assert survived == 0
    finally:
        scheduler.session_factory = original  # type: ignore[assignment]
        scheduler._freshness_evaluation_last_run_at.clear()

    async with maker() as verify:
        incidents = await _incidents(verify)
        assert len(incidents) == 1
        assert incidents[0].status == "OPEN"
        assert incidents[0].occurrence_count == 1
        assert (
            await verify.scalar(
                select(func.count())
                .select_from(FreshnessWatermarkConfig)
                .where(FreshnessWatermarkConfig.status == "ACTIVE")
            )
        ) == 1
