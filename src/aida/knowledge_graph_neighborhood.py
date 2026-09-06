"""Bounded knowledge-graph neighborhood: crossing policy, traversal, projection.

R02 named `intelligence_api.get_knowledge_graph_neighborhood` (455 lines) as a
hotspot whose useful boundary is "neighborhood service and projection adapter".
This module is both. The router keeps what only a router can do -- resolve the
datasource and focus table, refuse with the right HTTP status, and hand back a
`KnowledgeGraphRead` -- and everything below decides what the caller may see.

**One authority on crossing.** `DatasourceCrossingPolicy` is the only thing
here that decides whether the traversal may follow an edge into another
datasource, and it is the only thing that calls `check_cross_boundary_grant`
or `authorization_gate.gate`. KG-2 requires *both*: an ACTIVE
`CrossBoundaryGrant` letting the seed's data_domain see into the other one
(when they differ), and the caller's own RBAC allowing `READ_METADATA` on that
specific datasource. It decides once per newly-discovered datasource and
caches, mirroring the per-distinct-datasource `gate()` cost in
`list_tables_composed`, and it fails closed: an unresolvable or foreign-org
datasource is never a valid crossing target (INV-4/INV-5), whatever a stray
candidate row claims.

A refused crossing is indistinguishable from absence (INV-6): the edge and the
node simply are not in the response, with no truncation reason, no field and no
count naming the refusal. That is deliberate -- `AuthorizationDenied` carries no
resource detail, and neither may this.

**Bounds are separate from policy.** `node_limit` and `edge_limit` truncate;
policy withholds. They are reported differently on purpose, so `truncation_reasons`
never becomes a channel that leaks what was refused.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.authorization_gate import AuthorizationDenied, gate
from aida.classification import SENSITIVE_CLASSES
from aida.config import Settings
from aida.domain_service import check_cross_boundary_grant
from aida.knowledge_graph import GraphDirection, GraphLink, expand_cross_source_frontier
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    RelationshipCandidate,
)
from aida.schemas import GraphEdgeRead, GraphNodeRead, KnowledgeGraphRead
from aida.security_types import SecurityContext

SuggestionStatus = Literal["ALL", "PENDING", "APPROVED", "REJECTED"]


@dataclass(frozen=True, slots=True)
class NeighborhoodRequest:
    """One expansion, with its focus and its budget already validated.

    `datasource` and `focus` arrive resolved and authorized for the seed; this
    module decides only what may be reached *from* there.
    """

    datasource: DataSource
    focus: MetadataTable
    depth: int
    direction: GraphDirection
    suggestion_status: SuggestionStatus
    node_limit: int
    edge_limit: int


def bounds_policy_violation(
    settings: Settings, *, depth: int, node_limit: int, edge_limit: int
) -> str | None:
    """Name the first requested bound that exceeds configured policy.

    Stated once, here, rather than in each caller: the deployment's ceiling is
    a property of the graph read, not of the HTTP route that happens to ask
    for it. Returns the refusal detail, or `None` when the request fits.
    """
    if depth > settings.knowledge_graph_max_depth:
        return "requested graph depth exceeds policy"
    if node_limit > settings.knowledge_graph_max_nodes:
        return "requested graph node limit exceeds policy"
    if edge_limit > settings.knowledge_graph_max_edges:
        return "requested graph edge limit exceeds policy"
    return None


class DatasourceCrossingPolicy:
    """Whether this traversal may render rows from a given datasource.

    The seed datasource is authorized before this object exists (role check
    plus organization enforcement in the router). Every *other* datasource the
    frontier reaches is decided here, once, and cached for the rest of the
    request.
    """

    def __init__(
        self,
        session: AsyncSession,
        context: SecurityContext,
        settings: Settings,
        *,
        seed: DataSource,
    ) -> None:
        self._session = session
        self._context = context
        self._settings = settings
        self._seed = seed
        self._allowed: dict[UUID, bool] = {seed.id: True}
        self._datasources: dict[UUID, DataSource] = {seed.id: seed}
        #: Datasources whose rows may appear. Only ever grows through `admit`.
        self.authorized_datasource_ids: set[UUID] = {seed.id}

    def is_authorized(self, datasource_id: UUID) -> bool:
        return self._allowed.get(datasource_id, False)

    def undecided(self, datasource_ids: set[UUID]) -> set[UUID]:
        return {
            datasource_id
            for datasource_id in datasource_ids
            if datasource_id not in self.authorized_datasource_ids
            and datasource_id not in self._allowed
        }

    async def admit(self, datasource_ids: set[UUID]) -> None:
        """Decide, once each, whether these datasources may be crossed into.

        Fails closed at every step: a datasource that cannot be loaded, belongs
        to another organization, lacks an ACTIVE cross-boundary grant for its
        data_domain, or that the caller's own gate refuses, is recorded as
        denied and never retried.
        """
        if not datasource_ids:
            return
        loaded = (
            await self._session.scalars(select(DataSource).where(DataSource.id.in_(datasource_ids)))
        ).all()
        for datasource in loaded:
            self._datasources[datasource.id] = datasource
        for datasource_id in datasource_ids:
            self._allowed[datasource_id] = await self._decide(datasource_id)
            if self._allowed[datasource_id]:
                self.authorized_datasource_ids.add(datasource_id)

    async def _decide(self, datasource_id: UUID) -> bool:
        resolved = self._datasources.get(datasource_id)
        if resolved is None or resolved.organization_id != self._seed.organization_id:
            return False
        if resolved.data_domain_id != self._seed.data_domain_id:
            granted = await check_cross_boundary_grant(
                self._session,
                self._seed.organization_id,
                resolved.data_domain_id,
                self._seed.data_domain_id,
                edge_kind="SUGGESTED_RELATIONSHIP",
            )
            if not granted:
                return False
        try:
            await gate(
                self._session,
                self._context,
                settings=self._settings,
                action="READ_METADATA",
                resource_type="datasource",
                resource_id=str(resolved.id),
                datasource_id=resolved.id,
            )
        except AuthorizationDenied:
            return False
        return True


@dataclass(slots=True)
class NeighborhoodTraversal:
    """Which nodes the expansion reached, how far away, and what it had to cut."""

    visited: set[UUID]
    node_depths: dict[UUID, int]
    truncation_reasons: set[str] = field(default_factory=set)


def _frontier_predicates(
    direction: GraphDirection, frontier: set[UUID]
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    """Which end of an edge must sit on the frontier for it to be followed."""
    if direction == "REFERENCES":
        return (
            MetadataConstraint.table_id.in_(frontier),
            RelationshipCandidate.source_table_id.in_(frontier),
        )
    if direction == "REFERENCED_BY":
        return (
            MetadataConstraint.referenced_table_id.in_(frontier),
            RelationshipCandidate.target_table_id.in_(frontier),
        )
    return (
        or_(
            MetadataConstraint.table_id.in_(frontier),
            MetadataConstraint.referenced_table_id.in_(frontier),
        ),
        or_(
            RelationshipCandidate.source_table_id.in_(frontier),
            RelationshipCandidate.target_table_id.in_(frontier),
        ),
    )


async def traverse_neighborhood(
    session: AsyncSession, request: NeighborhoodRequest, policy: DatasourceCrossingPolicy
) -> NeighborhoodTraversal:
    """Expand outward from the focus table, hop by hop, inside the budget.

    KG-2: the frontier is not confined to the seed datasource. A
    `RelationshipCandidate` whose two sides name different datasources can
    carry it into another one -- but only through `policy`, and only after that
    datasource has been decided. Candidates already inside authorized
    datasources need no new decision, so they are probed separately from the
    ones that would cross a boundary.
    """
    focus_id = request.focus.id
    visited: set[UUID] = {focus_id}
    frontier: set[UUID] = {focus_id}
    traversal = NeighborhoodTraversal(visited=visited, node_depths={focus_id: 0})
    encountered_links: dict[str, GraphLink] = {}
    node_datasource_id: dict[UUID, UUID] = {focus_id: request.datasource.id}

    for current_depth in range(1, request.depth + 1):
        if (
            not frontier
            or len(visited) >= request.node_limit
            or len(encountered_links) >= request.edge_limit
        ):
            if frontier and len(visited) >= request.node_limit:
                traversal.truncation_reasons.add("NODE_LIMIT")
            if frontier and len(encountered_links) >= request.edge_limit:
                traversal.truncation_reasons.add("EDGE_LIMIT")
            break

        constraint_frontier, candidate_frontier = _frontier_predicates(request.direction, frontier)
        touched = policy.authorized_datasource_ids
        probe_limit = request.edge_limit - len(encountered_links) + 1

        constraints = (
            await session.scalars(
                select(MetadataConstraint)
                .where(
                    MetadataConstraint.datasource_id.in_(touched),
                    MetadataConstraint.status == "ACTIVE",
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.referenced_table_id.is_not(None),
                    constraint_frontier,
                )
                .order_by(MetadataConstraint.id)
                .limit(probe_limit)
            )
        ).all()

        # Both ends already authorized: no new decision is needed for these.
        candidate_filters = [
            RelationshipCandidate.datasource_id.in_(touched),
            RelationshipCandidate.target_datasource_id.in_(touched),
            candidate_frontier,
        ]
        if request.suggestion_status != "ALL":
            candidate_filters.append(RelationshipCandidate.status == request.suggestion_status)
        candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*candidate_filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(probe_limit)
            )
        ).all()

        # KG-2: candidates that would cross into a datasource not yet touched.
        # Probed separately because whether one may join `links` at all depends
        # on a per-datasource policy decision, not on row-level fields alone.
        boundary_filters = [
            RelationshipCandidate.organization_id == request.datasource.organization_id,
            or_(
                and_(
                    RelationshipCandidate.datasource_id.in_(touched),
                    RelationshipCandidate.target_datasource_id.notin_(touched),
                ),
                and_(
                    RelationshipCandidate.target_datasource_id.in_(touched),
                    RelationshipCandidate.datasource_id.notin_(touched),
                ),
            ),
            candidate_frontier,
        ]
        if request.suggestion_status != "ALL":
            boundary_filters.append(RelationshipCandidate.status == request.suggestion_status)
        boundary_candidates = (
            await session.scalars(
                select(RelationshipCandidate)
                .where(*boundary_filters)
                .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
                .limit(probe_limit)
            )
        ).all()

        if (
            len(constraints) == probe_limit
            or len(candidates) == probe_limit
            or len(boundary_candidates) == probe_limit
        ):
            traversal.truncation_reasons.add("EDGE_SCAN_LIMIT")

        await policy.admit(
            policy.undecided(
                {
                    other_id
                    for candidate in boundary_candidates
                    for other_id in (candidate.datasource_id, candidate.target_datasource_id)
                }
            )
        )
        allowed_boundary_candidates = [
            candidate
            for candidate in boundary_candidates
            if policy.is_authorized(candidate.datasource_id)
            and policy.is_authorized(candidate.target_datasource_id)
        ]

        for constraint in constraints:
            node_datasource_id[constraint.table_id] = constraint.datasource_id
            if constraint.referenced_table_id is not None:
                node_datasource_id[constraint.referenced_table_id] = constraint.datasource_id
        for candidate in (*candidates, *allowed_boundary_candidates):
            node_datasource_id[candidate.source_table_id] = candidate.datasource_id
            node_datasource_id[candidate.target_table_id] = candidate.target_datasource_id

        links = [
            GraphLink(
                edge_id=f"constraint:{constraint.id}",
                source_node_id=constraint.table_id,
                target_node_id=constraint.referenced_table_id,
            )
            for constraint in constraints
            if constraint.referenced_table_id is not None
        ]
        links.extend(
            GraphLink(
                edge_id=f"candidate:{candidate.id}",
                source_node_id=candidate.source_table_id,
                target_node_id=candidate.target_table_id,
            )
            for candidate in (*candidates, *allowed_boundary_candidates)
        )
        remaining_edge_capacity = request.edge_limit - len(encountered_links)
        for link in sorted(links, key=lambda item: item.edge_id)[:remaining_edge_capacity]:
            encountered_links.setdefault(link.edge_id, link)
        if len(links) > remaining_edge_capacity:
            traversal.truncation_reasons.add("EDGE_LIMIT")

        expansion = expand_cross_source_frontier(
            frontier=frontier,
            visited=visited,
            links=list(encountered_links.values()),
            direction=request.direction,
            depth=current_depth,
            node_limit=request.node_limit,
            node_datasource_id=node_datasource_id,
            is_datasource_authorized=policy.is_authorized,
        )
        if expansion.truncated:
            traversal.truncation_reasons.add("NODE_LIMIT")
        frontier = set(expansion.node_ids)
        visited.update(frontier)
        traversal.node_depths.update(expansion.node_depths)

    return traversal


async def _load_seed_totals(
    session: AsyncSession, request: NeighborhoodRequest
) -> tuple[int, int, int, int]:
    """Inventory counts for the seed datasource alone.

    Deliberately not widened to whatever the traversal crossed into: these
    numbers tell the caller how much of *their* datasource the bounded view
    represents, and making them depend on grants would leak the grant state.
    """
    datasource_id = request.datasource.id
    total_tables = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataTable)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
            )
        )
        or 0
    )
    declared_total = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataConstraint)
            .where(
                MetadataConstraint.datasource_id == datasource_id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
            )
        )
        or 0
    )
    suggested_total = int(
        await session.scalar(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(RelationshipCandidate.datasource_id == datasource_id)
        )
        or 0
    )
    pending_suggestions = int(
        await session.scalar(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(
                RelationshipCandidate.datasource_id == datasource_id,
                RelationshipCandidate.status == "PENDING",
            )
        )
        or 0
    )
    return total_tables, declared_total, suggested_total, pending_suggestions


async def project_neighborhood(
    session: AsyncSession,
    request: NeighborhoodRequest,
    traversal: NeighborhoodTraversal,
    policy: DatasourceCrossingPolicy,
) -> KnowledgeGraphRead:
    """Turn a set of reached table ids into the value-free response.

    The datasource filter here is belt-and-braces: `policy` only ever widened
    `authorized_datasource_ids` through an approved crossing, so re-applying it
    changes nothing today. It is re-applied anyway because a table from a
    datasource that never cleared the grant and the gate must not be able to
    reach the response even if something upstream had a bug.
    """
    authorized = policy.authorized_datasource_ids
    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.id.in_(traversal.visited),
                MetadataTable.datasource_id.in_(authorized),
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    active_table_ids = {table.id for table, _, _ in table_rows}
    table_labels = {
        table.id: f"{catalog.name}.{schema.name}.{table.name}"
        for table, schema, catalog in table_rows
    }
    columns = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id.in_(active_table_ids),
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    columns_by_id: dict[UUID, MetadataColumn] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
        columns_by_id[column.id] = column

    final_constraints = (
        await session.scalars(
            select(MetadataConstraint)
            .where(
                MetadataConstraint.datasource_id.in_(authorized),
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
                MetadataConstraint.table_id.in_(active_table_ids),
                MetadataConstraint.referenced_table_id.in_(active_table_ids),
            )
            .order_by(MetadataConstraint.id)
            .limit(request.edge_limit + 1)
        )
    ).all()
    final_candidate_filters: list[ColumnElement[bool]] = [
        RelationshipCandidate.datasource_id.in_(authorized),
        RelationshipCandidate.target_datasource_id.in_(authorized),
        RelationshipCandidate.source_table_id.in_(active_table_ids),
        RelationshipCandidate.target_table_id.in_(active_table_ids),
    ]
    if request.suggestion_status != "ALL":
        final_candidate_filters.append(RelationshipCandidate.status == request.suggestion_status)
    final_candidates = (
        await session.scalars(
            select(RelationshipCandidate)
            .where(*final_candidate_filters)
            .order_by(RelationshipCandidate.confidence.desc(), RelationshipCandidate.id)
            .limit(request.edge_limit + 1)
        )
    ).all()

    edge_records: list[GraphEdgeRead] = [
        GraphEdgeRead(
            id=f"constraint:{constraint.id}",
            edge_type="DECLARED_FOREIGN_KEY",
            source_node_id=constraint.table_id,
            target_node_id=constraint.referenced_table_id,
            source_label=table_labels[constraint.table_id],
            target_label=table_labels[constraint.referenced_table_id],
            source_columns=constraint.columns,
            target_columns=constraint.referenced_columns,
            status="DECLARED",
            confidence=1.0,
            evidence={"source": "DATABASE_CONSTRAINT", "source_values_inspected": False},
        )
        for constraint in final_constraints
        if constraint.referenced_table_id is not None
    ]
    edge_records.extend(
        GraphEdgeRead(
            id=f"candidate:{candidate.id}",
            edge_type="SUGGESTED_RELATIONSHIP",
            source_node_id=candidate.source_table_id,
            target_node_id=candidate.target_table_id,
            source_label=table_labels[candidate.source_table_id],
            target_label=table_labels[candidate.target_table_id],
            source_columns=[columns_by_id[candidate.source_column_id].name],
            target_columns=[columns_by_id[candidate.target_column_id].name],
            status=candidate.status,
            confidence=candidate.confidence,
            evidence=candidate.evidence,
            candidate_id=candidate.id,
        )
        for candidate in final_candidates
        if candidate.source_column_id in columns_by_id
        and candidate.target_column_id in columns_by_id
    )
    truncation_reasons = set(traversal.truncation_reasons)
    if len(edge_records) > request.edge_limit:
        truncation_reasons.add("EDGE_LIMIT")
        edge_records = edge_records[: request.edge_limit]

    inbound = Counter(edge.target_node_id for edge in edge_records)
    outbound = Counter(edge.source_node_id for edge in edge_records)
    nodes = sorted(
        (
            GraphNodeRead(
                id=table.id,
                node_type="TABLE",
                label=table.name,
                qualified_name=table_labels[table.id],
                object_type=table.object_type,
                status=table.status,
                column_count=len(columns_by_table.get(table.id, [])),
                sensitive_column_count=sum(
                    column.classification in SENSITIVE_CLASSES
                    for column in columns_by_table.get(table.id, [])
                ),
                depth=traversal.node_depths.get(table.id, request.depth),
                inbound_edge_count=inbound[table.id],
                outbound_edge_count=outbound[table.id],
            )
            for table, _, _ in table_rows
        ),
        key=lambda node: (node.depth, node.qualified_name),
    )

    total_tables, declared_total, suggested_total, pending_suggestions = await _load_seed_totals(
        session, request
    )
    return KnowledgeGraphRead(
        datasource_id=request.datasource.id,
        nodes=nodes,
        edges=edge_records,
        total_tables=total_tables,
        total_declared_edges=declared_total,
        total_suggested_edges=suggested_total,
        pending_suggestions=pending_suggestions,
        truncated=bool(truncation_reasons),
        focus_node_id=request.focus.id,
        direction=request.direction,
        requested_depth=request.depth,
        returned_node_count=len(nodes),
        returned_edge_count=len(edge_records),
        node_limit=request.node_limit,
        edge_limit=request.edge_limit,
        truncation_reasons=sorted(truncation_reasons),
    )


async def build_knowledge_graph_neighborhood(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    request: NeighborhoodRequest,
) -> KnowledgeGraphRead:
    """Expand and project one bounded neighborhood.

    Traversal and projection share one `DatasourceCrossingPolicy` instance so
    the set of datasources whose rows may appear is decided once and applied to
    both -- an edge the traversal followed and a row the projection returns can
    never disagree about who was authorized.
    """
    policy = DatasourceCrossingPolicy(session, context, settings, seed=request.datasource)
    traversal = await traverse_neighborhood(session, request, policy)
    return await project_neighborhood(session, request, traversal, policy)
