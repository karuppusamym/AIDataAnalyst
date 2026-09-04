"""P1-05: read-model helpers and shared resolution logic for the parsed
lineage-edge review queue.

The five non-governed parser-produced lineage edge tables --
`ViewLineageEdge`, `ProcedureLineageEdge`, `DbtLineageEdge`,
`OpenLineageTableEdge`, `OpenLineageColumnEdge` -- share a review-state
column set added by the 2026-09-04 P1-05 migration
(`0026a6f31c05_p1_05_parsed_lineage_review_state.py`). They deliberately
do NOT share a supertype (see ADR-0026 for the rationale); this module
composes across them at read time instead, and shares the "should this
new row land ACTIVE or PROPOSED" decision so every parser applies the
same rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    DbtLineageEdge,
    OpenLineageColumnEdge,
    OpenLineageTableEdge,
    ProcedureLineageEdge,
    ViewLineageEdge,
)

# Confidence values on ViewLineageEdge / ProcedureLineageEdge / DbtLineageEdge
# are string enums (FULL/PARTIAL/LOW), not floats -- see the parser's own
# `Confidence` type and the mapping used by `unified_lineage_api`. Keep the
# mapping in one place so a change here changes the review decision and the
# unified-lineage projection together.
_STRING_CONFIDENCE_TO_FLOAT = {"FULL": 1.0, "PARTIAL": 0.6, "LOW": 0.3}

# The five edge tables under review. Kept as a stable list so the queue
# service, the review endpoint dispatch, and the auditing details all
# agree on the identifier vocabulary. VIEW / PROCEDURE / DBT /
# OPENLINEAGE_TABLE / OPENLINEAGE_COLUMN is what the client-facing
# `edge_type` field on the decision endpoint accepts.
EDGE_TYPE_TO_MODEL: dict[str, Any] = {
    "VIEW": ViewLineageEdge,
    "PROCEDURE": ProcedureLineageEdge,
    "DBT": DbtLineageEdge,
    "OPENLINEAGE_TABLE": OpenLineageTableEdge,
    "OPENLINEAGE_COLUMN": OpenLineageColumnEdge,
}

EDGE_TYPES = tuple(EDGE_TYPE_TO_MODEL.keys())

REVIEW_STATUSES = ("PROPOSED", "ACTIVE", "REJECTED", "SUPERSEDED")


def _coerce_confidence_to_float(confidence: str | float | int | None) -> float:
    """Return a 0..1 float confidence from an edge's raw confidence value.

    Every string enum the parsers emit (FULL/PARTIAL/LOW) maps to a
    canonical float; a numeric confidence passes through, clamped; an
    unknown value degrades to 0.0 rather than raising -- P1-05 must not
    make a re-parse fail because an unrecognised confidence string
    slipped past the parser."""
    if confidence is None:
        return 0.0
    if isinstance(confidence, (int, float)):
        try:
            return max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            return 0.0
    return _STRING_CONFIDENCE_TO_FLOAT.get(str(confidence).upper(), 0.0)


def resolve_review_status_for_new_edge(
    *,
    review_mode: str,
    confidence: str | float | int | None,
    threshold: float,
    source_trusted: bool | None,
) -> str:
    """Decide the `review_status` a freshly-parsed edge should land at.

    * `auto_active` (the default) always returns ACTIVE, preserving the
      exact pre-P1-05 write behavior. Nothing in an existing deployment
      changes on this call until the operator flips the setting.
    * `require_review` returns PROPOSED, EXCEPT:
        - if the edge's confidence is at or above `threshold`, ACTIVE
          (matches ADR-0025 -- high-confidence output that a reviewer
          would rubber-stamp anyway lands ACTIVE straight away);
        - if `source_trusted is True`, ACTIVE (connector-pushed lineage
          from a `Datasource.trusted_for_lineage=True` source, per the
          P1-05 spec).
    """
    if review_mode == "auto_active":
        return "ACTIVE"
    if source_trusted is True:
        return "ACTIVE"
    if _coerce_confidence_to_float(confidence) >= threshold:
        return "ACTIVE"
    return "PROPOSED"


@dataclass(frozen=True)
class ParsedLineageReviewItem:
    """One row for the parsed-lineage review queue -- the shape the
    reviewer UI/API renders. Deliberately narrow: enough to judge the
    edge (source, target, transformation, confidence, source-SQL back-
    reference) without pulling the raw SQL into the queue itself."""

    edge_id: UUID
    edge_type: str
    organization_id: UUID
    created_at: Any
    created_by: str | None
    confidence: str | float | int | None
    source_label: str
    target_label: str
    transformation_type: str | None
    # Back-reference the UI can dereference to the source SQL. For view/
    # procedure edges it's the datasource + sql_hash; for dbt, the dbt
    # artifact_import_id + resource pair; for OpenLineage, the run_event
    # id + dataset pair. `source_sql_reference["kind"]` names which.
    source_sql_reference: dict[str, str]


async def list_parsed_lineage_review_queue(
    session: AsyncSession,
    organization_id: UUID,
    *,
    edge_type: str | None = None,
    min_confidence: float | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ParsedLineageReviewItem], int]:
    """Composite PROPOSED-edge review queue across the five parser-
    produced edge tables. Returns `(items, total)`, ordered newest first.

    `edge_type` narrows to one table; `min_confidence` filters on the
    coerced 0..1 confidence (string enums included). Pagination is
    (limit, offset) over the composed list; the total counts across all
    five tables to give the caller an honest "how much is waiting"
    number rather than one table's slice."""
    edge_types = (edge_type,) if edge_type else EDGE_TYPES
    items: list[ParsedLineageReviewItem] = []
    total = 0
    for etype in edge_types:
        model = EDGE_TYPE_TO_MODEL.get(etype)
        if model is None:
            continue
        # Total count first -- cheaper than materialising the rows only
        # to discard them beyond `limit`. `min_confidence` is applied
        # in Python for the string-enum tables so we don't have to
        # bake the enum->float mapping into SQL for one query.
        base_stmt = select(model).where(
            model.organization_id == organization_id,
            model.review_status == "PROPOSED",
        )
        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        this_total = int(await session.scalar(count_stmt) or 0)
        total += this_total
        if this_total == 0:
            continue
        rows = (
            await session.scalars(base_stmt.order_by(model.created_at.desc()))
        ).all()
        for row in rows:
            item = _row_to_item(etype, row)
            if item is None:
                continue
            if min_confidence is not None and _coerce_confidence_to_float(
                item.confidence
            ) < min_confidence:
                continue
            items.append(item)
    # Compose newest-first across every source table.
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items[offset : offset + limit], total


def _row_to_item(edge_type: str, row: Any) -> ParsedLineageReviewItem | None:
    """Project one edge row onto the queue item shape. Table-specific
    because each of the five tables carries a different natural key
    back to its source SQL -- see the `ParsedLineageReviewItem` doc."""
    if edge_type in ("VIEW", "PROCEDURE"):
        return ParsedLineageReviewItem(
            edge_id=row.id,
            edge_type=edge_type,
            organization_id=row.organization_id,
            created_at=row.created_at,
            created_by=row.created_by,
            confidence=row.confidence,
            source_label=f"{row.source_table}.{row.source_column}",
            target_label=f"{row.target_table}.{row.target_column}",
            transformation_type=row.transformation_type,
            source_sql_reference={
                "kind": (
                    "VIEW_DEFINITION"
                    if edge_type == "VIEW"
                    else "PROCEDURE_DEFINITION"
                ),
                "datasource_id": str(row.datasource_id),
                "sql_hash": row.sql_hash,
                "dialect": row.dialect,
            },
        )
    if edge_type == "DBT":
        return ParsedLineageReviewItem(
            edge_id=row.id,
            edge_type=edge_type,
            organization_id=row.organization_id,
            created_at=row.created_at,
            created_by=row.created_by,
            confidence=row.confidence,
            source_label=f"resource:{row.source_resource_id}"
            + (f".{row.source_column}" if row.source_column else ""),
            target_label=f"resource:{row.target_resource_id}"
            + (f".{row.target_column}" if row.target_column else ""),
            transformation_type=row.transformation_type,
            source_sql_reference={
                "kind": "DBT_ARTIFACT",
                "artifact_import_id": str(row.artifact_import_id),
                "edge_type": row.edge_type,
            },
        )
    if edge_type in ("OPENLINEAGE_TABLE", "OPENLINEAGE_COLUMN"):
        source = f"{row.input_dataset_namespace}.{row.input_dataset_name}"
        target = f"{row.output_dataset_namespace}.{row.output_dataset_name}"
        transformation = None
        if edge_type == "OPENLINEAGE_COLUMN":
            source += f".{row.input_column_name}"
            target += f".{row.output_column_name}"
            transformation = row.transformation_type
        return ParsedLineageReviewItem(
            edge_id=row.id,
            edge_type=edge_type,
            organization_id=row.organization_id,
            created_at=row.created_at,
            created_by=row.created_by,
            # OpenLineage carries no confidence -- treat as UNKNOWN so
            # the queue can still show something meaningful.
            confidence=None,
            source_label=source,
            target_label=target,
            transformation_type=transformation,
            source_sql_reference={
                "kind": "OPENLINEAGE_RUN",
                "run_event_id": str(row.run_event_id),
                "edge_kind": row.edge_kind,
            },
        )
    return None


__all__ = [
    "EDGE_TYPES",
    "EDGE_TYPE_TO_MODEL",
    "REVIEW_STATUSES",
    "ParsedLineageReviewItem",
    "list_parsed_lineage_review_queue",
    "resolve_review_status_for_new_edge",
]
