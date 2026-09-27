"""R11-FP12 (F08): the Ask picker can ask the listing for what it can actually ask through.

`GET /v1/projects/{id}/context-products` answers the question the Context Products screen
asks -- "what does this project contain, and where is each version in its lifecycle" -- and a
lifecycle reader (PlatformAdmin, SemanticAdmin, DataSteward, Reviewer, Auditor) correctly sees
every product at its latest version, drafts included. That is their authoring surface and this
row does not touch it.

The Ask screen's product picker was built on that same listing and filtered it by status alone,
which is the wrong half of the admission rule: the ask path admits a product only when it is
PUBLISHED **and** names a consumer role the caller holds (`agent_orchestrator.py:1131-1148`).
A steward was therefore offered published products their roles are not consumers of, picked one,
and got `CONTEXT_PRODUCT_CONSUMER_ROLE_REQUIRED` back. No client-side filter could have fixed it:
"which roles do I hold" is not in the listing.

`askable=true` is that rule, applied server-side, as an explicit opt-in -- so the lifecycle view
stays the default and an existing caller that sends nothing sees exactly what it saw before.

These run the real handler against a real SQLite database, like
`test_r11c6_rest_context_product_listing_filter.py`: the property under test is a SQL filter, and
a session double would return whatever rows it was handed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from itertools import count
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.context_product_api import (
    list_context_products,
    replace_context_product_role_bindings,
)
from aida.db import Base
from aida.models import (
    AuditEvent,
    ContextProduct,
    ContextProductVersion,
    Organization,
    Project,
)
from aida.security import SecurityContext

_audit_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_id(_mapper: object, _connection: object, target: AuditEvent) -> None:
    # SQLite only auto-populates a bare INTEGER PRIMARY KEY.
    if target.id is None:
        target.id = next(_audit_ids)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class _Estate:
    def __init__(self, organization: Organization, project: Project) -> None:
        self.organization = organization
        self.project = project


async def _product(
    db: AsyncSession,
    estate: _Estate,
    *,
    key: str,
    status: str,
    consumer_roles: Iterable[str],
) -> ContextProduct:
    product = ContextProduct(
        id=uuid4(),
        organization_id=estate.organization.id,
        project_id=estate.project.id,
        product_key=key,
        lifecycle_status="ACTIVE",
        created_by="product-author",
    )
    db.add(product)
    await db.flush()
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=estate.organization.id,
        product_id=product.id,
        version=1,
        status=status,
        name=f"{key} package",
        description="What an agent needs to answer questions here.",
        purpose="Answer questions about this subject.",
        owner_type="INDIVIDUAL",
        owner_principal="product-author",
        # A product must name at least one governed reference to be a valid
        # definition (`ContextProductDefinition`), and the listing only reads it
        # back out -- it resolves nothing -- so one synthetic table id is enough.
        table_ids=[str(uuid4())],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=[],
        allowed_consumer_roles=list(consumer_roles),
        fingerprint=f"fp-{uuid4().hex[:8]}",
        created_by="product-author",
    )
    db.add(version)
    await db.flush()
    # Written through the same function publication uses, so the fixture cannot
    # drift from the rows a really-published version has.
    await replace_context_product_role_bindings(db, version)
    await db.flush()
    return product


@pytest_asyncio.fixture
async def estate(db: AsyncSession) -> _Estate:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(organization)
    await db.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        name="Core Banking",
        slug=f"core-{uuid4().hex[:6]}",
    )
    db.add(project)
    await db.flush()
    built = _Estate(organization, project)
    # Published, and a consumer role a steward does NOT hold -- the case that
    # produced the refusal the picker offered.
    await _product(db, built, key="orders-context", status="PUBLISHED", consumer_roles=["Analyst"])
    # Published, and bound to the steward's own role.
    await _product(
        db, built, key="risk-context", status="PUBLISHED", consumer_roles=["DataSteward"]
    )
    # Never askable at any status: the ask path resolves a PUBLISHED version only.
    await _product(
        db,
        built,
        key="draft-context",
        status="DRAFT",
        consumer_roles=["Analyst", "DataSteward"],
    )
    await db.commit()
    return built


def _context(estate: _Estate, *, roles: set[str]) -> SecurityContext:
    return SecurityContext(
        principal_id="person@bank.example",
        principal_type="USER",
        organization_id=estate.organization.id,
        roles=frozenset(roles),
    )


async def _list(
    db: AsyncSession,
    estate: _Estate,
    context: SecurityContext,
    *,
    askable: bool = False,
) -> tuple[list[str], int]:
    page = await list_context_products(
        estate.project.id,
        limit=100,
        offset=0,
        askable=askable,
        context=context,
        session=db,
    )
    return sorted(item.product_key for item in page.items), page.total


async def test_a_lifecycle_reader_still_sees_every_product(
    db: AsyncSession, estate: _Estate
) -> None:
    """The authoring view is not what this row narrows. A steward who could no
    longer see their own drafts here would have nowhere else to see them."""
    keys, total = await _list(db, estate, _context(estate, roles={"DataSteward"}))

    assert keys == ["draft-context", "orders-context", "risk-context"]
    assert total == 3


async def test_a_lifecycle_reader_asking_for_askable_gets_only_what_the_ask_admits(
    db: AsyncSession, estate: _Estate
) -> None:
    """The defect, stated as a test. `orders-context` is published but names
    `Analyst` as its consumer, so a steward picking it out of the picker was
    refused with CONTEXT_PRODUCT_CONSUMER_ROLE_REQUIRED; `draft-context` is not
    published at all."""
    keys, total = await _list(
        db, estate, _context(estate, roles={"DataSteward"}), askable=True
    )

    assert keys == ["risk-context"]
    # The count agrees with the rows, so the picker cannot report more options
    # than it can show.
    assert total == 1


async def test_a_consumer_sees_the_same_list_either_way(
    db: AsyncSession, estate: _Estate
) -> None:
    """An Analyst was already filtered to PUBLISHED plus their own bound role,
    so the flag must be a no-op for them -- otherwise it would be a second,
    differently-shaped access rule."""
    analyst = _context(estate, roles={"Analyst"})

    assert await _list(db, estate, analyst) == (["orders-context"], 1)
    assert await _list(db, estate, analyst, askable=True) == (["orders-context"], 1)


async def test_an_administrator_sees_every_published_product(
    db: AsyncSession, estate: _Estate
) -> None:
    """The ask path exempts PlatformAdmin from the consumer-role check
    (`agent_orchestrator.py:1144`), so the askable listing does too: applying
    the binding filter here would hide products an administrator can in fact
    ask through. The PUBLISHED half still applies."""
    keys, total = await _list(
        db, estate, _context(estate, roles={"PlatformAdmin"}), askable=True
    )

    assert keys == ["orders-context", "risk-context"]
    assert total == 2


async def test_the_default_is_unchanged_for_every_reader(
    db: AsyncSession, estate: _Estate
) -> None:
    """Backward compatibility, asserted rather than assumed: `askable` is a new
    optional parameter and an existing caller sends nothing.
    `scripts/openapi_diff.py` gates the wire contract; this gates the rows."""
    for roles in ({"DataSteward"}, {"Reviewer"}, {"Auditor"}, {"PlatformAdmin"}):
        keys, total = await _list(db, estate, _context(estate, roles=roles))
        assert keys == ["draft-context", "orders-context", "risk-context"]
        assert total == 3


async def test_an_unlisted_role_can_ask_through_nothing(
    db: AsyncSession, estate: _Estate
) -> None:
    """A Viewer holds a reader role for this listing but is no product's
    consumer, so the honest offer is none -- not "every published product"."""
    keys, total = await _list(db, estate, _context(estate, roles={"Viewer"}), askable=True)

    assert keys == []
    assert total == 0
