"""F15 server half: `q=` search and fetch-by-id on the four shared-picker routes.

The client half of F15 pages the four picker list routes with a budget and
labels the result truncated; this is the server half that makes a resource
beyond the cap findable in one request instead of a scan.

**What this suite is really for.** Adding a filter to a tenant-scoped list
route is the kind of change that can quietly widen it -- a filter built as
`WHERE name LIKE ... OR organization_id = ...`, a fetch-by-id route that
forgets the boundary the list route enforces, a search that runs before the
tenant predicate. So for every one of the four routes there is a test that a
term matching a resource in **another organization** returns nothing, and for
every one of the four new by-id routes a test that another organization's id
is refused. Those eight tests are the point of the change being safe; the
"search actually finds things" tests only establish that the feature works.

Run against a real SQL engine (in-memory SQLite via aiosqlite) with the real
ORM models and the real endpoint bodies, following `test_catalog_pagination`'s
precedent: PostgreSQL is not reachable in this sandbox, and `lower(x) LIKE
'%term%'` is semantics SQLite implements identically for the ASCII fixtures
used here. What SQLite cannot exercise -- whether the planner picks the
`pg_trgm` GIN indexes from `c3f0a71d5e94_picker_search_trgm_indexes.py` -- is
a performance property, not a correctness or an isolation one.

Handlers are called directly rather than over HTTP because that is the only
way to assert the *query's* tenant behaviour rather than the dependency
wiring's; `tests/test_inv5_tenant_isolation.py` and the generated
`Docs/50-security/surface-control-matrix.md` cover the wiring. Note that every
`Query(...)`-defaulted parameter must be passed explicitly here: called
directly, an omitted one would bind the `Query` object itself, not its default.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
    Workspace,
)
from aida.operational_api import (
    get_datasource,
    get_project,
    list_organization_datasources,
    list_organization_projects,
)
from aida.security_types import SecurityContext
from atlas.modules.identity_tenancy.router import (
    get_organization,
    get_workspace,
    list_organizations,
    list_workspaces,
)

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class Tenant:
    """One organization's picker estate: one of each resource, named after it."""

    organization: Organization
    project: Project
    datasource: DataSource
    workspace: Workspace

    def context(self, *, roles: frozenset[str] = frozenset({"Viewer"})) -> SecurityContext:
        return SecurityContext(
            principal_id=f"{self.organization.slug}-principal",
            principal_type="USER",
            organization_id=self.organization.id,
            roles=roles,
        )


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


async def _seed_tenant(session: AsyncSession, label: str) -> Tenant:
    """One organization whose every resource carries `label` in name and slug.

    Naming every resource after its tenant is what makes the cross-tenant
    assertions readable: searching for the *other* tenant's label is a term
    that matches a real row in the database and must still return nothing.
    """
    organization = Organization(id=uuid4(), name=f"{label.title()} Bank", slug=label)
    lob = LineOfBusiness(
        id=uuid4(), organization_id=organization.id, name=f"{label} retail", code=f"{label}-RTL"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name=f"{label} ungoverned",
        code=f"{label}-UNG",
    )
    project = Project(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name=f"{label.title()} Warehouse",
        slug=f"{label}-warehouse",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"{label.title()} Ledger",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    workspace = Workspace(
        id=uuid4(),
        organization_id=organization.id,
        name=f"{label.title()} Analytics",
        slug=f"{label}-analytics",
        purpose="analysis",
    )
    session.add_all([organization, lob, domain, project, datasource, workspace])
    await session.flush()
    return Tenant(
        organization=organization, project=project, datasource=datasource, workspace=workspace
    )


@pytest.fixture
async def estate(session: AsyncSession) -> tuple[Tenant, Tenant]:
    """Two tenants, `alpha` and `beta`, with mirror-image resource names."""
    alpha = await _seed_tenant(session, "alpha")
    beta = await _seed_tenant(session, "beta")
    return alpha, beta


async def _projects(session: AsyncSession, tenant: Tenant, q: str | None) -> list[str]:
    page = await list_organization_projects(
        organization_id=tenant.organization.id,
        q=q,
        line_of_business_id=None,
        limit=50,
        offset=0,
        context=tenant.context(),
        session=session,
    )
    assert page.total == len(page.items)
    return [item.name for item in page.items]


async def _datasources(session: AsyncSession, tenant: Tenant, q: str | None) -> list[str]:
    page = await list_organization_datasources(
        organization_id=tenant.organization.id,
        q=q,
        project_id=None,
        datasource_status=None,
        limit=50,
        offset=0,
        context=tenant.context(),
        session=session,
    )
    assert page.total == len(page.items)
    return [item.name for item in page.items]


async def _workspaces(session: AsyncSession, tenant: Tenant, q: str | None) -> list[str]:
    page = await list_workspaces(
        organization_id=tenant.organization.id,
        q=q,
        limit=50,
        offset=0,
        context=tenant.context(),
        session=session,
    )
    assert page.total == len(page.items)
    return [item.name for item in page.items]


async def _organizations(session: AsyncSession, tenant: Tenant, q: str | None) -> list[str]:
    page = await list_organizations(
        q=q,
        limit=50,
        offset=0,
        context=tenant.context(roles=frozenset({"OrganizationAdmin"})),
        session=session,
    )
    assert page.total == len(page.items)
    return [item.name for item in page.items]


# --- the search filters within the caller's own tenant ------------------------


async def test_organization_project_search_matches_name_and_slug(session, estate):
    alpha, _ = estate
    assert await _projects(session, alpha, "warehouse") == ["Alpha Warehouse"]
    assert await _projects(session, alpha, "alpha-ware") == ["Alpha Warehouse"]
    assert await _projects(session, alpha, "no-such-project") == []


async def test_organization_datasource_search_matches_name(session, estate):
    alpha, _ = estate
    assert await _datasources(session, alpha, "ledger") == ["Alpha Ledger"]
    assert await _datasources(session, alpha, "no-such-source") == []


async def test_organization_workspace_search_matches_name_and_slug(session, estate):
    alpha, _ = estate
    assert await _workspaces(session, alpha, "analytics") == ["Alpha Analytics"]
    assert await _workspaces(session, alpha, "alpha-analy") == ["Alpha Analytics"]
    assert await _workspaces(session, alpha, "no-such-workspace") == []


async def test_organization_search_matches_name_and_slug(session, estate):
    alpha, _ = estate
    assert await _organizations(session, alpha, "bank") == ["Alpha Bank"]
    assert await _organizations(session, alpha, "alpha") == ["Alpha Bank"]
    assert await _organizations(session, alpha, "no-such-org") == []


async def test_picker_search_is_case_insensitive_on_every_route(session, estate):
    alpha, _ = estate
    assert await _projects(session, alpha, "WAREHOUSE") == ["Alpha Warehouse"]
    assert await _datasources(session, alpha, "LEDGER") == ["Alpha Ledger"]
    assert await _workspaces(session, alpha, "ANALYTICS") == ["Alpha Analytics"]
    assert await _organizations(session, alpha, "BANK") == ["Alpha Bank"]


@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_absent_or_blank_search_behaves_as_no_filter(session, estate, blank):
    """The pre-F15 contract: no `q`, an empty `q=`, and a whitespace-only `q=`
    are all the unfiltered page -- never an error, never `contains("")`."""
    alpha, _ = estate
    assert await _projects(session, alpha, blank) == ["Alpha Warehouse"]
    assert await _datasources(session, alpha, blank) == ["Alpha Ledger"]
    assert await _workspaces(session, alpha, blank) == ["Alpha Analytics"]
    assert await _organizations(session, alpha, blank) == ["Alpha Bank"]


# --- the search never widens the tenant boundary ------------------------------
#
# One test per route, each searching for a term that matches a row that really
# exists in the database but belongs to the other organization.


async def test_organization_project_search_cannot_reach_another_organization(session, estate):
    alpha, beta = estate
    assert beta.project.name == "Beta Warehouse"  # the row exists...
    assert await _projects(session, alpha, "beta") == []  # ...and stays invisible
    assert await _projects(session, alpha, "beta-warehouse") == []


async def test_organization_datasource_search_cannot_reach_another_organization(session, estate):
    alpha, beta = estate
    assert beta.datasource.name == "Beta Ledger"
    assert await _datasources(session, alpha, "beta") == []
    assert await _datasources(session, alpha, "Beta Ledger") == []


async def test_organization_workspace_search_cannot_reach_another_organization(session, estate):
    alpha, beta = estate
    assert beta.workspace.name == "Beta Analytics"
    assert await _workspaces(session, alpha, "beta") == []
    assert await _workspaces(session, alpha, "beta-analytics") == []


async def test_organization_search_cannot_reach_another_organization(session, estate):
    alpha, beta = estate
    assert beta.organization.name == "Beta Bank"
    assert await _organizations(session, alpha, "beta") == []
    assert await _organizations(session, alpha, "bank") == ["Alpha Bank"]


async def test_platform_admin_organization_search_still_spans_tenants(session, estate):
    """The counterpart assertion: the filter narrows, it does not authorize.

    A PlatformAdmin was always able to list every organization; searching must
    not have taken that away, or the emptiness above would prove nothing about
    the tenant predicate.
    """
    alpha, _ = estate
    page = await list_organizations(
        q="bank",
        limit=50,
        offset=0,
        context=alpha.context(roles=frozenset({"PlatformAdmin"})),
        session=session,
    )
    assert sorted(item.name for item in page.items) == ["Alpha Bank", "Beta Bank"]


# --- fetch by id --------------------------------------------------------------


async def test_get_project_resolves_in_scope_and_refuses_other_organizations(session, estate):
    alpha, beta = estate
    resolved = await get_project(
        project_id=alpha.project.id, context=alpha.context(), session=session
    )
    assert resolved.id == alpha.project.id

    with pytest.raises(HTTPException) as cross_tenant:
        await get_project(project_id=beta.project.id, context=alpha.context(), session=session)
    assert cross_tenant.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        await get_project(project_id=uuid4(), context=alpha.context(), session=session)
    assert missing.value.status_code == 404


async def test_get_datasource_resolves_in_scope_and_refuses_other_organizations(session, estate):
    alpha, beta = estate
    resolved = await get_datasource(
        datasource_id=alpha.datasource.id, context=alpha.context(), session=session
    )
    assert resolved.id == alpha.datasource.id
    assert resolved.name == "Alpha Ledger"
    # The summary projection, not `DataSourceRead`: this route is readable down
    # to Viewer, and `DataSourceRead` carries `credential_reference`.
    assert not hasattr(resolved, "credential_reference")

    with pytest.raises(HTTPException) as cross_tenant:
        await get_datasource(
            datasource_id=beta.datasource.id, context=alpha.context(), session=session
        )
    assert cross_tenant.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        await get_datasource(datasource_id=uuid4(), context=alpha.context(), session=session)
    assert missing.value.status_code == 404


async def test_get_workspace_resolves_in_scope_and_refuses_other_organizations(session, estate):
    alpha, beta = estate
    resolved = await get_workspace(
        workspace_id=alpha.workspace.id, context=alpha.context(), session=session
    )
    assert resolved.id == alpha.workspace.id

    with pytest.raises(HTTPException) as cross_tenant:
        await get_workspace(
            workspace_id=beta.workspace.id, context=alpha.context(), session=session
        )
    assert cross_tenant.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        await get_workspace(workspace_id=uuid4(), context=alpha.context(), session=session)
    assert missing.value.status_code == 404


async def test_get_organization_resolves_in_scope_and_refuses_other_organizations(session, estate):
    alpha, beta = estate
    resolved = await get_organization(
        organization_id=alpha.organization.id, context=alpha.context(), session=session
    )
    assert resolved.id == alpha.organization.id

    with pytest.raises(HTTPException) as cross_tenant:
        await get_organization(
            organization_id=beta.organization.id, context=alpha.context(), session=session
        )
    assert cross_tenant.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        await get_organization(
            organization_id=uuid4(),
            context=alpha.context(roles=frozenset({"PlatformAdmin"})),
            session=session,
        )
    assert missing.value.status_code == 404


async def test_deep_linked_id_resolves_without_paging_the_estate(session, estate):
    """F15's actual complaint, expressed as a test.

    A resource that sits past the picker's page budget used to be reachable
    only by walking pages. Here the id is resolved directly while the list
    route is asked for a single-row page that does *not* contain it -- the by-id
    route answers anyway.
    """
    alpha, _ = estate
    extra = Project(
        id=uuid4(),
        organization_id=alpha.organization.id,
        line_of_business_id=alpha.project.line_of_business_id,
        data_domain_id=alpha.project.data_domain_id,
        name="Zeta Warehouse",
        slug="zeta-warehouse",
    )
    session.add(extra)
    await session.flush()

    first_page = await list_organization_projects(
        organization_id=alpha.organization.id,
        q=None,
        line_of_business_id=None,
        limit=1,
        offset=0,
        context=alpha.context(),
        session=session,
    )
    assert [item.id for item in first_page.items] == [alpha.project.id]
    assert first_page.total == 2

    resolved = await get_project(project_id=extra.id, context=alpha.context(), session=session)
    assert resolved.id == extra.id
    assert await _projects(session, alpha, "zeta") == ["Zeta Warehouse"]
