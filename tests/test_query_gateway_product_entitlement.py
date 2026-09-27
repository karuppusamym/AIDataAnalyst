"""R11-B4 follow-up -- a revoked data-product entitlement stops the query, not
just the marketplace claim.

B4 made an approved grant real and made revocation visible at
`POST /v1/marketplace/products/{version_id}/consume`. That checkpoint hands
back a product's ports; it does not stand between the consumer and the
warehouse. So a consumer who had claimed the product once already knew the
table names, and `QueryExecutionGateway` -- the INV-2 choke point, the only
path to a source -- authorized them on workspace/ABAC alone and never read the
grant. Revoking took away the claim ticket and left the data reachable.

These tests pin the close, and equally pin its limits, because the limits are
the design:

* a revoked grant refuses the real `execute` path, through
  `AuthorizationRejected` carrying the entitlement's own reason code;
* an expired grant does the same, since expiry is also "had it, lost it";
* a principal who never requested the product is **not** affected -- the
  entitlement can subtract access at this choke point and never add a
  requirement, so this check cannot break every existing query over a table
  that happens to be a product output;
* a pending or rejected request is likewise not a denial here, for the same
  reason: neither says the principal lost anything;
* a revocation is per-principal, not per-product;
* `validate` agrees with `execute`, so an agent is never told a statement is
  fine that the money path will refuse.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AccessPolicy,
    DataDomain,
    DataProduct,
    DataProductAccessRequest,
    DataProductPort,
    DataProductVersion,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SourceBinding,
    Workspace,
    WorkspaceMembership,
)
from aida.query_gateway import AuthorizationRejected, QueryExecutionGateway
from aida.workspace_access import ENFORCE
from tests.support.doubles import FakeSqlExecutor, security_context

_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


# --------------------------------------------------------------------------- #
# Estate: one workspace that allows the read, one table, one published product
# whose OUTPUT port is that table.
# --------------------------------------------------------------------------- #


async def _estate(
    session: AsyncSession, *, principal_id: str = "alice"
) -> tuple[Organization, DataSource, Workspace, MetadataTable, DataProductVersion]:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:4]}")
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Core", code="CORE"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()

    catalog = MetadataCatalog(
        organization_id=org.id, datasource_id=datasource.id, name="bank", fingerprint="c"
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="s"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customers",
        object_type="BASE_TABLE",
        fingerprint="t",
        status="ACTIVE",
    )
    session.add(table)
    await session.flush()
    session.add(
        MetadataColumn(
            organization_id=org.id,
            table_id=table.id,
            name="customer_id",
            ordinal_position=1,
            physical_type="varchar",
            nullable=False,
            classification="UNCLASSIFIED",
            status="ACTIVE",
            fingerprint="fp",
        )
    )

    # The workspace allows the read outright. That is the point: every refusal
    # below has to come from the entitlement, because ABAC has already said yes.
    workspace = Workspace(
        organization_id=org.id,
        name="Analytics",
        slug=f"w-{uuid4().hex[:6]}",
        purpose="analysis",
        authorization_mode=ENFORCE,
    )
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMembership(
            organization_id=org.id,
            workspace_id=workspace.id,
            principal_id=principal_id,
            role="analyst",
            status="ACTIVE",
            granted_by="test",
        )
    )
    session.add(
        SourceBinding(
            organization_id=org.id,
            workspace_id=workspace.id,
            datasource_id=datasource.id,
            purpose="analysis",
            status="ACTIVE",
            requested_by="test",
        )
    )
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="baseline-allow",
            name="baseline allow",
            effect="ALLOW",
            subject_match={"roles": ["analyst"]},
            action_match=[],
            created_by="test",
        )
    )

    product = DataProduct(
        organization_id=org.id,
        project_id=project.id,
        product_key=f"customer-360-{uuid4().hex[:6]}",
        lifecycle_status="ACTIVE",
        created_by="owner",
    )
    session.add(product)
    await session.flush()
    version = DataProductVersion(
        organization_id=org.id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Customer 360",
        description="Customer master",
        domain_name="Retail",
        owner_principal="owner",
        usage_terms="internal use",
        classification="INTERNAL",
        # Empty on purpose: no role grants this product, so the entitlement is
        # the only possible basis. With a matching consumer role,
        # `role_has_product_access` would short-circuit every assertion below
        # to ALLOW and the suite would prove nothing.
        discoverable_roles=[],
        consumer_roles=[],
        fingerprint="f",
        created_by="owner",
    )
    session.add(version)
    await session.flush()
    session.add(
        DataProductPort(
            organization_id=org.id,
            data_product_version_id=version.id,
            port_key="customers",
            direction="OUTPUT",
            name="customers",
            description="the customer table",
            asset_type="TABLE",
            asset_id=str(table.id),
        )
    )
    await session.flush()
    return org, datasource, workspace, table, version


async def _access_request(
    session: AsyncSession,
    org: Organization,
    version: DataProductVersion,
    *,
    principal_id: str,
    status: str,
    fulfillment_status: str = "PROVISIONED",
    expires_at: datetime | None = None,
    revoked_by: str | None = None,
    revoked_at: datetime | None = None,
) -> DataProductAccessRequest:
    review = GovernanceReview(
        organization_id=org.id,
        object_type="DATA_PRODUCT_ACCESS",
        object_id=str(version.id),
        requested_action="GRANT",
        status="APPROVED",
        requested_by=principal_id,
    )
    session.add(review)
    await session.flush()
    request = DataProductAccessRequest(
        organization_id=org.id,
        data_product_version_id=version.id,
        requested_by=principal_id,
        purpose="analysis",
        duration_days=30,
        status=status,
        governance_review_id=review.id,
        expires_at=expires_at,
        revoked_by=revoked_by,
        revoked_at=revoked_at,
        fulfillment_status=fulfillment_status,
    )
    session.add(request)
    await session.flush()
    return request


def _gateway() -> QueryExecutionGateway:
    return QueryExecutionGateway(Settings(_env_file=None))


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeSqlExecutor(({"customer_id": "c-1"},))
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session", lambda connector_type, dsn: executor
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type("_Resolver", (), {"resolve": staticmethod(lambda ref: "dsn://x")})(),
    )


async def _execute(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    workspace: Workspace,
    *,
    principal_id: str = "alice",
) -> object:
    return await _gateway().execute(
        session,
        datasource=datasource,
        context=security_context(organization_id=org.id, principal_id=principal_id),
        correlation_id=f"corr-{uuid4().hex[:8]}",
        sql="SELECT customer_id FROM customers",
        requested_limit=10,
        semantic_version=None,
        workspace_id=workspace.id,
    )


# --------------------------------------------------------------------------- #
# 1. The defect this closes
# --------------------------------------------------------------------------- #


async def test_a_revoked_entitlement_refuses_the_real_query(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline. Same workspace, same ALLOW policy, same statement that
    succeeds in `test_a_live_grant_still_executes` -- the only difference is
    the revoked grant, so the refusal can only have come from it."""
    org, datasource, workspace, _table, version = await _estate(session)
    await _access_request(
        session,
        org,
        version,
        principal_id="alice",
        status="REVOKED",
        fulfillment_status="REVOKED",
        revoked_by="steward@bank",
        revoked_at=_NOW - timedelta(hours=1),
    )
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _execute(session, org, datasource, workspace)

    assert excinfo.value.reason_code == "entitlement_revoked"


async def test_an_expired_entitlement_refuses_the_real_query(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is the other way a grant ends. A time-boxed approval that nobody
    got round to revoking has to stop working on its own, or `duration_days`
    is decoration."""
    org, datasource, workspace, _table, version = await _estate(session)
    await _access_request(
        session,
        org,
        version,
        principal_id="alice",
        status="APPROVED",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _execute(session, org, datasource, workspace)

    assert excinfo.value.reason_code == "entitlement_expired"


async def test_validate_refuses_what_execute_would_refuse(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pipeline, two entry points. An agent asking "is this statement OK?"
    must not be told yes about a statement the money path will refuse."""
    org, datasource, workspace, _table, version = await _estate(session)
    await _access_request(
        session,
        org,
        version,
        principal_id="alice",
        status="REVOKED",
        fulfillment_status="REVOKED",
        revoked_by="steward@bank",
        revoked_at=_NOW - timedelta(hours=1),
    )
    _patch_executor(monkeypatch)

    with pytest.raises(AuthorizationRejected) as excinfo:
        await _gateway().validate(
            session,
            datasource=datasource,
            context=security_context(organization_id=org.id, principal_id="alice"),
            correlation_id="corr-validate",
            sql="SELECT customer_id FROM customers",
            requested_limit=10,
            workspace_id=workspace.id,
        )

    assert excinfo.value.reason_code == "entitlement_revoked"


# --------------------------------------------------------------------------- #
# 2. The limits, which are the design and not an omission
# --------------------------------------------------------------------------- #


async def test_a_principal_who_never_requested_the_product_is_unaffected(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The falsifier for the whole change. If this failed, the check would have
    turned every product output table into entitlement-gated data and refused
    every existing query over one -- a far larger policy change than making
    revocation bite, and not the one that was asked for."""
    org, datasource, workspace, _table, _version = await _estate(session)
    _patch_executor(monkeypatch)

    assert await _execute(session, org, datasource, workspace) is not None


@pytest.mark.parametrize("status", ["PENDING", "REJECTED"])
async def test_a_request_that_never_granted_anything_is_not_a_denial(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """PENDING and REJECTED both mean no grant was ever made through this
    product. Neither says anything about whether the principal may read the
    table by some other right, so neither refuses here -- only revocation and
    expiry, which mean access existed and ended, travel to the query path."""
    org, datasource, workspace, _table, version = await _estate(session)
    await _access_request(
        session,
        org,
        version,
        principal_id="alice",
        status=status,
        fulfillment_status="NOT_REQUESTED",
    )
    _patch_executor(monkeypatch)

    assert await _execute(session, org, datasource, workspace) is not None


async def test_another_principals_revocation_does_not_refuse_this_one(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check is per-principal. A revocation is a statement about who lost
    access; a query matched on the product alone would refuse the whole
    organization the moment one person's grant was withdrawn."""
    org, datasource, workspace, _table, version = await _estate(session)
    await _access_request(
        session,
        org,
        version,
        principal_id="mallory",
        status="REVOKED",
        fulfillment_status="REVOKED",
        revoked_by="steward@bank",
        revoked_at=_NOW - timedelta(hours=1),
    )
    _patch_executor(monkeypatch)

    assert await _execute(session, org, datasource, workspace) is not None


async def test_a_live_grant_still_executes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control for the revoked case: identical estate, an
    APPROVED and PROVISIONED grant, and the statement runs."""
    org, datasource, workspace, _table, version = await _estate(session)
    await _access_request(
        session,
        org,
        version,
        principal_id="alice",
        status="APPROVED",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    _patch_executor(monkeypatch)

    assert await _execute(session, org, datasource, workspace) is not None
