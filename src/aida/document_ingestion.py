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
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    candidate_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(DataSource, DataSource.id == MetadataTable.datasource_id)
            .where(
                DataSource.project_id == document.project_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    tables_by_key: dict[tuple[str, str], list[MetadataTable]] = {}
    tables_by_name: dict[str, list[MetadataTable]] = {}
    for table, schema_name in candidate_rows:
        tables_by_key.setdefault((schema_name.casefold(), table.name.casefold()), []).append(
            table
        )
        tables_by_name.setdefault(table.name.casefold(), []).append(table)

    mappings: list[DocumentMapping] = []
    for section in sections:
        table_key = (
            (section.raw_schema_name.casefold(), section.raw_table_name.casefold())
            if section.raw_schema_name
            else None
        )
        matched_tables = (
            tables_by_key.get(table_key)
            if table_key is not None
            else tables_by_name.get(section.raw_table_name.casefold())
        ) or []
        if len(matched_tables) != 1:
            mappings.append(
                DocumentMapping(
                    organization_id=document.organization_id,
                    document_section_id=section.id,
                    subject_type="COLUMN" if section.raw_column_name else "TABLE",
                    subject_id=None,
                    mapping_kind="UNMATCHED",
                    confidence=0.0,
                )
            )
            continue
        table = matched_tables[0]
        if section.raw_column_name is None:
            mappings.append(
                DocumentMapping(
                    organization_id=document.organization_id,
                    document_section_id=section.id,
                    subject_type="TABLE",
                    subject_id=str(table.id),
                    mapping_kind="STRUCTURAL",
                    confidence=1.0,
                )
            )
            continue
        column = await session.scalar(
            select(MetadataColumn).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.status == "ACTIVE",
                MetadataColumn.name.ilike(section.raw_column_name),
            )
        )
        if column is None:
            mappings.append(
                DocumentMapping(
                    organization_id=document.organization_id,
                    document_section_id=section.id,
                    subject_type="COLUMN",
                    subject_id=None,
                    mapping_kind="UNMATCHED",
                    confidence=0.0,
                )
            )
            continue
        mappings.append(
            DocumentMapping(
                organization_id=document.organization_id,
                document_section_id=section.id,
                subject_type="COLUMN",
                subject_id=str(column.id),
                mapping_kind="STRUCTURAL",
                confidence=1.0,
            )
        )
    session.add_all(mappings)
    await session.flush()
    document.status = "MAPPED"
    return mappings


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
    claims: list[DocumentClaim] = []
    for mapping, section in rows:
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
            created_by=requested_by,
        )
        session.add(claim)
        await session.flush()
        review.object_id = str(claim.id)
        claims.append(claim)
    return claims


async def apply_document_claim(
    claim: DocumentClaim, *, reviewer: str, now: datetime
) -> str:
    """Publish an approved claim to its terminal state.

    No existing column-level description surface in this codebase consumes
    an approved claim yet (see `DocumentClaim`'s own docstring) -- this is
    deliberately the end of the line for this pass, not a stub awaiting a
    follow-up write that was simply forgotten.
    """
    claim.status = "APPROVED"
    claim.reviewed_by = reviewer
    claim.reviewed_at = now
    return "document.claim.approved.v1"


async def reject_document_claim(
    claim: DocumentClaim, *, reviewer: str, now: datetime
) -> str:
    claim.status = "REJECTED"
    claim.reviewed_by = reviewer
    claim.reviewed_at = now
    return "document.claim.rejected.v1"
