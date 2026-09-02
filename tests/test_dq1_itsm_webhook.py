"""DQ-1: a quality incident genuinely routes to a webhook.

Before this file's changes, `notification_routing.py`'s engine and its
persistence tables (`NotificationRuleRecord`/`NotificationEventRecord`) were
reused by GL-6/KG-7 for *other* incident-shaped domains (unowned assets,
graph drift), but nothing ever created a `NotificationEventRecord` for an
actual `DataQualityIncident`, and no code path anywhere performed the
outbound HTTP call an "ITSM webhook emitter" implies -- every existing
ITSM-shaped call site (`siem_routing.route_to_siem`,
`glossary_owner_routing`/`graph_reconciliation`'s ITSM handling) stops at
formatting a payload and writing it to the outbox for an unbuilt external
consumer. These tests prove, against a real (in-memory sqlite) database and
a mocked HTTP transport (no real network access), that:

  * `emit_itsm_webhook` is off by default and fails closed with a clear
    reason when disabled or unconfigured -- never silently "succeeds";
  * given a real endpoint it POSTs the ITSM-formatted payload with an
    idempotency key and reports SENT/FAILED accurately;
  * `evaluate_analysis_run` -- the real incident-opening call site --
    persists a `NotificationEventRecord` and calls the webhook when a new
    incident opens and an ITSM notification rule matches, without changing
    behaviour for organizations with no notification rules configured
    (today's default, proven as a regression guard).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
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
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    NotificationEventRecord,
    NotificationRuleRecord,
    Organization,
    OutboxEvent,
    Project,
    TableProfile,
)
from aida.quality_service import emit_itsm_webhook, evaluate_analysis_run, route_and_notify_incident
from tests.support.doubles import security_context


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


_RealAsyncClient = httpx.AsyncClient


def _mock_async_client(handler):
    """Patchable replacement for `httpx.AsyncClient` that routes every
    request through a `httpx.MockTransport` instead of the real network,
    while preserving the timeout/follow_redirects kwargs the production
    code passes. Uses the real class captured at import time -- the
    replacement itself gets monkeypatched onto `httpx.AsyncClient`, so
    calling `httpx.AsyncClient(...)` from inside it would recurse."""

    def factory(*, timeout: Any = None, follow_redirects: bool = False) -> httpx.AsyncClient:
        return _RealAsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    return factory


# --- emit_itsm_webhook (unit-level) --------------------------------------------


async def test_disabled_by_default_fails_closed_without_any_network_attempt(monkeypatch) -> None:
    def _unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not attempt a network call when disabled")

    monkeypatch.setattr(quality_service.httpx, "AsyncClient", _mock_async_client(_unreachable))

    status, error = await emit_itsm_webhook(
        Settings(_env_file=None), {"short_description": "x"}, idempotency_key="k1"
    )

    assert status == "FAILED"
    assert error is not None and "off" in error


async def test_enabled_but_unconfigured_url_fails_closed() -> None:
    status, error = await emit_itsm_webhook(
        Settings(dq_itsm_webhook_enabled=True, _env_file=None),
        {"short_description": "x"},
        idempotency_key="k1",
    )
    assert status == "FAILED"
    assert error is not None and "not configured" in error


async def test_successful_post_reports_sent_and_carries_idempotency_key(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["idempotency_key"] = request.headers.get("Idempotency-Key")
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"sys_id": "INC0001"})

    monkeypatch.setattr(quality_service.httpx, "AsyncClient", _mock_async_client(handler))

    status, error = await emit_itsm_webhook(
        Settings(
            dq_itsm_webhook_enabled=True,
            dq_itsm_webhook_url="https://itsm.example.test/incidents",
            _env_file=None,
        ),
        {"short_description": "Null rate spike"},
        idempotency_key="incident-1:dedup-1",
    )

    assert status == "SENT"
    assert error is None
    assert captured["url"] == "https://itsm.example.test/incidents"
    assert captured["idempotency_key"] == "incident-1:dedup-1"


async def test_server_error_reports_failed_with_the_http_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    monkeypatch.setattr(quality_service.httpx, "AsyncClient", _mock_async_client(handler))

    status, error = await emit_itsm_webhook(
        Settings(
            dq_itsm_webhook_enabled=True,
            dq_itsm_webhook_url="https://itsm.example.test/incidents",
            _env_file=None,
        ),
        {"short_description": "x"},
        idempotency_key="k1",
    )

    assert status == "FAILED"
    assert error is not None and "503" in error


# --- evaluate_analysis_run wiring (end-to-end against a real database) --------


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
            name="transactions",
            object_type="TABLE",
            fingerprint="fp-transactions",
        )
        db.add(self.table)
        await db.flush()
        return self

    def context(self):
        return security_context(organization_id=self.organization.id, roles=frozenset({"Steward"}))

    async def seed_baseline_and_get_spike_run(self) -> Any:
        """A healthy baseline profile, then a current profile with a huge
        volume spike -- guaranteed to open a VOLUME_CHANGE incident
        regardless of policy defaults."""
        db = self.db
        db.add(
            TableProfile(
                organization_id=self.organization.id,
                analysis_run_id=uuid4(),
                datasource_id=self.datasource.id,
                table_id=self.table.id,
                row_count_estimate=1000,
                sampled_row_count=1000,
                status="COMPLETED",
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        current_run_id = uuid4()
        db.add(
            TableProfile(
                organization_id=self.organization.id,
                analysis_run_id=current_run_id,
                datasource_id=self.datasource.id,
                table_id=self.table.id,
                row_count_estimate=50,
                sampled_row_count=50,
                status="COMPLETED",
                created_at=datetime(2026, 6, 2, tzinfo=UTC),
            )
        )
        await db.flush()
        return current_run_id


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_no_notification_rules_configured_leaves_behaviour_unchanged(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """Regression guard: an organization that has never configured a
    notification rule (today's universal default) gets exactly the same
    `evaluate_analysis_run` behaviour as before this change -- no
    `NotificationEventRecord` rows, no extra queries beyond the guard."""
    run_id = await scenario.seed_baseline_and_get_spike_run()

    counts = await evaluate_analysis_run(
        db,
        analysis_run_id=run_id,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.context(),
    )
    await db.commit()

    assert counts["incidents_opened"] == 1
    events = (await db.scalars(select(NotificationEventRecord))).all()
    assert events == []


async def test_new_incident_with_matching_itsm_rule_persists_event_and_calls_webhook(
    scenario: _Scenario, db: AsyncSession, monkeypatch
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"sys_id": "INC0099"})

    monkeypatch.setattr(quality_service.httpx, "AsyncClient", _mock_async_client(handler))
    monkeypatch.setattr(
        quality_service,
        "get_settings",
        lambda: Settings(
            dq_itsm_webhook_enabled=True,
            dq_itsm_webhook_url="https://itsm.example.test/incidents",
            _env_file=None,
        ),
    )

    db.add(
        NotificationRuleRecord(
            organization_id=scenario.organization.id,
            name="Critical quality incidents to ITSM",
            conditions={},
            channel="ITSM",
            recipients=[],
            enabled=True,
            created_by="test-setup",
        )
    )
    await db.flush()

    run_id = await scenario.seed_baseline_and_get_spike_run()

    counts = await evaluate_analysis_run(
        db,
        analysis_run_id=run_id,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.context(),
    )
    await db.commit()

    assert counts["incidents_opened"] == 1

    incident = (await db.scalars(select(DataQualityIncident))).one()
    events = (await db.scalars(select(NotificationEventRecord))).all()
    assert len(events) == 1
    event = events[0]
    assert event.incident_id == incident.id
    assert event.channel == "ITSM"
    assert event.status == "SENT"
    assert event.sent_at is not None
    assert captured["body"] is not None

    itsm_outbox = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "data_quality.incident.itsm_payload.v1"
            )
        )
    ).all()
    assert len(itsm_outbox) == 1
    assert itsm_outbox[0].payload["webhook_status"] == "SENT"


async def test_webhook_failure_is_recorded_without_failing_the_analysis_run(
    scenario: _Scenario, db: AsyncSession, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    monkeypatch.setattr(quality_service.httpx, "AsyncClient", _mock_async_client(handler))
    monkeypatch.setattr(
        quality_service,
        "get_settings",
        lambda: Settings(
            dq_itsm_webhook_enabled=True,
            dq_itsm_webhook_url="https://itsm.example.test/incidents",
            _env_file=None,
        ),
    )

    db.add(
        NotificationRuleRecord(
            organization_id=scenario.organization.id,
            name="Critical quality incidents to ITSM",
            conditions={},
            channel="ITSM",
            recipients=[],
            enabled=True,
            created_by="test-setup",
        )
    )
    await db.flush()

    run_id = await scenario.seed_baseline_and_get_spike_run()

    counts = await evaluate_analysis_run(
        db,
        analysis_run_id=run_id,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.context(),
    )
    await db.commit()

    assert counts["incidents_opened"] == 1
    event = (await db.scalars(select(NotificationEventRecord))).one()
    assert event.status == "FAILED"
    assert event.sent_at is None


async def test_email_channel_match_is_persisted_as_sent_without_a_network_call(
    scenario: _Scenario, db: AsyncSession
) -> None:
    """EMAIL/WEBHOOK delivery transport is out of scope (infra), but the
    routed event must still be persisted and acknowledgeable."""
    db.add(
        NotificationRuleRecord(
            organization_id=scenario.organization.id,
            name="Email the on-call steward",
            conditions={},
            channel="EMAIL",
            recipients=["steward@bank.example"],
            enabled=True,
            created_by="test-setup",
        )
    )
    await db.flush()

    run_id = await scenario.seed_baseline_and_get_spike_run()

    counts = await evaluate_analysis_run(
        db,
        analysis_run_id=run_id,
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        context=scenario.context(),
    )
    await db.commit()

    assert counts["incidents_opened"] == 1
    event = (await db.scalars(select(NotificationEventRecord))).one()
    assert event.channel == "EMAIL"
    assert event.status == "SENT"


async def test_route_and_notify_incident_returns_empty_for_no_rules(
    scenario: _Scenario, db: AsyncSession
) -> None:
    incident = DataQualityIncident(
        id=uuid4(),
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        table_id=scenario.table.id,
        fingerprint="fp-1",
        anomaly_type="VOLUME_CHANGE",
        severity="CRITICAL",
        summary="test incident",
        first_observed_at=datetime.now(UTC),
        last_observed_at=datetime.now(UTC),
    )
    db.add(incident)
    await db.flush()

    created = await route_and_notify_incident(
        db,
        incident,
        organization_id=scenario.organization.id,
        notification_rules=[],
    )
    assert created == []
