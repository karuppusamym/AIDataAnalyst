"""catalog -- domain logic (module 04). Two responsibilities live here:
the read-model composer that turns a page of catalog rows into
``CatalogRowRead`` DTOs (UX-12, below), and the per-item stewardship
apply-functions the bulk endpoints dispatch to (CT-1, the "Bulk actions"
section at the bottom of this file).

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
from aida.certification_evidence import summarize_evidence
from aida.models import (
    AssetCertification,
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    MetadataBusinessAnnotationVersion,
    MetadataTable,
)
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
) -> tuple[str, datetime | None, dict | None]:
    """Return ``(state, expires_at, evidence_summary)`` for one table's
    latest certification row.

    P3-09: ``evidence_summary`` is the small counts fold of the row's
    ``evidence`` blob (``summarize_evidence``) so ``CatalogRowRead`` can
    surface a "based on" tooltip without every reader re-parsing the JSON;
    ``None`` for legacy pre-P3-09 rows (``evidence IS NULL``), for the
    ``NONE`` state, and for the ``EXPIRED`` / ``REVOKED`` states where the
    catalog row's certification is no longer being asserted.
    """
    if certification is None:
        return "NONE", None, None
    expires_at = _as_aware(certification.expires_at)
    if asset_certification_is_active(
        _CertificationForActiveCheck(status=certification.status, expires_at=expires_at), at=now
    ):
        return "CERTIFIED", expires_at, summarize_evidence(certification.evidence)
    if certification.status == "REVOKED":
        return "REVOKED", None, None
    return "EXPIRED", None, None


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
    # `aida.schemas` re-exports bulk-action symbols from this module. Keep the
    # DTO import lazy so Alembic can load the model metadata without cycling
    # through that compatibility shim.
    from aida.schemas import CatalogRowRead

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
        (
            certification,
            certification_expires_at,
            certification_evidence_summary,
        ) = _certification_state(
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
                certification_evidence_summary=certification_evidence_summary,
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



# ---------------------------------------------------------------------------
# CT-1: Bulk stewardship actions (tag, classify, own, certify)
#
# One ``apply_<action>_item`` function per action -- the *single-item* core
# each bulk endpoint dispatches to, one subject at a time, inside that item's
# own SAVEPOINT (``session.begin_nested``). Mirrors PG-3's
# ``_apply_governance_review_decision`` pattern: a single code path decides
# whether one subject succeeds or fails, so a single-item bulk call and a
# batched one can never drift, and a failure partway through one item's
# dispatch is contained to that item's own SAVEPOINT rather than corrupting
# the batch or leaking into sibling items that already committed within the
# same transaction.
#
# Each ``apply_*_item`` function either mutates an already-fetched, session-
# attached ORM row in place (for an update) and returns it, or returns a
# brand-new, not-yet-``session.add``-ed row for the caller to add and flush;
# a precondition failure (subject not found, not ACTIVE, ...) raises
# ``CatalogBulkItemError``, which the API layer catches per item to record a
# FAILED result and move on to the next subject.
#
# Ownership and certification reuse the exact fields and idempotency rules
# already established by GL-2 (``OwnershipAssignment`` subject_type/
# subject_id keying) and GL-5/CT-5 (``AssetCertification`` supersede-then-
# create, ``asset_certification.py``'s active/expiry projection) -- this
# section adds an immediate, per-item partial-success execution path next to
# those workflows, plus the previously-missing tag and classify actions
# described by module 04 (catalog).
#
# Status: moved verbatim from `aida.catalog_bulk_actions` on 2026-09-03 as
# ST-07 Commit B (Phase 5 of `Docs/40-engineering/06-refactor-plan.md`).
# The old path stays as a re-export shim, same pattern as
# `aida.catalog_read_model`.
# ---------------------------------------------------------------------------

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from aida.classification import SENSITIVE_CLASSES
from aida.models import AssetTag, MetadataColumn, OwnershipAssignment

# A single bulk request may touch at most this many subjects, whether the
# caller supplied an explicit ID list (rejected outright above this size) or
# a filter (silently truncated -- see `match_tables_by_filter`). This keeps
# every bulk action bounded in spirit of CT-2 (no unbounded catalog scans).
CATALOG_BULK_ACTION_MAX_ITEMS = 500

# Filter-mode selection scans at most this many candidate rows before giving
# up on finding more matches, mirroring the existing bound used by
# `apply_ownership_rule` in stewardship_api.py.
CATALOG_BULK_FILTER_SCAN_CAP = 10_000

ALLOWED_CLASSIFICATIONS = frozenset({"UNCLASSIFIED", "PUBLIC", "INTERNAL"} | SENSITIVE_CLASSES)

_TABLE_MATCH_FIELDS = ("TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME")


@dataclass(frozen=True)
class BulkItemResult:
    subject_id: str
    status: str
    reason: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"subject_id": self.subject_id, "status": self.status, "reason": self.reason}


@dataclass
class BulkPlan:
    """Accumulates the per-item results of a bulk run as the API layer works
    through ``subject_ids`` one SAVEPOINT at a time. ``new_rows`` is unused by
    the per-item-SAVEPOINT endpoints (each row is added and flushed inside its
    own SAVEPOINT as it is decided) and stays only so any caller that still
    wants a whole-plan-at-once view has somewhere to put one.
    """

    results: list[BulkItemResult] = field(default_factory=list)
    new_rows: list[Any] = field(default_factory=list)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for item in self.results if item.status == "SUCCEEDED")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if item.status == "FAILED")


class CatalogBulkItemError(Exception):
    """Raised by an ``apply_*_item`` function when one subject fails a
    business-rule precondition (not found, wrong status, ...).

    The API layer catches this per item, inside that item's own SAVEPOINT, to
    record a FAILED result with ``str(exc)`` as the reason and continue to the
    next subject -- exactly PG-3's ``HTTPException``-per-item convention,
    adapted to a plain exception since these functions are ORM-only (no
    ``HTTPException`` semantics belong this far from the transport layer).
    """


def dedupe_preserving_order(values: Sequence[UUID]) -> list[UUID]:
    return list(dict.fromkeys(values))


def match_tables_by_filter(
    candidates: Sequence[tuple[MetadataTable, str]],
    *,
    match_field: str,
    match_pattern: str,
    cap: int = CATALOG_BULK_ACTION_MAX_ITEMS,
) -> tuple[list[UUID], bool]:
    """Select active table ids whose name/schema/qualified-name matches a pattern.

    Returns the matched ids (capped at ``cap``) and whether the match set was
    truncated by the cap. Case-insensitive shell-style matching, the same
    matcher already used by ``apply_ownership_rule``.
    """
    if match_field not in _TABLE_MATCH_FIELDS:
        raise ValueError(f"unsupported match_field: {match_field}")
    matched: list[UUID] = []
    truncated = False
    normalized_pattern = match_pattern.casefold()
    for table, schema_name in candidates:
        value = {
            "TABLE_NAME": table.name,
            "SCHEMA_NAME": schema_name,
            "QUALIFIED_NAME": f"{schema_name}.{table.name}",
        }[match_field]
        if not fnmatchcase(value.casefold(), normalized_pattern):
            continue
        if len(matched) >= cap:
            truncated = True
            break
        matched.append(table.id)
    return matched, truncated


def match_columns_by_pattern(
    candidates: Sequence[MetadataColumn],
    *,
    name_pattern: str,
    cap: int = CATALOG_BULK_ACTION_MAX_ITEMS,
) -> tuple[list[UUID], bool]:
    matched: list[UUID] = []
    truncated = False
    normalized_pattern = name_pattern.casefold()
    for column in candidates:
        if not fnmatchcase(column.name.casefold(), normalized_pattern):
            continue
        if len(matched) >= cap:
            truncated = True
            break
        matched.append(column.id)
    return matched, truncated


def _require_active_table(table: MetadataTable | None) -> MetadataTable:
    if table is None:
        raise CatalogBulkItemError("table not found in this organization")
    if table.status != "ACTIVE":
        raise CatalogBulkItemError(f"table status is {table.status}, not ACTIVE")
    return table


def apply_tag_item(
    subject_id: UUID,
    *,
    tables: Mapping[UUID, MetadataTable],
    existing_tags: Mapping[UUID, AssetTag],
    organization_id: UUID,
    tag_key: str,
    tag_value: str | None,
    applied_by: str,
) -> tuple[AssetTag, bool]:
    """Apply one tag to one table. Returns ``(row, is_new)``: for an existing
    tag, ``row`` is that same object mutated in place (already session-
    attached, just needs a flush); for a new tag, ``row`` is a fresh instance
    the caller must ``session.add`` before flushing. Raises
    ``CatalogBulkItemError`` if the table is missing or not ACTIVE.
    """
    _require_active_table(tables.get(subject_id))
    existing = existing_tags.get(subject_id)
    if existing is not None:
        existing.tag_value = tag_value
        existing.applied_by = applied_by
        return existing, False
    return (
        AssetTag(
            organization_id=organization_id,
            table_id=subject_id,
            tag_key=tag_key,
            tag_value=tag_value,
            applied_by=applied_by,
        ),
        True,
    )


def apply_classify_item(
    subject_id: UUID,
    *,
    columns: Mapping[UUID, tuple[MetadataColumn, MetadataTable]],
    classification: str,
) -> MetadataColumn:
    """Apply one classification to one column, mutating it in place (already
    session-attached). Raises ``CatalogBulkItemError`` if the column is
    missing, or if the column or its parent table is not ACTIVE.
    """
    found = columns.get(subject_id)
    if found is None:
        raise CatalogBulkItemError("column not found in this organization")
    column, table = found
    if column.status != "ACTIVE":
        raise CatalogBulkItemError(f"column status is {column.status}, not ACTIVE")
    if table.status != "ACTIVE":
        raise CatalogBulkItemError(f"parent table status is {table.status}, not ACTIVE")
    column.classification = classification
    return column


def apply_own_item(
    subject_id: UUID,
    *,
    tables: Mapping[UUID, MetadataTable],
    existing_assignments: Mapping[UUID, OwnershipAssignment],
    organization_id: UUID,
    owner_type: str,
    owner_principal: str,
    assigned_by: str,
) -> tuple[OwnershipAssignment, bool]:
    """Assign ownership of one table. Returns ``(row, is_new)`` with the same
    convention as ``apply_tag_item``: an existing (subject, owner) assignment
    is reactivated in place (GL-2's idempotency rule), otherwise a fresh
    ``OwnershipAssignment`` is returned for the caller to add. Raises
    ``CatalogBulkItemError`` if the table is missing or not ACTIVE.
    """
    _require_active_table(tables.get(subject_id))
    existing = existing_assignments.get(subject_id)
    if existing is not None:
        existing.status = "ACTIVE"
        existing.assigned_by = assigned_by
        return existing, False
    return (
        OwnershipAssignment(
            organization_id=organization_id,
            subject_type="TABLE",
            subject_id=str(subject_id),
            owner_type=owner_type,
            owner_principal=owner_principal,
            assignment_kind="BULK",
            assigned_by=assigned_by,
        ),
        True,
    )


def apply_certify_item(
    subject_id: UUID,
    *,
    tables: Mapping[UUID, MetadataTable],
    active_certifications: Mapping[UUID, Sequence[AssetCertification]],
    organization_id: UUID,
    rationale: str,
    expires_at: datetime,
    certified_by: str,
    evidence: dict | None = None,
) -> tuple[AssetCertification, list[AssetCertification]]:
    """Certify one table. Returns ``(new_certification, superseded_priors)``:
    ``superseded_priors`` are the table's prior ACTIVE table-level
    certifications, already mutated to ``status="SUPERSEDED"`` in place (GL-5's
    supersede-then-create rule) -- the caller must flush both the new row (once
    added) and every superseded prior together, inside the same SAVEPOINT, so
    a failure can never leave a table with two simultaneously-ACTIVE table
    certifications. Raises ``CatalogBulkItemError`` if the table is missing or
    not ACTIVE.

    P3-09: ``evidence`` is the structured snapshot returned by
    ``aida.certification_evidence.compute_certification_evidence``; every
    caller (single-certify, direct-write bulk, reviewed bulk, playbook)
    passes it so the four paths cannot drift on what "evidence" means.
    Optional here (default ``None``) so a partial-fixture unit test that
    does not exercise the composition can still call this helper.
    """
    _require_active_table(tables.get(subject_id))
    priors = list(active_certifications.get(subject_id, ()))
    for prior in priors:
        prior.status = "SUPERSEDED"
    new_certification = AssetCertification(
        organization_id=organization_id,
        table_id=subject_id,
        asset_type="TABLE",
        rationale=rationale,
        certified_by=certified_by,
        expires_at=expires_at,
        evidence=evidence,
    )
    return new_certification, priors
