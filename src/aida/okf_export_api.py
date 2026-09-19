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

**Source bundles (R11-OKF02).** `/v1/datasources/{datasource_id}/okf-bundle...` are the same five
doors -- manifest, download, document, publications, question context -- onto one datasource's
bundle of discovered, authorized objects (design section 14). They read through
`aida.okf_store.read_published_source_bundle`, which takes the datasource's `READ_METADATA`
decision on every request and then applies the product bundle's own store rules, and they
answer with the product routes' own response shapes wherever the shape does not name a product.
A refused datasource is a 403 with the bare reason code, as the catalog's read of it is.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.okf_context import OkfContext, citation_ids, render_markdown
from aida.okf_export import OKF_CONFORMANCE_STATUS, OkfBundle, bundle_archive_bytes
from aida.okf_store import (
    BUNDLE_ROLE_CHANNELS,
    SOURCE_BUNDLE_CHANNELS,
    OkfPublishedBundle,
    OkfPublishedSourceBundle,
    OkfStoredContext,
    OkfStoredSourceContext,
    as_bundle,
    list_publications,
    load_document,
    load_documents,
    read_object_knowledge,
    read_okf_context,
    read_okf_source_context,
    read_published_bundle,
    read_published_source_bundle,
    record_okf_read,
    record_okf_source_read,
)
from aida.okf_store_models import OkfBundleDocument, OkfBundlePublication
from aida.schemas import (
    ApiModel,
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


class OkfSourcePublicationHistoryRead(ApiModel):
    """R11-OKF02: the reader's own lineage of stored publications for one datasource's bundle,
    newest first. Only the lineage this request's read decision computes: a publication built
    under another admission is neither listed nor counted."""

    datasource_id: UUID
    items: list[OkfPublicationRead]


class OkfSourceContextRead(ApiModel):
    """R11-OKF02: question-specific context from one datasource's stored OKF bundle.

    The product context read's fields, with the datasource in place of the product. A source
    bundle holds no business concepts and no tools, so a question about meaning or a current
    figure is better asked of a context product; `NO_MATCH` is still an answer, not an error.
    """

    datasource_id: UUID
    datasource_name: str
    publication: OkfPublicationRead
    status: str
    question_terms: list[str]
    documents: list[OkfContextDocumentRead]
    omitted: list[OkfContextOmissionRead]
    omitted_count: int
    ambiguous: list[str]
    max_chars: int
    used_chars: int
    guidance: str
    markdown: str


def bundle_read(stored: OkfPublishedBundle | OkfPublishedSourceBundle) -> OkfBundleRead:
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
    stored = found.stored
    return OkfContextRead(
        context_product_version_id=stored.version.id,
        product_key=stored.product.product_key,
        product_version=stored.version.version,
        publication=publication_read(stored.publication, is_current=stored.is_current),
        **_selection_fields(
            found.context,
            label=f"{stored.product.product_key} v{stored.version.version}",
        ),
    )


def source_context_read(found: OkfStoredSourceContext) -> OkfSourceContextRead:
    """R11-OKF02: question-specific context from a source bundle, built by the same field
    mapping as a product's so the two answers cannot describe one selection differently."""
    stored = found.stored
    return OkfSourceContextRead(
        datasource_id=stored.datasource.id,
        datasource_name=stored.datasource.name,
        publication=publication_read(stored.publication, is_current=stored.is_current),
        **_selection_fields(found.context, label=f"data source {stored.datasource.name}"),
    )


def _selection_fields(selected: OkfContext, *, label: str) -> dict[str, Any]:
    """The part of a context answer that is the selection itself, whichever bundle it is of."""
    ids = citation_ids(selected)
    return dict(
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
        markdown=render_markdown(selected, product=label),
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
    bundle = await _publishable_bundle(session, stored)
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
    return _archive_response(
        stored.publication, bundle, filename=f"{product_key}-{product_version}-okf-bundle.zip"
    )


async def _publishable_bundle(
    session: AsyncSession, stored: OkfPublishedBundle | OkfPublishedSourceBundle
) -> OkfBundle:
    """The stored rows of a publication that may be handed out, or the 409 that says why not.
    One rule for every download door: a file cannot be recalled, so policy findings refuse it."""
    validation = stored.validation
    if not validation.valid:
        raise HTTPException(status_code=409, detail={"findings": list(validation.findings)})
    return as_bundle(stored.publication, await load_documents(session, stored.publication))


def _archive_response(
    publication: OkfBundlePublication, bundle: OkfBundle, *, filename: str
) -> Response:
    """The archive of exactly the stored bytes, with the digests a caller compares it against."""
    return Response(
        content=bundle_archive_bytes(bundle),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
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


# --- source bundles (R11-OKF02) ------------------------------------------------------------


@router.get("/datasources/{datasource_id}/okf-bundle", response_model=OkfBundleRead)
async def inspect_source_okf_bundle(
    datasource_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    publication_id: Annotated[UUID | None, Query(description=_PINNED_READ)] = None,
) -> OkfBundleRead:
    """One datasource's stored bundle: manifest, file index and verdict, no document bodies.

    The same contract as a product's manifest route. Every object in it is one the reader's
    `READ_METADATA` decision on this datasource -- and on each schema, where a workspace
    decides -- admitted; nothing else is named or counted.
    """
    stored = await read_published_source_bundle(
        session, datasource_id, context, settings, publication_id=publication_id
    )
    record_okf_source_read(
        session,
        context,
        stored,
        action="datasource.okf_bundle_inspect",
        channel=SOURCE_BUNDLE_CHANNELS["manifest"],
    )
    read = bundle_read(stored)
    await session.commit()
    return read


@router.get("/datasources/{datasource_id}/okf-bundle/download")
async def download_source_okf_bundle(
    datasource_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    publication_id: Annotated[UUID | None, Query(description=_PINNED_DOWNLOAD)] = None,
) -> Response:
    """One datasource's stored bundle as a deterministic archive, under the product download's
    rule: refused unless it satisfies the publish policy, built from the stored rows. Named by
    the datasource id rather than its name, which is catalog text and has no place in a header.
    """
    stored = await read_published_source_bundle(
        session, datasource_id, context, settings, publication_id=publication_id
    )
    bundle = await _publishable_bundle(session, stored)
    record_okf_source_read(
        session,
        context,
        stored,
        action="datasource.okf_bundle_download",
        channel=SOURCE_BUNDLE_CHANNELS["download"],
    )
    await session.commit()
    return _archive_response(
        stored.publication, bundle, filename=f"datasource-{datasource_id}-okf-bundle.zip"
    )


@router.get("/datasources/{datasource_id}/okf-bundle/document", response_model=OkfDocumentRead)
async def read_source_okf_document(
    datasource_id: UUID,
    path: Annotated[
        str, Query(min_length=1, max_length=512, description="Bundle-relative document path")
    ],
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    publication_id: Annotated[UUID | None, Query(description=_PINNED_READ)] = None,
) -> OkfDocumentRead:
    """One stored document of a datasource's bundle, looked up only among the rows of the
    caller's own publication -- a path in another lineage reads as not found."""
    stored = await read_published_source_bundle(
        session, datasource_id, context, settings, publication_id=publication_id
    )
    document = await load_document(session, stored.publication, path.lstrip("/"))
    if document is None:
        raise HTTPException(status_code=404, detail="document not found in this bundle")
    record_okf_source_read(
        session,
        context,
        stored,
        action="datasource.okf_document_read",
        channel=SOURCE_BUNDLE_CHANNELS["document"],
        path=document.path,
    )
    read = document_read(stored.publication, document)
    await session.commit()
    return read


@router.get(
    "/datasources/{datasource_id}/okf-bundle/publications",
    response_model=OkfSourcePublicationHistoryRead,
)
async def list_source_okf_publications(
    datasource_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfSourcePublicationHistoryRead:
    """The caller's lineage of stored publications for one datasource -- what changed, when."""
    stored = await read_published_source_bundle(session, datasource_id, context, settings)
    items = [
        publication_read(item, is_current=item.id == stored.head.publication_id)
        for item in await list_publications(session, stored)
    ]
    record_okf_source_read(
        session,
        context,
        stored,
        action="datasource.okf_publications_read",
        channel=SOURCE_BUNDLE_CHANNELS["history"],
    )
    await session.commit()
    return OkfSourcePublicationHistoryRead(datasource_id=datasource_id, items=items)


@router.post(
    "/datasources/{datasource_id}/okf-bundle/context", response_model=OkfSourceContextRead
)
async def select_source_okf_context(
    datasource_id: UUID,
    payload: OkfContextRequest,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfSourceContextRead:
    """The sections of one datasource's stored bundle a question needs, with receipts.

    The product context route's contract: a POST so the question stays out of URLs, `NO_MATCH`
    as an answer, and an audit record naming section anchors but never the question.
    """
    found = await read_okf_source_context(
        session,
        datasource_id,
        context,
        settings,
        payload.question,
        max_chars=payload.max_chars or settings.okf_context_default_max_chars,
        publication_id=payload.publication_id,
    )
    record_okf_source_read(
        session,
        context,
        found.stored,
        action="datasource.okf_context_read",
        channel=SOURCE_BUNDLE_CHANNELS["context"],
        sections=found.context.receipts(),
    )
    read = source_context_read(found)
    await session.commit()
    return read


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
