"""R11-OKF02 (design item 14B): the one stored, approved OKF bundle every door reads.

R11-OKF01 froze and rendered a bundle on every request, so a manifest a caller inspected and the
archive it downloaded a moment later were two independent captures that could describe different
catalog states. This module makes a bundle a stored thing. A consumer -- the REST manifest,
document and download routes, the MCP resource reader and the Catalog / Context Products
knowledge views -- reads a **published** bundle through `read_published_bundle`, and nothing else
in the codebase renders one for a consumer. That is what "REST/MCP and later GraphQL reads share
the same approved snapshot" means structurally rather than coincidentally:
`tests/test_okf_store.py` fails if a reading surface stops reaching this function or starts
reaching the freeze or the renderer directly.

**Keyed on the reader's authority, evaluated on every request (INV-5, OKF-D).** A stored bundle
belongs to a *lineage*: one product version under one `authority_digest`, a digest of the
version and of the exact set of datasources the reader's own authorization admitted
(`aida.okf_snapshot.admit_datasources` -- the freeze's own decision, taken once and handed to the
freeze). Every read takes that decision again before it looks anything up, so:

* two readers with the same authority share one publication and see identical bytes;
* a reader whose cross-boundary grant, source binding or policy was revoked computes a
  different digest, and the bundle built under the grant is simply not in their lineage. There
  is no cache entry keyed without the caller's authority that a revoked caller could still hit,
  and nothing to "invalidate" by hand -- the key moved with the authorization.

**Stale content is detected before it is served.** A lineage's head records the digest of the
*change marks* inside a window: FP15/FP16 change signals for the product's tables and routines,
approved-description versions (asset, column, routine), reviewed view and procedure lineage,
captured definition versions and the pinned tool versions. Every read recomputes that digest; a
new mark -- including one that committed late with an earlier timestamp -- makes the head stale
and the read rebuilds. A change that leaves no mark at all (a column reclassified in place, a
datasource renamed) is caught by revalidation once the head is older than
`OKF_REVALIDATE_AFTER`, which re-freezes and publishes **only if content moved**.

**Rebuilds are incremental (OKF-C).** A rebuild freezes once, then
`aida.okf_export.export_okf_bundle_incremental` renders only the documents whose subject, source
or link targets moved plus the indexes that list them, and carries every other document's stored
bytes forward unrendered. A revalidation whose snapshot is unchanged writes no publication at
all, so a no-op scan cannot move a hash.

**A capture is read-consistent or refused.** The marks are read before any content and again
after the freeze; if a mark moved in between, the capture may mix two catalog states and the read
is refused with a 409 rather than published. The next request captures afresh.

**Publication is atomic.** The publication row, every document row and the head move in one
savepoint. A reader resolves the head and reads that publication's documents; it sees the old
complete bundle or the new complete bundle and never a mixture, because publications are
immutable and a superseded one is kept (`RETAINED_PUBLICATIONS`) for readers pinned to it.

**Value-free (INV-6).** The stored snapshot is `OkfSnapshot` written out -- a type with no field
that can hold a body, a definition, a default or a row -- and a bundle that fails the publish
policy's code-fence check is refused storage outright. Audit and outbox payloads carry ids,
digests and counts only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import structlog
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.config import Settings
from aida.context import get_correlation_id
from aida.context_compiler_api import _load_source
from aida.envelope_models import (
    MetadataRoutineDefinitionVersion,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.events import record_audit, record_outbox
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ViewLineageEdge,
)
from aida.okf_context import (
    DEFAULT_MAX_CHARS,
    OkfContext,
    assemble_context,
    hop_targets,
    plan_context,
)
from aida.okf_export import (
    EXPORT_PROFILE,
    EXPORT_PROFILE_VERSION,
    MAX_DOCUMENTS,
    OKF_SPEC_REVISION,
    TRIGGER_INITIAL,
    TRIGGER_RENDERER_CHANGE,
    TRIGGER_REVALIDATION,
    TRIGGER_SOURCE_CHANGE,
    OkfBundle,
    OkfDocument,
    OkfExportError,
    OkfLogEntry,
    OkfPublicationStamp,
    OkfSnapshot,
    OkfValidation,
    document_subjects,
    export_okf_bundle_incremental,
    object_key,
    snapshot_from_document,
    snapshot_to_document,
    validate_atlas_publish_policy,
)
from aida.okf_snapshot import admit_datasources, freeze_snapshot
from aida.okf_store_models import OkfBundleDocument, OkfBundleHead, OkfBundlePublication
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.security import enforce_organization
from aida.security_types import SecurityContext

logger = structlog.get_logger(__name__)

#: A head older than this is re-frozen before it is served, which is what bounds how long a
#: change that leaves no mark can go unnoticed. Re-freezing publishes nothing if nothing moved.
OKF_REVALIDATE_AFTER: Final = timedelta(minutes=15)
#: How far back a change mark is looked for. A mark committed late -- a long scan transaction
#: whose signal carries its own earlier `detected_at` -- is still inside the window and still
#: moves the digest. The window bounds the longest open transaction the probe can see.
MARK_LOOKBACK: Final = timedelta(hours=6)
#: Superseded publications kept per lineage, so a reader pinned to one by id (the manifest it
#: inspected, then the archive it downloads) is served the same bytes after a newer publish.
RETAINED_PUBLICATIONS: Final = 5
#: Early refusal bounds, checked from the product version's own pins *before* anything is
#: frozen: "reject size/cardinality excess before building the full in-memory document list,
#: and bound snapshot loading as well as final output" (R11-OKF02 re-review pickup).
MAX_SCOPE_SUBJECTS: Final = MAX_DOCUMENTS - 1_000
MAX_SCOPE_COLUMNS: Final = 250_000
#: How many rendered paths a publication's change summary lists; the count is always exact.
MAX_SUMMARY_PATHS: Final = 1_000
#: How many section receipts one context read's audit record lists; the count is always exact.
MAX_AUDITED_SECTIONS: Final = 100

BUNDLE_ROLE_CHANNELS: Final = {
    "manifest": "OKF_MANIFEST",
    "download": "OKF_DOWNLOAD",
    "document": "OKF_DOCUMENT",
    "history": "OKF_HISTORY",
    "object": "OKF_OBJECT",
    "mcp": "MCP_OKF",
    # Question-specific context: the REST route, the MCP knowledge tool and Ask generation.
    "context": "OKF_CONTEXT",
    "mcp_context": "MCP_OKF_CONTEXT",
    "ask": "ASK_OKF_CONTEXT",
}

#: Findings that mean a document may carry code text. Never stored, whatever else is true.
_UNSTORABLE_FINDINGS: Final = ("FORBIDDEN_CODE_FENCE",)


@dataclass(frozen=True, slots=True)
class OkfMarks:
    """Change marks for one product scope: `kind:subject:row:instant` strings with their
    instant. Value-free: ids and timestamps only."""

    marks: tuple[tuple[str, datetime], ...]

    def digest(self, since: datetime) -> str:
        inside = sorted(mark for mark, at in self.marks if at >= since)
        return hashlib.sha256(json.dumps(inside).encode("utf-8")).hexdigest()

    def subjects_since(self, since: datetime) -> frozenset[str]:
        return frozenset(
            mark.split(":", 2)[1] for mark, at in self.marks if at >= since
        )


@dataclass(frozen=True, slots=True)
class OkfPublishedBundle:
    """One stored publication as a reader receives it, plus the scope it was read under."""

    publication: OkfBundlePublication
    head: OkfBundleHead
    product: ContextProduct
    version: ContextProductVersion
    quality_snapshot: dict[str, object]
    authority_digest: str
    is_current: bool
    published_now: bool

    @property
    def validation(self) -> OkfValidation:
        verdict = dict(self.publication.change_summary.get("validation") or {})
        return OkfValidation(
            valid=bool(verdict.get("valid", False)),
            findings=tuple(str(item) for item in verdict.get("findings") or ()),
        )

    @property
    def history(self) -> tuple[OkfLogEntry, ...]:
        return tuple(_entry_from_document(item) for item in self.publication.history or [])


# --- time -------------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes and PostgreSQL aware ones; compare them as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --- authority --------------------------------------------------------------------------


def authority_digest(version: ContextProductVersion, admitted: Sequence[UUID]) -> str:
    """The lineage key: this version, under this admitted datasource set, by this renderer.

    The renderer pin is part of the key's *publication* chain rather than of the key, so a
    profile bump is a RENDERER_CHANGE publication in the same lineage (its log keeps history).
    """
    payload = {
        "organization_id": str(version.organization_id),
        "context_product_version_id": str(version.id),
        "product_fingerprint": version.fingerprint,
        "admitted_datasource_ids": sorted(str(value) for value in admitted),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --- change marks -----------------------------------------------------------------------


def _uuid_list(values: Sequence[Any] | None) -> list[UUID]:
    out: list[UUID] = []
    for value in values or []:
        try:
            out.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return out


async def change_marks(
    session: AsyncSession, version: ContextProductVersion, *, since: datetime
) -> OkfMarks:
    """Every value-free change mark on this product's scope at or after `since`.

    FP15 change signals are the primary source -- they are how FP16 already knows which view was
    redefined, which table reshaped or retired, which routine changed -- and the rest are the
    approval and review events FP15 does not signal but a document states: approved descriptions,
    reviewed lineage and the pinned tools. Each query restates `organization_id` (INV-5).
    """
    organization_id = version.organization_id
    table_ids = _uuid_list(version.table_ids)
    routine_ids = _uuid_list(version.routine_ids)
    tool_ids = _uuid_list(version.eligible_tool_version_ids)
    subject_ids = [*table_ids, *routine_ids]
    marks: list[tuple[str, datetime]] = []

    async def collect(kind: str, statement: Any) -> None:
        for row_id, subject, at in (await session.execute(statement)).all():
            if at is None:
                continue
            moment = _utc(at)
            marks.append((f"{kind}:{subject}:{row_id}:{moment.isoformat()}", moment))

    if subject_ids:
        await collect(
            "signal",
            select(
                MetadataChangeSignal.id,
                MetadataChangeSignal.subject_id,
                MetadataChangeSignal.detected_at,
            ).where(
                MetadataChangeSignal.organization_id == organization_id,
                MetadataChangeSignal.subject_id.in_(subject_ids),
                MetadataChangeSignal.detected_at >= since,
            ),
        )
    if table_ids:
        await collect(
            "asset-description",
            select(
                AssetDocumentationVersion.id,
                AssetDocumentation.table_id,
                AssetDocumentationVersion.updated_at,
            )
            .join(
                AssetDocumentation,
                AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
            )
            .where(
                AssetDocumentation.organization_id == organization_id,
                AssetDocumentationVersion.organization_id == organization_id,
                AssetDocumentation.table_id.in_(table_ids),
                AssetDocumentationVersion.updated_at >= since,
            ),
        )
        await collect(
            "column-description",
            select(
                ColumnDocumentationVersion.id,
                ColumnDocumentation.table_id,
                ColumnDocumentationVersion.updated_at,
            )
            .join(
                ColumnDocumentation,
                ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
            )
            .where(
                ColumnDocumentation.organization_id == organization_id,
                ColumnDocumentationVersion.organization_id == organization_id,
                ColumnDocumentation.table_id.in_(table_ids),
                ColumnDocumentationVersion.updated_at >= since,
            ),
        )
        await collect(
            "view-lineage",
            select(
                ViewLineageEdge.id, ViewLineageEdge.target_table_id, ViewLineageEdge.updated_at
            ).where(
                ViewLineageEdge.organization_id == organization_id,
                ViewLineageEdge.target_table_id.in_(table_ids),
                ViewLineageEdge.updated_at >= since,
            ),
        )
    if routine_ids:
        await collect(
            "routine-description",
            select(
                RoutineDocumentationVersion.id,
                RoutineDocumentation.routine_id,
                RoutineDocumentationVersion.updated_at,
            )
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.organization_id == organization_id,
                RoutineDocumentationVersion.organization_id == organization_id,
                RoutineDocumentation.routine_id.in_(routine_ids),
                RoutineDocumentationVersion.updated_at >= since,
            ),
        )
        await collect(
            "procedure-lineage",
            select(
                DeepProcedureLineageEdge.id,
                DeepProcedureLineageEdge.routine_id,
                DeepProcedureLineageEdge.updated_at,
            ).where(
                DeepProcedureLineageEdge.organization_id == organization_id,
                DeepProcedureLineageEdge.routine_id.in_(routine_ids),
                DeepProcedureLineageEdge.updated_at >= since,
            ),
        )
        await collect(
            "definition-version",
            select(
                MetadataRoutineDefinitionVersion.id,
                MetadataRoutineDefinitionVersion.routine_id,
                MetadataRoutineDefinitionVersion.captured_at,
            ).where(
                MetadataRoutineDefinitionVersion.organization_id == organization_id,
                MetadataRoutineDefinitionVersion.routine_id.in_(routine_ids),
                MetadataRoutineDefinitionVersion.captured_at >= since,
            ),
        )
    if tool_ids:
        await collect(
            "tool-version",
            select(
                GovernedToolVersion.id, GovernedToolVersion.id, GovernedToolVersion.updated_at
            ).where(
                GovernedToolVersion.organization_id == organization_id,
                GovernedToolVersion.id.in_(tool_ids),
                GovernedToolVersion.updated_at >= since,
            ),
        )
    return OkfMarks(marks=tuple(sorted(marks)))


def _document_digest(snapshot: OkfSnapshot) -> str:
    """The snapshot's identity as far as any *document* is concerned.

    Source read times are manifest-only (R11-OKF01 put them there precisely so a scan that
    changed nothing could not move a document), so they are left out here too: a revalidation
    that finds only newer read times is a no-op, publishes nothing and moves no hash -- the log
    included. The head's `validated_at` records that the check ran.
    """
    return replace(snapshot, freshness=()).content_digest()


# --- early refusal ----------------------------------------------------------------------


async def _refuse_oversized_scope(session: AsyncSession, version: ContextProductVersion) -> None:
    """Refuse a scope that cannot produce a bundle within limits, before loading it.

    The renderer's final byte and document limits stay as the last word; this is the first one,
    counted from the version's own pins and one column count, so an oversized product is refused
    without first materializing every column and document in memory.
    """
    subjects = (
        len(version.table_ids or [])
        + len(version.routine_ids or [])
        + len(version.eligible_tool_version_ids or [])
    )
    if subjects > MAX_SCOPE_SUBJECTS:
        raise OkfExportError(
            f"scope names {subjects} objects, routines and tools; the bundle limit allows "
            f"{MAX_SCOPE_SUBJECTS}. Refused before any document was built."
        )
    table_ids = _uuid_list(version.table_ids)
    if table_ids:
        columns = await session.scalar(
            select(func.count(MetadataColumn.id)).where(
                MetadataColumn.organization_id == version.organization_id,
                MetadataColumn.table_id.in_(table_ids),
            )
        )
        if int(columns or 0) > MAX_SCOPE_COLUMNS:
            raise OkfExportError(
                f"scope holds {int(columns or 0)} columns; the snapshot limit is "
                f"{MAX_SCOPE_COLUMNS}. Refused before any column was loaded."
            )


# --- history serialization --------------------------------------------------------------


def _entry_document(entry: OkfLogEntry) -> dict[str, Any]:
    return {
        "date": entry.date,
        "sequence": entry.sequence,
        "trigger": entry.trigger,
        "documents": entry.documents,
        "added": list(entry.added),
        "changed": list(entry.changed),
        "removed": list(entry.removed),
        "unchanged": entry.unchanged,
        "truncated": entry.truncated,
    }


def _entry_from_document(payload: dict[str, Any]) -> OkfLogEntry:
    return OkfLogEntry(
        date=str(payload["date"]),
        sequence=int(payload["sequence"]),
        trigger=str(payload["trigger"]),
        documents=int(payload.get("documents") or 0),
        added=tuple(payload.get("added") or ()),
        changed=tuple(payload.get("changed") or ()),
        removed=tuple(payload.get("removed") or ()),
        unchanged=int(payload.get("unchanged") or 0),
        truncated=bool(payload.get("truncated")),
    )


# --- reads ------------------------------------------------------------------------------


async def _head(
    session: AsyncSession, version: ContextProductVersion, digest: str
) -> OkfBundleHead | None:
    head: OkfBundleHead | None = await session.scalar(
        select(OkfBundleHead).where(
            OkfBundleHead.organization_id == version.organization_id,
            OkfBundleHead.context_product_version_id == version.id,
            OkfBundleHead.authority_digest == digest,
        )
    )
    return head


async def _publication(
    session: AsyncSession,
    version: ContextProductVersion,
    digest: str,
    publication_id: UUID,
) -> OkfBundlePublication | None:
    """A publication, only if it is in the caller's own lineage. Anything else reads as absent,
    which is the same answer a publication that never existed gives."""
    publication: OkfBundlePublication | None = await session.scalar(
        select(OkfBundlePublication).where(
            OkfBundlePublication.id == publication_id,
            OkfBundlePublication.organization_id == version.organization_id,
            OkfBundlePublication.context_product_version_id == version.id,
            OkfBundlePublication.authority_digest == digest,
        )
    )
    return publication


async def load_documents(
    session: AsyncSession, publication: OkfBundlePublication
) -> tuple[OkfBundleDocument, ...]:
    """Every document of one publication, or a refusal if the set is not whole.

    Publication is atomic, so an incomplete set cannot happen through this module; the check
    exists so that if it ever did, a reader gets an error rather than half a bundle.
    """
    rows = (
        await session.scalars(
            select(OkfBundleDocument)
            .where(
                OkfBundleDocument.organization_id == publication.organization_id,
                OkfBundleDocument.publication_id == publication.id,
            )
            .order_by(OkfBundleDocument.path)
        )
    ).all()
    if len(rows) != publication.document_count:
        raise HTTPException(status_code=409, detail="stored OKF bundle is incomplete")
    return tuple(rows)


async def load_document(
    session: AsyncSession, publication: OkfBundlePublication, path: str
) -> OkfBundleDocument | None:
    document: OkfBundleDocument | None = await session.scalar(
        select(OkfBundleDocument).where(
            OkfBundleDocument.organization_id == publication.organization_id,
            OkfBundleDocument.publication_id == publication.id,
            OkfBundleDocument.path == path,
        )
    )
    return document


async def load_documents_by_path(
    session: AsyncSession, publication: OkfBundlePublication, paths: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """Path -> (content, sha256) for the named documents of one publication, and no others.

    What lets question-specific context load a handful of documents rather than the bundle.
    A path the publication does not hold is simply absent from the result.
    """
    wanted = sorted(set(paths))
    if not wanted:
        return {}
    rows = (
        await session.scalars(
            select(OkfBundleDocument).where(
                OkfBundleDocument.organization_id == publication.organization_id,
                OkfBundleDocument.publication_id == publication.id,
                OkfBundleDocument.path.in_(wanted),
            )
        )
    ).all()
    return {row.path: (row.content, row.sha256) for row in rows}


@dataclass(frozen=True, slots=True)
class OkfStoredContext:
    """Question-specific context, and the stored publication it was selected from."""

    stored: OkfPublishedBundle
    context: OkfContext


async def read_okf_context(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
    settings: Settings,
    question: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    publication_id: UUID | None = None,
    now: datetime | None = None,
) -> OkfStoredContext:
    """The sections of the caller's stored publication a question needs (design §14 step 5).

    Through `read_published_bundle` first, so scope, admission and the lineage key are exactly
    every other door's -- a question cannot reach knowledge a manifest read would be refused.
    Then `aida.okf_context` ranks from the publication's own frozen snapshot, and only the
    ranked documents and one hop of their links are loaded.
    """
    stored = await read_published_bundle(
        session, version_id, context, settings, publication_id=publication_id, now=now
    )
    snapshot = snapshot_from_document(stored.publication.snapshot)
    plan = plan_context(snapshot, question)
    loaded = await load_documents_by_path(session, stored.publication, plan.paths)
    targets = hop_targets(plan, loaded)
    fetched = await load_documents_by_path(session, stored.publication, targets)
    # In the ranking's order, not the database's: citation ids follow document order, so the
    # same question over the same publication must number its sources the same way anywhere.
    hops = {path: fetched[path] for path in targets if path in fetched}
    return OkfStoredContext(
        stored=stored,
        context=assemble_context(plan, loaded, hops, max_chars=max_chars),
    )


def as_bundle(
    publication: OkfBundlePublication, documents: Sequence[OkfBundleDocument]
) -> OkfBundle:
    """The stored rows as the renderer's own value, so the archive and the policy check run on
    exactly what was stored rather than on a re-render."""
    return OkfBundle(
        documents=tuple(
            OkfDocument(path=row.path, text=row.content)
            for row in sorted(documents, key=lambda item: item.path)
        ),
        manifest=dict(publication.manifest),
    )


async def list_publications(
    session: AsyncSession, stored: OkfPublishedBundle
) -> list[OkfBundlePublication]:
    """The caller's lineage, newest first: every retained publication of the same authority."""
    return list(
        (
            await session.scalars(
                select(OkfBundlePublication)
                .where(
                    OkfBundlePublication.organization_id == stored.version.organization_id,
                    OkfBundlePublication.context_product_version_id == stored.version.id,
                    OkfBundlePublication.authority_digest == stored.authority_digest,
                )
                .order_by(OkfBundlePublication.sequence.desc())
            )
        ).all()
    )


async def read_published_bundle(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
    settings: Settings,
    *,
    publication_id: UUID | None = None,
    now: datetime | None = None,
) -> OkfPublishedBundle:
    """The one way any surface obtains an OKF bundle for a consumer.

    Order matters and is the whole design: the change marks are read first, before any content,
    so nothing the capture reads can predate them; then the compiler's own scope resolver decides
    whether the caller may consume the version at all; then the per-datasource admission decides
    the caller's lineage; only then is anything stored looked up. A pinned `publication_id` is
    served only from the caller's own lineage and never rebuilt.
    """
    clock = now or datetime.now(UTC)
    # The marks before anything else. `_load_source` repeats this get and the organization check
    # with its own error semantics; doing them here too costs one identity-map hit.
    pinned_version = await session.get(ContextProductVersion, version_id)
    if pinned_version is not None:
        enforce_organization(context, pinned_version.organization_id)
    marks_since = clock - MARK_LOOKBACK - OKF_REVALIDATE_AFTER
    marks = (
        await change_marks(session, pinned_version, since=marks_since)
        if pinned_version is not None
        else OkfMarks(())
    )
    (
        product,
        version,
        _tables,
        _negative,
        _exemplars,
        routines,
        views,
        ontology,
        freshness,
        quality_snapshot,
    ) = await _load_source(session, version_id, context)
    admitted = await admit_datasources(session, context, settings, product=product, version=version)
    digest = authority_digest(version, list(admitted))
    head = await _head(session, version, digest)

    if publication_id is not None:
        pinned = await _publication(session, version, digest, publication_id)
        if pinned is None or head is None:
            raise HTTPException(
                status_code=404,
                detail="OKF publication not found for this reader, or no longer retained",
            )
        return OkfPublishedBundle(
            publication=pinned,
            head=head,
            product=product,
            version=version,
            quality_snapshot=quality_snapshot,
            authority_digest=digest,
            is_current=pinned.id == head.publication_id,
            published_now=False,
        )

    current = await session.get(OkfBundlePublication, head.publication_id) if head else None
    trigger: str | None = None
    if head is None or current is None:
        trigger = TRIGGER_INITIAL
    else:
        prior_snapshot = snapshot_from_document(current.snapshot)
        renderer = (
            prior_snapshot.profile,
            prior_snapshot.profile_version,
            prior_snapshot.spec_revision,
        )
        window_known = _utc(head.marks_window_start) >= marks_since
        if renderer != (EXPORT_PROFILE, EXPORT_PROFILE_VERSION, OKF_SPEC_REVISION):
            trigger = TRIGGER_RENDERER_CHANGE
        elif window_known and marks.digest(_utc(head.marks_window_start)) != head.marks_digest:
            trigger = TRIGGER_SOURCE_CHANGE
        elif clock - _utc(head.validated_at) > OKF_REVALIDATE_AFTER:
            # Also the case where the head's window predates what was read: a head that old is
            # past its revalidation age by construction.
            trigger = TRIGGER_REVALIDATION
    if trigger is None and head is not None and current is not None:
        return OkfPublishedBundle(
            publication=current,
            head=head,
            product=product,
            version=version,
            quality_snapshot=quality_snapshot,
            authority_digest=digest,
            is_current=True,
            published_now=False,
        )

    try:
        await _refuse_oversized_scope(session, version)
    except OkfExportError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    window_start = clock - MARK_LOOKBACK
    before = marks.digest(window_start)
    snapshot = await freeze_snapshot(
        session,
        context,
        settings,
        product=product,
        version=version,
        routines=routines,
        views=views,
        ontology=ontology,
        freshness=freshness,
        captured_at=clock,
        admitted=admitted,
    )
    after_marks = await change_marks(session, version, since=window_start)
    if after_marks.digest(window_start) != before:
        # Something committed while the snapshot was being read. Publishing now could combine a
        # column list from one catalog state with a definition digest from the next.
        raise HTTPException(
            status_code=409,
            detail=(
                "the product's sources changed while the OKF snapshot was being captured; "
                "nothing was published. Retry to capture a consistent state."
            ),
        )

    if head is not None and current is not None and trigger in (
        TRIGGER_SOURCE_CHANGE,
        TRIGGER_REVALIDATION,
    ):
        prior_snapshot = snapshot_from_document(current.snapshot)
        if _document_digest(prior_snapshot) == _document_digest(snapshot):
            # A no-op: the marks moved or the head aged, and the content did not. OKF-C says a
            # no-op must reproduce every hash, and the surest way is to publish nothing at all.
            await session.execute(
                update(OkfBundleHead)
                .where(
                    OkfBundleHead.id == head.id,
                    OkfBundleHead.organization_id == version.organization_id,
                    OkfBundleHead.publication_id == current.id,
                )
                .values(
                    validated_at=clock,
                    marks_window_start=window_start,
                    marks_digest=after_marks.digest(window_start),
                )
            )
            head.validated_at = clock
            head.marks_window_start = window_start
            head.marks_digest = after_marks.digest(window_start)
            return OkfPublishedBundle(
                publication=current,
                head=head,
                product=product,
                version=version,
                quality_snapshot=quality_snapshot,
                authority_digest=digest,
                is_current=True,
                published_now=False,
            )

    assert trigger is not None
    signalled = sorted(after_marks.subjects_since(_utc(head.validated_at))) if head else []
    return await _publish(
        session,
        context=context,
        product=product,
        version=version,
        quality_snapshot=quality_snapshot,
        digest=digest,
        head=head,
        current=current,
        snapshot=snapshot,
        trigger=trigger,
        clock=clock,
        window_start=window_start,
        marks_digest=after_marks.digest(window_start),
        signalled=signalled,
    )


async def _publish(
    session: AsyncSession,
    *,
    context: SecurityContext,
    product: ContextProduct,
    version: ContextProductVersion,
    quality_snapshot: dict[str, object],
    digest: str,
    head: OkfBundleHead | None,
    current: OkfBundlePublication | None,
    snapshot: OkfSnapshot,
    trigger: str,
    clock: datetime,
    window_start: datetime,
    marks_digest: str,
    signalled: Sequence[str],
) -> OkfPublishedBundle:
    """Render incrementally against the stored head and publish atomically, or refuse."""
    prior_snapshot = snapshot_from_document(current.snapshot) if current is not None else None
    prior_rows = await load_documents(session, current) if current is not None else ()
    prior_documents = {row.path: row.content for row in prior_rows}
    prior_sequences = {row.path: row.rendered_in_sequence for row in prior_rows}
    prior_history = (
        tuple(_entry_from_document(item) for item in current.history or []) if current else ()
    )
    sequence = (current.sequence + 1) if current is not None else 1
    try:
        bundle, report, history = export_okf_bundle_incremental(
            snapshot,
            prior_snapshot=prior_snapshot,
            prior_documents=prior_documents,
            prior_history=prior_history,
            stamp=OkfPublicationStamp(
                date=clock.astimezone(UTC).date().isoformat(), sequence=sequence, trigger=trigger
            ),
        )
    except OkfExportError as error:
        # 409 with the reason and no object detail, as R11-OKF01's routes did.
        raise HTTPException(status_code=409, detail=str(error)) from error
    validation = validate_atlas_publish_policy(bundle)
    unstorable = sorted(
        finding
        for finding in validation.findings
        if finding.startswith(_UNSTORABLE_FINDINGS)
    )
    if unstorable:
        # INV-6 defence in depth: a fence means text that may be code. Never at rest.
        raise HTTPException(status_code=409, detail={"findings": unstorable})

    subjects = document_subjects(snapshot)
    carried = set(report.carried)
    publication = OkfBundlePublication(
        organization_id=version.organization_id,
        context_product_version_id=version.id,
        authority_digest=digest,
        sequence=sequence,
        trigger=trigger,
        captured_at=clock,
        snapshot=snapshot_to_document(snapshot),
        content_snapshot_digest=str(bundle.manifest["content_snapshot_digest"]),
        bundle_content_digest=bundle.content_digest,
        scope_digest=str(bundle.manifest["scope_digest"]),
        manifest=bundle.manifest,
        document_count=len(bundle.documents),
        rendered_count=len(report.rendered),
        carried_count=len(report.carried),
        change_summary={
            "added": list(report.added),
            "changed": list(report.changed),
            "removed": list(report.removed),
            # What this publication's builders actually ran on; everything else was carried.
            "rendered": list(report.rendered[:MAX_SUMMARY_PATHS]),
            "changed_subjects": list(report.changed_subjects),
            "marked_subjects": list(signalled),
            "full_render": report.full,
            "validation": {"valid": validation.valid, "findings": list(validation.findings)},
        },
        history=[_entry_document(entry) for entry in history],
        built_by=context.principal_id[:255],
    )
    try:
        async with session.begin_nested():
            session.add(publication)
            await session.flush()
            session.add_all(
                OkfBundleDocument(
                    organization_id=version.organization_id,
                    publication_id=publication.id,
                    path=document.path,
                    subject_key=subjects.get(document.path),
                    sha256=document.sha256,
                    byte_length=document.byte_length,
                    content=document.text,
                    # The first publication that produced these exact bytes: earlier for a
                    # carried document, and earlier too for one re-derived to identical bytes.
                    rendered_in_sequence=(
                        prior_sequences.get(document.path, sequence)
                        if document.path in carried
                        or prior_documents.get(document.path) == document.text
                        else sequence
                    ),
                )
                for document in bundle.documents
            )
            await session.flush()
            if head is None:
                head = OkfBundleHead(
                    organization_id=version.organization_id,
                    context_product_version_id=version.id,
                    authority_digest=digest,
                    publication_id=publication.id,
                    validated_at=clock,
                    marks_window_start=window_start,
                    marks_digest=marks_digest,
                )
                session.add(head)
                await session.flush()
            else:
                assert current is not None
                moved = await session.execute(
                    update(OkfBundleHead)
                    .where(
                        OkfBundleHead.id == head.id,
                        OkfBundleHead.organization_id == version.organization_id,
                        # Optimistic: only if nobody published over this head meanwhile.
                        OkfBundleHead.publication_id == current.id,
                    )
                    .values(
                        publication_id=publication.id,
                        validated_at=clock,
                        marks_window_start=window_start,
                        marks_digest=marks_digest,
                    )
                )
                if getattr(moved, "rowcount", 1) != 1:
                    raise _LostRace
                head.publication_id = publication.id
                head.validated_at = clock
                head.marks_window_start = window_start
                head.marks_digest = marks_digest
            await _prune(session, version, digest, keep_from=sequence - RETAINED_PUBLICATIONS + 1)
    except (IntegrityError, _LostRace):
        # A concurrent reader published this lineage first. Its publication is as current as
        # ours would have been; serve it rather than fail the read.
        winner = await _head(session, version, digest)
        published = (
            await session.get(OkfBundlePublication, winner.publication_id) if winner else None
        )
        if winner is None or published is None:
            raise HTTPException(
                status_code=409, detail="OKF bundle publication raced; retry"
            ) from None
        return OkfPublishedBundle(
            publication=published,
            head=winner,
            product=product,
            version=version,
            quality_snapshot=quality_snapshot,
            authority_digest=digest,
            is_current=True,
            published_now=False,
        )

    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="context_product.okf_bundle_publish",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "publication_id": str(publication.id),
            "sequence": sequence,
            "trigger": trigger,
            "bundle_content_digest": bundle.content_digest,
            "documents": len(bundle.documents),
            "rendered": len(report.rendered),
            "carried": len(report.carried),
            "changed": len(report.changed),
        },
    )
    logger.info(
        "okf_bundle_published",
        context_product_version_id=str(version.id),
        sequence=sequence,
        trigger=trigger,
        rendered=len(report.rendered),
        carried=len(report.carried),
    )
    return OkfPublishedBundle(
        publication=publication,
        head=head,
        product=product,
        version=version,
        quality_snapshot=quality_snapshot,
        authority_digest=digest,
        is_current=True,
        published_now=True,
    )


class _LostRace(Exception):
    """The optimistic head update matched no row: another publisher moved the head first."""


async def _prune(
    session: AsyncSession, version: ContextProductVersion, digest: str, *, keep_from: int
) -> None:
    """Drop publications more than `RETAINED_PUBLICATIONS` behind, documents first.

    Never the head's: the head always names the newest, and `keep_from` is at most its sequence.
    """
    if keep_from <= 1:
        return
    stale = (
        await session.scalars(
            select(OkfBundlePublication.id).where(
                OkfBundlePublication.organization_id == version.organization_id,
                OkfBundlePublication.context_product_version_id == version.id,
                OkfBundlePublication.authority_digest == digest,
                OkfBundlePublication.sequence < keep_from,
            )
        )
    ).all()
    if not stale:
        return
    await session.execute(
        delete(OkfBundleDocument).where(
            OkfBundleDocument.organization_id == version.organization_id,
            OkfBundleDocument.publication_id.in_(list(stale)),
        )
    )
    await session.execute(
        delete(OkfBundlePublication).where(
            OkfBundlePublication.organization_id == version.organization_id,
            OkfBundlePublication.id.in_(list(stale)),
        )
    )


# --- the catalog door -------------------------------------------------------------------


#: How many product bundles one catalog object view consults. Each is a full authorized read.
MAX_OBJECT_PRODUCTS: Final = 5


@dataclass(frozen=True, slots=True)
class OkfObjectKnowledge:
    stored: OkfPublishedBundle
    document: OkfBundleDocument


async def read_object_knowledge(
    session: AsyncSession,
    table_id: UUID,
    context: SecurityContext,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[OkfObjectKnowledge]:
    """The stored document about one catalog object, from each product bundle the caller may read.

    Every bundle goes through `read_published_bundle` -- the same scope resolver, admission and
    lineage key as every other door -- and a product the caller may not consume is skipped
    silently, exactly as its absence from a listing would be. The object's document is present
    only where its datasource was admitted for that reader; otherwise that bundle contributes
    nothing, not a placeholder.
    """
    row = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(MetadataTable.id == table_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="table not found")
    table, schema, catalog = row
    enforce_organization(context, table.organization_id)
    subject = object_key(str(table.datasource_id), catalog.name, schema.name, table.name)
    versions = (
        await session.scalars(
            select(ContextProductVersion)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(
                ContextProductVersion.organization_id == table.organization_id,
                ContextProduct.organization_id == table.organization_id,
                ContextProduct.lifecycle_status == "ACTIVE",
                ContextProductVersion.status.in_(("PUBLISHED", "SUPPORTED")),
            )
            .order_by(ContextProductVersion.published_at.desc().nulls_last())
            .limit(200)
        )
    ).all()
    found: list[OkfObjectKnowledge] = []
    for candidate in versions:
        if str(table_id) not in {str(value) for value in candidate.table_ids or []}:
            continue
        try:
            stored = await read_published_bundle(
                session, candidate.id, context, settings, now=now
            )
        except HTTPException:
            continue
        document = await session.scalar(
            select(OkfBundleDocument).where(
                OkfBundleDocument.organization_id == table.organization_id,
                OkfBundleDocument.publication_id == stored.publication.id,
                OkfBundleDocument.subject_key == subject,
            )
        )
        if document is not None:
            found.append(OkfObjectKnowledge(stored=stored, document=document))
        if len(found) >= MAX_OBJECT_PRODUCTS:
            break
    return found


# --- evidence ---------------------------------------------------------------------------


def record_okf_read(
    session: AsyncSession,
    context: SecurityContext,
    stored: OkfPublishedBundle,
    *,
    action: str,
    channel: str,
    path: str | None = None,
    sections: Sequence[str] = (),
) -> None:
    """One read of a stored bundle, recorded the same way whichever door it came through.

    Audit, outbox and -- for a PUBLISHED version -- a consumption edge on that version, the
    evidence R11-OKF01's routes already left, now naming the publication read so a reviewer can
    tell which stored bytes a consumer saw. Ids, digests and counts only. A context read also
    names the `path#anchor` of each section it handed out -- opaque bundle paths and heading
    slugs, never the question and never the text.
    """
    version = stored.version
    publication = stored.publication
    correlation_id = get_correlation_id()
    details: dict[str, Any] = {
        "publication_id": str(publication.id),
        "publication_sequence": publication.sequence,
        "bundle_content_digest": publication.bundle_content_digest,
        "content_snapshot_digest": publication.content_snapshot_digest,
        "documents": publication.document_count,
        "is_current": stored.is_current,
    }
    if path is not None:
        details["path"] = path
    if sections:
        details["section_count"] = len(sections)
        details["sections"] = list(sections)[:MAX_AUDITED_SECTIONS]
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action=action,
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details=details,
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version.id),
        event_type="context.okf_bundle_exported.v1",
        payload={
            "bundle_content_digest": publication.bundle_content_digest,
            "documents": publication.document_count,
            "channel": channel,
            "publication_id": str(publication.id),
        },
    )
    if version.status == "PUBLISHED":
        session.add(
            ContextProductConsumptionEdge(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel=channel,
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=stored.quality_snapshot,
            )
        )
