"""P2-08: manual revoke endpoint + expiry-warning job + active-tuple backstop.

Same posture as `tests/test_dq3_certification_expiry.py`: exercised against a
real (in-memory sqlite) database so the alembic-declared partial unique
`ix_asset_certification_active_tuple` is the actual thing being tested, and
the revoke endpoint's audit + outbox writes are inspected as rows rather than
mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.certification_expiry_warning import warn_upcoming_certification_expiries
from aida.db import Base
from aida.models import (
    AssetCertification,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
)

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


async def _seed_org_and_table(
    session: AsyncSession,
) -> tuple[Organization, MetadataTable, MetadataColumn]:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code="RETAIL")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Finance", code="FINANCE"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core Banking",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="core-warehouse",
        connector_type="POSTGRES",
        dialect="dialect",
        environment="environment",
        credential_reference="credential_reference",
    )
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(
        organization_id=org.id,
        datasource_id=datasource.id,
        name="warehouse",
        fingerprint="fp",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="accounts",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    column = MetadataColumn(
        organization_id=org.id,
        table_id=table.id,
        name="balance_usd",
        ordinal_position=1,
        physical_type="NUMERIC",
        status="ACTIVE",
        nullable=False,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return org, table, column


async def _extra_table(
    session: AsyncSession, *, org: Organization, schema_id, name: str
) -> MetadataTable:
    """Another ACTIVE table in the same schema, for tests that need more than
    one certifiable asset (see `ix_asset_certification_active_tuple`)."""
    table = MetadataTable(
        organization_id=org.id,
        datasource_id=(
            await session.scalar(
                select(MetadataTable.datasource_id).where(MetadataTable.schema_id == schema_id)
            )
        ),
        schema_id=schema_id,
        name=name,
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


async def _seed_active_cert(
    session: AsyncSession,
    *,
    org: Organization,
    table: MetadataTable,
    column: MetadataColumn | None = None,
    certified_by: str = "steward-a",
    expires_in_days: int = 60,
) -> AssetCertification:
    cert = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        column_id=column.id if column else None,
        asset_type="COLUMN" if column else "TABLE",
        status="ACTIVE",
        rationale="Certified against the approved quarterly data contract.",
        certified_by=certified_by,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    session.add(cert)
    await session.flush()
    return cert


# ---------------------------------------------------------------------------
# Revoke endpoint (exercised through the underlying handler, matching how
# `test_dq3_certification_expiry` exercises `evaluate_analysis_run`
# session-first rather than through an HTTP TestClient).
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    """Minimal duck-typed settings object for the sweep + revoke tests."""

    class _S:
        certification_expiry_warn_days = 7
        certification_expiry_warn_interval_seconds = 86_400
        certification_revoke_enforce_maker_checker = True

    s = _S()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


async def _invoke_revoke(
    session: AsyncSession,
    *,
    table_id,
    reason: str,
    principal_id: str,
    organization_id=None,
    column_id=None,
    settings=None,
):
    from aida.schemas import CertificationRevokeRequest
    from aida.security import SecurityContext
    from atlas.modules.catalog.router import revoke_table_certification

    context = SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )
    body = CertificationRevokeRequest(reason=reason, column_id=column_id)
    return await revoke_table_certification(
        table_id=table_id,
        body=body,
        context=context,
        session=session,
        settings=settings or _make_settings(),
    )


@pytest.mark.asyncio
async def test_revoke_by_different_principal_succeeds(db: AsyncSession) -> None:
    org, table, _ = await _seed_org_and_table(db)
    cert = await _seed_active_cert(db, org=org, table=table, certified_by="steward-a")

    result = await _invoke_revoke(
        db,
        table_id=table.id,
        reason="policy change: quarterly",
        organization_id=org.id,
        principal_id="steward-b",
    )

    assert result.status == "REVOKED"
    assert result.is_active is False
    assert result.revoked_by == "steward-b"
    assert result.revocation_reason == "policy change: quarterly"

    refreshed = await db.get(AssetCertification, cert.id)
    assert refreshed is not None
    assert refreshed.status == "REVOKED"
    assert refreshed.revoked_at is not None

    audits = (
        await db.scalars(select(AuditEvent).where(AuditEvent.action == "CERTIFICATION_REVOKED"))
    ).all()
    assert len(audits) == 1
    outboxes = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "catalog.asset.certification_revoked.v1"
            )
        )
    ).all()
    assert len(outboxes) == 1


@pytest.mark.asyncio
async def test_revoke_by_same_principal_is_refused(db: AsyncSession) -> None:
    from fastapi import HTTPException

    org, table, _ = await _seed_org_and_table(db)
    cert = await _seed_active_cert(db, org=org, table=table, certified_by="steward-a")

    with pytest.raises(HTTPException) as exc:
        await _invoke_revoke(
            db,
            table_id=table.id,
            reason="second thoughts about it",
            organization_id=org.id,
            principal_id="steward-a",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "same_principal_cannot_revoke_own_certification"

    refreshed = await db.get(AssetCertification, cert.id)
    assert refreshed is not None
    assert refreshed.status == "ACTIVE"
    assert refreshed.revoked_at is None


@pytest.mark.asyncio
async def test_revoke_nonexistent_certification_returns_404(db: AsyncSession) -> None:
    from fastapi import HTTPException

    org, table, _ = await _seed_org_and_table(db)
    # no active cert seeded
    with pytest.raises(HTTPException) as exc:
        await _invoke_revoke(
            db,
            table_id=table.id,
            reason="policy change: quarterly",
            organization_id=org.id,
            principal_id="steward-b",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_revoke_column_leaves_table_certification_untouched(
    db: AsyncSession,
) -> None:
    org, table, column = await _seed_org_and_table(db)
    table_cert = await _seed_active_cert(db, org=org, table=table, certified_by="steward-a")
    column_cert = await _seed_active_cert(
        db, org=org, table=table, column=column, certified_by="steward-c"
    )

    await _invoke_revoke(
        db,
        table_id=table.id,
        reason="column semantics changed",
        organization_id=org.id,
        principal_id="steward-b",
        column_id=column.id,
    )

    tc = await db.get(AssetCertification, table_cert.id)
    cc = await db.get(AssetCertification, column_cert.id)
    assert tc is not None and tc.status == "ACTIVE"
    assert cc is not None and cc.status == "REVOKED"


@pytest.mark.asyncio
async def test_maker_checker_can_be_disabled_via_settings(db: AsyncSession) -> None:
    org, table, _ = await _seed_org_and_table(db)
    await _seed_active_cert(db, org=org, table=table, certified_by="steward-a")

    result = await _invoke_revoke(
        db,
        table_id=table.id,
        reason="single-steward deployment revoke",
        organization_id=org.id,
        principal_id="steward-a",
        settings=_make_settings(certification_revoke_enforce_maker_checker=False),
    )
    assert result.status == "REVOKED"


# ---------------------------------------------------------------------------
# Expiry-warning sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_warning_only_fires_inside_window(db: AsyncSession) -> None:
    org, table, _ = await _seed_org_and_table(db)
    now = datetime.now(UTC)

    # One table per certification: `ix_asset_certification_active_tuple`
    # permits a single ACTIVE row per (table, asset_type, column), and this
    # test needs three ACTIVE rows to cover in-window / out-of-window /
    # already-expired. Seeding them on one table asserted nothing about the
    # warning window -- it just violated the index.
    warn_table = table
    far_table = await _extra_table(db, org=org, schema_id=table.schema_id, name="accounts_far")
    expired_table = await _extra_table(
        db, org=org, schema_id=table.schema_id, name="accounts_expired"
    )

    # ACTIVE, expires in 3 days -> should warn
    warn_cert = AssetCertification(
        organization_id=org.id,
        table_id=warn_table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Quarterly certification against the approved data contract.",
        certified_by="steward-a",
        expires_at=now + timedelta(days=3),
    )
    # ACTIVE, expires in 10 days -> outside window
    far_cert = AssetCertification(
        organization_id=org.id,
        table_id=far_table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Longer certification against the approved data contract.",
        certified_by="steward-b",
        expires_at=now + timedelta(days=10),
    )
    # ACTIVE but already past its expires_at (query-time EXPIRED) -> skip
    expired_cert = AssetCertification(
        organization_id=org.id,
        table_id=expired_table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Older certification against the approved data contract.",
        certified_by="steward-c",
        expires_at=now - timedelta(hours=1),
    )
    db.add_all([warn_cert, far_cert, expired_cert])
    await db.flush()

    emitted = await warn_upcoming_certification_expiries(db, now=now, warn_days=7)

    assert [e.certification_id for e in emitted] == [warn_cert.id]

    audits = (
        await db.scalars(
            select(AuditEvent).where(AuditEvent.action == "CERTIFICATION_EXPIRY_WARNING_SENT")
        )
    ).all()
    assert len(audits) == 1
    outboxes = (
        await db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "catalog.asset.certification_expiry_warning.v1"
            )
        )
    ).all()
    assert len(outboxes) == 1
    payload = outboxes[0].payload
    assert payload["notify_principal"] == "steward-a"
    assert payload["days_until_expiry"] == 2 or payload["days_until_expiry"] == 3


@pytest.mark.asyncio
async def test_expiry_warning_is_idempotent_inside_cooldown(db: AsyncSession) -> None:
    org, table, _ = await _seed_org_and_table(db)
    now = datetime.now(UTC)
    cert = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Quarterly certification against the approved data contract.",
        certified_by="steward-a",
        expires_at=now + timedelta(days=3),
    )
    db.add(cert)
    await db.flush()

    first = await warn_upcoming_certification_expiries(db, now=now, warn_days=7)
    assert len(first) == 1

    # A second sweep one hour later inside the same cooldown must skip it.
    second = await warn_upcoming_certification_expiries(
        db, now=now + timedelta(hours=1), warn_days=7
    )
    assert second == []


# ---------------------------------------------------------------------------
# Uniqueness backstop (P2-08 c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_tuple_uniqueness_refuses_a_second_active_row(
    db: AsyncSession,
) -> None:
    org, table, _ = await _seed_org_and_table(db)
    await _seed_active_cert(db, org=org, table=table, certified_by="steward-a")
    # A second ACTIVE row for the same (table, TABLE, None, org) tuple.
    duplicate = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Second, racing insert against the same tuple as above.",
        certified_by="steward-b",
        expires_at=datetime.now(UTC) + timedelta(days=60),
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.asyncio
async def test_active_tuple_uniqueness_allows_superseded_row_alongside_new_active(
    db: AsyncSession,
) -> None:
    """A prior SUPERSEDED row for the same tuple must not block a new ACTIVE one --
    that is the whole point of gating the partial index on `WHERE status =
    'ACTIVE'`."""
    org, table, _ = await _seed_org_and_table(db)
    old = await _seed_active_cert(db, org=org, table=table, certified_by="steward-a")
    old.status = "SUPERSEDED"
    await db.flush()
    fresh = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="Fresh certification after the previous was superseded.",
        certified_by="steward-b",
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    db.add(fresh)
    await db.flush()  # should NOT raise
    assert fresh.id != old.id


# ---------------------------------------------------------------------------
# REVOKED single-writer regression: only the revoke endpoint writes the value.
# ---------------------------------------------------------------------------


def test_revoked_status_has_a_single_writer_in_the_codebase() -> None:
    """P2-08 sanity check: `REVOKED` used to be dead status -- three readers,
    zero writers on the `AssetCertification` side. With the manual revoke
    endpoint in place there is EXACTLY one writer (the endpoint's
    `active.status = "REVOKED"`), and nothing else should be flipping the
    value silently."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    writers: list[tuple[str, int, str]] = []
    for pyfile in root.rglob("*.py"):
        for lineno, line in enumerate(pyfile.read_text(encoding="utf-8").splitlines(), start=1):
            # Signature of a WRITE to an AssetCertification row: an
            # assignment landing "REVOKED" on a `.status` attribute. Bare
            # occurrences of the string (frozenset members, docstrings,
            # comments, log details) do not match this pattern.
            if '.status = "REVOKED"' in line or ".status = 'REVOKED'" in line:
                # `as_posix()` so the substring match below is not defeated
                # by Windows backslash separators.
                writers.append((pyfile.relative_to(root).as_posix(), lineno, line.strip()))
    # AssetCertification writer is the catalog router; everything else that
    # matches is a *different* domain (delegation, product_marketplace, ...)
    # setting its OWN model's status, not AssetCertification's.
    cert_writers = [
        (path, lineno, snippet)
        for path, lineno, snippet in writers
        if "atlas/modules/catalog/router.py" in path
    ]
    assert len(cert_writers) == 1, (
        f"expected exactly one AssetCertification REVOKED writer, found {cert_writers}"
    )
