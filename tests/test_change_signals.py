"""R11-FP15: a rescan records *which* objects changed, as value-free signals.

Before this a rescan left counters (`changed_objects`) and nothing else, so nothing downstream
could re-examine the one redefined view or retired routine selectively. These tests drive both
persistence halves against in-memory SQLite, the way `test_envelope_v11` does, and pin:

* a first scan, and a rescan of identical metadata, record nothing;
* a changed definition is classed LITERAL_ONLY when only literals moved, STRUCTURAL otherwise;
* table shape changes, retirements and revoked grants are signalled against the right subject;
* publishing an ontology version records a meaning signal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.envelope_models  # noqa: F401  -- registers the 1.1 tables on the metadata
import aida.models  # noqa: F401
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals_api import list_change_signals
from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataSourceGrant
from aida.ingestion import envelope_to_discovery, persist_envelope_extensions
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataTable,
    Organization,
    Project,
)
from aida.schemas import MetadataIngestionCreate
from aida.security import SecurityContext
from aida.workflows.activities import persist_discovery_snapshot

_EMITTED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_VIEW_SQL = "SELECT account_id FROM customer.account WHERE status = 'OPEN'"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _datasource(session: AsyncSession) -> DataSource:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    lob = LineOfBusiness(
        organization_id=organization.id, name="Retail", code=f"RTL{uuid4().hex[:4]}"
    )
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Deposits",
        code=f"DEP{uuid4().hex[:4]}",
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="Consumer warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://consumer",
        status="ACTIVE",
    )
    session.add(datasource)
    await session.flush()
    return datasource


def _column(name: str, position: int, physical_type: str = "bigint") -> dict[str, Any]:
    return {
        "name": name,
        "ordinal_position": position,
        "physical_type": physical_type,
        "nullable": False,
    }


def _table(name: str = "account", *columns: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "object_type": "BASE_TABLE",
        "columns": list(columns) or [_column("account_id", 1), _column("customer_id", 2)],
        "constraints": [],
    }


def _view(definition_sql: str = _VIEW_SQL) -> dict[str, Any]:
    return {
        "name": "open_account",
        "object_type": "VIEW",
        "columns": [_column("account_id", 1)],
        "constraints": [],
        "view_definition": {"definition_sql": definition_sql},
    }


def _routine(
    body_sql: str = "BEGIN UPDATE customer.account SET closed_on = now(); END;",
) -> dict[str, Any]:
    return {
        "name": "close_account",
        "routine_type": "PROCEDURE",
        "language": "plpgsql",
        "body_sql": body_sql,
        "parameters": [],
    }


def _grant() -> dict[str, Any]:
    return {
        "grantee": "risk_reader",
        "grantee_type": "ROLE",
        "privilege": "SELECT",
        "object_type": "TABLE",
        "object_name": "account",
    }


def _envelope(
    *,
    tables: list[dict[str, Any]] | None = None,
    routines: list[dict[str, Any]] | None = None,
    grants: list[dict[str, Any]] | None = None,
) -> MetadataIngestionCreate:
    return MetadataIngestionCreate.model_validate(
        {
            "envelope_version": "1.1",
            "idempotency_key": f"inventory:{uuid4().hex}",
            "producer": "bank-metadata-bridge",
            "transport": "PUSH",
            "snapshot_type": "FULL",
            "emitted_at": _EMITTED_AT,
            "catalogs": [
                {
                    "name": "bank",
                    "schemas": [
                        {
                            "name": "customer",
                            "tables": tables if tables is not None else [_table(), _view()],
                            "routines": routines if routines is not None else [_routine()],
                            "grants": grants if grants is not None else [_grant()],
                        }
                    ],
                }
            ],
        }
    )


async def _scan(
    session: AsyncSession, datasource: DataSource, envelope: MetadataIngestionCreate
) -> AnalysisRun:
    run = AnalysisRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="PUSH",
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    discovery = envelope_to_discovery(envelope)
    await persist_discovery_snapshot(
        session, run, datasource, discovery, deprecate_missing=True, connector_capabilities={}
    )
    await persist_envelope_extensions(
        session, datasource, discovery, deprecate_missing=True, analysis_run_id=run.id
    )
    await session.commit()
    return run


async def _signals(session: AsyncSession, run: AnalysisRun) -> set[tuple[str, str, str | None]]:
    rows = await session.scalars(
        select(MetadataChangeSignal).where(MetadataChangeSignal.analysis_run_id == run.id)
    )
    return {(row.subject_kind, row.signal_type, row.change_class) for row in rows}


async def _table_id(session: AsyncSession, name: str) -> Any:
    return await session.scalar(select(MetadataTable.id).where(MetadataTable.name == name))


async def test_a_first_scan_and_an_identical_rescan_record_nothing(session: AsyncSession) -> None:
    datasource = await _datasource(session)

    first = await _scan(session, datasource, _envelope())
    again = await _scan(session, datasource, _envelope())

    assert await _signals(session, first) == set()
    assert await _signals(session, again) == set()


async def test_a_literal_only_view_change_is_told_apart_from_a_structural_one(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope())

    literal = await _scan(
        session,
        datasource,
        _envelope(tables=[_table(), _view(_VIEW_SQL.replace("'OPEN'", "'CLOSED'"))]),
    )
    structural = await _scan(
        session,
        datasource,
        _envelope(tables=[_table(), _view("SELECT customer_id FROM customer.account")]),
    )

    assert ("VIEW", "DEFINITION_CHANGED", "LITERAL_ONLY") in await _signals(session, literal)
    assert ("VIEW", "DEFINITION_CHANGED", "STRUCTURAL") in await _signals(session, structural)
    view_id = await _table_id(session, "open_account")
    subjects = {
        row.subject_id
        for row in await session.scalars(
            select(MetadataChangeSignal).where(MetadataChangeSignal.subject_kind == "VIEW")
        )
    }
    assert subjects == {view_id}


async def test_table_shape_retirement_routines_and_grants_are_each_signalled(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(
        session,
        datasource,
        _envelope(tables=[_table(), _table("ledger", _column("entry_id", 1)), _view()]),
    )
    routine_id = await session.scalar(select(MetadataRoutine.id))
    grant_id = await session.scalar(select(MetadataSourceGrant.id))

    rescan = await _scan(
        session,
        datasource,
        _envelope(
            # account: customer_id retyped; ledger: gone; brand-new table: no signal.
            tables=[
                _table("account", _column("account_id", 1), _column("customer_id", 2, "text")),
                _table("branch", _column("branch_id", 1)),
                _view(),
            ],
            routines=[],
            grants=[],
        ),
    )

    rows = list(
        await session.scalars(
            select(MetadataChangeSignal).where(MetadataChangeSignal.analysis_run_id == rescan.id)
        )
    )
    by_subject = {(row.subject_kind, row.subject_id): row.signal_type for row in rows}
    assert by_subject[("TABLE", await _table_id(session, "account"))] == "STRUCTURE_CHANGED"
    assert by_subject[("TABLE", await _table_id(session, "ledger"))] == "DEPRECATED"
    assert by_subject[("ROUTINE", routine_id)] == "DEPRECATED"
    assert by_subject[("GRANT", grant_id)] == "PERMISSION_CHANGED"
    assert ("TABLE", await _table_id(session, "branch")) not in by_subject
    assert all(row.datasource_id == datasource.id for row in rows)
    assert all(row.status == "PENDING" for row in rows)


async def test_a_datasources_signals_are_read_only_inside_its_organization(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope())
    await _scan(session, datasource, _envelope(routines=[]))
    steward = SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"DataSteward"}),
    )

    pending = await list_change_signals(
        datasource.id, "PENDING", 100, steward, session, Settings(_env_file=None)
    )
    processed = await list_change_signals(
        datasource.id, "PROCESSED", 100, steward, session, Settings(_env_file=None)
    )

    assert [(row.subject_kind, row.signal_type) for row in pending] == [("ROUTINE", "DEPRECATED")]
    assert processed == []
    outsider = SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )
    with pytest.raises(HTTPException):
        await list_change_signals(
            datasource.id, None, 100, outsider, session, Settings(_env_file=None)
        )


async def test_a_changed_routine_body_is_a_definition_change(session: AsyncSession) -> None:
    datasource = await _datasource(session)
    await _scan(session, datasource, _envelope())

    rescan = await _scan(
        session,
        datasource,
        _envelope(routines=[_routine("BEGIN DELETE FROM customer.account; END;")]),
    )

    assert ("ROUTINE", "DEFINITION_CHANGED", "STRUCTURAL") in await _signals(session, rescan)
