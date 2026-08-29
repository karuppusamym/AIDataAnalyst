import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.entitlements import apply_entitlement
from aida.events import record_audit, record_outbox
from aida.models import (
    AgentRun,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataContractVersion,
    DataProduct,
    DataProductAccessRequest,
    DataProductPort,
    DataProductRoleBinding,
    DataProductVersion,
    GovernanceReview,
    McpConsumptionEvidence,
    MetadataTable,
    Project,
    QueryExecution,
    SemanticModelVersion,
    ToolExecution,
)
from aida.platform_schemas import (
    DataContractCreate,
    DataContractVersionRead,
    DataProductCreate,
    DataProductDefinition,
    DataProductPortDefinition,
    DataProductVersionCreate,
    DataProductVersionRead,
    EntitlementOperation,
    MarketplaceAccessRequestCreate,
    MarketplaceAccessRequestRead,
    MarketplaceProductRead,
    PortfolioAccessRead,
    PortfolioAnalyticsSummaryRead,
    PortfolioAnalyticsTrendsRead,
    PortfolioLifecycleRead,
    PortfolioQualityRead,
    PortfolioQueueRead,
    PortfolioTopProductRead,
    PortfolioTrendPointRead,
    PortfolioUsageRead,
)
from aida.schemas import GovernanceReviewRead, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["data-products-marketplace"])

PRODUCT_AUTHORS = ("PlatformAdmin", "DataProductOwner", "DataSteward", "MetadataAdmin")
PRODUCT_READERS = (*PRODUCT_AUTHORS, "Reviewer", "Auditor", "Analyst", "Viewer")
MARKETPLACE_USERS = ("PlatformAdmin", "Analyst", "Viewer", "DataConsumer", "DataScientist")
ANALYTICS_READERS = (*PRODUCT_READERS, "Operations")


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def data_product_fingerprint(definition: DataProductDefinition) -> str:
    return _canonical_hash(definition.model_dump(mode="json"))


def data_contract_fingerprint(contract: DataContractCreate) -> str:
    return _canonical_hash(contract.model_dump(mode="json"))


def evaluate_contract_compatibility(
    previous_fields: list[dict[str, Any]],
    candidate_fields: list[dict[str, Any]],
    compatibility_mode: str,
) -> list[dict[str, Any]]:
    """Return deterministic breaking-change findings for ODCS-style schema evolution."""
    if compatibility_mode == "NONE":
        return []
    previous = {str(field["name"]).casefold(): field for field in previous_fields}
    candidate = {str(field["name"]).casefold(): field for field in candidate_fields}
    findings: list[dict[str, Any]] = []

    for key in sorted(previous):
        old = previous[key]
        new = candidate.get(key)
        if new is None and compatibility_mode in {"BACKWARD", "FULL"}:
            findings.append(
                {
                    "code": "FIELD_REMOVED",
                    "field": old["name"],
                    "message": "A previously available field was removed.",
                }
            )
            continue
        if new is not None and str(old["data_type"]).casefold() != str(new["data_type"]).casefold():
            findings.append(
                {
                    "code": "FIELD_TYPE_CHANGED",
                    "field": old["name"],
                    "previous": old["data_type"],
                    "candidate": new["data_type"],
                    "message": "Changing a field type is not compatibility-safe.",
                }
            )
        if (
            new is not None
            and compatibility_mode in {"BACKWARD", "FULL"}
            and not bool(old.get("required"))
            and bool(new.get("required"))
        ):
            findings.append(
                {
                    "code": "FIELD_BECAME_REQUIRED",
                    "field": old["name"],
                    "message": "An optional field became required.",
                }
            )

    if compatibility_mode in {"FORWARD", "FULL"}:
        for key in sorted(candidate.keys() - previous.keys()):
            field = candidate[key]
            if bool(field.get("required")):
                findings.append(
                    {
                        "code": "REQUIRED_FIELD_ADDED",
                        "field": field["name"],
                        "message": "A new required field breaks forward compatibility.",
                    }
                )
    return findings


async def _project_scope(
    session: AsyncSession, project_id: UUID, context: SecurityContext
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    return project


async def _product_scope(
    session: AsyncSession, product_id: UUID, context: SecurityContext
) -> DataProduct:
    product = await session.get(DataProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="data product not found")
    enforce_organization(context, product.organization_id)
    return product


async def _version_scope(
    session: AsyncSession, version_id: UUID, context: SecurityContext
) -> tuple[DataProduct, DataProductVersion]:
    version = await session.get(DataProductVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="data product version not found")
    enforce_organization(context, version.organization_id)
    product = await session.get(DataProduct, version.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="data product not found")
    return product, version


async def _validate_product_references(
    session: AsyncSession,
    organization_id: UUID,
    definition: DataProductDefinition,
) -> None:
    if definition.context_product_version_id is not None:
        context_version = await session.get(
            ContextProductVersion, definition.context_product_version_id
        )
        if (
            context_version is None
            or context_version.organization_id != organization_id
            or context_version.status != "PUBLISHED"
        ):
            raise HTTPException(
                status_code=422,
                detail="context product reference must be published in this organization",
            )
    for port in definition.ports:
        try:
            asset_uuid = UUID(port.asset_id)
        except ValueError:
            if port.asset_type != "API":
                raise HTTPException(
                    status_code=422, detail=f"port {port.port_key} requires a UUID asset identifier"
                ) from None
            continue
        asset: MetadataTable | SemanticModelVersion | ContextProductVersion | None
        if port.asset_type == "TABLE":
            asset = await session.get(MetadataTable, asset_uuid)
        elif port.asset_type == "SEMANTIC_MODEL":
            asset = await session.get(SemanticModelVersion, asset_uuid)
        elif port.asset_type == "CONTEXT_PRODUCT":
            asset = await session.get(ContextProductVersion, asset_uuid)
        else:
            asset = None
        if asset is None or getattr(asset, "organization_id", None) != organization_id:
            raise HTTPException(
                status_code=422,
                detail=f"port {port.port_key} asset is unavailable in this organization",
            )
        expected_status = "APPROVED" if port.asset_type == "SEMANTIC_MODEL" else "PUBLISHED"
        asset_status = getattr(asset, "status", "ACTIVE")
        if port.asset_type in {"SEMANTIC_MODEL", "CONTEXT_PRODUCT"} and asset_status not in {
            expected_status,
            "PUBLISHED",
        }:
            raise HTTPException(
                status_code=422, detail=f"port {port.port_key} must reference a published asset"
            )


def _apply_product_definition(
    version: DataProductVersion, definition: DataProductDefinition
) -> DataProductVersion:
    version.name = definition.name
    version.description = definition.description
    version.domain_name = definition.domain_name
    version.owner_principal = definition.owner_principal
    version.usage_terms = definition.usage_terms
    version.classification = definition.classification
    version.certification_status = definition.certification_status
    version.quality_score = definition.quality_score
    version.lineage_coverage = definition.lineage_coverage
    version.context_product_version_id = definition.context_product_version_id
    version.discoverable_roles = list(definition.discoverable_roles)
    version.consumer_roles = list(definition.consumer_roles)
    version.fingerprint = data_product_fingerprint(definition)
    return version


async def _replace_product_children(
    session: AsyncSession,
    version: DataProductVersion,
    ports: list[DataProductPortDefinition],
) -> None:
    await session.execute(
        delete(DataProductPort).where(DataProductPort.data_product_version_id == version.id)
    )
    await session.execute(
        delete(DataProductRoleBinding).where(
            DataProductRoleBinding.data_product_version_id == version.id
        )
    )
    session.add_all(
        [
            DataProductPort(
                organization_id=version.organization_id,
                data_product_version_id=version.id,
                **port.model_dump(),
            )
            for port in ports
        ]
    )
    session.add_all(
        [
            DataProductRoleBinding(
                organization_id=version.organization_id,
                data_product_version_id=version.id,
                role_kind=kind,
                role_name=role,
            )
            for kind, roles in (
                ("DISCOVER", version.discoverable_roles),
                ("CONSUME", version.consumer_roles),
            )
            for role in roles
        ]
    )


async def _ports_by_version(
    session: AsyncSession, version_ids: list[UUID]
) -> dict[UUID, list[DataProductPort]]:
    result: dict[UUID, list[DataProductPort]] = {version_id: [] for version_id in version_ids}
    if not version_ids:
        return result
    ports = (
        await session.scalars(
            select(DataProductPort)
            .where(DataProductPort.data_product_version_id.in_(version_ids))
            .order_by(DataProductPort.data_product_version_id, DataProductPort.port_key)
        )
    ).all()
    for port in ports:
        result.setdefault(port.data_product_version_id, []).append(port)
    return result


def _version_read(
    product: DataProduct,
    version: DataProductVersion,
    ports: list[DataProductPort],
) -> DataProductVersionRead:
    return DataProductVersionRead(
        id=version.id,
        organization_id=version.organization_id,
        product_id=version.product_id,
        product_key=product.product_key,
        version=version.version,
        status=version.status,
        name=version.name,
        description=version.description,
        domain_name=version.domain_name,
        owner_principal=version.owner_principal,
        usage_terms=version.usage_terms,
        classification=version.classification,
        certification_status=version.certification_status,
        quality_score=version.quality_score,
        lineage_coverage=version.lineage_coverage,
        context_product_version_id=version.context_product_version_id,
        discoverable_roles=version.discoverable_roles,
        consumer_roles=version.consumer_roles,
        ports=[
            DataProductPortDefinition(
                port_key=port.port_key,
                direction=port.direction,
                name=port.name,
                description=port.description,
                asset_type=port.asset_type,
                asset_id=port.asset_id,
            )
            for port in ports
        ],
        fingerprint=version.fingerprint,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        published_at=version.published_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def _contract_read(contract: DataContractVersion) -> DataContractVersionRead:
    return DataContractVersionRead.model_validate(contract)


def _is_discoverable(context: SecurityContext, version: DataProductVersion) -> bool:
    return (
        "PlatformAdmin" in context.roles
        or "*" in version.discoverable_roles
        or not context.roles.isdisjoint(version.discoverable_roles)
    )


def _role_has_product_access(context: SecurityContext, version: DataProductVersion) -> bool:
    return (
        "PlatformAdmin" in context.roles
        or "*" in version.consumer_roles
        or not context.roles.isdisjoint(version.consumer_roles)
    )


def _request_access_status(
    context: SecurityContext,
    version: DataProductVersion,
    request: DataProductAccessRequest | None,
    now: datetime,
) -> str:
    if _role_has_product_access(context, version):
        return "ROLE_GRANTED"
    if (
        request is not None
        and request.status == "APPROVED"
        and (request.expires_at is None or request.expires_at > now)
    ):
        return "REQUEST_APPROVED"
    if request is not None and request.status == "PENDING":
        return "REQUEST_PENDING"
    return "NOT_REQUESTED"


def _marketplace_access_request_read(
    request: DataProductAccessRequest,
) -> MarketplaceAccessRequestRead:
    return MarketplaceAccessRequestRead(
        id=request.id,
        organization_id=request.organization_id,
        data_product_version_id=request.data_product_version_id,
        requested_by=request.requested_by,
        purpose=request.purpose,
        duration_days=request.duration_days,
        status=request.status,
        governance_review_id=request.governance_review_id,
        decided_by=request.decided_by,
        decision_reason=request.decision_reason,
        decided_at=request.decided_at,
        expires_at=request.expires_at,
        revoked_by=request.revoked_by,
        revoked_at=request.revoked_at,
        fulfillment_status=request.fulfillment_status,
        fulfillment_provider=request.fulfillment_provider,
        fulfillment_reference=request.fulfillment_reference,
        fulfillment_error=request.fulfillment_error,
        fulfilled_at=request.fulfilled_at,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def _count_map(rows: Sequence[Any]) -> dict[str, int]:
    return {str(key): int(value) for key, value in rows}


def _count_value(counts: dict[str, int], key: str) -> int:
    return int(counts.get(key, 0))


def _trend_bucket_ranges(
    *, now: datetime, window_days: int, bucket_days: int
) -> list[tuple[datetime, datetime]]:
    window_start = now - timedelta(days=window_days)
    ranges: list[tuple[datetime, datetime]] = []
    cursor = window_start
    step = timedelta(days=bucket_days)
    while cursor < now:
        bucket_end = min(now, cursor + step)
        ranges.append((cursor, bucket_end))
        cursor = bucket_end
    return ranges or [(window_start, now)]


def _build_portfolio_trend_points(
    *,
    now: datetime,
    window_days: int,
    bucket_days: int,
    access_request_times: list[datetime],
    context_read_times: list[datetime],
    mcp_operation_times: list[datetime],
    mcp_tool_call_times: list[datetime],
    agent_runs: Sequence[Any],
    query_execution_times: list[datetime],
) -> list[PortfolioTrendPointRead]:
    ranges = _trend_bucket_ranges(now=now, window_days=window_days, bucket_days=bucket_days)
    window_start = ranges[0][0]
    bucket_seconds = bucket_days * 86_400
    counters: list[dict[str, Any]] = [
        {
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "access_requests": 0,
            "context_reads": 0,
            "mcp_operations": 0,
            "mcp_tool_calls": 0,
            "agent_runs": 0,
            "governed_tool_runs": 0,
            "model_gateway_runs": 0,
            "query_executions": 0,
        }
        for bucket_start, bucket_end in ranges
    ]

    def bucket_index(timestamp: datetime) -> int | None:
        elapsed = (timestamp - window_start).total_seconds()
        if elapsed < 0:
            return None
        return min(int(elapsed // bucket_seconds), len(counters) - 1)

    def increment_all(timestamps: list[datetime], field_name: str) -> None:
        for timestamp in timestamps:
            index = bucket_index(timestamp)
            if index is not None:
                counters[index][field_name] += 1

    increment_all(access_request_times, "access_requests")
    increment_all(context_read_times, "context_reads")
    increment_all(mcp_operation_times, "mcp_operations")
    increment_all(mcp_tool_call_times, "mcp_tool_calls")
    increment_all(query_execution_times, "query_executions")

    for created_at, generation_source in agent_runs:
        index = bucket_index(created_at)
        if index is None:
            continue
        counters[index]["agent_runs"] += 1
        if generation_source == "GOVERNED_TOOL":
            counters[index]["governed_tool_runs"] += 1
        elif generation_source == "MODEL_GATEWAY":
            counters[index]["model_gateway_runs"] += 1

    return [PortfolioTrendPointRead.model_validate(counter) for counter in counters]


@router.post(
    "/projects/{project_id}/data-products",
    response_model=DataProductVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_product(
    project_id: UUID,
    body: DataProductCreate,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> DataProductVersionRead:
    project = await _project_scope(session, project_id, context)
    await _validate_product_references(session, project.organization_id, body)
    product = DataProduct(
        organization_id=project.organization_id,
        project_id=project.id,
        product_key=body.product_key,
        created_by=context.principal_id,
    )
    session.add(product)
    try:
        await session.flush()
        version = _apply_product_definition(
            DataProductVersion(
                organization_id=project.organization_id,
                product_id=product.id,
                version=1,
                created_by=context.principal_id,
            ),
            body,
        )
        session.add(version)
        await session.flush()
        await _replace_product_children(session, version, body.ports)
        record_audit(
            session,
            replace(context, organization_id=project.organization_id),
            action="data_product.create",
            resource_type="data_product_version",
            resource_id=str(version.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"product_key": product.product_key, "version": 1},
        )
        record_outbox(
            session,
            organization_id=project.organization_id,
            aggregate_type="data_product_version",
            aggregate_id=str(version.id),
            event_type="data_product.draft_created.v1",
            payload={"data_product_id": str(product.id), "version": 1},
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="data product key or version already exists"
        ) from exc
    return (
        _version_read(product, version, [])
        if not body.ports
        else _version_read(
            product,
            version,
            [
                DataProductPort(
                    id=uuid4(),
                    organization_id=version.organization_id,
                    data_product_version_id=version.id,
                    **port.model_dump(),
                )
                for port in body.ports
            ],
        )
    )


@router.post(
    "/data-products/{product_id}/versions",
    response_model=DataProductVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_product_version(
    product_id: UUID,
    body: DataProductVersionCreate,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> DataProductVersionRead:
    product = await _product_scope(session, product_id, context)
    if product.lifecycle_status == "RETIRED":
        raise HTTPException(status_code=409, detail="retired data products cannot be versioned")
    await _validate_product_references(session, product.organization_id, body)
    latest = await session.scalar(
        select(func.max(DataProductVersion.version)).where(
            DataProductVersion.product_id == product.id
        )
    )
    version = _apply_product_definition(
        DataProductVersion(
            organization_id=product.organization_id,
            product_id=product.id,
            version=int(latest or 0) + 1,
            created_by=context.principal_id,
        ),
        body,
    )
    session.add(version)
    try:
        await session.flush()
        await _replace_product_children(session, version, body.ports)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="data product version conflict") from exc
    ports = (await _ports_by_version(session, [version.id]))[version.id]
    return _version_read(product, version, ports)


@router.put("/data-product-versions/{version_id}", response_model=DataProductVersionRead)
async def update_data_product_version(
    version_id: UUID,
    body: DataProductVersionCreate,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> DataProductVersionRead:
    product, version = await _version_scope(session, version_id, context)
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft data product versions can change")
    await _validate_product_references(session, version.organization_id, body)
    _apply_product_definition(version, body)
    await _replace_product_children(session, version, body.ports)
    await session.commit()
    ports = (await _ports_by_version(session, [version.id]))[version.id]
    return _version_read(product, version, ports)


@router.get("/projects/{project_id}/data-products", response_model=Page)
async def list_data_products(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await _project_scope(session, project_id, context)
    latest = (
        select(func.max(DataProductVersion.version))
        .where(DataProductVersion.product_id == DataProduct.id)
        .correlate(DataProduct)
        .scalar_subquery()
    )
    filters = (
        DataProduct.project_id == project.id,
        DataProduct.organization_id == project.organization_id,
        DataProductVersion.version == latest,
    )
    rows = (
        await session.execute(
            select(DataProduct, DataProductVersion)
            .join(DataProductVersion, DataProductVersion.product_id == DataProduct.id)
            .where(*filters)
            .order_by(DataProduct.product_key)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(
        select(func.count())
        .select_from(DataProduct)
        .where(
            DataProduct.project_id == project.id,
            DataProduct.organization_id == project.organization_id,
        )
    )
    ports = await _ports_by_version(session, [version.id for _, version in rows])
    return Page(
        items=[_version_read(product, version, ports[version.id]) for product, version in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/data-product-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_data_product_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    product, version = await _version_scope(session, version_id, context)
    if version.status != "DRAFT":
        raise HTTPException(
            status_code=409, detail="only draft data product versions can be submitted"
        )
    published_contract = await session.scalar(
        select(DataContractVersion.id).where(
            DataContractVersion.product_id == product.id,
            DataContractVersion.status == "PUBLISHED",
        )
    )
    if published_contract is None:
        raise HTTPException(status_code=409, detail="publish a compatible data contract first")
    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="DATA_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    version.status = "REVIEW_REQUIRED"
    session.add(review)
    await session.flush()
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={"review_id": str(review.id), "object_type": review.object_type},
    )
    await session.commit()
    return review


@router.post(
    "/data-product-versions/{version_id}/retire",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_data_product_retirement(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    _, version = await _version_scope(session, version_id, context)
    if version.status != "PUBLISHED":
        raise HTTPException(status_code=409, detail="only published data products can retire")
    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="DATA_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action="RETIRE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.commit()
    return review


@router.post(
    "/data-products/{product_id}/contracts",
    response_model=DataContractVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_contract(
    product_id: UUID,
    body: DataContractCreate,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> DataContractVersionRead:
    product = await _product_scope(session, product_id, context)
    previous = await session.scalar(
        select(DataContractVersion).where(
            DataContractVersion.product_id == product.id,
            DataContractVersion.status == "PUBLISHED",
        )
    )
    fields = [field.model_dump(mode="json") for field in body.schema_definition]
    findings = (
        evaluate_contract_compatibility(previous.schema_definition, fields, body.compatibility_mode)
        if previous is not None
        else []
    )
    latest = await session.scalar(
        select(func.max(DataContractVersion.version)).where(
            DataContractVersion.product_id == product.id
        )
    )
    contract = DataContractVersion(
        organization_id=product.organization_id,
        product_id=product.id,
        version=int(latest or 0) + 1,
        compatibility_mode=body.compatibility_mode,
        compatibility_status=(
            "INITIAL" if previous is None else "BREAKING" if findings else "COMPATIBLE"
        ),
        compatibility_findings=findings,
        schema_definition=fields,
        quality_rules=[rule.model_dump(mode="json") for rule in body.quality_rules],
        freshness_sla_minutes=body.freshness_sla_minutes,
        availability_sla_percent=body.availability_sla_percent,
        producer_principal=body.producer_principal,
        consumer_roles=body.consumer_roles,
        fingerprint=data_contract_fingerprint(body),
        created_by=context.principal_id,
    )
    session.add(contract)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="data contract version conflict") from exc
    return _contract_read(contract)


@router.get("/data-products/{product_id}/contracts", response_model=Page)
async def list_data_contracts(
    product_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    product = await _product_scope(session, product_id, context)
    contracts = (
        await session.scalars(
            select(DataContractVersion)
            .where(DataContractVersion.product_id == product.id)
            .order_by(DataContractVersion.version.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(
        select(func.count())
        .select_from(DataContractVersion)
        .where(DataContractVersion.product_id == product.id)
    )
    return Page(
        items=[_contract_read(contract) for contract in contracts],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/data-contract-versions/{contract_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_data_contract(
    contract_id: UUID,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    contract = await session.get(DataContractVersion, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="data contract version not found")
    enforce_organization(context, contract.organization_id)
    if contract.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft data contracts can be submitted")
    review = GovernanceReview(
        organization_id=contract.organization_id,
        object_type="DATA_CONTRACT_VERSION",
        object_id=str(contract.id),
        requested_action=(
            "PUBLISH_BREAKING_EXCEPTION"
            if contract.compatibility_status == "BREAKING"
            else "PUBLISH"
        ),
        requested_by=context.principal_id,
    )
    contract.status = "REVIEW_REQUIRED"
    session.add(review)
    await session.commit()
    return review


@router.get("/marketplace/products", response_model=Page)
async def search_marketplace(
    q: str | None = Query(default=None, max_length=200),
    domain: str | None = Query(default=None, max_length=200),
    classification: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*MARKETPLACE_USERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    if context.organization_id is None:
        raise HTTPException(status_code=403, detail="organization context is required")
    discoverability_filter = (
        select(DataProductRoleBinding.id)
        .where(
            DataProductRoleBinding.data_product_version_id == DataProductVersion.id,
            DataProductRoleBinding.role_kind == "DISCOVER",
            or_(
                DataProductRoleBinding.role_name == "*",
                DataProductRoleBinding.role_name.in_(context.roles),
            ),
        )
        .exists()
    )
    filters: list[Any] = [
        DataProductVersion.organization_id == context.organization_id,
        DataProductVersion.status == "PUBLISHED",
        DataProduct.lifecycle_status == "ACTIVE",
        discoverability_filter,
    ]
    if q:
        term = f"%{q.strip()}%"
        filters.append(
            or_(
                DataProduct.product_key.ilike(term),
                DataProductVersion.name.ilike(term),
                DataProductVersion.description.ilike(term),
            )
        )
    if domain:
        filters.append(DataProductVersion.domain_name == domain)
    if classification:
        filters.append(DataProductVersion.classification == classification.upper())
    base = (
        select(DataProduct, DataProductVersion)
        .join(DataProductVersion, DataProductVersion.product_id == DataProduct.id)
        .where(*filters)
    )
    rows = (
        await session.execute(base.order_by(DataProductVersion.name).limit(limit).offset(offset))
    ).all()
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    version_ids = [version.id for _, version in rows]
    ports = await _ports_by_version(session, version_ids)
    requests = (
        (
            await session.scalars(
                select(DataProductAccessRequest)
                .where(
                    DataProductAccessRequest.data_product_version_id.in_(version_ids),
                    DataProductAccessRequest.requested_by == context.principal_id,
                )
                .order_by(DataProductAccessRequest.created_at.desc())
            )
        ).all()
        if version_ids
        else []
    )
    request_by_version: dict[UUID, DataProductAccessRequest] = {}
    for item in requests:
        request_by_version.setdefault(item.data_product_version_id, item)
    now = datetime.now(UTC)
    items = []
    for product, version in rows:
        base_read = _version_read(product, version, ports[version.id])
        items.append(
            MarketplaceProductRead(
                **base_read.model_dump(),
                access_status=_request_access_status(
                    context, version, request_by_version.get(version.id), now
                ),
            )
        )
    return Page(items=items, limit=limit, offset=offset, total=total or 0)


@router.post(
    "/marketplace/products/{version_id}/access-requests",
    response_model=MarketplaceAccessRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_marketplace_access(
    version_id: UUID,
    body: MarketplaceAccessRequestCreate,
    context: SecurityContext = Depends(require_roles(*MARKETPLACE_USERS)),
    session: AsyncSession = Depends(get_session),
) -> DataProductAccessRequest:
    _, version = await _version_scope(session, version_id, context)
    if version.status != "PUBLISHED" or not _is_discoverable(context, version):
        raise HTTPException(status_code=404, detail="marketplace product not found")
    if _role_has_product_access(context, version):
        raise HTTPException(status_code=409, detail="caller already has role-based access")
    existing_pending_request = await session.scalar(
        select(DataProductAccessRequest.id).where(
            DataProductAccessRequest.data_product_version_id == version.id,
            DataProductAccessRequest.requested_by == context.principal_id,
            DataProductAccessRequest.status == "PENDING",
        )
    )
    if existing_pending_request is not None:
        raise HTTPException(status_code=409, detail="an access request is already pending")
    request_id = uuid4()
    review_id = uuid4()
    review = GovernanceReview(
        id=review_id,
        organization_id=version.organization_id,
        object_type="DATA_PRODUCT_ACCESS_REQUEST",
        object_id=str(request_id),
        requested_action="GRANT_ACCESS",
        requested_by=context.principal_id,
    )
    access_request = DataProductAccessRequest(
        id=request_id,
        organization_id=version.organization_id,
        data_product_version_id=version.id,
        requested_by=context.principal_id,
        purpose=body.purpose,
        duration_days=body.duration_days,
        governance_review_id=review_id,
    )
    session.add(review)
    await session.flush()
    session.add(access_request)
    record_audit(
        session,
        context,
        action="marketplace.access.request",
        resource_type="data_product_access_request",
        resource_id=str(request_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"data_product_version_id": str(version.id), "duration_days": body.duration_days},
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="data_product_access_request",
        aggregate_id=str(request_id),
        event_type="data_product.access_requested.v1",
        payload={"review_id": str(review_id), "data_product_version_id": str(version.id)},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        existing_pending_request = await session.scalar(
            select(DataProductAccessRequest.id).where(
                DataProductAccessRequest.data_product_version_id == version.id,
                DataProductAccessRequest.requested_by == context.principal_id,
                DataProductAccessRequest.status == "PENDING",
            )
        )
        if existing_pending_request is not None:
            raise HTTPException(
                status_code=409,
                detail="an access request is already pending",
            ) from exc
        raise
    return access_request


@router.get("/marketplace/access-requests", response_model=Page)
async def list_marketplace_access_requests(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    if context.organization_id is None:
        raise HTTPException(status_code=403, detail="organization context is required")
    filters: list[Any] = [DataProductAccessRequest.organization_id == context.organization_id]
    if context.roles.isdisjoint(PRODUCT_AUTHORS + ("Reviewer", "Auditor")):
        filters.append(DataProductAccessRequest.requested_by == context.principal_id)
    requests = (
        await session.scalars(
            select(DataProductAccessRequest)
            .where(*filters)
            .order_by(DataProductAccessRequest.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(
        select(func.count()).select_from(DataProductAccessRequest).where(*filters)
    )
    return Page(
        items=[_marketplace_access_request_read(request) for request in requests],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/organizations/{organization_id}/portfolio-analytics/summary",
    response_model=PortfolioAnalyticsSummaryRead,
)
async def portfolio_analytics_summary(
    organization_id: UUID,
    window_days: int = Query(default=30, ge=1, le=365),
    low_quality_threshold: int = Query(default=80, ge=0, le=100),
    top_products_limit: int = Query(default=10, ge=1, le=25),
    context: SecurityContext = Depends(require_roles(*ANALYTICS_READERS)),
    session: AsyncSession = Depends(get_session),
) -> PortfolioAnalyticsSummaryRead:
    enforce_organization(context, organization_id)
    now = datetime.now(UTC)
    window_start = now - timedelta(days=window_days)

    product_lifecycle_counts = _count_map(
        (
            await session.execute(
                select(DataProduct.lifecycle_status, func.count())
                .where(DataProduct.organization_id == organization_id)
                .group_by(DataProduct.lifecycle_status)
            )
        ).all()
    )
    product_version_counts = _count_map(
        (
            await session.execute(
                select(DataProductVersion.status, func.count())
                .where(DataProductVersion.organization_id == organization_id)
                .group_by(DataProductVersion.status)
            )
        ).all()
    )
    contract_counts = _count_map(
        (
            await session.execute(
                select(DataContractVersion.status, func.count())
                .where(DataContractVersion.organization_id == organization_id)
                .group_by(DataContractVersion.status)
            )
        ).all()
    )
    context_product_total = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ContextProduct)
                .where(ContextProduct.organization_id == organization_id)
            )
        )
        or 0
    )
    context_version_counts = _count_map(
        (
            await session.execute(
                select(ContextProductVersion.status, func.count())
                .where(ContextProductVersion.organization_id == organization_id)
                .group_by(ContextProductVersion.status)
            )
        ).all()
    )

    access_status_counts = _count_map(
        (
            await session.execute(
                select(DataProductAccessRequest.status, func.count())
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.created_at >= window_start,
                )
                .group_by(DataProductAccessRequest.status)
            )
        ).all()
    )
    fulfillment_counts = _count_map(
        (
            await session.execute(
                select(DataProductAccessRequest.fulfillment_status, func.count())
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.created_at >= window_start,
                )
                .group_by(DataProductAccessRequest.fulfillment_status)
            )
        ).all()
    )
    active_grants = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(DataProductAccessRequest)
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.status == "APPROVED",
                    or_(
                        DataProductAccessRequest.expires_at.is_(None),
                        DataProductAccessRequest.expires_at > now,
                    ),
                )
            )
        )
        or 0
    )
    grants_expiring = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(DataProductAccessRequest)
                .where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.status == "APPROVED",
                    DataProductAccessRequest.expires_at.is_not(None),
                    and_(
                        DataProductAccessRequest.expires_at >= now,
                        DataProductAccessRequest.expires_at <= now + timedelta(days=30),
                    ),
                )
            )
        )
        or 0
    )

    context_product_reads = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ContextProductConsumptionEdge)
                .where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
            )
        )
        or 0
    )
    unique_context_consumers = int(
        (
            await session.scalar(
                select(func.count(func.distinct(ContextProductConsumptionEdge.principal_id))).where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
            )
        )
        or 0
    )
    mcp_operation_counts = _count_map(
        (
            await session.execute(
                select(McpConsumptionEvidence.operation_kind, func.count())
                .where(
                    McpConsumptionEvidence.organization_id == organization_id,
                    McpConsumptionEvidence.consumed_at >= window_start,
                )
                .group_by(McpConsumptionEvidence.operation_kind)
            )
        ).all()
    )
    total_mcp_operations = sum(mcp_operation_counts.values())
    unique_mcp_consumers = int(
        (
            await session.scalar(
                select(func.count(func.distinct(McpConsumptionEvidence.principal_id))).where(
                    McpConsumptionEvidence.organization_id == organization_id,
                    McpConsumptionEvidence.consumed_at >= window_start,
                )
            )
        )
        or 0
    )
    agent_generation_counts = _count_map(
        (
            await session.execute(
                select(AgentRun.generation_source, func.count())
                .where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.created_at >= window_start,
                )
                .group_by(AgentRun.generation_source)
            )
        ).all()
    )
    agent_runs_total = sum(agent_generation_counts.values())
    unique_agent_principals = int(
        (
            await session.scalar(
                select(func.count(func.distinct(AgentRun.principal_id))).where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.created_at >= window_start,
                )
            )
        )
        or 0
    )
    query_executions = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(QueryExecution)
                .where(
                    QueryExecution.organization_id == organization_id,
                    QueryExecution.created_at >= window_start,
                )
            )
        )
        or 0
    )
    governed_tool_executions = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ToolExecution)
                .where(
                    ToolExecution.organization_id == organization_id,
                    ToolExecution.created_at >= window_start,
                )
            )
        )
        or 0
    )

    published_product_rows = (
        await session.execute(
            select(DataProduct, DataProductVersion)
            .join(DataProductVersion, DataProductVersion.product_id == DataProduct.id)
            .where(
                DataProduct.organization_id == organization_id,
                DataProduct.lifecycle_status == "ACTIVE",
                DataProductVersion.status == "PUBLISHED",
            )
            .order_by(DataProduct.product_key)
        )
    ).all()
    scored_products = [
        version.quality_score
        for _, version in published_product_rows
        if version.quality_score is not None
    ]
    average_quality_score = (
        round(sum(scored_products) / len(scored_products), 2) if scored_products else None
    )
    average_lineage_coverage = (
        round(
            sum(version.lineage_coverage for _, version in published_product_rows)
            / len(published_product_rows),
            2,
        )
        if published_product_rows
        else None
    )

    access_by_version_rows = (
        await session.execute(
            select(
                DataProductAccessRequest.data_product_version_id,
                func.count(),
                func.count().filter(DataProductAccessRequest.status == "APPROVED"),
            )
            .where(
                DataProductAccessRequest.organization_id == organization_id,
                DataProductAccessRequest.created_at >= window_start,
            )
            .group_by(DataProductAccessRequest.data_product_version_id)
        )
    ).all()
    access_by_version = {
        version_id: {"requests": int(total), "approved": int(approved)}
        for version_id, total, approved in access_by_version_rows
    }
    context_reads_by_version = {
        version_id: int(total)
        for version_id, total in (
            await session.execute(
                select(
                    ContextProductConsumptionEdge.context_product_version_id,
                    func.count(),
                )
                .where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
                .group_by(ContextProductConsumptionEdge.context_product_version_id)
            )
        ).all()
    }
    top_products = sorted(
        [
            PortfolioTopProductRead(
                data_product_version_id=version.id,
                product_key=product.product_key,
                name=version.name,
                domain_name=version.domain_name,
                certification_status=version.certification_status,
                quality_score=version.quality_score,
                lineage_coverage=version.lineage_coverage,
                access_request_count=access_by_version.get(version.id, {}).get("requests", 0),
                approved_access_count=access_by_version.get(version.id, {}).get("approved", 0),
                context_read_count=context_reads_by_version.get(
                    version.context_product_version_id, 0
                )
                if version.context_product_version_id is not None
                else 0,
            )
            for product, version in published_product_rows
        ],
        key=lambda item: (
            -(item.access_request_count + item.context_read_count),
            -item.approved_access_count,
            -(item.quality_score or -1),
            item.product_key,
        ),
    )[:top_products_limit]

    return PortfolioAnalyticsSummaryRead(
        generated_at=now,
        window_days=window_days,
        low_quality_threshold=low_quality_threshold,
        lifecycle=PortfolioLifecycleRead(
            data_products_total=sum(product_lifecycle_counts.values()),
            data_products_active=_count_value(product_lifecycle_counts, "ACTIVE"),
            data_products_candidate=_count_value(product_lifecycle_counts, "CANDIDATE"),
            data_products_retired=_count_value(product_lifecycle_counts, "RETIRED"),
            data_product_versions_draft=_count_value(product_version_counts, "DRAFT"),
            data_product_versions_review_required=_count_value(
                product_version_counts, "REVIEW_REQUIRED"
            ),
            data_product_versions_published=_count_value(product_version_counts, "PUBLISHED"),
            data_product_versions_retired=_count_value(product_version_counts, "RETIRED"),
            data_contract_versions_draft=_count_value(contract_counts, "DRAFT"),
            data_contract_versions_review_required=_count_value(
                contract_counts, "REVIEW_REQUIRED"
            ),
            data_contract_versions_published=_count_value(contract_counts, "PUBLISHED"),
            context_products_total=context_product_total,
            context_product_versions_draft=_count_value(context_version_counts, "DRAFT"),
            context_product_versions_review_required=_count_value(
                context_version_counts, "REVIEW_REQUIRED"
            ),
            context_product_versions_published=_count_value(context_version_counts, "PUBLISHED"),
            context_product_versions_deprecated=_count_value(
                context_version_counts, "DEPRECATED"
            ),
        ),
        access=PortfolioAccessRead(
            requests_created=sum(access_status_counts.values()),
            requests_pending=_count_value(access_status_counts, "PENDING"),
            requests_approved=_count_value(access_status_counts, "APPROVED"),
            requests_rejected=_count_value(access_status_counts, "REJECTED"),
            requests_revoked=_count_value(access_status_counts, "REVOKED"),
            requests_expired=_count_value(access_status_counts, "EXPIRED"),
            active_grants=active_grants,
            grants_expiring_within_30_days=grants_expiring,
            fulfillment_pending=_count_value(fulfillment_counts, "PENDING"),
            fulfillment_provisioned=_count_value(fulfillment_counts, "PROVISIONED"),
            fulfillment_failed=_count_value(fulfillment_counts, "FAILED"),
            fulfillment_revoked=_count_value(fulfillment_counts, "REVOKED"),
        ),
        usage=PortfolioUsageRead(
            unique_context_consumers=unique_context_consumers,
            unique_mcp_consumers=unique_mcp_consumers,
            unique_agent_principals=unique_agent_principals,
            context_product_reads=context_product_reads,
            mcp_operations=total_mcp_operations,
            mcp_resource_reads=_count_value(mcp_operation_counts, "RESOURCE"),
            mcp_prompt_reads=_count_value(mcp_operation_counts, "PROMPT"),
            mcp_tool_calls=_count_value(mcp_operation_counts, "TOOL"),
            mcp_control_operations=_count_value(mcp_operation_counts, "CONTROL"),
            agent_runs=agent_runs_total,
            governed_tool_agent_runs=_count_value(agent_generation_counts, "GOVERNED_TOOL"),
            model_gateway_agent_runs=_count_value(agent_generation_counts, "MODEL_GATEWAY"),
            development_override_agent_runs=_count_value(
                agent_generation_counts, "DEVELOPMENT_OVERRIDE"
            ),
            policy_blocked_agent_runs=_count_value(agent_generation_counts, "POLICY_BLOCK"),
            query_executions=query_executions,
            governed_tool_executions=governed_tool_executions,
        ),
        quality=PortfolioQualityRead(
            published_products=len(published_product_rows),
            scored_products=len(scored_products),
            average_quality_score=average_quality_score,
            low_quality_products=sum(
                1 for score in scored_products if int(score) < low_quality_threshold
            ),
            certified_products=sum(
                1
                for _, version in published_product_rows
                if version.certification_status == "CERTIFIED"
            ),
            uncertified_products=sum(
                1
                for _, version in published_product_rows
                if version.certification_status != "CERTIFIED"
            ),
            average_lineage_coverage=average_lineage_coverage,
        ),
        queues=PortfolioQueueRead(
            review_required_data_product_versions=_count_value(
                product_version_counts, "REVIEW_REQUIRED"
            ),
            review_required_data_contract_versions=_count_value(
                contract_counts, "REVIEW_REQUIRED"
            ),
            review_required_context_product_versions=_count_value(
                context_version_counts, "REVIEW_REQUIRED"
            ),
            pending_marketplace_access_requests=_count_value(access_status_counts, "PENDING"),
        ),
        top_products=top_products,
    )


@router.get(
    "/organizations/{organization_id}/portfolio-analytics/trends",
    response_model=PortfolioAnalyticsTrendsRead,
)
async def portfolio_analytics_trends(
    organization_id: UUID,
    window_days: int = Query(default=30, ge=1, le=365),
    bucket_days: int = Query(default=7, ge=1, le=90),
    context: SecurityContext = Depends(require_roles(*ANALYTICS_READERS)),
    session: AsyncSession = Depends(get_session),
) -> PortfolioAnalyticsTrendsRead:
    enforce_organization(context, organization_id)
    now = datetime.now(UTC)
    window_start = now - timedelta(days=window_days)

    access_request_times = list(
        (
            await session.scalars(
                select(DataProductAccessRequest.created_at).where(
                    DataProductAccessRequest.organization_id == organization_id,
                    DataProductAccessRequest.created_at >= window_start,
                )
            )
        ).all()
    )
    context_read_times = list(
        (
            await session.scalars(
                select(ContextProductConsumptionEdge.consumed_at).where(
                    ContextProductConsumptionEdge.organization_id == organization_id,
                    ContextProductConsumptionEdge.consumed_at >= window_start,
                )
            )
        ).all()
    )
    mcp_operation_times = list(
        (
            await session.scalars(
                select(McpConsumptionEvidence.consumed_at).where(
                    McpConsumptionEvidence.organization_id == organization_id,
                    McpConsumptionEvidence.consumed_at >= window_start,
                )
            )
        ).all()
    )
    mcp_tool_call_times = list(
        (
            await session.scalars(
                select(McpConsumptionEvidence.consumed_at).where(
                    McpConsumptionEvidence.organization_id == organization_id,
                    McpConsumptionEvidence.consumed_at >= window_start,
                    McpConsumptionEvidence.operation_kind == "TOOL",
                )
            )
        ).all()
    )
    agent_runs = list(
        (
            await session.execute(
                select(AgentRun.created_at, AgentRun.generation_source).where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.created_at >= window_start,
                )
            )
        ).all()
    )
    query_execution_times = list(
        (
            await session.scalars(
                select(QueryExecution.created_at).where(
                    QueryExecution.organization_id == organization_id,
                    QueryExecution.created_at >= window_start,
                )
            )
        ).all()
    )

    return PortfolioAnalyticsTrendsRead(
        generated_at=now,
        window_days=window_days,
        bucket_days=bucket_days,
        points=_build_portfolio_trend_points(
            now=now,
            window_days=window_days,
            bucket_days=bucket_days,
            access_request_times=access_request_times,
            context_read_times=context_read_times,
            mcp_operation_times=mcp_operation_times,
            mcp_tool_call_times=mcp_tool_call_times,
            agent_runs=agent_runs,
            query_execution_times=query_execution_times,
        ),
    )


@router.post(
    "/marketplace/access-requests/{request_id}/revoke",
    response_model=MarketplaceAccessRequestRead,
)
async def revoke_marketplace_access(
    request_id: UUID,
    context: SecurityContext = Depends(require_roles(*PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> DataProductAccessRequest:
    access_request = await session.get(DataProductAccessRequest, request_id)
    if access_request is None:
        raise HTTPException(status_code=404, detail="access request not found")
    enforce_organization(context, access_request.organization_id)
    if access_request.status != "APPROVED":
        raise HTTPException(status_code=409, detail="only approved access can be revoked")
    access_request.status = "REVOKED"
    access_request.revoked_by = context.principal_id
    access_request.revoked_at = datetime.now(UTC)
    if access_request.fulfillment_status == "PROVISIONED":
        access_request.fulfillment_status = "PENDING"
    record_outbox(
        session,
        organization_id=access_request.organization_id,
        aggregate_type="data_product_access_request",
        aggregate_id=str(access_request.id),
        event_type="data_product.access_revoked.v1",
        payload={"data_product_version_id": str(access_request.data_product_version_id)},
    )
    await session.commit()
    return access_request


@router.post(
    "/marketplace/access-requests/{request_id}/entitlement",
    response_model=MarketplaceAccessRequestRead,
)
async def fulfill_marketplace_entitlement(
    request_id: UUID,
    body: EntitlementOperation,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataProductAccessRequest:
    access_request = await session.get(DataProductAccessRequest, request_id)
    if access_request is None:
        raise HTTPException(status_code=404, detail="access request not found")
    enforce_organization(context, access_request.organization_id)
    if body.action == "PROVISION" and access_request.status != "APPROVED":
        raise HTTPException(status_code=409, detail="only approved access can be provisioned")
    if body.action == "REVOKE" and access_request.status not in {"REVOKED", "EXPIRED"}:
        raise HTTPException(status_code=409, detail="access must be revoked or expired first")
    result = await apply_entitlement(settings, access_request, body.action)
    access_request.fulfillment_status = result.status
    access_request.fulfillment_provider = result.provider
    access_request.fulfillment_reference = result.reference
    access_request.fulfillment_error = result.error
    access_request.fulfilled_at = (
        datetime.now(UTC) if result.status in {"PROVISIONED", "REVOKED"} else None
    )
    correlation_id = get_correlation_id()
    record_audit(
        session,
        context,
        action=f"marketplace.entitlement.{body.action.lower()}",
        resource_type="data_product_access_request",
        resource_id=str(access_request.id),
        outcome="SUCCESS" if result.status != "FAILED" else "FAILURE",
        correlation_id=correlation_id,
        details={"provider": result.provider, "fulfillment_status": result.status},
    )
    record_outbox(
        session,
        organization_id=access_request.organization_id,
        aggregate_type="data_product_access_request",
        aggregate_id=str(access_request.id),
        event_type=f"data_product.entitlement_{result.status.lower()}.v1",
        payload={"action": body.action, "provider": result.provider},
    )
    await session.commit()
    return access_request


def approve_access_request(
    access_request: DataProductAccessRequest,
    *,
    reviewer: str,
    reason: str | None,
    approved: bool,
    now: datetime,
) -> None:
    """Shared governance transition used by the unified review endpoint."""
    if access_request.status != "PENDING":
        raise ValueError("access request is no longer pending")
    access_request.status = "APPROVED" if approved else "REJECTED"
    access_request.decided_by = reviewer
    access_request.decision_reason = reason
    access_request.decided_at = now
    if approved:
        access_request.expires_at = now + timedelta(days=access_request.duration_days)
        access_request.fulfillment_status = "PENDING"
