"""HTTP surface for the access and classification axes (ADR-0018).

Workspaces, memberships, source bindings, the business graph, access policies, and
an authorization probe. Every mutation here is audited in the same transaction as
the change (INV-7), and every grant of source access is maker-checker separated
(INV-8) because a binding is a grant of reach into a data source.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_graph import (
    assign,
    build_hierarchy,
    classification_scope,
    descendants_count,
    extend_closure_for_new_node,
    load_policies,
    rollup,
    rollup_freshness,
    tree,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AccessPolicy,
    BusinessAssignment,
    BusinessNode,
    DataSource,
    SourceBinding,
    Workspace,
    WorkspaceMembership,
)
from aida.policy_engine import Resource, Subject
from aida.policy_engine import simulate as simulate_policy
from aida.schemas import (
    AccessPolicyCreate,
    AccessPolicyRead,
    AuthorizationProbeRead,
    AuthorizationProbeRequest,
    AuthorizationSimulationRead,
    AuthorizationSimulationRequest,
    BusinessAssignmentCreate,
    BusinessAssignmentRead,
    BusinessNodeCreate,
    BusinessNodeRead,
    BusinessNodeRollupRead,
    Page,
    SimulatedDecision,
    SourceBindingCreate,
    SourceBindingDecision,
    SourceBindingRead,
    WorkspaceCreate,
    WorkspaceMembershipCreate,
    WorkspaceMembershipRead,
    WorkspaceRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.workspace_service import (
    BindingApprovalError,
    approve_binding,
    authorize,
    create_workspace,
    request_binding,
)

router = APIRouter(prefix="/v1", tags=["workspaces"])

_ADMIN = ("PlatformAdmin", "OrganizationAdmin", "DataAdmin")
_ANY_MEMBER = ("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Steward", "Analyst", "Reviewer")


async def _load_workspace(session: AsyncSession, workspace_id: UUID) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return workspace


# --- workspaces -------------------------------------------------------------


@router.post(
    "/organizations/{organization_id}/workspaces",
    response_model=WorkspaceRead,
    status_code=201,
)
async def create_workspace_route(
    organization_id: UUID,
    body: WorkspaceCreate,
    context: SecurityContext = Depends(require_roles(*_ADMIN)),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> Workspace:
    enforce_organization(context, organization_id)
    existing = await session.scalar(
        select(Workspace).where(
            Workspace.organization_id == organization_id, Workspace.slug == body.slug
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="workspace slug already exists")
    workspace = await create_workspace(
        session,
        organization_id=organization_id,
        name=body.name,
        slug=body.slug,
        purpose=body.purpose,
        owner_principal=context.principal_id,
        isolation_boundary_id=body.isolation_boundary_id,
        monthly_cost_ceiling=body.monthly_cost_ceiling,
    )
    record_audit(
        session,
        context,
        action="WORKSPACE_CREATED",
        resource_type="WORKSPACE",
        resource_id=str(workspace.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"slug": workspace.slug},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="WORKSPACE",
        aggregate_id=str(workspace.id),
        event_type="workspace.created.v1",
        payload={"workspace_id": str(workspace.id), "slug": workspace.slug},
    )
    await session.commit()
    await session.refresh(workspace)
    return workspace


@router.get("/organizations/{organization_id}/workspaces", response_model=Page)
async def list_workspaces(
    organization_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    statement = select(Workspace).where(Workspace.organization_id == organization_id)
    rows = await session.scalars(statement.order_by(Workspace.slug).limit(limit).offset(offset))
    items = [WorkspaceRead.model_validate(row) for row in rows.all()]
    total = len((await session.scalars(statement)).all())
    return Page(items=list(items), limit=limit, offset=offset, total=total)


# --- membership -------------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/members",
    response_model=WorkspaceMembershipRead,
    status_code=201,
)
async def add_member(
    workspace_id: UUID,
    body: WorkspaceMembershipCreate,
    context: SecurityContext = Depends(require_roles(*_ADMIN)),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> WorkspaceMembership:
    workspace = await _load_workspace(session, workspace_id)
    enforce_organization(context, workspace.organization_id)
    existing = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.principal_id == body.principal_id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="principal already has a membership")
    membership = WorkspaceMembership(
        organization_id=workspace.organization_id,
        workspace_id=workspace_id,
        principal_id=body.principal_id,
        principal_kind=body.principal_kind,
        role=body.role,
        granted_by=context.principal_id,
        expires_at=body.expires_at,
    )
    session.add(membership)
    record_audit(
        session,
        context,
        action="WORKSPACE_MEMBER_ADDED",
        resource_type="WORKSPACE",
        resource_id=str(workspace_id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"role": body.role, "principal_kind": body.principal_kind},
    )
    await session.commit()
    await session.refresh(membership)
    return membership


@router.get("/workspaces/{workspace_id}/members", response_model=Page)
async def list_members(
    workspace_id: UUID,
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    workspace = await _load_workspace(session, workspace_id)
    enforce_organization(context, workspace.organization_id)
    rows = await session.scalars(
        select(WorkspaceMembership)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(WorkspaceMembership.principal_id)
    )
    items = [WorkspaceMembershipRead.model_validate(row) for row in rows.all()]
    return Page(items=list(items), limit=len(items), offset=0, total=len(items))


# --- source bindings --------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/source-bindings",
    response_model=SourceBindingRead,
    status_code=201,
)
async def request_source_binding(
    workspace_id: UUID,
    body: SourceBindingCreate,
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> SourceBinding:
    workspace = await _load_workspace(session, workspace_id)
    enforce_organization(context, workspace.organization_id)
    datasource = await session.get(DataSource, body.datasource_id)
    if datasource is None or datasource.organization_id != workspace.organization_id:
        # 403, never 404: a caller must not be able to distinguish "exists in
        # another organization" from "does not exist".
        raise HTTPException(status_code=403, detail="datasource is not available")
    existing = await session.scalar(
        select(SourceBinding).where(
            SourceBinding.workspace_id == workspace_id,
            SourceBinding.datasource_id == body.datasource_id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="a binding already exists for this datasource")
    binding = await request_binding(
        session,
        organization_id=workspace.organization_id,
        workspace_id=workspace_id,
        datasource_id=body.datasource_id,
        purpose=body.purpose,
        requested_by=context.principal_id,
        schema_scope=body.schema_scope,
        permitted_classifications=body.permitted_classifications,
        masking_profile=body.masking_profile,
        max_query_cost=body.max_query_cost,
    )
    record_audit(
        session,
        context,
        action="SOURCE_BINDING_REQUESTED",
        resource_type="SOURCE_BINDING",
        resource_id=str(binding.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"workspace_id": str(workspace_id), "datasource_id": str(body.datasource_id)},
    )
    record_outbox(
        session,
        organization_id=workspace.organization_id,
        aggregate_type="SOURCE_BINDING",
        aggregate_id=str(binding.id),
        event_type="source_binding.requested.v1",
        payload={
            "binding_id": str(binding.id),
            "workspace_id": str(workspace_id),
            "datasource_id": str(body.datasource_id),
        },
    )
    await session.commit()
    await session.refresh(binding)
    return binding


@router.post("/source-bindings/{binding_id}/decision", response_model=SourceBindingRead)
async def decide_source_binding(
    binding_id: UUID,
    body: SourceBindingDecision,
    context: SecurityContext = Depends(require_roles(*_ADMIN, "Reviewer")),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> SourceBinding:
    binding = await session.get(SourceBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="source binding not found")
    enforce_organization(context, binding.organization_id)
    if body.decision == "REJECT":
        if binding.status != "PENDING_APPROVAL":
            raise HTTPException(status_code=409, detail="binding is not pending")
        if binding.requested_by == context.principal_id:
            raise HTTPException(status_code=409, detail="maker-checker separation is required")
        binding.status = "REJECTED"
        binding.approved_by = context.principal_id
        binding.approved_at = datetime.now(UTC)
    else:
        try:
            await approve_binding(
                session,
                binding,
                approver_principal=context.principal_id,
                valid_for_days=body.valid_for_days,
            )
        except BindingApprovalError as exc:
            status = 409 if exc.reason_code != "BINDING_NOT_PENDING" else 409
            raise HTTPException(status_code=status, detail=exc.reason_code) from exc
    record_audit(
        session,
        context,
        action=f"SOURCE_BINDING_{body.decision}D",
        resource_type="SOURCE_BINDING",
        resource_id=str(binding.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"expires_at": binding.expires_at.isoformat() if binding.expires_at else None},
    )
    record_outbox(
        session,
        organization_id=binding.organization_id,
        aggregate_type="SOURCE_BINDING",
        aggregate_id=str(binding.id),
        event_type=f"source_binding.{body.decision.lower()}d.v1",
        payload={"binding_id": str(binding.id), "status": binding.status},
    )
    await session.commit()
    await session.refresh(binding)
    return binding


@router.get("/workspaces/{workspace_id}/source-bindings", response_model=Page)
async def list_source_bindings(
    workspace_id: UUID,
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    workspace = await _load_workspace(session, workspace_id)
    enforce_organization(context, workspace.organization_id)
    rows = await session.scalars(
        select(SourceBinding).where(SourceBinding.workspace_id == workspace_id)
    )
    items = [SourceBindingRead.model_validate(row) for row in rows.all()]
    return Page(items=list(items), limit=len(items), offset=0, total=len(items))


# --- business graph ---------------------------------------------------------


@router.post(
    "/organizations/{organization_id}/business-nodes",
    response_model=BusinessNodeRead,
    status_code=201,
)
async def create_business_node(
    organization_id: UUID,
    body: BusinessNodeCreate,
    context: SecurityContext = Depends(require_roles(*_ADMIN, "Steward")),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> BusinessNode:
    enforce_organization(context, organization_id)
    if body.parent_id is not None:
        parent = await session.get(BusinessNode, body.parent_id)
        if parent is None or parent.organization_id != organization_id:
            raise HTTPException(
                status_code=422,
                detail="parent_id must reference a node in this organization",
            )
    existing = await session.scalar(
        select(BusinessNode).where(
            BusinessNode.organization_id == organization_id, BusinessNode.code == body.code
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="business node code already exists")
    node = BusinessNode(
        organization_id=organization_id,
        parent_id=body.parent_id,
        kind=body.kind,
        name=body.name,
        code=body.code,
        description=body.description,
        owner_principal=body.owner_principal,
    )
    session.add(node)
    await session.flush()
    # Keep the closure projection correct as the tree grows. Bounded by depth, not by
    # tree size, so this stays cheap however large the taxonomy becomes.
    await extend_closure_for_new_node(session, node)
    record_audit(
        session,
        context,
        action="BUSINESS_NODE_CREATED",
        resource_type="BUSINESS_NODE",
        resource_id=body.code,
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"kind": body.kind},
    )
    await session.commit()
    await session.refresh(node)
    return node


@router.get("/organizations/{organization_id}/business-nodes")
async def get_business_tree(
    organization_id: UUID,
    as_of: datetime | None = Query(default=None),
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """The live classification tree.

    `as_of` makes the tree historical: pass a past timestamp and you get the
    taxonomy as it stood then, which is what lets an audit record from before a
    reorganisation still resolve correctly.
    """
    enforce_organization(context, organization_id)
    moment = as_of or datetime.now(UTC)
    nodes = await tree(session, organization_id, as_of=moment)
    return {"as_of": moment.isoformat(), "roots": build_hierarchy(nodes), "node_count": len(nodes)}


@router.post(
    "/organizations/{organization_id}/business-assignments",
    response_model=BusinessAssignmentRead,
    status_code=201,
)
async def create_business_assignment(
    organization_id: UUID,
    body: BusinessAssignmentCreate,
    context: SecurityContext = Depends(require_roles(*_ADMIN, "Steward")),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> BusinessAssignment:
    enforce_organization(context, organization_id)
    node = await session.get(BusinessNode, body.business_node_id)
    if node is None or node.organization_id != organization_id:
        raise HTTPException(status_code=422, detail="business_node_id must be in this organization")
    assignment = await assign(
        session,
        organization_id=organization_id,
        business_node_id=body.business_node_id,
        target_type=body.target_type,
        target_id=body.target_id,
        assigned_by=context.principal_id,
        confidence=body.confidence,
    )
    record_audit(
        session,
        context,
        action="BUSINESS_ASSIGNMENT_CREATED",
        resource_type=body.target_type,
        resource_id=body.target_id,
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"business_node_id": str(body.business_node_id)},
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


@router.get("/business-nodes/{node_id}/rollup", response_model=BusinessNodeRollupRead)
async def get_rollup(
    node_id: UUID,
    as_of: datetime | None = Query(default=None),
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> BusinessNodeRollupRead:
    """Counts of assigned objects at or below a node -- "everything under Retail Banking"."""
    node = await session.get(BusinessNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="business node not found")
    enforce_organization(context, node.organization_id)
    moment = as_of or datetime.now(UTC)
    counts = await rollup(session, node.organization_id, node_id, as_of=as_of)
    return BusinessNodeRollupRead(
        business_node_id=node_id,
        descendant_node_count=await descendants_count(
            session, node.organization_id, node_id, as_of
        ),
        assigned_by_target_type=counts,
        as_of=moment,
        # Surfaced, not hidden: a coverage number that silently drifts is worse than one
        # labelled three hours old. None means the projection has never been built and
        # the counts above were computed live.
        computed_at=await rollup_freshness(session, node.organization_id, node_id),
    )


# --- access policies --------------------------------------------------------


@router.get("/organizations/{organization_id}/access-policies", response_model=Page)
async def list_access_policies(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*_ADMIN, "Reviewer")),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    rows = await session.scalars(
        select(AccessPolicy)
        .where(AccessPolicy.organization_id == organization_id)
        .order_by(AccessPolicy.code, AccessPolicy.version)
    )
    items = [AccessPolicyRead.model_validate(row) for row in rows.all()]
    return Page(items=list(items), limit=len(items), offset=0, total=len(items))


@router.post(
    "/organizations/{organization_id}/access-policies",
    response_model=AccessPolicyRead,
    status_code=201,
)
async def create_access_policy(
    organization_id: UUID,
    body: AccessPolicyCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "OrganizationAdmin")),
    session: AsyncSession = Depends(get_session),
    correlation_id: str = Depends(get_correlation_id),
) -> AccessPolicy:
    enforce_organization(context, organization_id)
    latest = await session.scalar(
        select(AccessPolicy)
        .where(AccessPolicy.organization_id == organization_id, AccessPolicy.code == body.code)
        .order_by(AccessPolicy.version.desc())
    )
    policy = AccessPolicy(
        organization_id=organization_id,
        code=body.code,
        version=(latest.version + 1) if latest else 1,
        name=body.name,
        description=body.description,
        effect=body.effect,
        priority=body.priority,
        subject_match=body.subject_match,
        resource_match=body.resource_match,
        action_match=body.action_match,
        transform=body.transform,
        condition=body.condition,
        status=body.status,
        created_by=context.principal_id,
    )
    session.add(policy)
    record_audit(
        session,
        context,
        action="ACCESS_POLICY_CREATED",
        resource_type="ACCESS_POLICY",
        resource_id=body.code,
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"effect": body.effect, "status": body.status, "version": policy.version},
    )
    await session.commit()
    await session.refresh(policy)
    return policy


# --- authorization probe ----------------------------------------------------


@router.post("/authorization-probes", response_model=AuthorizationProbeRead)
async def probe_authorization(
    body: AuthorizationProbeRequest,
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> AuthorizationProbeRead:
    """Ask what the engine would decide, without performing the action.

    An access model nobody can interrogate is an access model nobody trusts, and
    "why can this principal see this?" is the question every access review asks.
    Read-only, and value-free: reason codes and policy codes, never data.
    """
    result = await authorize(
        session,
        context,
        workspace_id=body.workspace_id,
        action=body.action,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        datasource_id=body.datasource_id,
        schema_name=body.schema_name,
        classifications=frozenset(body.classifications),
        certification=body.certification,
        quality_state=body.quality_state,
        freshness_state=body.freshness_state,
        principal_kind=body.principal_kind,
    )
    decision = result.decision
    return AuthorizationProbeRead(
        allowed=result.allowed,
        reason_code=result.reason_code,
        workspace_id=result.workspace_id,
        binding_id=result.binding_id,
        matched_policy_code=decision.matched_policy_code if decision else None,
        masked_classifications=sorted(decision.masked_classifications) if decision else [],
        row_filters=list(decision.row_filters) if decision else [],
        evaluated_policy_count=len(decision.evaluated_policy_ids) if decision else 0,
    )


@router.post(
    "/workspaces/{workspace_id}/authorization-simulations",
    response_model=AuthorizationSimulationRead,
)
async def simulate_authorization(
    workspace_id: UUID,
    body: AuthorizationSimulationRequest,
    context: SecurityContext = Depends(require_roles(*_ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> AuthorizationSimulationRead:
    """"Who could see this?" (PG-8) -- one resource, several hypothetical subjects.

    Deliberately built on `aida.policy_engine.simulate`, the same pure engine
    `authorization-probes` and the query-execution path (`query_gateway.py`)
    both evaluate through -- not a second, disconnected evaluator -- so this
    answers with the policies actually enforced rather than a simulation that
    could drift from them. Read-only and value-free (INV-6): reason codes and
    policy codes, never data, and the hypothetical subjects a caller supplies
    do not need to exist as real principals or workspace members.
    """
    if body.workspace_id != workspace_id:
        raise HTTPException(status_code=422, detail="workspace_id mismatch between path and body")
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.organization_id != context.organization_id:
        raise HTTPException(status_code=404, detail="workspace not found")

    node_scope: frozenset[UUID] = frozenset()
    if body.resource_id is not None:
        node_scope = await classification_scope(
            session, workspace.organization_id, body.resource_type, body.resource_id
        )

    resource = Resource(
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        classifications=frozenset(body.classifications),
        business_node_ids=node_scope,
        certification=body.certification,
        datasource_id=body.datasource_id,
        schema_name=body.schema_name,
        quality_state=body.quality_state,
        freshness_state=body.freshness_state,
    )
    subjects = tuple(
        Subject(
            principal_id=f"simulated-{index}",
            principal_kind=simulated.principal_kind,
            roles=frozenset(simulated.roles),
            workspace_id=workspace_id,
            purpose=simulated.purpose,
            isolation_boundary_id=workspace.isolation_boundary_id,
        )
        for index, simulated in enumerate(body.subjects)
    )
    policies = await load_policies(session, workspace.organization_id)
    decisions = simulate_policy(policies, subjects, resource, body.action)

    return AuthorizationSimulationRead(
        workspace_id=workspace_id,
        decisions=[
            SimulatedDecision(
                principal_kind=simulated.principal_kind,
                roles=simulated.roles,
                allowed=decision.allowed,
                reason_code=decision.reason_code,
                matched_policy_code=decision.matched_policy_code,
                masked_classifications=sorted(decision.masked_classifications),
                row_filters=list(decision.row_filters),
            )
            for simulated, decision in zip(body.subjects, decisions, strict=True)
        ],
    )
