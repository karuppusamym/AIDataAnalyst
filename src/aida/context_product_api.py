import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.context import get_correlation_id

# The read decisions REST shares with GraphQL (R11-GQL01); re-imported so every existing
# caller of these names is unchanged.
from aida.context_product_coverage import (
    PublishedScope,
    load_changes_since_published_counts,
    publication_time,
)
from aida.context_product_read_service import COMPILER_ROLES
from aida.context_product_reads import (
    CONTEXT_PRODUCT_AUTHORS,
    CONTEXT_PRODUCT_LIFECYCLE_READERS,
    CONTEXT_PRODUCT_READERS,
    _can_read_context_product_version,
    _can_read_lifecycle,
    _definition_from_version,
    _enforce_capability_envelope,
    _envelope_listing_clause,
    _product_read,
    _product_scope,
    _version_read,
    _version_scope,
    context_product_listing,
    read_context_product_version,
)
from aida.db import get_session
from aida.domain_service import check_cross_boundary_grant
from aida.envelope_models import MetadataRoutine
from aida.events import record_audit, record_outbox
from aida.models import (
    BusinessDomain,
    ContextProduct,
    ContextProductConsumerBinding,
    ContextProductRoleBinding,
    ContextProductVersion,
    DataSource,
    GlossaryTermVersion,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    MetadataBusinessAnnotation,
    MetadataSchema,
    MetadataTable,
    Project,
    SemanticModelVersion,
)
from aida.ontology_models import OntologyVersion
from aida.resource_scope import load_project_in_scope
from aida.schemas import (
    ContextProductChangesSummaryListRead,
    ContextProductChangesSummaryRead,
    ContextProductConsumerBindingCreate,
    ContextProductConsumerBindingRead,
    ContextProductCreate,
    ContextProductDefinition,
    ContextProductRead,
    ContextProductRoutineOptionRead,
    ContextProductScopeRead,
    ContextProductVersionCreate,
    ContextProductVersionRead,
    ContextProductVersionUpdate,
    GovernanceReviewRead,
    Page,
)
from aida.security import SecurityContext, require_roles

# The names this module re-exports, listed so a type checker treats them as exported
# (implicit re-export is off); what the module defines itself is public as before.
__all__ = [
    "CONTEXT_PRODUCT_AUTHORS",
    "CONTEXT_PRODUCT_LIFECYCLE_READERS",
    "CONTEXT_PRODUCT_READERS",
    "_can_read_context_product_version",
    "_can_read_lifecycle",
    "_definition_from_version",
    "_enforce_capability_envelope",
    "_envelope_listing_clause",
    "_product_read",
    "_product_scope",
    "_version_read",
    "_version_scope",
    "context_product_listing",
    "read_context_product_version",
]

router = APIRouter(prefix="/v1", tags=["context-products"])


def context_product_fingerprint(body: ContextProductDefinition) -> str:
    definition = body.model_dump(mode="json")
    # R11-FP12/FP09: a reference group added after products existed is left out while empty,
    # so a definition naming none fingerprints exactly as it did before the group existed and
    # no stored fingerprint -- or etag built on one -- goes stale.
    for late_group in ("routine_ids", "ontology_version_ids"):
        if not definition.get(late_group):
            definition.pop(late_group, None)
    payload = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def replace_context_product_role_bindings(
    session: AsyncSession, version: ContextProductVersion
) -> None:
    """Public: reused by `aida.studio_context_product` (ST-A7) to materialize a
    Studio CONTEXT_PRODUCT change-set item through this exact same code path,
    rather than a parallel reimplementation."""
    await session.execute(
        delete(ContextProductRoleBinding).where(
            ContextProductRoleBinding.context_product_version_id == version.id
        )
    )
    for role_name in version.allowed_consumer_roles:
        session.add(
            ContextProductRoleBinding(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                role_name=role_name,
            )
        )


def apply_context_product_definition(
    version: ContextProductVersion, body: ContextProductDefinition
) -> ContextProductVersion:
    """Public: reused by `aida.studio_context_product` (ST-A7) -- see
    `replace_context_product_role_bindings` above."""
    payload = body.model_dump(mode="json")
    version.name = body.name
    version.description = body.description
    version.purpose = body.purpose
    version.owner_type = body.owner_type
    version.owner_principal = body.owner_principal
    version.table_ids = payload["table_ids"]
    version.semantic_model_version_ids = payload["semantic_model_version_ids"]
    version.glossary_term_version_ids = payload["glossary_term_version_ids"]
    version.eligible_tool_version_ids = payload["eligible_tool_version_ids"]
    version.routine_ids = payload["routine_ids"]
    version.ontology_version_ids = payload["ontology_version_ids"]
    version.allowed_consumer_roles = list(body.allowed_consumer_roles)
    version.lineage_depth = body.lineage_depth
    version.quality_requirements = payload["quality_requirements"]
    version.policy_summary = payload["policy_summary"]
    version.support_window_days = body.support_window_days
    version.fingerprint = context_product_fingerprint(body)
    return version


async def _require_exact_ids(
    session: AsyncSession,
    statement: Select[tuple[UUID]],
    expected: list[UUID],
    label: str,
) -> None:
    if not expected:
        return
    found = set((await session.scalars(statement)).all())
    if found != set(expected):
        raise HTTPException(
            status_code=422,
            detail=f"one or more {label} are not available in the approved product scope",
        )


async def validate_context_product_references(
    session: AsyncSession,
    project: Project,
    body: ContextProductDefinition,
) -> None:
    """Public: reused by `aida.studio_context_product` (ST-A7) -- see
    `replace_context_product_role_bindings` above."""
    await _require_exact_ids(
        session,
        select(MetadataTable.id)
        .join(DataSource, DataSource.id == MetadataTable.datasource_id)
        .where(
            MetadataTable.id.in_(body.table_ids),
            MetadataTable.organization_id == project.organization_id,
            MetadataTable.status == "ACTIVE",
            DataSource.project_id == project.id,
        ),
        body.table_ids,
        "tables",
    )
    await _require_exact_ids(
        session,
        select(SemanticModelVersion.id).where(
            SemanticModelVersion.id.in_(body.semantic_model_version_ids),
            SemanticModelVersion.organization_id == project.organization_id,
            SemanticModelVersion.project_id == project.id,
            SemanticModelVersion.status == "PUBLISHED",
        ),
        body.semantic_model_version_ids,
        "semantic model versions",
    )
    await _require_exact_ids(
        session,
        select(GlossaryTermVersion.id).where(
            GlossaryTermVersion.id.in_(body.glossary_term_version_ids),
            GlossaryTermVersion.organization_id == project.organization_id,
            GlossaryTermVersion.status == "APPROVED",
        ),
        body.glossary_term_version_ids,
        "glossary term versions",
    )
    await _require_exact_ids(
        session,
        select(GovernedToolVersion.id)
        .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
        .where(
            GovernedToolVersion.id.in_(body.eligible_tool_version_ids),
            GovernedToolVersion.organization_id == project.organization_id,
            GovernedToolVersion.status == "PUBLISHED",
            GovernedTool.project_id == project.id,
        ),
        body.eligible_tool_version_ids,
        "governed tool versions",
    )
    await _require_exact_ids(
        session,
        select(MetadataRoutine.id)
        .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
        .where(
            MetadataRoutine.id.in_(body.routine_ids),
            MetadataRoutine.organization_id == project.organization_id,
            MetadataRoutine.status == "ACTIVE",
            DataSource.project_id == project.id,
        ),
        body.routine_ids,
        "routines",
    )
    await _require_exact_ids(
        session,
        select(OntologyVersion.id).where(
            OntologyVersion.id.in_(body.ontology_version_ids),
            OntologyVersion.organization_id == project.organization_id,
            OntologyVersion.status == "APPROVED",
        ),
        body.ontology_version_ids,
        "ontology versions",
    )


@router.post(
    "/projects/{project_id}/context-products",
    response_model=ContextProductRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_context_product(
    project_id: UUID,
    body: ContextProductCreate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductRead:
    project = await load_project_in_scope(session, project_id, context)
    await validate_context_product_references(session, project, body)
    existing = await session.scalar(
        select(ContextProduct.id).where(
            ContextProduct.organization_id == project.organization_id,
            ContextProduct.product_key == body.product_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="context product key already exists")

    product = ContextProduct(
        organization_id=project.organization_id,
        project_id=project.id,
        product_key=body.product_key,
        created_by=context.principal_id,
    )
    session.add(product)
    await session.flush()
    version = apply_context_product_definition(
        ContextProductVersion(
            organization_id=project.organization_id,
            product_id=product.id,
            version=1,
            created_by=context.principal_id,
        ),
        body,
    )
    session.add(version)
    await session.flush()
    await replace_context_product_role_bindings(session, version)
    audit_context = replace(context, organization_id=project.organization_id)
    record_audit(
        session,
        audit_context,
        action="context_product.create",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"product_key": product.product_key, "version": version.version},
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version.id),
        event_type="context.product_draft_created.v1",
        payload={
            "context_product_id": str(product.id),
            "context_product_version_id": str(version.id),
            "version": version.version,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="context product allocation conflict") from exc
    return _product_read(product, version)


@router.get(
    "/projects/{project_id}/context-product-routine-options",
    response_model=list[ContextProductRoutineOptionRead],
)
async def list_context_product_routine_options(
    project_id: UUID,
    limit: int = Query(default=200, ge=1, le=500),
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> list[ContextProductRoutineOptionRead]:
    """R11-FP12: the routines a draft in this project may name. The filter is the one
    `validate_context_product_references` applies -- ACTIVE, on this project's own datasources
    -- so the picker never offers a routine the create call would refuse."""
    project = await load_project_in_scope(session, project_id, context)
    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name, DataSource.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
            .where(
                MetadataRoutine.organization_id == project.organization_id,
                MetadataRoutine.status == "ACTIVE",
                DataSource.project_id == project.id,
            )
            .order_by(MetadataSchema.name, MetadataRoutine.name, MetadataRoutine.signature)
            .limit(limit)
        )
    ).all()
    return [
        ContextProductRoutineOptionRead(
            id=routine.id,
            datasource_id=routine.datasource_id,
            datasource_name=datasource_name,
            schema_name=schema_name,
            name=routine.name,
            routine_type=routine.routine_type,
            signature=routine.signature,
        )
        for routine, schema_name, datasource_name in rows
    ]


@router.get("/projects/{project_id}/context-products", response_model=Page)
async def list_context_products(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    # Only the products this caller could ask a question through: PUBLISHED, and naming a
    # consumer role the caller holds. False -- the lifecycle view -- is what every existing
    # caller gets. A plain literal default rather than `Query(default=False)` (the style the
    # paging parameters above use for their bounds) because this handler is also called
    # directly by tests: a `Query` object as the default would arrive as a truthy value on
    # every such call, silently switching them to the askable view.
    askable: bool = False,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    listing = await context_product_listing(
        session, context, project_id=project_id, askable=askable
    )
    rows = (
        await session.execute(
            listing.statement.order_by(ContextProduct.product_key).limit(limit).offset(offset)
        )
    ).all()
    total = await session.scalar(listing.count_statement)
    return Page(
        items=[_product_read(product, version) for product, version in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/projects/{project_id}/context-products/changes-since-published",
    response_model=ContextProductChangesSummaryListRead,
)
async def list_context_product_changes_since_published(
    project_id: UUID,
    product_id: UUID | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    context: SecurityContext = Depends(require_roles(*COMPILER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductChangesSummaryListRead:
    """R11-FP12: what has moved under each product's latest version, for a whole project.

    The same reading `Query.contextProductCoverage` gives for one version, counted for every
    product a caller can see, in a fixed number of queries
    (`load_changes_since_published_counts`). It exists so a list can show a product as stale
    without a person pressing a button per row, and so the screens that only ever list products
    -- the agent gateway's exposure list, Ask's picker, the rollout version list -- can show the
    same thing.

    **Why its own route, and not a field on the product list.** The product list admits
    `CONTEXT_PRODUCT_READERS` (Viewer and Auditor among them); a coverage reading admits
    `COMPILER_ROLES`, and the compile route and the GraphQL coverage field both refuse the
    others. Carrying the reading on the list row would hand it to roles those doors refuse, so
    it is a separate read with the coverage roles, and a screen asks for it only when the
    session holds them.

    `product_id` narrows it to one product and answers for EVERY version of it, newest first,
    rather than one row per product: what the rollout screen needs, where the choice is between a
    product's versions and an older one can have drifted further than the latest. The product is
    read through the same listing, so a product the caller may not list is not answered for.

    Visibility is the product list's own (`context_product_listing`), so a caller is told about
    exactly the products it may already list, in the same order, and a project in another
    organization is refused there, before any coverage is read.
    """
    listing = await context_product_listing(
        session, context, project_id=project_id, askable=False
    )
    if product_id is not None:
        product = await session.scalar(
            listing.statement.with_only_columns(ContextProduct).where(
                ContextProduct.id == product_id
            )
        )
        if product is None:
            raise HTTPException(status_code=404, detail="context product not found")
        versions = (
            await session.scalars(
                select(ContextProductVersion)
                .where(ContextProductVersion.product_id == product.id)
                .order_by(ContextProductVersion.version.desc())
                .limit(limit + 1)
            )
        ).all()
        rows: list[tuple[ContextProduct, ContextProductVersion]] = [
            (product, version) for version in versions
        ]
    else:
        rows = [
            (listed, version)
            for listed, version in (
                await session.execute(
                    listing.statement.order_by(ContextProduct.product_key).limit(limit + 1)
                )
            ).all()
        ]
    truncated = len(rows) > limit
    rows = rows[:limit]
    scopes = [
        PublishedScope(
            version_id=version.id,
            table_ids=version.table_ids or [],
            routine_ids=version.routine_ids or [],
            since=publication_time(version),
        )
        for _product, version in rows
        if version is not None
    ]
    # The products' own organization: every row is from the one project the listing admitted,
    # and a platform-level caller's context need not name an organization at all.
    counts = (
        await load_changes_since_published_counts(session, rows[0][0].organization_id, scopes)
        if rows
        else {}
    )
    return ContextProductChangesSummaryListRead(
        project_id=project_id,
        generated_at=datetime.now(UTC),
        truncated=truncated,
        items=[
            ContextProductChangesSummaryRead(
                product_id=product.id,
                version_id=version.id,
                version=version.version,
                status=version.status,
                changed_subjects=counts.get(version.id),
            )
            for product, version in rows
            if version is not None
        ],
    )


@router.get("/context-products/{product_id}/versions", response_model=Page)
async def list_context_product_versions(
    product_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    product = await _product_scope(session, product_id, context)
    # AR-06 (R11-C6): this listing is one product's versions, so it is the
    # same envelope decision as reading one of them -- and the cheapest
    # remaining door to "does product B exist and what versions does it have"
    # for an agent whose envelope names only product A.
    await _enforce_capability_envelope(session, context, product)
    statement = select(ContextProductVersion)
    count_statement = select(func.count()).select_from(ContextProductVersion)
    visibility: tuple[ColumnElement[bool], ...] = ()
    if not _can_read_lifecycle(context):
        # EXISTS rather than a join plus `.distinct()`: DISTINCT over the whole version row
        # includes its `JSON` id lists, which PostgreSQL cannot compare (a 500 for every
        # non-lifecycle reader), and the count joined without DISTINCT, so a version bound to
        # two roles the caller holds was counted twice. One row per version needs neither.
        visibility = (
            ContextProductVersion.status == "PUBLISHED",
            exists().where(
                ContextProductRoleBinding.context_product_version_id
                == ContextProductVersion.id,
                ContextProductRoleBinding.organization_id == product.organization_id,
                ContextProductRoleBinding.role_name.in_(context.roles),
            ),
        )
    versions = (
        await session.scalars(
            statement
            .where(ContextProductVersion.product_id == product.id, *visibility)
            .order_by(ContextProductVersion.version.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(
        count_statement.where(ContextProductVersion.product_id == product.id, *visibility)
    )
    return Page(
        items=[_version_read(product, version) for version in versions],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/context-product-versions/{version_id}", response_model=ContextProductVersionRead
)
async def get_context_product_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductVersionRead:
    product, version = await read_context_product_version(
        session, context, version_id, channel="REST"
    )
    return _version_read(product, version)


@router.get(
    "/context-product-versions/{version_id}/scope",
    response_model=ContextProductScopeRead,
)
async def get_context_product_version_scope(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductScopeRead:
    """Compose both ADR-0017 SS9 axes for this version so an agent or MCP
    client can see, before it retrieves anything, which tenancy boundaries and
    which business domains the context product actually spans -- and whether
    the product's own domain is missing a cross_boundary_grant into any of
    them. Read-only and side-effect-free (no consumption edge, no purpose/
    quality gate) -- it describes scope, it is not itself a retrieval.
    """
    product, version = await _version_scope(session, version_id, context)
    # AR-06 (R11-C6): the scope composition is still this product, so the
    # envelope still bounds who may see how far it reaches.
    await _enforce_capability_envelope(session, context, product, version)
    if not _can_read_context_product_version(context, version):
        raise HTTPException(status_code=404, detail="context product version not found")
    project = await session.get(Project, product.project_id)
    if project is None:
        raise HTTPException(status_code=409, detail="context product project is unavailable")
    product_data_domain_id = project.data_domain_id

    table_ids = [UUID(value) for value in version.table_ids]
    tables_by_id: dict[UUID, MetadataTable] = {}
    domain_ids: set[UUID] = set()
    if table_ids:
        rows = (
            await session.execute(
                select(MetadataTable, DataSource.data_domain_id)
                .join(DataSource, DataSource.id == MetadataTable.datasource_id)
                .where(MetadataTable.id.in_(table_ids))
            )
        ).all()
        for table, data_domain_id in rows:
            tables_by_id[table.id] = table
            domain_ids.add(data_domain_id)
    unresolved_table_ids = [tid for tid in table_ids if tid not in tables_by_id]

    ungranted_domain_ids: list[UUID] = []
    for domain_id in domain_ids:
        if domain_id == product_data_domain_id:
            continue
        allowed = await check_cross_boundary_grant(
            session,
            version.organization_id,
            domain_id,
            product_data_domain_id,
        )
        if not allowed:
            ungranted_domain_ids.append(domain_id)

    business_domain_names: set[str] = set()
    if tables_by_id:
        annotation_rows = (
            await session.execute(
                select(BusinessDomain.display_name)
                .join(
                    MetadataBusinessAnnotation,
                    MetadataBusinessAnnotation.domain_id == BusinessDomain.id,
                )
                .where(MetadataBusinessAnnotation.table_id.in_(tables_by_id.keys()))
            )
        ).all()
        business_domain_names = {row[0] for row in annotation_rows}

    return ContextProductScopeRead(
        context_product_version_id=version.id,
        product_data_domain_id=product_data_domain_id,
        data_domain_ids=sorted(domain_ids, key=str),
        ungranted_data_domain_ids=sorted(ungranted_domain_ids, key=str),
        business_domain_names=sorted(business_domain_names),
        cross_domain=len(domain_ids - {product_data_domain_id}) > 0,
        table_count=len(tables_by_id),
        unresolved_table_ids=unresolved_table_ids,
    )


@router.post(
    "/context-products/{product_id}/versions",
    response_model=ContextProductVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_context_product_version(
    product_id: UUID,
    body: ContextProductVersionCreate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductVersionRead:
    product = await _product_scope(session, product_id, context)
    if product.lifecycle_status != "ACTIVE":
        raise HTTPException(status_code=409, detail="context product is not active")
    project = await load_project_in_scope(session, product.project_id, context)
    await validate_context_product_references(session, project, body)
    if body.based_on_version_id is not None:
        base = await session.get(ContextProductVersion, body.based_on_version_id)
        if base is None or base.product_id != product.id:
            raise HTTPException(status_code=422, detail="base context product version is invalid")
    latest = await session.scalar(
        select(func.max(ContextProductVersion.version)).where(
            ContextProductVersion.product_id == product.id
        )
    )
    version = apply_context_product_definition(
        ContextProductVersion(
            organization_id=product.organization_id,
            product_id=product.id,
            version=(latest or 0) + 1,
            created_by=context.principal_id,
            based_on_version_id=body.based_on_version_id,
        ),
        body,
    )
    session.add(version)
    await session.flush()
    await replace_context_product_role_bindings(session, version)
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.create",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"product_key": product.product_key, "version": version.version},
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version.id),
        event_type="context.product_draft_created.v1",
        payload={
            "context_product_id": str(product.id),
            "context_product_version_id": str(version.id),
            "version": version.version,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="context product version conflict") from exc
    return _version_read(product, version)


@router.put(
    "/context-product-versions/{version_id}", response_model=ContextProductVersionRead
)
async def update_context_product_version(
    version_id: UUID,
    body: ContextProductVersionUpdate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductVersionRead:
    product, version = await _version_scope(session, version_id, context)
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft context products can be changed")
    project = await load_project_in_scope(session, product.project_id, context)
    await validate_context_product_references(session, project, body)
    apply_context_product_definition(version, body)
    await replace_context_product_role_bindings(session, version)
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.update",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"fingerprint": version.fingerprint, "version": version.version},
    )
    await session.commit()
    return _version_read(product, version)


@router.post(
    "/context-product-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_context_product_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    product, version = await _version_scope(session, version_id, context)
    if version.status == "REVIEW_REQUIRED":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
                GovernanceReview.object_id == str(version.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing is not None:
            return existing
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft context product can be submitted")
    project = await load_project_in_scope(session, product.project_id, context)
    await validate_context_product_references(session, project, _definition_from_version(version))
    review = GovernanceReview(
        organization_id=product.organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    version.status = "REVIEW_REQUIRED"
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"context_product_version_id": str(version.id)},
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
        },
    )
    await session.commit()
    return review


@router.post(
    "/context-product-versions/{version_id}/deprecate",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_context_product_deprecation(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    product, version = await _version_scope(session, version_id, context)
    existing = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
            GovernanceReview.object_id == str(version.id),
            GovernanceReview.requested_action == "DEPRECATE",
            GovernanceReview.status == "PENDING",
        )
    )
    if existing is not None:
        return existing
    if version.status not in ("PUBLISHED", "SUPPORTED"):
        raise HTTPException(status_code=409, detail="only a published context product can retire")
    review = GovernanceReview(
        organization_id=product.organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action="DEPRECATE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.deprecation_request",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"context_product_version_id": str(version.id)},
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
            "requested_action": review.requested_action,
        },
    )
    await session.commit()
    return review


# --- AT-7(b): consumer-binding registry (staged rollout) --------------------


def _binding_read(
    binding: ContextProductConsumerBinding, bound_version: ContextProductVersion
) -> ContextProductConsumerBindingRead:
    return ContextProductConsumerBindingRead(
        id=binding.id,
        organization_id=binding.organization_id,
        product_id=binding.product_id,
        consumer_principal_id=binding.consumer_principal_id,
        bound_version_id=binding.bound_version_id,
        bound_version_number=bound_version.version,
        created_by=binding.created_by,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


@router.put(
    "/context-products/{product_id}/bindings/{consumer_principal_id}",
    response_model=ContextProductConsumerBindingRead,
)
async def set_context_product_consumer_binding(
    product_id: UUID,
    consumer_principal_id: str,
    body: ContextProductConsumerBindingCreate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductConsumerBindingRead:
    """Pin (or move) one named consumer onto one specific version -- staged
    rollout under explicit operator control, not a percentage/weight split.

    Idempotent by (product, consumer): calling this again for an
    already-bound consumer moves the existing binding rather than creating a
    second one, matching `PUT`'s replace-in-place semantics.
    """
    product = await _product_scope(session, product_id, context)
    bound_version = await session.get(ContextProductVersion, body.bound_version_id)
    if bound_version is None or bound_version.product_id != product.id:
        raise HTTPException(
            status_code=422, detail="bound_version_id is not a version of this context product"
        )
    binding = await session.scalar(
        select(ContextProductConsumerBinding).where(
            ContextProductConsumerBinding.product_id == product.id,
            ContextProductConsumerBinding.consumer_principal_id == consumer_principal_id,
        )
    )
    is_new = binding is None
    if binding is None:
        binding = ContextProductConsumerBinding(
            organization_id=product.organization_id,
            product_id=product.id,
            consumer_principal_id=consumer_principal_id,
            bound_version_id=bound_version.id,
            created_by=context.principal_id,
        )
        session.add(binding)
    else:
        binding.bound_version_id = bound_version.id
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.consumer_binding.set",
        resource_type="context_product_consumer_binding",
        resource_id=str(binding.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "product_key": product.product_key,
            "consumer_principal_id": consumer_principal_id,
            "bound_version": bound_version.version,
            "created": is_new,
        },
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="context_product_consumer_binding",
        aggregate_id=str(binding.id),
        event_type="context.product_consumer_binding_set.v1",
        payload={
            "product_key": product.product_key,
            "consumer_principal_id": consumer_principal_id,
            "bound_version": bound_version.version,
        },
    )
    await session.commit()
    return _binding_read(binding, bound_version)


@router.get(
    "/context-products/{product_id}/bindings",
    response_model=Page,
)
async def list_context_product_consumer_bindings(
    product_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_LIFECYCLE_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    product = await _product_scope(session, product_id, context)
    # AR-06 (R11-C6): the fourth door to one named product, and the same
    # per-product decision as the three above. The governance roles this
    # listing is restricted to make it a *narrower* door, not an exempt one:
    # a contracted agent holding `DataSteward` has no more business
    # administering a product outside its envelope than consuming one.
    await _enforce_capability_envelope(session, context, product)
    statement = (
        select(ContextProductConsumerBinding, ContextProductVersion)
        .join(
            ContextProductVersion,
            ContextProductVersion.id == ContextProductConsumerBinding.bound_version_id,
        )
        .where(ContextProductConsumerBinding.product_id == product.id)
    )
    count = await session.scalar(
        select(func.count()).select_from(
            select(ContextProductConsumerBinding.id)
            .where(ContextProductConsumerBinding.product_id == product.id)
            .subquery()
        )
    )
    rows = (
        await session.execute(
            statement.order_by(ContextProductConsumerBinding.consumer_principal_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_binding_read(binding, version) for binding, version in rows],
        limit=limit,
        offset=offset,
        total=count or 0,
    )


@router.delete(
    "/context-products/{product_id}/bindings/{consumer_principal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_context_product_consumer_binding(
    product_id: UUID,
    consumer_principal_id: str,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Unpin a consumer -- their next unversioned read falls back to whatever
    is currently PUBLISHED, exactly as if they had never been bound."""
    product = await _product_scope(session, product_id, context)
    binding = await session.scalar(
        select(ContextProductConsumerBinding).where(
            ContextProductConsumerBinding.product_id == product.id,
            ContextProductConsumerBinding.consumer_principal_id == consumer_principal_id,
        )
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="context product consumer binding not found")
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.consumer_binding.delete",
        resource_type="context_product_consumer_binding",
        resource_id=str(binding.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "product_key": product.product_key,
            "consumer_principal_id": consumer_principal_id,
        },
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="context_product_consumer_binding",
        aggregate_id=str(binding.id),
        event_type="context.product_consumer_binding_removed.v1",
        payload={
            "product_key": product.product_key,
            "consumer_principal_id": consumer_principal_id,
        },
    )
    await session.delete(binding)
    await session.commit()
