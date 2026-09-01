"""IN-1: bulk source onboarding -- 200 datasources registered in one operation.

PostgreSQL is not reachable in this sandbox (see `tests/test_catalog_pagination.py`),
so these tests run the real ORM models and the real `bulk_onboard_datasources` /
`create_datasource` endpoint bodies against an in-memory SQLite database via
aiosqlite. That is enough to exercise the part of this feature that actually
matters: the `DataSource.project_id + name` UNIQUE constraint firing mid-batch
and the per-item SAVEPOINT (`session.begin_nested()`) isolating that failure
from the rest of the batch, which a session double could not prove.

Every test here calls the real endpoint functions directly (no HTTP layer),
matching this module's existing pattern for datasource fixtures and
`test_catalog_pagination.py`'s pattern for a real-engine integration test.
"""

import itertools
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.api import bulk_onboard_datasources, create_datasource
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    OutboxEvent,
    Project,
)
from aida.schemas import (
    DATASOURCE_BULK_ONBOARD_MAX_ITEMS,
    DataSourceBulkOnboardRequest,
    DataSourceCreate,
)
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings()

# `AuditEvent.id` is a `BigInteger` autoincrement primary key, relying in
# production on Postgres's own identity/sequence generation. sqlite only
# auto-populates a bare `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger`
# compiles to `BIGINT`, which sqlite does not treat as that alias -- so an
# in-memory sqlite session (as used by every test below) leaves `id` NULL and
# violates the NOT NULL constraint on insert. Every `record_audit()` call in
# the endpoints under test hits this; assign ids by hand for this test
# module's sqlite engine only, same fix as
# `test_relationship_intelligence_review.py` -- nothing about the production
# model changes.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


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


async def _seed_project(session: AsyncSession) -> Project:
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
    session.add_all([org, lob, domain, project])
    await session.flush()
    return project


def _context(project: Project) -> SecurityContext:
    return SecurityContext(
        principal_id="platform-admin",
        principal_type="USER",
        organization_id=project.organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


def _spec(name: str, *, credential_reference: str = "env://AIDA_TEST_SECRET") -> DataSourceCreate:
    return DataSourceCreate(
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference=credential_reference,
    )


# ---------------------------------------------------------------------------
# Happy path: a full 200-item batch, item by item
# ---------------------------------------------------------------------------


async def test_bulk_onboard_registers_200_datasources_in_one_operation(session) -> None:
    project = await _seed_project(session)
    context = _context(project)
    specs = [_spec(f"source-{i:04d}") for i in range(DATASOURCE_BULK_ONBOARD_MAX_ITEMS)]
    assert len(specs) == 200

    result = await bulk_onboard_datasources(
        project.id,
        DataSourceBulkOnboardRequest(datasources=specs),
        context=context,
        session=session,
        settings=_SETTINGS,
    )

    assert result.requested_count == 200
    assert result.succeeded_count == 200
    assert result.failed_count == 0
    assert len(result.results) == 200
    for index, item in enumerate(result.results):
        assert item.index == index
        assert item.status == "SUCCEEDED"
        assert item.datasource_id is not None
        assert item.reason is None

    persisted = (
        await session.scalars(select(DataSource).where(DataSource.project_id == project.id))
    ).all()
    assert len(persisted) == 200
    assert {row.name for row in persisted} == {spec.name for spec in specs}
    assert all(row.status == "REGISTERED" for row in persisted)


async def test_bulk_onboard_at_the_cap_is_accepted() -> None:
    # Exactly at the cap must not be rejected by the request schema.
    specs = [_spec(f"source-{i:04d}") for i in range(DATASOURCE_BULK_ONBOARD_MAX_ITEMS)]
    request = DataSourceBulkOnboardRequest(datasources=specs)
    assert len(request.datasources) == 200


# ---------------------------------------------------------------------------
# Partial success: one bad item in the middle of a 200-item batch fails only
# that item, and every other item still succeeds (CT-1 / RL-6 precedent).
# ---------------------------------------------------------------------------


async def test_one_bad_credential_reference_in_the_middle_fails_only_that_item(session) -> None:
    project = await _seed_project(session)
    context = _context(project)
    specs = [_spec(f"source-{i:04d}") for i in range(DATASOURCE_BULK_ONBOARD_MAX_ITEMS)]
    bad_index = 46  # item #47 of 200, per the task's own example
    bad_name = "source-bad-cred"
    specs[bad_index] = DataSourceCreate(
        name=bad_name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        # not the configured secret provider ("env://") -- a raw connection
        # string, exactly what `_validate_datasource_create` rejects.
        credential_reference="postgresql://user:pass@host/db",
    )

    result = await bulk_onboard_datasources(
        project.id,
        DataSourceBulkOnboardRequest(datasources=specs),
        context=context,
        session=session,
        settings=_SETTINGS,
    )

    assert result.requested_count == 200
    assert result.succeeded_count == 199
    assert result.failed_count == 1
    failed_items = [item for item in result.results if item.status == "FAILED"]
    assert len(failed_items) == 1
    assert failed_items[0].index == bad_index
    assert failed_items[0].name == bad_name
    assert failed_items[0].datasource_id is None
    assert "credential_reference must use the configured secret provider" in (
        failed_items[0].reason or ""
    )
    # every other item, before and after the bad one, still succeeded
    for index, item in enumerate(result.results):
        if index == bad_index:
            continue
        assert item.status == "SUCCEEDED", f"item {index} unexpectedly failed: {item.reason}"

    persisted_names = set(
        await session.scalars(select(DataSource.name).where(DataSource.project_id == project.id))
    )
    assert bad_name not in persisted_names
    assert len(persisted_names) == 199


async def test_duplicate_name_mid_batch_fails_only_the_later_occurrence(session) -> None:
    project = await _seed_project(session)
    context = _context(project)
    specs = [_spec(f"source-{i:04d}") for i in range(50)]
    dup_index = 25
    specs[dup_index] = _spec("source-0003")  # collides with the already-listed item 3

    result = await bulk_onboard_datasources(
        project.id,
        DataSourceBulkOnboardRequest(datasources=specs),
        context=context,
        session=session,
        settings=_SETTINGS,
    )

    assert result.succeeded_count == 49
    assert result.failed_count == 1
    dup_result = result.results[dup_index]
    assert dup_result.status == "FAILED"
    assert dup_result.reason == "datasource name already exists in this project"
    # the earlier, original item 3 is untouched and still succeeded
    assert result.results[3].status == "SUCCEEDED"

    persisted = (
        await session.scalars(select(DataSource).where(DataSource.project_id == project.id))
    ).all()
    assert len(persisted) == 49


async def test_duplicate_name_against_a_pre_existing_datasource_fails_only_that_item(
    session,
) -> None:
    project = await _seed_project(session)
    context = _context(project)
    # register one datasource the ordinary, single-item way first
    await create_datasource(
        project.id,
        _spec("already-registered"),
        context=context,
        session=session,
        settings=_SETTINGS,
    )

    specs = [_spec(f"source-{i:04d}") for i in range(20)]
    specs[10] = _spec("already-registered")

    result = await bulk_onboard_datasources(
        project.id,
        DataSourceBulkOnboardRequest(datasources=specs),
        context=context,
        session=session,
        settings=_SETTINGS,
    )

    assert result.succeeded_count == 19
    assert result.failed_count == 1
    assert result.results[10].status == "FAILED"
    assert result.results[10].reason == "datasource name already exists in this project"


# ---------------------------------------------------------------------------
# Over-cap rejection: 201+ items is rejected outright, before the DB is
# touched -- never silently truncated to the first 200.
# ---------------------------------------------------------------------------


async def test_over_cap_request_is_rejected_before_touching_the_database(session) -> None:
    project = await _seed_project(session)
    too_many = [
        _spec(f"source-{i:04d}") for i in range(DATASOURCE_BULK_ONBOARD_MAX_ITEMS + 1)
    ]

    with pytest.raises(ValidationError, match="at most 200 items"):
        DataSourceBulkOnboardRequest(datasources=too_many)

    # the request never even reached the handler, so nothing was persisted
    persisted = (
        await session.scalars(select(DataSource).where(DataSource.project_id == project.id))
    ).all()
    assert persisted == []


async def test_empty_batch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DataSourceBulkOnboardRequest(datasources=[])


# ---------------------------------------------------------------------------
# Emitted events per item match the single-item `create_datasource` path
# exactly -- same audit action, resource_type, outcome shape, and outbox
# event_type/payload keys -- because both call the identical
# `_record_datasource_registration_events` helper.
# ---------------------------------------------------------------------------


async def test_bulk_item_audit_and_outbox_events_match_the_single_item_path(session) -> None:
    single_project = await _seed_project(session)
    single_context = _context(single_project)
    single_datasource = await create_datasource(
        single_project.id,
        _spec("solo-source"),
        context=single_context,
        session=session,
        settings=_SETTINGS,
    )
    await session.commit()

    single_audit = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "datasource.register",
                AuditEvent.resource_id == str(single_datasource.id),
            )
        )
    ).one()
    single_outbox = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == str(single_datasource.id))
        )
    ).one()

    bulk_project = await _seed_project(session)
    bulk_context = _context(bulk_project)
    bulk_result = await bulk_onboard_datasources(
        bulk_project.id,
        DataSourceBulkOnboardRequest(datasources=[_spec("solo-source")]),
        context=bulk_context,
        session=session,
        settings=_SETTINGS,
    )
    bulk_datasource_id = bulk_result.results[0].datasource_id
    assert bulk_datasource_id is not None

    bulk_item_audit = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "datasource.register",
                AuditEvent.resource_id == str(bulk_datasource_id),
            )
        )
    ).one()
    bulk_item_outbox = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == str(bulk_datasource_id))
        )
    ).one()

    # same action, resource_type, outcome, and detail keys per item -- the
    # bulk path is not a looser or differently-shaped audit trail.
    assert bulk_item_audit.action == single_audit.action == "datasource.register"
    assert bulk_item_audit.resource_type == single_audit.resource_type == "datasource"
    assert bulk_item_audit.outcome == single_audit.outcome == "SUCCESS"
    assert set(bulk_item_audit.details) == set(single_audit.details) == {
        "connector_type",
        "network_zone",
    }

    assert bulk_item_outbox.aggregate_type == single_outbox.aggregate_type == "datasource"
    assert bulk_item_outbox.event_type == single_outbox.event_type == "datasource.registered.v1"
    assert set(bulk_item_outbox.payload) == set(single_outbox.payload) == {
        "datasource_id",
        "project_id",
        "connector_type",
    }

    # the bulk request also writes one summary audit row for the whole batch,
    # on top of (not instead of) each item's own registration event.
    summary_audit = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "datasource.bulk_register")
        )
    ).one()
    assert summary_audit.outcome == "SUCCESS"
    assert summary_audit.details["succeeded_count"] == 1
    assert summary_audit.details["failed_count"] == 0
