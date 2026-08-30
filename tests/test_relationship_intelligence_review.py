"""RL-4 / RL-5 / RL-6 / RL-7: relationship-intelligence review, calibration, and
the fix that lets an approved/rejected candidate actually reach Neo4j projection.

Module 06 (`Docs/20-modules/06-relationship-intelligence.md`) SS7 requires
inspectable evidence and SS14 tracks four open items exercised here:

* RL-4 -- `decide_relationship_candidate` previously emitted a single
  `relationship_candidate.decided.v1` event that `graph_projector.run_projector`
  never listened for (it listens for `.approved.v1` / `.rejected.v1`), and the
  payload never carried a `datasource_id` either, so `project_unified_lineage`
  had no way to know which datasource's graph to rebuild even if it had fired.
  Both halves are exercised directly against the real event-dispatch and
  payload-extraction code the projector runs, without a live Kafka/Neo4j.
* RL-5 -- cross-source candidate discovery now matches columns by
  `canonical_column_name` and `physical_type_family` instead of raw
  `.lower()` equality, so naming-convention and dialect-spelling differences
  no longer hide a real candidate.
* RL-6 -- the new bulk-decision endpoint, by explicit id list and by filter,
  with the same maker-checker/PENDING-only rules and partial-success
  reporting as the single-candidate endpoint.
* RL-7 -- the new confidence-calibration endpoint: bucketed observed
  approval rate from real decision history, with an optional
  `RelationshipCandidateGroundTruthLabel` override.
"""

import itertools
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import get_settings
from aida.db import Base
from aida.intelligence_api import (
    _relationship_candidate_decision_event_type,
    _relationship_candidate_decision_payload,
    bulk_decide_relationship_candidates,
    decide_relationship_candidate,
    discover_cross_source_relationship_candidates,
    get_relationship_candidate_confidence_calibration,
)
from aida.models import (
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
    RelationshipCandidate,
    RelationshipCandidateGroundTruthLabel,
)
from aida.projectors.graph_projector import (
    UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES,
    _event_datasource_id,
)
from aida.relationship_naming import canonical_column_name, physical_type_family
from aida.schemas import (
    CrossSourceRelationshipCandidateDiscoveryRequest,
    RelationshipCandidateBulkDecisionRequest,
    RelationshipCandidateBulkSelectionFilter,
    RelationshipCandidateDecision,
)
from aida.security_types import SecurityContext

# `AuditEvent.id` is a `BigInteger` autoincrement primary key, relying in
# production on Postgres's own identity/sequence generation. sqlite only
# auto-populates a bare `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger`
# compiles to `BIGINT`, which sqlite does not treat as that alias -- so an
# in-memory sqlite session (as used by every test below) leaves `id` NULL and
# violates the NOT NULL constraint on insert. Every `record_audit()` call in
# the endpoints under test hits this, so assign ids by hand for this test
# module's sqlite engine only; nothing about the production model changes.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


# --- fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _lob(session: AsyncSession, org: Organization) -> LineOfBusiness:
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code=f"RB{uuid4().hex[:6]}")
    session.add(lob)
    await session.flush()
    return lob


async def _domain(
    session: AsyncSession, org: Organization, lob: LineOfBusiness
) -> DataDomain:
    domain = DataDomain(
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Retail Domain",
        code=f"D{uuid4().hex[:6]}",
    )
    session.add(domain)
    await session.flush()
    return domain


async def _project(
    session: AsyncSession, org: Organization, lob: LineOfBusiness, domain: DataDomain
) -> Project:
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="proj",
        slug=f"proj-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    return project


async def _datasource(
    session: AsyncSession,
    org: Organization,
    lob: LineOfBusiness,
    domain: DataDomain,
    project: Project,
    *,
    name: str,
) -> DataSource:
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=name,
        connector_type="POSTGRES",
        dialect="postgres",
        environment="PROD",
        credential_reference=f"secret://{name}",
    )
    session.add(datasource)
    await session.flush()
    return datasource


async def _table_with_column(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    *,
    table_name: str,
    column_name: str,
    physical_type: str,
) -> tuple[MetadataTable, MetadataColumn]:
    catalog = MetadataCatalog(
        organization_id=org.id,
        datasource_id=datasource.id,
        name=f"cat-{uuid4().hex[:6]}",
        fingerprint="f" * 8,
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=org.id,
        catalog_id=catalog.id,
        name=f"schema-{uuid4().hex[:6]}",
        fingerprint="f" * 8,
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=table_name,
        object_type="TABLE",
        fingerprint="f" * 8,
    )
    session.add(table)
    await session.flush()
    column = MetadataColumn(
        organization_id=org.id,
        table_id=table.id,
        name=column_name,
        ordinal_position=1,
        physical_type=physical_type,
        nullable=False,
        fingerprint="f" * 8,
    )
    session.add(column)
    await session.flush()
    return table, column


def _context(org: Organization, principal: str = "steward") -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )


async def _seed_candidate(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    *,
    created_by: str = "maker",
    confidence: float = 0.9,
    status: str = "PENDING",
) -> RelationshipCandidate:
    source_table, source_column = await _table_with_column(
        session,
        org,
        datasource,
        table_name=f"src_{uuid4().hex[:6]}",
        column_name="customer_id",
        physical_type="INTEGER",
    )
    target_table, target_column = await _table_with_column(
        session,
        org,
        datasource,
        table_name=f"tgt_{uuid4().hex[:6]}",
        column_name="customer_id",
        physical_type="INTEGER",
    )
    candidate = RelationshipCandidate(
        organization_id=org.id,
        datasource_id=datasource.id,
        target_datasource_id=datasource.id,
        source_table_id=source_table.id,
        source_column_id=source_column.id,
        target_table_id=target_table.id,
        target_column_id=target_column.id,
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=confidence,
        evidence={"column_name_match": "EXACT"},
        created_by=created_by,
        status=status,
    )
    session.add(candidate)
    await session.flush()
    return candidate


# ---------------------------------------------------------------------------
# RL-4: the event-name/payload mismatch that silently dropped every approved
# or rejected candidate from Neo4j unified-lineage projection.
# ---------------------------------------------------------------------------


def test_decision_event_types_match_exactly_what_the_projector_listens_for() -> None:
    """The literal regression this bug was: two modules each hardcoded a name
    and they drifted. Assert both names directly against the projector's own
    named constant, not just against each other's copy of the string.
    """
    approved = _relationship_candidate_decision_event_type("APPROVED")
    rejected = _relationship_candidate_decision_event_type("REJECTED")
    assert approved == "relationship_candidate.approved.v1"
    assert rejected == "relationship_candidate.rejected.v1"
    assert approved in UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES
    assert rejected in UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES


async def test_decide_relationship_candidate_emits_a_projectable_event(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")
    candidate = await _seed_candidate(session, org, datasource)

    result = await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=_context(org),
        session=session,
    )
    assert result.status == "APPROVED"

    outbox_rows = (await session.scalars(select(OutboxEvent))).all()
    assert len(outbox_rows) == 1
    event = outbox_rows[0]
    assert event.event_type == "relationship_candidate.approved.v1"
    assert event.event_type in UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES
    assert event.organization_id == org.id
    assert event.payload["datasource_id"] == str(datasource.id)

    # The exact shape `outbox_publisher.py` puts on the wire (event_id,
    # event_type, organization_id, payload) -- see
    # `aida/projectors/outbox_publisher.py`. If `_event_datasource_id` cannot
    # resolve a datasource_id from this envelope, `project_unified_lineage`
    # silently no-ops even with the event_type match fixed.
    wire_event = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "organization_id": str(event.organization_id),
        "payload": event.payload,
    }
    resolved_datasource_id = await _event_datasource_id(wire_event)
    assert resolved_datasource_id == datasource.id


async def test_rejecting_a_candidate_emits_the_reject_sibling_event(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")
    candidate = await _seed_candidate(session, org, datasource)

    await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="REJECT", reason="not a real key"),
        context=_context(org),
        session=session,
    )
    event = (await session.scalars(select(OutboxEvent))).one()
    assert event.event_type == "relationship_candidate.rejected.v1"


def test_decision_payload_carries_both_datasource_ids() -> None:
    candidate = RelationshipCandidate(
        id=uuid4(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        target_datasource_id=uuid4(),
        source_table_id=uuid4(),
        source_column_id=uuid4(),
        target_table_id=uuid4(),
        target_column_id=uuid4(),
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.9,
        status="APPROVED",
        created_by="maker",
    )
    payload = _relationship_candidate_decision_payload(candidate)
    assert payload["datasource_id"] == str(candidate.datasource_id)
    assert payload["target_datasource_id"] == str(candidate.target_datasource_id)
    assert payload["candidate_id"] == str(candidate.id)
    assert payload["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# RL-5: naming/type normalization for cross-source discovery.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["customer_id", "customerId", "CustomerId", "CUSTOMER_ID", "Customer_Id"],
)
def test_canonical_column_name_collapses_naming_conventions(name: str) -> None:
    assert canonical_column_name(name) == "customer_id"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("NUMBER(38,0)", "INT64"),
        ("integer", "BIGINT"),
        ("NUMERIC(10,2)", "decimal"),
        ("VARCHAR(255)", "text"),
        ("TIMESTAMP", "datetime"),
        ("boolean", "BIT"),
    ],
)
def test_physical_type_family_matches_across_dialect_spellings(a: str, b: str) -> None:
    assert physical_type_family(a) == physical_type_family(b)


def test_physical_type_family_does_not_conflate_different_families() -> None:
    assert physical_type_family("INTEGER") != physical_type_family("VARCHAR(50)")
    assert physical_type_family("TIMESTAMP") != physical_type_family("INTEGER")


async def test_cross_source_discovery_matches_camel_case_against_snake_case(
    session: AsyncSession,
) -> None:
    """The exact scenario the pre-RL-5 `.lower()`-only match could not survive:
    the same logical key spelled `customerId` in one datasource's PK and
    `customer_id` in another's FK-shaped column, with an Oracle-style
    `NUMBER(38,0)` type on one side and BigQuery's `INT64` on the other.
    """
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    ds_pk = await _datasource(session, org, lob, domain, project, name="oracle-core")
    ds_fk = await _datasource(session, org, lob, domain, project, name="bq-analytics")

    pk_table, pk_column = await _table_with_column(
        session, org, ds_pk, table_name="customers", column_name="customerId",
        physical_type="NUMBER(38,0)",
    )
    constraint = MetadataConstraint(
        organization_id=org.id,
        datasource_id=ds_pk.id,
        table_id=pk_table.id,
        name="pk_customers",
        constraint_type="PRIMARY_KEY",
        columns=["customerId"],
        fingerprint="f" * 8,
    )
    session.add(constraint)
    await session.flush()

    _fk_table, fk_column = await _table_with_column(
        session, org, ds_fk, table_name="orders", column_name="customer_id",
        physical_type="INT64",
    )

    context = _context(org)
    page = await discover_cross_source_relationship_candidates(
        domain.id,
        CrossSourceRelationshipCandidateDiscoveryRequest(),
        context=context,
        session=session,
        settings=get_settings(),
    )
    assert page.total == 1
    created = page.items[0]
    assert created.source_column_id == fk_column.id
    assert created.target_column_id == pk_column.id
    # Evidence tells the reviewer this was a canonical/family match, not a
    # literal exact one -- SS7's "evidence must be inspectable" requirement.
    assert created.evidence["column_name_match"] == "CANONICAL"
    assert created.evidence["physical_type_match"] == "FAMILY"
    assert created.confidence < 0.75


# ---------------------------------------------------------------------------
# RL-6: bulk review.
# ---------------------------------------------------------------------------


async def test_bulk_decide_by_explicit_ids_reports_partial_success(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")

    approvable = await _seed_candidate(session, org, datasource, created_by="maker-a")
    own_candidate = await _seed_candidate(session, org, datasource, created_by="reviewer")
    already_decided = await _seed_candidate(
        session, org, datasource, created_by="maker-b", status="APPROVED"
    )

    context = SecurityContext(
        principal_id="reviewer", principal_type="USER", organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )
    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[approvable.id, own_candidate.id, already_decided.id],
            decision="APPROVE",
        ),
        context=context,
        session=session,
    )
    assert result.requested_count == 3
    assert result.succeeded_count == 1
    assert result.failed_count == 2
    by_id = {item.candidate_id: item for item in result.results}
    assert by_id[str(approvable.id)].status == "SUCCEEDED"
    assert by_id[str(own_candidate.id)].status == "FAILED"
    assert "own candidate" in (by_id[str(own_candidate.id)].reason or "")
    assert by_id[str(already_decided.id)].status == "FAILED"

    await session.refresh(approvable)
    assert approvable.status == "APPROVED"
    events = (await session.scalars(select(OutboxEvent))).all()
    assert len(events) == 1
    assert events[0].event_type == "relationship_candidate.approved.v1"


async def test_bulk_decide_by_filter_only_selects_pending_and_respects_cap(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")

    pending = [
        await _seed_candidate(session, org, datasource, created_by="maker") for _ in range(3)
    ]
    await _seed_candidate(session, org, datasource, created_by="maker", status="REJECTED")

    context = _context(org)
    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            filter=RelationshipCandidateBulkSelectionFilter(datasource_id=datasource.id),
            decision="APPROVE",
        ),
        context=context,
        session=session,
    )
    assert result.selection_mode == "FILTER"
    assert result.requested_count == 3
    assert result.succeeded_count == 3
    assert not result.truncated
    for candidate in pending:
        await session.refresh(candidate)
        assert candidate.status == "APPROVED"


async def test_bulk_decide_rejects_without_a_reason(session: AsyncSession) -> None:
    with pytest.raises(Exception, match="reason is required"):
        RelationshipCandidateBulkDecisionRequest(candidate_ids=[uuid4()], decision="REJECT")


def test_bulk_decide_requires_exactly_one_selection_source() -> None:
    with pytest.raises(Exception, match="exactly one selection"):
        RelationshipCandidateBulkDecisionRequest(decision="APPROVE")
    with pytest.raises(Exception, match="exactly one selection"):
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[uuid4()],
            filter=RelationshipCandidateBulkSelectionFilter(datasource_id=uuid4()),
            decision="APPROVE",
        )


async def test_bulk_decide_cross_organization_candidate_is_reported_failed(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    other_org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")
    candidate = await _seed_candidate(session, org, datasource, created_by="maker")

    context = SecurityContext(
        principal_id="reviewer",
        principal_type="USER",
        organization_id=other_org.id,
        roles=frozenset({"DataSteward"}),
    )
    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(candidate_ids=[candidate.id], decision="APPROVE"),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 0
    assert result.results[0].status == "FAILED"
    assert "organization" in (result.results[0].reason or "")


# ---------------------------------------------------------------------------
# RL-7: confidence calibration.
# ---------------------------------------------------------------------------


async def test_calibration_buckets_observed_approval_rate_by_confidence(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")

    # bucket [0.9, 1.0): two approved, one rejected -> 2/3 observed rate.
    await _seed_candidate(session, org, datasource, confidence=0.95, status="APPROVED")
    await _seed_candidate(session, org, datasource, confidence=0.91, status="APPROVED")
    await _seed_candidate(session, org, datasource, confidence=0.99, status="REJECTED")
    # bucket [0.5, 0.6): one rejected -> 0/1.
    await _seed_candidate(session, org, datasource, confidence=0.55, status="REJECTED")
    # still PENDING -- must not count anywhere.
    await _seed_candidate(session, org, datasource, confidence=0.95, status="PENDING")

    context = SecurityContext(
        principal_id="auditor", principal_type="USER", organization_id=org.id,
        roles=frozenset({"Auditor"}),
    )
    calibration = await get_relationship_candidate_confidence_calibration(
        datasource_id=datasource.id, bucket_width=0.1, context=context, session=session
    )
    assert calibration.total_decided == 4
    # Explicitly denies being a published, externally-validated curve rather
    # than silently omitting the disclaimer -- the honesty requirement is
    # that this distinction is stated, not that the phrase never appears.
    assert "not a published calibration curve" in calibration.methodology_note
    assert "no such corpus exists" in calibration.methodology_note

    high_bucket = next(b for b in calibration.buckets if b.confidence_low == pytest.approx(0.9))
    assert high_bucket.decided_count == 3
    assert high_bucket.approved_count == 2
    assert high_bucket.rejected_count == 1
    assert high_bucket.observed_approval_rate == pytest.approx(2 / 3)

    mid_bucket = next(b for b in calibration.buckets if b.confidence_low == pytest.approx(0.5))
    assert mid_bucket.decided_count == 1
    assert mid_bucket.observed_approval_rate == 0.0

    empty_bucket = next(b for b in calibration.buckets if b.confidence_low == pytest.approx(0.0))
    assert empty_bucket.decided_count == 0
    assert empty_bucket.observed_approval_rate is None


async def test_calibration_ground_truth_override_supersedes_original_decision(
    session: AsyncSession,
) -> None:
    """RL-7's optional additive extra: a later, stronger signal overrides the
    steward's original call for calibration math only -- the candidate's own
    `status` (the steward's actual decision, negative-knowledge included)
    is untouched.
    """
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")

    candidate = await _seed_candidate(session, org, datasource, confidence=0.95, status="REJECTED")
    session.add(
        RelationshipCandidateGroundTruthLabel(
            organization_id=org.id,
            candidate_id=candidate.id,
            label="APPROVED",
            source="labelled_corpus_v1",
            rationale="confirmed by downstream usage",
            created_by="corpus-importer",
        )
    )
    await session.flush()

    context = SecurityContext(
        principal_id="auditor", principal_type="USER", organization_id=org.id,
        roles=frozenset({"Auditor"}),
    )
    calibration = await get_relationship_candidate_confidence_calibration(
        datasource_id=datasource.id, bucket_width=0.1, context=context, session=session
    )
    assert calibration.ground_truth_overrides_applied == 1
    bucket = next(b for b in calibration.buckets if b.confidence_low == pytest.approx(0.9))
    assert bucket.approved_count == 1
    assert bucket.rejected_count == 0

    # The original steward decision on the candidate itself is untouched.
    await session.refresh(candidate)
    assert candidate.status == "REJECTED"


async def test_calibration_rejects_cross_organization_datasource(session: AsyncSession) -> None:
    org = await _org(session)
    other_org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project, name="core-banking")

    context = SecurityContext(
        principal_id="auditor",
        principal_type="USER",
        organization_id=other_org.id,
        roles=frozenset({"Auditor"}),
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_relationship_candidate_confidence_calibration(
            datasource_id=datasource.id, bucket_width=0.1, context=context, session=session
        )
    assert exc_info.value.status_code == 403
