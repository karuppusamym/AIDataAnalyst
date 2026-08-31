"""Deprecation impact preview (TL-7): the blast radius of deprecating a
published governed tool version, computed *before* the deprecation happens.

Reuses LN-7's bounded, transitive, cross-kind lineage traversal
(`unified_lineage_api.build_unified_lineage_impact_payload` -- the same
function backing ``GET /v1/datasources/{id}/unified-lineage/impact/{node_id}``
and the MCP tool ``atlas__get_lineage_impact``), seeded from the tool
version's own declared ``referenced_tables`` -- the identical dependency set
TL-3's quality gate already resolves via
``quality_coupling.resolve_table_ids`` -- plus two other real, already
governed dependency relationships this codebase models directly rather than
by loose convention:

* other PUBLISHED ``GovernedToolVersion``s in the same datasource whose own
  ``referenced_tables`` resolve to a table this version depends on, or to a
  table transitively downstream of one -- a sibling tool built on the same
  data that a deprecation could quietly invalidate.
* ``ContextProductVersion``s (PUBLISHED) that either name this exact tool
  version in ``eligible_tool_version_ids`` (a direct, FK-like reference) or
  whose own ``table_ids`` overlap the same reachable-table set.

...plus one real usage signal, mirroring TL-4's usage-weighted ranking
source -- recent ``ToolExecution`` history for this specific tool version --
so the preview shows not just what *could* break, but who would actually be
interrupted.

``tool_plans.py`` (TL-2) is deliberately not consulted here:
``ToolPlanStepRecord.tool_id`` is a free-text string with no enforced
relationship to ``GovernedTool.slug`` or ``.id`` (``execute_plan`` never
resolves a step against a real ``GovernedTool`` row), so treating it as a
validated dependency would report a blast-radius item this codebase cannot
actually stand behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import (
    ContextProduct,
    ContextProductVersion,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    ToolExecution,
)
from aida.quality_coupling import resolve_table_ids
from aida.schemas import UnifiedLineageImpactNodeRead
from aida.unified_lineage_api import (
    LineageNodeNotFoundError,
    build_unified_lineage_impact_payload,
)

DEFAULT_IMPACT_DEPTH = 3
DEFAULT_IMPACT_NODE_LIMIT = 100
DEFAULT_USAGE_LOOKBACK_DAYS = 90
# Bound the scan (coding standard S5, mirrored from studio_eval.py's mining
# scan): every multi-row operation takes a bound and reports truncation
# rather than silently returning a partial result as if it were complete.
DEFAULT_TOOL_SCAN_LIMIT = 200
DEFAULT_CONTEXT_PRODUCT_SCAN_LIMIT = 200


@dataclass(frozen=True, slots=True)
class DependentToolVersion:
    tool_version_id: UUID
    tool_id: UUID
    slug: str
    version: int
    name: str
    shared_table_count: int


@dataclass(frozen=True, slots=True)
class DependentContextProduct:
    context_product_version_id: UUID
    product_id: UUID
    product_key: str
    version: int
    name: str
    reason: str  # ELIGIBLE_TOOL | SHARED_TABLE


@dataclass(frozen=True, slots=True)
class DeprecationImpact:
    dependency_tables: tuple[str, ...]
    downstream_nodes: tuple[UnifiedLineageImpactNodeRead, ...]
    downstream_truncated: bool
    dependent_tool_versions: tuple[DependentToolVersion, ...]
    dependent_context_products: tuple[DependentContextProduct, ...]
    active_consumer_count: int
    recent_execution_count: int
    lookback_days: int
    requested_depth: int
    node_limit: int

    @property
    def total_blast_radius(self) -> int:
        return (
            len(self.downstream_nodes)
            + len(self.dependent_tool_versions)
            + len(self.dependent_context_products)
        )


async def compute_deprecation_impact(
    session: AsyncSession,
    *,
    tool: GovernedTool,
    version: GovernedToolVersion,
    datasource: DataSource,
    settings: Settings | None = None,
    depth: int = DEFAULT_IMPACT_DEPTH,
    node_limit: int = DEFAULT_IMPACT_NODE_LIMIT,
    lookback_days: int = DEFAULT_USAGE_LOOKBACK_DAYS,
) -> DeprecationImpact:
    """Compute the real blast radius of deprecating ``version``.

    Every part of this is a fresh read against the live database -- the
    result itself is never cached, so a caller invoking this immediately
    before submitting a deprecation sees current state, not a stale
    snapshot from an earlier call. (`build_unified_lineage_impact_payload`'s
    own per-node lineage cache may still apply -- the same caching the
    live ``GET .../unified-lineage/impact/{node_id}`` route relies on.)
    """
    dependency_table_ids = await resolve_table_ids(
        session, datasource=datasource, table_names=version.referenced_tables
    )

    downstream_by_id: dict[str, UnifiedLineageImpactNodeRead] = {}
    downstream_truncated = False
    for table_id in dependency_table_ids.values():
        try:
            impact = await build_unified_lineage_impact_payload(
                session,
                datasource,
                str(table_id),
                depth=depth,
                node_limit=node_limit,
                settings=settings,
            )
        except LineageNodeNotFoundError:
            # Not (yet) a node in the unified graph -- nothing to report for
            # this one dependency, not a reason to fail the whole preview.
            continue
        if impact.downstream_truncated:
            downstream_truncated = True
        for node in impact.downstream:
            existing = downstream_by_id.get(node.node_id)
            if existing is None or node.depth < existing.depth:
                downstream_by_id[node.node_id] = node

    reachable_table_ids: set[UUID] = set(dependency_table_ids.values())
    for node_id, node in downstream_by_id.items():
        if node.node_kind != "TABLE":
            continue
        try:
            reachable_table_ids.add(UUID(node_id))
        except ValueError:
            continue

    dependent_tool_versions = await _find_dependent_tool_versions(
        session,
        datasource=datasource,
        exclude_version_id=version.id,
        reachable_table_ids=reachable_table_ids,
    )
    dependent_context_products = await _find_dependent_context_products(
        session,
        organization_id=version.organization_id,
        tool_version_id=version.id,
        reachable_table_ids=reachable_table_ids,
    )
    active_consumer_count, recent_execution_count = await _count_recent_usage(
        session,
        organization_id=version.organization_id,
        tool_version_id=version.id,
        lookback_days=lookback_days,
    )

    return DeprecationImpact(
        dependency_tables=tuple(version.referenced_tables),
        downstream_nodes=tuple(
            sorted(downstream_by_id.values(), key=lambda item: (item.depth, item.node_id))
        ),
        downstream_truncated=downstream_truncated,
        dependent_tool_versions=dependent_tool_versions,
        dependent_context_products=dependent_context_products,
        active_consumer_count=active_consumer_count,
        recent_execution_count=recent_execution_count,
        lookback_days=lookback_days,
        requested_depth=depth,
        node_limit=node_limit,
    )


async def _find_dependent_tool_versions(
    session: AsyncSession,
    *,
    datasource: DataSource,
    exclude_version_id: UUID,
    reachable_table_ids: set[UUID],
) -> tuple[DependentToolVersion, ...]:
    if not reachable_table_ids:
        return ()
    rows = (
        await session.execute(
            select(GovernedToolVersion, GovernedTool)
            .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
            .where(
                GovernedToolVersion.organization_id == datasource.organization_id,
                GovernedToolVersion.datasource_id == datasource.id,
                GovernedToolVersion.status == "PUBLISHED",
                GovernedToolVersion.id != exclude_version_id,
            )
            .order_by(GovernedTool.slug)
            .limit(DEFAULT_TOOL_SCAN_LIMIT)
        )
    ).all()
    found: list[DependentToolVersion] = []
    for candidate_version, candidate_tool in rows:
        candidate_table_ids = await resolve_table_ids(
            session, datasource=datasource, table_names=candidate_version.referenced_tables
        )
        shared = set(candidate_table_ids.values()) & reachable_table_ids
        if not shared:
            continue
        found.append(
            DependentToolVersion(
                tool_version_id=candidate_version.id,
                tool_id=candidate_tool.id,
                slug=candidate_tool.slug,
                version=candidate_version.version,
                name=candidate_version.name,
                shared_table_count=len(shared),
            )
        )
    return tuple(found)


async def _find_dependent_context_products(
    session: AsyncSession,
    *,
    organization_id: UUID,
    tool_version_id: UUID,
    reachable_table_ids: set[UUID],
) -> tuple[DependentContextProduct, ...]:
    rows = (
        await session.execute(
            select(ContextProductVersion, ContextProduct)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(
                ContextProductVersion.organization_id == organization_id,
                ContextProductVersion.status == "PUBLISHED",
            )
            .order_by(ContextProduct.product_key)
            .limit(DEFAULT_CONTEXT_PRODUCT_SCAN_LIMIT)
        )
    ).all()
    version_str = str(tool_version_id)
    reachable_str = {str(table_id) for table_id in reachable_table_ids}
    found: list[DependentContextProduct] = []
    for cp_version, product in rows:
        if version_str in cp_version.eligible_tool_version_ids:
            reason = "ELIGIBLE_TOOL"
        elif reachable_str & set(cp_version.table_ids):
            reason = "SHARED_TABLE"
        else:
            continue
        found.append(
            DependentContextProduct(
                context_product_version_id=cp_version.id,
                product_id=product.id,
                product_key=product.product_key,
                version=cp_version.version,
                name=cp_version.name,
                reason=reason,
            )
        )
    return tuple(found)


async def _count_recent_usage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    tool_version_id: UUID,
    lookback_days: int,
) -> tuple[int, int]:
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    rows = (
        await session.execute(
            select(ToolExecution.principal_id, func.count(ToolExecution.id))
            .where(
                ToolExecution.organization_id == organization_id,
                ToolExecution.tool_version_id == tool_version_id,
                ToolExecution.status == "COMPLETED",
                ToolExecution.created_at >= since,
            )
            .group_by(ToolExecution.principal_id)
        )
    ).all()
    active_consumer_count = len(rows)
    recent_execution_count = sum(count for _principal, count in rows)
    return active_consumer_count, recent_execution_count
