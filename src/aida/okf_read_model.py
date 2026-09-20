"""What a reader of a stored OKF bundle is told, shared by every door (R11-GQL01, R11-OKF02).

The REST routes (`aida.okf_export_api`), the MCP tools and resources (`aida.mcp_server`) and the
GraphQL reads (`aida.graphql_okf`) all describe a stored publication the same way. Those
descriptions -- the role set that may read a bundle, and the mappings from a stored publication,
document and manifest to the API's read models -- lived beside the routes, in a router module,
which GraphQL may not import. They are moved here unchanged and `okf_export_api` re-imports every
name, so its routes and their callers are untouched.

Nothing here reads, freezes, renders or stores anything: every function takes what
`aida.okf_store` already returned. The gates are the store's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aida.okf_export import OKF_CONFORMANCE_STATUS
from aida.okf_store import OkfPublishedBundle, OkfPublishedSourceBundle
from aida.okf_store_models import OkfBundleDocument, OkfBundlePublication
from aida.schemas import (
    OkfBundleFileRead,
    OkfBundleRead,
    OkfChangeSummaryRead,
    OkfDocumentRead,
    OkfPublicationRead,
)

__all__ = [
    "OKF_ROLES",
    "BundleSummary",
    "bundle_read",
    "bundle_summary",
    "document_read",
    "publication_read",
]

#: The same role set the context compiler's own read uses. A role here is necessary and never
#: sufficient: the capability envelope, the version's consumer roles, the purpose and quality
#: gates and the per-datasource authorization decision all still apply below.
OKF_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataProductOwner",
    "DataSteward",
    "AgentDeveloper",
    "Analyst",
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
