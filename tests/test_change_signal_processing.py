"""R11-FP16: change signals hold exactly what a source change can have broken.

Signals (R11-FP15) say which objects changed. These tests drive real rescans against in-memory
SQLite, process the signals, and pin what each kind of change does -- through the incident sink
DQ-3 already couples to governed tools, never through a second lifecycle:

* a redefined view -- even literal-only -- holds the tools over it (CRITICAL, tool gate BLOCKs);
* a reshaped table warns, and a redefined routine opens nothing (the lineage agent re-examines);
* every signal ends PROCESSED with its action, a second pass does nothing, and a resolved hold
  reopens on the next change instead of stacking a second incident;
* the scheduler pass is off by default and opens no session when off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.workflows.scheduler as scheduler
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import (
    ACTION_LINEAGE_REEXAMINE,
    ACTION_TABLE_RESHAPED,
    ACTION_VIEW_REDEFINED,
    SOURCE_CHANGE_ANOMALY_TYPE,
    process_change_signals,
)
from aida.config import Settings
from aida.db import Base
from aida.models import DataQualityIncident
from aida.quality_coupling import check_tool_gate, fetch_open_incidents
from tests.test_change_signals import (
    _VIEW_SQL,
    _column,
    _datasource,
    _envelope,
    _routine,
    _scan,
    _table,
    _table_id,
    _view,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _incident(session: AsyncSession, table_id: object) -> DataQualityIncident | None:
    return await session.scalar(
        select(DataQualityIncident).where(
            DataQualityIncident.table_id == table_id,
            DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
        )
    )


async def test_a_redefined_view_holds_the_tools_over_it_and_every_signal_is_processed(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope())
    # Literal-only: lineage is unchanged, and every answer over the view is not.
    await _scan(
        session,
        datasource,
        _envelope(tables=[_table(), _view(_VIEW_SQL.replace("'OPEN'", "'CLOSED'"))]),
    )

    outcome = await process_change_signals(
        session, organization_id=datasource.organization_id, limit=100
    )
    await session.commit()

    view_id = await _table_id(session, "open_account")
    incident = await _incident(session, view_id)
    assert incident is not None
    assert (incident.severity, incident.status) == ("CRITICAL", "OPEN")
    gate = check_tool_gate(
        tool_id="tool-over-the-view",
        dependency_asset_ids=[str(view_id)],
        incidents=await fetch_open_incidents(session, datasource=datasource, table_ids=[view_id]),
    )
    assert gate.action == "BLOCK"

    signals = list(await session.scalars(select(MetadataChangeSignal)))
    assert signals and all(signal.status == "PROCESSED" for signal in signals)
    assert outcome.processed == len(signals) and outcome.failed == 0
    assert outcome.actions[ACTION_VIEW_REDEFINED] == 1
    view_signal = next(signal for signal in signals if signal.subject_kind == "VIEW")
    assert view_signal.outcome == {"action": ACTION_VIEW_REDEFINED, "incident_id": str(incident.id)}

    again = await process_change_signals(
        session, organization_id=datasource.organization_id, limit=100
    )
    assert again.processed == 0


async def test_a_reshaped_table_warns_and_a_redefined_routine_opens_nothing(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope())
    await _scan(
        session,
        datasource,
        _envelope(
            tables=[
                _table("account", _column("account_id", 1), _column("customer_id", 2, "text")),
                _view(),
            ],
            routines=[_routine("BEGIN DELETE FROM customer.account; END;")],
        ),
    )

    outcome = await process_change_signals(
        session, organization_id=datasource.organization_id, limit=100
    )
    await session.commit()

    account_id = await _table_id(session, "account")
    incident = await _incident(session, account_id)
    assert incident is not None and incident.severity == "WARNING"
    gate = check_tool_gate(
        tool_id="tool-over-account",
        dependency_asset_ids=[str(account_id)],
        incidents=await fetch_open_incidents(
            session, datasource=datasource, table_ids=[account_id]
        ),
    )
    assert gate.action == "WARN"
    assert outcome.actions == {ACTION_LINEAGE_REEXAMINE: 1, ACTION_TABLE_RESHAPED: 1}
    incidents = list(await session.scalars(select(DataQualityIncident)))
    assert [row.table_id for row in incidents] == [account_id]


async def test_a_resolved_hold_reopens_on_the_next_change_rather_than_stacking(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope())
    await _scan(
        session,
        datasource,
        _envelope(tables=[_table(), _view("SELECT customer_id FROM customer.account")]),
    )
    await process_change_signals(session, organization_id=datasource.organization_id, limit=100)
    view_id = await _table_id(session, "open_account")
    incident = await _incident(session, view_id)
    assert incident is not None
    incident.status = "RESOLVED"
    incident.resolved_by = "steward@example.com"
    await session.commit()

    await _scan(
        session,
        datasource,
        _envelope(tables=[_table(), _view("SELECT account_id FROM customer.account")]),
    )
    outcome = await process_change_signals(
        session, organization_id=datasource.organization_id, limit=100
    )
    await session.commit()

    rows = list(
        await session.scalars(
            select(DataQualityIncident).where(DataQualityIncident.table_id == view_id)
        )
    )
    assert len(rows) == 1
    (reopened,) = rows
    assert (reopened.status, reopened.occurrence_count, reopened.resolved_by) == ("OPEN", 2, None)
    assert len(reopened.evidence["changes"]) == 2
    assert outcome.incidents_opened == 1


async def test_the_scheduler_pass_is_off_by_default_and_opens_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> None:
        raise AssertionError("the pass opened a session while off")

    monkeypatch.setattr(scheduler, "session_factory", refuse)

    assert await scheduler.run_change_signal_processing_pass(Settings(_env_file=None)) == 0
