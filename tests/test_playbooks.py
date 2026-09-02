"""AT-1: saved, scheduled bulk-metadata playbook objects.

Uses the same real-sqlite-engine pattern as `test_catalog_bulk_actions_endpoints.py`
(rather than a hand-simulated session) since `resolve_playbook_matches` and
`evaluate_and_run_playbook` issue real queries against `MetadataTable`/
`MetadataColumn`, and `_auto_apply`/`_queue_for_review` need real
flush-generated ids on the rows they create.
"""

import itertools
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.main import app
from aida.models import (
    AssetCertification,
    AssetTag,
    AuditEvent,
    BulkStewardshipOperation,
    CatalogBulkActionRun,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataPlaybook,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.playbooks import evaluate_and_run_playbook, playbook_due, resolve_playbook_matches
from aida.playbooks_api import (
    PlaybookCreate,
    PlaybookUpdate,
    create_playbook,
    delete_playbook,
    get_playbook,
    list_playbooks,
    run_playbook_now,
    update_playbook,
)
from aida.security import SecurityContext

# Same sqlite-only AuditEvent.id workaround as test_catalog_bulk_actions_endpoints.py.
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
    name_prefix: str = "table",
) -> list[MetadataTable]:
    tables = [
        MetadataTable(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=f"{name_prefix}_{i:03d}",
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint="fp",
        )
        for i in range(count)
    ]
    session.add_all(tables)
    await session.flush()
    return tables


async def _seed_columns(
    session: AsyncSession, table: MetadataTable, *, count: int, name_prefix: str = "col"
) -> list[MetadataColumn]:
    columns = [
        MetadataColumn(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            name=f"{name_prefix}_{i:03d}",
            ordinal_position=i,
            physical_type="VARCHAR",
            nullable=True,
            status="ACTIVE",
            fingerprint="fp",
        )
        for i in range(count)
    ]
    session.add_all(columns)
    await session.flush()
    return columns


def _tag_playbook(
    datasource: DataSource, *, auto_apply_max_items: int = 500, match_pattern: str = "tagme_*"
) -> MetadataPlaybook:
    return MetadataPlaybook(
        id=uuid4(),
        organization_id=datasource.organization_id,
        name="tag-pii-candidates",
        action="TAG",
        datasource_id=datasource.id,
        match_field="TABLE_NAME",
        match_pattern=match_pattern,
        action_parameters={"tag_key": "pii-review", "tag_value": "pending"},
        schedule_interval_minutes=60,
        auto_apply_max_items=auto_apply_max_items,
        enabled=True,
        created_by="steward@example.com",
    )


def _context(datasource: DataSource, *, roles: frozenset[str] | None = None) -> SecurityContext:
    return SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=roles or frozenset({"DataSteward"}),
    )


# ---------------------------------------------------------------------------
# playbook_due
# ---------------------------------------------------------------------------


def test_playbook_due_when_never_run() -> None:
    assert playbook_due(None, datetime.now(UTC), 60) is True


def test_playbook_due_before_interval_elapsed() -> None:
    now = datetime.now(UTC)
    assert playbook_due(now - timedelta(minutes=30), now, 60) is False


def test_playbook_due_after_interval_elapsed() -> None:
    now = datetime.now(UTC)
    assert playbook_due(now - timedelta(minutes=61), now, 60) is True


# ---------------------------------------------------------------------------
# resolve_playbook_matches
# ---------------------------------------------------------------------------


async def test_resolve_playbook_matches_filters_by_table_name_pattern(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    matching = await _seed_tables(session, datasource, schema, count=3, name_prefix="tagme")
    await _seed_tables(session, datasource, schema, count=5, name_prefix="skip")
    playbook = _tag_playbook(datasource)

    matched_ids = await resolve_playbook_matches(session, playbook)

    assert set(matched_ids) == {table.id for table in matching}


async def test_resolve_playbook_matches_narrows_to_columns_for_classify(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=1, name_prefix="tagme")
    matching_columns = await _seed_columns(session, tables[0], count=2, name_prefix="ssn")
    await _seed_columns(session, tables[0], count=3, name_prefix="notes")
    playbook = MetadataPlaybook(
        id=uuid4(),
        organization_id=datasource.organization_id,
        name="classify-ssn",
        action="CLASSIFY",
        datasource_id=datasource.id,
        match_field="TABLE_NAME",
        match_pattern="tagme_*",
        column_name_pattern="ssn_*",
        action_parameters={"classification": "PII"},
        schedule_interval_minutes=60,
        auto_apply_max_items=500,
        enabled=True,
        created_by="steward@example.com",
    )

    matched_ids = await resolve_playbook_matches(session, playbook)

    assert set(matched_ids) == {column.id for column in matching_columns}


# ---------------------------------------------------------------------------
# evaluate_and_run_playbook
# ---------------------------------------------------------------------------


async def test_evaluate_no_matches_still_advances_last_run_at(session: AsyncSession) -> None:
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=2, name_prefix="skip")
    playbook = _tag_playbook(datasource)
    session.add(playbook)
    await session.flush()
    now = datetime.now(UTC)

    result = await evaluate_and_run_playbook(session, playbook, now=now)

    assert result.outcome == "NO_MATCHES"
    assert result.matched_count == 0
    assert playbook.last_run_at == now


async def test_evaluate_auto_applies_tag_under_threshold(session: AsyncSession) -> None:
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=3, name_prefix="tagme")
    playbook = _tag_playbook(datasource, auto_apply_max_items=10)
    session.add(playbook)
    await session.flush()

    result = await evaluate_and_run_playbook(session, playbook)
    await session.commit()

    assert result.outcome == "AUTO_APPLIED"
    assert result.matched_count == 3
    assert result.bulk_action_run_id is not None
    run = await session.get(CatalogBulkActionRun, result.bulk_action_run_id)
    assert run is not None
    assert run.selection_mode == "PLAYBOOK_AUTO"
    assert run.succeeded_count == 3
    assert run.parameters["playbook_id"] == str(playbook.id)
    tags = (await session.scalars(select(AssetTag))).all()
    assert {tag.table_id for tag in tags} == {table.id for table in tables}
    assert all(tag.tag_key == "pii-review" for tag in tags)


async def test_evaluate_queues_for_review_over_threshold(session: AsyncSession) -> None:
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=3, name_prefix="tagme")
    playbook = _tag_playbook(datasource, auto_apply_max_items=1)
    session.add(playbook)
    await session.flush()

    result = await evaluate_and_run_playbook(session, playbook)
    await session.commit()

    assert result.outcome == "QUEUED_FOR_REVIEW"
    assert result.matched_count == 3
    assert result.bulk_stewardship_operation_id is not None
    assert result.governance_review_id is not None
    operation = await session.get(BulkStewardshipOperation, result.bulk_stewardship_operation_id)
    review = await session.get(GovernanceReview, result.governance_review_id)
    assert operation is not None and review is not None
    assert operation.operation_type == "TAG"
    assert operation.status == "REVIEW_REQUIRED"
    assert len(operation.subject_ids) == 3
    assert operation.parameters["tag_key"] == "pii-review"
    assert operation.parameters["playbook_id"] == str(playbook.id)
    assert review.object_type == "BULK_STEWARDSHIP_OPERATION"
    assert review.object_id == str(operation.id)
    # No AssetTag rows exist yet -- queued, not applied.
    assert (await session.scalars(select(AssetTag))).first() is None


async def test_evaluate_auto_applies_certify_resolves_relative_expiry(
    session: AsyncSession,
) -> None:
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=1, name_prefix="tagme")
    playbook = MetadataPlaybook(
        id=uuid4(),
        organization_id=datasource.organization_id,
        name="certify-gold",
        action="CERTIFY",
        datasource_id=datasource.id,
        match_field="TABLE_NAME",
        match_pattern="tagme_*",
        action_parameters={"rationale": "quarterly gold-tier review", "expires_after_days": 30},
        schedule_interval_minutes=60,
        auto_apply_max_items=500,
        enabled=True,
        created_by="steward@example.com",
    )
    session.add(playbook)
    await session.flush()
    now = datetime(2026, 9, 1, tzinfo=UTC)

    result = await evaluate_and_run_playbook(session, playbook, now=now)
    await session.commit()

    assert result.outcome == "AUTO_APPLIED"
    certification = (await session.scalars(select(AssetCertification))).one()
    assert certification.table_id == tables[0].id
    assert certification.expires_at == (now + timedelta(days=30)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# REST layer -- direct function calls, matching test_catalog_bulk_actions_endpoints.py's
# convention of calling the FastAPI route function in-process rather than via
# an HTTP client.
# ---------------------------------------------------------------------------


def test_playbook_endpoints_are_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/organizations/{organization_id}/playbooks",
        "/v1/playbooks/{playbook_id}",
        "/v1/playbooks/{playbook_id}/run",
    }
    assert expected <= paths.keys()
    assert "post" in paths["/v1/organizations/{organization_id}/playbooks"]
    assert "get" in paths["/v1/organizations/{organization_id}/playbooks"]
    assert "patch" in paths["/v1/playbooks/{playbook_id}"]
    assert "delete" in paths["/v1/playbooks/{playbook_id}"]
    assert "post" in paths["/v1/playbooks/{playbook_id}/run"]


async def test_create_playbook_rejects_duplicate_name(session: AsyncSession) -> None:
    datasource, _schema = await _seed_datasource(session)
    context = _context(datasource)
    body = PlaybookCreate(
        name="dup",
        action="TAG",
        datasource_id=datasource.id,
        match_pattern="tagme_*",
        action_parameters={"tag_key": "pii-review"},
        schedule_interval_minutes=60,
    )

    first = await create_playbook(datasource.organization_id, body, context, session)
    assert first.name == "dup"

    with pytest.raises(HTTPException) as exc_info:
        await create_playbook(datasource.organization_id, body, context, session)
    assert exc_info.value.status_code == 409


async def test_create_playbook_rejects_wrong_organization(session: AsyncSession) -> None:
    datasource, _schema = await _seed_datasource(session)
    context = _context(datasource)
    body = PlaybookCreate(
        name="cross-org",
        action="TAG",
        datasource_id=datasource.id,
        match_pattern="tagme_*",
        action_parameters={"tag_key": "pii-review"},
        schedule_interval_minutes=60,
    )

    with pytest.raises(HTTPException) as exc_info:
        await create_playbook(uuid4(), body, context, session)
    assert exc_info.value.status_code == 403


def test_playbook_create_validates_action_parameters_per_action() -> None:
    with pytest.raises(ValueError, match="tag_key"):
        PlaybookCreate(
            name="bad",
            action="TAG",
            datasource_id=uuid4(),
            match_pattern="*",
            action_parameters={},
            schedule_interval_minutes=60,
        )
    with pytest.raises(ValueError, match="column_name_pattern"):
        PlaybookCreate(
            name="bad",
            action="CLASSIFY",
            datasource_id=uuid4(),
            match_pattern="*",
            action_parameters={"classification": "PII"},
            schedule_interval_minutes=60,
        )


async def test_list_get_update_delete_playbook_round_trip(session: AsyncSession) -> None:
    datasource, _schema = await _seed_datasource(session)
    context = _context(datasource)
    created = await create_playbook(
        datasource.organization_id,
        PlaybookCreate(
            name="round-trip",
            action="TAG",
            datasource_id=datasource.id,
            match_pattern="tagme_*",
            action_parameters={"tag_key": "pii-review"},
            schedule_interval_minutes=60,
        ),
        context,
        session,
    )

    page = await list_playbooks(datasource.organization_id, 100, 0, context, session)
    assert page.total == 1
    assert page.items[0].id == created.id

    fetched = await get_playbook(created.id, context, session)
    assert fetched.id == created.id

    updated = await update_playbook(
        created.id, PlaybookUpdate(auto_apply_max_items=25), context, session
    )
    assert updated.auto_apply_max_items == 25

    with pytest.raises(HTTPException) as exc_info:
        await update_playbook(
            created.id,
            PlaybookUpdate(action_parameters={}),
            context,
            session,
        )
    assert exc_info.value.status_code == 422

    await delete_playbook(created.id, context, session)
    with pytest.raises(HTTPException) as exc_info:
        await get_playbook(created.id, context, session)
    assert exc_info.value.status_code == 404


async def test_run_playbook_now_applies_immediately(session: AsyncSession) -> None:
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=2, name_prefix="tagme")
    context = _context(datasource)
    created = await create_playbook(
        datasource.organization_id,
        PlaybookCreate(
            name="run-now",
            action="TAG",
            datasource_id=datasource.id,
            match_pattern="tagme_*",
            action_parameters={"tag_key": "pii-review"},
            schedule_interval_minutes=60,
            auto_apply_max_items=10,
        ),
        context,
        session,
    )

    result = await run_playbook_now(created.id, context, session)

    assert result.outcome == "AUTO_APPLIED"
    assert result.matched_count == 2


async def test_run_playbook_now_rejects_disabled_playbook(session: AsyncSession) -> None:
    datasource, _schema = await _seed_datasource(session)
    context = _context(datasource)
    created = await create_playbook(
        datasource.organization_id,
        PlaybookCreate(
            name="disabled",
            action="TAG",
            datasource_id=datasource.id,
            match_pattern="tagme_*",
            action_parameters={"tag_key": "pii-review"},
            schedule_interval_minutes=60,
            enabled=False,
        ),
        context,
        session,
    )

    with pytest.raises(HTTPException) as exc_info:
        await run_playbook_now(created.id, context, session)
    assert exc_info.value.status_code == 409
