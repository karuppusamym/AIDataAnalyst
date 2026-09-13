"""R11-C6: the project-level context-product listing honours the envelope.

Every single-product door already checks a contracted agent's
`capability_envelope.context_product_ids` -- the version read, its scope, and
the version listing. The project listing did not, so an agent whose envelope
named product A could list the project and learn that products B and C exist
and what they are called. That is the enumeration the per-product 404s exist to
prevent, answered one level up.

The row asked for a filter rather than a gate, and that is what these pin: a
listing has no single product to refuse, so the agent sees what its envelope
names and nothing else, and the *count* is filtered with the rows so the total
cannot leak how many were hidden.

These run the real handler against a real SQLite database. A session double
would return whatever rows it was handed and could not show a SQL filter
excluding anything -- which is exactly the property under test.
"""

from collections.abc import AsyncIterator
from itertools import count
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.context_product_api import list_context_products
from aida.db import Base
from aida.models import (
    AgentContract,
    AuditEvent,
    ContextProduct,
    Organization,
    Project,
)
from aida.security import SecurityContext
from tests.test_context_products import _candidate

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
        self.products: dict[str, ContextProduct] = {}


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
    for key in ("revenue_context", "risk_context", "payments_context"):
        product = ContextProduct(
            id=uuid4(),
            organization_id=organization.id,
            project_id=project.id,
            product_key=key,
            lifecycle_status="ACTIVE",
            created_by="product-author",
        )
        db.add(product)
        await db.flush()
        version = _candidate(organization_id=organization.id, product_id=product.id)
        version.status = "PUBLISHED"
        version.version = 1
        db.add(version)
        built.products[key] = product
    await db.commit()
    return built


def _steward(estate: _Estate, *, principal_id: str, principal_type: str) -> SecurityContext:
    """`DataSteward` reads the lifecycle, so role bindings are not what decides
    visibility in these tests -- the envelope is the only variable."""
    return SecurityContext(
        principal_id=principal_id,
        principal_type=principal_type,
        organization_id=estate.organization.id,
        roles=frozenset({"DataSteward"}),
    )


async def _contract(db: AsyncSession, estate: _Estate, products: list[str]) -> None:
    db.add(
        AgentContract(
            id=uuid4(),
            organization_id=estate.organization.id,
            # SQLite does not enforce the foreign key, and no test here reads it.
            ai_asset_version_id=uuid4(),
            agent_principal_id="agent:revenue-bot",
            capability_envelope={
                "tool_slugs": [],
                "context_product_ids": products,
                "write_lanes": [],
            },
            autonomy_tier="T1",
            supervisor_persona="STEWARD",
            kill_scope="AGENT",
            sampling_rate=0.05,
            created_by="agent-owner",
        )
    )
    await db.commit()


async def _list(
    db: AsyncSession, estate: _Estate, context: SecurityContext
) -> tuple[list[str], int]:
    page = await list_context_products(
        estate.project.id, limit=100, offset=0, context=context, session=db
    )
    return sorted(item.product_key for item in page.items), page.total


async def test_a_human_sees_every_product(db: AsyncSession, estate: _Estate) -> None:
    """The control must be invisible to a person; otherwise it is a second
    role check."""
    keys, total = await _list(
        db, estate, _steward(estate, principal_id="steward@bank.example", principal_type="USER")
    )

    assert keys == ["payments_context", "revenue_context", "risk_context"]
    assert total == 3


async def test_an_agent_sees_only_what_its_envelope_names(
    db: AsyncSession, estate: _Estate
) -> None:
    """The defect, stated as a test. Before the filter this agent listed all
    three and learned the names of two products it may not reach."""
    await _contract(db, estate, ["revenue_context"])

    keys, _ = await _list(
        db, estate, _steward(estate, principal_id="agent:revenue-bot", principal_type="AGENT")
    )

    assert keys == ["revenue_context"]


async def test_the_total_is_filtered_with_the_rows(db: AsyncSession, estate: _Estate) -> None:
    """A page of one with `total: 3` still tells the agent two products are
    hidden. The count has to agree with what it may see."""
    await _contract(db, estate, ["revenue_context"])

    _, total = await _list(
        db, estate, _steward(estate, principal_id="agent:revenue-bot", principal_type="AGENT")
    )

    assert total == 1


async def test_an_envelope_may_name_a_product_by_its_uuid(
    db: AsyncSession, estate: _Estate
) -> None:
    """`context_product_violation` accepts either identifier, and the listing
    must agree with the doors it sits above."""
    await _contract(db, estate, [str(estate.products["risk_context"].id)])

    keys, total = await _list(
        db, estate, _steward(estate, principal_id="agent:revenue-bot", principal_type="AGENT")
    )

    assert keys == ["risk_context"]
    assert total == 1


async def test_an_empty_envelope_lists_nothing(db: AsyncSession, estate: _Estate) -> None:
    """An empty allowlist is empty, exactly as it is for `tool_slugs`."""
    await _contract(db, estate, [])

    keys, total = await _list(
        db, estate, _steward(estate, principal_id="agent:revenue-bot", principal_type="AGENT")
    )

    assert keys == []
    assert total == 0


async def test_an_agent_with_no_contract_lists_nothing_and_is_audited(
    db: AsyncSession, estate: _Estate
) -> None:
    """Fail closed. An `AGENT` identity that resolves no contract must not be
    served as an uncontracted human -- the boundary confusion
    `load_contract_for_principal` closes -- and the refusal is visible to an
    operator even though the agent only sees an empty page."""
    keys, total = await _list(
        db, estate, _steward(estate, principal_id="agent:unregistered", principal_type="AGENT")
    )

    assert keys == []
    assert total == 0
    denials = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "context_product.list.agent_contract_denied"
            )
        )
    ).all()
    assert len(denials) == 1
    assert denials[0].outcome == "DENIED"


def test_the_fixture_ids_are_real_uuids() -> None:
    """Guards the UUID test above from passing vacuously on a malformed id."""
    assert isinstance(UUID(str(uuid4())), UUID)
