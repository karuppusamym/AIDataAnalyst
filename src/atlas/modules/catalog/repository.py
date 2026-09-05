"""catalog -- PRIVATE. Data access for the read-model composer.

Not importable from outside this module (enforced by the `catalog module
privacy` import-linter contract; the `aida.catalog_read_model` shim is on
the allowed_importers list for backward compatibility).

Every function below takes a page's worth of ``table_ids`` and returns one
dict keyed by table id -- a fixed, small number of queries regardless of how
many rows are on the page (`test_catalog_rows_read_model.py` asserts this
with the same `before_cursor_execute` statement-counting pattern
``test_bulk_governance_decisions.py`` uses for its bulk-decision endpoint).

Status: real content (tracker ST-07, Phase 5 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.catalog_read_model`, which now re-exports these helpers for backward
compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.business_annotation_versions import current_version_alias
from aida.models import (
    AssetCertification,
    AssetDescriptionDraft,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    DataQualityIncident,
    DataQualityObservation,
    GlossaryTermVersion,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    OwnershipAssignment,
    TableProfile,
)

# Module 11 doc: the evidence pane describes staleness as "no observation in
# 14 days" (ui-next/src/lib/fixtures.ts mirrors the exact wording) -- this is
# the one place that number is defined so the API and the fixture standing in
# for it agree.
_QUALITY_STALE_AFTER = timedelta(days=14)
_OPEN_INCIDENT_STATUSES = ("OPEN", "ACKNOWLEDGED")


@dataclass(slots=True)
class _CertificationForActiveCheck:
    """Satisfies `AssetCertificationLike` with a tz-normalized `expires_at`,
    without mutating the ORM row (which would mark it dirty).

    Not frozen: `AssetCertificationLike` declares plain (settable) attributes,
    and a frozen dataclass's fields are read-only, which mypy correctly
    treats as not satisfying that protocol even though nothing here ever
    reassigns them.
    """

    status: str
    expires_at: datetime


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC-aware.

    Production runs on PostgreSQL, whose `TIMESTAMPTZ` round-trips a
    `DateTime(timezone=True)` column tz-aware. SQLite (used in this repo's
    test suite, per `test_catalog_pagination.py`, because PostgreSQL is
    unreachable in this sandbox) has no native tz-aware timestamp type and
    hands the same column back naive, which would otherwise make a
    naive/aware comparison raise instead of answering a question.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _latest_table_profiles(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, TableProfile]:
    if not table_ids:
        return {}
    ranked = (
        select(
            TableProfile,
            func.row_number()
            .over(partition_by=TableProfile.table_id, order_by=TableProfile.created_at.desc())
            .label("rn"),
        )
        .where(TableProfile.table_id.in_(table_ids), TableProfile.status == "COMPLETED")
        .subquery()
    )
    alias = aliased(TableProfile, ranked)
    rows = (await session.scalars(select(alias).where(ranked.c.rn == 1))).all()
    return {profile.table_id: profile for profile in rows}


async def _latest_certifications(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, AssetCertification]:
    if not table_ids:
        return {}
    ranked = (
        select(
            AssetCertification,
            func.row_number()
            .over(
                partition_by=AssetCertification.table_id,
                order_by=AssetCertification.created_at.desc(),
            )
            .label("rn"),
        )
        .where(
            AssetCertification.table_id.in_(table_ids),
            AssetCertification.asset_type == "TABLE",
        )
        .subquery()
    )
    alias = aliased(AssetCertification, ranked)
    rows = (await session.scalars(select(alias).where(ranked.c.rn == 1))).all()
    return {cert.table_id: cert for cert in rows}


async def _earliest_active_owners(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, str]:
    """The GL-2 ``OwnershipAssignment`` each table id maps to, if any is active.

    A table can carry more than one active assignment (the unique constraint
    is on the full ``(subject, owner_type, owner_principal)`` tuple), so this
    picks the earliest-assigned one -- stable across re-runs, and consistent
    with "who was made responsible for this first."
    """
    if not table_ids:
        return {}
    ids = [str(value) for value in table_ids]
    ranked = (
        select(
            OwnershipAssignment.subject_id,
            OwnershipAssignment.owner_principal,
            func.row_number()
            .over(
                partition_by=OwnershipAssignment.subject_id,
                order_by=(
                    OwnershipAssignment.created_at.asc(),
                    OwnershipAssignment.owner_principal.asc(),
                ),
            )
            .label("rn"),
        )
        .where(
            OwnershipAssignment.subject_type == "TABLE",
            OwnershipAssignment.status == "ACTIVE",
            OwnershipAssignment.subject_id.in_(ids),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.subject_id, ranked.c.owner_principal).where(ranked.c.rn == 1)
        )
    ).all()
    return {UUID(subject_id): owner for subject_id, owner in rows}


async def _latest_approved_documentation(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, AssetDocumentationVersion]:
    """The current published GL-9 documentation per table, if any.

    ``apply_asset_description_draft`` (`asset_description_service.py`)
    supersedes the prior ``APPROVED`` version in the same transaction it
    approves a new one, so there is normally at most one ``APPROVED`` row per
    ``documentation_id`` -- the ``row_number`` ranking is defence in depth,
    not load-bearing, and picks the highest version if that were ever
    violated.
    """
    if not table_ids:
        return {}
    ranked = (
        select(
            AssetDocumentationVersion,
            AssetDocumentation.table_id.label("table_id"),
            func.row_number()
            .over(
                partition_by=AssetDocumentation.table_id,
                order_by=AssetDocumentationVersion.version.desc(),
            )
            .label("rn"),
        )
        .join(
            AssetDocumentation,
            AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
        )
        .where(
            AssetDocumentation.table_id.in_(table_ids),
            AssetDocumentationVersion.status == "APPROVED",
        )
        .subquery()
    )
    alias = aliased(AssetDocumentationVersion, ranked)
    rows = (
        await session.execute(select(alias, ranked.c.table_id).where(ranked.c.rn == 1))
    ).all()
    return {table_id: version for version, table_id in rows}


async def _latest_pending_drafts(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, AssetDescriptionDraft]:
    if not table_ids:
        return {}
    ranked = (
        select(
            AssetDescriptionDraft,
            func.row_number()
            .over(
                partition_by=AssetDescriptionDraft.table_id,
                order_by=AssetDescriptionDraft.created_at.desc(),
            )
            .label("rn"),
        )
        .where(
            AssetDescriptionDraft.table_id.in_(table_ids),
            AssetDescriptionDraft.status == "PENDING_APPROVAL",
        )
        .subquery()
    )
    alias = aliased(AssetDescriptionDraft, ranked)
    rows = (await session.scalars(select(alias).where(ranked.c.rn == 1))).all()
    return {draft.table_id: draft for draft in rows}


async def _business_annotations(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, MetadataBusinessAnnotationVersion]:
    """The current (`APPROVED`) content version per table -- AT-6:
    `MetadataBusinessAnnotation` itself carries no content any more, see
    `business_annotation_versions.py`.
    """
    if not table_ids:
        return {}
    alias, ranked = current_version_alias()
    rows = (
        await session.execute(
            select(alias, MetadataBusinessAnnotation.table_id)
            .join(alias, alias.annotation_id == MetadataBusinessAnnotation.id)
            .where(
                MetadataBusinessAnnotation.table_id.in_(table_ids),
                ranked.c.rn == 1,
            )
        )
    ).all()
    return {table_id: version for version, table_id in rows}


async def _glossary_terms_by_table(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, list[str]]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(AssetTermLink.table_id, GlossaryTermVersion.display_name)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == AssetTermLink.term_id)
            .where(
                AssetTermLink.table_id.in_(table_ids),
                GlossaryTermVersion.status == "APPROVED",
            )
            .order_by(GlossaryTermVersion.display_name)
        )
    ).all()
    terms: dict[UUID, list[str]] = {}
    for table_id, display_name in rows:
        terms.setdefault(table_id, []).append(display_name)
    return terms


async def _open_incident_table_ids(session: AsyncSession, table_ids: list[UUID]) -> set[UUID]:
    if not table_ids:
        return set()
    rows = await session.scalars(
        select(DataQualityIncident.table_id)
        .where(
            DataQualityIncident.table_id.in_(table_ids),
            DataQualityIncident.status.in_(_OPEN_INCIDENT_STATUSES),
        )
        .distinct()
    )
    return set(rows.all())


async def _latest_observation_at(
    session: AsyncSession, table_ids: list[UUID]
) -> dict[UUID, datetime]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(DataQualityObservation.table_id, func.max(DataQualityObservation.created_at))
            .where(DataQualityObservation.table_id.in_(table_ids))
            .group_by(DataQualityObservation.table_id)
        )
    ).all()
    return {table_id: _as_aware(observed_at) for table_id, observed_at in rows}
