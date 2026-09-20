"""Stored OKF bundles over GraphQL (R11-GQL01, R11-OKF02; design sections 13 and 14).

Read-only fields over the *stored* publication REST already serves -- a context product
version's bundle and a datasource's -- and nothing else:

===========================================  =================================================
GraphQL field                                REST route it answers for
===========================================  =================================================
``contextProductOkfBundle(versionId)``       ``GET /v1/context-product-versions/{id}/okf-bundle``
``datasourceOkfBundle(datasourceId)``        ``GET /v1/datasources/{id}/okf-bundle``
``OkfBundle.documents``                      the manifest's file index, a page at a time
``OkfBundle.document(path)``                 ``GET .../okf-bundle/document?path=``
``OkfBundle.publications``                   ``GET .../okf-bundle/publications``
``OkfBundle.findings``                       the manifest's ``findings``
===========================================  =================================================

**One store, no second reader.** Every field reaches `aida.okf_store.read_published_bundle`
(a version) or `read_published_source_bundle` (a datasource) -- the functions the REST routes and
the MCP resource reader call -- and none freezes, resolves scope or renders anything of its own
(`tests/test_graphql_okf.py` asserts that structurally, in the manner of the REST doors'
one-door tests). So the tenant boundary, the capability envelope, the consumer-role, purpose and
quality gates, the per-datasource admission, and for a datasource the workspace's `READ_METADATA`
decision (on the datasource and on each schema where a workspace decides) are the store's own.
A caller a datasource's gate refuses is answered `FORBIDDEN` with the gate's reason code before
anything is looked up -- never an empty bundle.

**What a list node carries.** Path, kind, digest, size, the publication that first rendered its
bytes, and a citation. Never the text: a document's text is read one document at a time by
`OkfBundle.document(path)`, which looks the path up among the stored rows of the caller's own
publication exactly as the REST route does (a path in another lineage is not found) and returns
the stored text unchanged, as that route does. Every list is a connection, so the endpoint's
admission prices it before it runs.

**Decided again on every child field, once per request.** A field beneath a bundle asks
`get_okf_bundle` for the same key again before it pages, as every child connection does. The
answer -- a stored publication or the refusal -- is the request's own: the store's decision is
taken once and remembered in the scope, so asking again costs nothing and a refusal stays a
refusal for every field that asks. What a read records is recorded once per request too: the
audit event, the outbox event and, for a PUBLISHED version, a consumption edge -- the evidence
REST's routes leave -- under GraphQL's own channels (`aida.okf_store.BUNDLE_ROLE_CHANNELS`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from aida.graphql_reads import (
    Page,
    ReadRefused,
    ReadScope,
    _bounded,
    _list_page,
    _page_size,
    _require_roles,
)
from aida.okf_export import document_kind
from aida.okf_read_model import OKF_ROLES, publication_read
from aida.okf_store import (
    BUNDLE_ROLE_CHANNELS,
    SOURCE_BUNDLE_CHANNELS,
    OkfPublishedBundle,
    OkfPublishedSourceBundle,
    list_publications,
    load_document,
    read_published_bundle,
    read_published_source_bundle,
    record_okf_read,
    record_okf_source_read,
)
from aida.okf_store_models import OkfBundleDocument
from aida.schemas import OkfPublicationRead

__all__ = [
    "OKF_ROLES",
    "PRODUCT",
    "SOURCE",
    "OkfBundleHandle",
    "OkfDocumentBody",
    "OkfDocumentEntry",
    "document_citation",
    "get_okf_bundle",
    "list_okf_documents",
    "list_okf_findings",
    "list_okf_publications",
    "read_okf_document",
]

BundleTarget = Literal["PRODUCT", "SOURCE"]
PRODUCT: Final = "PRODUCT"
SOURCE: Final = "SOURCE"

#: The bound REST puts on a document path (`min_length=1`, `max_length=512`).
_MAX_PATH_LENGTH: Final = 512
#: A gate's reason code is a bare upper-snake token (`NO_WORKSPACE_MEMBERSHIP`); the tenant
#: boundary's 403 is prose. That is the whole difference between the two 403s the store raises.
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


@dataclass(frozen=True, slots=True)
class OkfBundleHandle:
    """One stored publication a request read, and the key it was decided under.

    `pinned` is the publication id the caller named, or None for the current one. The stored
    value is whichever of the two scopes the read was of.
    """

    target: BundleTarget
    target_id: UUID
    pinned: UUID | None
    stored: OkfPublishedBundle | OkfPublishedSourceBundle

    @property
    def product_key(self) -> str | None:
        """The product a version's bundle is of; None for a datasource's."""
        stored = self.stored
        return stored.product.product_key if isinstance(stored, OkfPublishedBundle) else None

    @property
    def product_version(self) -> int | None:
        stored = self.stored
        return stored.version.version if isinstance(stored, OkfPublishedBundle) else None


@dataclass(frozen=True, slots=True)
class OkfDocumentEntry:
    """One stored document as a list shows it: identity and digest, never the text."""

    publication_id: UUID
    publication_sequence: int
    path: str
    kind: str
    citation: str
    sha256: str
    bytes: int
    rendered_in_sequence: int
    subject_key: str | None


@dataclass(frozen=True, slots=True)
class OkfDocumentBody:
    """One document with its exact stored text, as `GET .../okf-bundle/document` returns it."""

    entry: OkfDocumentEntry
    text: str


def document_citation(publication_id: UUID, path: str, sha256: str) -> str:
    """The reference an answer carries to cite exactly this document: the publication it was
    read from, its path, and the digest of its bytes -- the triple an Ask run's receipts keep.
    Ids, an opaque path and a digest: nothing that names an object."""
    return f"okf:{publication_id}:{path}@{sha256}"


def _okf_refusal(exc: HTTPException) -> ReadRefused:
    """A refusal from the store, with its meaning and none of its prose.

    Its `detail` is never forwarded: a conflict's is a sentence (some carry counts), and the
    anti-enumeration 404 is the same answer for "no such bundle" and "not yours to see". The one
    detail that is a value is a gate's reason code, which `read_published_source_bundle` raises
    as the bare 403 the catalog's own read of a datasource gives.
    """
    if exc.status_code == 404:
        return ReadRefused("NOT_FOUND")
    if exc.status_code == 403:
        detail = exc.detail
        if isinstance(detail, str) and _REASON_CODE.fullmatch(detail):
            return ReadRefused("FORBIDDEN", detail)
        return ReadRefused("FORBIDDEN", "CROSS_ORGANIZATION")
    return ReadRefused("CONFLICT", "OKF_BUNDLE_UNAVAILABLE")


async def get_okf_bundle(
    scope: ReadScope, target: BundleTarget, target_id: UUID, publication_id: UUID | None
) -> OkfBundleHandle:
    """A stored bundle, decided as the REST manifest route decides it.

    The role gate (`OKF_ROLES`), then the store's read -- which is every other decision -- and
    the manifest read recorded. Decided and recorded once per request, however many aliases and
    child fields ask again: checked and filled under the lock, because sibling fields resolve
    concurrently and the second must wait for the first's answer rather than decide twice.
    """
    _require_roles(scope, OKF_ROLES)
    key = ("bundle", target, target_id, publication_id)
    async with scope.lock:
        remembered = scope.okf.get(key)
        if remembered is None:
            try:
                remembered = await _read_bundle(scope, target, target_id, publication_id)
            except ReadRefused as refused:
                remembered = refused
            scope.okf[key] = remembered
    if isinstance(remembered, ReadRefused):
        raise ReadRefused(remembered.code, remembered.reason)
    assert isinstance(remembered, OkfBundleHandle)
    return remembered


async def _read_bundle(
    scope: ReadScope, target: BundleTarget, target_id: UUID, publication_id: UUID | None
) -> OkfBundleHandle:
    """The store's read and the manifest read's record. The caller holds `scope.lock`."""
    session = scope.session
    handle: OkfBundleHandle
    try:
        if target == PRODUCT:
            product_bundle = await read_published_bundle(
                session, target_id, scope.context, scope.settings, publication_id=publication_id
            )
            record_okf_read(
                session,
                scope.context,
                product_bundle,
                action="graphql.context_product.okf_bundle_read",
                channel=BUNDLE_ROLE_CHANNELS["graphql_manifest"],
            )
            handle = OkfBundleHandle(PRODUCT, target_id, publication_id, product_bundle)
        else:
            source_bundle = await read_published_source_bundle(
                session, target_id, scope.context, scope.settings, publication_id=publication_id
            )
            record_okf_source_read(
                session,
                scope.context,
                source_bundle,
                action="graphql.datasource.okf_bundle_read",
                channel=SOURCE_BUNDLE_CHANNELS["graphql_manifest"],
            )
            handle = OkfBundleHandle(SOURCE, target_id, publication_id, source_bundle)
    except HTTPException as exc:
        raise _okf_refusal(exc) from exc
    await session.commit()
    return handle


def _entry(
    handle: OkfBundleHandle,
    *,
    path: str,
    sha256: str,
    size: int,
    rendered_in_sequence: int,
    subject_key: str | None,
) -> OkfDocumentEntry:
    publication = handle.stored.publication
    return OkfDocumentEntry(
        publication_id=publication.id,
        publication_sequence=publication.sequence,
        path=path,
        kind=document_kind(path),
        citation=document_citation(publication.id, path, sha256),
        sha256=sha256,
        bytes=size,
        rendered_in_sequence=rendered_in_sequence,
        subject_key=subject_key,
    )


async def list_okf_documents(
    scope: ReadScope, handle: OkfBundleHandle, *, first: int, after: str | None
) -> Page[OkfDocumentEntry]:
    """A bundle's documents a page at a time, in the manifest's file-index order (path order),
    without their text.

    Decided again first, as every child page is. The page is cut from the manifest's own file
    index -- the list `GET .../okf-bundle` returns as `files`, so the order, the page boundaries
    and the totals are REST's on every database -- and each entry is completed from its stored
    document row (`subjectKey`, `renderedInSequence`), one statement for the page and no text.
    A listed path with no stored row, or an index that is not the publication's whole, is the
    store's own "incomplete" refusal: a listed path is always one `document(path)` can fetch.
    """
    first = _page_size(scope, first)
    current = await get_okf_bundle(scope, handle.target, handle.target_id, handle.pinned)
    publication = current.stored.publication
    files: list[dict[str, Any]] = [
        dict(entry) for entry in dict(publication.manifest).get("files") or []
    ]
    page = _list_page(
        files, key=lambda entry: (str(entry["path"]),), coercers=(str,), first=first, after=after
    )
    if after is None and page.total != publication.document_count:
        raise ReadRefused("CONFLICT", "OKF_BUNDLE_INCOMPLETE")
    paths = [str(entry["path"]) for entry in page.items]
    stored_rows: dict[str, Any] = {}
    if paths:
        async with scope.lock:
            rows = (
                await scope.session.execute(
                    select(
                        OkfBundleDocument.path,
                        OkfBundleDocument.subject_key,
                        OkfBundleDocument.rendered_in_sequence,
                    ).where(
                        OkfBundleDocument.organization_id == publication.organization_id,
                        OkfBundleDocument.publication_id == publication.id,
                        OkfBundleDocument.path.in_(paths),
                    )
                )
            ).all()
        stored_rows = {row.path: row for row in rows}
    items: list[OkfDocumentEntry] = []
    for entry in page.items:
        row = stored_rows.get(str(entry["path"]))
        if row is None:
            raise ReadRefused("CONFLICT", "OKF_BUNDLE_INCOMPLETE")
        items.append(
            _entry(
                current,
                path=str(entry["path"]),
                sha256=str(entry["sha256"]),
                size=int(entry["bytes"]),
                rendered_in_sequence=row.rendered_in_sequence,
                subject_key=row.subject_key,
            )
        )
    return Page(
        items=items,
        total=page.total,
        end_cursor=page.end_cursor,
        has_next_page=page.has_next_page,
    )


async def read_okf_document(
    scope: ReadScope, handle: OkfBundleHandle, *, path: str
) -> OkfDocumentBody:
    """One stored document with its text, as `GET .../okf-bundle/document` reads it.

    The path is bounded as REST bounds it (1 to 512 characters, a leading slash ignored) and
    looked up only among the stored rows of the caller's own publication, so a path that is not
    in the bundle -- or is in a bundle of another lineage -- is NOT_FOUND. Recorded once per
    request per path, with the path, as the route records it.
    """
    bounded = _bounded(path, maximum=_MAX_PATH_LENGTH, minimum=1)
    assert bounded is not None
    current = await get_okf_bundle(scope, handle.target, handle.target_id, handle.pinned)
    wanted = bounded.lstrip("/")
    key = ("document", handle.target, handle.target_id, handle.pinned, wanted)
    async with scope.lock:
        remembered = scope.okf.get(key)
        if remembered is None:
            try:
                remembered = await _read_document(scope, current, wanted)
            except ReadRefused as refused:
                remembered = refused
            scope.okf[key] = remembered
    if isinstance(remembered, ReadRefused):
        raise ReadRefused(remembered.code, remembered.reason)
    assert isinstance(remembered, OkfDocumentBody)
    return remembered


async def _read_document(
    scope: ReadScope, current: OkfBundleHandle, path: str
) -> OkfDocumentBody:
    """The lookup and its record. The caller holds `scope.lock`."""
    session = scope.session
    stored = current.stored
    document = await load_document(session, stored.publication, path)
    if document is None:
        raise ReadRefused("NOT_FOUND", "OKF_DOCUMENT_NOT_FOUND")
    if isinstance(stored, OkfPublishedBundle):
        record_okf_read(
            session,
            scope.context,
            stored,
            action="graphql.context_product.okf_document_read",
            channel=BUNDLE_ROLE_CHANNELS["graphql_document"],
            path=document.path,
        )
    else:
        record_okf_source_read(
            session,
            scope.context,
            stored,
            action="graphql.datasource.okf_document_read",
            channel=SOURCE_BUNDLE_CHANNELS["graphql_document"],
            path=document.path,
        )
    await session.commit()
    return OkfDocumentBody(
        entry=_entry(
            current,
            path=document.path,
            sha256=document.sha256,
            size=document.byte_length,
            rendered_in_sequence=document.rendered_in_sequence,
            subject_key=document.subject_key,
        ),
        text=document.content,
    )


async def list_okf_publications(
    scope: ReadScope, handle: OkfBundleHandle, *, first: int, after: str | None
) -> Page[OkfPublicationRead]:
    """The caller's own lineage of stored publications, newest first -- what changed, when.

    Only the lineage this request's read decision computes: a publication built under another
    authority is neither listed nor counted. Read and recorded once per request; the list is
    the retained handful the store keeps, so it is paged from memory in sequence order.
    """
    first = _page_size(scope, first)
    current = await get_okf_bundle(scope, handle.target, handle.target_id, handle.pinned)
    key = ("history", handle.target, handle.target_id, handle.pinned)
    async with scope.lock:
        remembered = scope.okf.get(key)
        if remembered is None:
            remembered = await _read_history(scope, current)
            scope.okf[key] = remembered
    assert isinstance(remembered, list)
    return _list_page(
        remembered,
        key=lambda item: (-item.sequence,),
        coercers=(int,),
        first=first,
        after=after,
    )


async def _read_history(scope: ReadScope, current: OkfBundleHandle) -> list[OkfPublicationRead]:
    """The lineage's publications and the read's record. The caller holds `scope.lock`."""
    session = scope.session
    stored = current.stored
    items = [
        publication_read(item, is_current=item.id == stored.head.publication_id)
        for item in await list_publications(session, stored)
    ]
    if isinstance(stored, OkfPublishedBundle):
        record_okf_read(
            session,
            scope.context,
            stored,
            action="graphql.context_product.okf_publications_read",
            channel=BUNDLE_ROLE_CHANNELS["graphql_history"],
        )
    else:
        record_okf_source_read(
            session,
            scope.context,
            stored,
            action="graphql.datasource.okf_publications_read",
            channel=SOURCE_BUNDLE_CHANNELS["graphql_history"],
        )
    await session.commit()
    return items


async def list_okf_findings(
    scope: ReadScope, handle: OkfBundleHandle, *, first: int, after: str | None
) -> Page[str]:
    """The publish policy's findings for this publication -- sorted, distinct and bounded by
    the policy itself -- a page at a time. Decided again first, as every child page is."""
    first = _page_size(scope, first)
    current = await get_okf_bundle(scope, handle.target, handle.target_id, handle.pinned)
    return _list_page(
        list(current.stored.validation.findings),
        key=lambda text: (text,),
        coercers=(str,),
        first=first,
        after=after,
    )
