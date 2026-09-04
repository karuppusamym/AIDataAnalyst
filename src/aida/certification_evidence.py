"""P3-09: structured evidence blob for an ``AssetCertification`` at certify time.

``AssetCertification`` used to carry only the free-text ``rationale`` --
which meant a follow-up question of "what did the certifier actually
vouch for?" could not be answered mechanically, and a future
revoke-on-evidence-change job (auto-revoke when the description that was
current at certify time is superseded, or when the owner assignment that
was ACTIVE at certify time lapses) had nothing structured to key off.
This module composes the four things a certifier is implicitly attesting
to -- description version, active ownership, quality snapshot, and glossary
terms -- into one JSON-serialisable dict written to
``AssetCertification.evidence`` alongside the ``rationale`` (both are
populated on every new write; the free-text field stays for human
readability and pre-P3-09 rows still project correctly with
``evidence IS NULL``).

Called from every code path that creates an ``AssetCertification`` row --
``atlas.modules.catalog.router.certify_table_asset`` (single),
``atlas.modules.catalog.router.bulk_certify_tables`` (direct-write bulk),
``aida.stewardship_service`` CERTIFY_ASSET branch (reviewed bulk),
``aida.playbooks._apply_one_item`` CERTIFY (playbook auto-apply) -- so the
four paths cannot drift on what "evidence" means. The helper is async
because two of the composition steps hit the DB; each of them mirrors an
existing composition idiom already used by the catalog read model
(``atlas.modules.catalog.repository``), rather than introducing a fresh
query shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AssetCertification,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    DataQualityIncident,
    DataQualityObservation,
    GlossaryTermVersion,
    OwnershipAssignment,
    TableProfile,
)

# Bumped when the shape written by `compute_certification_evidence` changes
# in a way readers must notice. Written into the evidence blob so a future
# `revoke-on-evidence-change` job can refuse to make a decision from a
# schema version it does not understand. Kept as a string so downstream
# projections can compare stably without importing this module.
EVIDENCE_SCHEMA_VERSION = "1"


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


async def compute_certification_evidence(
    session: AsyncSession,
    table_id: UUID,
    *,
    organization_id: UUID,
    now: datetime,
    certifier_notes: str | None = None,
) -> dict[str, Any]:
    """Snapshot the four current facts a table-level certification vouches
    for, as a JSON-serialisable dict ready for
    ``AssetCertification.evidence``.

    Callers that already know ``rationale`` (the free-text field kept
    populated in parallel) should pass it as ``certifier_notes`` so the
    machine-consumable and human-readable copies agree; the shape stays
    valid with ``certifier_notes=None`` for callers that intend to leave
    the JSON side empty.

    Kept intentionally best-effort at ``supporting_dq_check_ids`` (empty
    list for now) -- the eventual per-check granularity (specific DQ rule
    outcomes that underpinned the trust) is future work; the field is
    written today so pre-existing readers do not have to grow a new key
    later.
    """
    # Current APPROVED description version for this table (the same query
    # `_latest_approved_documentation` picks per-table, restricted to one).
    description_version_id_row = (
        await session.execute(
            select(AssetDocumentationVersion.id)
            .join(
                AssetDocumentation,
                AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
            )
            .where(
                AssetDocumentation.table_id == table_id,
                AssetDocumentationVersion.status == "APPROVED",
                AssetDocumentationVersion.organization_id == organization_id,
            )
            .order_by(AssetDocumentationVersion.version.desc())
            .limit(1)
        )
    ).first()
    description_version_id: str | None = (
        str(description_version_id_row[0]) if description_version_id_row else None
    )

    # ACTIVE OwnershipAssignment ids for the (TABLE, table_id) subject.
    owner_rows = (
        await session.scalars(
            select(OwnershipAssignment.id).where(
                OwnershipAssignment.organization_id == organization_id,
                OwnershipAssignment.subject_type == "TABLE",
                OwnershipAssignment.subject_id == str(table_id),
                OwnershipAssignment.status == "ACTIVE",
            )
        )
    ).all()
    ownership_assignment_ids: list[str] = [str(v) for v in owner_rows]

    # Quality snapshot: open incident count (OPEN|ACKNOWLEDGED), latest
    # observation timestamp, and count of COMPLETED profile runs for this
    # table. Mirrors the shape module 11 already exposes for the catalog
    # composer -- there is no new query intent here, only "capture it once
    # so the certification carries its own copy."
    open_incident_count = (
        await session.scalar(
            select(func.count(DataQualityIncident.id)).where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.table_id == table_id,
                DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        )
    ) or 0
    latest_observation_at = await session.scalar(
        select(func.max(DataQualityObservation.created_at)).where(
            DataQualityObservation.organization_id == organization_id,
            DataQualityObservation.table_id == table_id,
        )
    )
    profiles_ran = (
        await session.scalar(
            select(func.count(TableProfile.id)).where(
                TableProfile.organization_id == organization_id,
                TableProfile.table_id == table_id,
                TableProfile.status == "COMPLETED",
            )
        )
    ) or 0

    # Asset-linked terms that carry an APPROVED version (same join as the
    # catalog composer's `_glossary_terms_by_table`).
    term_id_rows = (
        await session.execute(
            select(AssetTermLink.term_id)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == AssetTermLink.term_id)
            .where(
                AssetTermLink.table_id == table_id,
                AssetTermLink.organization_id == organization_id,
                GlossaryTermVersion.status == "APPROVED",
            )
            .distinct()
        )
    ).all()
    glossary_term_ids: list[str] = [str(row[0]) for row in term_id_rows]

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "captured_at": _to_iso(now),
        "description_version_id": description_version_id,
        "ownership_assignment_ids": ownership_assignment_ids,
        "quality_snapshot": {
            "open_incident_count_at_certify": int(open_incident_count),
            "latest_observation_at": _to_iso(latest_observation_at),
            "profiles_ran": int(profiles_ran),
        },
        "glossary_term_ids": glossary_term_ids,
        # Reserved for the future revoke-on-evidence-change workflow, which
        # will point specific DQ check outcomes (custom rule packs, external
        # signals) at the certification they underpinned. Empty list today
        # so schema readers do not have to grow a new optional key later.
        "supporting_dq_check_ids": [],
        "certifier_notes": certifier_notes,
    }


def summarize_evidence(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fold a stored `evidence` blob into the small dict the catalog UI
    hover-tooltip reads. Returns ``None`` when the certification has no
    structured evidence (legacy row), so ``CatalogRowRead`` fields can
    stay nullable.
    """
    if not evidence:
        return None
    quality = evidence.get("quality_snapshot") or {}
    return {
        "description_version_id": evidence.get("description_version_id"),
        "active_owner_count": len(evidence.get("ownership_assignment_ids") or []),
        "open_incident_count_at_certify": int(
            quality.get("open_incident_count_at_certify") or 0
        ),
        "glossary_term_count": len(evidence.get("glossary_term_ids") or []),
        "backfilled": bool(evidence.get("backfilled", False)),
    }


async def backfill_certification_evidence_v1(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """Best-effort one-shot backfill for pre-P3-09 rows.

    The certification row's own ``created_at`` is retained, but the true
    historical state of description version / ownership / quality / glossary
    at that instant is gone (there is no as-of query for these tables), so
    this fills the blob from *now's* state and tags it ``backfilled=True``
    so a downstream reader can distinguish an as-of-certify snapshot from a
    reconstructed one. Only rows with ``status == "ACTIVE"`` and
    ``evidence IS NULL`` are touched -- SUPERSEDED / REVOKED / EXPIRED rows
    are frozen audit evidence and are never mutated by a background job.
    Second and subsequent runs are no-ops on any row already populated
    (the `evidence IS NULL` filter is the idempotency guarantee).

    Returns the number of rows populated. Callers must ``commit`` the
    session; this function only stages the writes so a CLI wrapping it can
    log a dry-run count without committing.
    """
    moment = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.status == "ACTIVE",
                AssetCertification.evidence.is_(None),
                # Column-level certifications carry no independent evidence
                # composition path today; skip them so this run does not
                # tag them with a table-level snapshot.
                AssetCertification.asset_type == "TABLE",
            )
        )
    ).all()
    populated = 0
    for row in rows:
        evidence = await compute_certification_evidence(
            session,
            row.table_id,
            organization_id=row.organization_id,
            now=moment,
            certifier_notes=row.rationale,
        )
        evidence["backfilled"] = True
        evidence["backfilled_at"] = _to_iso(moment)
        row.evidence = evidence
        populated += 1
    return populated
