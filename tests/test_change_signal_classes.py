"""R11-FP15: grant additions and replaced routine signatures are signalled, with their class.

* A grant new to a schema an earlier run read is GRANT_ADDED, and so is one back after a revoke.
  The first read of a schema still records nothing. A changed grant is GRANT_MODIFIED, a revoked
  one GRANT_REVOKED.
* A routine whose one signature was replaced by one new signature retires as SIGNATURE_CHANGED
  and names the routine that replaced it. An overload added beside a routine that stays pairs
  nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.change_signal_models import MetadataChangeSignal
from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataSourceGrant
from aida.models import AnalysisRun
from tests.test_change_signals import _datasource, _envelope, _grant, _routine, _scan


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _signals_of(
    session: AsyncSession, run: AnalysisRun, kind: str
) -> list[MetadataChangeSignal]:
    return list(
        await session.scalars(
            select(MetadataChangeSignal).where(
                MetadataChangeSignal.analysis_run_id == run.id,
                MetadataChangeSignal.subject_kind == kind,
            )
        )
    )


async def _grant_id(session: AsyncSession, grantee: str) -> Any:
    return await session.scalar(
        select(MetadataSourceGrant.id).where(MetadataSourceGrant.grantee == grantee)
    )


def _score(physical_type: str) -> dict[str, Any]:
    return {
        **_routine(),
        "name": "score",
        "routine_type": "FUNCTION",
        "parameters": [{"ordinal_position": 1, "physical_type": physical_type}],
    }


async def test_a_grant_added_modified_revoked_and_restored_is_classed(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    reader_grantable = {**_grant(), "is_grantable": True}
    auditor = {**_grant(), "grantee": "auditor"}

    first = await _scan(session, datasource, _envelope())
    added = await _scan(session, datasource, _envelope(grants=[_grant(), auditor]))
    modified = await _scan(session, datasource, _envelope(grants=[reader_grantable, auditor]))
    revoked = await _scan(session, datasource, _envelope(grants=[reader_grantable]))
    restored = await _scan(session, datasource, _envelope(grants=[reader_grantable, auditor]))

    reader_id = await _grant_id(session, "risk_reader")
    auditor_id = await _grant_id(session, "auditor")

    def classes(signals: list[MetadataChangeSignal]) -> set[tuple[Any, str, str | None]]:
        return {(s.subject_id, s.signal_type, s.change_class) for s in signals}

    assert await _signals_of(session, first, "GRANT") == []
    assert classes(await _signals_of(session, added, "GRANT")) == {
        (auditor_id, "PERMISSION_CHANGED", "GRANT_ADDED")
    }
    assert classes(await _signals_of(session, modified, "GRANT")) == {
        (reader_id, "PERMISSION_CHANGED", "GRANT_MODIFIED")
    }
    assert classes(await _signals_of(session, revoked, "GRANT")) == {
        (auditor_id, "PERMISSION_CHANGED", "GRANT_REVOKED")
    }
    assert classes(await _signals_of(session, restored, "GRANT")) == {
        (auditor_id, "PERMISSION_CHANGED", "GRANT_ADDED")
    }


async def test_a_replaced_signature_names_its_successor_and_an_added_overload_pairs_nothing(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope(routines=[_score("bigint")]))
    old_id = await session.scalar(select(MetadataRoutine.id))

    replaced = await _scan(session, datasource, _envelope(routines=[_score("text")]))
    new_id = await session.scalar(
        select(MetadataRoutine.id).where(MetadataRoutine.status == "ACTIVE")
    )
    overload = await _scan(
        session, datasource, _envelope(routines=[_score("text"), _score("numeric")])
    )

    (retired,) = await _signals_of(session, replaced, "ROUTINE")
    assert new_id != old_id
    assert (
        retired.subject_id,
        retired.signal_type,
        retired.change_class,
        retired.related_subject_id,
    ) == (old_id, "DEPRECATED", "SIGNATURE_CHANGED", new_id)
    assert await _signals_of(session, overload, "ROUTINE") == []
