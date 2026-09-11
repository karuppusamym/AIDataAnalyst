from __future__ import annotations

import itertools
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.models import AuditEvent, GovernanceReview, Organization
from aida.ontology_api import (
    OntologyCreate,
    OntologyDefinition,
    create_ontology_version,
    list_ontology_versions,
    submit_ontology_version,
)
from aida.ontology_models import OntologyHead
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import compose_governance_review_diff, decide_governance_review


def definition(**updates):
    return {
        "name": "Customer",
        "owner": "steward",
        "provenance": "Business glossary",
        "concepts": [
            {"key": "customer", "name": "Customer", "description": "A party with a relationship"}
        ],
        **updates,
    }


def context(org, name="maker"):
    return SecurityContext(
        principal_id=name,
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"DataSteward"}),
    )


@pytest.fixture
async def store():
    counter = itertools.count(1)

    def assign_id(mapper, connection, target):
        if target.id is None:
            target.id = next(counter)

    event.listen(AuditEvent, "before_insert", assign_id)
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        org = Organization(id=uuid4(), name="Test", slug="ontology-test")
        session.add(org)
        await session.commit()
        yield session, org.id
    await engine.dispose()
    event.remove(AuditEvent, "before_insert", assign_id)


async def create(session, org, base=0, **updates):
    return await create_ontology_version(
        org,
        OntologyCreate(
            ontology_key="customer",
            base_version=base,
            definition=OntologyDefinition.model_validate(definition(**updates)),
        ),
        context(org),
        session,
        Settings(),
    )


async def test_ontology_draft_review_diff_and_independent_publication(store):
    session, org = store
    draft = await create(session, org)
    assert draft.status == "DRAFT"
    submitted = await submit_ontology_version(draft.id, context(org), session, Settings())
    review = await session.get(GovernanceReview, submitted.governance_review_id)
    diff = await compose_governance_review_diff(session, review)
    assert diff.diffable and diff.after["name"] == "Customer"
    with pytest.raises(HTTPException) as refused:
        await decide_governance_review(
            review.id, GovernanceDecisionRequest(decision="APPROVE"), context(org), session
        )
    assert refused.value.status_code == 409
    await decide_governance_review(
        review.id, GovernanceDecisionRequest(decision="APPROVE"), context(org, "checker"), session
    )
    head = await session.get(OntologyHead, draft.ontology_id)
    assert head.published_version == 1
    rows = await list_ontology_versions(org, 50, 0, context(org), session, Settings())
    assert rows[0].status == "APPROVED"
    assert rows[0].ontology_key == "customer"


async def test_context_product_review_uses_the_declared_base_and_rejects_foreign_baselines(store):
    from aida.models import ContextProductVersion
    from aida.review_detail_snapshots import detail_snapshots

    session, org = store
    product_id = uuid4()
    baseline = ContextProductVersion(
        id=uuid4(),
        organization_id=org,
        product_id=product_id,
        version=1,
        name="Original",
        description="Original definition",
        purpose="Reporting",
        owner_principal="owner",
        allowed_consumer_roles=["Analyst"],
        fingerprint="a" * 64,
        created_by="maker",
        status="PUBLISHED",
    )
    proposed = ContextProductVersion(
        id=uuid4(),
        organization_id=org,
        product_id=product_id,
        version=2,
        name="Proposed",
        description="Edited definition",
        purpose="Reporting",
        owner_principal="owner",
        allowed_consumer_roles=["Analyst"],
        fingerprint="b" * 64,
        created_by="maker",
        based_on_version_id=baseline.id,
        status="REVIEW_REQUIRED",
    )
    session.add_all([baseline, proposed])
    await session.flush()
    review = GovernanceReview(
        id=uuid4(),
        organization_id=org,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(proposed.id),
        requested_action="PUBLISH",
        status="PENDING",
        requested_by="maker",
    )
    before, after, _ = await detail_snapshots(session, review)
    assert before["name"] == "Original" and after["name"] == "Proposed"
    baseline.organization_id = uuid4()
    with pytest.raises(HTTPException) as refused:
        await detail_snapshots(session, review)
    assert refused.value.status_code == 409


async def test_ontology_stale_second_proposal_cannot_replace_new_publication(store):
    session, org = store
    first = await create(session, org)
    second = await create(session, org, name="Another proposal")
    first = await submit_ontology_version(first.id, context(org), session, Settings())
    second = await submit_ontology_version(second.id, context(org), session, Settings())
    await decide_governance_review(
        first.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context(org, "checker"),
        session,
    )
    with pytest.raises(HTTPException) as refused:
        await decide_governance_review(
            second.governance_review_id,
            GovernanceDecisionRequest(decision="APPROVE"),
            context(org, "checker"),
            session,
        )
    assert refused.value.status_code == 409


async def test_ontology_rejects_cross_tenant_and_unknown_mapping(store):
    session, org = store
    with pytest.raises(HTTPException) as refused:
        await create_ontology_version(
            org,
            OntologyCreate(
                ontology_key="customer", definition=OntologyDefinition.model_validate(definition())
            ),
            context(uuid4()),
            session,
            Settings(),
        )
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await create(
            session,
            org,
            mappings=[{"concept": "customer", "subject_type": "TABLE", "subject_id": str(uuid4())}],
        )
    assert refused.value.status_code == 422


async def test_ontology_published_keys_must_be_deprecated_not_deleted(store):
    session, org = store
    first = await create(session, org)
    first = await submit_ontology_version(first.id, context(org), session, Settings())
    await decide_governance_review(
        first.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context(org, "checker"),
        session,
    )
    with pytest.raises(HTTPException) as refused:
        await create(
            session,
            org,
            base=1,
            concepts=[{"key": "different", "name": "Different", "description": "Replacement"}],
        )
    assert refused.value.status_code == 422
    retired = await create(
        session,
        org,
        base=1,
        lifecycle="DEPRECATED",
        concepts=[
            {
                "key": "customer",
                "name": "Customer",
                "description": "Retired meaning",
                "deprecated": True,
            }
        ],
    )
    assert retired.status == "DRAFT" and retired.base_version == 1


@pytest.mark.parametrize(
    "updates",
    [
        {
            "concepts": [
                {"key": "x", "name": "X", "description": "X"},
                {"key": "x", "name": "X2", "description": "X2"},
            ]
        },
        {
            "relations": [
                {
                    "key": "owns",
                    "source": "customer",
                    "target": "missing",
                    "description": "Owns",
                    "cardinality": "ONE_TO_MANY",
                }
            ]
        },
        {
            "concepts": [
                {"key": "customer", "name": "Customer", "description": "X", "aliases": ["customer"]}
            ]
        },
        {"mappings": [{"concept": "missing", "subject_type": "TABLE", "subject_id": str(uuid4())}]},
    ],
)
def test_ontology_structural_validation(updates):
    with pytest.raises(ValidationError):
        OntologyDefinition.model_validate(definition(**updates))
