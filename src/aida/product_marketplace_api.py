import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.entitlements import apply_entitlement
from aida.events import record_audit, record_outbox
from aida.models import (
    ContextProductVersion,
    DataContractVersion,
    DataProduct,
    DataProductAccessRequest,
    DataProductPort,
    DataProductRoleBinding,
    DataProductVersion,
    GovernanceReview,
    MetadataTable,
    Project,
    SemanticModelVersion,
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
)
from aida.schemas import GovernanceReviewRead, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["data-products-marketplace"])

PRODUCT_AUTHORS = ("PlatformAdmin", "DataProductOwner", "DataSteward", "MetadataAdmin")
PRODUCT_READERS = (*PRODUCT_AUTHORS, "Reviewer", "Auditor", "Analyst", "Viewer")
MARKETPLACE_USERS = ("PlatformAdmin", "Analyst", "Viewer", "DataConsumer", "DataScientist")


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
    filters: list[Any] = [
        DataProductVersion.organization_id == context.organization_id,
        DataProductVersion.status == "PUBLISHED",
        DataProduct.lifecycle_status == "ACTIVE",
        DataProductRoleBinding.role_kind == "DISCOVER",
        or_(
            DataProductRoleBinding.role_name == "*",
            DataProductRoleBinding.role_name.in_(context.roles),
        ),
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
        .join(
            DataProductRoleBinding,
            DataProductRoleBinding.data_product_version_id == DataProductVersion.id,
        )
        .where(*filters)
        .distinct()
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
    session.add_all([review, access_request])
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
        raise HTTPException(status_code=409, detail="an access request is already pending") from exc
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
    return Page(items=requests, limit=limit, offset=offset, total=total or 0)


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
