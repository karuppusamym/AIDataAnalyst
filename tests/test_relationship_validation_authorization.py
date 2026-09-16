"""R11-FP06: a relationship's validation is read under each side's metadata gate.

A validation names columns and carries profile counts from both sides of a join, so organization
membership alone is not enough to read it:

* the read applies the authorization gate to every datasource the validation reads;
* a join whose two sides sit in different data domains is read only under an ACTIVE
  cross-boundary grant from the key side's domain to the referencing side's domain, and a grant
  naming other edge kinds is not that grant;
* deciding the join applies the same gates, on the single and the bulk path, so approving is
  never the way around a read the gates refuse;
* the decision refusal carries the outcome, never the evidence;
* the validation an approval records is served by that gated read alone: the candidate reads and
  the graph edges that organization membership opens carry the rest of the evidence without it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.intelligence_api import (
    bulk_decide_relationship_candidates,
    decide_relationship_candidate,
)
from aida.models import (
    CrossBoundaryGrant,
    DataDomain,
    LineOfBusiness,
    MetadataConstraint,
    Organization,
    RelationshipCandidate,
)
from aida.relationship_validation_api import (
    NO_CROSS_BOUNDARY_GRANT,
    get_relationship_candidate_validation,
)
from aida.schemas import (
    GraphEdgeRead,
    RelationshipCandidateBulkDecisionRequest,
    RelationshipCandidateDecision,
    RelationshipCandidateRead,
    UnifiedLineageEdgeRead,
)
from tests.test_relationship_intelligence_review import (
    _context,
    _datasource,
    _lob,
    _org,
    _project,
    _table_with_column,
)
from tests.test_relationship_validation import _candidate, _source


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _named_domain(
    session: AsyncSession, org: Organization, lob: LineOfBusiness, name: str
) -> DataDomain:
    domain = DataDomain(
        organization_id=org.id,
        line_of_business_id=lob.id,
        name=name,
        code=f"D{uuid4().hex[:6]}",
    )
    session.add(domain)
    await session.flush()
    return domain


async def test_the_validation_read_applies_the_datasource_metadata_gate(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, _ = await _candidate(session, org, datasource, target_key=True)
    reviewer = _context(org, "reviewer")

    # A datasource outside any workspace, under a posture that denies unresolved access.
    with pytest.raises(HTTPException) as denied:
        await get_relationship_candidate_validation(
            candidate.id,
            context=reviewer,
            session=session,
            settings=Settings(unresolved_workspace_posture="DENY"),
        )
    assert denied.value.status_code == 403

    allowed = await get_relationship_candidate_validation(
        candidate.id, context=reviewer, session=session, settings=Settings()
    )
    assert allowed.approvable


async def _cross_domain_candidate(
    session: AsyncSession,
) -> tuple[Organization, DataDomain, DataDomain, RelationshipCandidate]:
    """An orders -> customers join whose two sides sit in different data domains."""
    org = await _org(session)
    lob = await _lob(session, org)
    retail = await _named_domain(session, org, lob, "Retail")
    risk = await _named_domain(session, org, lob, "Risk")
    orders_source = await _datasource(
        session, org, lob, retail, await _project(session, org, lob, retail), name="orders-db"
    )
    customer_source = await _datasource(
        session, org, lob, risk, await _project(session, org, lob, risk), name="customer-db"
    )
    orders, orders_customer = await _table_with_column(
        session,
        org,
        orders_source,
        table_name="orders",
        column_name="customer_id",
        physical_type="INTEGER",
    )
    customers, customer_key = await _table_with_column(
        session,
        org,
        customer_source,
        table_name="customers",
        column_name="customer_id",
        physical_type="INTEGER",
    )
    session.add(
        MetadataConstraint(
            organization_id=org.id,
            datasource_id=customer_source.id,
            table_id=customers.id,
            name="pk_customers",
            constraint_type="PRIMARY_KEY",
            columns=["customer_id"],
            fingerprint="f" * 8,
        )
    )
    candidate = RelationshipCandidate(
        organization_id=org.id,
        datasource_id=orders_source.id,
        target_datasource_id=customer_source.id,
        source_table_id=orders.id,
        source_column_id=orders_customer.id,
        target_table_id=customers.id,
        target_column_id=customer_key.id,
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_CROSS_SOURCE_V1",
        confidence=0.75,
        evidence={},
        created_by="maker",
    )
    session.add(candidate)
    await session.flush()
    return org, retail, risk, candidate


async def _grant(
    session: AsyncSession,
    org: Organization,
    *,
    source_domain: DataDomain,
    target_domain: DataDomain,
    edge_kinds: list[str] | None = None,
) -> None:
    session.add(
        CrossBoundaryGrant(
            organization_id=org.id,
            source_data_domain_id=source_domain.id,
            target_data_domain_id=target_domain.id,
            reason="orders reference customers",
            status="ACTIVE",
            requested_by="steward",
            edge_kinds=edge_kinds or [],
        )
    )
    await session.flush()


async def test_a_join_across_data_domains_is_read_only_under_a_grant(
    session: AsyncSession,
) -> None:
    org, retail, risk, candidate = await _cross_domain_candidate(session)
    reviewer = _context(org, "reviewer")

    with pytest.raises(HTTPException) as denied:
        await get_relationship_candidate_validation(
            candidate.id, context=reviewer, session=session, settings=Settings()
        )
    assert (denied.value.status_code, denied.value.detail) == (403, NO_CROSS_BOUNDARY_GRANT)

    await _grant(session, org, source_domain=risk, target_domain=retail)
    granted = await get_relationship_candidate_validation(
        candidate.id, context=reviewer, session=session, settings=Settings()
    )
    assert granted.approvable
    assert (granted.source_key_columns, granted.target_key_columns) == (
        ["customer_id"],
        ["customer_id"],
    )


async def test_the_decision_refusal_carries_the_outcome_not_the_evidence(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, _ = await _candidate(session, org, datasource, target_key=False)

    with pytest.raises(HTTPException) as refused:
        await decide_relationship_candidate(
            candidate.id,
            RelationshipCandidateDecision(decision="APPROVE"),
            context=_context(org, "reviewer"),
            session=session,
            settings=Settings(),
        )

    detail = refused.value.detail
    assert isinstance(detail, dict)
    assert set(detail) == {"code", "message", "outcome"}


async def test_deciding_a_join_applies_the_gates_the_validation_read_applies(
    session: AsyncSession,
) -> None:
    org, retail, risk, candidate = await _cross_domain_candidate(session)
    reviewer = _context(org, "reviewer")

    with pytest.raises(HTTPException) as denied:
        await decide_relationship_candidate(
            candidate.id,
            RelationshipCandidateDecision(decision="APPROVE"),
            context=reviewer,
            session=session,
            settings=Settings(),
        )
    assert (denied.value.status_code, denied.value.detail) == (403, NO_CROSS_BOUNDARY_GRANT)
    # The refused decision decided nothing and recorded no evidence to be read back.
    assert candidate.status == "PENDING"
    assert "validation" not in (candidate.evidence or {})

    await _grant(session, org, source_domain=risk, target_domain=retail)
    decided = await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=reviewer,
        session=session,
        settings=Settings(),
    )
    assert decided.status == "APPROVED"
    assert "validation" in (decided.evidence or {})


async def test_a_bulk_decision_item_without_a_grant_fails(session: AsyncSession) -> None:
    org, _retail, _risk, candidate = await _cross_domain_candidate(session)

    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[candidate.id], decision="APPROVE"
        ),
        context=_context(org, "reviewer"),
        session=session,
        settings=Settings(),
    )

    assert (result.succeeded_count, result.failed_count) == (0, 1)
    assert result.results[0].status == "FAILED"
    assert result.results[0].reason == NO_CROSS_BOUNDARY_GRANT
    assert candidate.status == "PENDING"


async def test_a_grant_for_other_edge_kinds_does_not_admit_the_validation(
    session: AsyncSession,
) -> None:
    org, retail, risk, candidate = await _cross_domain_candidate(session)
    await _grant(
        session, org, source_domain=risk, target_domain=retail, edge_kinds=["DBT_DEPENDENCY"]
    )

    with pytest.raises(HTTPException) as denied:
        await get_relationship_candidate_validation(
            candidate.id, context=_context(org, "reviewer"), session=session, settings=Settings()
        )
    assert (denied.value.status_code, denied.value.detail) == (403, NO_CROSS_BOUNDARY_GRANT)


async def test_an_approved_join_serves_its_validation_only_through_the_gated_read(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, _ = await _candidate(session, org, datasource, target_key=True)
    reviewer = _context(org, "reviewer")

    approved = await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=reviewer,
        session=session,
        settings=Settings(),
    )
    recorded = (approved.evidence or {}).get("validation") or {}
    assert recorded.get("fingerprint")

    # The candidate reads organization membership opens keep the rest of the evidence.
    served = RelationshipCandidateRead.model_validate(approved).model_dump()
    assert "validation" not in served["evidence"]
    assert served["evidence"] == {
        key: value for key, value in approved.evidence.items() if key != "validation"
    }

    gated = await get_relationship_candidate_validation(
        candidate.id, context=reviewer, session=session, settings=Settings()
    )
    assert gated.recorded_fingerprint == recorded["fingerprint"]


def test_a_graph_edge_never_carries_a_recorded_validation() -> None:
    evidence = {
        "signals": ["NAME_MATCH"],
        "validation": {"fingerprint": "abc", "source_key_columns": ["customer_id"]},
    }

    graph_edge = GraphEdgeRead(
        id="candidate:1",
        edge_type="SUGGESTED_RELATIONSHIP",
        source_node_id=uuid4(),
        target_node_id=uuid4(),
        source_label="orders",
        target_label="customers",
        source_columns=["customer_id"],
        target_columns=["customer_id"],
        status="APPROVED",
        confidence=0.9,
        evidence=evidence,
    )
    unified_edge = UnifiedLineageEdgeRead(
        id="candidate:1",
        edge_source="SUGGESTED_RELATIONSHIP",
        source_node_id="orders",
        target_node_id="customers",
        source_label="orders",
        target_label="customers",
        status="APPROVED",
        confidence=0.9,
        evidence=evidence,
    )

    assert graph_edge.model_dump()["evidence"] == {"signals": ["NAME_MATCH"]}
    assert unified_edge.model_dump()["evidence"] == {"signals": ["NAME_MATCH"]}
