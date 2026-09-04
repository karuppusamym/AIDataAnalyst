"""CT-1: catalog bulk actions (tag, classify, own, certify) against a real
database.

`tests/test_catalog_bulk_actions.py` covers each action's single-item
precondition logic (`apply_*_item`) as pure functions, without a database.
This file proves what only a real database can prove, following the same
real-engine pattern PG-3's own bulk endpoint test uses
(`tests/test_bulk_governance_decisions.py`) and CT-2's catalog-pagination
tests (`tests/test_catalog_pagination.py`), rather than a hand-simulated
session:

  1. Partial success at real scale -- a full 500-item batch (the request's
     own cap, `CATALOG_BULK_ACTION_MAX_ITEMS`) with a scattered mix of
     failing and succeeding subjects -- persists every succeeded item and
     reports every failed one correctly, with nothing silently dropped and
     no failure blocking the rest. (Filter-mode selection only ever matches
     currently-ACTIVE rows by construction -- see `_resolve_bulk_table_subjects`
     -- so it structurally cannot produce a "not found"/"not ACTIVE" failure;
     explicit selection is what this proves, at the request's actual ceiling.)
  2. The filter-selection cap: matching thousands of candidate rows still
     caps the *processed* batch at 500 with `truncated=True` in the run's
     recorded parameters, and the database ends up with exactly 500 new
     rows, never more.
  3. SAVEPOINT isolation actually contains a failure discovered only when the
     database itself rejects a write (a real `IntegrityError`, not a mocked
     one): the in-memory mutation that one item's dispatch had already made
     (superseding a prior certification) is rolled back with it, while every
     sibling item in the same request still commits.

The bulk endpoints return the `CatalogBulkActionRun` ORM row directly (FastAPI
serializes it against `response_model=CatalogBulkActionRunRead` only on the
HTTP path); calling them in-process like this, `run.results` is therefore the
raw `list[dict]` JSON column (`{"subject_id", "status", "reason"}`), not a
Pydantic model, and `truncated` lives in `run.parameters["selection_truncated"]`
rather than as a top-level attribute -- both exactly as `_persist_catalog_bulk_action_run`
builds them.
"""

import itertools
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from atlas.modules.catalog.router import (
    bulk_assign_ownership,
    bulk_certify_tables,
    bulk_classify_columns,
    bulk_tag_tables,
)
from aida.catalog_bulk_actions import CATALOG_BULK_ACTION_MAX_ITEMS
from aida.db import Base
from aida.models import (
    AssetCertification,
    AssetTag,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    OwnershipAssignment,
    Project,
)
from aida.schemas import (
    CatalogBulkCertifyRequest,
    CatalogBulkClassifyRequest,
    CatalogBulkOwnRequest,
    CatalogBulkSelectionFilter,
    CatalogBulkTagRequest,
)
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio

# `AuditEvent.id` is a `BigInteger` autoincrement primary key that relies, in
# production, on Postgres's own identity/sequence generation. sqlite only
# auto-populates a bare `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger`
# compiles to `BIGINT`, which sqlite does not treat as that alias -- so an
# in-memory sqlite session (as every test below uses) leaves `id` NULL and
# violates the NOT NULL constraint on insert. Assign ids by hand for this
# test module's sqlite engine only; nothing about the production model
# changes. (Same workaround as tests/test_bulk_governance_decisions.py.)
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


async def _seed_datasource(session: AsyncSession) -> tuple[DataSource, MetadataSchema]:
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
        name="primary",
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
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    return datasource, schema


async def _seed_tables(
    session: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    count: int,
    status: str = "ACTIVE",
    name_prefix: str = "table",
) -> list[MetadataTable]:
    tables = [
        MetadataTable(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=f"{name_prefix}_{i:06d}",
            object_type="BASE_TABLE",
            status=status,
            fingerprint="fp",
        )
        for i in range(count)
    ]
    session.add_all(tables)
    await session.flush()
    return tables


async def _seed_columns(
    session: AsyncSession,
    table: MetadataTable,
    *,
    count: int,
    status: str = "ACTIVE",
    name_prefix: str = "col",
) -> list[MetadataColumn]:
    columns = [
        MetadataColumn(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            name=f"{name_prefix}_{i:06d}",
            ordinal_position=i,
            physical_type="VARCHAR",
            nullable=True,
            status=status,
            fingerprint="fp",
        )
        for i in range(count)
    ]
    session.add_all(columns)
    await session.flush()
    return columns


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"DataSteward"}),
    )


def _platform_admin_context(datasource: DataSource) -> SecurityContext:
    """GV-2 / P0-02: `DataSteward` is now in the default
    `bulk_governance_roles_requiring_review` list -- a request under that
    role is (correctly) routed through `BulkStewardshipOperation` +
    `GovernanceReview` regardless of count. The CT-1 tests below prove the
    direct-write batch mechanics (cap-at-500, SAVEPOINT isolation) that
    still need to run on the direct path, and are executed under
    `PlatformAdmin` -- a role deliberately absent from the review-required
    list so admins can direct-write within the count threshold.
    """
    return SecurityContext(
        principal_id="admin@example.com",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


def _high_threshold_settings() -> "Settings":
    """Settings override for the two CT-1 tests that exercise 40-item and
    500-item direct-write batches. Default threshold is 10 (P0-02), so a
    truncation-cap test needs a threshold well above the cap; the empty
    review-required-roles list is defensive so this fixture also stays
    stable if a caller here ever ran under `DataSteward` again.
    """
    from atlas.platform.config import Settings

    return Settings(
        environment="test",
        bulk_governance_threshold=10_000,
        bulk_governance_roles_requiring_review=[],
    )


# ---------------------------------------------------------------------------
# Partial success at real scale: a full 500-item explicit-selection batch --
# CATALOG_BULK_ACTION_MAX_ITEMS, the request's own ceiling, not 2-3 rows --
# with a scattered mix of failing and succeeding subjects.
# ---------------------------------------------------------------------------


async def test_bulk_tag_reports_partial_success_at_full_batch_scale(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    active_tables = await _seed_tables(
        session, datasource, schema, count=470, status="ACTIVE", name_prefix="active"
    )
    deprecated_tables = await _seed_tables(
        session, datasource, schema, count=25, status="DEPRECATED", name_prefix="deprecated"
    )
    missing_ids = [uuid4() for _ in range(5)]
    await session.commit()

    table_ids = (
        [t.id for t in active_tables] + [t.id for t in deprecated_tables] + missing_ids
    )
    assert len(table_ids) == CATALOG_BULK_ACTION_MAX_ITEMS

    result = await bulk_tag_tables(
        datasource.organization_id,
        CatalogBulkTagRequest(table_ids=table_ids, tag_key="gold-tier", tag_value="true"),
        context=_context(datasource),
        session=session,
    )

    assert result.selection_mode == "EXPLICIT"
    assert result.requested_count == CATALOG_BULK_ACTION_MAX_ITEMS
    assert result.succeeded_count == 470
    assert result.failed_count == 30
    by_id = {item["subject_id"]: item for item in result.results}
    for table in active_tables:
        assert by_id[str(table.id)]["status"] == "SUCCEEDED"
    for table in deprecated_tables:
        assert by_id[str(table.id)]["status"] == "FAILED"
        assert "DEPRECATED" in (by_id[str(table.id)]["reason"] or "")
    for missing_id in missing_ids:
        assert by_id[str(missing_id)]["status"] == "FAILED"
        assert "not found" in (by_id[str(missing_id)]["reason"] or "")

    # The database reflects exactly the succeeded count -- no failure left a
    # partial row behind, and no success went unpersisted.
    persisted = await session.scalar(select(func.count()).select_from(AssetTag))
    assert persisted == 470


async def test_bulk_classify_reports_partial_success_at_full_batch_scale(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=1)
    table = tables[0]
    active_columns = await _seed_columns(
        session, table, count=480, status="ACTIVE", name_prefix="active"
    )
    deprecated_columns = await _seed_columns(
        session, table, count=20, status="DEPRECATED", name_prefix="deprecated"
    )
    await session.commit()

    column_ids = [c.id for c in active_columns] + [c.id for c in deprecated_columns]
    assert len(column_ids) == CATALOG_BULK_ACTION_MAX_ITEMS

    result = await bulk_classify_columns(
        datasource.organization_id,
        CatalogBulkClassifyRequest(column_ids=column_ids, classification="PII"),
        context=_context(datasource),
        session=session,
    )

    assert result.selection_mode == "EXPLICIT"
    assert result.succeeded_count == 480
    assert result.failed_count == 20
    by_id = {item["subject_id"]: item for item in result.results}
    for column in deprecated_columns:
        assert by_id[str(column.id)]["status"] == "FAILED"
        assert "column status" in (by_id[str(column.id)]["reason"] or "")

    classified = await session.scalar(
        select(func.count())
        .select_from(MetadataColumn)
        .where(MetadataColumn.classification == "PII")
    )
    assert classified == 480


# ---------------------------------------------------------------------------
# The cap: matching thousands of candidate rows still caps the processed
# batch at CATALOG_BULK_ACTION_MAX_ITEMS with truncated=True, never silently
# more -- and the database ends up with exactly that many new rows.
# ---------------------------------------------------------------------------


async def test_bulk_own_by_filter_caps_at_500_and_reports_truncation(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    # 5,000 candidate tables, all ACTIVE and all matching -- proving the cap
    # binds on selection size itself, not as a side effect of some items
    # failing a precondition.
    await _seed_tables(session, datasource, schema, count=5000)
    await session.commit()

    result = await bulk_assign_ownership(
        datasource.organization_id,
        CatalogBulkOwnRequest(
            filter=CatalogBulkSelectionFilter(
                datasource_id=datasource.id, match_field="TABLE_NAME", match_pattern="table_*"
            ),
            owner_type="GROUP",
            owner_principal="retail-data-stewards",
        ),
        context=_platform_admin_context(datasource),
        session=session,
        settings=_high_threshold_settings(),
    )

    assert result.selection_mode == "FILTER"
    assert result.parameters["selection_truncated"] is True
    assert result.requested_count == CATALOG_BULK_ACTION_MAX_ITEMS
    assert result.succeeded_count == CATALOG_BULK_ACTION_MAX_ITEMS
    assert result.failed_count == 0

    persisted = await session.scalar(select(func.count()).select_from(OwnershipAssignment))
    assert persisted == CATALOG_BULK_ACTION_MAX_ITEMS


async def test_bulk_own_by_filter_matching_fewer_than_the_cap_is_not_truncated(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=40)
    await session.commit()

    result = await bulk_assign_ownership(
        datasource.organization_id,
        CatalogBulkOwnRequest(
            filter=CatalogBulkSelectionFilter(
                datasource_id=datasource.id, match_field="TABLE_NAME", match_pattern="table_*"
            ),
            owner_type="INDIVIDUAL",
            owner_principal="jane.steward",
        ),
        context=_platform_admin_context(datasource),
        session=session,
        settings=_high_threshold_settings(),
    )
    assert result.parameters["selection_truncated"] is False
    assert result.requested_count == 40
    assert result.succeeded_count == 40


# ---------------------------------------------------------------------------
# SAVEPOINT isolation: a failure the database itself raises at flush time
# never leaks a partial write, and never blocks sibling items in the batch.
# ---------------------------------------------------------------------------


async def test_bulk_certify_isolates_a_failure_within_one_items_dispatch(
    session: AsyncSession,
) -> None:
    """One item's dispatch (`apply_certify_item`) supersedes a prior
    certification in memory *before* the new certification row is flushed.
    Prove that when the flush itself fails -- a real `IntegrityError` raised
    by the database, not a mocked one -- the per-item SAVEPOINT rolls back
    that supersede too, and every sibling item in the same request still
    commits normally.

    This sandbox's sqlite fixture is a single connection/session, so a
    genuine concurrent-writer race can't be reproduced deterministically
    here. Instead this trips a real, table-defined CHECK constraint
    (`ck_asset_certification_column_consistency`, which forbids an
    ``asset_type="TABLE"`` row from carrying a non-null ``column_id``) on
    exactly one item's new row via a `before_insert` listener -- simulating
    "something else populated column_id on a table-level certification" and
    letting the real database reject it, exactly the class of failure the
    per-item SAVEPOINT exists to contain.
    """
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=3)
    poisoned_table, ok_table_a, ok_table_b = tables
    now = datetime.now(UTC)
    prior = AssetCertification(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=poisoned_table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Prior quarter certification.",
        certified_by="old-owner@example.com",
        expires_at=now + timedelta(days=1),
    )
    session.add(prior)
    await session.commit()

    def _poison_one_row(mapper: object, connection: object, target: AssetCertification) -> None:
        if target.table_id == poisoned_table.id:
            target.column_id = uuid4()

    event.listen(AssetCertification, "before_insert", _poison_one_row)
    try:
        result = await bulk_certify_tables(
            datasource.organization_id,
            CatalogBulkCertifyRequest(
                table_ids=[poisoned_table.id, ok_table_a.id, ok_table_b.id],
                rationale="Certified against the approved quarterly data contract.",
                expires_at=now + timedelta(days=90),
            ),
            context=_platform_admin_context(datasource),
            session=session,
        )
    finally:
        event.remove(AssetCertification, "before_insert", _poison_one_row)

    assert result.succeeded_count == 2
    assert result.failed_count == 1
    by_id = {item["subject_id"]: item for item in result.results}
    assert by_id[str(poisoned_table.id)]["status"] == "FAILED"
    assert "constraint" in (by_id[str(poisoned_table.id)]["reason"] or "")
    assert by_id[str(ok_table_a.id)]["status"] == "SUCCEEDED"
    assert by_id[str(ok_table_b.id)]["status"] == "SUCCEEDED"

    # The prior certification's supersede was rolled back with the rest of
    # that item's SAVEPOINT -- it must still read ACTIVE, not stuck
    # SUPERSEDED with no replacement ever having landed.
    await session.refresh(prior)
    assert prior.status == "ACTIVE"

    # No new certification row exists for the poisoned table at all -- only
    # the original `prior`, unchanged.
    poisoned_certs = await session.scalar(
        select(func.count())
        .select_from(AssetCertification)
        .where(AssetCertification.table_id == poisoned_table.id)
    )
    assert poisoned_certs == 1

    # The two sibling items committed normally, unaffected by the failure --
    # the outer session, and its final commit, were never aborted by it.
    for ok_table in (ok_table_a, ok_table_b):
        active_count = await session.scalar(
            select(func.count())
            .select_from(AssetCertification)
            .where(
                AssetCertification.table_id == ok_table.id,
                AssetCertification.status == "ACTIVE",
            )
        )
        assert active_count == 1
