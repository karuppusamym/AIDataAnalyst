"""N4: lineage proposal / review / negative-knowledge workflow for
``RelationshipCandidate``.

Exercises the real composition described in
`aida.relationship_candidate_review`'s module docstring end to end, against
the actual FastAPI endpoint functions and a real (in-memory sqlite) session
-- not mocks:

* impact-ordering -- a PENDING candidate connecting a busy hub table sorts
  ahead of one connecting two isolated tables, using EA.14's real bounded
  traversal (`build_unified_lineage_impact_payload`), with confidence
  deliberately set backwards from impact so a confidence-based (or
  insertion-order) tiebreak would fail this test.
* bulk decisions -- `bulk_decide_relationship_candidates` (RL-6, unchanged)
  decides a mixed approve/reject batch; an approved candidate's edge is
  provably present in the unified lineage graph (what "approved" already
  does), a rejected one is provably recorded as negative knowledge.
* re-proposal suppression -- a rejected candidate's edge is not
  re-discovered even after the original (now-decided) row is gone, proving
  negative-knowledge is doing real, independent suppression work rather
  than piggy-backing on the discovery loop's own existing-row dedup.
"""

import itertools
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import get_settings
from aida.db import Base
from aida.intelligence_api import (
    bulk_decide_relationship_candidates,
    decide_relationship_candidate,
    discover_relationship_candidates,
    get_relationship_candidate_review_queue,
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
    NegativeAssertionRecord,
    Organization,
    Project,
    RelationshipCandidate,
)
from aida.negative_knowledge import check_re_proposal
from aida.relationship_candidate_review import relationship_candidate_negative_predicate
from aida.schemas import (
    RelationshipCandidateBulkDecisionRequest,
    RelationshipCandidateDecision,
    RelationshipCandidateDiscoveryRequest,
)
from aida.security_types import SecurityContext
from aida.unified_lineage_api import build_unified_lineage_graph_payload

# Same sqlite BigInteger-autoincrement workaround
# `test_relationship_intelligence_review.py` already established -- every
# `record_audit()` call under test needs it.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


# --- fixtures (mirrors test_relationship_intelligence_review.py) -------------


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


async def _domain(session: AsyncSession, org: Organization, lob: LineOfBusiness) -> DataDomain:
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
    name: str = "core-banking",
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
    column_name: str = "id",
    physical_type: str = "INTEGER",
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


async def _seed_edge(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    *,
    source_table: MetadataTable,
    source_column: MetadataColumn,
    target_table: MetadataTable,
    target_column: MetadataColumn,
    status: str = "PENDING",
    confidence: float = 0.9,
    created_by: str = "maker",
) -> RelationshipCandidate:
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
        evidence={
            "signals": [
                {"name": "TARGET_IS_PRIMARY_KEY", "score": confidence, "maximum": confidence,
                 "reason": "test fixture"},
            ]
        },
        created_by=created_by,
        status=status,
    )
    session.add(candidate)
    await session.flush()
    return candidate


# ---------------------------------------------------------------------------
# Impact-ordering: real computed impact, not confidence or insertion order.
# ---------------------------------------------------------------------------


async def test_review_queue_orders_by_real_impact_not_by_confidence(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project)

    # A "hub" table (t_hub) with three APPROVED edges into it -- three real
    # nodes reachable through EA.14's traversal from t_hub.
    hub_table, hub_column = await _table_with_column(
        session, org, datasource, table_name="t_hub", column_name="hub_id"
    )
    spokes = []
    for index in range(3):
        spoke_table, spoke_column = await _table_with_column(
            session, org, datasource, table_name=f"t_spoke_{index}", column_name="spoke_id"
        )
        spokes.append((spoke_table, spoke_column))
        await _seed_edge(
            session,
            org,
            datasource,
            source_table=hub_table,
            source_column=hub_column,
            target_table=spoke_table,
            target_column=spoke_column,
            status="APPROVED",
            confidence=0.9,
        )

    # Two isolated tables with no approved edges at all.
    isolated_a_table, isolated_a_column = await _table_with_column(
        session, org, datasource, table_name="t_isolated_a", column_name="a_id"
    )
    isolated_b_table, isolated_b_column = await _table_with_column(
        session, org, datasource, table_name="t_isolated_b", column_name="b_id"
    )
    # A second isolated pair for the low-impact PENDING candidate's target.
    isolated_c_table, isolated_c_column = await _table_with_column(
        session, org, datasource, table_name="t_isolated_c", column_name="c_id"
    )

    # High-impact PENDING candidate: connects the busy hub to an isolated
    # table. Deliberately LOW confidence so a confidence-based sort would
    # rank it last.
    high_impact = await _seed_edge(
        session,
        org,
        datasource,
        source_table=hub_table,
        source_column=hub_column,
        target_table=isolated_a_table,
        target_column=isolated_a_column,
        status="PENDING",
        confidence=0.55,
        created_by="maker-1",
    )
    # Low-impact PENDING candidate: connects two isolated tables.
    # Deliberately HIGH confidence so a confidence-based sort would rank it
    # first.
    low_impact = await _seed_edge(
        session,
        org,
        datasource,
        source_table=isolated_b_table,
        source_column=isolated_b_column,
        target_table=isolated_c_table,
        target_column=isolated_c_column,
        status="PENDING",
        confidence=0.95,
        created_by="maker-2",
    )

    context = _context(org)
    queue = await get_relationship_candidate_review_queue(
        datasource.id,
        limit=50,
        offset=0,
        context=context,
        session=session,
        settings=get_settings(),
    )

    assert queue.total_pending_count == 2
    assert not queue.truncated
    assert [item.candidate.id for item in queue.items] == [high_impact.id, low_impact.id]

    high_item, low_item = queue.items
    # The hub candidate really does score higher: it sees the three approved
    # spoke edges at its source endpoint.
    assert high_item.impact.impact_score >= 3
    assert high_item.impact.impact_score > low_item.impact.impact_score
    assert low_item.impact.impact_score == 0
    # Confirms the sort key really is impact, not confidence: the winner has
    # the *lower* confidence of the two.
    assert high_item.candidate.confidence < low_item.candidate.confidence

    # Diff-based presentation: "nothing -> this edge" -- every field of the
    # curated snapshot is `added` (SM-7's diff engine, before=None), with
    # AT-15's confidence breakdown carried through.
    diff_fields = {entry.field: entry for entry in high_item.diff}
    assert set(diff_fields) == {
        "source_table",
        "source_column",
        "target_table",
        "target_column",
        "detection_rule",
        "confidence",
        "confidence_signals",
    }
    assert all(entry.change == "added" for entry in diff_fields.values())
    assert diff_fields["source_column"].after == "hub_id"
    assert diff_fields["target_column"].after == "a_id"
    assert diff_fields["confidence_signals"].after == high_impact.evidence["signals"]


# ---------------------------------------------------------------------------
# Bulk decisions: RL-6's real mechanism, unchanged -- correct downstream
# effect per outcome.
# ---------------------------------------------------------------------------


async def test_bulk_decision_approve_becomes_a_real_edge_reject_becomes_negative_knowledge(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project)

    approve_source, approve_source_col = await _table_with_column(
        session, org, datasource, table_name="orders_a", column_name="customer_id"
    )
    approve_target, approve_target_col = await _table_with_column(
        session, org, datasource, table_name="customers_a", column_name="customer_id"
    )
    reject_source, reject_source_col = await _table_with_column(
        session, org, datasource, table_name="orders_b", column_name="customer_id"
    )
    reject_target, reject_target_col = await _table_with_column(
        session, org, datasource, table_name="customers_b", column_name="customer_id"
    )

    to_approve = await _seed_edge(
        session, org, datasource,
        source_table=approve_source, source_column=approve_source_col,
        target_table=approve_target, target_column=approve_target_col,
        status="PENDING", created_by="maker",
    )
    to_reject = await _seed_edge(
        session, org, datasource,
        source_table=reject_source, source_column=reject_source_col,
        target_table=reject_target, target_column=reject_target_col,
        status="PENDING", created_by="maker",
    )

    context = _context(org, principal="reviewer")
    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[to_approve.id], decision="APPROVE"
        ),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 1
    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[to_reject.id], decision="REJECT", reason="not a real key"
        ),
        context=context,
        session=session,
    )
    assert result.succeeded_count == 1

    await session.refresh(to_approve)
    await session.refresh(to_reject)
    assert to_approve.status == "APPROVED"
    assert to_reject.status == "REJECTED"

    # Approved: the edge is real, present in the unified lineage graph --
    # exactly what "approved" already means on this platform, not
    # reimplemented here.
    graph = await build_unified_lineage_graph_payload(
        session, datasource, node_limit=100, edge_limit=1000,
        suggestion_status="APPROVED", settings=get_settings(),
    )
    approved_edge = next(
        (
            edge for edge in graph.edges
            if edge.edge_source == "SUGGESTED_RELATIONSHIP"
            and edge.source_node_id == str(approve_source.id)
            and edge.target_node_id == str(approve_target.id)
        ),
        None,
    )
    assert approved_edge is not None
    assert approved_edge.status == "APPROVED"

    # Rejected: recorded as negative knowledge through the real EE.3/N16
    # mechanism -- queryable, suppression active.
    negatives = (
        await session.scalars(
            select(NegativeAssertionRecord).where(
                NegativeAssertionRecord.organization_id == org.id,
                NegativeAssertionRecord.assertion_type == "RELATIONSHIP_REJECTED",
            )
        )
    ).all()
    assert len(negatives) == 1
    negative = negatives[0]
    assert negative.suppression_active is True
    assert negative.rejected_by == "reviewer"
    assert negative.subject_id == f"relationship:{reject_source_col.id}:{reject_target_col.id}"

    match = await check_re_proposal(
        session,
        org.id,
        negative.subject_id,
        relationship_candidate_negative_predicate(reject_source_col.id, reject_target_col.id),
    )
    assert match is not None
    assert match.id == negative.id

    # The approved candidate must NOT have produced a negative assertion.
    approve_predicate_match = await check_re_proposal(
        session,
        org.id,
        f"relationship:{approve_source_col.id}:{approve_target_col.id}",
        relationship_candidate_negative_predicate(approve_source_col.id, approve_target_col.id),
    )
    assert approve_predicate_match is None


# ---------------------------------------------------------------------------
# Re-proposal suppression: negative knowledge does real, independent work --
# not just piggy-backing on discovery's own existing-row dedup.
# ---------------------------------------------------------------------------


async def test_rejected_candidate_is_suppressed_from_re_proposal_even_after_row_is_gone(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    datasource = await _datasource(session, org, lob, domain, project)

    target_table, target_column = await _table_with_column(
        session, org, datasource, table_name="customers", column_name="customer_id",
        physical_type="INTEGER",
    )
    constraint = MetadataConstraint(
        organization_id=org.id,
        datasource_id=datasource.id,
        table_id=target_table.id,
        name="pk_customers",
        constraint_type="PRIMARY_KEY",
        columns=["customer_id"],
        fingerprint="f" * 8,
    )
    session.add(constraint)
    await session.flush()
    _source_table, source_column = await _table_with_column(
        session, org, datasource, table_name="orders", column_name="customer_id",
        physical_type="INTEGER",
    )

    maker_context = _context(org, principal="maker")
    discovery = await discover_relationship_candidates(
        datasource.id, RelationshipCandidateDiscoveryRequest(),
        context=maker_context, session=session, settings=get_settings(),
    )
    assert discovery.total == 1
    candidate_id = discovery.items[0].id
    assert discovery.items[0].source_column_id == source_column.id
    assert discovery.items[0].target_column_id == target_column.id

    reviewer_context = _context(org, principal="reviewer")
    await decide_relationship_candidate(
        candidate_id,
        RelationshipCandidateDecision(decision="REJECT", reason="not a real key"),
        context=reviewer_context,
        session=session,
    )

    negatives = (
        await session.scalars(
            select(NegativeAssertionRecord).where(
                NegativeAssertionRecord.organization_id == org.id,
                NegativeAssertionRecord.assertion_type == "RELATIONSHIP_REJECTED",
            )
        )
    ).all()
    assert len(negatives) == 1
    assert negatives[0].suppression_active is True

    # Purge the decided candidate row itself (e.g. housekeeping/archival) --
    # `existing_candidate_pairs` alone, freshly re-queried, would no longer
    # know this pair was ever proposed, so it could NOT by itself block
    # re-creation. Only the negative-knowledge check can.
    stale_candidate = await session.get(RelationshipCandidate, candidate_id)
    assert stale_candidate is not None
    await session.delete(stale_candidate)
    await session.flush()

    remaining = (
        await session.scalars(
            select(RelationshipCandidate).where(
                RelationshipCandidate.datasource_id == datasource.id
            )
        )
    ).all()
    assert remaining == []

    re_discovery = await discover_relationship_candidates(
        datasource.id, RelationshipCandidateDiscoveryRequest(),
        context=maker_context, session=session, settings=get_settings(),
    )
    assert re_discovery.total == 0

    audit_rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "relationship_candidates.discover")
        )
    ).all()
    assert audit_rows[-1].details["suppressed_by_negative_knowledge"] == 1
