"""Compose one datasource's model -- tables, columns, relationships -- as a
workbook, for offline bulk review and editing.

The read half of the download/edit/re-upload round trip. `document_ingestion`
already owns the upload half for the `schema | table | column | description`
CSV shape; this module produces the richer workbook that shape grew out of, so
a steward can pull the whole model down, work through it in Excel, and (once
the re-import lands) push the edits back through the same
`GovernanceReview` queue every other change on this platform goes through.

Two properties matter more than the formatting:

* **Stable identifiers travel with every row.** `table_id` / `column_id` /
  `relationship_id` are what a re-import matches on, so an edited workbook
  still applies correctly after an object has been renamed in the source
  system -- name-matching alone (which is all the CSV path can do today)
  silently drops those rows.
* **Authored and source-derived descriptions stay in separate columns.**
  `source_description` is the source system's own comment, overwritten by the
  next rediscovery; `business_description` is reviewed, authored content from
  `ColumnDocumentationVersion` / `AssetDocumentationVersion`. Collapsing them
  into one "description" column -- the CSV shape's compromise -- makes it
  impossible for a reader to tell which of the two they are editing.

Composition is bounded, not streamed: see `EXPORT_MAX_ROWS_PER_SHEET`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.catalog_read_model import (
    # Imported through this shim, not from `atlas.modules.catalog.repository`
    # directly: the "catalog module privacy" import-linter contract in
    # `pyproject.toml` names the shim as an allowed importer and this module
    # is not, which is the same route `asset_evidence`, `asset_context` and
    # `stewardship_api` already take to these helpers.
    _business_annotations,
    _latest_approved_documentation,
)
from aida.column_documentation import current_descriptions_by_column_id
from aida.models import (
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    RelationshipCandidate,
)
from aida.xlsx import CellValue, Sheet

#: Per-sheet row cap. Well under Excel's own million-row limit, because the
#: binding constraint is not the format: the whole workbook is composed in
#: memory (`aida.xlsx` does not stream), and a workbook large enough to matter
#: for that is already far past the size anyone can review by hand. A truncated
#: sheet is reported in the README sheet and in the endpoint's response
#: headers -- never silently.
EXPORT_MAX_ROWS_PER_SHEET = 50_000

TABLE_SHEET = "Tables"
COLUMN_SHEET = "Columns"
RELATIONSHIP_SHEET = "Relationships"
README_SHEET = "README"

_TABLE_HEADERS = [
    "table_id",
    "schema",
    "table",
    "object_type",
    "status",
    "source_description",
    "business_name",
    "business_description",
    "table_role",
    "grain_statement",
    "synonyms",
    "tags",
    "readme",
    "readme_version",
]

_COLUMN_HEADERS = [
    "column_id",
    "table_id",
    "schema",
    "table",
    "column",
    "ordinal_position",
    "physical_type",
    "nullable",
    "classification",
    "classification_source",
    "source_description",
    "business_description",
    "description_version",
    "approved_by",
    "approved_at",
]

_RELATIONSHIP_HEADERS = [
    "relationship_id",
    "status",
    "detection_rule",
    "confidence",
    "source_schema",
    "source_table",
    "source_column",
    "target_schema",
    "target_table",
    "target_column",
    "reviewed_by",
    "review_reason",
]

#: Columns a future re-import will read back. Everything else in the workbook
#: is read-only context -- catalog facts owned by ingestion, which an edit here
#: could not change even in principle. Stated in the README sheet so a steward
#: does not spend an afternoon editing a column that will be ignored.
EDITABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE_SHEET: ("business_name", "business_description", "grain_statement", "readme"),
    COLUMN_SHEET: ("business_description",),
    RELATIONSHIP_SHEET: (),
}


@dataclass(frozen=True, slots=True)
class WorkbookComposition:
    """The composed sheets plus what had to be left out of them."""

    sheets: list[Sheet]
    #: Sheet name -> whether it hit `EXPORT_MAX_ROWS_PER_SHEET`.
    truncated: dict[str, bool]
    row_counts: dict[str, int]

    @property
    def any_truncated(self) -> bool:
        return any(self.truncated.values())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _joined(values: list[str] | None) -> str | None:
    """JSON list columns (`synonyms`, `tags`) as a "; "-separated cell.

    A separator a steward can retype, rather than JSON they would have to keep
    syntactically valid by hand in a spreadsheet cell.
    """
    return "; ".join(values) if values else None


async def _table_rows(
    session: AsyncSession, datasource_id: UUID
) -> tuple[list[list[CellValue]], bool]:
    records = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataSchema.name, MetadataTable.name)
            .limit(EXPORT_MAX_ROWS_PER_SHEET + 1)
        )
    ).all()
    truncated = len(records) > EXPORT_MAX_ROWS_PER_SHEET
    records = records[:EXPORT_MAX_ROWS_PER_SHEET]

    table_ids = [table.id for table, _ in records]
    annotations = await _business_annotations(session, table_ids)
    documentation = await _latest_approved_documentation(session, table_ids)

    rows: list[list[CellValue]] = []
    for table, schema_name in records:
        annotation = annotations.get(table.id)
        document = documentation.get(table.id)
        rows.append(
            [
                str(table.id),
                schema_name,
                table.name,
                table.object_type,
                table.status,
                table.source_description,
                annotation.business_name if annotation else None,
                annotation.business_description if annotation else None,
                annotation.table_role if annotation else None,
                annotation.grain_statement if annotation else None,
                _joined(annotation.synonyms) if annotation else None,
                _joined(annotation.tags) if annotation else None,
                document.readme if document else None,
                document.version if document else None,
            ]
        )
    return rows, truncated


async def _column_rows(
    session: AsyncSession, datasource_id: UUID
) -> tuple[list[list[CellValue]], bool]:
    records = (
        await session.execute(
            select(MetadataColumn, MetadataTable.name, MetadataSchema.name)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
                MetadataColumn.status == "ACTIVE",
            )
            .order_by(
                MetadataSchema.name,
                MetadataTable.name,
                MetadataColumn.ordinal_position,
            )
            .limit(EXPORT_MAX_ROWS_PER_SHEET + 1)
        )
    ).all()
    truncated = len(records) > EXPORT_MAX_ROWS_PER_SHEET
    records = records[:EXPORT_MAX_ROWS_PER_SHEET]

    descriptions = await current_descriptions_by_column_id(
        session, [column.id for column, _, _ in records]
    )

    rows: list[list[CellValue]] = []
    for column, table_name, schema_name in records:
        documented = descriptions.get(column.id)
        rows.append(
            [
                str(column.id),
                str(column.table_id),
                schema_name,
                table_name,
                column.name,
                column.ordinal_position,
                column.physical_type,
                column.nullable,
                column.classification,
                column.classification_source,
                column.source_description,
                documented.description if documented else None,
                documented.version if documented else None,
                documented.approved_by if documented else None,
                _iso(documented.approved_at) if documented else None,
            ]
        )
    return rows, truncated


async def _relationship_rows(
    session: AsyncSession, datasource_id: UUID
) -> tuple[list[list[CellValue]], bool]:
    """Same-datasource relationship candidates only.

    Cross-source candidates (`target_datasource_id != datasource_id`) are
    deliberately excluded: per `RelationshipCandidate`'s own docstring, a
    cross-domain edge may only be rendered when
    `domain_service.check_cross_boundary_grant` confirms an ACTIVE grant, and
    that is a per-read check this export does not perform. Excluding them is
    the safe side of that line; the README sheet says so, so a reader does not
    mistake the omission for "there are none".
    """
    source_table = aliased(MetadataTable)
    source_schema = aliased(MetadataSchema)
    source_column = aliased(MetadataColumn)
    target_table = aliased(MetadataTable)
    target_schema = aliased(MetadataSchema)
    target_column = aliased(MetadataColumn)

    records = (
        await session.execute(
            select(
                RelationshipCandidate,
                source_schema.name,
                source_table.name,
                source_column.name,
                target_schema.name,
                target_table.name,
                target_column.name,
            )
            .join(source_table, source_table.id == RelationshipCandidate.source_table_id)
            .join(source_schema, source_schema.id == source_table.schema_id)
            .join(source_column, source_column.id == RelationshipCandidate.source_column_id)
            .join(target_table, target_table.id == RelationshipCandidate.target_table_id)
            .join(target_schema, target_schema.id == target_table.schema_id)
            .join(target_column, target_column.id == RelationshipCandidate.target_column_id)
            .where(
                RelationshipCandidate.datasource_id == datasource_id,
                RelationshipCandidate.target_datasource_id == datasource_id,
            )
            .order_by(
                source_schema.name,
                source_table.name,
                source_column.name,
                RelationshipCandidate.id,
            )
            .limit(EXPORT_MAX_ROWS_PER_SHEET + 1)
        )
    ).all()
    truncated = len(records) > EXPORT_MAX_ROWS_PER_SHEET
    records = records[:EXPORT_MAX_ROWS_PER_SHEET]

    return [
        [
            str(candidate.id),
            candidate.status,
            candidate.detection_rule,
            candidate.confidence,
            src_schema,
            src_table,
            src_column,
            tgt_schema,
            tgt_table,
            tgt_column,
            candidate.reviewed_by,
            candidate.review_reason,
        ]
        for (
            candidate,
            src_schema,
            src_table,
            src_column,
            tgt_schema,
            tgt_table,
            tgt_column,
        ) in records
    ], truncated


def _readme_sheet(
    *,
    datasource: DataSource,
    generated_at: datetime,
    generated_by: str,
    row_counts: dict[str, int],
    truncated: dict[str, bool],
) -> Sheet:
    """A first sheet that says what the workbook is and which cells matter.

    Not decoration: the round trip depends on a steward not editing the id
    columns and not expecting edits to read-only context to apply. Putting
    that in the file means it travels with the file, which a note in the UI
    does not.
    """
    rows: list[list[CellValue]] = [
        ["Datasource", datasource.name],
        ["Datasource id", str(datasource.id)],
        ["Generated at", generated_at.isoformat()],
        ["Generated by", generated_by],
        [None, None],
        ["Sheet", "Rows"],
    ]
    for sheet_name in (TABLE_SHEET, COLUMN_SHEET, RELATIONSHIP_SHEET):
        note = " (TRUNCATED)" if truncated.get(sheet_name) else ""
        rows.append([sheet_name, f"{row_counts.get(sheet_name, 0)}{note}"])
    rows.extend(
        [
            [None, None],
            [
                "Identifier columns",
                "Columns ending in _id are stable identifiers used to match "
                "rows on re-import. Do not edit, reorder, or delete them; a "
                "row whose id is changed cannot be matched and will be "
                "rejected rather than applied to the wrong object.",
            ],
            [
                "source_description",
                "The source system's own comment. Read-only here: it is "
                "re-derived by every rediscovery pass, so an edit would be "
                "overwritten. Edit business_description instead.",
            ],
            [
                "business_description",
                "Reviewed, authored content. This is the editable column.",
            ],
            [
                "Editable columns",
                "; ".join(
                    f"{sheet}: {', '.join(columns) or 'none (read-only sheet)'}"
                    for sheet, columns in EDITABLE_COLUMNS.items()
                ),
            ],
            [
                "Review",
                "Edits are not applied on upload. Each change becomes a "
                "governance review, decided by someone other than the person "
                "who made it, exactly like every other change on this "
                "platform.",
            ],
            [
                "Relationships",
                "Same-datasource candidates only. Cross-source relationships "
                "need a per-read cross-domain grant check that this export "
                "does not perform, so they are omitted here -- their absence "
                "does not mean none exist.",
            ],
            [
                "Scope",
                "ACTIVE tables and columns only. Deprecated objects are "
                "excluded so the workbook reflects what the catalog currently "
                "considers live.",
            ],
        ]
    )
    return Sheet(name=README_SHEET, headers=["Field", "Value"], rows=rows)


async def compose_model_workbook(
    session: AsyncSession,
    *,
    datasource: DataSource,
    generated_at: datetime,
    generated_by: str,
) -> WorkbookComposition:
    """Compose every sheet for `datasource`.

    Pure read: nothing here writes, so an export can be run by any caller the
    endpoint's read gate admits without a side effect to reason about.
    """
    table_rows, tables_truncated = await _table_rows(session, datasource.id)
    column_rows, columns_truncated = await _column_rows(session, datasource.id)
    relationship_rows, relationships_truncated = await _relationship_rows(session, datasource.id)

    truncated = {
        TABLE_SHEET: tables_truncated,
        COLUMN_SHEET: columns_truncated,
        RELATIONSHIP_SHEET: relationships_truncated,
    }
    row_counts = {
        TABLE_SHEET: len(table_rows),
        COLUMN_SHEET: len(column_rows),
        RELATIONSHIP_SHEET: len(relationship_rows),
    }
    sheets = [
        _readme_sheet(
            datasource=datasource,
            generated_at=generated_at,
            generated_by=generated_by,
            row_counts=row_counts,
            truncated=truncated,
        ),
        Sheet(name=TABLE_SHEET, headers=_TABLE_HEADERS, rows=table_rows),
        Sheet(name=COLUMN_SHEET, headers=_COLUMN_HEADERS, rows=column_rows),
        Sheet(
            name=RELATIONSHIP_SHEET,
            headers=_RELATIONSHIP_HEADERS,
            rows=relationship_rows,
        ),
    ]
    return WorkbookComposition(sheets=sheets, truncated=truncated, row_counts=row_counts)
