"""Unified Lineage Explorer API.

Milestone 1 of the Collibra-parity lineage plan (see
`Docs/competitors/08-collibra-lineage-and-platform-analysis-2026-08.md` and
`Docs/20-modules/09-lineage.md`): one canonical graph that merges declared
foreign keys, human-approved/candidate column relationships, dbt manifest
dependency edges, OpenLineage table edges, and SQL-parsed view/procedure
lineage edges (LN-2, folded in for LN-7 -- table pairs resolved to the
catalog on both ends only), plus transitive, cross-kind, bounded
upstream/downstream impact traversal (`traverse` in `aida.unified_lineage`)
in place of direct-reference counting.

R11-B13 extended it past the warehouse edge: BI report nodes and their
`BI_LINEAGE` edges (LN-4's Tableau/Power BI report -> metric -> column chain,
folded to report -> table) now merge in through
`unified_lineage_builder.collect_bi_lineage`, so a REFERENCED_BY traversal from
a table reaches the dashboards it feeds. Consumption edges are deliberately
*not* merged -- they record who read an asset rather than what derives from it;
that provider's docstring gives the four reasons.

This intentionally does not yet cover: authoritative column-level mappings
(dbt UI still matches columns by name -- see `transformation-workbench.js`),
unmatched (free-text) view/procedure table names, AI decision edges, or
export. Those remain tracked as LN-3, LN-10, LN-12 in
`Docs/20-modules/09-lineage.md` and EA.9, EC.6+ in
`Docs/60-delivery/02-epic-backlog.md`.

AT-19: a `VIEW_DEFINITION` edge's `evidence` also carries a bounded, resolvable
`transformation_reference` (`{tool: "get_transformation_detail", entity_id,
kind}`) plus `redaction_status`/`availability`, sourced from envelope 1.1's
`MetadataViewDefinition` (unique per `table_id`, so the lookup is exact, not
guessed) -- never the DDL text itself, keeping this graph response's size
bound (ADR-0010) intact. A `PROCEDURE_DEFINITION` edge gets one only when a
single captured routine establishes it -- the routine-aware
`DeepProcedureLineageEdge` carries `routine_id`, since 2026-09-11 -- and then
`entity_id` is that routine and `kind` is `ROUTINE_BODY`. `ProcedureLineageEdge`,
the pasted-SQL table, carries no identity back to a routine, so no reference
is fabricated for an edge it alone establishes (see
`mcp_server.py::_transformation_detail`).
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.models import DataDomain
from aida.resource_scope import load_datasource_in_scope
from aida.schemas import (
    DomainLineageGraphRead,
    UnifiedLineageGraphRead,
    UnifiedLineageImpactRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles

# R11-GQL01: the builders, the reader roles and the not-found error live in
# `aida.unified_lineage_service`, so GraphQL can serve the same reads without importing
# a router. Re-exported here for the importers this module already had.
from aida.unified_lineage_service import (  # noqa: F401
    UNIFIED_LINEAGE_READER_ROLES,
    LineageNodeNotFoundError,
    _build_unified_graph,
    build_domain_unified_lineage_graph_payload,
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
)

router = APIRouter(prefix="/v1", tags=["unified-lineage"])


async def _load_domain(
    session: AsyncSession, context: SecurityContext, domain_id: UUID
) -> DataDomain:
    domain = await session.get(DataDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, domain.organization_id)
    return domain


@router.get(
    "/datasources/{datasource_id}/unified-lineage/graph",
    response_model=UnifiedLineageGraphRead,
)
async def get_unified_lineage_graph(
    datasource_id: UUID,
    node_limit: int = Query(default=300, ge=5, le=2_000),
    edge_limit: int = Query(default=1_500, ge=5, le=10_000),
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = Query(
        default="APPROVED"
    ),
    include_pending_edges: bool = Query(
        default=False,
        description=(
            "P1-05: opt in to include PROPOSED parsed-lineage edges "
            "(view/procedure/dbt/OpenLineage). Default false keeps them "
            "in the review queue only, per ADR-0026."
        ),
    ),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UnifiedLineageGraphRead:
    """Return the merged FK + suggested + dbt + OpenLineage + view/procedure graph for one
    datasource.

    This is the canonical lineage graph called for in the Collibra-parity
    plan: one node/edge set spanning every lineage source instead of
    separate, unlinked workbenches. Also served as the native MCP tool
    `atlas__get_lineage_graph` (`mcp_server.py`).
    """

    datasource = await load_datasource_in_scope(session, context, datasource_id)
    return await build_unified_lineage_graph_payload(
        session,
        datasource,
        node_limit=node_limit,
        edge_limit=edge_limit,
        suggestion_status=suggestion_status,
        settings=settings,
        include_pending_edges=include_pending_edges,
    )


@router.get(
    "/datasources/{datasource_id}/unified-lineage/impact/{node_id}",
    response_model=UnifiedLineageImpactRead,
)
async def get_unified_lineage_impact(
    datasource_id: UUID,
    node_id: str,
    depth: int = Query(default=5, ge=1, le=8),
    node_limit: int = Query(default=200, ge=5, le=2_000),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UnifiedLineageImpactRead:
    """Transitive upstream/downstream impact across every merged lineage source.

    Replaces `GET /v1/metadata/tables/{table_id}/impact`'s direct-reference
    count with a bounded multi-hop traversal: "what would break, N hops out,
    if this node changed" -- the gap called out against Collibra's impact
    analysis view. Also served as the native MCP tool
    `atlas__get_lineage_impact` (`mcp_server.py`).
    """

    datasource = await load_datasource_in_scope(session, context, datasource_id)
    try:
        return await build_unified_lineage_impact_payload(
            session,
            datasource,
            node_id,
            depth=depth,
            node_limit=node_limit,
            settings=settings,
        )
    except LineageNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/data-domains/{domain_id}/unified-lineage/graph",
    response_model=DomainLineageGraphRead,
)
async def get_domain_unified_lineage_graph(
    domain_id: UUID,
    node_limit: int = Query(default=600, ge=5, le=4_000),
    edge_limit: int = Query(default=3_000, ge=5, le=20_000),
    suggestion_status: Literal["ALL", "PENDING", "APPROVED", "REJECTED"] = Query(
        default="APPROVED"
    ),
    include_pending_edges: bool = Query(
        default=False,
        description=(
            "P1-05: opt in to include PROPOSED parsed-lineage edges "
            "(view/procedure/dbt/OpenLineage). Default false keeps them "
            "in the review queue only, per ADR-0026."
        ),
    ),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DomainLineageGraphRead:
    """Return the merged FK + suggested + dbt + OpenLineage + view/procedure graph across every
    datasource in one data_domain (ADR-0017 SS3) -- the domain-scoped
    traversal endpoint closing KG-2/RL-5's "cross-source traversal" gap for
    sources sharing a governance boundary, without opening an unbounded
    org-wide graph (ADR-0010's bounded/lazy/value-free contract still
    applies at this wider scope; see build_domain_unified_lineage_graph_payload).
    """

    domain = await _load_domain(session, context, domain_id)
    return await build_domain_unified_lineage_graph_payload(
        session,
        domain,
        node_limit=node_limit,
        edge_limit=edge_limit,
        suggestion_status=suggestion_status,
        settings=settings,
        include_pending_edges=include_pending_edges,
    )
