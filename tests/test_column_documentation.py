"""Column-level business description of record: the store, the publish path
that fills it, and the read endpoint that surfaces it.

Real-sqlite-engine pattern, matching `test_document_ingestion.py`: the publish
path runs inside `decide_governance_review` against real rows and needs real
flush-generated ids and a real supersede `UPDATE`, none of which a mock would
exercise.

The behaviour under test is the one `DocumentClaim`'s docstring used to
describe as missing: before `aida.column_documentation` existed, approving a
column `DESCRIBES` claim moved a status and published nothing, so no reader
could resolve the description a steward had just approved.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.column_documentation import (
    current_descriptions_by_column_id,
    current_descriptions_for_table,
    publish_column_description,
)
from aida.column_documentation_api import list_column_documentation
from aida.config import Settings
from aida.db import Base
from aida.document_ingestion import (
    create_document_from_csv,
    extract_description_claims,
    resolve_structural_mappings,
)
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    AuditEvent,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review

_SETTINGS = Settings()
_MAKER = "maker@example.com"
_CHECKER = "checker@example.com"

# Same sqlite-only AuditEvent.id workaround as test_document_ingestion.py.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


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


async def _seed_catalog(session: AsyncSession) -> tuple[Project, DataSource, MetadataTable]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
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
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    session.add_all([org, lob, domain, project])
    await session.flush()

    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="warehouse",
        fingerprint="fp",
    )
    session.add_all([datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customers",
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return project, datasource, table


async def _seed_column(
    session: AsyncSession,
    table: MetadataTable,
    *,
    name: str,
    ordinal: int = 0,
    source_description: str | None = None,
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=ordinal,
        physical_type="VARCHAR",
        nullable=True,
        source_description=source_description,
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


def _context(organization_id, principal: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


async def test_first_publish_creates_the_parent_and_version_one(session) -> None:
    _, _, table = await _seed_catalog(session)
    column = await _seed_column(session, table, name="customer_id")

    version = await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="Unique customer identifier.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )

    assert version.version == 1
    assert version.status == "APPROVED"
    parent = await session.scalar(
        select(ColumnDocumentation).where(ColumnDocumentation.column_id == column.id)
    )
    assert parent is not None
    # Denormalized so table-scoped reads need no join back through the column.
    assert parent.table_id == table.id


async def test_republishing_supersedes_rather_than_overwrites(session) -> None:
    """Append-only is the whole point: a run grounded on the old text has to
    stay resolvable against exactly that text after a later approval.
    """
    _, _, table = await _seed_catalog(session)
    column = await _seed_column(session, table, name="customer_id")
    common = {
        "organization_id": table.organization_id,
        "table_id": table.id,
        "column_id": column.id,
        "created_by": _MAKER,
        "approved_by": _CHECKER,
    }

    first = await publish_column_description(
        session, description="First wording.", approved_at=datetime.now(UTC), **common
    )
    second = await publish_column_description(
        session, description="Second wording.", approved_at=datetime.now(UTC), **common
    )

    assert (first.version, second.version) == (1, 2)
    await session.refresh(first)
    assert first.status == "SUPERSEDED"
    assert first.description == "First wording."
    assert second.status == "APPROVED"

    # Exactly one parent, two versions -- not two parents.
    parents = (
        (
            await session.execute(
                select(ColumnDocumentation).where(ColumnDocumentation.column_id == column.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(parents) == 1


async def test_resolvers_return_only_the_current_version(session) -> None:
    _, _, table = await _seed_catalog(session)
    column = await _seed_column(session, table, name="customer_id")
    common = {
        "organization_id": table.organization_id,
        "table_id": table.id,
        "column_id": column.id,
        "created_by": _MAKER,
        "approved_by": _CHECKER,
    }
    await publish_column_description(
        session, description="Old.", approved_at=datetime.now(UTC), **common
    )
    await publish_column_description(
        session, description="Current.", approved_at=datetime.now(UTC), **common
    )

    by_id = await current_descriptions_by_column_id(session, [column.id])
    by_table = await current_descriptions_for_table(session, table.id)
    assert by_id[column.id].description == "Current."
    assert by_table[column.id].description == "Current."


async def test_resolvers_omit_columns_with_no_approved_description(session) -> None:
    """Absence is normal, not an error: most columns have never been
    described, and a caller renders the source comment instead.
    """
    _, _, table = await _seed_catalog(session)
    described = await _seed_column(session, table, name="customer_id", ordinal=0)
    undescribed = await _seed_column(session, table, name="ssn", ordinal=1)
    await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=described.id,
        description="Unique customer identifier.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )

    resolved = await current_descriptions_by_column_id(session, [described.id, undescribed.id])
    assert set(resolved) == {described.id}
    assert await current_descriptions_by_column_id(session, []) == {}


# ---------------------------------------------------------------------------
# The publish path: approving a document claim
# ---------------------------------------------------------------------------


_DICTIONARY_CSV = (
    "schema,table,column,description\n"
    "public,customers,customer_id,unique customer identifier\n"
    "public,customers,,one row per customer\n"
)


async def _claims_for_dictionary(session, project, table):
    await _seed_column(session, table, name="customer_id")
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by=_MAKER,
    )
    await resolve_structural_mappings(session, document)
    claims = await extract_description_claims(session, document, requested_by=_MAKER)
    await session.commit()
    return {claim.subject_type: claim for claim in claims}


async def test_approving_a_column_claim_publishes_a_resolvable_description(session) -> None:
    project, _, table = await _seed_catalog(session)
    claims = await _claims_for_dictionary(session, project, table)
    claim = claims["COLUMN"]

    await decide_governance_review(
        claim.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(project.organization_id, _CHECKER),
        session,
    )

    column_id = UUID(claim.subject_id)
    published = (await current_descriptions_by_column_id(session, [column_id]))[column_id]
    assert published.description == "unique customer identifier"
    # Traceable back through the claim to the exact uploaded source text.
    assert published.source_claim_id == claim.id
    # Maker and checker stay distinguishable on the published version.
    assert published.created_by == _MAKER
    assert published.approved_by == _CHECKER


async def test_approving_a_table_claim_publishes_asset_documentation(session) -> None:
    """A data dictionary carries table rows as well as column rows; approving
    one had to land somewhere rather than silently do nothing.
    """
    project, _, table = await _seed_catalog(session)
    claims = await _claims_for_dictionary(session, project, table)

    await decide_governance_review(
        claims["TABLE"].governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(project.organization_id, _CHECKER),
        session,
    )

    version = await session.scalar(
        select(AssetDocumentationVersion)
        .join(
            AssetDocumentation,
            AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
        )
        .where(AssetDocumentation.table_id == table.id)
    )
    assert version is not None
    assert version.readme == "one row per customer"
    assert version.status == "APPROVED"
    assert version.created_by == _MAKER
    assert version.approved_by == _CHECKER


async def test_rejecting_a_column_claim_publishes_nothing(session) -> None:
    project, _, table = await _seed_catalog(session)
    claims = await _claims_for_dictionary(session, project, table)

    await decide_governance_review(
        claims["COLUMN"].governance_review_id,
        GovernanceDecisionRequest(decision="REJECT", reason="not accurate for this column"),
        _context(project.organization_id, _CHECKER),
        session,
    )

    versions = (await session.execute(select(ColumnDocumentationVersion))).scalars().all()
    assert versions == []


async def test_a_claim_whose_column_vanished_approves_without_publishing(session) -> None:
    """The catalog object can be dropped between mapping and review. The
    steward's decision on the text still stands, but nothing is published
    against an id that no longer resolves.
    """
    project, _, table = await _seed_catalog(session)
    claims = await _claims_for_dictionary(session, project, table)
    claim = claims["COLUMN"]
    column = await session.get(MetadataColumn, UUID(claim.subject_id))
    await session.delete(column)
    await session.flush()

    await decide_governance_review(
        claim.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(project.organization_id, _CHECKER),
        session,
    )

    await session.refresh(claim)
    assert claim.status == "APPROVED"
    assert (await session.execute(select(ColumnDocumentationVersion))).scalars().all() == []


# ---------------------------------------------------------------------------
# The read endpoint
# ---------------------------------------------------------------------------


async def test_endpoint_keeps_source_and_authored_descriptions_apart(session) -> None:
    """The two are not interchangeable: one is overwritten by the next
    rediscovery pass and one is not, so the pane must be able to tell them
    apart.
    """
    _, _, table = await _seed_catalog(session)
    column = await _seed_column(
        session, table, name="customer_id", source_description="pk, from DDL comment"
    )
    await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="The customer's unique identifier across retail systems.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )

    page = await list_column_documentation(
        table.id,
        limit=200,
        offset=0,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )

    assert page.total == 1
    item = page.items[0]
    assert item.source_description == "pk, from DDL comment"
    assert item.business_description == ("The customer's unique identifier across retail systems.")
    assert item.description_version == 1
    assert item.description_approved_by == _CHECKER


async def test_endpoint_returns_undescribed_columns_with_a_null_description(session) -> None:
    _, _, table = await _seed_catalog(session)
    await _seed_column(session, table, name="customer_id", ordinal=0)
    await _seed_column(session, table, name="ssn", ordinal=1)

    page = await list_column_documentation(
        table.id,
        limit=200,
        offset=0,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )

    assert page.total == 2
    assert [item.name for item in page.items] == ["customer_id", "ssn"]
    assert all(item.business_description is None for item in page.items)


async def test_endpoint_excludes_deprecated_columns(session) -> None:
    _, _, table = await _seed_catalog(session)
    await _seed_column(session, table, name="customer_id", ordinal=0)
    dropped = await _seed_column(session, table, name="legacy_id", ordinal=1)
    dropped.status = "DEPRECATED"
    await session.flush()

    page = await list_column_documentation(
        table.id,
        limit=200,
        offset=0,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )

    assert [item.name for item in page.items] == ["customer_id"]
