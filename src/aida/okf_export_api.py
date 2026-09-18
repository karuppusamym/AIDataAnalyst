"""R11-OKF01/OKF02: the REST doors onto a stored OKF bundle.

Inspect its manifest, download it, read one document, list its publications, and -- from the
catalog -- read the document about one object. All read one context product version, all gated
the same way, and none touches the existing single-file compiler contracts. The design's
instruction on delivery is explicit: "Add an OKF bundle target beside existing context compiler
targets. Keep single-file compile responses compatible: use a separate bundle job/download
contract if necessary rather than putting a ZIP into a text-content field." So
`ContextCompilerTarget` is not extended, `ContextCompilationRead` is not touched, and an archive
is returned by its own route with its own media type.

**Scope resolution is the compiler's, not a second copy.** Every handler reaches
`context_compiler_api._load_source` (through the store), which is where the capability
envelope, the published/consumer-role check, the purpose gate, the quality gate and the resolved
routine/view/ontology/freshness references already live. The design requires exactly this --
"Source/object preview and product export must reuse the compiler's scope resolver" -- and a
second resolver would be a second place for a governance check to go missing.
`aida.okf_snapshot.admit_datasources` then adds the per-datasource admission decision.

**What each route records.** A bundle read is a consumption of governed context, so every
route audits and, for a PUBLISHED version, writes a `ContextProductConsumptionEdge` with its own
channel -- the same evidence the compiler's read and download already leave, on the same
version, so a reviewer sees one timeline rather than two.

**R11-OKF02: every route reads the stored bundle.** Up to OKF01 each route froze and rendered
its own bundle, so the manifest a caller inspected and the archive it downloaded next were two
captures that could disagree. Now every handler here -- and the MCP resource reader, and the
catalog object view -- obtains its bundle from `aida.okf_store.read_published_bundle`, which
resolves scope and authority on the request and then serves (or incrementally rebuilds and
atomically publishes) the one stored publication for the caller's lineage. A manifest names its
`publication_id`; a download or document read given that id returns exactly those stored bytes,
even after a newer publication, for as long as it is retained.

**Question-specific context.** `POST .../okf-bundle/context` is how a reader that has a
question -- rather than a path -- takes the few sections of the bundle it needs, with exact
receipts (`aida.okf_context`). It reads through the same store function as every other door.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.okf_context import citation_ids, render_markdown
from aida.okf_export import OKF_CONFORMANCE_STATUS, bundle_archive_bytes
from aida.okf_store import (
    BUNDLE_ROLE_CHANNELS,
    OkfPublishedBundle,
    OkfStoredContext,
    as_bundle,
    list_publications,
    load_document,
    load_documents,
    read_object_knowledge,
    read_okf_context,
    read_published_bundle,
    record_okf_read,
)
from aida.okf_store_models import OkfBundleDocument, OkfBundlePublication
from aida.schemas import (
    OkfBundleFileRead,
    OkfBundleRead,
    OkfChangeSummaryRead,
    OkfContextDocumentRead,
    OkfContextOmissionRead,
    OkfContextRead,
    OkfContextRequest,
    OkfContextSectionRead,
    OkfDocumentRead,
    OkfObjectKnowledgeItemRead,
    OkfObjectKnowledgeRead,
    OkfPublicationHistoryRead,
    OkfPublicationRead,
)
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["okf-export"])

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

_PINNED_READ = (
    "Read this stored publication of the caller's own lineage instead of the current one. "
    "Refused as not found once it is no longer retained."
)
_PINNED_DOWNLOAD = (
    "Download this stored publication -- the one a manifest named -- rather than the current "
    "one, so an inspected manifest and its archive are the same bytes."
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


def bundle_read(stored: OkfPublishedBundle) -> OkfBundleRead:
    """The manifest view of a stored publication. Built from stored columns only -- no document
    body is loaded to answer it."""
    publication = stored.publication
    manifest: dict[str, Any] = dict(publication.manifest)
    validation = stored.validation
    return OkfBundleRead(
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
        files=[OkfBundleFileRead(**entry) for entry in manifest.get("files") or []],
        manifest=manifest,
        publication=publication_read(publication, is_current=stored.is_current),
        validated_at=stored.head.validated_at,
    )


def context_read(found: OkfStoredContext) -> OkfContextRead:
    """Question-specific context as the API describes it. Shared with the MCP knowledge tool."""
    stored, selected = found.stored, found.context
    ids = citation_ids(selected)
    return OkfContextRead(
        context_product_version_id=stored.version.id,
        product_key=stored.product.product_key,
        product_version=stored.version.version,
        publication=publication_read(stored.publication, is_current=stored.is_current),
        status=selected.status,
        question_terms=list(selected.question_terms),
        documents=[
            OkfContextDocumentRead(
                citation=ids[document.path],
                path=document.path,
                sha256=document.sha256,
                type=document.type,
                title=document.title,
                status=document.status,
                description=document.description,
                hop=document.hop,
                score=document.score,
                matched_terms=list(document.matched_terms),
                linked_from=document.linked_from,
                approved_statements=list(document.approved),
                derived_statements=list(document.derived),
                sections=[
                    OkfContextSectionRead(
                        anchor=section.anchor,
                        heading=section.heading,
                        text=section.text,
                        rows_shown=section.rows_shown,
                        rows_total=section.rows_total,
                    )
                    for section in document.sections
                ],
            )
            for document in selected.documents
        ],
        omitted=[
            OkfContextOmissionRead(
                path=item.path, anchor=item.anchor, reason=item.reason, chars=item.chars
            )
            for item in selected.omitted
        ],
        omitted_count=selected.omitted_count,
        ambiguous=list(selected.ambiguous),
        max_chars=selected.max_chars,
        used_chars=selected.used_chars,
        guidance=selected.guidance,
        markdown=render_markdown(
            selected, product=f"{stored.product.product_key} v{stored.version.version}"
        ),
    )


@router.post(
    "/context-product-versions/{version_id}/okf-bundle/context",
    response_model=OkfContextRead,
)
async def select_okf_context(
    version_id: UUID,
    payload: OkfContextRequest,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfContextRead:
    """The sections of the stored bundle a question needs, with exact receipts (OKF-E).

    A read, sent as POST so the question travels in a body rather than a URL. `NO_MATCH` is a
    successful answer -- the bundle holds nothing on this question -- not an error. The audit
    record names each section handed out by path and anchor; it never carries the question.
    """
    found = await read_okf_context(
        session,
        version_id,
        context,
        settings,
        payload.question,
        max_chars=payload.max_chars or settings.okf_context_default_max_chars,
        publication_id=payload.publication_id,
    )
    record_okf_read(
        session,
        context,
        found.stored,
        action="context_product.okf_context_read",
        channel=BUNDLE_ROLE_CHANNELS["context"],
        sections=found.context.receipts(),
    )
    read = context_read(found)
    await session.commit()
    return read


@router.get(
    "/context-product-versions/{version_id}/okf-bundle",
    response_model=OkfBundleRead,
)
async def inspect_okf_bundle(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    publication_id: Annotated[UUID | None, Query(description=_PINNED_READ)] = None,
) -> OkfBundleRead:
    """The stored bundle's manifest and file index, without moving the documents.

    Returns the validation verdict alongside it rather than refusing an invalid bundle: the
    findings are how a caller learns *why* an export would not publish, and
    `validate_atlas_publish_policy` is strictly stronger than OKF conformance, so a `valid:
    false` here is an Atlas policy answer and not a statement that the bundle is unreadable.
    The `publication.publication_id` returned is what a caller passes to the download to get
    exactly these bytes.
    """
    stored = await read_published_bundle(
        session, version_id, context, settings, publication_id=publication_id
    )
    record_okf_read(
        session,
        context,
        stored,
        action="context_product.okf_bundle_inspect",
        channel=BUNDLE_ROLE_CHANNELS["manifest"],
    )
    read = bundle_read(stored)
    await session.commit()
    return read


@router.get("/context-product-versions/{version_id}/okf-bundle/download")
async def download_okf_bundle(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    publication_id: Annotated[UUID | None, Query(description=_PINNED_DOWNLOAD)] = None,
) -> Response:
    """The stored bundle as a deterministic archive, refused unless it satisfies the policy.

    Stricter than the manifest route on purpose. Inspecting findings is how a caller diagnoses
    an export; handing out a file is publication, and a file cannot be recalled -- "a downloaded
    file cannot be remotely revoked". So the download refuses what the manifest is willing to
    describe. The archive is built from the stored document rows, never re-rendered.
    """
    stored = await read_published_bundle(
        session, version_id, context, settings, publication_id=publication_id
    )
    validation = stored.validation
    if not validation.valid:
        raise HTTPException(status_code=409, detail={"findings": list(validation.findings)})
    publication = stored.publication
    bundle = as_bundle(publication, await load_documents(session, publication))
    archive = bundle_archive_bytes(bundle)
    record_okf_read(
        session,
        context,
        stored,
        action="context_product.okf_bundle_download",
        channel=BUNDLE_ROLE_CHANNELS["download"],
    )
    product_key = stored.product.product_key
    product_version = stored.version.version
    await session.commit()
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{product_key}-{product_version}-okf-bundle.zip"'
            ),
            "X-Atlas-Bundle-Content-SHA256": publication.bundle_content_digest,
            "X-Atlas-Content-Snapshot-SHA256": publication.content_snapshot_digest,
            "X-Atlas-OKF-Spec-Revision": str(bundle.manifest["specification"]["revision"]),
            "X-Atlas-OKF-Publication-Id": str(publication.id),
            "X-Atlas-OKF-Publication-Sequence": str(publication.sequence),
        },
    )


@router.get(
    "/context-product-versions/{version_id}/okf-bundle/document",
    response_model=OkfDocumentRead,
)
async def read_okf_document(
    version_id: UUID,
    path: Annotated[
        str, Query(min_length=1, max_length=512, description="Bundle-relative document path")
    ],
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    publication_id: Annotated[UUID | None, Query(description=_PINNED_READ)] = None,
) -> OkfDocumentRead:
    """One stored document -- the wiki view of a bundle, page by page.

    The path is looked up among the stored document rows of the caller's own publication, so a
    path that is not in the bundle, or is in a bundle of another lineage, reads as not found.
    """
    stored = await read_published_bundle(
        session, version_id, context, settings, publication_id=publication_id
    )
    document = await load_document(session, stored.publication, path.lstrip("/"))
    if document is None:
        raise HTTPException(status_code=404, detail="document not found in this bundle")
    record_okf_read(
        session,
        context,
        stored,
        action="context_product.okf_document_read",
        channel=BUNDLE_ROLE_CHANNELS["document"],
        path=document.path,
    )
    read = document_read(stored.publication, document)
    await session.commit()
    return read


@router.get(
    "/context-product-versions/{version_id}/okf-bundle/publications",
    response_model=OkfPublicationHistoryRead,
)
async def list_okf_publications(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfPublicationHistoryRead:
    """The caller's lineage of stored publications -- what changed, when, and why.

    Only the caller's own lineage: a publication built under another authority (a wider grant,
    a different admitted source set) is neither listed nor counted.
    """
    stored = await read_published_bundle(session, version_id, context, settings)
    items = [
        publication_read(item, is_current=item.id == stored.head.publication_id)
        for item in await list_publications(session, stored)
    ]
    record_okf_read(
        session,
        context,
        stored,
        action="context_product.okf_publications_read",
        channel=BUNDLE_ROLE_CHANNELS["history"],
    )
    await session.commit()
    return OkfPublicationHistoryRead(context_product_version_id=version_id, items=items)


@router.get(
    "/metadata/tables/{table_id}/okf-knowledge",
    response_model=OkfObjectKnowledgeRead,
)
async def read_object_okf_knowledge(
    table_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfObjectKnowledgeRead:
    """The Catalog door: this object's document from each product bundle the caller may read.

    Each bundle is read through the same store, scope resolver and admission as the product
    routes; a product the caller may not consume, or whose bundle does not admit this object's
    datasource for them, contributes nothing -- not an empty entry, not a count.
    """
    found = await read_object_knowledge(session, table_id, context, settings)
    items: list[OkfObjectKnowledgeItemRead] = []
    for entry in found:
        stored = entry.stored
        manifest: dict[str, Any] = dict(stored.publication.manifest)
        coverage = next(
            (
                dict(row)
                for row in manifest.get("source_objects") or []
                if row.get("key") == entry.document.subject_key
            ),
            {},
        )
        items.append(
            OkfObjectKnowledgeItemRead(
                context_product_version_id=stored.version.id,
                product_key=stored.product.product_key,
                product_version=stored.version.version,
                product_name=stored.version.name,
                publication=publication_read(stored.publication, is_current=stored.is_current),
                document=document_read(stored.publication, entry.document),
                coverage=coverage,
            )
        )
        record_okf_read(
            session,
            context,
            stored,
            action="context_product.okf_object_read",
            channel=BUNDLE_ROLE_CHANNELS["object"],
            path=entry.document.path,
        )
    await session.commit()
    return OkfObjectKnowledgeRead(table_id=table_id, items=items)
