import hashlib
from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.context_compiler import (
    ResolvedNegativeAssertion,
    ResolvedTableReference,
    compilation_drift_paths,
    compile_context_product,
    validate_compiled_artifact,
)
from aida.context_product_policy import (
    evaluate_context_product_purpose,
    evaluate_context_product_quality_from_db,
)
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
)
from aida.negative_knowledge import query_negatives_for_scope
from aida.platform_schemas import (
    ContextCompilationDriftRead,
    ContextCompilationDriftRequest,
    ContextCompilationRead,
    ContextCompilationValidateRequest,
    ContextCompilationValidationRead,
    ContextCompilerTarget,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["context-compiler"])

COMPILER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataProductOwner",
    "DataSteward",
    "AgentDeveloper",
    "Analyst",
)


async def _load_negative_knowledge(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: list[str],
) -> list[ResolvedNegativeAssertion]:
    """Load negative knowledge bounded to this context product version's own
    table scope (never the whole organization's negative-knowledge surface),
    pre-serialized so `compile_context_product` stays a pure function.
    """
    records = await query_negatives_for_scope(session, organization_id, table_ids)
    return [
        ResolvedNegativeAssertion(
            subject_id=record.subject_id,
            assertion_type=record.assertion_type,
            predicate=record.predicate,
            rejected_by=record.rejected_by,
            rejected_at=record.rejected_at.isoformat(),
            suppression_active=record.suppression_active,
            lift_reason=record.lift_reason,
        )
        for record in records
    ]


async def _load_source(
    session: AsyncSession,
    version_id: UUID,
    context: SecurityContext,
) -> tuple[
    ContextProduct,
    ContextProductVersion,
    list[ResolvedTableReference],
    list[ResolvedNegativeAssertion],
    dict[str, object],
]:
    version = await session.get(ContextProductVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="context product version not found")
    enforce_organization(context, version.organization_id)
    product = await session.get(ContextProduct, version.product_id)
    if product is None or product.lifecycle_status != "ACTIVE":
        raise HTTPException(status_code=404, detail="context product version not found")
    lifecycle_reader = bool(
        context.roles & {"PlatformAdmin", "MetadataAdmin", "DataProductOwner", "DataSteward"}
    )
    if not lifecycle_reader and (
        version.status != "PUBLISHED" or context.roles.isdisjoint(version.allowed_consumer_roles)
    ):
        raise HTTPException(status_code=404, detail="context product version not found")
    purpose = evaluate_context_product_purpose(context.business_purpose, version.policy_summary)
    if not lifecycle_reader and not purpose.allowed:
        raise HTTPException(status_code=404, detail="context product version not found")
    quality = await evaluate_context_product_quality_from_db(
        session,
        organization_id=version.organization_id,
        table_id_values=version.table_ids,
        requirements=version.quality_requirements,
    )
    if not lifecycle_reader and not quality.allowed:
        raise HTTPException(status_code=404, detail="context product version not found")
    table_ids = [UUID(value) for value in version.table_ids]
    rows = (
        (
            await session.execute(
                select(MetadataTable, MetadataSchema, MetadataCatalog)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(
                    MetadataTable.id.in_(table_ids),
                    MetadataTable.organization_id == version.organization_id,
                )
            )
        ).all()
        if table_ids
        else []
    )
    resolved = [
        ResolvedTableReference(
            table_id=str(table.id),
            qualified_name=f"{catalog.name}.{schema.name}.{table.name}",
        )
        for table, schema, catalog in rows
    ]
    if len(resolved) != len(table_ids):
        raise HTTPException(
            status_code=409, detail="context product contains unresolved table references"
        )
    negative_knowledge = await _load_negative_knowledge(
        session, version.organization_id, version.table_ids
    )
    return product, version, resolved, negative_knowledge, quality.snapshot()


@router.get("/context-product-versions/{version_id}/compile", response_model=ContextCompilationRead)
async def compile_context_product_version(
    version_id: UUID,
    target: ContextCompilerTarget = Query(default="MCP"),
    context: SecurityContext = Depends(require_roles(*COMPILER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ContextCompilationRead:
    product, version, tables, negative_knowledge, quality_snapshot = await _load_source(
        session, version_id, context
    )
    compiled = compile_context_product(product, version, target, tables, negative_knowledge)
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
    product, version, tables, negative_knowledge, quality_snapshot = await _load_source(
        session, version_id, context
    )
    compiled = compile_context_product(product, version, target, tables, negative_knowledge)
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
    product, version, tables, negative_knowledge, _ = await _load_source(
        session, version_id, context
    )
    compiled = compile_context_product(
        product, version, body.target, tables, negative_knowledge
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
