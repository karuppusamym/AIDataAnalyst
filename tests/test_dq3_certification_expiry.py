"""DQ-3's fifth coupling row: a sustained-incident table loses certification.

`quality_coupling.should_expire_certification` was a real, unit-tested pure
function with zero call sites anywhere in `src/aida` -- nothing ever
transitioned an `AssetCertification` row because of a quality incident. This
proves, against a real (in-memory sqlite) database, that
`quality_service.evaluate_analysis_run` now does exactly that when
`quality_certification_expiry_enabled` is on (off by default -- proven here
too, as a regression guard identical in spirit to
`test_dq1_itsm_webhook.py::test_no_notification_rules_configured_leaves_behaviour_unchanged`):
the table's ACTIVE certification flips to EXPIRED (never deleted, never
backdated -- only `status` moves, the same single-field-write shape every
other certification transition in this codebase already uses for
"SUPERSEDED"), and both an audit row and an outbox event are recorded.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida import quality_service
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AssetCertification,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
    TableProfile,
)
from aida.quality_service import evaluate_analysis_run
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

    async def certify_table(self) -> AssetCertification:
        certification = AssetCertification(
            organization_id=self.organization.id,
            table_id=self.table.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Reviewed and certified for the quarterly close.",
            certified_by="steward@tenant.example",
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )
        self.db.add(certification)
        await self.db.flush()
        return certification

    async def seed_baseline_and_get_spike_run(self):
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


async def test_disabled_by_default_leaves_certification_untouched(
    scenario: _Scenario, db: AsyncSession
) -> None:
    certification = await scenario.certify_table()
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
    assert counts["certifications_expired"] == 0
    await db.refresh(certification)
    assert certification.status == "ACTIVE"


async def test_enabled_expires_certification_on_sustained_incidents_and_records_evidence(
    scenario: _Scenario, db: AsyncSession, monkeypatch
) -> None:
    certification = await scenario.certify_table()
    run_id = await scenario.seed_baseline_and_get_spike_run()
    # sustained_threshold=1 keeps the fixture to a single anomaly type
    # (VOLUME_CHANGE) -- the pure function's own threshold-boundary behavior
    # (>=3 by default, custom thresholds, ACKNOWLEDGED counting, RESOLVED not
    # counting) is already proven in tests/test_quality_coupling.py; this
    # file proves the wiring, not the threshold arithmetic again.
    monkeypatch.setattr(
        quality_service,
        "get_settings",
        lambda: Settings(
            quality_certification_expiry_enabled=True,
            quality_certification_sustained_threshold=1,
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

    assert counts["incidents_opened"] == 1
    assert counts["certifications_expired"] == 1

    await db.refresh(certification)
    assert certification.status == "EXPIRED"
    # Retained evidence: expiry never backdates or rewrites what the table
    # was originally certified as.
    assert certification.rationale == "Reviewed and certified for the quarterly close."
    assert certification.certified_by == "steward@tenant.example"

    audit_rows = (
        await db.scalars(
            select(AuditEvent).where(AuditEvent.action == "catalog.asset.certification_expired")
        )
    ).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].resource_id == str(certification.id)
    assert audit_rows[0].details["reason"] == "SUSTAINED_QUALITY_INCIDENTS"

    outbox_rows = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "catalog.asset.certification_expired.v1"
            )
        )
    ).all()
    assert len(outbox_rows) == 1
    assert outbox_rows[0].payload["certification_id"] == str(certification.id)


async def test_enabled_with_no_certification_is_a_no_op(
    scenario: _Scenario, db: AsyncSession, monkeypatch
) -> None:
    """No certification exists on the table at all -- the flag firing must
    not fabricate one or raise, just find nothing to expire."""
    run_id = await scenario.seed_baseline_and_get_spike_run()
    monkeypatch.setattr(
        quality_service,
        "get_settings",
        lambda: Settings(
            quality_certification_expiry_enabled=True,
            quality_certification_sustained_threshold=1,
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

    assert counts["incidents_opened"] == 1
    assert counts["certifications_expired"] == 0
