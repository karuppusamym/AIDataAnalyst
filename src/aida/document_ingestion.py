"""N8: document ingestion -- the data-dictionary-spreadsheet special case.

Per `Docs/review-2026-08/target/01-metadata-graph-wiki.md` §3, this module
builds the flow's first four steps (upload -> parse -> map -> extract claims)
for exactly the one shape the design brief itself calls the highest-value
special case: a CSV data dictionary of `schema | table | column |
description` rows, recognised and mapped directly rather than chunked as
prose. General document parsing (PDF/DOCX/XLSX structure-preserving
extraction) and semantic/embedding-similarity mapping are both real,
separate builds this pass does not attempt -- see `aida.models.Document`/
`DocumentMapping`'s own docstrings for why.

Claims are routed through the existing unified `GovernanceReview` queue
(`semantic_api.decide_governance_review`, `object_type="DOCUMENT_CLAIM"`) the
same way every other proposal on this platform is -- one review per claim,
since deciding a claim means reading its specific source section text.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import publish_asset_documentation_version
from aida.column_documentation import publish_column_description
from aida.models import (
    DataSource,
    Document,
    DocumentClaim,
    DocumentMapping,
    DocumentSection,
    GovernanceReview,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
)
from aida.refusal import RefusalDetail

#: A data dictionary is a small operational document, not a bulk data feed --
#: this cap keeps parsing and the structural-mapping pass that follows it
#: O(rows), bounded, and fast, matching CT-1's own
#: `CATALOG_BULK_FILTER_SCAN_CAP` philosophy for a different kind of bounded
#: scan.
DOCUMENT_MAX_CONTENT_BYTES = 1_000_000
DOCUMENT_MAX_SECTIONS = 5_000


@dataclass(frozen=True, slots=True)
class ParsedDictionaryRow:
    schema_name: str | None
    table_name: str
    column_name: str | None
    description: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    rows: list[ParsedDictionaryRow]
    error_count: int
    truncated: bool


def parse_csv_data_dictionary(content: str) -> ParseResult:
    """Parse a `schema | table | column | description` CSV into rows.

    Pure and DB-free, mirroring `catalog_bulk_actions.match_tables_by_filter`'s
    own separation of parsing/matching logic from persistence. Header
    matching is case-insensitive and order-independent; `schema`/`column`
    are optional (a column-less row is a table-level description, a
    schema-less row matches by table name alone). A row missing a non-empty
    `table` or `description` is dropped and counted as an error rather than
    raising -- one malformed row in a hand-edited spreadsheet should not
    fail the whole upload.
    """
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        return ParseResult(rows=[], error_count=0, truncated=False)
    header_map = {(name or "").strip().casefold(): name for name in reader.fieldnames}
    rows: list[ParsedDictionaryRow] = []
    error_count = 0
    truncated = False

    def _field(row: dict[str, str | None], key: str) -> str:
        source_key = header_map.get(key)
        if source_key is None:
            return ""
        return (row.get(source_key) or "").strip()

    for raw_row in reader:
        if len(rows) >= DOCUMENT_MAX_SECTIONS:
            truncated = True
            break

        table_name = _field(raw_row, "table")
        description = _field(raw_row, "description")
        if not table_name or not description:
            error_count += 1
            continue
        rows.append(
            ParsedDictionaryRow(
                schema_name=_field(raw_row, "schema") or None,
                table_name=table_name,
                column_name=_field(raw_row, "column") or None,
                description=description,
            )
        )
    return ParseResult(rows=rows, error_count=error_count, truncated=truncated)


async def create_document_from_csv(
    session: AsyncSession,
    *,
    organization_id: UUID,
    project_id: UUID,
    filename: str,
    content: str,
    uploaded_by: str,
) -> Document:
    """Upload + parse in one step -- a data dictionary is already tabular,
    so (unlike the general prose-parsing flow the design brief describes for
    other document types) there is no separate async parse stage to wait on.

    Raises `HTTPException` (413) over the size cap rather than truncating
    silently -- an oversized upload is very likely the wrong file, not a
    large-but-legitimate dictionary.
    """
    if len(content.encode("utf-8")) > DOCUMENT_MAX_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"document content exceeds {DOCUMENT_MAX_CONTENT_BYTES} bytes",
        )
    parsed = parse_csv_data_dictionary(content)
    document = Document(
        organization_id=organization_id,
        project_id=project_id,
        filename=filename,
        media_type="CSV",
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        status="PARSED",
        section_count=len(parsed.rows),
        parse_error_count=parsed.error_count,
        uploaded_by=uploaded_by,
    )
    session.add(document)
    await session.flush()
    session.add_all(
        DocumentSection(
            organization_id=organization_id,
            document_id=document.id,
            ordinal=ordinal,
            raw_schema_name=row.schema_name,
            raw_table_name=row.table_name,
            raw_column_name=row.column_name,
            raw_description=row.description,
        )
        for ordinal, row in enumerate(parsed.rows)
    )
    await session.flush()
    return document


@dataclass(slots=True)
class _CatalogIndex:
    """One project's live catalog, indexed the way structural mapping reads it."""

    tables_by_key: dict[tuple[str, str], list[MetadataTable]] = field(default_factory=dict)
    tables_by_name: dict[str, list[MetadataTable]] = field(default_factory=dict)
    columns_by_table: dict[UUID, dict[str, list[MetadataColumn]]] = field(default_factory=dict)


async def _catalog_index(session: AsyncSession, project_id: UUID) -> _CatalogIndex:
    candidate_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(DataSource, DataSource.id == MetadataTable.datasource_id)
            .where(
                DataSource.project_id == project_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    index = _CatalogIndex()
    for table, schema_name in candidate_rows:
        key = (schema_name.casefold(), table.name.casefold())
        index.tables_by_key.setdefault(key, []).append(table)
        index.tables_by_name.setdefault(table.name.casefold(), []).append(table)
    return index


async def _resolve_section(
    session: AsyncSession, index: _CatalogIndex, section: DocumentSection
) -> tuple[str, str | None]:
    """The subject a section names, as `(subject_type, subject_id)`; no id unless exactly one."""
    subject_type = "COLUMN" if section.raw_column_name else "TABLE"
    table_key = (
        (section.raw_schema_name.casefold(), section.raw_table_name.casefold())
        if section.raw_schema_name
        else None
    )
    matched_tables = (
        index.tables_by_key.get(table_key)
        if table_key is not None
        else index.tables_by_name.get(section.raw_table_name.casefold())
    ) or []
    if len(matched_tables) != 1:
        return subject_type, None
    table = matched_tables[0]
    if section.raw_column_name is None:
        return "TABLE", str(table.id)
    # Column names are matched the way table names are -- casefolded equality,
    # in Python -- and never with `ILIKE`, which reads `%` and `_` in a
    # spreadsheet cell as wildcards: a column cell of `%` matched whichever
    # column the database returned first, at confidence 1.0 (AR-03).
    if table.id not in index.columns_by_table:
        by_name: dict[str, list[MetadataColumn]] = {}
        for candidate in await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.status == "ACTIVE",
            )
        ):
            by_name.setdefault(candidate.name.casefold(), []).append(candidate)
        index.columns_by_table[table.id] = by_name
    matched_columns = index.columns_by_table[table.id].get(section.raw_column_name.casefold(), [])
    # Two columns differing only by case are as ambiguous as two tables.
    if len(matched_columns) != 1:
        return "COLUMN", None
    return "COLUMN", str(matched_columns[0].id)


def _set_subject(mapping: DocumentMapping, subject_type: str, subject_id: str | None) -> None:
    mapping.subject_type = subject_type
    mapping.subject_id = subject_id
    mapping.mapping_kind = "STRUCTURAL" if subject_id is not None else "UNMATCHED"
    mapping.confidence = 1.0 if subject_id is not None else 0.0


async def resolve_structural_mappings(
    session: AsyncSession, document: Document
) -> list[DocumentMapping]:
    """Deterministic exact-name matching against the live catalog, scoped to
    the document's own project via `DataSource.project_id` -- the design
    brief's "structural" route (§3.3), the only one this pass implements.

    A section resolves to `UNMATCHED` (never a guess) whenever its names
    match more than one live candidate -- e.g. two datasources in the same
    project both have a `public.customers` table -- since a data dictionary
    row alone carries no datasource to disambiguate with.
    """
    sections = (
        await session.scalars(
            select(DocumentSection)
            .where(DocumentSection.document_id == document.id)
            .order_by(DocumentSection.ordinal)
        )
    ).all()
    index = await _catalog_index(session, document.project_id)
    mappings: list[DocumentMapping] = []
    for section in sections:
        subject_type, subject_id = await _resolve_section(session, index, section)
        mapping = DocumentMapping(
            organization_id=document.organization_id, document_section_id=section.id
        )
        _set_subject(mapping, subject_type, subject_id)
        mappings.append(mapping)
    session.add_all(mappings)
    await session.flush()
    document.status = "MAPPED"
    return mappings


async def _propose_claim(
    session: AsyncSession,
    document: Document,
    section: DocumentSection,
    mapping: DocumentMapping,
    *,
    requested_by: str,
    created_by: str,
) -> DocumentClaim:
    assert mapping.subject_id is not None
    review = GovernanceReview(
        organization_id=document.organization_id,
        object_type="DOCUMENT_CLAIM",
        object_id="pending",
        requested_action="DESCRIBES",
        requested_by=requested_by,
    )
    session.add(review)
    await session.flush()
    claim = DocumentClaim(
        organization_id=document.organization_id,
        document_section_id=section.id,
        subject_type=mapping.subject_type,
        subject_id=mapping.subject_id,
        predicate="DESCRIBES",
        object_value=section.raw_description,
        confidence=mapping.confidence,
        status="PENDING",
        governance_review_id=review.id,
        created_by=created_by,
    )
    session.add(claim)
    await session.flush()
    review.object_id = str(claim.id)
    return claim


async def extract_description_claims(
    session: AsyncSession, document: Document, *, requested_by: str
) -> list[DocumentClaim]:
    """One `DocumentClaim` + one `GovernanceReview` per structurally-mapped
    section -- mirrors `aida.playbooks._queue_for_review`'s pairing, except
    every claim is reviewed individually (never bulk), since a steward
    deciding a claim needs to read its own source text, not a batch of
    unrelated sections' worth.
    """
    rows = (
        await session.execute(
            select(DocumentMapping, DocumentSection)
            .join(DocumentSection, DocumentSection.id == DocumentMapping.document_section_id)
            .where(
                DocumentMapping.document_section_id.in_(
                    select(DocumentSection.id).where(DocumentSection.document_id == document.id)
                ),
                DocumentMapping.mapping_kind == "STRUCTURAL",
            )
        )
    ).all()
    return [
        await _propose_claim(
            session, document, section, mapping, requested_by=requested_by, created_by=requested_by
        )
        for mapping, section in rows
    ]


@dataclass(slots=True)
class RemapOutcome:
    """What mapping a document again changed: counts, and the claims it proposed."""

    remapped: int = 0
    unmatched: int = 0
    claims: list[DocumentClaim] = field(default_factory=list)


async def remap_document(
    session: AsyncSession, document: Document, *, requested_by: str
) -> RemapOutcome:
    """R11-FP16: map a document's sections again, against the catalog as it is now.

    The rules are `resolve_structural_mappings`'s own. A section whose table or column left the
    source, or became ambiguous, is UNMATCHED again, and a pending claim on it cannot be approved
    (`claim_subject_retired`). A section that now names exactly one live table or column, other
    than the one it named, maps to it and gets a claim proposed for review, attributed to the
    document's uploader -- unless its text was already proposed for that subject, pending or
    decided, since a person's decision on it stands.
    """
    index = await _catalog_index(session, document.project_id)
    rows = (
        await session.execute(
            select(DocumentSection, DocumentMapping)
            .join(DocumentMapping, DocumentMapping.document_section_id == DocumentSection.id)
            .where(DocumentSection.document_id == document.id)
            .order_by(DocumentSection.ordinal)
        )
    ).all()
    outcome = RemapOutcome()
    for section, mapping in rows:
        subject_type, subject_id = await _resolve_section(session, index, section)
        if (subject_type, subject_id) == (mapping.subject_type, mapping.subject_id):
            continue
        _set_subject(mapping, subject_type, subject_id)
        if subject_id is None:
            outcome.unmatched += 1
            continue
        outcome.remapped += 1
        proposed = await session.scalar(
            select(DocumentClaim.id)
            .where(
                DocumentClaim.document_section_id == section.id,
                DocumentClaim.subject_id == subject_id,
            )
            .limit(1)
        )
        if proposed is None:
            outcome.claims.append(
                await _propose_claim(
                    session,
                    document,
                    section,
                    mapping,
                    requested_by=requested_by,
                    created_by=document.uploaded_by,
                )
            )
    await session.flush()
    return outcome


#: R11-FP16: the table or column a claim describes is no longer in the source.
CLAIM_SUBJECT_RETIRED = "CLAIM_SUBJECT_RETIRED"


async def claim_subject_retired(
    session: AsyncSession, claim: DocumentClaim
) -> RefusalDetail | None:
    """Why a claim cannot be published onto its subject, or `None` while its subject stands.

    Catalog objects are retired, not deleted, so a claim mapped before its table or column left
    the source would otherwise publish a description onto an object the source no longer has.
    Refusing keeps the claim pending, so the same claim can be approved if the object returns. A
    subject whose row is gone altogether cannot return; `apply_document_claim` approves its text
    and publishes nothing.
    """
    try:
        subject_id = UUID(claim.subject_id)
    except ValueError:
        return None
    if claim.subject_type == "COLUMN":
        column = await session.get(MetadataColumn, subject_id)
        if column is None:
            return None
        table = await session.get(MetadataTable, column.table_id)
        standing = column.status == "ACTIVE" and (table is None or table.status == "ACTIVE")
    else:
        table = await session.get(MetadataTable, subject_id)
        if table is None:
            return None
        standing = table.status == "ACTIVE"
    if standing:
        return None
    return RefusalDetail(
        code=CLAIM_SUBJECT_RETIRED,
        message=(
            "The table or column this claim describes has left the source since the document "
            "was mapped, so approving it would describe an object that no longer exists. Reject "
            "it; the document is mapped again after the next scan."
        ),
    )


async def apply_document_claim(
    session: AsyncSession, claim: DocumentClaim, *, reviewer: str, now: datetime
) -> tuple[str, UUID | None]:
    """Publish an approved claim into the description store for its subject.

    When this pipeline was first built there was nowhere to publish to: a
    column-level description store did not exist, so an approved claim's
    terminal state was this row (see `DocumentClaim`'s own docstring, written
    at that time). `aida.column_documentation` is now that store, so a claim
    approval publishes for real:

    * `subject_type="COLUMN"` -> a new `ColumnDocumentationVersion`.
    * `subject_type="TABLE"`  -> a new `AssetDocumentationVersion`, the same
      store a GL-9 `AssetDescriptionDraft` approval publishes to, through the
      same shared `publish_asset_documentation_version` helper. A data
      dictionary carries both table and column rows, so approving its
      table rows had to land somewhere too rather than silently do nothing.

    `created_by` carries the claim's uploader, not the reviewer -- maker and
    checker stay distinguishable on the published version exactly as they do
    for a GL-9 draft.

    Returns the event type and the published version's id. A claim whose
    table or column has left the source since mapping is refused with
    `CLAIM_SUBJECT_RETIRED` before anything changes (R11-FP16): catalog
    objects are retired rather than deleted, so the retired object would
    otherwise still receive the description. The id is `None` when the
    subject's row is gone altogether, or the id is not an id: the steward's
    decision on the text stands, and nothing is published against it.
    """
    refusal = await claim_subject_retired(session, claim)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    claim.status = "APPROVED"
    claim.reviewed_by = reviewer
    claim.reviewed_at = now
    try:
        subject_id = UUID(claim.subject_id)
    except ValueError:
        return "document.claim.approved.v1", None

    if claim.subject_type == "COLUMN":
        column = await session.get(MetadataColumn, subject_id)
        if column is None:
            return "document.claim.approved.v1", None
        version = await publish_column_description(
            session,
            organization_id=claim.organization_id,
            table_id=column.table_id,
            column_id=column.id,
            description=claim.object_value,
            created_by=claim.created_by,
            approved_by=reviewer,
            approved_at=now,
            source_claim_id=claim.id,
        )
        return "document.claim.approved.v1", version.id

    table = await session.get(MetadataTable, subject_id)
    if table is None:
        return "document.claim.approved.v1", None
    asset_version = await publish_asset_documentation_version(
        session,
        organization_id=claim.organization_id,
        table_id=table.id,
        readme=claim.object_value,
        created_by=claim.created_by,
        approved_by=reviewer,
        approved_at=now,
    )
    return "document.claim.approved.v1", asset_version.id


async def reject_document_claim(claim: DocumentClaim, *, reviewer: str, now: datetime) -> str:
    claim.status = "REJECTED"
    claim.reviewed_by = reviewer
    claim.reviewed_at = now
    return "document.claim.rejected.v1"
