"""What a reader of a stored OKF bundle is told, shared by every door (R11-GQL01, R11-OKF02).

The REST routes (`aida.okf_export_api`), the MCP tools and resources (`aida.mcp_server`) and the
GraphQL reads (`aida.graphql_okf`) all describe a stored publication the same way. Those
descriptions -- the role set that may read a bundle, and the mappings from a stored publication,
document and manifest to the API's read models -- lived beside the routes, in a router module,
which GraphQL may not import. They are moved here unchanged and `okf_export_api` re-imports every
name, so its routes and their callers are untouched.

Nothing here reads, freezes, renders or stores anything: every function takes what
`aida.okf_store` already returned. The gates are the store's.

R11-OKF02 adds the Catalog object view's two mappers, `object_coverage` (a product entry's and a
source entry's coverage row, one rule) and `object_source_read` (what a datasource's own bundle
says about one object), for the same reason: a second door must not restate them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aida.okf_export import OKF_CONFORMANCE_STATUS
from aida.okf_store import (
    SOURCE_NOT_IN_BUNDLE,
    SOURCE_REFUSED,
    OkfObjectSourceKnowledge,
    OkfPublishedBundle,
    OkfPublishedSourceBundle,
)
from aida.okf_store_models import OkfBundleDocument, OkfBundlePublication
from aida.schemas import (
    OkfBundleFileRead,
    OkfBundleRead,
    OkfChangeSummaryRead,
    OkfDocumentRead,
    OkfObjectSourceRead,
    OkfPublicationRead,
)

__all__ = [
    "OKF_ROLES",
    "BundleSummary",
    "bundle_read",
    "bundle_summary",
    "document_read",
    "object_coverage",
    "object_source_read",
    "publication_read",
]

#: The same role set the context compiler's own read uses. A role here is necessary and never
#: sufficient: the capability envelope, the version's consumer roles, the purpose and quality
#: gates and the per-datasource authorization decision all still apply below.
OKF_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataSteward",
    "AgentDeveloper",
    "Analyst",
)

#: R11-OKF02, decided 2026-09-25: the one-object read (the Catalog's Knowledge section and its
#: MCP and GraphQL twins) admits every role that reads the same object's evidence and
#: documentation beside it -- the governance readers who review and approve that very meaning.
#: A knowledge document holds approved metadata and no source value, and the datasource's own
#: `READ_METADATA` decision and every product's consumer, purpose and quality gates still apply
#: inside the store. Whole-bundle manifests, downloads, history and question context stay at
#: `OKF_ROLES`: a bulk export is a different act from reading one object's page.
OBJECT_KNOWLEDGE_ROLES = (
    *OKF_ROLES,
    "Auditor",
    "DataAdmin",
    "Reviewer",
    "SemanticAdmin",
    "Viewer",
)


def publication_read(
    publication: OkfBundlePublication, *, is_current: bool
) -> OkfPublicationRead:
    """One stored publication as the API describes it. Shared with the MCP reader."""
    summary: dict[str, Any] = dict(publication.change_summary or {})
    verdict: dict[str, Any] = dict(summary.get("validation") or {})
    return OkfPublicationRead(
        publication_id=publication.id,
        sequence=publication.sequence,
        trigger=publication.trigger,
        captured_at=publication.captured_at,
        is_current=is_current,
        bundle_content_digest=publication.bundle_content_digest,
        content_snapshot_digest=publication.content_snapshot_digest,
        document_count=publication.document_count,
        rendered_count=publication.rendered_count,
        carried_count=publication.carried_count,
        valid=bool(verdict.get("valid", False)),
        changes=OkfChangeSummaryRead(
            added=[str(item) for item in summary.get("added") or []],
            changed=[str(item) for item in summary.get("changed") or []],
            removed=[str(item) for item in summary.get("removed") or []],
            changed_subjects=len(summary.get("changed_subjects") or []),
            marked_subjects=len(summary.get("marked_subjects") or []),
            full_render=bool(summary.get("full_render", False)),
        ),
    )


def document_read(
    publication: OkfBundlePublication, document: OkfBundleDocument
) -> OkfDocumentRead:
    return OkfDocumentRead(
        publication_id=publication.id,
        publication_sequence=publication.sequence,
        path=document.path,
        sha256=document.sha256,
        bytes=document.byte_length,
        rendered_in_sequence=document.rendered_in_sequence,
        subject_key=document.subject_key,
        content=document.content,
    )


def object_coverage(
    publication: OkfBundlePublication, subject_key: str | None
) -> dict[str, Any]:
    """The manifest's value-free `source_objects` row for one subject: definition digest and
    capture version, description state and version. Empty when the manifest lists no such
    subject. One rule for a product bundle's entry and a source bundle's."""
    manifest: dict[str, Any] = dict(publication.manifest)
    return next(
        (
            dict(row)
            for row in manifest.get("source_objects") or []
            if row.get("key") == subject_key
        ),
        {},
    )


def object_source_read(found: OkfObjectSourceKnowledge) -> OkfObjectSourceRead:
    """A datasource's own bundle's answer about one object, carrying only what its state may.

    A refusal names no bundle; an absence names the datasource the reader was admitted to and
    nothing about the publication, so `NOT_IN_BUNDLE` cannot be used to count what was left out.
    """
    if found.state == SOURCE_REFUSED:
        return OkfObjectSourceRead(state="REFUSED", reason=found.reason)
    stored = found.stored
    if stored is None:
        raise ValueError(f"{found.state} needs the stored source bundle it was read from")
    datasource = stored.datasource
    if found.state == SOURCE_NOT_IN_BUNDLE or found.document is None:
        return OkfObjectSourceRead(
            state="NOT_IN_BUNDLE", datasource_id=datasource.id, datasource_name=datasource.name
        )
    return OkfObjectSourceRead(
        state="DOCUMENT",
        datasource_id=datasource.id,
        datasource_name=datasource.name,
        publication=publication_read(stored.publication, is_current=stored.is_current),
        document=document_read(stored.publication, found.document),
        coverage=object_coverage(stored.publication, found.document.subject_key),
    )


@dataclass(frozen=True, slots=True)
class BundleSummary:
    """A stored publication's manifest, without its file index: the scalars, the counts, the
    validation verdict and the publication. What a reader who does not want every document's
    path (GraphQL pages them) is told, derived once for every door."""

    okf_version: str
    spec_revision: str
    spec_conformance: str
    profile: str
    content_snapshot_digest: str
    bundle_content_digest: str
    scope_digest: str
    document_count: int
    valid: bool
    findings: list[str]
    counts: dict[str, int]
    publication: OkfPublicationRead
    validated_at: datetime


def bundle_summary(stored: OkfPublishedBundle | OkfPublishedSourceBundle) -> BundleSummary:
    """The manifest view of a stored publication, less its files. Built from stored columns
    only -- no document body is loaded to answer it."""
    publication = stored.publication
    manifest: dict[str, Any] = dict(publication.manifest)
    validation = stored.validation
    return BundleSummary(
        okf_version=str(manifest["okf_version"]),
        spec_revision=str(manifest["specification"]["revision"]),
        spec_conformance=OKF_CONFORMANCE_STATUS,
        profile=f"{manifest['compiler']['profile']}/{manifest['compiler']['profile_version']}",
        content_snapshot_digest=publication.content_snapshot_digest,
        bundle_content_digest=publication.bundle_content_digest,
        scope_digest=publication.scope_digest,
        document_count=publication.document_count,
        valid=validation.valid,
        findings=list(validation.findings),
        counts={
            str(name): int(value) for name, value in dict(manifest.get("counts") or {}).items()
        },
        publication=publication_read(publication, is_current=stored.is_current),
        validated_at=stored.head.validated_at,
    )


def bundle_read(stored: OkfPublishedBundle | OkfPublishedSourceBundle) -> OkfBundleRead:
    """The manifest view of a stored publication. Built from stored columns only -- no document
    body is loaded to answer it."""
    summary = bundle_summary(stored)
    manifest: dict[str, Any] = dict(stored.publication.manifest)
    return OkfBundleRead(
        okf_version=summary.okf_version,
        spec_revision=summary.spec_revision,
        spec_conformance=summary.spec_conformance,
        profile=summary.profile,
        content_snapshot_digest=summary.content_snapshot_digest,
        bundle_content_digest=summary.bundle_content_digest,
        scope_digest=summary.scope_digest,
        document_count=summary.document_count,
        valid=summary.valid,
        findings=summary.findings,
        files=[OkfBundleFileRead(**entry) for entry in manifest.get("files") or []],
        manifest=manifest,
        publication=summary.publication,
        validated_at=summary.validated_at,
    )
