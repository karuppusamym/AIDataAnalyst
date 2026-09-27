"""Context product reads: the decisions REST and GraphQL share (R11-GQL01).

Moved out of `aida.context_product_api` unchanged, so the GraphQL facade can make the decision
the REST route makes without importing a router: the listing's filter and visibility
(`context_product_listing`) and the governed version read (`read_context_product_version`), with
the checks they depend on -- tenant, the agent capability envelope, role and pinned-version
eligibility, retirement, purpose and quality. `aida.context_product_api` re-imports every name,
so its routes and callers are untouched.

Refusals are the `HTTPException`s the routes have always raised; the GraphQL layer maps them to
its field codes.
"""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import Select, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.agent_contracts import (
    REASON_CONTEXT_PRODUCT_VIOLATION,
    AgentContractValidationError,
    context_product_violation,
    load_contract_for_principal,
    parse_capability_envelope,
)
from aida.consumption_lineage import ConsumptionEdge, record_consumption
from aida.context_product_policy import (
    can_serve_pinned_version,
    current_published_version_number,
    evaluate_context_product_purpose,
    evaluate_context_product_quality_from_db,
    is_version_retired,
    was_previously_authorized_consumer,
)
from aida.events import record_audit, record_outbox
from aida.models import (
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductRoleBinding,
    ContextProductVersion,
)
from aida.resource_scope import load_project_in_scope
from aida.schemas import ContextProductDefinition, ContextProductRead, ContextProductVersionRead
from aida.security import SecurityContext, enforce_organization
from atlas.platform.context import get_correlation_id

CONTEXT_PRODUCT_AUTHORS = ("PlatformAdmin", "SemanticAdmin", "DataSteward")
CONTEXT_PRODUCT_READERS = (
    "PlatformAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Auditor",
    "Viewer",
)
CONTEXT_PRODUCT_LIFECYCLE_READERS = frozenset(
    {*CONTEXT_PRODUCT_AUTHORS, "Reviewer", "Auditor"}
)


def _can_read_context_product_version(
    context: SecurityContext, version: ContextProductVersion
) -> bool:
    """AT-7(a): a version-pinned read is not gated to PUBLISHED alone any
    more -- a SUPPORTED version still within its support window reads
    exactly like PUBLISHED for an otherwise-eligible consumer."""
    if not context.roles.isdisjoint(CONTEXT_PRODUCT_LIFECYCLE_READERS):
        return True
    return can_serve_pinned_version(version) and not context.roles.isdisjoint(
        version.allowed_consumer_roles
    )


def _can_read_lifecycle(context: SecurityContext) -> bool:
    return not context.roles.isdisjoint(CONTEXT_PRODUCT_LIFECYCLE_READERS)


def _definition_from_version(version: ContextProductVersion) -> ContextProductDefinition:
    return ContextProductDefinition.model_validate(
        {
            "name": version.name,
            "description": version.description,
            "purpose": version.purpose,
            "owner_type": version.owner_type,
            "owner_principal": version.owner_principal,
            "table_ids": version.table_ids,
            "semantic_model_version_ids": version.semantic_model_version_ids,
            "glossary_term_version_ids": version.glossary_term_version_ids,
            "eligible_tool_version_ids": version.eligible_tool_version_ids,
            "routine_ids": version.routine_ids or [],
            "ontology_version_ids": version.ontology_version_ids or [],
            "allowed_consumer_roles": version.allowed_consumer_roles,
            "lineage_depth": version.lineage_depth,
            "quality_requirements": version.quality_requirements,
            "policy_summary": version.policy_summary,
            "support_window_days": version.support_window_days,
        }
    )


def _version_read(
    product: ContextProduct, version: ContextProductVersion
) -> ContextProductVersionRead:
    return ContextProductVersionRead(
        **_definition_from_version(version).model_dump(),
        id=version.id,
        organization_id=version.organization_id,
        product_id=version.product_id,
        product_key=product.product_key,
        version=version.version,
        status=version.status,
        fingerprint=version.fingerprint,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        published_at=version.published_at,
        based_on_version_id=version.based_on_version_id,
        created_at=version.created_at,
        updated_at=version.updated_at,
        superseded_at=version.superseded_at,
        support_window_ends_at=version.support_window_ends_at,
        superseded_by_version_id=version.superseded_by_version_id,
    )


def _product_read(
    product: ContextProduct, latest_version: ContextProductVersion
) -> ContextProductRead:
    return ContextProductRead(
        id=product.id,
        organization_id=product.organization_id,
        project_id=product.project_id,
        product_key=product.product_key,
        lifecycle_status=product.lifecycle_status,
        created_by=product.created_by,
        latest_version=_version_read(product, latest_version),
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


async def _product_scope(
    session: AsyncSession, product_id: UUID, context: SecurityContext
) -> ContextProduct:
    product = await session.get(ContextProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="context product not found")
    enforce_organization(context, product.organization_id)
    return product


async def _version_scope(
    session: AsyncSession, version_id: UUID, context: SecurityContext
) -> tuple[ContextProduct, ContextProductVersion]:
    version = await session.get(ContextProductVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="context product version not found")
    enforce_organization(context, version.organization_id)
    product = await session.get(ContextProduct, version.product_id)
    if product is None or product.organization_id != version.organization_id:
        raise HTTPException(status_code=409, detail="context product identity is unavailable")
    return product, version


async def _enforce_capability_envelope(
    session: AsyncSession,
    context: SecurityContext,
    product: ContextProduct,
    version: ContextProductVersion | None = None,
) -> None:
    """AR-06 (R11-C6): a contracted agent's capability envelope, on the REST
    door too.

    `mcp_server` resolves the caller's contract and refuses a context product
    `capability_envelope.context_product_ids` does not name -- on
    `tools/list`, `tools/call`, `resources/read` and `prompts/get`. REST is
    the same product through a different transport, and it only checked
    roles. So a contracted agent whose envelope named product A could read
    product B by asking this API for it instead of asking MCP: one product,
    two doors, one of them locked. Which door an agent knocks on is not a
    governance boundary.

    Deliberately *not* symmetric with the MCP fix in one respect: the AR-10
    outbound screening that accompanies it there is not applied here. REST
    serves the UI, and the screening design keeps quarantined text visible to
    a human looking at the object -- withholding it here would hide an
    author's own prose from the author. The envelope is about *who may reach
    the product*; the screening is about *what text leaves for a model*. Only
    the first belongs on this path.

    A human principal has no contract and is unaffected (`None` from
    `load_contract_for_principal`), so this never becomes a second role
    check. An `agent:`/`AGENT` identity with no contract, or with more than
    one, is refused -- ambiguity must not disable the restriction.

    Both denials raise the identical anti-enumeration 404 the role denial
    beside them already raises, and both are audited: an operator needs to
    see that an agent reached past its envelope, while the agent must not
    learn from the response that the product exists.

    `version` is the version being read where there is one, and `None` on the
    version *listing*, which is a per-product decision. The envelope names
    products, never versions, so the answer does not depend on it -- it only
    decides which object the denial is recorded against.
    """
    resource_type = "context_product_version" if version is not None else "context_product"
    resource_id = str(version.id) if version is not None else str(product.id)
    evidence: dict[str, object] = {"product_key": product.product_key}
    if version is not None:
        evidence["version"] = version.version
    try:
        contract = await load_contract_for_principal(
            session,
            organization_id=product.organization_id,
            agent_principal_id=context.principal_id,
            principal_type=context.principal_type,
        )
    except AgentContractValidationError as exc:
        record_audit(
            session,
            context,
            action="context_product.read.agent_contract_denied",
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details={**evidence, "reason": exc.code},
        )
        await session.commit()
        raise HTTPException(
            status_code=404, detail="context product version not found"
        ) from exc
    if contract is None:
        return
    if (
        context_product_violation(
            contract, product_key=product.product_key, product_id=str(product.id)
        )
        is None
    ):
        return
    record_audit(
        session,
        context,
        action="context_product.read.envelope_denied",
        resource_type=resource_type,
        resource_id=resource_id,
        outcome="DENIED",
        correlation_id=get_correlation_id(),
        details={**evidence, "reason": REASON_CONTEXT_PRODUCT_VIOLATION},
    )
    await session.commit()
    raise HTTPException(status_code=404, detail="context product version not found")


async def _envelope_listing_clause(
    session: AsyncSession,
    context: SecurityContext,
    *,
    organization_id: UUID,
    project_id: UUID,
) -> ColumnElement[bool] | None:
    """R11-C6: a contracted agent lists only the context products it may reach.

    Every single-product door checks the envelope -- the version read, its
    scope, the version listing (`_enforce_capability_envelope`). The
    project-level listing did not, so an agent whose envelope named product A
    could learn that products B and C exist, and what they are called, just by
    listing the project. That is the enumeration the per-product 404s exist to
    prevent, answered one level up.

    A *filter*, not a gate, because a listing has no single product to refuse:
    the agent sees the products its envelope names and nothing else, and the
    count is filtered with the rows so the total cannot leak how many were
    hidden. Either identifier matches, as `context_product_violation` allows.

    Fail closed without a signal: an agent identity whose contract cannot be
    resolved, or whose envelope cannot be parsed, is allowed nothing and sees
    an empty page -- the same answer an empty project gives -- while the
    refusal is audited so an operator can see it. A human principal holds no
    contract and gets `None`: this never becomes a second role check.
    """
    try:
        contract = await load_contract_for_principal(
            session,
            organization_id=organization_id,
            agent_principal_id=context.principal_id,
            principal_type=context.principal_type,
        )
    except AgentContractValidationError as exc:
        record_audit(
            session,
            context,
            action="context_product.list.agent_contract_denied",
            resource_type="project",
            resource_id=str(project_id),
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details={"reason": exc.code},
        )
        await session.commit()
        return false()
    if contract is None:
        return None
    try:
        envelope = parse_capability_envelope(dict(contract.capability_envelope or {}))
    except AgentContractValidationError:
        return false()
    allowed = set(envelope.context_product_ids)
    if not allowed:
        return false()
    product_ids: list[UUID] = []
    for value in allowed:
        try:
            product_ids.append(UUID(value))
        except ValueError:
            continue
    by_key = ContextProduct.product_key.in_(allowed)
    return or_(by_key, ContextProduct.id.in_(product_ids)) if product_ids else by_key


@dataclass(frozen=True, slots=True)
class ProductListing:
    """What `GET /v1/projects/{project_id}/context-products` lists for a caller: rows of
    (product, version) and their count, filtered and made visible exactly as that route
    decides. The caller orders and pages -- REST by offset, GraphQL by keyset -- so the
    decision cannot differ between them."""

    statement: Select[tuple[ContextProduct, ContextProductVersion]]
    count_statement: Select[tuple[int]]


async def context_product_listing(
    session: AsyncSession,
    context: SecurityContext,
    *,
    project_id: UUID,
    askable: bool,
) -> ProductListing:
    """The project's products this caller may list: its lifecycle view, or with `askable` only
    the published ones a consumer role of theirs may ask through -- and for a contracted agent
    only those its envelope names (`_envelope_listing_clause`)."""
    project = await load_project_in_scope(session, project_id, context)
    filters: tuple[ColumnElement[bool], ...] = (
        ContextProduct.organization_id == project.organization_id,
        ContextProduct.project_id == project.id,
    )
    envelope_clause = await _envelope_listing_clause(
        session, context, organization_id=project.organization_id, project_id=project.id
    )
    if envelope_clause is not None:
        filters = (*filters, envelope_clause)
    statement = select(ContextProduct, ContextProductVersion).join(
        ContextProductVersion,
        ContextProductVersion.product_id == ContextProduct.id,
    )
    count_statement = select(func.count(func.distinct(ContextProduct.id))).select_from(
        ContextProduct
    ).join(ContextProductVersion, ContextProductVersion.product_id == ContextProduct.id)
    # R11-FP12 (F08): `askable` answers a different question from the lifecycle view -- not
    # "what does this project contain" but "what could I ask a question through". A lifecycle
    # reader (steward, reviewer, auditor) must keep seeing drafts here, because this listing is
    # their authoring surface; the Ask picker built on that same listing was offering products
    # the ask itself refuses, because the ask admits a product only when it is PUBLISHED and
    # names a consumer role the caller holds (`agent_orchestrator.py:1131-1148`). So this is a
    # caller-chosen mode on one route rather than a narrowed default: a client that sends
    # nothing sees exactly what it saw before.
    lifecycle_view = _can_read_lifecycle(context) and not askable
    # The ask path exempts PlatformAdmin from the consumer-role check
    # (`agent_orchestrator.py:1144`), so the askable listing exempts it too. Applying the
    # binding filter to an administrator would hide products they can in fact ask through,
    # which is the same class of disagreement between picker and endpoint as the defect above,
    # only in the other direction.
    consumer_roles_apply = not (askable and "PlatformAdmin" in context.roles)
    if lifecycle_view:
        latest_version = (
            select(func.max(ContextProductVersion.version))
            .where(ContextProductVersion.product_id == ContextProduct.id)
            .correlate(ContextProduct)
            .scalar_subquery()
        )
        visibility: tuple[ColumnElement[bool], ...] = (
            ContextProductVersion.version == latest_version,
        )
    elif not consumer_roles_apply:
        visibility = (ContextProductVersion.status == "PUBLISHED",)
    else:
        # EXISTS, not a join to the bindings with `.distinct()`. The join needed the DISTINCT to
        # collapse a version bound to two roles the caller holds, and DISTINCT over this select
        # covers every column of both entities -- including the version's `JSON` id lists, for
        # which PostgreSQL has no equality operator (`could not identify an equality operator
        # for type json`). That was a 500 for every non-lifecycle reader (an Analyst or Viewer
        # asking for their product list, i.e. the Ask picker), and SQLite, where every test of
        # this listing ran, accepts DISTINCT over JSON. One row per version needs neither.
        consumer_binding = exists().where(
            ContextProductRoleBinding.context_product_version_id == ContextProductVersion.id,
            ContextProductRoleBinding.organization_id == project.organization_id,
            ContextProductRoleBinding.role_name.in_(context.roles),
        )
        visibility = (ContextProductVersion.status == "PUBLISHED", consumer_binding)
    return ProductListing(
        statement=statement.where(*filters, *visibility),
        count_statement=count_statement.where(*filters, *visibility),
    )


async def read_context_product_version(
    session: AsyncSession,
    context: SecurityContext,
    version_id: UUID,
    *,
    channel: str,
) -> tuple[ContextProduct, ContextProductVersion]:
    """One context product version, read the governed way -- the decision
    `GET /v1/context-product-versions/{version_id}` makes, for every channel that reads it.

    Tenant, the agent's capability envelope, role and pinned-version eligibility, the
    distinguishable retirement signal (410) for a provably earlier consumer, and -- for a
    consumer rather than a lifecycle reader -- the purpose and quality gates, then the
    consumption edge, audit, outbox event and consumption lineage, all recorded under
    `channel` (`REST`, `GRAPHQL`). Refusals raise the same `HTTPException`s the route raises.
    """
    product, version = await _version_scope(session, version_id, context)
    # AR-06 (R11-C6): before any lifecycle disclosure. The retirement branch
    # below deliberately tells a previously-authorized caller that a version
    # was retired rather than that it does not exist; an agent whose envelope
    # excludes this product must not be able to use that branch to learn the
    # version exists at all.
    await _enforce_capability_envelope(session, context, product, version)
    if not _can_read_context_product_version(context, version):
        # AT-7(a)/AT-D1: a retired version (SUPERSEDED/DEPRECATED, or a
        # SUPPORTED version past its window) is not always the same "not
        # found" as a role denial or a version that never published. Only a
        # caller whose role would be eligible for this version AND who was
        # actually, provably authorized for *this exact version* at some
        # point (a real prior consumption edge, not merely a role match --
        # see `was_previously_authorized_consumer`) gets the distinguishable
        # retirement signal. Everyone else -- wrong role, or never actually
        # read it before, or the version simply never published -- gets the
        # identical anti-enumeration 404 as always.
        if context.roles.isdisjoint(version.allowed_consumer_roles) or not is_version_retired(
            version
        ):
            raise HTTPException(status_code=404, detail="context product version not found")
        authorized_before = await was_previously_authorized_consumer(
            session, version_id=version.id, principal_id=context.principal_id
        )
        if not authorized_before:
            raise HTTPException(status_code=404, detail="context product version not found")
        current_version = await current_published_version_number(session, product.id)
        record_audit(
            session,
            context,
            action="context_product.read.retired",
            resource_type="context_product_version",
            resource_id=str(version.id),
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details={
                "product_key": product.product_key,
                "version": version.version,
                "current_version": current_version,
            },
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "context_product_version_retired",
                "message": (
                    "This context product version has been retired. "
                    "Re-pin to the current published version."
                ),
                "product_key": product.product_key,
                "version": version.version,
                "current_version": current_version,
            },
        )
    if not _can_read_lifecycle(context):
        purpose_decision = evaluate_context_product_purpose(
            context.business_purpose, version.policy_summary
        )
        if not purpose_decision.allowed:
            record_audit(
                session,
                context,
                action="context_product.read.purpose_denied",
                resource_type="context_product_version",
                resource_id=str(version.id),
                outcome="DENIED",
                correlation_id=get_correlation_id(),
                details={"purpose": purpose_decision.snapshot()},
            )
            await session.commit()
            raise HTTPException(status_code=404, detail="context product version not found")
        quality_decision = await evaluate_context_product_quality_from_db(
            session,
            organization_id=version.organization_id,
            table_id_values=version.table_ids,
            requirements=version.quality_requirements,
        )
        if not quality_decision.allowed:
            record_audit(
                session,
                context,
                action="context_product.read.quality_denied",
                resource_type="context_product_version",
                resource_id=str(version.id),
                outcome="DENIED",
                correlation_id=get_correlation_id(),
                details={"quality": quality_decision.snapshot()},
            )
            await session.commit()
            raise HTTPException(status_code=404, detail="context product version not found")
        correlation_id = get_correlation_id()
        session.add(
            ContextProductConsumptionEdge(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel=channel,
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality_decision.snapshot(),
            )
        )
        record_audit(
            session,
            context,
            action="context_product.read",
            resource_type="context_product_version",
            resource_id=str(version.id),
            outcome="SUCCESS",
            correlation_id=correlation_id,
            details={"fingerprint": version.fingerprint},
        )
        record_outbox(
            session,
            organization_id=version.organization_id,
            aggregate_type="context_product_version",
            aggregate_id=str(version.id),
            event_type="context.product_consumed.v1",
            payload={
                "product_key": product.product_key,
                "version": version.version,
                "fingerprint": version.fingerprint,
                "principal_id": context.principal_id,
                "channel": channel,
            },
        )
        # CX-4: Record consumption lineage
        await record_consumption(
            session,
            organization_id=version.organization_id,
            edge=ConsumptionEdge(
                consumer_id=context.principal_id,
                consumer_type=context.principal_type,
                resource_type="context_product_version",
                resource_id=str(version.id),
                channel=channel,
                correlation_id=correlation_id,
                policy_decision="ALLOW",
                business_purpose=context.business_purpose,
                details={
                    "product_key": product.product_key,
                    "version": version.version,
                    "fingerprint": version.fingerprint,
                },
            ),
        )
        await session.commit()
    return product, version
