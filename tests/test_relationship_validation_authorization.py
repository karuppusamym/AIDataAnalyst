"""R11-FP06: a relationship's validation is read under each side's metadata gate.

A validation names columns and carries profile counts from both sides of a join, so organization
membership alone is not enough to read it:

* the read applies the authorization gate to every datasource the validation reads;
* a join whose two sides sit in different data domains is read only under an ACTIVE
  cross-boundary grant from the key side's domain to the referencing side's domain;
* the decision refusal carries the outcome, never the evidence.
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
from aida.intelligence_api import decide_relationship_candidate
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
from aida.schemas import RelationshipCandidateDecision
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


async def test_a_join_across_data_domains_is_read_only_under_a_grant(
    session: AsyncSession,
) -> None:
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
    reviewer = _context(org, "reviewer")

    with pytest.raises(HTTPException) as denied:
        await get_relationship_candidate_validation(
            candidate.id, context=reviewer, session=session, settings=Settings()
        )
    assert (denied.value.status_code, denied.value.detail) == (403, NO_CROSS_BOUNDARY_GRANT)

    session.add(
        CrossBoundaryGrant(
            organization_id=org.id,
            source_data_domain_id=risk.id,
            target_data_domain_id=retail.id,
            reason="orders reference customers",
            status="ACTIVE",
            requested_by="steward",
        )
    )
    await session.flush()
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
        )

    detail = refused.value.detail
    assert isinstance(detail, dict)
    assert set(detail) == {"code", "message", "outcome"}
