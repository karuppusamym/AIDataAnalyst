"""DQ-2: maker-checker approval activates freshness watermark contracts.

`freshness.py`'s own docstring says a config only activates freshness once
approved -- `evaluate_freshness` treats anything but ACTIVE as
AWAITING_APPROVAL/NOT_CONFIGURED. Until this file's endpoint
(`quality_api.approve_freshness_config`) existed, nothing in the platform
ever set a config's status away from PENDING_APPROVAL: `upsert_freshness_config`
always creates/resets it there, and no other code path wrote `approved_by`/
`approved_at`. These tests prove, against a real (in-memory sqlite) database,
that: (1) a freshly-upserted config reports AWAITING_APPROVAL, never FRESH,
even with a fresh watermark observation on file; (2) the same principal who
authored the config cannot approve it (maker-checker); (3) a different
principal approving flips it to ACTIVE and `get_freshness_status` then
genuinely evaluates FRESH/STALE from the real watermark; (4) editing an
already-approved config resets it back to PENDING_APPROVAL (regression
guard on the existing reset-on-update behavior in `upsert_freshness_config`).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    FreshnessObservation,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.quality_api import approve_freshness_config, get_freshness_status, upsert_freshness_config
from aida.schemas import FreshnessConfigUpsert
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


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_freshly_upserted_config_awaits_approval_even_with_a_fresh_watermark(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=60),
        context=scenario.maker_context(),
        session=db,
    )
    db.add(
        FreshnessObservation(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.table.id,
            watermark_value=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await db.commit()

    status = await get_freshness_status(
        scenario.datasource.id,
        scenario.table.id,
        context=scenario.checker_context(),
        session=db,
    )

    assert status.status == "AWAITING_APPROVAL"


async def test_the_configs_own_author_cannot_approve_it(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=60),
        context=scenario.maker_context(),
        session=db,
    )
    await db.commit()

    with pytest.raises(HTTPException) as excinfo:
        await approve_freshness_config(
            scenario.datasource.id,
            scenario.table.id,
            context=scenario.maker_context(),
            session=db,
        )
    assert excinfo.value.status_code == 403


async def test_a_different_principal_approving_activates_freshness_evaluation(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=60),
        context=scenario.maker_context(),
        session=db,
    )
    await db.commit()

    approved = await approve_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        context=scenario.checker_context(),
        session=db,
    )
    assert approved.status == "ACTIVE"
    assert approved.approved_by == "steward-checker"
    assert approved.approved_at is not None

    fresh_watermark = datetime.now(UTC) - timedelta(minutes=10)
    db.add(
        FreshnessObservation(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.table.id,
            watermark_value=fresh_watermark,
        )
    )
    await db.commit()

    status = await get_freshness_status(
        scenario.datasource.id,
        scenario.table.id,
        context=scenario.checker_context(),
        session=db,
    )
    assert status.status == "FRESH"
    assert status.evidence["evaluation_source"] == "data_watermark"

    stale_watermark = datetime.now(UTC) - timedelta(minutes=120)
    db.add(
        FreshnessObservation(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.table.id,
            watermark_value=stale_watermark,
        )
    )
    await db.commit()

    stale_status = await get_freshness_status(
        scenario.datasource.id,
        scenario.table.id,
        context=scenario.checker_context(),
        session=db,
    )
    assert stale_status.status == "STALE"


async def test_approving_twice_fails_with_conflict(scenario: _Scenario, db: AsyncSession) -> None:
    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=60),
        context=scenario.maker_context(),
        session=db,
    )
    await db.commit()
    await approve_freshness_config(
        scenario.datasource.id, scenario.table.id, context=scenario.checker_context(), session=db
    )
    await db.commit()

    with pytest.raises(HTTPException) as excinfo:
        await approve_freshness_config(
            scenario.datasource.id,
            scenario.table.id,
            context=scenario.checker_context(),
            session=db,
        )
    assert excinfo.value.status_code == 409


async def test_editing_an_active_config_resets_it_to_pending_approval(
    scenario: _Scenario, db: AsyncSession
) -> None:
    await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=60),
        context=scenario.maker_context(),
        session=db,
    )
    await db.commit()
    await approve_freshness_config(
        scenario.datasource.id, scenario.table.id, context=scenario.checker_context(), session=db
    )
    await db.commit()

    updated = await upsert_freshness_config(
        scenario.datasource.id,
        scenario.table.id,
        FreshnessConfigUpsert(watermark_column="updated_at", threshold_minutes=120),
        context=scenario.maker_context(),
        session=db,
    )
    assert updated.status == "PENDING_APPROVAL"
    assert updated.approved_by is None
