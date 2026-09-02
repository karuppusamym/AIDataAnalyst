"""DQ-8 -- open framework for third-party detector quality signals.

Exercises the real endpoint bodies (`aida.quality_api.ingest_external_quality_signal`
/ `list_external_quality_signals`) and the real reconciliation service
(`aida.external_quality_signals.ingest_external_signal`) against an in-memory
SQLite database, following `tests/test_asset_evidence.py`'s rationale: PostgreSQL
is unreachable in this sandbox, but SQLite is a real SQL engine that enforces the
same row semantics -- including the `(vendor, native id, observed_at)` uniqueness
the idempotency claim rests on.

Coverage, matching the DQ-8 exit criterion ("Monte Carlo / Anomalo signals
ingested"):

1. a Monte Carlo signal and an Anomalo signal ingested end to end, each opening a
   durable incident and readable back as externally-sourced;
2. the `source` discriminator keeps external incidents separate from internally
   computed ones on the same table;
3. idempotent re-ingest on (vendor, native id, observed_at);
4. a RESOLVED signal auto-resolves the matching incident;
5. an audit row is written in the ingest transaction (INV-7);
6. cross-tenant access is denied (INV-5) and the wrong role is denied (AU-7);
7. INV-6: no raw source values are persisted -- only detector metadata/refs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    AuditEvent,
    DataDomain,
    DataQualityIncident,
    DataSource,
    ExternalQualitySignal,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
)
from aida.quality_api import ingest_external_quality_signal, list_external_quality_signals
from aida.schemas import ExternalQualitySignalIngest
from tests.support.app_surface import iter_api_routes, require_roles_gate
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_datasource(session: AsyncSession) -> DataSource:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"src-{uuid4().hex[:8]}",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return datasource


async def _seed_table(session: AsyncSession, datasource: DataSource, *, name: str) -> MetadataTable:
    schema = datasource._test_schema  # type: ignore[attr-defined]
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


async def _seed_column(
    session: AsyncSession, table: MetadataTable, *, name: str
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=1,
        physical_type="numeric",
        nullable=True,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


def _context(datasource: DataSource, **overrides):
    return security_context(
        organization_id=datasource.organization_id,
        roles=frozenset({"DataSteward"}),
        **overrides,
    )


def _envelope(table: MetadataTable, **overrides) -> ExternalQualitySignalIngest:
    payload = {
        "detector_vendor": "MONTE_CARLO",
        "detector_native_id": "monitor-42",
        "table_id": table.id,
        "severity": "CRITICAL",
        "signal_status": "OPEN",
        "summary": "Freshness breach detected by external monitor.",
        "observed_at": _NOW,
        "details": {"monitor_name": "orders freshness", "breach_count": 3},
    }
    payload.update(overrides)
    return ExternalQualitySignalIngest(**payload)


async def _ingest(session, datasource, envelope, **ctx_overrides):
    return await ingest_external_quality_signal(
        datasource.id,
        envelope,
        context=_context(datasource, **ctx_overrides),
        session=session,
    )


# ---------------------------------------------------------------------------
# 1. Monte Carlo + Anomalo ingested end to end, readable back as external
# ---------------------------------------------------------------------------


async def test_monte_carlo_signal_ingested_and_readable_back_as_external(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="orders")
    await session.commit()

    result = await _ingest(session, datasource, _envelope(table))

    assert result.deduplicated is False
    assert result.incident_opened is True
    assert result.signal.detector_vendor == "MONTE_CARLO"
    assert result.signal.incident_id is not None

    # Persisted as an ExternalQualitySignal row (its own table).
    stored = (await session.scalars(select(ExternalQualitySignal))).all()
    assert len(stored) == 1
    assert stored[0].signal_status == "OPEN"

    # Reconciled into a durable incident stamped source="EXTERNAL".
    incident = await session.get(DataQualityIncident, result.signal.incident_id)
    assert incident is not None
    assert incident.source == "EXTERNAL"
    assert incident.status == "OPEN"
    assert incident.anomaly_type == "EXTERNAL:MONTE_CARLO"

    # Readable back via the list endpoint as externally-sourced.
    page = await list_external_quality_signals(
        datasource.id,
        detector_vendor=None,
        table_id=None,
        limit=100,
        offset=0,
        context=_context(datasource),
        session=session,
    )
    assert page.total == 1
    assert page.items[0].detector_vendor == "MONTE_CARLO"


async def test_anomalo_signal_ingested_end_to_end(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="customers")
    await session.commit()

    result = await _ingest(
        session,
        datasource,
        _envelope(
            table,
            detector_vendor="ANOMALO",
            detector_native_id="check-7",
            severity="WARNING",
            summary="Null-rate anomaly flagged by Anomalo.",
        ),
    )

    assert result.incident_opened is True
    incident = await session.get(DataQualityIncident, result.signal.incident_id)
    assert incident.source == "EXTERNAL"
    assert incident.severity == "WARNING"
    assert incident.anomaly_type == "EXTERNAL:ANOMALO"


async def test_vendor_name_is_normalized_to_upper_snake(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_norm")
    await session.commit()

    result = await _ingest(session, datasource, _envelope(table, detector_vendor="Monte Carlo"))
    assert result.signal.detector_vendor == "MONTE_CARLO"


async def test_column_scoped_signal_records_the_column(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_col")
    column = await _seed_column(session, table, name="amount")
    await session.commit()

    result = await _ingest(session, datasource, _envelope(table, column_id=column.id))
    assert result.signal.column_id == column.id


# ---------------------------------------------------------------------------
# 2. source discriminator keeps external and internal signals apart
# ---------------------------------------------------------------------------


async def test_external_incident_never_conflated_with_internal(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_mixed")
    # An internally-computed incident on the same table (source defaults to INTERNAL).
    internal = DataQualityIncident(
        id=uuid4(),
        organization_id=table.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        fingerprint=uuid4().hex,
        anomaly_type="VOLUME_CHANGE",
        severity="WARNING",
        status="OPEN",
        summary="Row count dropped below the governed baseline.",
        first_observed_at=_NOW,
        last_observed_at=_NOW,
    )
    session.add(internal)
    await session.commit()
    assert internal.source == "INTERNAL"

    result = await _ingest(session, datasource, _envelope(table))

    # Two distinct incidents; the external one did not reopen/mutate the internal one.
    incidents = (await session.scalars(select(DataQualityIncident))).all()
    assert len(incidents) == 2
    by_source = {i.source for i in incidents}
    assert by_source == {"INTERNAL", "EXTERNAL"}
    assert result.signal.incident_id != internal.id

    # The list endpoint returns only external signals; the internal incident is
    # not an ExternalQualitySignal at all.
    assert await session.scalar(select(func.count()).select_from(ExternalQualitySignal)) == 1


# ---------------------------------------------------------------------------
# 3. idempotent re-ingest on (vendor, native id, observed_at)
# ---------------------------------------------------------------------------


async def test_idempotent_reingest_does_not_duplicate(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_idem")
    await session.commit()

    first = await _ingest(session, datasource, _envelope(table))
    second = await _ingest(session, datasource, _envelope(table))

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.incident_opened is False
    assert second.signal.id == first.signal.id

    # Exactly one signal and one incident; the incident was not bumped a 2nd time.
    assert await session.scalar(select(func.count()).select_from(ExternalQualitySignal)) == 1
    incidents = (await session.scalars(select(DataQualityIncident))).all()
    assert len(incidents) == 1
    assert incidents[0].occurrence_count == 1


async def test_reingest_at_a_new_observed_at_reopens_the_same_incident(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_reopen")
    await session.commit()

    await _ingest(session, datasource, _envelope(table))
    # Same monitor, later observation -> new signal, same incident, occurrence bumped.
    later = _envelope(table, observed_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
    result = await _ingest(session, datasource, later)

    assert result.deduplicated is False
    assert await session.scalar(select(func.count()).select_from(ExternalQualitySignal)) == 2
    incidents = (await session.scalars(select(DataQualityIncident))).all()
    assert len(incidents) == 1
    assert incidents[0].occurrence_count == 2


# ---------------------------------------------------------------------------
# 4. RESOLVED signal auto-resolves the matching incident
# ---------------------------------------------------------------------------


async def test_resolved_signal_resolves_open_incident(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_resolve")
    await session.commit()

    await _ingest(session, datasource, _envelope(table))
    resolve = _envelope(
        table,
        signal_status="RESOLVED",
        observed_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
        summary="Monitor recovered.",
    )
    result = await _ingest(session, datasource, resolve)

    assert result.incident_resolved is True
    incident = await session.get(DataQualityIncident, result.signal.incident_id)
    assert incident.status == "RESOLVED"
    assert incident.resolved_by == "external-detector:MONTE_CARLO"


# ---------------------------------------------------------------------------
# 5. audit + event in the ingest transaction (INV-7)
# ---------------------------------------------------------------------------


async def test_ingest_writes_an_audit_and_event(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_audit")
    await session.commit()

    result = await _ingest(session, datasource, _envelope(table))

    audit = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "data_quality.external_signal.ingest")
        )
    ).all()
    assert len(audit) == 1
    assert audit[0].organization_id == datasource.organization_id
    assert audit[0].resource_id == str(result.signal.id)

    event = (
        await session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "data_quality.external_signal.ingested.v1"
            )
        )
    ).all()
    assert len(event) == 1
    assert event[0].payload["detector_vendor"] == "MONTE_CARLO"


# ---------------------------------------------------------------------------
# 6. tenant isolation (INV-5) and RBAC (AU-7)
# ---------------------------------------------------------------------------


async def test_cross_tenant_is_denied(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_secure")
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await ingest_external_quality_signal(
            datasource.id,
            _envelope(table),
            context=security_context(
                organization_id=uuid4(), roles=frozenset({"DataSteward"})
            ),
            session=session,
        )
    assert exc_info.value.status_code == 403
    assert "cross-organization" in str(exc_info.value.detail)


async def test_wrong_role_is_denied(session) -> None:
    # The role gate FastAPI actually wired onto the ingest route rejects a Viewer.
    route = next(
        r
        for r in iter_api_routes()
        if r.path.endswith("/quality/external-signals") and "POST" in r.methods
    )
    gate = require_roles_gate(route)
    assert gate is not None
    call, allowed = gate
    assert "Viewer" not in allowed
    with pytest.raises(HTTPException) as exc_info:
        await call(context=security_context(organization_id=uuid4(), roles=frozenset({"Viewer"})))
    assert exc_info.value.status_code == 403


async def test_signal_targeting_a_foreign_table_is_422(session) -> None:
    datasource = await _seed_datasource(session)
    other = await _seed_datasource(session)
    foreign_table = await _seed_table(session, other, name="t_foreign")
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await ingest_external_quality_signal(
            datasource.id,
            _envelope(foreign_table),
            context=_context(datasource),
            session=session,
        )
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# 7. INV-6 -- no raw source values persisted, only metadata/refs
# ---------------------------------------------------------------------------


async def test_nested_details_rejected_to_keep_control_plane_value_free(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_valuefree")
    await session.commit()

    # A nested object/array in details is where raw rows would hide -- rejected at
    # the envelope boundary before anything is persisted.
    with pytest.raises(ValueError):
        _envelope(table, details={"sampled_rows": [{"ssn": "123-45-6789"}]})


async def test_persisted_details_are_only_the_declared_metadata(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="t_meta")
    await session.commit()

    result = await _ingest(
        session,
        datasource,
        _envelope(table, details={"monitor_name": "orders freshness", "breach_count": 3}),
    )
    stored = await session.get(ExternalQualitySignal, result.signal.id)
    assert stored.details == {"monitor_name": "orders freshness", "breach_count": 3}
    # Flat scalar metadata only -- no nested containers ever reached the column.
    assert all(not isinstance(v, dict | list) for v in stored.details.values())
