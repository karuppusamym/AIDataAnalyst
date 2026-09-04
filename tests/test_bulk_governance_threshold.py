"""GV-2 / P0-02: prove the maker-checker bypass on the catalog-router bulk
endpoints (`bulk_assign_ownership`, `bulk_certify_tables`) is closed by
the threshold-based governance policy added under this row.

Before P0-02, a `DataSteward` could bulk-assign ownership or bulk-certify
tables directly to ACTIVE via the catalog router, bypassing the
`BulkStewardshipOperation` + `GovernanceReview` maker-checker contract that
the governed `aida.stewardship_api._create_bulk_operation` path enforces
for the same operations. This file proves the router now consults
`Settings.bulk_governance_threshold` and
`Settings.bulk_governance_roles_requiring_review` on every request and
routes accordingly:

  * count <= threshold AND role NOT in review list -> direct write, and a
    `catalog.bulk_action.direct_write.v1` audit event is emitted naming
    operator, count, subjects and the resolved reason.
  * count > threshold -> routed through `BulkStewardshipOperation` +
    `GovernanceReview` (HTTP 202), no `OwnershipAssignment` /
    `AssetCertification` mutation applied yet.
  * DataSteward at ANY count -> routed through review (proves the audit
    finding directly).
  * PlatformAdmin at count <= threshold -> direct write (preserves the
    deliberate "single deliberate action by an authorized user" exemption
    the P0-02 fix is careful not to break).
  * Config override respected -- lifting the threshold on a per-call
    settings instance restores direct-write for higher counts.

This test file uses the real-engine (in-memory sqlite) pattern of
`tests/test_catalog_bulk_actions_endpoints.py` (CT-1) and
`tests/test_bulk_governance_decisions.py` (PG-3): rows seeded directly
through the ORM, the endpoint called in-process, and results asserted
against real database state -- never a mocked session.
"""

import itertools
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import Response
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from atlas.modules.catalog.router import (
    bulk_assign_ownership,
    bulk_certify_tables,
)
from atlas.platform.config import Settings
from aida.db import Base
from aida.models import (
    AssetCertification,
    AuditEvent,
    BulkStewardshipOperation,
    CatalogBulkActionRun,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OwnershipAssignment,
    Project,
)
from aida.schemas import (
    CatalogBulkCertifyRequest,
    CatalogBulkOwnRequest,
)
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio


# Same sqlite-only `AuditEvent.id` seed as CT-1 and PG-3.
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


async def _seed(
    session: AsyncSession, *, table_count: int
) -> tuple[DataSource, list[MetadataTable]]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Core",
        code=f"COR{uuid4().hex[:6]}",
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
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="main",
    )
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        catalog_id=catalog.id,
        name="public",
    )
    session.add_all([org, lob, domain, project, datasource, catalog, schema])
    tables = [
        MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            catalog_id=catalog.id,
            schema_id=schema.id,
            name=f"table_{i:04d}",
            object_kind="TABLE",
            status="ACTIVE",
        )
        for i in range(table_count)
    ]
    session.add_all(tables)
    await session.flush()
    return datasource, tables


def _ctx(datasource: DataSource, *, role: str, principal: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({role}),
    )


def _settings(*, threshold: int, review_roles: list[str] | None = None) -> Settings:
    return Settings(
        environment="test",
        bulk_governance_threshold=threshold,
        bulk_governance_roles_requiring_review=(
            review_roles if review_roles is not None else ["DataSteward"]
        ),
    )


# ---------------------------------------------------------------------------
# Direct-write path: count <= threshold and role NOT in review-required list.
# The `catalog.bulk_action.direct_write.v1` audit event MUST fire, and the
# `OwnershipAssignment` row must be persisted immediately.
# ---------------------------------------------------------------------------


async def test_platform_admin_within_threshold_direct_writes_and_audits(
    session: AsyncSession,
) -> None:
    datasource, tables = await _seed(session, table_count=5)
    await session.commit()

    result = await bulk_assign_ownership(
        datasource.organization_id,
        CatalogBulkOwnRequest(
            table_ids=[t.id for t in tables],
            owner_type="INDIVIDUAL",
            owner_principal="alice",
        ),
        context=_ctx(datasource, role="PlatformAdmin", principal="admin@example.com"),
        session=session,
        settings=_settings(threshold=10),
    )

    # Direct-write path returns the CatalogBulkActionRun ORM row (not a
    # Response), and every ACTIVE OwnershipAssignment is persisted.
    assert isinstance(result, CatalogBulkActionRun)
    assert result.succeeded_count == 5
    assignments = (await session.scalars(select(OwnershipAssignment))).all()
    assert len(assignments) == 5

    # No governed BulkStewardshipOperation was created on the direct path.
    assert (await session.scalar(select(func.count()).select_from(BulkStewardshipOperation))) == 0

    # The direct-write audit event fired with count, subject_ids and reason.
    direct = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "catalog.bulk_action.direct_write.v1"
            )
        )
    ).all()
    assert len(direct) == 1
    details = direct[0].details
    assert details["operation_type"] == "ASSIGN_OWNERSHIP"
    assert details["subject_count"] == 5
    assert set(details["subject_ids"]) == {str(t.id) for t in tables}
    assert details["reason"] == "within_threshold_and_role_not_in_review_list"


# ---------------------------------------------------------------------------
# Threshold exceeded: even a PlatformAdmin request MUST route through review.
# The response is a 202 with the BulkStewardshipOperation, no OwnershipAssignment
# has been created yet, and a GovernanceReview row backs the operation.
# ---------------------------------------------------------------------------


async def test_count_over_threshold_routes_through_governance(
    session: AsyncSession,
) -> None:
    datasource, tables = await _seed(session, table_count=15)
    await session.commit()

    result = await bulk_assign_ownership(
        datasource.organization_id,
        CatalogBulkOwnRequest(
            table_ids=[t.id for t in tables],
            owner_type="GROUP",
            owner_principal="retail-stewards",
        ),
        context=_ctx(datasource, role="PlatformAdmin", principal="admin@example.com"),
        session=session,
        settings=_settings(threshold=10),
    )

    assert isinstance(result, Response)
    assert result.status_code == 202
    # No OwnershipAssignment written yet -- review must be approved first.
    assert (await session.scalar(select(func.count()).select_from(OwnershipAssignment))) == 0
    # BulkStewardshipOperation + GovernanceReview persisted.
    op = await session.scalar(select(BulkStewardshipOperation))
    assert op is not None
    assert op.operation_type == "ASSIGN_OWNERSHIP"
    assert len(op.subject_ids) == 15
    review = await session.get(GovernanceReview, op.governance_review_id)
    assert review is not None
    # And the router-side "review_routed" audit was recorded with the reason.
    routed = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "catalog.bulk_action.review_routed.v1"
            )
        )
    ).all()
    assert len(routed) == 1
    assert routed[0].details["reason"] == "count_above_threshold"
    assert routed[0].details["subject_count"] == 15


# ---------------------------------------------------------------------------
# DataSteward at ANY count routes through review -- the audit finding directly.
# ---------------------------------------------------------------------------


async def test_data_steward_always_routes_through_governance(
    session: AsyncSession,
) -> None:
    datasource, tables = await _seed(session, table_count=1)
    await session.commit()

    result = await bulk_assign_ownership(
        datasource.organization_id,
        CatalogBulkOwnRequest(
            table_ids=[tables[0].id],
            owner_type="INDIVIDUAL",
            owner_principal="jane",
        ),
        context=_ctx(datasource, role="DataSteward", principal="steward@example.com"),
        session=session,
        settings=_settings(threshold=10),
    )

    assert isinstance(result, Response)
    assert result.status_code == 202
    assert (await session.scalar(select(func.count()).select_from(OwnershipAssignment))) == 0
    op = await session.scalar(select(BulkStewardshipOperation))
    assert op is not None
    routed = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "catalog.bulk_action.review_routed.v1"
        )
    )
    assert routed is not None
    assert routed.details["reason"] == "role_requires_review"


# ---------------------------------------------------------------------------
# The same threshold gate applies to bulk_certify_tables.
# ---------------------------------------------------------------------------


async def test_bulk_certify_data_steward_routes_through_governance(
    session: AsyncSession,
) -> None:
    datasource, tables = await _seed(session, table_count=2)
    await session.commit()

    result = await bulk_certify_tables(
        datasource.organization_id,
        CatalogBulkCertifyRequest(
            table_ids=[t.id for t in tables],
            rationale="Certified against the approved quarterly data contract.",
            expires_at=datetime.now(UTC) + timedelta(days=90),
        ),
        context=_ctx(datasource, role="DataSteward", principal="steward@example.com"),
        session=session,
        settings=_settings(threshold=10),
    )

    assert isinstance(result, Response)
    assert result.status_code == 202
    # No AssetCertification created yet -- review must be approved first.
    assert (await session.scalar(select(func.count()).select_from(AssetCertification))) == 0
    op = await session.scalar(select(BulkStewardshipOperation))
    assert op is not None
    assert op.operation_type == "CERTIFY_ASSET"


async def test_bulk_certify_platform_admin_within_threshold_direct_writes(
    session: AsyncSession,
) -> None:
    datasource, tables = await _seed(session, table_count=3)
    await session.commit()

    expires = datetime.now(UTC) + timedelta(days=60)
    result = await bulk_certify_tables(
        datasource.organization_id,
        CatalogBulkCertifyRequest(
            table_ids=[t.id for t in tables],
            rationale="Certified against approved quarterly data contract.",
            expires_at=expires,
        ),
        context=_ctx(datasource, role="PlatformAdmin", principal="admin@example.com"),
        session=session,
        settings=_settings(threshold=10),
    )
    assert isinstance(result, CatalogBulkActionRun)
    assert result.succeeded_count == 3
    assert (await session.scalar(select(func.count()).select_from(AssetCertification))) == 3
    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "catalog.bulk_action.direct_write.v1"
        )
    )
    assert audit is not None
    assert audit.details["operation_type"] == "CERTIFY_ASSET"
    assert audit.details["subject_count"] == 3
    assert audit.details["expires_at"] == expires.isoformat()


# ---------------------------------------------------------------------------
# Config-override respected: bumping the threshold above the request size
# and clearing the review-required list flips the path back to direct-write.
# ---------------------------------------------------------------------------


async def test_config_override_respects_higher_threshold(session: AsyncSession) -> None:
    datasource, tables = await _seed(session, table_count=25)
    await session.commit()

    result = await bulk_assign_ownership(
        datasource.organization_id,
        CatalogBulkOwnRequest(
            table_ids=[t.id for t in tables],
            owner_type="INDIVIDUAL",
            owner_principal="alice",
        ),
        context=_ctx(datasource, role="PlatformAdmin", principal="admin@example.com"),
        session=session,
        settings=_settings(threshold=50, review_roles=[]),
    )
    assert isinstance(result, CatalogBulkActionRun)
    assert result.succeeded_count == 25
    assert (await session.scalar(select(func.count()).select_from(OwnershipAssignment))) == 25


# ---------------------------------------------------------------------------
# Playbook auto-apply: OWN branch emits PLAYBOOK_AUTO_APPLY per subject.
# ---------------------------------------------------------------------------


async def test_playbook_auto_apply_own_emits_per_subject_audit(session: AsyncSession) -> None:
    """Playbooks are rule-defined and keep direct-write behaviour by design
    (the human is the rule author). GV-2 adds a per-subject audit trail so
    an admin can query "which playbook, if any, set this table's owner?"
    without correlating a run id back to its list of results.
    """
    from aida.models import MetadataPlaybook
    from aida.playbooks import _apply_one_item
    from aida.security import SecurityContext as SC

    datasource, tables = await _seed(session, table_count=2)
    await session.commit()

    playbook = MetadataPlaybook(
        id=uuid4(),
        organization_id=datasource.organization_id,
        name="RetailOwner",
        action="OWN",
        action_parameters={
            "owner_type": "GROUP",
            "owner_principal": "retail-owners",
        },
        filter_definition={},
        auto_apply_max_items=100,
        schedule_interval_minutes=60,
        created_by="playbook-author@example.com",
    )
    session.add(playbook)
    await session.flush()

    worker_ctx = SC(
        principal_id="playbook-worker",
        principal_type="SERVICE",
        organization_id=datasource.organization_id,
        roles=frozenset({"PlaybookWorker"}),
    )
    now = datetime.now(UTC)
    tables_by_id = {t.id: t for t in tables}
    for t in tables:
        await _apply_one_item(
            session,
            playbook,
            t.id,
            applied_by=worker_ctx.principal_id,
            context=worker_ctx,
            tables=tables_by_id,
            existing_tags={},
            existing_assignments={},
            active_certifications={},
            columns={},
            now=now,
        )
    await session.commit()

    audits = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "PLAYBOOK_AUTO_APPLY")
        )
    ).all()
    assert len(audits) == 2
    assert {a.resource_id for a in audits} == {str(t.id) for t in tables}
    for a in audits:
        assert a.resource_type == "TABLE"
        assert a.details["playbook_kind"] == "OWN"
        assert a.details["playbook_id"] == str(playbook.id)
        assert a.details["owner_principal"] == "retail-owners"
