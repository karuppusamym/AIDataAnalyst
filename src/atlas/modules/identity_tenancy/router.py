"""atlas.modules.identity_tenancy -- HTTP routes.

Moved verbatim from `aida.workspace_api` on 2026-09-03 under ST-07 Commit C
for identity_tenancy (analog of the observability_audit module's Commit C).
Every endpoint keeps its path, method, response model, `tags=["workspaces"]`,
required roles and status code, so `openapi.json` is byte-identical after the
move. Only the source module changes.

The old path `aida.workspace_api` remains as a re-export shim so `main.py`
and any test that imports a handler function directly keep working
unchanged.

Original module docstring follows.

---
HTTP surface for the access and classification axes (ADR-0018).

Workspaces, memberships, source bindings, the business graph, access policies, and
an authorization probe. Every mutation here is audited in the same transaction as
the change (INV-7), and every grant of source access is maker-checker separated
(INV-8) because a binding is a grant of reach into a data source.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.api import _commit_or_conflict
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
from aida.domain_service import ensure_default_domain, resolve_domain
from aida.events import record_audit, record_outbox
from aida.models import (
    AccessPolicy,
    AgentEvaluationRun,
    BusinessAssignment,
    BusinessNode,
    CrossBoundaryGrant,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    Organization,
    OrganizationIntegrationPolicy,
    Project,
    SourceBinding,
    Workspace,
    WorkspaceMembership,
)
from aida.policy_engine import Resource, Subject
from aida.policy_engine import simulate as simulate_policy
from aida.schemas import (
    AccessPolicyCreate,
    AccessPolicyRead,
    AgentEvaluationRunRead,
    AuthorizationProbeRead,
    AuthorizationProbeRequest,
    AuthorizationSimulationRead,
    AuthorizationSimulationRequest,
    BusinessAssignmentCreate,
    BusinessAssignmentRead,
    BusinessNodeCreate,
    BusinessNodeRead,
    BusinessNodeRollupRead,
    CrossBoundaryGrantCreate,
    CrossBoundaryGrantRead,
    DataDomainCreate,
    DataDomainRead,
    LineOfBusinessCreate,
    LineOfBusinessRead,
    OrganizationCreate,
    OrganizationRead,
    Page,
    ProjectCreate,
    ProjectRead,
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


# ---- Moved from aida.api ST-07 identity_tenancy Commit C (2026-09-03) ----
# The 7 required endpoints (per session addendum's identity_tenancy TODO row) plus
# 4 judgment-call companions moved together so list and create endpoints for the
# same resource land in one place rather than splitting a resource's CRUD across
# two bounded contexts:
#
#   GET  /organizations/{organization_id}/agent-evaluations -- list_agent_evaluations
#   GET  /organizations                                      -- list_organizations
#   GET  /organizations/{organization_id}/lines-of-business  -- list_lines_of_business
#   GET  /lines-of-business/{lob_id}/data-domains             -- list_data_domains
#   GET  /lines-of-business/{lob_id}/projects                 -- list_projects
#   POST /organizations                                       -- create_organization
#   GET  /data-domains/{domain_id}/cross-boundary-grants      -- list_cross_boundary_grants
#
#   -- judgment-call companions (POST siblings of the GETs above, same org/lob/
#      domain/project models, moved so each resource's list+create endpoints are
#      not split across two routers):
#   POST /organizations/{organization_id}/lines-of-business  -- create_line_of_business
#   POST /lines-of-business/{lob_id}/data-domains             -- create_data_domain
#   POST /data-domains/{domain_id}/cross-boundary-grants      -- request_cross_boundary_grant
#   POST /lines-of-business/{lob_id}/projects                 -- create_project
#
# Deliberately NOT moved (per tracker scope, stay in aida.api):
#   /organizations/{organization_id}/catalog/*        -- catalog (already moved, Commit C).
#   /projects/{project_id}/datasources                -- connectivity (already moved).
#   /organizations/{organization_id}/model-routes/*   -- model gateway, not present in
#                                                          aida.api at all.
#   agent-analyses / agent-runs / analysis-runs        -- agent runtime, not identity_tenancy.
#   organization-integration-policy GET/PUT            -- integration-policy administration,
#                                                          a separate concern from the
#                                                          org/lob/domain/project resource
#                                                          tree; left in aida.api.
#
# `_commit_or_conflict` stays defined in `aida.api` -- it is shared by the (still-there)
# datasource/table endpoints too, so this router imports it back from `aida.api`, the
# same precedent `atlas.modules.connectivity.router` set for `create_datasource`.
# `ensure_default_domain` / `resolve_domain` come from `aida.domain_service`, which was
# never part of `aida.api` -- imported directly from their real home, same as before.


@router.get("/organizations/{organization_id}/agent-evaluations", response_model=Page)
async def list_agent_evaluations(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "AgentDeveloper", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = (AgentEvaluationRun.organization_id == organization_id,)
    total = await session.scalar(
        select(func.count()).select_from(AgentEvaluationRun).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AgentEvaluationRun)
            .where(*filters)
            .order_by(AgentEvaluationRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AgentEvaluationRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )

@router.get("/organizations", response_model=Page)
async def list_organizations(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    filters = []
    if "PlatformAdmin" not in context.roles:
        filters.append(Organization.id == context.require_organization())
    total = await session.scalar(select(func.count()).select_from(Organization).where(*filters))
    rows = (
        await session.scalars(
            select(Organization)
            .where(*filters)
            .order_by(Organization.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[OrganizationRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )

@router.get("/organizations/{organization_id}/lines-of-business", response_model=Page)
async def list_lines_of_business(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    filters = (LineOfBusiness.organization_id == organization_id,)
    total = await session.scalar(select(func.count()).select_from(LineOfBusiness).where(*filters))
    rows = (
        await session.scalars(
            select(LineOfBusiness)
            .where(*filters)
            .order_by(LineOfBusiness.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[LineOfBusinessRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )

@router.get("/lines-of-business/{lob_id}/data-domains", response_model=Page)
async def list_data_domains(
    lob_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    await ensure_default_domain(session, lob)
    await session.commit()
    filters = (DataDomain.line_of_business_id == lob.id,)
    total = await session.scalar(select(func.count()).select_from(DataDomain).where(*filters))
    rows = (
        await session.scalars(
            select(DataDomain).where(*filters).order_by(DataDomain.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[DataDomainRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )

@router.get("/lines-of-business/{lob_id}/projects", response_model=Page)
async def list_projects(
    lob_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    filters = (Project.line_of_business_id == lob.id,)
    total = await session.scalar(select(func.count()).select_from(Project).where(*filters))
    rows = (
        await session.scalars(
            select(Project).where(*filters).order_by(Project.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[ProjectRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )

@router.post("/organizations", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin")),
    session: AsyncSession = Depends(get_session),
) -> Organization:
    organization = Organization(name=body.name, slug=body.slug)
    session.add(organization)
    await session.flush()
    session.add(OrganizationIntegrationPolicy(organization_id=organization.id))
    audit_context = replace(context, organization_id=organization.id)
    record_audit(
        session,
        audit_context,
        action="organization.create",
        resource_type="organization",
        resource_id=str(organization.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=organization.id,
        aggregate_type="organization",
        aggregate_id=str(organization.id),
        event_type="organization.created.v1",
        payload={"organization_id": str(organization.id), "slug": organization.slug},
    )
    await _commit_or_conflict(session, "organization slug already exists")
    return organization

@router.post(
    "/organizations/{organization_id}/lines-of-business",
    response_model=LineOfBusinessRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_line_of_business(
    organization_id: UUID,
    body: LineOfBusinessCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "OrganizationAdmin")),
    session: AsyncSession = Depends(get_session),
) -> LineOfBusiness:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    lob = LineOfBusiness(organization_id=organization_id, name=body.name, code=body.code)
    session.add(lob)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="line_of_business.create",
        resource_type="line_of_business",
        resource_id=str(lob.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="line_of_business",
        aggregate_id=str(lob.id),
        event_type="line_of_business.created.v1",
        payload={"line_of_business_id": str(lob.id), "code": lob.code},
    )
    await _commit_or_conflict(session, "line-of-business code already exists")
    return lob

@router.post(
    "/lines-of-business/{lob_id}/data-domains",
    response_model=DataDomainRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_domain(
    lob_id: UUID,
    body: DataDomainCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> DataDomain:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    parent = None
    if body.parent_domain_id is not None:
        parent = await session.get(DataDomain, body.parent_domain_id)
        if parent is None or parent.line_of_business_id != lob.id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "parent_domain_id must reference an existing domain "
                    "in the same line of business"
                ),
            )
    domain = DataDomain(
        organization_id=lob.organization_id,
        line_of_business_id=lob.id,
        parent_domain_id=body.parent_domain_id,
        name=body.name,
        code=body.code,
    )
    session.add(domain)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=lob.organization_id),
        action="data_domain.create",
        resource_type="data_domain",
        resource_id=str(domain.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"parent_domain_id": str(body.parent_domain_id) if body.parent_domain_id else None},
    )
    record_outbox(
        session,
        organization_id=lob.organization_id,
        aggregate_type="data_domain",
        aggregate_id=str(domain.id),
        event_type="data_domain.created.v1",
        payload={
            "data_domain_id": str(domain.id),
            "line_of_business_id": str(lob.id),
            "parent_domain_id": str(body.parent_domain_id) if body.parent_domain_id else None,
        },
    )
    await _commit_or_conflict(session, "data domain code already exists in this line of business")
    return domain

@router.get("/data-domains/{domain_id}/cross-boundary-grants", response_model=Page)
async def list_cross_boundary_grants(
    domain_id: UUID,
    grant_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    domain = await session.get(DataDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, domain.organization_id)
    filters = [
        or_(
            CrossBoundaryGrant.source_data_domain_id == domain.id,
            CrossBoundaryGrant.target_data_domain_id == domain.id,
        )
    ]
    if grant_status is not None:
        filters.append(CrossBoundaryGrant.status == grant_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(CrossBoundaryGrant).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CrossBoundaryGrant)
            .where(*filters)
            .order_by(CrossBoundaryGrant.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CrossBoundaryGrantRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )

@router.post(
    "/data-domains/{domain_id}/cross-boundary-grants",
    response_model=CrossBoundaryGrantRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_cross_boundary_grant(
    domain_id: UUID,
    body: CrossBoundaryGrantCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> CrossBoundaryGrant:
    """Request permission for `body.target_data_domain_id` to see across the
    boundary into `domain_id` (the source, owning domain). Creates the grant in
    PENDING_APPROVAL and files it into the same governance review queue every
    other governed object here uses (ADR-0017 SS4) — it only becomes ACTIVE once
    a *different* principal approves it via POST /governance/reviews/{id}/decision.
    """
    source_domain = await session.get(DataDomain, domain_id)
    if source_domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, source_domain.organization_id)
    if body.target_data_domain_id == source_domain.id:
        raise HTTPException(
            status_code=422, detail="target_data_domain_id must differ from the source domain"
        )
    target_domain = await session.get(DataDomain, body.target_data_domain_id)
    if target_domain is None or target_domain.organization_id != source_domain.organization_id:
        raise HTTPException(status_code=422, detail="target_data_domain_id not found")
    grant = CrossBoundaryGrant(
        organization_id=source_domain.organization_id,
        source_data_domain_id=source_domain.id,
        target_data_domain_id=target_domain.id,
        edge_kinds=body.edge_kinds,
        reason=body.reason,
        requested_by=context.principal_id,
        expires_at=body.expires_at,
    )
    session.add(grant)
    await session.flush()
    review = GovernanceReview(
        organization_id=source_domain.organization_id,
        object_type="CROSS_BOUNDARY_GRANT",
        object_id=str(grant.id),
        requested_action="GRANT",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=source_domain.organization_id),
        action="cross_boundary_grant.request",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "cross_boundary_grant_id": str(grant.id),
            "source_data_domain_id": str(source_domain.id),
            "target_data_domain_id": str(target_domain.id),
        },
    )
    record_outbox(
        session,
        organization_id=source_domain.organization_id,
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
    return grant

@router.post(
    "/lines-of-business/{lob_id}/projects",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    lob_id: UUID,
    body: ProjectCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "ProjectAdmin")),
    session: AsyncSession = Depends(get_session),
) -> Project:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    if body.data_domain_id is not None:
        explicit_domain = await session.get(DataDomain, body.data_domain_id)
        if explicit_domain is None or explicit_domain.line_of_business_id != lob.id:
            raise HTTPException(
                status_code=422,
                detail="data_domain_id must reference an existing domain in this line of business",
            )
    domain = await resolve_domain(session, lob, body.data_domain_id)
    project = Project(
        organization_id=lob.organization_id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name=body.name,
        slug=body.slug,
    )
    session.add(project)
    await session.flush()
    audit_context = replace(context, organization_id=lob.organization_id)
    record_audit(
        session,
        audit_context,
        action="project.create",
        resource_type="project",
        resource_id=str(project.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=lob.organization_id,
        aggregate_type="project",
        aggregate_id=str(project.id),
        event_type="project.created.v1",
        payload={"project_id": str(project.id), "lob_id": str(lob.id)},
    )
    await _commit_or_conflict(session, "project slug already exists")
    return project
