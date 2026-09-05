"""The workbook round trip end to end: export, edit, upload, review, apply.

Every test here starts from a real `compose_model_workbook` export and edits
the file, rather than hand-building an upload. That is deliberate: the bug
this pass most needs to not have is the export and the import disagreeing
about a header name or a version column, and a test that constructs its own
upload would never catch it.

Real-sqlite-engine pattern, matching `test_document_ingestion.py` -- the apply
path runs inside `decide_governance_review` against real rows, and the whole
point of the stale check is a real second write landing between export and
upload.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.config import Settings
from aida.db import Base
from aida.model_export import (
    COLUMN_SHEET,
    EDITABLE_COLUMNS,
    RELATIONSHIP_SHEET,
    TABLE_SHEET,
    compose_model_workbook,
)
from aida.model_import import (
    COLUMN_FIELDS,
    TABLE_FIELDS,
    apply_model_import_batch,
    parse_and_diff_workbook,
    set_change_exclusion,
    submit_batch_for_review,
)
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ModelImportChange,
    Organization,
    Project,
)
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.xlsx import Sheet, write_workbook
from aida.xlsx_reader import read_workbook

_SETTINGS = Settings()
_MAKER = "maker@example.com"
_CHECKER = "checker@example.com"

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
        name="Retail",
        code=f"RET{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:8]}",
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
        credential_reference="env://X",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="wh",
        fingerprint="fp",
    )
    session.add_all([datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
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
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name="customer_id",
        ordinal_position=0,
        physical_type="uuid",
        nullable=False,
        source_description="pk",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return datasource, table, column


async def _seed_annotation(
    session: AsyncSession, datasource: DataSource, table: MetadataTable
) -> MetadataBusinessAnnotation:
    annotation = MetadataBusinessAnnotation(
        id=uuid4(),
        organization_id=table.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=uuid4(),
    )
    session.add(annotation)
    await session.flush()
    session.add(
        MetadataBusinessAnnotationVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            annotation_id=annotation.id,
            version=1,
            status="APPROVED",
            business_name="Customers",
            business_description="Original annotation description.",
            table_role="DIMENSION",
            grain_statement="One row per customer.",
            synonyms=["clients"],
            suggested_questions=["How many customers?"],
            tags=["retail"],
            confidence=0.8,
            approved_by=_CHECKER,
            approved_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return annotation


def _context(organization_id, principal: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )


async def _export(session: AsyncSession, datasource: DataSource) -> bytes:
    composition = await compose_model_workbook(
        session,
        datasource=datasource,
        generated_at=datetime(2026, 9, 5, tzinfo=UTC),
        generated_by=_MAKER,
    )
    return write_workbook(composition.sheets)


def _rewrite(content: bytes, mutate) -> bytes:
    """Read a workbook back, let `mutate` change it, and write it out again.

    Stands in for "a steward opened this in Excel, edited a cell and saved".
    """
    sheets = read_workbook(content)
    rebuilt: list[Sheet] = []
    for name, parsed in sheets.items():
        rows = [[row.get(header, "") for header in parsed.headers] for row in parsed.rows]
        mutate(name, parsed.headers, rows)
        rebuilt.append(Sheet(name=name, headers=list(parsed.headers), rows=rows))
    return write_workbook(rebuilt)


def _set_cell(sheet_name: str, field: str, value: str, row: int = 0):
    def mutate(name: str, headers: list[str], rows: list[list[object]]) -> None:
        if name != sheet_name or not rows:
            return
        rows[row][headers.index(field)] = value

    return mutate


async def _changes(session: AsyncSession, batch_id) -> list[ModelImportChange]:
    return list(
        (
            await session.execute(
                select(ModelImportChange)
                .where(ModelImportChange.batch_id == batch_id)
                .order_by(ModelImportChange.row_number, ModelImportChange.field)
            )
        )
        .scalars()
        .all()
    )


async def _upload(session, datasource, content, *, filename="model.xlsx"):
    return await parse_and_diff_workbook(
        session,
        datasource=datasource,
        content=content,
        filename=filename,
        uploaded_by=_MAKER,
    )


async def _approve(session, batch, organization_id):
    await submit_batch_for_review(session, batch, requested_by=_MAKER)
    await session.commit()
    await decide_governance_review(
        batch.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(organization_id, _CHECKER),
        session,
    )


# ---------------------------------------------------------------------------
# The export and the import must agree about what is editable
# ---------------------------------------------------------------------------


def test_the_readme_promises_exactly_the_fields_the_import_reads_back() -> None:
    """The README sheet tells a steward which columns apply on re-upload. If
    that list and the code that reads them drift, the file lies to the person
    holding it -- which is worse than not documenting it at all.
    """
    assert set(EDITABLE_COLUMNS[COLUMN_SHEET]) == set(COLUMN_FIELDS)
    assert set(EDITABLE_COLUMNS[TABLE_SHEET]) == set(TABLE_FIELDS)
    assert EDITABLE_COLUMNS[RELATIONSHIP_SHEET] == ()


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


async def test_an_unedited_workbook_changes_nothing(session) -> None:
    datasource, _, _ = await _seed(session)
    batch = await _upload(session, datasource, await _export(session, datasource))
    assert batch.change_count == 0
    assert batch.rejected_row_count == 0
    assert batch.status == "DRAFT"


async def test_an_edited_column_description_becomes_one_pending_change(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "The customer's unique identifier."),
    )

    batch = await _upload(session, datasource, content)

    assert batch.change_count == 1
    change = (await _changes(session, batch.id))[0]
    assert change.subject_type == "COLUMN"
    assert change.subject_id == str(column.id)
    assert change.subject_label == "customers.customer_id"
    assert change.field == "business_description"
    assert change.old_value is None
    assert change.new_value == "The customer's unique identifier."
    assert change.status == "PENDING"


async def test_a_blank_cell_is_not_read_as_a_deletion(session) -> None:
    """Blanking a cell by accident is the easiest mistake in a spreadsheet, and
    an empty cell is indistinguishable from one nobody filled in.
    """
    datasource, table, column = await _seed(session)
    await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="An approved description.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", ""),
    )

    batch = await _upload(session, datasource, content)

    assert batch.change_count == 0


async def test_an_edited_id_is_reported_as_a_rejected_row_not_ignored(session) -> None:
    datasource, _, _ = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "column_id", str(uuid4())),
    )

    batch = await _upload(session, datasource, content)

    assert batch.change_count == 0
    assert batch.rejected_row_count == 1
    change = (await _changes(session, batch.id))[0]
    assert change.status == "REJECTED"
    assert "no active column with this id" in (change.skip_reason or "")


async def test_a_column_from_another_datasource_is_rejected(session) -> None:
    """Scoping is enforced against the datasource being imported into, not
    just against the organization -- otherwise a workbook could carry edits
    into a source the uploader never opened.
    """
    datasource, _, _ = await _seed(session)
    other_datasource, _, other_column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "column_id", str(other_column.id)),
    )

    batch = await _upload(session, datasource, content)

    assert batch.rejected_row_count == 1
    assert batch.change_count == 0


async def test_a_workbook_with_no_recognised_sheets_is_refused(session) -> None:
    datasource, _, _ = await _seed(session)
    content = write_workbook([Sheet(name="Notes", headers=["a"], rows=[["b"]])])
    with pytest.raises(HTTPException) as exc_info:
        await _upload(session, datasource, content)
    assert exc_info.value.status_code == 422


async def test_a_non_workbook_upload_is_refused(session) -> None:
    datasource, _, _ = await _seed(session)
    with pytest.raises(HTTPException) as exc_info:
        await _upload(session, datasource, b"schema,table,column,description\n")
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Review and apply
# ---------------------------------------------------------------------------


async def test_upload_does_not_submit_and_publishes_nothing(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "Proposed but not submitted."),
    )

    batch = await _upload(session, datasource, content)

    assert batch.status == "DRAFT"
    assert batch.governance_review_id is None
    assert await current_descriptions_by_column_id(session, [column.id]) == {}


async def test_approving_a_batch_publishes_the_column_description(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "The customer's unique identifier."),
    )
    batch = await _upload(session, datasource, content)

    await _approve(session, batch, table.organization_id)

    published = (await current_descriptions_by_column_id(session, [column.id]))[column.id]
    assert published.description == "The customer's unique identifier."
    # Maker and checker stay distinguishable through the bulk path too.
    assert published.created_by == _MAKER
    assert published.approved_by == _CHECKER
    await session.refresh(batch)
    assert (batch.status, batch.applied_count, batch.skipped_count) == ("APPLIED", 1, 0)


async def test_rejecting_a_batch_publishes_nothing_and_keeps_the_changes(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "Rejected wording."),
    )
    batch = await _upload(session, datasource, content)
    await submit_batch_for_review(session, batch, requested_by=_MAKER)
    await session.commit()

    await decide_governance_review(
        batch.governance_review_id,
        GovernanceDecisionRequest(decision="REJECT", reason="wording is not accurate"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    assert await current_descriptions_by_column_id(session, [column.id]) == {}
    await session.refresh(batch)
    assert batch.status == "REJECTED"
    # Retained, not deleted: the uploader needs to see what was turned down.
    changes = await _changes(session, batch.id)
    assert [change.status for change in changes] == ["REJECTED"]
    assert changes[0].new_value == "Rejected wording."


async def test_the_submitter_cannot_approve_their_own_batch(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "Self-approved?"),
    )
    batch = await _upload(session, datasource, content)
    await submit_batch_for_review(session, batch, requested_by=_MAKER)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await decide_governance_review(
            batch.governance_review_id,
            GovernanceDecisionRequest(decision="APPROVE"),
            _context(table.organization_id, _MAKER),
            session,
        )
    assert exc_info.value.status_code == 409
    assert await current_descriptions_by_column_id(session, [column.id]) == {}


async def test_a_batch_with_nothing_to_change_cannot_be_submitted(session) -> None:
    datasource, _, _ = await _seed(session)
    batch = await _upload(session, datasource, await _export(session, datasource))
    with pytest.raises(HTTPException) as exc_info:
        await submit_batch_for_review(session, batch, requested_by=_MAKER)
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# The lost-update guard
# ---------------------------------------------------------------------------


async def test_an_edit_superseded_since_export_is_skipped_not_applied(session) -> None:
    """The failure this whole `*_version` mechanism exists to prevent: a
    workbook exported before someone else published, uploaded after, silently
    discarding their work.
    """
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "From the stale workbook."),
    )
    batch = await _upload(session, datasource, content)

    # Someone else publishes in the window between export and approval.
    await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="Published by a colleague in the meantime.",
        created_by="colleague@example.com",
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )

    await _approve(session, batch, table.organization_id)

    current = (await current_descriptions_by_column_id(session, [column.id]))[column.id]
    assert current.description == "Published by a colleague in the meantime."
    change = (await _changes(session, batch.id))[0]
    assert change.status == "SKIPPED_STALE"
    assert "newer description" in (change.skip_reason or "")
    await session.refresh(batch)
    assert (batch.applied_count, batch.skipped_count) == (0, 1)


async def test_an_edit_on_top_of_the_version_it_was_exported_from_applies(session) -> None:
    """The other side of the same check: editing the version you were shown
    must still work, or the guard would block every real edit.
    """
    datasource, table, column = await _seed(session)
    await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description="Version one.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "Version two, edited in Excel."),
    )
    batch = await _upload(session, datasource, content)

    await _approve(session, batch, table.organization_id)

    current = (await current_descriptions_by_column_id(session, [column.id]))[column.id]
    assert current.description == "Version two, edited in Excel."
    assert current.version == 2


# ---------------------------------------------------------------------------
# Table-level fields
# ---------------------------------------------------------------------------


async def test_an_edited_readme_publishes_asset_documentation(session) -> None:
    datasource, table, _ = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(TABLE_SHEET, "readme", "Customer master, loaded nightly."),
    )
    batch = await _upload(session, datasource, content)

    await _approve(session, batch, table.organization_id)

    version = await session.scalar(
        select(AssetDocumentationVersion)
        .join(
            AssetDocumentation,
            AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
        )
        .where(AssetDocumentation.table_id == table.id)
    )
    assert version is not None
    assert version.readme == "Customer master, loaded nightly."
    assert version.created_by == _MAKER
    assert version.approved_by == _CHECKER


async def test_several_annotation_edits_publish_one_new_version(session) -> None:
    """Three edited cells on one table are one steward edit. Publishing three
    versions would make the version chain describe the spreadsheet's shape
    rather than what anyone did.
    """
    datasource, table, _ = await _seed(session)
    await _seed_annotation(session, datasource, table)

    def mutate(name: str, headers: list[str], rows: list[list[object]]) -> None:
        if name != TABLE_SHEET or not rows:
            return
        rows[0][headers.index("business_name")] = "Retail Customers"
        rows[0][headers.index("business_description")] = "Every retail banking customer."
        rows[0][headers.index("grain_statement")] = "One row per customer, per region."

    content = _rewrite(await _export(session, datasource), mutate)
    batch = await _upload(session, datasource, content)
    assert batch.change_count == 3

    await _approve(session, batch, table.organization_id)

    versions = list(
        (
            await session.execute(
                select(MetadataBusinessAnnotationVersion).order_by(
                    MetadataBusinessAnnotationVersion.version
                )
            )
        )
        .scalars()
        .all()
    )
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].status == "SUPERSEDED"
    current = versions[1]
    assert current.business_name == "Retail Customers"
    assert current.business_description == "Every retail banking customer."
    assert current.grain_statement == "One row per customer, per region."
    # Fields the workbook does not expose must survive a re-publish.
    assert current.table_role == "DIMENSION"
    assert current.synonyms == ["clients"]
    assert current.tags == ["retail"]
    assert current.confidence == 0.8


async def test_an_annotation_edit_on_a_table_with_no_annotation_is_rejected(session) -> None:
    """A spreadsheet cell cannot supply the domain and entity classification a
    new annotation needs, so this reports why rather than inventing one.
    """
    datasource, table, _ = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(TABLE_SHEET, "business_name", "Invented from a spreadsheet"),
    )

    batch = await _upload(session, datasource, content)

    assert batch.change_count == 0
    assert batch.rejected_row_count == 1
    change = (await _changes(session, batch.id))[0]
    assert change.status == "REJECTED"
    assert "no approved business annotation" in (change.skip_reason or "")


async def test_a_stale_annotation_edit_is_skipped(session) -> None:
    datasource, table, _ = await _seed(session)
    annotation = await _seed_annotation(session, datasource, table)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(TABLE_SHEET, "business_description", "From the stale workbook."),
    )
    batch = await _upload(session, datasource, content)

    from aida.business_annotation_versions import (
        AnnotationVersionContent,
        write_annotation_version,
    )

    await write_annotation_version(
        session,
        organization_id=table.organization_id,
        annotation_id=annotation.id,
        content=AnnotationVersionContent(
            business_name="Customers",
            business_description="Published by a colleague in the meantime.",
            table_role="DIMENSION",
            grain_statement="One row per customer.",
            synonyms=["clients"],
            suggested_questions=[],
            tags=["retail"],
            confidence=0.8,
        ),
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )

    await _approve(session, batch, table.organization_id)

    change = (await _changes(session, batch.id))[0]
    assert change.status == "SKIPPED_STALE"
    current = await session.scalar(
        select(MetadataBusinessAnnotationVersion).where(
            MetadataBusinessAnnotationVersion.status == "APPROVED"
        )
    )
    assert current is not None
    assert current.business_description == "Published by a colleague in the meantime."


async def test_a_column_dropped_between_upload_and_approval_is_skipped(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "For a column that will vanish."),
    )
    batch = await _upload(session, datasource, content)

    await session.delete(await session.get(MetadataColumn, column.id))
    await session.flush()

    await _approve(session, batch, table.organization_id)

    change = (await _changes(session, batch.id))[0]
    assert change.status == "SKIPPED_MISSING"
    await session.refresh(batch)
    assert (batch.applied_count, batch.skipped_count) == (0, 1)


async def test_applying_a_batch_twice_is_refused(session) -> None:
    datasource, table, column = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "business_description", "Applied once."),
    )
    batch = await _upload(session, datasource, content)
    await _approve(session, batch, table.organization_id)

    with pytest.raises(HTTPException) as exc_info:
        await apply_model_import_batch(session, batch, reviewer=_CHECKER, now=datetime.now(UTC))
    assert exc_info.value.status_code == 409
    # Still exactly one published version, not two.
    published = (await current_descriptions_by_column_id(session, [column.id]))[column.id]
    assert published.version == 1
    assert UUID(str(batch.id)) == batch.id


# ---------------------------------------------------------------------------
# Excluding rows before submitting
# ---------------------------------------------------------------------------


async def _two_column_workbook(session, datasource, table):
    """A workbook editing two columns, so one can be dropped and one kept."""
    second = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name="segment_code",
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(second)
    await session.flush()

    def mutate(name, headers, rows):
        if name != COLUMN_SHEET:
            return
        for row in rows:
            col_name = row[headers.index("column")]
            row[headers.index("business_description")] = f"Description for {col_name}."

    return _rewrite(await _export(session, datasource), mutate), second


async def test_excluding_a_row_drops_it_from_what_a_reviewer_is_asked_to_decide(session) -> None:
    """One wrong row used to force rejecting the whole file and re-uploading.
    Excluding is an uploader-side edit, not a partial approval.
    """
    datasource, table, column = await _seed(session)
    content, second = await _two_column_workbook(session, datasource, table)
    batch = await _upload(session, datasource, content)
    assert batch.change_count == 2

    changes = await _changes(session, batch.id)
    dropped = next(c for c in changes if c.subject_id == str(second.id))
    remaining = await set_change_exclusion(
        session, batch, change_ids=[dropped.id], excluded=True
    )

    assert remaining == 1
    assert batch.change_count == 1
    await session.refresh(dropped)
    assert dropped.status == "EXCLUDED"
    assert "excluded by the uploader" in (dropped.skip_reason or "")


async def test_an_excluded_row_is_never_applied_and_never_counted_as_skipped(session) -> None:
    """Excluded is not "decided and skipped": it is a row the uploader
    withdrew before anyone was asked to look at it.
    """
    datasource, table, column = await _seed(session)
    content, second = await _two_column_workbook(session, datasource, table)
    batch = await _upload(session, datasource, content)
    changes = await _changes(session, batch.id)
    dropped = next(c for c in changes if c.subject_id == str(second.id))
    await set_change_exclusion(session, batch, change_ids=[dropped.id], excluded=True)

    await _approve(session, batch, table.organization_id)

    await session.refresh(batch)
    assert (batch.applied_count, batch.skipped_count) == (1, 0)
    published = await current_descriptions_by_column_id(session, [column.id, second.id])
    assert column.id in published
    assert second.id not in published


async def test_an_excluded_row_can_be_put_back(session) -> None:
    datasource, table, column = await _seed(session)
    content, second = await _two_column_workbook(session, datasource, table)
    batch = await _upload(session, datasource, content)
    changes = await _changes(session, batch.id)
    dropped = next(c for c in changes if c.subject_id == str(second.id))

    await set_change_exclusion(session, batch, change_ids=[dropped.id], excluded=True)
    remaining = await set_change_exclusion(
        session, batch, change_ids=[dropped.id], excluded=False
    )

    assert remaining == 2
    await session.refresh(dropped)
    assert (dropped.status, dropped.skip_reason) == ("PENDING", None)


async def test_excluding_every_row_leaves_nothing_to_submit(session) -> None:
    datasource, table, column = await _seed(session)
    content, _ = await _two_column_workbook(session, datasource, table)
    batch = await _upload(session, datasource, content)
    await set_change_exclusion(
        session, batch, change_ids=[c.id for c in await _changes(session, batch.id)], excluded=True
    )

    with pytest.raises(HTTPException) as exc_info:
        await submit_batch_for_review(session, batch, requested_by=_MAKER)
    assert exc_info.value.status_code == 409


async def test_a_submitted_batch_can_no_longer_be_edited(session) -> None:
    """What a reviewer sees must be fixed the moment it is submitted --
    otherwise "approve this batch" would not name a stable thing.
    """
    datasource, table, column = await _seed(session)
    content, _ = await _two_column_workbook(session, datasource, table)
    batch = await _upload(session, datasource, content)
    changes = await _changes(session, batch.id)
    await submit_batch_for_review(session, batch, requested_by=_MAKER)

    with pytest.raises(HTTPException) as exc_info:
        await set_change_exclusion(session, batch, change_ids=[changes[0].id], excluded=True)
    assert exc_info.value.status_code == 409
    assert "decides it as one batch" in exc_info.value.detail


async def test_a_rejected_diff_row_cannot_be_toggled_into_a_change(session) -> None:
    """REJECTED rows are the diff's own findings, not proposals -- they were
    never going to apply, and un-excluding one must not smuggle it in.
    """
    datasource, table, _ = await _seed(session)
    content = _rewrite(
        await _export(session, datasource),
        _set_cell(COLUMN_SHEET, "column_id", str(uuid4())),
    )
    batch = await _upload(session, datasource, content)
    rejected = (await _changes(session, batch.id))[0]
    assert rejected.status == "REJECTED"

    await set_change_exclusion(session, batch, change_ids=[rejected.id], excluded=False)

    await session.refresh(rejected)
    assert rejected.status == "REJECTED"
    assert batch.change_count == 0
