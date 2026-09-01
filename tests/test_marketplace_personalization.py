"""CX-9 -- marketplace personalization by role/domain.

Tracker exit condition (`Docs/60-delivery/03-tracker.md`, CX-9): two requesters
with different domain ownership (GL-2) call the same listing endpoint and get
demonstrably different default orderings -- the product each owns a domain for
sorts higher in their own response than in the other's -- while both responses
still contain the full policy-filtered catalog (this ranks, it does not filter;
`Docs/20-modules/19-context-products-and-mcp.md` CP-4 addendum).

PostgreSQL is not reachable in this sandbox, so the endpoint-level tests run the
real `search_marketplace` function body against an in-memory SQLite database via
aiosqlite, in the same style as `tests/test_catalog_pagination.py`.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    BusinessDomain,
    DataDomain,
    DataProduct,
    DataProductPort,
    DataProductRoleBinding,
    DataProductVersion,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    Organization,
    OwnershipAssignment,
    Project,
)
from aida.product_marketplace_api import score_marketplace_product, search_marketplace
from aida.security_types import SecurityContext


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


# ---------------------------------------------------------------------------
# score_marketplace_product: a plain, deterministic scoring function --
# unit-testable in isolation from the database and the FastAPI layer.
# ---------------------------------------------------------------------------


def test_score_rewards_owned_domain_over_a_role_match() -> None:
    owned_domain = score_marketplace_product(
        domain_name="Payments",
        owned_domains=frozenset({"payments"}),
        roles=frozenset({"Viewer"}),  # business role; port shape favors technical
        technical_port_count=3,
        curated_port_count=0,
    )
    role_match_only = score_marketplace_product(
        domain_name="Risk",
        owned_domains=frozenset({"payments"}),
        roles=frozenset({"Analyst"}),
        technical_port_count=3,
        curated_port_count=0,
    )
    assert owned_domain.domain_affinity is True
    assert owned_domain.role_affinity is False
    assert role_match_only.domain_affinity is False
    assert role_match_only.role_affinity is True
    assert owned_domain.score > role_match_only.score  # ownership dominates a role tiebreak


def test_role_affinity_is_symmetric_and_neutral_outside_the_two_role_groups() -> None:
    technical = score_marketplace_product(
        domain_name="Ops",
        owned_domains=frozenset(),
        roles=frozenset({"DataScientist"}),
        technical_port_count=2,
        curated_port_count=1,
    )
    business = score_marketplace_product(
        domain_name="Ops",
        owned_domains=frozenset(),
        roles=frozenset({"DataConsumer"}),
        technical_port_count=1,
        curated_port_count=2,
    )
    neutral = score_marketplace_product(
        domain_name="Ops",
        owned_domains=frozenset(),
        roles=frozenset({"PlatformAdmin"}),
        technical_port_count=2,
        curated_port_count=1,
    )
    tied = score_marketplace_product(
        domain_name="Ops",
        owned_domains=frozenset(),
        roles=frozenset({"Analyst"}),
        technical_port_count=1,
        curated_port_count=1,
    )
    assert technical.role_affinity is True
    assert business.role_affinity is True
    assert neutral.role_affinity is False
    assert neutral.score == 0
    assert tied.role_affinity is False  # a tie is not a match either way


def test_domain_affinity_ignores_case_and_surrounding_whitespace() -> None:
    affinity = score_marketplace_product(
        domain_name="  PAYMENTS  ",
        owned_domains=frozenset({"payments"}),
        roles=frozenset(),
        technical_port_count=0,
        curated_port_count=0,
    )
    assert affinity.domain_affinity is True


# ---------------------------------------------------------------------------
# search_marketplace: the real endpoint, real (in-memory) database
# ---------------------------------------------------------------------------


async def _seed_marketplace(
    session: AsyncSession,
) -> tuple[Organization, DataProductVersion, DataProductVersion]:
    """One org, two published, universally-discoverable products -- one tagged
    to a Payments business domain, one to Risk -- plus GL-2 ownership assigning
    a Payments table to `alice` and a Risk table to `bob`.
    """
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    tenancy_domain = DataDomain(
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
        data_domain_id=tenancy_domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    payments_domain = BusinessDomain(
        id=uuid4(),
        organization_id=org.id,
        domain_key="payments",
        display_name="Payments",
        description="Payments business domain.",
        approved_by="steward",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    risk_domain = BusinessDomain(
        id=uuid4(),
        organization_id=org.id,
        domain_key="risk",
        display_name="Risk",
        description="Risk business domain.",
        approved_by="steward",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add_all([org, lob, tenancy_domain, project, payments_domain, risk_domain])
    await session.flush()

    def _product(
        *, key: str, name: str, domain_name: str
    ) -> tuple[DataProduct, DataProductVersion]:
        product = DataProduct(
            id=uuid4(),
            organization_id=org.id,
            project_id=project.id,
            product_key=key,
            lifecycle_status="ACTIVE",
            created_by="steward",
        )
        version = DataProductVersion(
            id=uuid4(),
            organization_id=org.id,
            product_id=product.id,
            version=1,
            status="PUBLISHED",
            name=name,
            description="A published marketplace product used for CX-9 tests.",
            domain_name=domain_name,
            owner_principal="steward",
            usage_terms="Approved analytical use only.",
            classification="INTERNAL",
            discoverable_roles=["*"],
            consumer_roles=["*"],
            fingerprint=uuid4().hex,
            created_by="steward",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        return product, version

    payments_product, payments_version = _product(
        key="payments_360", name="Payments 360", domain_name="Payments"
    )
    risk_product, risk_version = _product(key="risk_360", name="Risk 360", domain_name="Risk")
    session.add_all([payments_product, payments_version, risk_product, risk_version])
    await session.flush()

    payments_table_id = uuid4()
    risk_table_id = uuid4()
    session.add_all(
        [
            DataProductRoleBinding(
                organization_id=org.id,
                data_product_version_id=payments_version.id,
                role_kind="DISCOVER",
                role_name="*",
            ),
            DataProductRoleBinding(
                organization_id=org.id,
                data_product_version_id=risk_version.id,
                role_kind="DISCOVER",
                role_name="*",
            ),
            DataProductPort(
                organization_id=org.id,
                data_product_version_id=payments_version.id,
                port_key="out",
                direction="OUTPUT",
                name="Output",
                description="Output port.",
                asset_type="TABLE",
                asset_id=str(uuid4()),
            ),
            DataProductPort(
                organization_id=org.id,
                data_product_version_id=risk_version.id,
                port_key="out",
                direction="OUTPUT",
                name="Output",
                description="Output port.",
                asset_type="TABLE",
                asset_id=str(uuid4()),
            ),
            # GL-2 ownership: alice owns a Payments table, bob owns a Risk table.
            OwnershipAssignment(
                organization_id=org.id,
                subject_type="TABLE",
                subject_id=str(payments_table_id),
                owner_type="INDIVIDUAL",
                owner_principal="alice",
                assignment_kind="MANUAL",
                status="ACTIVE",
                assigned_by="steward",
            ),
            OwnershipAssignment(
                organization_id=org.id,
                subject_type="TABLE",
                subject_id=str(risk_table_id),
                owner_type="INDIVIDUAL",
                owner_principal="bob",
                assignment_kind="MANUAL",
                status="ACTIVE",
                assigned_by="steward",
            ),
            # AT-6: `MetadataBusinessAnnotation` is identity/domain-pointer only --
            # content lives on `MetadataBusinessAnnotationVersion`
            # (`business_annotation_versions.py`). `_owned_domain_names` (the
            # function under test here) only reads `domain_id`/`table_id`, so no
            # version row is needed for this test.
            MetadataBusinessAnnotation(
                organization_id=org.id,
                datasource_id=uuid4(),
                table_id=payments_table_id,
                domain_id=payments_domain.id,
                entity_id=uuid4(),
                source_proposal_id=uuid4(),
            ),
            MetadataBusinessAnnotation(
                organization_id=org.id,
                datasource_id=uuid4(),
                table_id=risk_table_id,
                domain_id=risk_domain.id,
                entity_id=uuid4(),
                source_proposal_id=uuid4(),
            ),
        ]
    )
    await session.commit()
    return org, payments_version, risk_version


def _context(*, organization_id, principal_id: str) -> SecurityContext:
    # Same role for both requesters, so the only thing that can move the
    # ordering between the two of them is domain ownership.
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Analyst"}),
    )


async def test_default_ordering_is_personalized_by_domain_ownership(
    session: AsyncSession,
) -> None:
    org, _payments_version, _risk_version = await _seed_marketplace(session)

    alice_page = await search_marketplace(
        q=None,
        domain=None,
        classification=None,
        limit=50,
        offset=0,
        sort="personalized",
        context=_context(organization_id=org.id, principal_id="alice"),
        session=session,
    )
    bob_page = await search_marketplace(
        q=None,
        domain=None,
        classification=None,
        limit=50,
        offset=0,
        sort="personalized",
        context=_context(organization_id=org.id, principal_id="bob"),
        session=session,
    )

    alice_names = [item.name for item in alice_page.items]
    bob_names = [item.name for item in bob_page.items]

    # Rank, don't filter: both requesters see the *entire* policy-filtered
    # catalog -- nothing is hidden from either of them.
    assert set(alice_names) == {"Payments 360", "Risk 360"}
    assert set(bob_names) == {"Payments 360", "Risk 360"}
    assert alice_page.total == 2
    assert bob_page.total == 2

    # But the order is demonstrably different: each requester's own domain
    # sorts above the other requester's domain, in their own response.
    assert alice_names.index("Payments 360") < alice_names.index("Risk 360")
    assert bob_names.index("Risk 360") < bob_names.index("Payments 360")

    alice_payments = next(item for item in alice_page.items if item.name == "Payments 360")
    bob_payments = next(item for item in bob_page.items if item.name == "Payments 360")
    alice_risk = next(item for item in alice_page.items if item.name == "Risk 360")
    bob_risk = next(item for item in bob_page.items if item.name == "Risk 360")
    assert alice_payments.domain_affinity is True
    assert bob_payments.domain_affinity is False
    assert bob_risk.domain_affinity is True
    assert alice_risk.domain_affinity is False


async def test_catalog_sort_restores_the_pre_cx9_undifferentiated_order(
    session: AsyncSession,
) -> None:
    org, _payments_version, _risk_version = await _seed_marketplace(session)

    alice_page = await search_marketplace(
        q=None,
        domain=None,
        classification=None,
        limit=50,
        offset=0,
        sort="catalog",
        context=_context(organization_id=org.id, principal_id="alice"),
        session=session,
    )
    bob_page = await search_marketplace(
        q=None,
        domain=None,
        classification=None,
        limit=50,
        offset=0,
        sort="catalog",
        context=_context(organization_id=org.id, principal_id="bob"),
        session=session,
    )

    # sort=catalog is the escape hatch back to the identical, unranked order
    # every requester saw before CX-9.
    assert [item.name for item in alice_page.items] == [item.name for item in bob_page.items] == [
        "Payments 360",
        "Risk 360",
    ]
