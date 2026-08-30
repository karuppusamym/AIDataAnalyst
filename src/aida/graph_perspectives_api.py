"""KG-5: saved Knowledge Graph / Graph Explorer perspectives.

This is a standalone router (not folded into ``aida.api`` / ``aida.intelligence_api``)
so it can land without touching either hot hub module -- see ``aida.composite_key_api``
for the same convention on a prior tracker item.

This module is a thin persistence layer over an opaque, caller-defined JSON blob
(the frontend Graph Explorer's own view-state shape -- see ``models.GraphPerspective``
for an example). It never interprets graph semantics itself, so it has no dependency
on ``aida.knowledge_graph`` / ``aida.retrieval`` / ``aida.fusion_ranking`` /
``aida.graph_retrieval``.

There is no maker-checker review here (unlike most other candidate-row features in
this codebase): a perspective is a personal/shared productivity artifact, not a
governed object. The owner can freely create/update/delete; a caller whose role
intersects ``allowed_viewer_roles`` can only read.

Endpoints:
- ``POST /v1/organizations/{organization_id}/graph-perspectives`` -- create; the
  caller becomes ``owner_principal``.
- ``GET /v1/organizations/{organization_id}/graph-perspectives`` -- list perspectives
  the caller owns, plus ones shared with a role the caller holds, optionally narrowed
  to one ``datasource_id``, with the usual ``Page``/``limit``/``offset`` pagination.
- ``GET /v1/graph-perspectives/{perspective_id}`` -- 404 if the caller is neither the
  owner nor covered by ``allowed_viewer_roles``.
- ``PATCH /v1/graph-perspectives/{perspective_id}`` -- owner only (403 otherwise).
- ``DELETE /v1/graph-perspectives/{perspective_id}`` -- owner only (403 otherwise).

No domain event is emitted: see the module docstring on ``models.GraphPerspective``
for why (a personal productivity artifact, not a lineage/quality/policy fact that
other services need to react to). Mutations are still audited via ``record_audit``,
matching this codebase's broad convention of auditing writes regardless of whether
the resource itself is "governed".
"""

from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.intelligence_api import GRAPH_READER_ROLES
from aida.models import GraphPerspective, Organization
from aida.schemas import GraphPerspectiveCreate, GraphPerspectiveRead, GraphPerspectiveUpdate, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["graph-perspectives"])


def _can_view(context: SecurityContext, perspective: GraphPerspective) -> bool:
    if "PlatformAdmin" in context.roles:
        return True
    if perspective.owner_principal == context.principal_id:
        return True
    return not context.roles.isdisjoint(perspective.allowed_viewer_roles)


def _is_owner_or_admin(context: SecurityContext, perspective: GraphPerspective) -> bool:
    return "PlatformAdmin" in context.roles or perspective.owner_principal == context.principal_id


async def _get_or_404(session: AsyncSession, perspective_id: UUID) -> GraphPerspective:
    perspective = await session.get(GraphPerspective, perspective_id)
    if perspective is None:
        raise HTTPException(status_code=404, detail="graph perspective not found")
    return perspective


@router.post(
    "/organizations/{organization_id}/graph-perspectives",
    response_model=GraphPerspectiveRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_graph_perspective(
    organization_id: UUID,
    body: GraphPerspectiveCreate,
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GraphPerspective:
    enforce_organization(context, organization_id)
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    perspective = GraphPerspective(
        organization_id=organization_id,
        datasource_id=body.datasource_id,
        name=body.name,
        description=body.description,
        owner_principal=context.principal_id,
        allowed_viewer_roles=body.allowed_viewer_roles,
        view_state=body.view_state,
    )
    session.add(perspective)
    await session.flush()
    record_audit(
        session,
        context,
        action="graph_perspective.create",
        resource_type="graph_perspective",
        resource_id=str(perspective.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"name": perspective.name},
    )
    await session.commit()
    return perspective


@router.get(
    "/organizations/{organization_id}/graph-perspectives",
    response_model=Page,
)
async def list_graph_perspectives(
    organization_id: UUID,
    datasource_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [GraphPerspective.organization_id == organization_id]
    if datasource_id is not None:
        filters.append(GraphPerspective.datasource_id == datasource_id)
    # Visibility ("mine, or shared with a role I hold") is a role-set intersection
    # against a JSON list column. There is no existing JSON-containment query idiom
    # in this codebase to reuse (grep for `allowed_roles` elsewhere shows every other
    # call site -- e.g. `tool_api.py`'s `context.roles.isdisjoint(version.allowed_roles)`
    # -- does this same check in Python, not as a DB-level JSON operator), and doing it
    # in Python here keeps the query portable across this project's sqlite (tests) and
    # postgresql+asyncpg (production) backends alike rather than reaching for a
    # dialect-specific JSONB `@>`/`?|` operator. Row volume for one caller's/org's saved
    # perspectives is small, so filtering after a plain organization/datasource fetch
    # is the right tradeoff over a more complex SQL-level containment expression.
    rows = (await session.scalars(select(GraphPerspective).where(*filters))).all()
    visible = [row for row in rows if _can_view(context, row)]
    visible.sort(key=lambda row: row.created_at, reverse=True)
    total = len(visible)
    page_rows = visible[offset : offset + limit]
    return Page(
        items=[GraphPerspectiveRead.model_validate(row) for row in page_rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get("/graph-perspectives/{perspective_id}", response_model=GraphPerspectiveRead)
async def get_graph_perspective(
    perspective_id: UUID,
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GraphPerspective:
    perspective = await _get_or_404(session, perspective_id)
    enforce_organization(context, perspective.organization_id)
    if not _can_view(context, perspective):
        # Same 404 (not 403) as an unknown id: existence of a private perspective is
        # not something an unauthorized caller should be able to infer.
        raise HTTPException(status_code=404, detail="graph perspective not found")
    return perspective


@router.patch("/graph-perspectives/{perspective_id}", response_model=GraphPerspectiveRead)
async def update_graph_perspective(
    perspective_id: UUID,
    body: GraphPerspectiveUpdate,
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GraphPerspective:
    perspective = await _get_or_404(session, perspective_id)
    enforce_organization(context, perspective.organization_id)
    if not _can_view(context, perspective):
        raise HTTPException(status_code=404, detail="graph perspective not found")
    if not _is_owner_or_admin(context, perspective):
        raise HTTPException(
            status_code=403, detail="only the owner may update this graph perspective"
        )
    if body.name is not None:
        perspective.name = body.name
    if body.description is not None:
        perspective.description = body.description
    if body.allowed_viewer_roles is not None:
        perspective.allowed_viewer_roles = body.allowed_viewer_roles
    if body.view_state is not None:
        perspective.view_state = body.view_state
    record_audit(
        session,
        replace(context, organization_id=perspective.organization_id),
        action="graph_perspective.update",
        resource_type="graph_perspective",
        resource_id=str(perspective.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={},
    )
    await session.commit()
    return perspective


@router.delete("/graph-perspectives/{perspective_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_graph_perspective(
    perspective_id: UUID,
    context: SecurityContext = Depends(require_roles(*GRAPH_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    perspective = await _get_or_404(session, perspective_id)
    enforce_organization(context, perspective.organization_id)
    if not _can_view(context, perspective):
        raise HTTPException(status_code=404, detail="graph perspective not found")
    if not _is_owner_or_admin(context, perspective):
        raise HTTPException(
            status_code=403, detail="only the owner may delete this graph perspective"
        )
    await session.delete(perspective)
    record_audit(
        session,
        replace(context, organization_id=perspective.organization_id),
        action="graph_perspective.delete",
        resource_type="graph_perspective",
        resource_id=str(perspective.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={},
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
