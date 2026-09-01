"""DQ-4: custom quality rule packs, evaluated on their own schedule.

Covers:
  1. Pure functions: ``evaluate_rule`` for all three rule types (pass, fail,
     no-data-yet), ``rule_severity``, and ``rule_pack_due``.
  2. ``evaluate_rule_pack`` against a real in-memory sqlite database, seeded
     through the ORM: a violated rule opens a ``DataQualityIncident``; the
     same rule passing on a later sweep resolves it; failing again reopens
     it with an incremented ``occurrence_count``; a rule against a table
     with no stored profile is skipped, not treated as pass or fail.
  3. That the resulting incident is picked up by
     ``quality_coupling.fetch_open_incidents`` with zero changes to that
     module -- proving DQ-3's runtime coupling (retrieval demotion, tool
     gating, answer trust warnings) applies to a custom-rule incident
     exactly like a built-in one, since it filters only on
     datasource/table/status, never anomaly_type.
  4. ``run_due_rule_packs`` (the function wired into
     ``aida.workflows.scheduler.run_scheduler_iteration``): a pack sweeps
     when due and not when it isn't, and one pack's failure never blocks a
     sibling pack in the same sweep -- the same due-check/fault-isolation
     shape ``test_fleet_scheduling.py`` already proves for GL-6's owner
     routing.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida import custom_quality_rules
from aida.custom_quality_rules import (
    RuleEvaluation,
    evaluate_rule,
    evaluate_rule_pack,
    rule_pack_due,
    rule_severity,
    run_due_rule_packs,
)
from aida.db import Base
from aida.models import (
    ColumnProfile,
    DataDomain,
    DataQualityIncident,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QualityRule,
    QualityRulePack,
    TableProfile,
)
from aida.quality_coupling import fetch_open_incidents
from tests.support.doubles import security_context

# --- Pure functions -----------------------------------------------------------


def _rule(rule_type: str, threshold: float, *, column_id: UUID | None = None) -> QualityRule:
    return QualityRule(
        id=uuid4(),
        organization_id=uuid4(),
        rule_pack_id=uuid4(),
        table_id=uuid4(),
        column_id=column_id,
        name=f"{rule_type} check",
        rule_type=rule_type,
        threshold=threshold,
        enabled=True,
        created_by="steward",
    )


def _table_profile(row_count: int | None) -> TableProfile:
    return TableProfile(
        id=uuid4(),
        organization_id=uuid4(),
        analysis_run_id=uuid4(),
        datasource_id=uuid4(),
        table_id=uuid4(),
        row_count_estimate=row_count,
        sampled_row_count=row_count or 0,
        status="COMPLETED",
    )


def test_row_count_min_passes_and_fails() -> None:
    rule = _rule("TABLE_ROW_COUNT_MIN", 1000)
    assert evaluate_rule(rule, profile=_table_profile(1500), column_profile=None).passed is True
    assert evaluate_rule(rule, profile=_table_profile(500), column_profile=None).passed is False


def test_row_count_max_passes_and_fails() -> None:
    rule = _rule("TABLE_ROW_COUNT_MAX", 1000)
    assert evaluate_rule(rule, profile=_table_profile(500), column_profile=None).passed is True
    assert evaluate_rule(rule, profile=_table_profile(1500), column_profile=None).passed is False


def test_row_count_rule_with_no_profile_is_indeterminate() -> None:
    rule = _rule("TABLE_ROW_COUNT_MIN", 1000)
    result = evaluate_rule(rule, profile=None, column_profile=None)
    assert result.passed is None
    assert result.evidence == {"reason": "NO_PROFILE_DATA"}


def test_column_null_rate_max_passes_and_fails() -> None:
    rule = _rule("COLUMN_NULL_RATE_MAX", 0.1, column_id=uuid4())
    low_nulls = ColumnProfile(
        id=uuid4(),
        organization_id=uuid4(),
        table_profile_id=uuid4(),
        column_id=uuid4(),
        null_count=5,
        non_null_count=995,
        approximate_distinct_count=100,
    )
    high_nulls = ColumnProfile(
        id=uuid4(),
        organization_id=uuid4(),
        table_profile_id=uuid4(),
        column_id=uuid4(),
        null_count=500,
        non_null_count=500,
        approximate_distinct_count=100,
    )
    assert evaluate_rule(rule, profile=None, column_profile=low_nulls).passed is True
    assert evaluate_rule(rule, profile=None, column_profile=high_nulls).passed is False


def test_column_null_rate_rule_with_no_column_profile_is_indeterminate() -> None:
    rule = _rule("COLUMN_NULL_RATE_MAX", 0.1, column_id=uuid4())
    result = evaluate_rule(rule, profile=None, column_profile=None)
    assert result.passed is None


def test_evaluate_rule_rejects_unknown_rule_type() -> None:
    rule = _rule("NOT_A_REAL_TYPE", 1)
    with pytest.raises(ValueError, match="Unknown rule_type"):
        evaluate_rule(rule, profile=None, column_profile=None)


def test_rule_severity_escalates_far_past_threshold() -> None:
    rule = _rule("TABLE_ROW_COUNT_MIN", 1000)
    barely_under = RuleEvaluation(False, {"row_count": 900, "threshold": 1000})
    far_under = RuleEvaluation(False, {"row_count": 100, "threshold": 1000})
    assert rule_severity(barely_under, rule) == "WARNING"
    assert rule_severity(far_under, rule) == "CRITICAL"


def test_rule_pack_due() -> None:
    now = datetime.now(UTC)
    assert rule_pack_due(None, now, 60) is True
    assert rule_pack_due(now - timedelta(minutes=30), now, 60) is False
    assert rule_pack_due(now - timedelta(minutes=61), now, 60) is True


# --- evaluate_rule_pack against a real database --------------------------------


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

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()

        self.column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="amount",
            ordinal_position=1,
            physical_type="numeric",
            nullable=True,
            status="ACTIVE",
            fingerprint="fp-column-amount",
        )
        db.add(self.column)
        await db.flush()
        return self

    async def profile(
        self, *, row_count: int, null_count: int = 0, non_null_count: int = 100
    ) -> TableProfile:
        db = self.db
        table_profile = TableProfile(
            organization_id=self.organization.id,
            analysis_run_id=uuid4(),
            datasource_id=self.datasource.id,
            table_id=self.table.id,
            row_count_estimate=row_count,
            sampled_row_count=row_count,
            status="COMPLETED",
        )
        db.add(table_profile)
        await db.flush()
        db.add(
            ColumnProfile(
                organization_id=self.organization.id,
                table_profile_id=table_profile.id,
                column_id=self.column.id,
                null_count=null_count,
                non_null_count=non_null_count,
                approximate_distinct_count=non_null_count,
            )
        )
        await db.flush()
        return table_profile

    async def rule_pack(
        self, *, interval_minutes: int = 60, name: str = "Sales floor checks"
    ) -> QualityRulePack:
        db = self.db
        pack = QualityRulePack(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name=name,
            enabled=True,
            interval_minutes=interval_minutes,
            created_by="steward",
        )
        db.add(pack)
        await db.flush()
        return pack

    async def rule(
        self, pack: QualityRulePack, *, rule_type: str, threshold: float, column: bool = False
    ) -> QualityRule:
        db = self.db
        rule = QualityRule(
            organization_id=self.organization.id,
            rule_pack_id=pack.id,
            table_id=self.table.id,
            column_id=self.column.id if column else None,
            name=f"{rule_type} rule",
            rule_type=rule_type,
            threshold=threshold,
            enabled=True,
            created_by="steward",
        )
        db.add(rule)
        await db.flush()
        return rule

    def context(self):
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"Operations"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_violated_rule_opens_an_incident(scenario: _Scenario, db: AsyncSession) -> None:
    await scenario.profile(row_count=100)
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)

    counts = await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    assert counts == {
        "rules_evaluated": 1,
        "skipped_no_data": 0,
        "incidents_opened": 1,
        "incidents_resolved": 0,
    }
    incidents = (await db.scalars(select(DataQualityIncident))).all()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.status == "OPEN"
    assert incident.anomaly_type == f"CUSTOM_RULE:{rule.id}"
    assert incident.table_id == scenario.table.id
    assert incident.evidence == {"row_count": 100, "threshold": 1000.0}


async def test_custom_rule_incident_is_visible_to_dq3_coupling(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """Proves DQ-3's `fetch_open_incidents` -- already wired into tool gating
    (TL-3) and answer trust warnings (AG-6) -- picks up a custom-rule
    incident with no changes on its side."""
    await scenario.profile(row_count=100)
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    incidents = await fetch_open_incidents(
        db, datasource=scenario.datasource, table_ids=[scenario.table.id]
    )

    assert len(incidents) == 1
    assert incidents[0].anomaly_type == f"CUSTOM_RULE:{rule.id}"
    assert incidents[0].status == "OPEN"


async def test_passing_rule_resolves_the_open_incident(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await scenario.profile(row_count=100)
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    # The table recovers above the threshold on a later sweep.
    await scenario.profile(row_count=5000)
    counts = await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    assert counts["incidents_resolved"] == 1
    incident = (await db.scalars(select(DataQualityIncident))).one()
    assert incident.status == "RESOLVED"
    assert incident.resolved_by == "quality-rule-engine"


async def test_reopens_after_resolution_with_incremented_occurrence_count(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await scenario.profile(row_count=100)
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await scenario.profile(row_count=5000)
    await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    await scenario.profile(row_count=50)
    counts = await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    assert counts["incidents_opened"] == 1
    incident = (await db.scalars(select(DataQualityIncident))).one()
    assert incident.status == "OPEN"
    assert incident.occurrence_count == 2
    assert incident.resolved_at is None


async def test_rule_without_a_stored_profile_is_skipped_not_failed(
    scenario: _Scenario, db: AsyncSession
) -> None:
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)

    counts = await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    assert counts == {
        "rules_evaluated": 1,
        "skipped_no_data": 1,
        "incidents_opened": 0,
        "incidents_resolved": 0,
    }
    assert (await db.scalars(select(DataQualityIncident))).all() == []


async def test_column_null_rate_rule_end_to_end(scenario: _Scenario, db: AsyncSession) -> None:
    await scenario.profile(row_count=1000, null_count=600, non_null_count=400)
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="COLUMN_NULL_RATE_MAX", threshold=0.1, column=True)

    counts = await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())
    await db.commit()

    assert counts["incidents_opened"] == 1
    incident = (await db.scalars(select(DataQualityIncident))).one()
    assert incident.evidence["null_rate"] == 0.6


async def test_disabled_rules_are_never_evaluated(scenario: _Scenario, db: AsyncSession) -> None:
    await scenario.profile(row_count=1)
    pack = await scenario.rule_pack()
    rule = await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    rule.enabled = False
    await db.flush()

    counts = await evaluate_rule_pack(db, rule_pack=pack, rules=[rule], context=scenario.context())

    assert counts["rules_evaluated"] == 0
    assert (await db.scalars(select(DataQualityIncident))).all() == []


# --- run_due_rule_packs: the scheduler-wired sweep -----------------------------


class _NonClosingSession:
    """Wraps one already-open session so ``run_due_rule_packs``'s two
    ``async with session_factory() as session:`` blocks share it instead of
    each closing it on exit -- the same trick this codebase already uses
    (see ``test_profiling_exception_policy.py``'s ``session_factory``
    monkeypatches) to point a scheduler-style function at a real in-memory
    database without a live Postgres.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def test_run_due_rule_packs_sweeps_a_due_pack_and_tracks_last_run(
    scenario: _Scenario, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custom_quality_rules, "session_factory", lambda: _NonClosingSession(db))
    await scenario.profile(row_count=1)
    pack = await scenario.rule_pack(interval_minutes=60)
    await scenario.rule(pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    await db.commit()

    now = datetime.now(UTC)
    last_run_at: dict[UUID, datetime] = {}
    swept = await run_due_rule_packs(now=now, last_run_at=last_run_at)

    assert swept == 1
    assert last_run_at[pack.id] == now
    incidents = (await db.scalars(select(DataQualityIncident))).all()
    assert len(incidents) == 1

    # Immediately due again is False -- interval has not elapsed.
    swept_again = await run_due_rule_packs(now=now, last_run_at=last_run_at)
    assert swept_again == 0


async def test_run_due_rule_packs_isolates_one_packs_failure(
    scenario: _Scenario, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custom_quality_rules, "session_factory", lambda: _NonClosingSession(db))
    await scenario.profile(row_count=1)
    failing_pack = await scenario.rule_pack(interval_minutes=60, name="Failing pack")
    await scenario.rule(failing_pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    healthy_pack = await scenario.rule_pack(interval_minutes=60, name="Healthy pack")
    await scenario.rule(healthy_pack, rule_type="TABLE_ROW_COUNT_MIN", threshold=1000)
    await db.commit()

    real_evaluate = custom_quality_rules.evaluate_rule_pack

    async def flaky_evaluate(session, *, rule_pack, rules, context, now=None):
        if rule_pack.id == failing_pack.id:
            raise RuntimeError("simulated bad rule / transient DB error")
        return await real_evaluate(
            session, rule_pack=rule_pack, rules=rules, context=context, now=now
        )

    monkeypatch.setattr(custom_quality_rules, "evaluate_rule_pack", flaky_evaluate)

    now = datetime.now(UTC)
    last_run_at: dict[UUID, datetime] = {}
    swept = await run_due_rule_packs(now=now, last_run_at=last_run_at)

    assert swept == 1
    assert failing_pack.id not in last_run_at
    assert last_run_at[healthy_pack.id] == now
