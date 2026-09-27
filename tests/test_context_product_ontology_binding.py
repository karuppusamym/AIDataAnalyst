"""R11-FP09: a context product can bind the approved ontology meaning it was built on.

Pinned by version id, so republishing an ontology never changes what an existing product says,
and validated like every other reference group: APPROVED, in the product's own organization. A
product binding none compiles and fingerprints exactly as before the field existed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.context_compiler import ResolvedTableReference, compile_context_product
from aida.context_product_api import (
    context_product_fingerprint,
    validate_context_product_references,
)
from aida.db import Base
from aida.models import (
    ContextProduct,
    ContextProductVersion,
    DataDomain,
    LineOfBusiness,
    Organization,
    Project,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.schemas import ContextProductDefinition


def _definition(**changes: object) -> ContextProductDefinition:
    values: dict[str, object] = {
        "name": "Commerce context",
        "description": "Approved commerce metadata.",
        "purpose": "Support bounded bookings analysis.",
        "owner_type": "GROUP",
        "owner_principal": "commerce-owner",
        "table_ids": [uuid4()],
        "allowed_consumer_roles": ["Analyst"],
    }
    values.update(changes)
    return ContextProductDefinition.model_validate(values)


def test_binding_no_ontology_fingerprints_as_before_and_binding_one_does_not() -> None:
    definition = _definition()
    legacy = definition.model_dump(mode="json")
    legacy.pop("routine_ids")
    legacy.pop("ontology_version_ids")
    expected = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert context_product_fingerprint(definition) == expected
    assert context_product_fingerprint(_definition(ontology_version_ids=[uuid4()])) != expected


def test_the_bound_versions_reach_the_compiled_references_only_when_bound() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    product = ContextProduct(
        id=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        product_key="commerce_context",
        lifecycle_status="ACTIVE",
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    table_id = str(uuid4())
    bound = sorted([str(uuid4()), str(uuid4())])

    def version(ontology_version_ids: list[str]) -> ContextProductVersion:
        return ContextProductVersion(
            id=uuid4(),
            organization_id=product.organization_id,
            product_id=product.id,
            version=1,
            status="PUBLISHED",
            name="Commerce context",
            description="Approved commerce metadata.",
            purpose="Support bounded bookings analysis.",
            owner_principal="commerce-owner",
            table_ids=[table_id],
            semantic_model_version_ids=[],
            glossary_term_version_ids=[],
            eligible_tool_version_ids=[],
            ontology_version_ids=ontology_version_ids,
            allowed_consumer_roles=["Analyst"],
            lineage_depth=2,
            quality_requirements={},
            policy_summary={},
            fingerprint="a" * 64,
            created_by="maker",
            created_at=now,
            updated_at=now,
        )

    tables = [ResolvedTableReference(table_id, "BANK.SALES.ORDERS")]
    for target, envelope in (("MCP", "context"), ("REST", "context"), ("OSI", "semanticContext")):
        with_binding = json.loads(
            compile_context_product(product, version(list(reversed(bound))), target, tables).content
        )[envelope]
        assert with_binding["references"]["ontology_version_ids"] == bound
        without = compile_context_product(product, version([]), target, tables).content
        assert "ontology_version_ids" not in without


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _ontology_version(
    session: AsyncSession, organization: Organization, status: str, version: int
) -> OntologyVersion:
    head = OntologyHead(
        id=uuid4(),
        organization_id=organization.id,
        ontology_key=f"commerce-{uuid4().hex[:6]}",
        last_version=version,
        published_version=version if status == "APPROVED" else 0,
    )
    session.add(head)
    await session.flush()
    row = OntologyVersion(
        id=uuid4(),
        organization_id=organization.id,
        ontology_id=head.id,
        version=version,
        base_version=0,
        status=status,
        definition={"name": "Commerce", "concepts": []},
        created_by="author",
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_only_an_approved_version_of_this_organization_can_be_bound(
    session: AsyncSession,
) -> None:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    stranger = Organization(id=uuid4(), name="Other", slug=f"other-{uuid4().hex[:8]}")
    lob = LineOfBusiness(id=uuid4(), organization_id=org.id, name="R", code=f"R{uuid4().hex[:6]}")
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Commerce",
        code=f"C{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    session.add_all([org, stranger, lob, domain, project])
    await session.flush()
    approved = await _ontology_version(session, org, "APPROVED", 1)
    draft = await _ontology_version(session, org, "DRAFT", 1)
    foreign = await _ontology_version(session, stranger, "APPROVED", 1)
    await session.commit()

    await validate_context_product_references(
        session, project, _definition(table_ids=[], ontology_version_ids=[approved.id])
    )
    for refused in (draft, foreign):
        with pytest.raises(HTTPException) as caught:
            await validate_context_product_references(
                session, project, _definition(table_ids=[], ontology_version_ids=[refused.id])
            )
        assert caught.value.status_code == 422
        assert "ontology versions" in str(caught.value.detail)
