"""N8: document ingestion -- the data-dictionary-spreadsheet special case.

Real-sqlite-engine pattern (matching `test_playbooks.py`/
`test_catalog_bulk_actions_endpoints.py`): `resolve_structural_mappings`
issues real queries against `MetadataTable`/`MetadataColumn`/`DataSource`,
and `extract_description_claims` needs real flush-generated ids.
"""

import itertools
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.document_ingestion import (
    DOCUMENT_MAX_CONTENT_BYTES,
    DOCUMENT_MAX_SECTIONS,
    create_document_from_csv,
    extract_description_claims,
    parse_csv_data_dictionary,
    resolve_structural_mappings,
)
from aida.document_ingestion_api import (
    DocumentCreate,
    extract_claims,
    get_document,
    list_document_claims,
    list_document_mappings,
    list_document_sections,
    map_document,
    upload_document,
)
from aida.main import app
from aida.models import (
    AuditEvent,
    DataDomain,
    DataSource,
    DocumentClaim,
    GovernanceReview,
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

# Same sqlite-only AuditEvent.id workaround as test_playbooks.py.
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


async def _seed_project(session: AsyncSession) -> Project:
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
    return project


async def _seed_datasource(session: AsyncSession, project: Project, *, name: str) -> DataSource:
    datasource = DataSource(
        id=uuid4(),
        organization_id=project.organization_id,
        line_of_business_id=project.line_of_business_id,
        data_domain_id=project.data_domain_id,
        project_id=project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=project.organization_id,
        datasource_id=datasource.id,
        name=name,
        fingerprint="fp",
    )
    session.add_all([datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=project.organization_id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    return datasource


async def _seed_table(
    session: AsyncSession, datasource: DataSource, *, name: str
) -> MetadataTable:
    schema = (
        await session.scalars(
            select(MetadataSchema).where(MetadataSchema.catalog_id.in_(
                select(MetadataCatalog.id).where(MetadataCatalog.datasource_id == datasource.id)
            ))
        )
    ).one()
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


async def _seed_column(
    session: AsyncSession, table: MetadataTable, *, name: str
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=0,
        physical_type="VARCHAR",
        nullable=True,
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


def _context(project: Project, *, roles: frozenset[str] | None = None) -> SecurityContext:
    return SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=project.organization_id,
        roles=roles or frozenset({"DataSteward"}),
    )


_DICTIONARY_CSV = (
    "schema,table,column,description\n"
    "public,customers,customer_id,unique customer identifier\n"
    "public,customers,ssn,social security number of the customer\n"
    "public,orders,,one row per customer order\n"
)


# ---------------------------------------------------------------------------
# parse_csv_data_dictionary -- pure
# ---------------------------------------------------------------------------


def test_parse_csv_data_dictionary_parses_table_and_column_rows() -> None:
    result = parse_csv_data_dictionary(_DICTIONARY_CSV)

    assert result.error_count == 0
    assert result.truncated is False
    assert len(result.rows) == 3
    assert result.rows[0].column_name == "customer_id"
    assert result.rows[2].column_name is None
    assert result.rows[2].table_name == "orders"


def test_parse_csv_data_dictionary_is_case_insensitive_on_headers() -> None:
    csv_text = "Schema,Table,Column,Description\npublic,accounts,balance,current balance\n"

    result = parse_csv_data_dictionary(csv_text)

    assert len(result.rows) == 1
    assert result.rows[0].table_name == "accounts"


def test_parse_csv_data_dictionary_drops_and_counts_rows_missing_required_fields() -> None:
    csv_text = "schema,table,column,description\npublic,,id,missing table name\npublic,orders,,\n"

    result = parse_csv_data_dictionary(csv_text)

    assert result.rows == []
    assert result.error_count == 2


def test_parse_csv_data_dictionary_truncates_at_the_section_cap() -> None:
    header = "table,description\n"
    body = "".join(f"t{i},row {i}\n" for i in range(DOCUMENT_MAX_SECTIONS + 10))

    result = parse_csv_data_dictionary(header + body)

    assert len(result.rows) == DOCUMENT_MAX_SECTIONS
    assert result.truncated is True


# ---------------------------------------------------------------------------
# create_document_from_csv
# ---------------------------------------------------------------------------


async def test_create_document_from_csv_persists_sections(session: AsyncSession) -> None:
    project = await _seed_project(session)

    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by="steward@example.com",
    )

    assert document.status == "PARSED"
    assert document.section_count == 3
    assert document.parse_error_count == 0
    assert document.media_type == "CSV"
    assert len(document.sha256) == 64


async def test_create_document_from_csv_rejects_oversized_content(session: AsyncSession) -> None:
    project = await _seed_project(session)
    oversized = "table,description\n" + "a" * (DOCUMENT_MAX_CONTENT_BYTES + 1)

    with pytest.raises(HTTPException) as exc_info:
        await create_document_from_csv(
            session,
            organization_id=project.organization_id,
            project_id=project.id,
            filename="huge.csv",
            content=oversized,
            uploaded_by="steward@example.com",
        )
    assert exc_info.value.status_code == 413


# ---------------------------------------------------------------------------
# resolve_structural_mappings
# ---------------------------------------------------------------------------


async def test_resolve_structural_mappings_matches_table_and_column(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    await _seed_column(session, table, name="ssn")
    await _seed_table(session, datasource, name="orders")
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by="steward@example.com",
    )

    mappings = await resolve_structural_mappings(session, document)

    assert len(mappings) == 3
    assert all(mapping.mapping_kind == "STRUCTURAL" for mapping in mappings)
    column_mappings = [mapping for mapping in mappings if mapping.subject_type == "COLUMN"]
    table_mappings = [mapping for mapping in mappings if mapping.subject_type == "TABLE"]
    assert len(column_mappings) == 2
    assert len(table_mappings) == 1
    assert document.status == "MAPPED"


async def test_resolve_structural_mappings_reports_unmatched_rather_than_guessing(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    await _seed_datasource(session, project, name="primary")
    # No tables seeded at all -- every section should come back UNMATCHED.
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by="steward@example.com",
    )

    mappings = await resolve_structural_mappings(session, document)

    assert all(mapping.mapping_kind == "UNMATCHED" for mapping in mappings)
    assert all(mapping.subject_id is None for mapping in mappings)


async def test_resolve_structural_mappings_refuses_ambiguous_table_names(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    datasource_a = await _seed_datasource(session, project, name="ds-a")
    datasource_b = await _seed_datasource(session, project, name="ds-b")
    await _seed_table(session, datasource_a, name="orders")
    await _seed_table(session, datasource_b, name="orders")
    csv_text = "table,description\norders,ambiguous across two datasources\n"
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=csv_text,
        uploaded_by="steward@example.com",
    )

    mappings = await resolve_structural_mappings(session, document)

    assert len(mappings) == 1
    assert mappings[0].mapping_kind == "UNMATCHED"


# ---------------------------------------------------------------------------
# extract_description_claims + governance review dispatch
# ---------------------------------------------------------------------------


async def test_extract_description_claims_only_covers_structural_mappings(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    await _seed_column(session, table, name="ssn")
    # "orders" is not seeded -- its table-level row will be UNMATCHED.
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by="steward@example.com",
    )
    await resolve_structural_mappings(session, document)

    claims = await extract_description_claims(session, document, requested_by="steward@example.com")

    assert len(claims) == 2
    assert all(claim.status == "PENDING" for claim in claims)
    assert all(claim.predicate == "DESCRIBES" for claim in claims)
    review_ids = {claim.governance_review_id for claim in claims}
    assert len(review_ids) == 2
    reviews = (await session.scalars(select(GovernanceReview))).all()
    assert len(reviews) == 2
    assert all(review.object_type == "DOCUMENT_CLAIM" for review in reviews)


async def test_approving_a_claim_review_publishes_it_and_rejecting_does_not(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    await _seed_column(session, table, name="ssn")
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by="maker@example.com",
    )
    await resolve_structural_mappings(session, document)
    claims = await extract_description_claims(session, document, requested_by="maker@example.com")
    await session.commit()

    checker_context = SecurityContext(
        principal_id="checker@example.com",
        principal_type="USER",
        organization_id=project.organization_id,
        roles=frozenset({"DataSteward"}),
    )
    approved_claim, rejected_claim = claims[0], claims[1]

    await decide_governance_review(
        approved_claim.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        checker_context,
        session,
    )
    await decide_governance_review(
        rejected_claim.governance_review_id,
        GovernanceDecisionRequest(decision="REJECT", reason="description is inaccurate"),
        checker_context,
        session,
    )

    refreshed_approved = await session.get(DocumentClaim, approved_claim.id)
    refreshed_rejected = await session.get(DocumentClaim, rejected_claim.id)
    assert refreshed_approved is not None and refreshed_approved.status == "APPROVED"
    assert refreshed_approved.reviewed_by == "checker@example.com"
    assert refreshed_rejected is not None and refreshed_rejected.status == "REJECTED"


async def test_maker_cannot_check_their_own_claim(session: AsyncSession) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    await _seed_column(session, table, name="ssn")
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename="dictionary.csv",
        content=_DICTIONARY_CSV,
        uploaded_by="steward@example.com",
    )
    await resolve_structural_mappings(session, document)
    claims = await extract_description_claims(session, document, requested_by="steward@example.com")
    await session.commit()
    maker_context = _context(project)

    with pytest.raises(HTTPException) as exc_info:
        await decide_governance_review(
            claims[0].governance_review_id,
            GovernanceDecisionRequest(decision="APPROVE"),
            maker_context,
            session,
        )
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# REST layer -- direct function calls, matching test_playbooks.py's convention
# ---------------------------------------------------------------------------


def test_document_ingestion_endpoints_are_exposed() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/projects/{project_id}/documents",
        "/v1/documents/{document_id}",
        "/v1/documents/{document_id}/sections",
        "/v1/documents/{document_id}/map",
        "/v1/documents/{document_id}/mappings",
        "/v1/documents/{document_id}/extract-claims",
        "/v1/documents/{document_id}/claims",
    }
    assert expected <= paths.keys()
    assert "post" in paths["/v1/projects/{project_id}/documents"]
    assert "post" in paths["/v1/documents/{document_id}/map"]
    assert "post" in paths["/v1/documents/{document_id}/extract-claims"]


async def test_full_pipeline_through_the_api_layer(session: AsyncSession) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    await _seed_column(session, table, name="ssn")
    context = _context(project)

    document = await upload_document(
        project.id,
        DocumentCreate(filename="dictionary.csv", content=_DICTIONARY_CSV),
        context,
        session,
    )
    assert document.status == "PARSED"

    fetched = await get_document(document.id, context, session)
    assert fetched.id == document.id

    sections_page = await list_document_sections(document.id, 100, 0, context, session)
    assert sections_page.total == 3

    map_summary = await map_document(document.id, context, session)
    assert map_summary.matched_count == 2
    assert map_summary.unmatched_count == 1

    mappings_page = await list_document_mappings(document.id, 100, 0, context, session)
    assert mappings_page.total == 3

    claims_page = await extract_claims(document.id, context, session)
    assert claims_page.total == 2

    claims_list_page = await list_document_claims(document.id, 100, 0, context, session)
    assert claims_list_page.total == 2
    assert all(claim.status == "PENDING" for claim in claims_list_page.items)


async def test_map_document_refuses_a_document_not_in_parsed_status(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    await _seed_datasource(session, project, name="primary")
    context = _context(project)
    document = await upload_document(
        project.id,
        DocumentCreate(filename="dictionary.csv", content=_DICTIONARY_CSV),
        context,
        session,
    )
    await map_document(document.id, context, session)

    with pytest.raises(HTTPException) as exc_info:
        await map_document(document.id, context, session)
    assert exc_info.value.status_code == 409


async def test_extract_claims_refuses_a_document_not_yet_mapped(session: AsyncSession) -> None:
    project = await _seed_project(session)
    context = _context(project)
    document = await upload_document(
        project.id,
        DocumentCreate(filename="dictionary.csv", content=_DICTIONARY_CSV),
        context,
        session,
    )

    with pytest.raises(HTTPException) as exc_info:
        await extract_claims(document.id, context, session)
    assert exc_info.value.status_code == 409


async def test_get_document_rejects_cross_organization_access(session: AsyncSession) -> None:
    project = await _seed_project(session)
    context = _context(project)
    document = await upload_document(
        project.id,
        DocumentCreate(filename="dictionary.csv", content=_DICTIONARY_CSV),
        context,
        session,
    )
    other_context = SecurityContext(
        principal_id="intruder@example.com",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_document(document.id, other_context, session)
    assert exc_info.value.status_code == 403
