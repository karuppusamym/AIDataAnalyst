"""CT-4: reassign a merged rename candidate's downstream links to the new table.

Per `Docs/20-modules/04-catalog.md` #6, an object rename is today a delete plus a
create, which silently orphans anything hanging off the tombstoned object's stable
ID: semantic annotations, lineage edges, governed-tool linkage, relationship
candidates, open data-quality incidents. This module is the merge step a steward
triggers by approving a `RenameCandidate` -- it is never run automatically.

Deliberately an explicit allowlist, not a generic sweep of every foreign key that
targets `metadata_table.id`: several models (`MetadataColumn`,
`MetadataConstraint.table_id`, `TableProfile`, `DataQualityObservation`,
`FreshnessObservation`) are per-scan structural children or immutable historical
measurements. The new table already has its own freshly-created rows of those
kinds, so blindly repointing the old rows at it would duplicate data and can
violate the unique constraints those tables carry on `table_id`. Only genuine
downstream *references* -- rows that mean "this fact is about that table" rather
than "this table's own child row" -- belong in the allowlist below.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from aida.models import (
    AssetCertification,
    AssetDocumentation,
    AssetTag,
    AssetTermLink,
    BiMetricColumnEdge,
    CrossSourceResolutionCandidate,
    DataQualityIncident,
    DataQualityPolicy,
    DbtResource,
    FreshnessWatermarkConfig,
    GlossaryLinkProposal,
    MetadataBusinessAnnotation,
    MetadataConstraint,
    MetadataEnrichmentProposal,
    OpenLineageColumnEdge,
    OpenLineageDataset,
    OpenLineageTableEdge,
    ProcedureLineageEdge,
    RelationshipCandidate,
    SemanticMetricVersion,
    UnownedAssetEscalation,
    ViewLineageEdge,
)

# (model, column name) for every downstream reference to a table's stable ID that
# should follow a merged rename. See the module docstring for what is deliberately
# excluded and why.
TABLE_IDENTITY_DOWNSTREAM_LINKS: tuple[tuple[type, str], ...] = (
    (MetadataConstraint, "referenced_table_id"),
    (DataQualityPolicy, "table_id"),
    (DataQualityIncident, "table_id"),
    (FreshnessWatermarkConfig, "table_id"),
    (SemanticMetricVersion, "source_table_id"),
    (RelationshipCandidate, "source_table_id"),
    (RelationshipCandidate, "target_table_id"),
    (MetadataEnrichmentProposal, "table_id"),
    (MetadataBusinessAnnotation, "table_id"),
    (AssetDocumentation, "table_id"),
    (AssetTermLink, "table_id"),
    (AssetCertification, "table_id"),
    (AssetTag, "table_id"),
    (GlossaryLinkProposal, "table_id"),
    (UnownedAssetEscalation, "table_id"),
    (OpenLineageDataset, "matched_table_id"),
    (OpenLineageTableEdge, "input_table_id"),
    (OpenLineageTableEdge, "output_table_id"),
    (OpenLineageColumnEdge, "input_table_id"),
    (OpenLineageColumnEdge, "output_table_id"),
    (ViewLineageEdge, "source_table_id"),
    (ViewLineageEdge, "target_table_id"),
    (ProcedureLineageEdge, "source_table_id"),
    (ProcedureLineageEdge, "target_table_id"),
    (DbtResource, "matched_table_id"),
    (BiMetricColumnEdge, "matched_table_id"),
    (CrossSourceResolutionCandidate, "source_table_id"),
    (CrossSourceResolutionCandidate, "target_table_id"),
)


async def merge_table_identity(
    session: AsyncSession, *, old_table_id: UUID, new_table_id: UUID
) -> dict[str, int]:
    """Repoint every downstream reference to `old_table_id` at `new_table_id`.

    Returns the number of rows changed per `table.column`, omitting entries where
    nothing moved. Idempotent: re-running after a successful merge finds nothing
    left pointing at `old_table_id` and reassigns nothing.
    """
    reassigned: dict[str, int] = {}
    for model, column_name in TABLE_IDENTITY_DOWNSTREAM_LINKS:
        column: InstrumentedAttribute = getattr(model, column_name)
        result = await session.execute(
            update(model).where(column == old_table_id).values(**{column_name: new_table_id})
        )
        if result.rowcount:
            reassigned[f"{model.__tablename__}.{column_name}"] = result.rowcount
    return reassigned
