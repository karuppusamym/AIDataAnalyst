"""The datasource model workbook: composition and the download endpoint.

Asserted against the parsed workbook rather than the composer's return value
wherever the claim is about what a steward will actually see -- the sheets are
the deliverable, and a header that composes correctly but is written into the
wrong column is still a broken export.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from defusedxml import ElementTree
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.column_documentation import publish_column_description
from aida.config import Settings
from aida.db import Base
from aida.model_export import (
    COLUMN_SHEET,
    README_SHEET,
    RELATIONSHIP_SHEET,
    TABLE_SHEET,
    compose_model_workbook,
)
from aida.model_export_api import export_datasource_model
from aida.models import (
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    RelationshipCandidate,
)
from aida.security import SecurityContext
from aida.xlsx import write_workbook

_SETTINGS = Settings()
_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

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


async def _seed(session: AsyncSession) -> tuple[DataSource, MetadataTable, MetadataColumn]:
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
        name="Retail Warehouse / PROD",
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
        source_description="customer master",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name="customer_id",
        ordinal_position=0,
        physical_type="UUID",
        nullable=False,
        source_description="pk, from DDL comment",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return datasource, table, column


def _context(datasource: DataSource, *, roles: frozenset[str] | None = None) -> SecurityContext:
    return SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=roles or frozenset({"DataSteward"}),
    )


async def _compose(session, datasource):
    return await compose_model_workbook(
        session,
        datasource=datasource,
        generated_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        generated_by="steward@example.com",
    )


def _sheet_rows(content: bytes, sheet_index: int) -> list[list[str | None]]:
    archive = zipfile.ZipFile(io.BytesIO(content))
    root = ElementTree.fromstring(
        archive.read(f"xl/worksheets/sheet{sheet_index}.xml").decode("utf-8")
    )
    rows = []
    for row in root.findall(".//main:sheetData/main:row", _NS):
        values: list[str | None] = []
        for cell in row.findall("main:c", _NS):
            inline = cell.find("main:is/main:t", _NS)
            numeric = cell.find("main:v", _NS)
            values.append(
                inline.text
                if inline is not None
                else (numeric.text if numeric is not None else None)
            )
        rows.append(values)
    return rows


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def test_workbook_leads_with_the_readme_then_the_three_data_sheets(session) -> None:
    datasource, _, _ = await _seed(session)
    composition = await _compose(session, datasource)
    assert [sheet.name for sheet in composition.sheets] == [
        README_SHEET,
        TABLE_SHEET,
        COLUMN_SHEET,
        RELATIONSHIP_SHEET,
    ]


async def test_column_sheet_carries_both_descriptions_in_separate_columns(session) -> None:
    """The point of the workbook: a steward can see the source comment they
    must not edit next to the authored description they may.
    """
    datasource, table, column = await _seed(session)
    await publish_column_description(
        session,
        organization_id=datasource.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="Unique customer identifier across retail systems.",
        created_by="maker@example.com",
        approved_by="checker@example.com",
        approved_at=datetime.now(UTC),
    )

    composition = await _compose(session, datasource)
    columns_sheet = next(s for s in composition.sheets if s.name == COLUMN_SHEET)
    row = dict(zip(columns_sheet.headers, columns_sheet.rows[0], strict=True))

    assert row["column_id"] == str(column.id)
    assert row["table_id"] == str(table.id)
    assert row["schema"] == "public"
    assert row["table"] == "customers"
    assert row["column"] == "customer_id"
    assert row["source_description"] == "pk, from DDL comment"
    assert row["business_description"] == "Unique customer identifier across retail systems."
    assert row["description_version"] == 1
    assert row["approved_by"] == "checker@example.com"


async def test_every_data_sheet_carries_a_stable_identifier_column(session) -> None:
    """Re-import matches on these. A sheet that shipped without one would
    force name-matching, which silently drops renamed objects.
    """
    datasource, _, _ = await _seed(session)
    composition = await _compose(session, datasource)
    by_name = {sheet.name: sheet for sheet in composition.sheets}
    assert by_name[TABLE_SHEET].headers[0] == "table_id"
    assert by_name[COLUMN_SHEET].headers[0] == "column_id"
    assert by_name[RELATIONSHIP_SHEET].headers[0] == "relationship_id"


async def test_deprecated_objects_are_excluded(session) -> None:
    datasource, table, column = await _seed(session)
    column.status = "DEPRECATED"
    await session.flush()

    composition = await _compose(session, datasource)
    assert composition.row_counts[TABLE_SHEET] == 1
    assert composition.row_counts[COLUMN_SHEET] == 0


async def test_another_datasources_objects_are_not_included(session) -> None:
    datasource, _, _ = await _seed(session)
    other_datasource, _, _ = await _seed(session)

    composition = await _compose(session, datasource)
    tables_sheet = next(s for s in composition.sheets if s.name == TABLE_SHEET)
    assert composition.row_counts[TABLE_SHEET] == 1
    assert len(tables_sheet.rows) == 1
    assert other_datasource.id != datasource.id


async def test_same_datasource_relationships_are_included(session) -> None:
    datasource, table, column = await _seed(session)
    target = MetadataColumn(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=table.id,
        name="referrer_id",
        ordinal_position=1,
        physical_type="UUID",
        nullable=True,
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(target)
    await session.flush()
    session.add(
        RelationshipCandidate(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            target_datasource_id=datasource.id,
            source_table_id=table.id,
            source_column_id=target.id,
            target_table_id=table.id,
            target_column_id=column.id,
            detection_rule="NAME_AND_TYPE",
            confidence=0.9,
            status="APPROVED",
            created_by="discovery",
        )
    )
    await session.flush()

    composition = await _compose(session, datasource)
    sheet = next(s for s in composition.sheets if s.name == RELATIONSHIP_SHEET)
    row = dict(zip(sheet.headers, sheet.rows[0], strict=True))
    assert row["source_column"] == "referrer_id"
    assert row["target_column"] == "customer_id"
    assert row["status"] == "APPROVED"


async def test_cross_source_relationships_are_omitted(session) -> None:
    """Rendering a cross-domain edge needs a per-read cross-boundary grant
    check this export does not perform, so it must not render one.
    """
    datasource, table, column = await _seed(session)
    other_datasource, other_table, other_column = await _seed(session)
    session.add(
        RelationshipCandidate(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            target_datasource_id=other_datasource.id,
            source_table_id=table.id,
            source_column_id=column.id,
            target_table_id=other_table.id,
            target_column_id=other_column.id,
            detection_rule="NAME_AND_TYPE",
            confidence=0.9,
            status="APPROVED",
            created_by="discovery",
        )
    )
    await session.flush()

    composition = await _compose(session, datasource)
    assert composition.row_counts[RELATIONSHIP_SHEET] == 0


async def test_readme_names_the_scope_and_the_editable_columns(session) -> None:
    datasource, _, _ = await _seed(session)
    composition = await _compose(session, datasource)
    readme = next(s for s in composition.sheets if s.name == README_SHEET)
    fields = {row[0]: row[1] for row in readme.rows if row[0]}

    assert fields["Datasource id"] == str(datasource.id)
    assert fields["Generated by"] == "steward@example.com"
    assert "business_description" in str(fields["Editable columns"])
    # The instruction that keeps a steward from breaking their own re-upload.
    assert "Do not edit" in str(fields["Identifier columns"])


async def test_nothing_is_truncated_at_this_size(session) -> None:
    datasource, _, _ = await _seed(session)
    composition = await _compose(session, datasource)
    assert composition.any_truncated is False


async def test_the_composed_workbook_is_a_readable_package(session) -> None:
    datasource, table, column = await _seed(session)
    composition = await _compose(session, datasource)
    content = write_workbook(composition.sheets)

    # Sheet 3 is Columns: README, Tables, Columns, Relationships.
    rows = _sheet_rows(content, 3)
    assert rows[0][0] == "column_id"
    assert rows[1][0] == str(column.id)
    assert rows[1][4] == "customer_id"
    assert table.id is not None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


async def test_endpoint_returns_an_xlsx_attachment_with_a_content_hash(session) -> None:
    datasource, _, _ = await _seed(session)

    response = await export_datasource_model(
        datasource.id,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )

    assert response.status_code == 200
    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.body[:2] == b"PK"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert str(datasource.id) in disposition
    # The datasource name carries a space and a slash; neither may reach the
    # header raw.
    assert " / " not in disposition
    assert len(response.headers["x-artifact-sha256"]) == 64
    assert response.headers["x-export-truncated"] == "false"


async def test_endpoint_hash_covers_the_bytes_it_returns(session) -> None:
    """The hash's job is proving an attached file is the one the platform
    produced, so it must be computed over the response body itself.

    It is deliberately *not* stable across downloads: the README sheet records
    when the snapshot was taken, so two exports of an unchanged model differ.
    See this endpoint's module docstring.
    """
    datasource, _, _ = await _seed(session)
    response = await export_datasource_model(
        datasource.id, context=_context(datasource), session=session, settings=_SETTINGS
    )
    assert response.headers["x-artifact-sha256"] == hashlib.sha256(response.body).hexdigest()


async def test_composition_is_deterministic_for_identical_input(session) -> None:
    """Determinism lives in the writer: given the same composition, the bytes
    are identical, so a hash difference always means a content difference and
    never zip nondeterminism.
    """
    datasource, _, _ = await _seed(session)
    first = await _compose(session, datasource)
    second = await _compose(session, datasource)
    assert write_workbook(first.sheets) == write_workbook(second.sheets)


async def test_endpoint_404s_for_an_unknown_datasource(session) -> None:
    datasource, _, _ = await _seed(session)
    with pytest.raises(HTTPException) as exc_info:
        await export_datasource_model(
            uuid4(), context=_context(datasource), session=session, settings=_SETTINGS
        )
    assert exc_info.value.status_code == 404


async def test_endpoint_refuses_a_datasource_in_another_organization(session) -> None:
    datasource, _, _ = await _seed(session)
    foreign_context = SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )
    with pytest.raises(HTTPException) as exc_info:
        await export_datasource_model(
            datasource.id, context=foreign_context, session=session, settings=_SETTINGS
        )
    assert exc_info.value.status_code in (403, 404)
