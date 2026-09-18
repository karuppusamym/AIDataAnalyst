"""R11-OKF01: the two doors onto an OKF bundle -- inspect its manifest, or download it.

Two routes, both reading one context product version, both gated the same way, and neither
touching the existing single-file compiler contracts. The design's instruction on delivery is
explicit: "Add an OKF bundle target beside existing context compiler targets. Keep single-file
compile responses compatible: use a separate bundle job/download contract if necessary rather
than putting a ZIP into a text-content field." So `ContextCompilerTarget` is not extended,
`ContextCompilationRead` is not touched, and an archive is returned by its own route with its
own media type.

**Scope resolution is the compiler's, not a second copy.** Both handlers call
`context_compiler_api._load_source`, which is where the capability envelope, the published/
consumer-role check, the purpose gate, the quality gate and the resolved routine/view/ontology/
freshness references already live. The design requires exactly this -- "Source/object preview
and product export must reuse the compiler's scope resolver" -- and a second resolver would be
a second place for a governance check to go missing. `aida.okf_snapshot.freeze_snapshot` then
adds the per-datasource admission decision and the catalog reads the compiler does not make.

**What each route records.** A bundle read is a consumption of governed context, so both
routes audit and, for a PUBLISHED version, write a `ContextProductConsumptionEdge` with its own
channel -- the same evidence the compiler's read and download already leave, on the same
version, so a reviewer sees one timeline rather than two.

**Publication is atomic by construction.** The design requires bundle contents and manifest to
be published atomically. Here they are computed together from one frozen snapshot and returned
in one response: the manifest a caller inspects and the archive it downloads describe the same
bytes, and the manifest carries the digest that proves it. There is no window in which half a
bundle is visible, because there is no stored half.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.context_compiler_api import _load_source
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import ContextProduct, ContextProductConsumptionEdge, ContextProductVersion
from aida.okf_export import (
    OKF_CONFORMANCE_STATUS,
    OkfBundle,
    OkfExportError,
    bundle_archive_bytes,
    bundle_index,
    export_okf_bundle,
    validate_atlas_publish_policy,
)
from aida.okf_snapshot import freeze_snapshot
from aida.schemas import OkfBundleFileRead, OkfBundleRead
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


async def _build(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
    settings: Settings,
) -> tuple[OkfBundle, ContextProduct, ContextProductVersion, dict[str, object]]:
    """Freeze, render and validate one bundle, or refuse.

    The clock is read exactly once, here, and handed to the freeze as data. Nothing downstream
    reads it again, which is what lets two renders of one snapshot be byte-identical.
    """
    (
        product,
        version,
        _tables,
        _negative_knowledge,
        _exemplars,
        routines,
        views,
        ontology,
        freshness,
        quality_snapshot,
    ) = await _load_source(session, version_id, context)
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
        captured_at=datetime.now(UTC),
    )
    try:
        bundle = export_okf_bundle(snapshot)
    except OkfExportError as error:
        # 409 with the reason and no object detail: an identity collision, a dangling reference
        # or an over-limit document is a state the caller can act on, and the message must not
        # become a channel for a name the caller may not see.
        raise HTTPException(status_code=409, detail=str(error)) from error
    return bundle, product, version, quality_snapshot


def _record(
    session: AsyncSession,
    context: SecurityContext,
    version: ContextProductVersion,
    bundle: OkfBundle,
    *,
    action: str,
    channel: str,
    quality_snapshot: dict[str, object],
) -> None:
    version_id = version.id
    organization_id = version.organization_id
    correlation_id = get_correlation_id()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action=action,
        resource_type="context_product_version",
        resource_id=str(version_id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "bundle_content_digest": bundle.content_digest,
            "content_snapshot_digest": bundle.manifest["content_snapshot_digest"],
            "documents": len(bundle.documents),
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version_id),
        event_type="context.okf_bundle_exported.v1",
        payload={
            "bundle_content_digest": bundle.content_digest,
            "documents": len(bundle.documents),
            "channel": channel,
        },
    )
    if version.status == "PUBLISHED":
        session.add(
            ContextProductConsumptionEdge(
                organization_id=organization_id,
                context_product_version_id=version_id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel=channel,
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality_snapshot,
            )
        )


@router.get(
    "/context-product-versions/{version_id}/okf-bundle",
    response_model=OkfBundleRead,
)
async def inspect_okf_bundle(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfBundleRead:
    """The bundle's manifest and file index, without moving the documents.

    Returns the validation verdict alongside it rather than refusing an invalid bundle: the
    findings are how a caller learns *why* an export would not publish, and
    `validate_atlas_publish_policy` is strictly stronger than OKF conformance, so a `valid:
    false` here is an Atlas policy answer and not a statement that the bundle is unreadable.
    """
    bundle, _product, version, quality_snapshot = await _build(
        session, version_id, context, settings
    )
    validation = validate_atlas_publish_policy(bundle)
    _record(
        session,
        context,
        version,
        bundle,
        action="context_product.okf_bundle_inspect",
        channel="OKF_MANIFEST",
        quality_snapshot=quality_snapshot,
    )
    await session.commit()
    return OkfBundleRead(
        okf_version=str(bundle.manifest["okf_version"]),
        spec_revision=str(bundle.manifest["specification"]["revision"]),
        spec_conformance=OKF_CONFORMANCE_STATUS,
        profile=f"{bundle.manifest['compiler']['profile']}"
        f"/{bundle.manifest['compiler']['profile_version']}",
        content_snapshot_digest=str(bundle.manifest["content_snapshot_digest"]),
        bundle_content_digest=bundle.content_digest,
        scope_digest=str(bundle.manifest["scope_digest"]),
        document_count=len(bundle.documents),
        valid=validation.valid,
        findings=list(validation.findings),
        files=[OkfBundleFileRead(**entry) for entry in bundle_index(bundle)],
        manifest=bundle.manifest,
    )


@router.get("/context-product-versions/{version_id}/okf-bundle/download")
async def download_okf_bundle(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*OKF_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """The bundle as a deterministic archive, refused unless it satisfies the publish policy.

    Stricter than the manifest route on purpose. Inspecting findings is how a caller diagnoses
    an export; handing out a file is publication, and a file cannot be recalled -- "a downloaded
    file cannot be remotely revoked". So the download refuses what the manifest is willing to
    describe.
    """
    bundle, product, version, quality_snapshot = await _build(
        session, version_id, context, settings
    )
    validation = validate_atlas_publish_policy(bundle)
    if not validation.valid:
        raise HTTPException(status_code=409, detail={"findings": list(validation.findings)})
    archive = bundle_archive_bytes(bundle)
    _record(
        session,
        context,
        version,
        bundle,
        action="context_product.okf_bundle_download",
        channel="OKF_DOWNLOAD",
        quality_snapshot=quality_snapshot,
    )
    await session.commit()
    product_key = product.product_key
    product_version = version.version
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{product_key}-{product_version}-okf-bundle.zip"'
            ),
            "X-Atlas-Bundle-Content-SHA256": bundle.content_digest,
            "X-Atlas-Content-Snapshot-SHA256": str(
                bundle.manifest["content_snapshot_digest"]
            ),
            "X-Atlas-OKF-Spec-Revision": str(bundle.manifest["specification"]["revision"]),
        },
    )
