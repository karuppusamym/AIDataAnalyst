"""DQ-6: seasonality-aware thresholds, wired end-to-end into the same
`evaluate_analysis_run` -> `DataQualityObservation`/`DataQualityIncident` path
DQ-1's notification routing and DQ-3's runtime coupling already consume,
unchanged.

`tests/test_data_quality_seasonality.py` proves the pure comparison function
(`data_quality.day_of_week_baseline` / `evaluate_quality`) reduces false
positives on a synthetic weekly cycle. This file proves the *wiring*: against
a real (in-memory sqlite) database, seeded through the ORM the same way
`test_custom_quality_rules.py` seeds `evaluate_rule_pack`, with genuinely
persisted `TableProfile` rows (real timestamps, real row counts, inserted one
scan at a time exactly as the profiling workflow would) --

  * with `quality_seasonal_thresholds_enabled` off (the default),
    `evaluate_analysis_run` opens a VOLUME_CHANGE incident for a completely
    normal Saturday, because it only ever compares to the single most recent
    prior profile (Friday) -- the same false positive the pure-function test
    demonstrates, now observed through the real incident-creation call site;
  * with the flag on, `evaluate_analysis_run`, reading that table's own
    already-persisted history back out of the database, does not open one --
    and the persisted `DataQualityObservation.evidence` shows
    `threshold_strategy: SEASONAL_DAY_OF_WEEK`, so the reason is auditable,
    not just an absence of an incident.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida import quality_service
from aida.config import Settings
from aida.db import Base
from aida.models import (
    DataDomain,
    DataQualityIncident,
    DataQualityObservation,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    TableProfile,
)
from aida.quality_service import evaluate_analysis_run
from tests.support.doubles import security_context

_WEEK_0_MONDAY = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
_WEEKDAY_BASE = 1000
_WEEKEND_BASE = 400


def _row_count_for(day_offset: int) -> int:
    weekday = (_WEEK_0_MONDAY + timedelta(days=day_offset)).weekday()
    base = _WEEKEND_BASE if weekday >= 5 else _WEEKDAY_BASE
    jitter = ((day_offset * 7) % 11) - 5
    return base + jitter


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class _Scenario:
    """One organization/datasource with two independently-profiled tables --
    `table_seasonal_off` and `table_seasonal_on` -- each carrying its own 61
    days (8 full weeks plus a 9th week through Friday) of real, persisted
    weekday~1000/weekend~400 `TableProfile` history, so the two tables can be
    evaluated under different `quality_seasonal_thresholds_enabled` settings
    without one run's incident state leaking into the other's.
    """

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
            slug="core-banking",
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

        self.table_seasonal_off = await self._table(schema, "daily_transactions_off")
        self.table_seasonal_on = await self._table(schema, "daily_transactions_on")
        return self

    async def _table(self, schema: MetadataSchema, name: str) -> MetadataTable:
        table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name=name,
            object_type="TABLE",
            fingerprint=f"fp-{name}",
        )
        self.db.add(table)
        await self.db.flush()
        return table

    async def seed_history_and_get_current_run(
        self, table: MetadataTable, *, history_days: int, current_offset: int
    ) -> UUID:
        """Persists one `TableProfile` per day for `range(history_days)` (each its
        own analysis run, own timestamp -- exactly what daily profiling scans
        would produce), then a final "current" profile at `current_offset`
        (strictly after the history) under a fresh analysis run. Returns that
        current run's id, the one `evaluate_analysis_run` should be called with.
        """
        for offset in range(history_days):
            self.db.add(
                TableProfile(
                    organization_id=self.organization.id,
                    analysis_run_id=uuid4(),
                    datasource_id=self.datasource.id,
                    table_id=table.id,
                    row_count_estimate=_row_count_for(offset),
                    sampled_row_count=_row_count_for(offset),
                    status="COMPLETED",
                    created_at=_WEEK_0_MONDAY + timedelta(days=offset),
                )
            )
        current_run_id = uuid4()
        self.db.add(
            TableProfile(
                organization_id=self.organization.id,
                analysis_run_id=current_run_id,
                datasource_id=self.datasource.id,
                table_id=table.id,
                row_count_estimate=_row_count_for(current_offset),
                sampled_row_count=_row_count_for(current_offset),
                status="COMPLETED",
                created_at=_WEEK_0_MONDAY + timedelta(days=current_offset),
            )
        )
        await self.db.flush()
        return current_run_id

    def context(self):
        return security_context(organization_id=self.organization.id, roles=frozenset({"Steward"}))


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def _open_volume_change_incident(
    db: AsyncSession, table_id: UUID
) -> DataQualityIncident | None:
    incidents = (
        await db.scalars(
            select(DataQualityIncident).where(
                DataQualityIncident.table_id == table_id,
                DataQualityIncident.anomaly_type == "VOLUME_CHANGE",
                DataQualityIncident.status == "OPEN",
            )
        )
    ).all()
    assert len(incidents) <= 1
    return incidents[0] if incidents else None


async def test_default_flag_off_still_opens_the_incident_the_rolling_baseline_always_did(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """Baseline behavior, unchanged: a normal Saturday still trips VOLUME_CHANGE
    against the unmodified rolling-previous-profile comparison when the new
    flag is off (its default) -- proving the rollout is genuinely opt-in."""
    run_id = await scenario.seed_history_and_get_current_run(
        scenario.table_seasonal_off, history_days=61, current_offset=61
    )

    counts = await evaluate_analysis_run(
        db,
        analysis_run_id=run_id,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.context(),
    )
    await db.commit()

    assert counts["incidents_opened"] == 1
    incident = await _open_volume_change_incident(db, scenario.table_seasonal_off.id)
    assert incident is not None

    observation = (
        await db.scalars(
            select(DataQualityObservation).where(
                DataQualityObservation.table_id == scenario.table_seasonal_off.id
            )
        )
    ).one()
    assert observation.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"
    assert "VOLUME_CHANGE" in observation.anomaly_types


async def test_flag_on_reads_real_persisted_history_and_suppresses_the_same_false_positive(
    scenario: _Scenario, db: AsyncSession, monkeypatch
) -> None:
    """The same shape of table, same normal Saturday, evaluated with
    `quality_seasonal_thresholds_enabled=True`: `evaluate_analysis_run` reads
    this table's own already-persisted `TableProfile` history back out of the
    database (not a mock, not a synthetic in-memory list -- real rows this
    test itself inserted earlier in the same session) and no VOLUME_CHANGE
    incident opens."""
    run_id = await scenario.seed_history_and_get_current_run(
        scenario.table_seasonal_on, history_days=61, current_offset=61
    )
    monkeypatch.setattr(
        quality_service,
        "get_settings",
        lambda: Settings(
            quality_seasonal_thresholds_enabled=True,
            quality_seasonal_min_samples=3,
            _env_file=None,
        ),
    )

    counts = await evaluate_analysis_run(
        db,
        analysis_run_id=run_id,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.context(),
    )
    await db.commit()

    assert counts["incidents_opened"] == 0
    assert counts["healthy"] == 1
    incident = await _open_volume_change_incident(db, scenario.table_seasonal_on.id)
    assert incident is None

    observation = (
        await db.scalars(
            select(DataQualityObservation).where(
                DataQualityObservation.table_id == scenario.table_seasonal_on.id
            )
        )
    ).one()
    assert observation.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"
    assert observation.evidence["seasonal_sample_count"] >= 3
    assert observation.anomaly_types == []
