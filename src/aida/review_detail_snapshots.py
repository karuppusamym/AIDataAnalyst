"""On-demand review snapshots. Large import diffs are never embedded per queue row."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import ContextProductVersion, GovernanceReview, ModelImportBatch, ModelImportChange

DETAIL_TYPES = frozenset({"CONTEXT_PRODUCT_VERSION", "MODEL_IMPORT_BATCH"})
CONTEXT_FIELDS = (
    "name",
    "description",
    "purpose",
    "owner_type",
    "owner_principal",
    "table_ids",
    "semantic_model_version_ids",
    "glossary_term_version_ids",
    "eligible_tool_version_ids",
    "allowed_consumer_roles",
    "lineage_depth",
    "quality_requirements",
    "policy_summary",
    "support_window_days",
    "fingerprint",
)


async def detail_snapshots(
    session: AsyncSession, review: GovernanceReview
) -> tuple[dict[str, Any], dict[str, Any], str]:
    try:
        object_id = UUID(review.object_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="review target is unavailable") from exc
    if review.object_type == "CONTEXT_PRODUCT_VERSION":
        version = await session.get(ContextProductVersion, object_id)
        if version is None or version.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        base = (
            await session.get(ContextProductVersion, version.based_on_version_id)
            if version.based_on_version_id
            else None
        )
        if base is not None and (
            base.organization_id != version.organization_id or base.product_id != version.product_id
        ):
            raise HTTPException(status_code=409, detail="review baseline is unavailable")
        if version.based_on_version_id and base is None:
            raise HTTPException(status_code=409, detail="review baseline is unavailable")
        return (
            {field: getattr(base, field) for field in CONTEXT_FIELDS} if base else {},
            {field: getattr(version, field) for field in CONTEXT_FIELDS},
            "Compared with the declared base version; no base means a new definition. "
            "Lifecycle action: "
            + review.requested_action,
        )
    batch = await session.get(ModelImportBatch, object_id)
    if (
        batch is None
        or batch.organization_id != review.organization_id
        or batch.governance_review_id != review.id
    ):
        raise HTTPException(status_code=409, detail="review target is unavailable")
    rows = list(
        await session.scalars(
            select(ModelImportChange)
            .where(
                ModelImportChange.batch_id == batch.id,
                ModelImportChange.organization_id == review.organization_id,
            )
            .order_by(
                ModelImportChange.sheet_name, ModelImportChange.row_number, ModelImportChange.id
            )
            .limit(50001)
        )
    )
    if len(rows) > 50000:
        raise HTTPException(
            status_code=422, detail="review preview exceeds 50,000 rows; split the batch"
        )
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    omitted: dict[str, Any] = {}
    for row in rows:
        key = f"{row.subject_label} / {row.field} / row {row.row_number} / {row.id}"
        if row.status in {"REJECTED", "EXCLUDED"}:
            omitted[key] = {
                "status": row.status,
                "reason": row.skip_reason,
                "proposed_value": row.new_value,
            }
            continue
        before[key] = row.old_value
        after[key] = row.new_value
    return (
        {"changes": before, "not_included": omitted},
        {"changes": after, "not_included": omitted},
        f"{batch.filename}: {len(before)} included rows; {len(omitted)} rejected/excluded rows. "
        "Values reflect the saved proposal, not a claim that source data changed. "
        "Stale versions are checked at approval.",
    )
