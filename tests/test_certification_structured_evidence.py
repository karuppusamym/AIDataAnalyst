"""P3-09: `AssetCertification.evidence` structured evidence blob.

Same fixture posture as `tests/test_certification_revoke_and_expiry.py`:
a real in-memory SQLite via the alembic-declared metadata, so the new
JSON column and the four write paths (single-certify, direct-write bulk,
reviewed-bulk P0-02, playbook auto-apply) are exercised end-to-end
rather than mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on Base.metadata
from aida.certification_evidence import (
    EVIDENCE_SCHEMA_VERSION,
    backfill_certification_evidence_v1,
    compute_certification_evidence,
    summarize_evidence,
)
from aida.db import Base
from aida.models import (
    AnalysisRun,
    AssetCertification,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    DataDomain,
    DataQualityIncident,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OwnershipAssignment,
    Project,
    TableProfile,
)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_estate(session: AsyncSession) -> tuple[Organization, MetadataTable]:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code="RETAIL")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Finance", code="FIN"
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
    ds = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="warehouse",
        connector_type="POSTGRES",
        dialect="dialect",
        environment="environment",
        credential_reference="credential_reference",
    )
    session.add(ds)
    await session.flush()
    cat = MetadataCatalog(
        organization_id=org.id, datasource_id=ds.id, name="warehouse", fingerprint="fp"
    )
    session.add(cat)
    await session.flush()
    sch = MetadataSchema(organization_id=org.id, catalog_id=cat.id, name="public", fingerprint="fp")
    session.add(sch)
    await session.flush()
    table = MetadataTable(
        organization_id=org.id,
        datasource_id=ds.id,
        schema_id=sch.id,
        name="accounts",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return org, table


async def _seed_evidence_context(
    session: AsyncSession,
    org: Organization,
    table: MetadataTable,
    *,
    owners: int = 2,
    terms: int = 3,
    open_incidents: int = 0,
) -> None:
    """Seed the four sources `compute_certification_evidence` reads.
    Approved doc version (v1), N ACTIVE owners, M approved terms, K open
    incidents, and one completed profile row.
    """
    doc = AssetDocumentation(organization_id=org.id, table_id=table.id)
    session.add(doc)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            organization_id=org.id,
            documentation_id=doc.id,
            version=1,
            status="APPROVED",
            aliases=[],
            readme="canonical",
            created_by="steward-a",
            approved_by="reviewer-b",
            approved_at=datetime.now(UTC),
        )
    )
    for i in range(owners):
        session.add(
            OwnershipAssignment(
                organization_id=org.id,
                subject_type="TABLE",
                subject_id=str(table.id),
                owner_type="TEAM",
                owner_principal=f"team-{i}",
                assignment_kind="MANUAL",
                assigned_by="steward-a",
                status="ACTIVE",
            )
        )
    for i in range(terms):
        term = GlossaryTerm(organization_id=org.id, term_key=f"term_{i}_{uuid4().hex[:4]}")
        session.add(term)
        await session.flush()
        session.add(
            GlossaryTermVersion(
                organization_id=org.id,
                term_id=term.id,
                version=1,
                status="APPROVED",
                display_name=f"Term {i}",
                definition="def",
                synonyms=[],
                created_by="a",
            )
        )
        session.add(
            AssetTermLink(
                organization_id=org.id,
                table_id=table.id,
                term_id=term.id,
                linked_by="a",
            )
        )
    for _i in range(open_incidents):
        session.add(
            DataQualityIncident(
                organization_id=org.id,
                datasource_id=table.datasource_id,
                table_id=table.id,
                fingerprint=f"fp-{uuid4().hex[:8]}",
                anomaly_type="ROW_COUNT_DROP",
                severity="HIGH",
                status="OPEN",
                summary="row drop",
                first_observed_at=datetime.now(UTC),
                last_observed_at=datetime.now(UTC),
            )
        )
    # One completed profile so profiles_ran == 1.
    ar = AnalysisRun(
        organization_id=org.id,
        datasource_id=table.datasource_id,
        status="COMPLETED",
    )
    session.add(ar)
    await session.flush()
    session.add(
        TableProfile(
            organization_id=org.id,
            analysis_run_id=ar.id,
            datasource_id=table.datasource_id,
            table_id=table.id,
            sampled_row_count=100,
            status="COMPLETED",
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# compute_certification_evidence: shape correctness on a real fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_evidence_shape(db: AsyncSession) -> None:
    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=2, terms=3, open_incidents=0)

    now = datetime.now(UTC)
    ev = await compute_certification_evidence(
        db, table.id, organization_id=org.id, now=now, certifier_notes="quarterly"
    )
    assert ev["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert ev["description_version_id"] is not None
    UUID(ev["description_version_id"])  # parses
    assert len(ev["ownership_assignment_ids"]) == 2
    assert len(ev["glossary_term_ids"]) == 3
    assert ev["quality_snapshot"]["open_incident_count_at_certify"] == 0
    assert ev["quality_snapshot"]["profiles_ran"] == 1
    assert ev["supporting_dq_check_ids"] == []
    assert ev["certifier_notes"] == "quarterly"


# ---------------------------------------------------------------------------
# Path 1: single-certify populates `evidence`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_certify_table_asset_populates_evidence(db: AsyncSession) -> None:
    from aida.schemas import CertificationDecisionRequest
    from aida.security import SecurityContext
    from atlas.modules.catalog.router import certify_table_asset

    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=1, terms=2)

    ctx = SecurityContext(
        principal_id="steward-a",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )
    body = CertificationDecisionRequest(
        asset_type="TABLE",
        rationale="approved for quarterly reporting",
        expires_at=datetime.now(UTC) + timedelta(days=90),
    )
    result = await certify_table_asset(table_id=table.id, body=body, context=ctx, session=db)
    assert result.evidence is not None
    assert len(result.evidence.ownership_assignment_ids) == 1
    assert len(result.evidence.glossary_term_ids) == 2
    assert result.evidence.certifier_notes == "approved for quarterly reporting"

    row = await db.get(AssetCertification, result.id)
    assert row is not None
    assert row.evidence is not None
    assert row.evidence["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert row.rationale == "approved for quarterly reporting"


# ---------------------------------------------------------------------------
# Path 2: apply_certify_item (direct-write bulk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_apply_certify_item_populates_evidence(db: AsyncSession) -> None:
    from atlas.modules.catalog.service import apply_certify_item

    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=1, terms=1)

    now = datetime.now(UTC)
    ev = await compute_certification_evidence(
        db, table.id, organization_id=org.id, now=now, certifier_notes="bulk"
    )
    new_cert, _ = apply_certify_item(
        table.id,
        tables={table.id: table},
        active_certifications={},
        organization_id=org.id,
        rationale="bulk",
        expires_at=now + timedelta(days=30),
        certified_by="steward-a",
        evidence=ev,
    )
    db.add(new_cert)
    await db.flush()
    row = await db.get(AssetCertification, new_cert.id)
    assert row is not None
    assert row.evidence["ownership_assignment_ids"]
    assert row.evidence["glossary_term_ids"]


# ---------------------------------------------------------------------------
# Path 3: reviewed-bulk (stewardship_service CERTIFY_ASSET)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewed_bulk_certify_populates_evidence(db: AsyncSession) -> None:
    from aida.models import BulkStewardshipOperation
    from aida.stewardship_service import apply_bulk_operation

    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=2, terms=1)

    # BulkStewardshipOperation requires a `governance_review_id` FK, which
    # would be a burdensome fixture; `apply_bulk_operation` only reads the
    # instance's fields, so a stand-in unattached row is enough to exercise
    # the CERTIFY_ASSET branch.
    op = BulkStewardshipOperation(
        organization_id=org.id,
        operation_type="CERTIFY_ASSET",
        subject_type="TABLE",
        subject_ids=[str(table.id)],
        parameters={
            "rationale": "reviewed bulk",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        requested_by="steward-a",
    )
    await apply_bulk_operation(db, operation=op, reviewer="reviewer-b", now=datetime.now(UTC))
    cert = (
        await db.scalars(select(AssetCertification).where(AssetCertification.table_id == table.id))
    ).first()
    assert cert is not None
    assert cert.evidence is not None
    assert len(cert.evidence["ownership_assignment_ids"]) == 2


# ---------------------------------------------------------------------------
# Path 4: playbook auto-apply CERTIFY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playbook_certify_populates_evidence(db: AsyncSession) -> None:
    from aida.models import MetadataPlaybook
    from aida.playbooks import _apply_one_item
    from aida.security import SecurityContext

    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=1, terms=2)

    pb = MetadataPlaybook(
        organization_id=org.id,
        name="cert-auto",
        action="CERTIFY",
        datasource_id=table.datasource_id,
        action_parameters={
            "rationale": "auto-cert per playbook",
            "expires_after_days": 30,
        },
        match_field="TABLE_NAME",
        match_pattern="%",
        schedule_interval_minutes=60,
        created_by="steward-a",
    )
    db.add(pb)
    await db.flush()
    ctx = SecurityContext(
        principal_id="playbook",
        principal_type="SERVICE",
        organization_id=org.id,
        roles=frozenset({"PlatformAdmin"}),
    )
    await _apply_one_item(
        db,
        pb,
        table.id,
        applied_by="playbook",
        context=ctx,
        tables={table.id: table},
        existing_tags={},
        existing_assignments={},
        active_certifications={},
        columns={},
        now=datetime.now(UTC),
    )
    cert = (
        await db.scalars(select(AssetCertification).where(AssetCertification.table_id == table.id))
    ).first()
    assert cert is not None
    assert cert.evidence is not None
    assert cert.evidence["schema_version"] == EVIDENCE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# CatalogRowRead.certification_evidence_summary projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_row_evidence_summary_present_and_null(db: AsyncSession) -> None:
    from atlas.modules.catalog.service import _certification_state

    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=1, terms=1)
    now = datetime.now(UTC)
    ev = await compute_certification_evidence(
        db, table.id, organization_id=org.id, now=now, certifier_notes="c"
    )
    cert = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="c",
        certified_by="steward-a",
        expires_at=now + timedelta(days=30),
        evidence=ev,
    )
    db.add(cert)
    await db.flush()

    state, _exp, summary = _certification_state(cert, now=now)
    assert state == "CERTIFIED"
    assert summary is not None
    assert summary["active_owner_count"] == 1
    assert summary["glossary_term_count"] == 1
    assert summary["backfilled"] is False

    # Legacy row (evidence IS NULL) still projects, with null summary.
    legacy = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="legacy",
        certified_by="steward-a",
        expires_at=now + timedelta(days=30),
        evidence=None,
    )
    state2, _e2, s2 = _certification_state(legacy, now=now)
    assert state2 == "CERTIFIED"
    assert s2 is None


# ---------------------------------------------------------------------------
# Backfill helper: populates NULL rows, tags `backfilled=true`; idempotent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_certification_evidence(db: AsyncSession) -> None:
    org, table = await _seed_estate(db)
    await _seed_evidence_context(db, org, table, owners=1, terms=1)

    now = datetime.now(UTC)
    legacy = AssetCertification(
        organization_id=org.id,
        table_id=table.id,
        asset_type="TABLE",
        status="ACTIVE",
        rationale="pre-P3-09",
        certified_by="steward-a",
        expires_at=now + timedelta(days=30),
        evidence=None,
    )
    db.add(legacy)
    await db.flush()

    n = await backfill_certification_evidence_v1(db)
    assert n == 1
    # The backfill only *stages* the writes -- its docstring makes the caller
    # responsible for committing, so a dry-run CLI can count without writing.
    await db.flush()
    await db.refresh(legacy)
    assert legacy.evidence is not None
    assert legacy.evidence["backfilled"] is True

    # Second run must be a no-op (evidence IS NULL filter).
    n2 = await backfill_certification_evidence_v1(db)
    assert n2 == 0


# ---------------------------------------------------------------------------
# summarize_evidence: shape passthrough for a rich blob; None on None.
# ---------------------------------------------------------------------------


def test_summarize_evidence_none_returns_none() -> None:
    assert summarize_evidence(None) is None


def test_summarize_evidence_folds_counts() -> None:
    ev = {
        "description_version_id": "d0000000-0000-0000-0000-000000000000",
        "ownership_assignment_ids": ["a", "b"],
        "quality_snapshot": {"open_incident_count_at_certify": 3},
        "glossary_term_ids": ["t1", "t2", "t3", "t4"],
    }
    s = summarize_evidence(ev)
    assert s is not None
    assert s["active_owner_count"] == 2
    assert s["open_incident_count_at_certify"] == 3
    assert s["glossary_term_count"] == 4
    assert s["backfilled"] is False
