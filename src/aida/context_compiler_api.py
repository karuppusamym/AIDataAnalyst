import hashlib
from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.context_compiler import (
    compilation_drift_paths,
    compile_context_product,
    validate_compiled_artifact,
)

# R11-GQL01: the read decision -- who may read a version, and what it pins -- lives in
# `aida.context_product_read_service`, which GraphQL and the OKF store call too. Re-exported here
# for the importers this module already had; the routes below load through `_load_source` and
# compile what it returns.
from aida.context_product_read_service import (
    COMPILER_ROLES,
    _load_exemplars,
    _load_negative_knowledge,
    _load_source,
    load_coverage_extras,
)
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import ContextProductConsumptionEdge
from aida.platform_schemas import (
    ContextCompilationDriftRead,
    ContextCompilationDriftRequest,
    ContextCompilationRead,
    ContextCompilationValidateRequest,
    ContextCompilationValidationRead,
    ContextCompilerTarget,
)
from aida.security import SecurityContext, require_roles

# The names this module re-exports, listed so a type checker treats them as exported (implicit
# re-export is off); what the module defines itself is public as before.
__all__ = [
    "COMPILER_ROLES",
    "_load_exemplars",
    "_load_negative_knowledge",
    "_load_source",
]

router = APIRouter(prefix="/v1", tags=["context-compiler"])


@router.get("/context-product-versions/{version_id}/compile", response_model=ContextCompilationRead)
async def compile_context_product_version(
    version_id: UUID,
    target: ContextCompilerTarget = Query(default="MCP"),
    context: SecurityContext = Depends(require_roles(*COMPILER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ContextCompilationRead:
    (
        product,
        version,
        tables,
        negative_knowledge,
        exemplars,
        routines,
        views,
        ontology,
        sources,
        quality_snapshot,
    ) = await _load_source(session, version_id, context)
    meaning, changes = await load_coverage_extras(session, version)
    compiled = compile_context_product(
        product,
        version,
        target,
        tables,
        negative_knowledge,
        exemplars,
        routines,
        views,
        ontology,
        sources,
        meaning,
        changes,
    )
    correlation_id = get_correlation_id()
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="context_product.compile",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"target": target, "artifact_hash": compiled.artifact_hash},
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version.id),
        event_type="context.product_compiled.v1",
        payload={"target": target, "artifact_hash": compiled.artifact_hash},
    )
    if version.status == "PUBLISHED":
        session.add(
            ContextProductConsumptionEdge(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel="COMPILER",
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality_snapshot,
            )
        )
    await session.commit()
    return compiled


@router.get("/context-product-versions/{version_id}/compile/download")
async def download_context_compilation(
    version_id: UUID,
    target: ContextCompilerTarget = Query(default="YAML"),
    context: SecurityContext = Depends(require_roles(*COMPILER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    (
        product,
        version,
        tables,
        negative_knowledge,
        exemplars,
        routines,
        views,
        ontology,
        sources,
        quality_snapshot,
    ) = await _load_source(session, version_id, context)
    meaning, changes = await load_coverage_extras(session, version)
    compiled = compile_context_product(
        product,
        version,
        target,
        tables,
        negative_knowledge,
        exemplars,
        routines,
        views,
        ontology,
        sources,
        meaning,
        changes,
    )
    validation = validate_compiled_artifact(target, compiled.content)
    if not validation.valid:
        raise HTTPException(status_code=409, detail={"findings": validation.findings})
    extension = "yaml" if target == "YAML" else "json"
    correlation_id = get_correlation_id()
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="context_product.compile_download",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"target": target, "artifact_hash": compiled.artifact_hash},
    )
    if version.status == "PUBLISHED":
        session.add(
            ContextProductConsumptionEdge(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel="COMPILER_DOWNLOAD",
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality_snapshot,
            )
        )
    await session.commit()
    return Response(
        content=compiled.content,
        media_type=compiled.content_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{product.product_key}-{version.version}-'
                f'{target.lower()}.{extension}"'
            ),
            "X-Artifact-SHA256": compiled.artifact_hash,
        },
    )


@router.post("/context-compiler/validate", response_model=ContextCompilationValidationRead)
async def validate_context_compilation(
    body: ContextCompilationValidateRequest,
    _: SecurityContext = Depends(require_roles(*COMPILER_ROLES)),
) -> ContextCompilationValidationRead:
    return validate_compiled_artifact(body.target, body.content)


@router.post(
    "/context-product-versions/{version_id}/compile/drift",
    response_model=ContextCompilationDriftRead,
)
async def inspect_context_compilation_drift(
    version_id: UUID,
    body: ContextCompilationDriftRequest,
    context: SecurityContext = Depends(require_roles(*COMPILER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ContextCompilationDriftRead:
    (
        product,
        version,
        tables,
        negative_knowledge,
        exemplars,
        routines,
        views,
        ontology,
        sources,
        _,
    ) = await _load_source(session, version_id, context)
    meaning, changes = await load_coverage_extras(session, version)
    compiled = compile_context_product(
        product,
        version,
        body.target,
        tables,
        negative_knowledge,
        exemplars,
        routines,
        views,
        ontology,
        sources,
        meaning,
        changes,
    )
    deployed_hash = body.deployed_hash
    changed_paths: list[str] = []
    if body.deployed_content is not None:
        deployed_hash = hashlib.sha256(body.deployed_content.encode("utf-8")).hexdigest()
        changed_paths = compilation_drift_paths(compiled.content, body.deployed_content)
    if deployed_hash is None:
        raise HTTPException(status_code=422, detail="deployed artifact evidence is required")
    drifted = deployed_hash != compiled.artifact_hash
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="context_product.compile_drift",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"target": body.target, "drifted": drifted},
    )
    await session.commit()
    return ContextCompilationDriftRead(
        target=body.target,
        drifted=drifted,
        expected_hash=compiled.artifact_hash,
        deployed_hash=deployed_hash,
        changed_paths=changed_paths,
    )
