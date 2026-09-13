"""R11-B8: something finally writes the freshness observations the evaluator reads.

Every approved freshness contract evaluated STALE because nothing outside the
tests ever wrote a `FreshnessObservation`. The observer reads `MAX(<watermark
column>)` through the query gateway, under the ADR-0014 addendum of
2026-09-12, which permits that one statistic and nothing more. These tests pin
the addendum's conditions -- an approved contract, a temporal column, a
governed read whose masking is honoured, retention, and no value outside the
observation row -- because each is what keeps an observer from becoming a
general-purpose way to read source values.

The gateway is faked here: its own governance is proven by its own suites, and
what this module owns is what it asks the gateway and what it does with the
answer. The live run against the sample estate is recorded on the tracker row.
"""

import inspect
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.freshness import load_freshness_states
from aida.freshness_observation import (
    as_watermark,
    observe_freshness_for_datasource,
    watermark_statement,
)
from aida.models import AuditEvent, FreshnessObservation, MetadataColumn, MetadataTable
from aida.quality_api import approve_freshness_config, upsert_freshness_config
from aida.query_gateway import AuthorizationRejected
from aida.schemas import FreshnessConfigUpsert
from aida.workflows import scheduler
from tests import test_r11b8_freshness_incident_sink as sink
from tests.test_r11b8_freshness_incident_sink import _NOW, _Scenario

# The sink suite's in-memory database and two-table estate, reused rather than
# rebuilt. Bound as module attributes, which is all pytest needs to find them.
maker = sink.maker
db = sink.db
scenario = sink.scenario


class _Gateway:
    """Records each statement it is asked to run and answers as configured."""

    def __init__(
        self,
        *,
        value: Any = None,
        masked: tuple[str, ...] = (),
        errors: list[Exception | None] | None = None,
    ) -> None:
        self.value = value
        self.masked = masked
        self.errors = list(errors or [])
        self.statements: list[str] = []

    async def execute(self, session: AsyncSession, **kwargs: Any) -> SimpleNamespace:
        self.statements.append(kwargs["sql"])
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return SimpleNamespace(
            rows=({"watermark_value": self.value},),
            masked_columns=self.masked,
            tokenized_columns=(),
        )


async def _column(
    scenario: _Scenario, table: MetadataTable, name: str, physical_type: str
) -> None:
    scenario.db.add(
        MetadataColumn(
            id=uuid4(),
            organization_id=scenario.organization.id,
            table_id=table.id,
            name=name,
            ordinal_position=1,
            physical_type=physical_type,
            nullable=True,
            fingerprint=f"fp-{name}",
            status="ACTIVE",
        )
    )
    await scenario.db.commit()


async def _contract(
    scenario: _Scenario, table_id: UUID, column: str, *, approve: bool = True
) -> None:
    await upsert_freshness_config(
        scenario.datasource.id,
        table_id,
        FreshnessConfigUpsert(watermark_column=column, threshold_minutes=60),
        context=scenario.maker_context(),
        session=scenario.db,
    )
    if approve:
        await approve_freshness_config(
            scenario.datasource.id,
            table_id,
            context=scenario.checker_context(),
            session=scenario.db,
        )
    await scenario.db.commit()


async def _observe(scenario: _Scenario, gateway: _Gateway) -> Any:
    sweep = await observe_freshness_for_datasource(
        scenario.db,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.worker_context(),
        gateway=gateway,  # type: ignore[arg-type]
        max_tables=500,
        now=_NOW,
    )
    await scenario.db.commit()
    return sweep


async def _observations(scenario: _Scenario) -> list[FreshnessObservation]:
    return list((await scenario.db.scalars(select(FreshnessObservation))).all())


async def test_an_approved_contract_is_observed_and_then_evaluates_fresh(
    scenario: _Scenario,
) -> None:
    """The defect this closes: the contract is approved, the data is recent,
    and until now the table still read STALE, because no observation existed."""
    await _column(scenario, scenario.table, "updated_at", "timestamp with time zone")
    await _contract(scenario, scenario.table.id, "updated_at")
    gateway = _Gateway(value=_NOW - timedelta(minutes=30))

    sweep = await _observe(scenario, gateway)

    assert sweep.observed == 1
    assert gateway.statements == [
        'SELECT MAX("updated_at") AS watermark_value FROM "finance"."transactions"'
    ]
    [observation] = await _observations(scenario)
    assert as_watermark(observation.watermark_value) == _NOW - timedelta(minutes=30)
    states = await load_freshness_states(
        scenario.db, datasource_id=scenario.datasource.id, now=_NOW
    )
    assert [result.status for _, result in states.results] == ["FRESH"]


async def test_an_unapproved_contract_is_never_read(scenario: _Scenario) -> None:
    """Maker-checker is the addendum's first condition: nothing is read on a
    contract only its author has seen."""
    await _column(scenario, scenario.table, "updated_at", "timestamp")
    await _contract(scenario, scenario.table.id, "updated_at", approve=False)
    gateway = _Gateway(value=_NOW)

    sweep = await _observe(scenario, gateway)

    assert (sweep.contracts_read, gateway.statements) == (0, [])
    assert await _observations(scenario) == []


async def test_a_contract_on_a_column_that_is_not_a_timestamp_reads_nothing(
    scenario: _Scenario,
) -> None:
    """Without this the observer is a way to read `MAX(balance)` from any table
    a steward can write a contract for. Refused before a statement exists."""
    await _column(scenario, scenario.table, "balance", "numeric(18,2)")
    await _contract(scenario, scenario.table.id, "balance")
    gateway = _Gateway(value=Decimal("1000000.00"))

    sweep = await _observe(scenario, gateway)

    assert sweep.outcomes == {"COLUMN_NOT_TEMPORAL": 1}
    assert gateway.statements == []
    assert await _observations(scenario) == []


async def test_a_column_the_catalog_does_not_list_reads_nothing(scenario: _Scenario) -> None:
    await _contract(scenario, scenario.table.id, "updated_at")
    gateway = _Gateway(value=_NOW)

    sweep = await _observe(scenario, gateway)

    assert sweep.outcomes == {"COLUMN_NOT_IN_CATALOG": 1}
    assert gateway.statements == []


async def test_a_watermark_masked_for_the_scheduler_stores_nothing(scenario: _Scenario) -> None:
    """The gateway's masking applies to the observer like anyone else. A masked
    maximum is not a timestamp, and storing it would be storing the mask."""
    await _column(scenario, scenario.table, "updated_at", "timestamp")
    await _contract(scenario, scenario.table.id, "updated_at")
    gateway = _Gateway(value="***MASKED***", masked=("watermark_value",))

    sweep = await _observe(scenario, gateway)

    assert sweep.outcomes == {"MASKED": 1}
    assert await _observations(scenario) == []


async def test_a_refused_read_is_counted_and_the_next_table_is_still_read(
    scenario: _Scenario,
) -> None:
    for table in (scenario.table, scenario.other_table):
        await _column(scenario, table, "updated_at", "timestamp")
        await _contract(scenario, table.id, "updated_at")
    gateway = _Gateway(
        value=_NOW - timedelta(minutes=5),
        errors=[AuthorizationRejected("policy_denied"), None],
    )

    sweep = await _observe(scenario, gateway)

    assert sweep.outcomes == {"REFUSED:policy_denied": 1, "OBSERVED": 1}
    assert len(gateway.statements) == 2
    assert len(await _observations(scenario)) == 1


async def test_an_empty_table_yields_no_observation(scenario: _Scenario) -> None:
    await _column(scenario, scenario.table, "updated_at", "timestamp")
    await _contract(scenario, scenario.table.id, "updated_at")

    sweep = await _observe(scenario, _Gateway(value=None))

    assert sweep.outcomes == {"NO_VALUE": 1}
    assert await _observations(scenario) == []


async def test_observations_past_the_contracts_retention_are_deleted(
    scenario: _Scenario,
) -> None:
    await _column(scenario, scenario.table, "updated_at", "timestamp")
    await _contract(scenario, scenario.table.id, "updated_at")  # retention_days=365
    for age_days in (400, 10):
        scenario.db.add(
            FreshnessObservation(
                organization_id=scenario.organization.id,
                datasource_id=scenario.datasource.id,
                table_id=scenario.table.id,
                watermark_value=_NOW - timedelta(days=age_days),
                observed_at=_NOW - timedelta(days=age_days),
            )
        )
    await scenario.db.commit()

    await _observe(scenario, _Gateway(value=_NOW))

    kept = sorted(as_watermark(o.observed_at) for o in await _observations(scenario))
    assert kept == [_NOW - timedelta(days=10), _NOW]


async def test_the_watermark_never_reaches_the_audit_record(scenario: _Scenario) -> None:
    """The addendum permits the value on the observation row. The sweep's own
    record says what happened -- counts and reason codes -- and not what it
    read."""
    await _column(scenario, scenario.table, "updated_at", "timestamp")
    await _contract(scenario, scenario.table.id, "updated_at")
    distinctive = datetime(2019, 7, 23, 4, 56, 7, tzinfo=UTC)

    await _observe(scenario, _Gateway(value=distinctive))

    rows = (
        await scenario.db.scalars(
            select(AuditEvent).where(AuditEvent.action == "data_quality.freshness.observe")
        )
    ).all()
    assert len(rows) == 1
    dumped = json.dumps(
        [
            {
                attr.key: getattr(row, attr.key)
                for attr in sqlalchemy_inspect(row).mapper.column_attrs
            }
            for row in rows
        ],
        default=str,
    )
    assert "2019-07-23" not in dumped
    assert '"observed": 1' in dumped


def test_the_statement_is_quoted_for_the_sources_dialect() -> None:
    assert watermark_statement(
        schema="customer", table="account", column="opened_at", dialect="postgres"
    ) == 'SELECT MAX("opened_at") AS watermark_value FROM "customer"."account"'
    assert watermark_statement(
        schema="customer", table="account", column="opened_at", dialect="tsql"
    ) == "SELECT MAX([opened_at]) AS watermark_value FROM [customer].[account]"


def test_a_watermark_is_an_aware_instant_or_nothing() -> None:
    assert as_watermark(date(2026, 9, 1)) == datetime(2026, 9, 1, tzinfo=UTC)
    assert as_watermark(datetime(2026, 9, 1, 8, 30)) == datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    assert as_watermark("2026-09-01T08:30:00+00:00") == datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    assert as_watermark("not a date") is None
    assert as_watermark(Decimal("5")) is None


def test_the_scheduled_pass_observes_before_it_evaluates() -> None:
    source = inspect.getsource(scheduler.run_freshness_evaluation_pass)
    observe = source.find("observe_freshness_for_datasource(")
    evaluate = source.find("evaluate_freshness_for_datasource(")
    assert observe != -1, "the scheduled pass no longer observes"
    assert observe < evaluate, "evaluating before observing judges a stale watermark"
