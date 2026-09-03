"""catalog -- domain logic (module 04). The read-model composer that turns a
page of catalog rows into ``CatalogRowRead`` DTOs.

UX-12 (module 21 experience shell, built on module 04 catalog).
`Docs/20-modules/21-experience-shell.md` names the gap this closes: the
catalog table UI needs description, proposal state, owner, certification,
quality, glossary terms and a row estimate for every table, but those live
on five different endpoints keyed by table id. Rendering a page of 100 rows
the naive way costs 1 + (100 x 5) = 501 requests; this module composes the
same fields in a small, page-size-independent number of batched queries so
`GET /v1/organizations/{org}/catalog/rows` (`aida.api.list_catalog_rows`)
can do it in one round trip.

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
    either, the older ``MetadataBusinessAnnotation.business_description``
    (always review-approved -- see ``semantic_inference.py``) and finally
    the connector-sourced ``MetadataTable.source_description`` are used,
    both ``description_is_proposed=False`` since neither is a pending
    proposal.
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
    ``INCIDENT_OPEN``) and ``DataQualityObservation`` recency (no
    observation ever -> ``UNKNOWN``; last observation older than
    ``_QUALITY_STALE_AFTER`` -> ``STALE``; otherwise ``PASSING``). Module
    11's own coupling API (``get_trust_signal``, DQ-3) is documented as
    planned, not built, so this reads the same source tables directly rather
    than a call site that does not exist yet.
``glossary_terms``
    GL-8/SM-2 ``AssetTermLink`` joined to ``GlossaryTermVersion`` (status
    ``APPROVED``) -- the same join
    ``asset_description_service.gather_evidence`` uses for one table,
    batched here across a page.
``row_count_estimate``
    Latest ``TableProfile.row_count_estimate`` (module 05 profiling), the
    same "latest profile" query ``get_latest_table_profile`` uses for one
    table (`aida.api`), batched here across a page via the same
    ``row_number() OVER (PARTITION BY table_id ...)`` idiom already used in
    ``intelligence_api._latest_table_profiles`` and ``quality_service``.

Status: real content (tracker ST-07, Phase 5 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.catalog_read_model`, which now re-exports the public composer and the
still-`_prefixed` helpers for backward compatibility with the four external
callers named in that shim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_certification import asset_certification_is_active
from aida.models import (
    AssetCertification,
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    MetadataBusinessAnnotationVersion,
    MetadataTable,
)
from aida.schemas import CatalogRowRead
from atlas.modules.catalog.repository import (
    _QUALITY_STALE_AFTER,
    _as_aware,
    _business_annotations,
    _CertificationForActiveCheck,
    _earliest_active_owners,
    _glossary_terms_by_table,
    _latest_approved_documentation,
    _latest_certifications,
    _latest_observation_at,
    _latest_pending_drafts,
    _latest_table_profiles,
    _open_incident_table_ids,
)


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
