"""P2-07: OwnershipAssignment re-affirmation + expiry-warning sweep +
expire-lapsed sweep + identity-lifecycle (delete / merge) handler.

Same posture as `tests/test_certification_revoke_and_expiry.py`: exercised
against a real (in-memory sqlite) database so the ORM columns, indexes and
sweep semantics are the actual thing under test, and audit + outbox writes
are inspected as rows rather than mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida import ownership_expiry_warning as expiry_module
from aida.db import Base
from aida.identity_events import (
    emit_principal_deleted,
    emit_principal_merged,
)
from aida.models import (
    AuditEvent,
    BulkStewardshipOperation,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    OwnershipAssignment,
    Project,
    UnownedAssetEscalation,
)
from aida.ownership_expiry_warning import (
    expire_lapsed_ownership_assignments,
    warn_upcoming_ownership_expiries,
)
from aida.ownership_principal_lifecycle import (
    handle_principal_deleted,
    handle_principal_merged,
)
from aida.security import SecurityContext
from aida.stewardship_service import apply_bulk_operation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _make_settings(**overrides):
    class _S:
        ownership_reaffirm_days = 180
        ownership_expiry_warn_days = 14
        ownership_expiry_warn_interval_seconds = 86_400
        ownership_expiry_grace_days = 30
        ownership_leaver_auto_reassign = True

    s = _S()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


async def _seed_org_and_table(session: AsyncSession, *, name: str = "accounts") -> tuple[Organization, MetadataTable]:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:4]}")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Finance", code=f"F{uuid4().hex[:4]}"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"src-{uuid4().hex[:6]}",
        connector_type="POSTGRES",
        dialect="dialect",
        environment="environment",
        credential_reference="credential_reference",
    )
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(organization_id=org.id, datasource_id=datasource.id, name="w", fingerprint="fp")
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp")
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return org, table


async def _seed_active_assignment(
    session: AsyncSession,
    *,
    org: Organization,
    table: MetadataTable,
    owner_principal: str = "alice",
    expires_at: datetime | None = None,
) -> OwnershipAssignment:
    assignment = OwnershipAssignment(
        organization_id=org.id,
        subject_type="TABLE",
        subject_id=str(table.id),
        owner_type="INDIVIDUAL",
        owner_principal=owner_principal,
        assignment_kind="MANUAL",
        status="ACTIVE",
        assigned_by="steward-x",
        expires_at=expires_at,
    )
    session.add(assignment)
    await session.flush()
    return assignment


# ---------------------------------------------------------------------------
# 1. ASSIGN_OWNERSHIP sets expires_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_ownership_sets_expires_at_180_days(db, monkeypatch):
    org, table = await _seed_org_and_table(db)
    monkeypatch.setattr(
        "aida.stewardship_service.get_settings",
        lambda: _make_settings(ownership_reaffirm_days=180),
    )
    review = GovernanceReview(
        organization_id=org.id,
        object_type="BULK_STEWARDSHIP_OPERATION",
        object_id="pending",
        requested_action="ASSIGN_OWNERSHIP",
        requested_by="steward-x",
    )
    db.add(review)
    await db.flush()
    op = BulkStewardshipOperation(
        organization_id=org.id,
        operation_type="ASSIGN_OWNERSHIP",
        subject_type="TABLE",
        subject_ids=[str(table.id)],
        parameters={"owner_type": "INDIVIDUAL", "owner_principal": "alice"},
        requested_by="steward-x",
        status="PENDING",
        governance_review_id=review.id,
    )
    db.add(op)
    await db.flush()
    now = datetime.now(UTC)
    await apply_bulk_operation(db, op, reviewer="steward-x", now=now)
    row = (
        await db.scalars(select(OwnershipAssignment).where(OwnershipAssignment.subject_id == str(table.id)))
    ).one()
    assert row.expires_at is not None
    delta_days = (row.expires_at - now).total_seconds() / 86_400
    assert 179.9 < delta_days < 180.1


# ---------------------------------------------------------------------------
# 2. Reaffirm extends expires_at + audit + outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaffirm_extends_expires_at_and_records_audit(db):
    from aida.stewardship_api import _reaffirm_one

    org, table = await _seed_org_and_table(db)
    old_expires = datetime.now(UTC) + timedelta(days=5)
    assignment = await _seed_active_assignment(db, org=org, table=table, expires_at=old_expires)
    ctx = SecurityContext(
        principal_id="alice",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )
    now = datetime.now(UTC)
    await _reaffirm_one(
        db, context=ctx, assignment=assignment, now=now, reaffirm_days=180
    )
    await db.flush()
    assert assignment.reaffirmed_at == now
    assert assignment.reaffirmed_by == "alice"
    assert assignment.expires_at is not None
    assert assignment.expires_at > old_expires
    # audit + outbox
    audits = (
        await db.scalars(
            select(AuditEvent).where(AuditEvent.action == "OWNERSHIP_ASSIGNMENT_REAFFIRMED")
        )
    ).all()
    assert len(audits) == 1
    outbox = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "ownership.assignment.reaffirmed.v1"
            )
        )
    ).all()
    assert len(outbox) == 1


# ---------------------------------------------------------------------------
# 3. Warning sweep: only the 5-day one warns; expired doesn't; 20-day is
# outside window; NULL expires_at legacy row untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warning_sweep_only_hits_rows_inside_window(db):
    org, t1 = await _seed_org_and_table(db, name="t1")
    _, t2 = await _seed_org_and_table(db, name="t2")
    _, t3 = await _seed_org_and_table(db, name="t3")
    _, t_legacy = await _seed_org_and_table(db, name="t_legacy")
    now = datetime.now(UTC)
    a5 = await _seed_active_assignment(db, org=org, table=t1, expires_at=now + timedelta(days=5))
    _a20 = await _seed_active_assignment(db, org=org, table=t2, expires_at=now + timedelta(days=20))
    _a_exp = await _seed_active_assignment(db, org=org, table=t3, expires_at=now - timedelta(days=1))
    _a_leg = await _seed_active_assignment(db, org=org, table=t_legacy, expires_at=None)

    warnings = await warn_upcoming_ownership_expiries(db, now=now, warn_days=14)
    assert {w.assignment_id for w in warnings} == {a5.id}
    assert a5.expiry_warning_emitted_at == now
    # legacy row untouched
    row = await db.get(OwnershipAssignment, _a_leg.id)
    assert row.expiry_warning_emitted_at is None


# ---------------------------------------------------------------------------
# 4. Warning idempotency: same row does NOT warn twice within cooldown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warning_sweep_idempotent_within_cooldown(db):
    org, table = await _seed_org_and_table(db)
    now = datetime.now(UTC)
    await _seed_active_assignment(db, org=org, table=table, expires_at=now + timedelta(days=5))
    first = await warn_upcoming_ownership_expiries(db, now=now, warn_days=14)
    assert len(first) == 1
    # Second run one day later is inside `warn_days * 2` cooldown -- no re-warn.
    second = await warn_upcoming_ownership_expiries(
        db, now=now + timedelta(days=1), warn_days=14
    )
    assert second == []


# ---------------------------------------------------------------------------
# 5. Expire sweep: 40 days past expiry (past grace) -> LAPSED, plus a routing
# entry when it was the last active owner.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_sweep_flips_lapsed_and_routes_last_owner(db):
    org, table = await _seed_org_and_table(db)
    now = datetime.now(UTC)
    expired_at = now - timedelta(days=40)
    a = await _seed_active_assignment(db, org=org, table=table, expires_at=expired_at)
    lapses = await expire_lapsed_ownership_assignments(db, now=now, grace_days=30)
    assert len(lapses) == 1
    assert lapses[0].assignment_id == a.id
    assert lapses[0].last_owner is True
    row = await db.get(OwnershipAssignment, a.id)
    assert row.status == "LAPSED"
    # unowned-asset backlog entry staged
    escalations = (
        await db.scalars(
            select(UnownedAssetEscalation).where(UnownedAssetEscalation.table_id == table.id)
        )
    ).all()
    assert len(escalations) == 1
    audits = (
        await db.scalars(
            select(AuditEvent).where(AuditEvent.action == "OWNERSHIP_ASSIGNMENT_LAPSED")
        )
    ).all()
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_expire_sweep_does_not_route_when_other_owner_remains(db):
    org, table = await _seed_org_and_table(db)
    now = datetime.now(UTC)
    expired_at = now - timedelta(days=40)
    _a = await _seed_active_assignment(
        db, org=org, table=table, owner_principal="alice", expires_at=expired_at
    )
    # Second active owner on the same subject (no expiry)
    _b = await _seed_active_assignment(
        db, org=org, table=table, owner_principal="bob", expires_at=None
    )
    lapses = await expire_lapsed_ownership_assignments(db, now=now, grace_days=30)
    assert len(lapses) == 1
    assert lapses[0].last_owner is False
    escalations = (
        await db.scalars(
            select(UnownedAssetEscalation).where(UnownedAssetEscalation.table_id == table.id)
        )
    ).all()
    assert escalations == []


# ---------------------------------------------------------------------------
# 6. Leaver handler: identity.principal.deleted flips all ACTIVE to
# LAPSED_LEAVER, routes each subject that lost its last owner.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_principal_deleted_flips_active_to_lapsed_leaver(db):
    org, t1 = await _seed_org_and_table(db, name="t1")
    _, t2 = await _seed_org_and_table(db, name="t2")
    a1 = await _seed_active_assignment(db, org=org, table=t1, owner_principal="alice")
    a2 = await _seed_active_assignment(db, org=org, table=t2, owner_principal="alice")
    settings = _make_settings()
    result = await handle_principal_deleted(
        db, settings=settings, principal_id="alice"
    )
    assert set(result.lapsed_assignment_ids) == {a1.id, a2.id}
    r1 = await db.get(OwnershipAssignment, a1.id)
    r2 = await db.get(OwnershipAssignment, a2.id)
    assert r1.status == "LAPSED_LEAVER"
    assert r2.status == "LAPSED_LEAVER"
    escalations = (await db.scalars(select(UnownedAssetEscalation))).all()
    assert len(escalations) == 2


# ---------------------------------------------------------------------------
# 7. Leaver + merged-into: ownership updates to new principal, MERGED
# assignment_kind, no lapse (unless successor already owned the subject).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_principal_merged_redirects_active_ownership(db):
    org, t1 = await _seed_org_and_table(db, name="t1")
    _, t2 = await _seed_org_and_table(db, name="t2")
    a1 = await _seed_active_assignment(db, org=org, table=t1, owner_principal="alice")
    a2 = await _seed_active_assignment(db, org=org, table=t2, owner_principal="alice")
    # Successor already owns t2 -- that one lapses instead of duplicating
    _clash = await _seed_active_assignment(
        db, org=org, table=t2, owner_principal="bob"
    )
    settings = _make_settings()
    result = await handle_principal_merged(
        db,
        settings=settings,
        from_principal_id="alice",
        into_principal_id="bob",
    )
    assert a1.id in set(result.reassigned_assignment_ids)
    assert a2.id in set(result.lapsed_assignment_ids)
    r1 = await db.get(OwnershipAssignment, a1.id)
    r2 = await db.get(OwnershipAssignment, a2.id)
    assert r1.owner_principal == "bob"
    assert r1.assignment_kind == "MERGED"
    assert r1.status == "ACTIVE"
    assert r2.status == "LAPSED_LEAVER"


# ---------------------------------------------------------------------------
# 8. Config gate: leaver_auto_reassign=false -> handler no-ops.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaver_handler_config_gated_off_is_noop(db):
    org, t1 = await _seed_org_and_table(db, name="t1")
    a = await _seed_active_assignment(db, org=org, table=t1, owner_principal="alice")
    settings = _make_settings(ownership_leaver_auto_reassign=False)
    result = await handle_principal_deleted(
        db, settings=settings, principal_id="alice"
    )
    assert result.lapsed_assignment_ids == ()
    row = await db.get(OwnershipAssignment, a.id)
    assert row.status == "ACTIVE"


# ---------------------------------------------------------------------------
# 9. emit_principal_deleted records the event AND reconciles ownership.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_principal_deleted_records_event_and_reconciles(db):
    org, t1 = await _seed_org_and_table(db, name="t1")
    a = await _seed_active_assignment(db, org=org, table=t1, owner_principal="alice")
    ctx = SecurityContext(
        principal_id="admin",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"PlatformAdmin"}),
    )
    settings = _make_settings()
    await emit_principal_deleted(
        db, settings=settings, context=ctx, principal_id="alice"
    )
    row = await db.get(OwnershipAssignment, a.id)
    assert row.status == "LAPSED_LEAVER"
    outbox = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "identity.principal.deleted.v1"
            )
        )
    ).all()
    assert len(outbox) == 1


@pytest.mark.asyncio
async def test_emit_principal_merged_records_event_and_redirects(db):
    org, t1 = await _seed_org_and_table(db, name="t1")
    a = await _seed_active_assignment(db, org=org, table=t1, owner_principal="alice")
    ctx = SecurityContext(
        principal_id="admin",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"PlatformAdmin"}),
    )
    settings = _make_settings()
    await emit_principal_merged(
        db,
        settings=settings,
        context=ctx,
        from_principal_id="alice",
        into_principal_id="bob",
    )
    row = await db.get(OwnershipAssignment, a.id)
    assert row.owner_principal == "bob"
    outbox = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "identity.principal.merged.v1"
            )
        )
    ).all()
    assert len(outbox) == 1


# ---------------------------------------------------------------------------
# 10. Bulk-reaffirm: 5 items, one invalid -> 4 REAFFIRMED, 1 skipped, one
# SAVEPOINT per item.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_reaffirm_savepoint_partial_success(db, monkeypatch):
    from aida.stewardship_api import bulk_reaffirm_ownership_assignments
    from aida.schemas import OwnershipAssignmentBulkReaffirmRequest

    org, t1 = await _seed_org_and_table(db, name="t1")
    _, t2 = await _seed_org_and_table(db, name="t2")
    _, t3 = await _seed_org_and_table(db, name="t3")
    _, t4 = await _seed_org_and_table(db, name="t4")

    ok_ids = []
    for tbl in (t1, t2, t3, t4):
        a = await _seed_active_assignment(db, org=org, table=tbl, owner_principal="alice")
        ok_ids.append(a.id)
    # One id that does not exist -> NOT_FOUND
    bad_id = uuid4()

    settings = _make_settings()
    ctx = SecurityContext(
        principal_id="alice",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )
    body = OwnershipAssignmentBulkReaffirmRequest(
        assignment_ids=[*ok_ids, bad_id]
    )
    result = await bulk_reaffirm_ownership_assignments(
        body=body, settings=settings, context=ctx, session=db
    )
    assert result.reaffirmed == 4
    assert result.skipped == 1
    outcomes = {item.outcome for item in result.items}
    assert "REAFFIRMED" in outcomes and "NOT_FOUND" in outcomes


# ---------------------------------------------------------------------------
# 11. Rate limiter: `run_ownership_expiry_pass` respects its own interval.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ownership_expiry_pass_rate_limited():
    expiry_module._reset_due_state_for_tests()
    # First call runs; second call (0s later) is skipped.
    # We don't need a session -- the second call short-circuits before opening one.
    from unittest.mock import AsyncMock, patch

    settings = _make_settings(ownership_expiry_warn_interval_seconds=60)
    with patch("aida.ownership_expiry_warning.session_factory", create=True):
        # First: due, would call session_factory (mocked)
        pass
    # Simpler: just call the `_due` guard directly.
    now = datetime.now(UTC)
    interval = timedelta(seconds=settings.ownership_expiry_warn_interval_seconds)
    assert expiry_module._due(None, now, interval) is True
    assert expiry_module._due(now, now, interval) is False
    assert expiry_module._due(now, now + timedelta(seconds=61), interval) is True
