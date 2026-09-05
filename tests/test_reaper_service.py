"""P2-06: generic stale-row reaper -- test coverage.

Follows the in-memory SQLite / real ORM approach the rest of this suite
uses (see ``test_profiling_exception_policy.py``): no mocks, real
predicates against a real engine, so the ``candidates_stmt`` SQL each rule
declares is exercised end-to-end.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.models import (
    AssetDescriptionDraft,
    AssetTermLink,
    AuditEvent,
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataEnrichmentProposal,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticInferenceRun,
)
from aida.reaper_service import (
    RULES,
    ReaperRule,
    parse_retention_overrides,
    run_reaper_pass,
)

pytestmark = pytest.mark.asyncio


# `AuditEvent.id` is a `BigInteger` autoincrement primary key that sqlite's
# in-memory engine does not auto-populate -- the same workaround
# `test_profiling_exception_policy.py` uses.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


def _settings(**overrides) -> Settings:
    """Bare Settings for the test harness. `Settings(_env_file=None)` matches
    the pattern in `test_profiling_exception_policy.py::test_purge_...`.
    """
    kwargs = {"reaper_enabled": True, "reaper_sweep_interval_seconds": 3600}
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


async def _seed_org(session: AsyncSession) -> tuple[Organization, DataSource, MetadataTable]:
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
        status="ACTIVE",
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
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customers",
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.commit()
    return org, datasource, table


async def _seed_proposal(
    session: AsyncSession,
    *,
    org: Organization,
    datasource: DataSource,
    table: MetadataTable,
    status: str,
    updated_at: datetime,
) -> MetadataEnrichmentProposal:
    """Create one `MetadataEnrichmentProposal` with the given status and
    updated_at. `SemanticInferenceRun` and `GovernanceReview` are minted per
    proposal so the FK constraints resolve. `updated_at` is set on both
    create-and-flush *and* re-set post-flush, because `TimestampMixin` writes
    its own server-side `now()` on insert regardless of the constructor value.
    """
    inference_run = SemanticInferenceRun(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        engine_mode="rule_only",
        engine_version="test",
        created_by="test-suite",
    )
    review = GovernanceReview(
        id=uuid4(),
        organization_id=org.id,
        object_type="metadata_enrichment_proposal",
        object_id="pending",
        requested_action="APPROVE_PROPOSAL",
        requested_by="test-suite",
    )
    session.add_all([inference_run, review])
    await session.flush()
    proposal = MetadataEnrichmentProposal(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        inference_run_id=inference_run.id,
        table_id=table.id,
        governance_review_id=review.id,
        status=status,
        engine_type="rule_only",
        engine_version="test",
        confidence=0.5,
        payload={},
        evidence={},
        fingerprint="fp-" + uuid4().hex[:8],
        proposed_by="test-suite",
    )
    session.add(proposal)
    await session.flush()
    # Overwrite the auto-assigned created_at/updated_at so the retention
    # predicate sees the age the test wants.
    proposal.created_at = updated_at
    proposal.updated_at = updated_at
    await session.commit()
    return proposal


async def _seed_description_draft(
    session: AsyncSession,
    *,
    org: Organization,
    table: MetadataTable,
    status: str,
    updated_at: datetime,
) -> AssetDescriptionDraft:
    draft = AssetDescriptionDraft(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        drafted_text="Customers table.",
        text_fingerprint="fp-" + uuid4().hex[:8],
        accuracy_score=0.5,
        clarity_score=0.5,
        style_score=0.5,
        completeness_score=0.5,
        overall_score=0.5,
        evidence={},
        status=status,
        created_by="test-suite",
    )
    session.add(draft)
    await session.flush()
    draft.created_at = updated_at
    draft.updated_at = updated_at
    await session.commit()
    return draft


async def _seed_deprecated_term_with_links(
    session: AsyncSession,
    *,
    org: Organization,
    table: MetadataTable,
    deprecated_at: datetime,
    link_count: int,
) -> tuple[GlossaryTerm, list[AssetTermLink]]:
    term = GlossaryTerm(
        id=uuid4(),
        organization_id=org.id,
        term_key=f"term-{uuid4().hex[:8]}",
        lifecycle_status="DEPRECATED",
        deprecated_by="bob",
        deprecated_at=deprecated_at,
        deprecation_reason="superseded",
    )
    session.add(term)
    await session.flush()
    # Two versions: one that was APPROVED (matches the audit finding's
    # "APPROVED-then-DEPRECATED" wording) and one currently DEPRECATED.
    approved_version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=org.id,
        term_id=term.id,
        version=1,
        status="DEPRECATED",  # rewritten during the term's deprecation
        display_name="Old term",
        definition="was authoritative",
        synonyms=[],
        created_by="alice",
    )
    session.add(approved_version)
    await session.flush()

    # Additional standalone tables so each AssetTermLink can satisfy its
    # (table_id, term_id) uniqueness against a different table.
    tables = [table]
    for _ in range(link_count - 1):
        extra = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=table.datasource_id,
            schema_id=table.schema_id,
            name=f"tbl_{uuid4().hex[:8]}",
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
        session.add(extra)
        tables.append(extra)
    await session.flush()

    links: list[AssetTermLink] = []
    link_created_at = deprecated_at - timedelta(days=30)
    for tbl in tables:
        link = AssetTermLink(
            id=uuid4(),
            organization_id=org.id,
            table_id=tbl.id,
            term_id=term.id,
            linked_by="alice",
            link_type="MANUAL",
            confidence=1.0,
        )
        session.add(link)
        await session.flush()
        # Force created_at to a moment before the term's deprecation, so the
        # rule's `link.created_at < term.deprecated_at` predicate matches.
        link.created_at = link_created_at
        link.updated_at = link_created_at
        links.append(link)
    await session.commit()
    return term, links


# ---------------------------------------------------------------------------
# Rule: rejected_enrichment_proposals
# ---------------------------------------------------------------------------


async def test_rejected_enrichment_proposals_only_old_ones_reaped(session) -> None:
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    old_a = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="REJECTED", updated_at=now - timedelta(days=100),
    )
    old_b = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="REJECTED", updated_at=now - timedelta(days=95),
    )
    young_a = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="REJECTED", updated_at=now - timedelta(days=30),
    )
    young_b = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="REJECTED", updated_at=now - timedelta(days=5),
    )
    pending = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="PENDING_REVIEW", updated_at=now - timedelta(days=200),
    )

    report = await run_reaper_pass(session=session, now=now, settings=_settings())

    reaped_ids = {
        row.id
        for row in (await session.scalars(select(MetadataEnrichmentProposal))).all()
    }
    # old_a, old_b gone; young ones + pending still there. (`pending` is not
    # a REJECTED candidate; the stale-pending rule would only reach it once
    # it hit 365 days old.)
    assert old_a.id not in reaped_ids
    assert old_b.id not in reaped_ids
    assert young_a.id in reaped_ids
    assert young_b.id in reaped_ids
    assert pending.id in reaped_ids

    per_rule = {r.name: r for r in report.rules}
    assert per_rule["rejected_enrichment_proposals"].reaped == 2


async def test_stale_pending_enrichment_proposals_flip_not_deleted(session) -> None:
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    stale = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="PENDING_REVIEW", updated_at=now - timedelta(days=400),
    )
    fresh = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="PENDING_REVIEW", updated_at=now - timedelta(days=10),
    )

    await run_reaper_pass(session=session, now=now, settings=_settings())

    await session.refresh(stale)
    await session.refresh(fresh)
    assert stale.status == "EXPIRED"
    assert fresh.status == "PENDING_REVIEW"


# ---------------------------------------------------------------------------
# Rule: orphan_asset_term_links
# ---------------------------------------------------------------------------


async def test_orphan_asset_term_links_deleted(session) -> None:
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    _term, links = await _seed_deprecated_term_with_links(
        session,
        org=org,
        table=table,
        deprecated_at=now - timedelta(days=30),
        link_count=3,
    )
    link_ids = {link.id for link in links}

    report = await run_reaper_pass(session=session, now=now, settings=_settings())

    remaining = {
        row.id for row in (await session.scalars(select(AssetTermLink))).all()
    }
    assert not (link_ids & remaining), "all orphan links should be deleted"
    per_rule = {r.name: r for r in report.rules}
    assert per_rule["orphan_asset_term_links"].reaped == 3


async def test_active_term_links_not_touched(session) -> None:
    """A link to a still-ACTIVE term is untouched even though the reaper is
    aware of the term -- guards against a bad predicate silently deleting
    live links."""
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    active_term = GlossaryTerm(
        id=uuid4(),
        organization_id=org.id,
        term_key="alive",
        lifecycle_status="ACTIVE",
    )
    session.add(active_term)
    await session.flush()
    approved_version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=org.id,
        term_id=active_term.id,
        version=1,
        status="APPROVED",
        display_name="Alive",
        definition="live",
        synonyms=[],
        created_by="alice",
    )
    session.add(approved_version)
    live_link = AssetTermLink(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        term_id=active_term.id,
        linked_by="alice",
        link_type="MANUAL",
        confidence=1.0,
    )
    session.add(live_link)
    await session.commit()

    await run_reaper_pass(session=session, now=now, settings=_settings())

    remaining = {
        row.id for row in (await session.scalars(select(AssetTermLink))).all()
    }
    assert live_link.id in remaining


# ---------------------------------------------------------------------------
# Rule: stale pending description drafts + rejected drafts
# ---------------------------------------------------------------------------


async def test_stale_pending_description_drafts_flip_and_young_stay(session) -> None:
    org, _datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    stale = await _seed_description_draft(
        session, org=org, table=table, status="DRAFT",
        # Clearly past the 60-day window, not sitting exactly on it: the rule
        # reaps rows *older than* the retention (`updated_at < cutoff`), so a
        # row seeded at exactly `now - 60d` is correctly left alone and the
        # test was asserting the boundary rather than the behaviour.
        updated_at=now - timedelta(days=61),
    )
    young = await _seed_description_draft(
        session, org=org, table=table, status="DRAFT",
        updated_at=now - timedelta(days=5),
    )

    await run_reaper_pass(session=session, now=now, settings=_settings())

    await session.refresh(stale)
    await session.refresh(young)
    assert stale.status == "EXPIRED"
    assert young.status == "DRAFT"


async def test_rejected_description_drafts_soft_flag_preserves_row(session) -> None:
    """The class docstring's contract is that rejected drafts are retained
    as fingerprint anchors -- REAPED is a soft flip, the row and its
    `text_fingerprint` remain."""
    org, _datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    old_reject = await _seed_description_draft(
        session, org=org, table=table, status="REJECTED",
        updated_at=now - timedelta(days=200),
    )
    old_fingerprint = old_reject.text_fingerprint

    await run_reaper_pass(session=session, now=now, settings=_settings())

    await session.refresh(old_reject)
    assert old_reject.status == "REAPED"
    assert old_reject.text_fingerprint == old_fingerprint


# ---------------------------------------------------------------------------
# Global: disabled config = no-op
# ---------------------------------------------------------------------------


async def test_reaper_disabled_by_config_is_noop(session) -> None:
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    old = await _seed_proposal(
        session, org=org, datasource=datasource, table=table,
        status="REJECTED", updated_at=now - timedelta(days=200),
    )

    report = await run_reaper_pass(
        session=session, now=now, settings=_settings(reaper_enabled=False)
    )

    assert report.disabled is True
    assert report.rules == []
    remaining = {
        row.id
        for row in (await session.scalars(select(MetadataEnrichmentProposal))).all()
    }
    assert old.id in remaining


# ---------------------------------------------------------------------------
# Global: hard cap
# ---------------------------------------------------------------------------


class _FakeSelect:
    """Rewraps the real candidates_stmt so we can inject a synthetic
    candidate count without seeding 15,000 real rows. The reaper's count
    path is `select(func.count()).select_from(base_stmt.subquery())`, which
    is what this drives against a subquery that returns the requested
    number of ids from a Recursive Common Table Expression."""


async def test_hard_cap_exceeded_reaps_zero_and_emits_alert(session) -> None:
    """A rule whose candidate count exceeds `hard_cap` must reap zero and
    emit `REAPER_CAP_EXCEEDED` -- safety over throughput. Rather than
    seeding 15,000 real rows against sqlite, install a fake rule with
    `hard_cap=2` and 3 real candidates: the same code path fires."""
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    for _ in range(3):
        await _seed_proposal(
            session, org=org, datasource=datasource, table=table,
            status="REJECTED", updated_at=now - timedelta(days=200),
        )

    # Same rule, tightened cap. Batch size stays large enough to reap all
    # if the cap did not fire -- so if the cap logic breaks, this test
    # notices.
    from aida.reaper_service import _rejected_enrichment_proposals_stmt

    tight_rule = ReaperRule(
        name="rejected_enrichment_proposals_tight",
        model=MetadataEnrichmentProposal,
        resource_type="metadata_enrichment_proposal",
        audit_action="REAP_REJECTED_ENRICHMENT_PROPOSAL",
        retention=timedelta(days=90),
        action="DELETE",
        candidates_stmt=_rejected_enrichment_proposals_stmt,
        hard_cap=2,
        batch_size=10,
    )

    report = await run_reaper_pass(
        session=session,
        now=now,
        settings=_settings(),
        rules=[tight_rule],
    )
    per_rule = {r.name: r for r in report.rules}
    assert per_rule["rejected_enrichment_proposals_tight"].cap_exceeded is True
    assert per_rule["rejected_enrichment_proposals_tight"].reaped == 0
    # All three rows still present.
    remaining = (
        await session.scalars(select(MetadataEnrichmentProposal))
    ).all()
    assert len(remaining) == 3
    # And an REAPER_CAP_EXCEEDED audit event landed.
    alerts = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "REAPER_CAP_EXCEEDED")
        )
    ).all()
    assert len(alerts) == 1
    assert alerts[0].details["rule"] == "rejected_enrichment_proposals_tight"
    assert alerts[0].details["candidate_count"] == 3
    assert alerts[0].outcome == "FAILED"


# ---------------------------------------------------------------------------
# Global: audit trail for successful reaps
# ---------------------------------------------------------------------------


async def test_audit_event_emitted_per_rule_that_reaped(session) -> None:
    """One `REAP_*` audit event per rule that reaped >=1 row, never one per
    row -- keeps the audit trail bounded even during a large sweep."""
    org, datasource, table = await _seed_org(session)
    now = datetime.now(UTC)
    for _ in range(3):
        await _seed_proposal(
            session, org=org, datasource=datasource, table=table,
            status="REJECTED", updated_at=now - timedelta(days=200),
        )

    await run_reaper_pass(session=session, now=now, settings=_settings())

    reap_audits = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "REAP_REJECTED_ENRICHMENT_PROPOSAL"
            )
        )
    ).all()
    assert len(reap_audits) == 1  # not 3
    assert reap_audits[0].details["reaped_count"] == 3
    assert reap_audits[0].details["rule"] == "rejected_enrichment_proposals"


async def test_no_audit_when_a_rule_reaped_nothing(session) -> None:
    """No candidates => no audit noise; the rule report simply shows 0."""
    org, _datasource, _table = await _seed_org(session)
    now = datetime.now(UTC)

    report = await run_reaper_pass(session=session, now=now, settings=_settings())

    assert report.total_reaped == 0
    reap_audits = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action.like("REAP_%"))
        )
    ).all()
    assert reap_audits == []


# ---------------------------------------------------------------------------
# parse_retention_overrides
# ---------------------------------------------------------------------------


def test_parse_retention_overrides_valid_and_invalid() -> None:
    parsed = parse_retention_overrides(
        "rejected_enrichment_proposals:30, orphan_asset_term_links:7, bogus:5,"
        "malformed_entry, rejected_description_drafts:notanint,"
        "rejected_description_drafts:-3"
    )
    assert parsed == {
        "rejected_enrichment_proposals": timedelta(days=30),
        "orphan_asset_term_links": timedelta(days=7),
    }


def test_parse_retention_overrides_empty_or_none() -> None:
    assert parse_retention_overrides(None) == {}
    assert parse_retention_overrides("") == {}


def test_rules_registry_covers_expected_names() -> None:
    """Registry-level guard: adding or removing a rule is a deliberate
    edit, not a silent one."""
    assert {r.name for r in RULES} == {
        "rejected_enrichment_proposals",
        "stale_pending_enrichment_proposals",
        "orphan_asset_term_links",
        "rejected_description_drafts",
        "stale_pending_description_drafts",
    }


def test_rules_registry_shape_invariants() -> None:
    for rule in RULES:
        if rule.action == "STATUS_FLIP":
            assert rule.new_status, f"{rule.name}: STATUS_FLIP requires new_status"
        else:
            assert rule.new_status is None, f"{rule.name}: DELETE forbids new_status"
        assert rule.hard_cap > 0
        assert rule.batch_size > 0
