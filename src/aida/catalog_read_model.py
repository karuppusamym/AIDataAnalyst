"""UX-12: `CatalogRowRead` read-model composition (module 21 experience shell,
built on module 04 catalog).

`Docs/20-modules/21-experience-shell.md` names the gap this closes: the
catalog table UI needs description, proposal state, owner, certification,
quality, glossary terms and a row estimate for every table, but those live on
five different endpoints keyed by table id. Rendering a page of 100 rows the
naive way costs 1 + (100 x 5) = 501 requests; this module composes the same
fields in a small, page-size-independent number of batched queries so
`GET /v1/organizations/{org}/catalog/rows` (`aida.api.list_catalog_rows`) can
do it in one round trip.

No writes happen here (ADR-0004 is untouched -- nothing here executes
source-system SQL or accepts a caller-supplied statement; every query below
targets this platform's own metadata tables) and no new data is introduced:
every field already exists, this module only joins and picks the current
value.

Field sources
-------------
``description`` / ``description_is_proposed``
    GL-9's evidence-scored drafting pipeline (`asset_description_service.py`)
    is checked first: the latest ``APPROVED`` ``AssetDocumentationVersion``
    readme (``description_is_proposed=False``). Absent that, a
    ``PENDING_APPROVAL`` ``AssetDescriptionDraft`` is shown as a proposal
    (``description_is_proposed=True``) -- this is "proposal state" from the
    tracker exit criterion, folded into one boolean because the client type
    already committed to that shape (``CatalogRowRead.description_is_proposed``,
    ``ui-next/src/lib/types.ts``) and has no separate field for it. Absent
    either, the older ``MetadataBusinessAnnotation.business_description`` (always
    review-approved -- see ``semantic_inference.py``) and finally the
    connector-sourced ``MetadataTable.source_description`` are used, both
    ``description_is_proposed=False`` since neither is a pending proposal.
``owner``
    GL-2 ``OwnershipAssignment`` (status ``ACTIVE``, ``subject_type`` ``TABLE``)
    is authoritative; falls back to the approved documentation version's
    ``owner_principal`` when no explicit assignment exists, mirroring the
    two-source definition of "owned" in ``stewardship_api._owned_table_ids``.
``certification``
    CT-5 ``AssetCertification`` (``asset_type`` ``TABLE``), newest row first,
    projected through ``asset_certification_is_active`` -- the same
    query-time projection every other certification caller uses
    (`aida.asset_certification`), never the raw `status` column.
``quality``
    Module 11's ``DataQualityIncident`` (``OPEN``/``ACKNOWLEDGED`` ->
    ``INCIDENT_OPEN``) and ``DataQualityObservation`` recency (no observation
    ever -> ``UNKNOWN``; last observation older than ``_QUALITY_STALE_AFTER``
    -> ``STALE``; otherwise ``PASSING``). Module 11's own coupling API
    (``get_trust_signal``, DQ-3) is documented as planned, not built, so this
    reads the same source tables directly rather than a call site that does
    not exist yet.
``glossary_terms``
    GL-8/SM-2 ``AssetTermLink`` joined to ``GlossaryTermVersion`` (status
    ``APPROVED``) -- the same join ``asset_description_service.gather_evidence``
    uses for one table, batched here across a page.
``row_count_estimate``
    Latest ``TableProfile.row_count_estimate`` (module 05 profiling), the same
    "latest profile" query ``get_latest_table_profile`` uses for one table
    (`aida.api`), batched here across a page via the same
    ``row_number() OVER (PARTITION BY table_id ...)`` idiom already used in
    ``intelligence_api._latest_table_profiles`` and ``quality_service``.

Every function below takes a page's worth of ``table_ids`` and returns one
dict keyed by table id -- a fixed, small number of queries regardless of how
many rows are on the page (`test_catalog_rows_read_model.py` asserts this
with the same `before_cursor_execute` statement-counting pattern
``test_bulk_governance_decisions.py`` uses for its bulk-decision endpoint).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.asset_certification import asset_certification_is_active
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
    MetadataTable,
    OwnershipAssignment,
    TableProfile,
)
from aida.schemas import CatalogRowRead

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


def _quality_state(
    table_id: UUID,
    *,
    open_incident_ids: set[UUID],
    latest_observation_at: dict[UUID, datetime],
    now: datetime,
) -> str:
    if table_id in open_incident_ids:
        return "INCIDENT_OPEN"
    observed_at = latest_observation_at.get(table_id)
    if observed_at is None:
        return "UNKNOWN"
    if now - observed_at > _QUALITY_STALE_AFTER:
        return "STALE"
    return "PASSING"


def _certification_state(
    certification: AssetCertification | None, *, now: datetime
) -> tuple[str, datetime | None]:
    if certification is None:
        return "NONE", None
    expires_at = _as_aware(certification.expires_at)
    if asset_certification_is_active(
        _CertificationForActiveCheck(status=certification.status, expires_at=expires_at), at=now
    ):
        return "CERTIFIED", expires_at
    if certification.status == "REVOKED":
        return "REVOKED", None
    return "EXPIRED", None


def _description(
    table: MetadataTable,
    *,
    documentation: AssetDocumentationVersion | None,
    pending_draft: AssetDescriptionDraft | None,
    annotation: MetadataBusinessAnnotationVersion | None,
) -> tuple[str | None, bool]:
    if documentation is not None:
        return documentation.readme, False
    if pending_draft is not None:
        return pending_draft.drafted_text, True
    if annotation is not None:
        return annotation.business_description, False
    return table.source_description, False


async def compose_catalog_rows(
    session: AsyncSession,
    rows: list[tuple[MetadataTable, str, str]],
    *,
    now: datetime | None = None,
) -> list[CatalogRowRead]:
    """Compose one ``CatalogRowRead`` per ``(table, schema_name, datasource_name)``
    in ``rows``, in a fixed number of batched queries independent of ``len(rows)``.
    """
    moment = now or datetime.now(UTC)
    table_ids = [table.id for table, _, _ in rows]

    profiles = await _latest_table_profiles(session, table_ids)
    certifications = await _latest_certifications(session, table_ids)
    owners = await _earliest_active_owners(session, table_ids)
    documentation = await _latest_approved_documentation(session, table_ids)
    pending_drafts = await _latest_pending_drafts(session, table_ids)
    annotations = await _business_annotations(session, table_ids)
    glossary_terms = await _glossary_terms_by_table(session, table_ids)
    open_incident_ids = await _open_incident_table_ids(session, table_ids)
    latest_observation_at = await _latest_observation_at(session, table_ids)

    composed: list[CatalogRowRead] = []
    for table, schema_name, datasource_name in rows:
        description, description_is_proposed = _description(
            table,
            documentation=documentation.get(table.id),
            pending_draft=pending_drafts.get(table.id),
            annotation=annotations.get(table.id),
        )
        owner = owners.get(table.id)
        if owner is None:
            doc_version = documentation.get(table.id)
            owner = doc_version.owner_principal if doc_version else None
        certification, certification_expires_at = _certification_state(
            certifications.get(table.id), now=moment
        )
        profile = profiles.get(table.id)
        composed.append(
            CatalogRowRead(
                id=table.id,
                name=table.name,
                schema_name=schema_name,
                datasource_name=datasource_name,
                object_type=table.object_type,
                status=table.status,
                description=description,
                description_is_proposed=description_is_proposed,
                owner=owner,
                certification=certification,
                certification_expires_at=certification_expires_at,
                quality=_quality_state(
                    table.id,
                    open_incident_ids=open_incident_ids,
                    latest_observation_at=latest_observation_at,
                    now=moment,
                ),
                glossary_terms=glossary_terms.get(table.id, []),
                row_count_estimate=profile.row_count_estimate if profile else None,
                updated_at=table.updated_at,
            )
        )
    return composed
