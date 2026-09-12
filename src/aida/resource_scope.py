"""Load a tenant-scoped resource, or refuse.

**Invariant this module exists to hold:** fetching one of these resources by
id and checking that the caller's organization owns it are a single
operation. It is not possible to do the first and forget the second, because
there is no function here that only does the first.

Six routers carried a private copy of one of these two helpers -- three of
`_project_scope` (`context_product_api`, `document_ingestion_api`,
`product_marketplace_api`) and three of `_load_datasource`
(`procedure_lineage_api`, `unified_lineage_api`, `view_lineage_api` -- the
last of which was removed entirely by R11-X5), each
byte-identical to its siblings. `Docs/review-2026-09-05/REVIEW.md` R07 asks
for one authoritative implementation per invariant, and this is the invariant
where duplication costs most: every copy contains an `enforce_organization`
call, so a copy that drifts is a route that has quietly stopped enforcing the
tenant boundary while still looking like the other five.

**404, not 403, for a resource in another organization.** Both helpers raise
`404 not found` when the row is missing and let `enforce_organization` decide
the cross-tenant case, which is the behaviour the six copies already had --
preserved here deliberately rather than re-derived, since telling a caller
"this id exists but is not yours" is itself a disclosure.

This module imports no router and no service; it is a leaf that routers call.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import DataSource, Project
from aida.security import SecurityContext, enforce_organization

__all__ = ["load_datasource_in_scope", "load_project_in_scope"]


async def load_project_in_scope(
    session: AsyncSession, project_id: UUID, context: SecurityContext
) -> Project:
    """The project, once the caller's organization is confirmed to own it."""
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    return project


async def load_datasource_in_scope(
    session: AsyncSession, context: SecurityContext, datasource_id: UUID
) -> DataSource:
    """The datasource, once the caller's organization is confirmed to own it."""
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    return datasource
