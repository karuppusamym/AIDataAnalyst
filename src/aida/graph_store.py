"""The graph-store read path as a configurable per-organization port (ADR-0020).

`Docs/10-architecture/adr/ADR-0020-graph-store-decision.md`'s amendment,
2026-08-30: keep Neo4j, and make it a per-organization setting rather than a
fixed default. This module is the port, copying `aida/vector_store.py`'s shape
exactly, per the amendment's own instruction ("The port already has a
precedent in this codebase and should copy it exactly").

**Three adapters, selected by `resolve_graph_store_backend`.**

* **`postgres` (default, certified).** Reads the same relational lineage
  tables (`metadata_catalog`/`metadata_schema`/`metadata_table`/
  `metadata_column`/`metadata_constraint`) `aida.unified_lineage_api` already
  builds its merged graph from, and the same bounded, depth- and node-capped
  traversal (`aida.unified_lineage.traverse`) it already uses. Nothing here is
  a second implementation of that traversal -- `PostgresGraphStore` is handed
  the existing graph-building function as a dependency
  (`build_snapshot=_build_unified_graph`) so the two can never drift apart.
* **`neo4j`.** The existing Neo4j read logic (previously inline in `api.py`
  and `lineage_graph_store.py`), unchanged in query shape, moved here so every
  Cypher statement in the request-path read surface lives in one auditable
  module. **Uncertified (INV-9).** No Neo4j runs in this suite or in CI today
  (`tests/test_inv1_single_authoritative_store.py` says so of itself), and E5
  -- the projection rebuild drill -- has never run
  (`Docs/60-delivery/03-tracker.md` row E5). `resolve_graph_store_backend`
  reflects that: an organization may request `neo4j`, but it is only actually
  served when the process-wide `Settings.lineage_neo4j_read_enabled` operator
  flag is also on -- an org's request alone is not proof the backend is safe
  to serve, only that someone would like it served.
* **`disabled`.** An explicit refusal (`GraphStoreUnavailable`), never a
  silent empty result. Mirrors `vector_store.DisabledVectorIndex` exactly:
  "no results" and "this backend is switched off" must never look the same to
  a caller (INV-4).

**INV-1 scope.** This setting governs lineage and graph-exploration reads
only. It is never imported by `aida.authorization_gate` (the ABAC evaluator)
or by `aida.business_graph` (the classification/ownership roll-up) --
`tests/test_graph_store_inv1_isolation.py` asserts both the absence of that
import edge and, behaviourally, that flipping the setting changes nothing an
authorization decision or a roll-up value depends on. PostgreSQL remains
authoritative regardless of which adapter is selected; Neo4j is a rebuildable
projection, never a second source of truth (see
`tests/test_inv1_single_authoritative_store.py`).

**Conformance is the actual deliverable.** A per-organization setting that
lets two backends silently disagree turns a config flag into a correctness
surface. `tests/test_graph_store_conformance.py` asserts the `postgres` and
`neo4j` adapters return identical node sets, ordering, cap behaviour and
truncation reasons for the same traversal -- not merely that both return
something. The `neo4j` half of that suite requires a reachable Neo4j and
skips cleanly (with a stated reason, never a fabricated pass) when one is not
available, exactly like `tests/test_migration_orm_drift.py` does for
PostgreSQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import structlog
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from aida.classification import SENSITIVE_CLASSES
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
)
from aida.schemas import UnifiedLineageImpactNodeRead, UnifiedLineageImpactRead
from aida.unified_lineage import TraversalResult, UnifiedLink, traverse
from atlas.platform.config import Settings
from atlas.platform.db import Base, TimestampMixin

logger = structlog.get_logger(__name__)

GraphStoreBackend = Literal["postgres", "neo4j", "disabled"]
_VALID_BACKENDS: frozenset[str] = frozenset({"postgres", "neo4j", "disabled"})
# `_build_unified_graph`'s own parameter type, duplicated rather than imported so this
# leaf-ish module never has to import `aida.unified_lineage_api` (which would cycle back
# here once that module imports the port below).
_SuggestionStatus = Literal["ALL", "PENDING", "APPROVED", "REJECTED"]


class GraphStoreUnavailable(RuntimeError):
    """The configured backend cannot serve. Fail closed rather than degrade (INV-4)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class GraphSummaryProjection:
    """What the graph store currently reports for one datasource.

    `aida.api.get_graph_summary` reconciles this against PostgreSQL's own
    authoritative counts to compute `projection_status`/`projection_lag` --
    this dataclass is only the graph-side half.
    """

    catalogs: int
    schemas: int
    tables: int
    columns: int
    sensitive_columns: int
    constraints: int
    foreign_key_relationships: int


class GraphNodeInfo(Protocol):
    """Structural shape a lineage-graph node must have. `_NodeInfo` in
    `aida.unified_lineage_api` satisfies this without importing it."""

    node_kind: str
    label: str
    qualified_name: str


class LineageGraphSnapshot(Protocol):
    """Structural shape of a built lineage graph. `_UnifiedGraph` in
    `aida.unified_lineage_api` satisfies this without importing it."""

    nodes: Mapping[str, GraphNodeInfo]
    links: Sequence[UnifiedLink]


class SnapshotBuilder(Protocol):
    """Matches `aida.unified_lineage_api._build_unified_graph`'s signature exactly,
    which is what lets that function be passed in as `PostgresGraphStore`'s
    `build_snapshot` without this module ever importing `unified_lineage_api`."""

    async def __call__(
        self,
        session: AsyncSession,
        datasource: DataSource,
        *,
        node_limit: int,
        edge_limit: int,
        suggestion_status: _SuggestionStatus,
    ) -> LineageGraphSnapshot: ...


class GraphStorePort(ABC):
    """The port. Every backend is a read path over lineage/exploration data,
    never a source of truth for authorization or classification (INV-1)."""

    name: str

    @abstractmethod
    async def lineage_impact(
        self,
        session: AsyncSession,
        datasource: DataSource,
        node_id: str,
        *,
        depth: int,
        node_limit: int,
    ) -> UnifiedLineageImpactRead | None:
        """Bounded transitive upstream/downstream impact from one node.

        Returns `None` when the node is unknown to this backend (never an
        error -- the caller falls through to another backend or reports
        "not found"), and raises `GraphStoreUnavailable` when the backend
        itself cannot be used at all (`disabled`, or a `postgres` adapter
        built without a snapshot builder).
        """
        ...

    @abstractmethod
    async def graph_summary(
        self, session: AsyncSession, datasource: DataSource
    ) -> GraphSummaryProjection:
        """Node/edge counts this backend currently reports for one datasource.

        Raises `GraphStoreUnavailable` rather than returning a partial or
        zeroed count when the backend cannot answer.
        """
        ...


def _traversal_rows(
    result: TraversalResult, *, snapshot: LineageGraphSnapshot, seed: str
) -> list[UnifiedLineageImpactNodeRead]:
    """Shape a bounded BFS traversal into the API's node-row schema.

    Ordering is `(depth, node_id)`, matching `TraversalResult.node_depths`
    exhaustively -- the same tie-break the `neo4j` adapter's Cypher applies
    with `ORDER BY depth, node_id`, which is what makes the two adapters'
    output comparable at all (see `tests/test_graph_store_conformance.py`).
    """
    rows: list[UnifiedLineageImpactNodeRead] = []
    for candidate_id, candidate_depth in sorted(
        result.node_depths.items(), key=lambda item: (item[1], item[0])
    ):
        if candidate_id == seed:
            continue
        info = snapshot.nodes.get(candidate_id)
        if info is None:
            continue
        rows.append(
            UnifiedLineageImpactNodeRead(
                node_id=candidate_id,
                node_kind=info.node_kind,
                label=info.label,
                qualified_name=info.qualified_name,
                depth=candidate_depth,
                contributing_edge_sources=sorted(
                    result.contributing_edge_sources.get(candidate_id, frozenset())
                ),
            )
        )
    return rows


class PostgresGraphStore(GraphStorePort):
    """Reads the relational lineage tables directly. The default, and the
    certified adapter -- no second system to run, back up, or drill, and no
    projection that can lag or disagree with what it is projected from.

    `lineage_impact` is a thin traversal wrapper around a caller-supplied
    `build_snapshot` (normally `aida.unified_lineage_api._build_unified_graph`)
    rather than a reimplementation of it: that function already merges
    declared foreign keys, human-approved relationship candidates, dbt
    dependencies, OpenLineage runs and parsed view/procedure lineage into one
    graph, and duplicating that here would be a second, divergence-prone copy
    of business logic this module has no reason to own. `graph_summary` needs
    no such dependency -- it is a handful of `COUNT` queries against the same
    tables, done directly.
    """

    name = "postgres"

    def __init__(
        self,
        *,
        build_snapshot: SnapshotBuilder | None = None,
        node_cap: int = 2_000,
        edge_cap: int = 10_000,
    ) -> None:
        self._build_snapshot = build_snapshot
        self._node_cap = node_cap
        self._edge_cap = edge_cap

    async def lineage_impact(
        self,
        session: AsyncSession,
        datasource: DataSource,
        node_id: str,
        *,
        depth: int,
        node_limit: int,
    ) -> UnifiedLineageImpactRead | None:
        if self._build_snapshot is None:
            raise GraphStoreUnavailable("POSTGRES_GRAPH_STORE_SNAPSHOT_BUILDER_NOT_CONFIGURED")
        snapshot = await self._build_snapshot(
            session,
            datasource,
            node_limit=self._node_cap,
            edge_limit=self._edge_cap,
            suggestion_status="APPROVED",
        )
        focus = snapshot.nodes.get(node_id)
        if focus is None:
            return None
        links = list(snapshot.links)
        upstream = traverse(
            seed=node_id, links=links, direction="REFERENCES",
            max_depth=depth, node_limit=node_limit,
        )
        downstream = traverse(
            seed=node_id, links=links, direction="REFERENCED_BY",
            max_depth=depth, node_limit=node_limit,
        )
        return UnifiedLineageImpactRead(
            datasource_id=datasource.id,
            focus_node_id=node_id,
            focus_node_kind=focus.node_kind,
            focus_label=focus.qualified_name,
            upstream=_traversal_rows(upstream, snapshot=snapshot, seed=node_id),
            downstream=_traversal_rows(downstream, snapshot=snapshot, seed=node_id),
            requested_depth=depth,
            node_limit=node_limit,
            upstream_truncated=upstream.truncated,
            downstream_truncated=downstream.truncated,
        )

    async def graph_summary(
        self, session: AsyncSession, datasource: DataSource
    ) -> GraphSummaryProjection:
        catalogs = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataCatalog)
                .where(MetadataCatalog.datasource_id == datasource.id)
            )
            or 0
        )
        schemas = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataSchema)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(MetadataCatalog.datasource_id == datasource.id)
            )
            or 0
        )
        tables = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataTable)
                .where(MetadataTable.datasource_id == datasource.id)
            )
            or 0
        )
        columns = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataColumn)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(MetadataTable.datasource_id == datasource.id)
            )
            or 0
        )
        constraints = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataConstraint)
                .where(MetadataConstraint.datasource_id == datasource.id)
            )
            or 0
        )
        sensitive_columns = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataColumn)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(
                    MetadataTable.datasource_id == datasource.id,
                    MetadataColumn.classification.in_(SENSITIVE_CLASSES),
                )
            )
            or 0
        )
        foreign_key_relationships = int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataConstraint)
                .where(
                    MetadataConstraint.datasource_id == datasource.id,
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.referenced_table_id.is_not(None),
                )
            )
            or 0
        )
        return GraphSummaryProjection(
            catalogs=catalogs,
            schemas=schemas,
            tables=tables,
            columns=columns,
            sensitive_columns=sensitive_columns,
            constraints=constraints,
            foreign_key_relationships=foreign_key_relationships,
        )


class Neo4jGraphStore(GraphStorePort):
    """The existing Neo4j projection, promoted from an inline read-through
    cache to a selectable backend. Query shape is unchanged from before this
    module existed -- moved, not rewritten -- from `lineage_graph_store.py`
    (`lineage_impact`) and `api.py`'s `get_graph_summary` handler
    (`graph_summary`).

    **Uncertified (INV-9).** See the module docstring. `resolve_graph_store_backend`
    is what actually gates whether this class gets constructed for real traffic.
    """

    name = "neo4j"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def lineage_impact(
        self,
        session: AsyncSession,
        datasource: DataSource,
        node_id: str,
        *,
        depth: int,
        node_limit: int,
    ) -> UnifiedLineageImpactRead | None:
        """Read a bounded projection; return None so PostgreSQL can remain the
        fallback authority (matches `PostgresGraphStore.lineage_impact`'s own
        "unknown node -> None" contract, not a distinct failure shape)."""
        driver = AsyncGraphDatabase.driver(
            self._settings.neo4j_uri,
            auth=(self._settings.neo4j_user, self._settings.neo4j_password),
            connection_timeout=0.5,
        )
        prefix = f"{datasource.organization_id}:{datasource.id}:"
        projection_key = f"{prefix}{node_id}"
        try:
            async with driver.session() as graph_session:
                focus_result = await graph_session.run(
                    """
                    MATCH (focus:UnifiedLineageNode {projection_key: $projection_key})
                    RETURN focus.platform_id AS node_id,
                           focus.node_kind AS node_kind,
                           focus.label AS label,
                           focus.qualified_name AS qualified_name
                    """,
                    projection_key=projection_key,
                )
                focus_record = await focus_result.single()
                if focus_record is None:
                    return None
                path_limit = max(node_limit * depth * 4, node_limit)

                async def traverse_direction(
                    direction: str,
                ) -> tuple[list[UnifiedLineageImpactNodeRead], bool]:
                    # `UNIFIED_LINEAGE` edges point dependent -> dependency (the same
                    # convention `aida.unified_lineage.UnifiedLink` documents: "source_id
                    # is the dependent node and target_id is the node it depends on").
                    # UPSTREAM ("what this node depends on") therefore walks *forward*
                    # from focus, following edges in their stored direction; DOWNSTREAM
                    # ("what depends on this node") walks backward, finding nodes with a
                    # forward path *into* focus. Building the conformance suite
                    # (`tests/test_graph_store_conformance.py`) against
                    # `PostgresGraphStore` -- whose `aida.unified_lineage.traverse`
                    # unambiguously implements that convention -- found this pattern
                    # assignment swapped from when this lived in `lineage_graph_store.py`:
                    # nothing had ever run it against a real Neo4j to notice (INV-9).
                    pattern = (
                        f"(focus)-[:UNIFIED_LINEAGE*1..{depth}]->(node)"
                        if direction == "UPSTREAM"
                        else f"(node)-[:UNIFIED_LINEAGE*1..{depth}]->(focus)"
                    )
                    result = await graph_session.run(
                        f"""
                        MATCH (focus:UnifiedLineageNode {{projection_key: $projection_key}})
                        MATCH p={pattern}
                        WHERE all(rel IN relationships(p)
                                  WHERE rel.organization_id = $organization_id
                                    AND rel.datasource_id = $datasource_id)
                        RETURN node.platform_id AS node_id,
                               node.node_kind AS node_kind,
                               node.label AS label,
                               node.qualified_name AS qualified_name,
                               length(p) AS depth,
                               [rel IN relationships(p) | rel.edge_source] AS edge_sources
                        ORDER BY depth, node_id
                        LIMIT $path_limit
                        """,
                        projection_key=projection_key,
                        organization_id=str(datasource.organization_id),
                        datasource_id=str(datasource.id),
                        path_limit=path_limit,
                    )
                    records = [record.data() async for record in result]
                    rows, truncated = _neo4j_impact_rows(records, node_limit=node_limit)
                    return rows, truncated or len(records) >= path_limit

                upstream, upstream_truncated = await traverse_direction("UPSTREAM")
                downstream, downstream_truncated = await traverse_direction("DOWNSTREAM")
                focus = focus_record.data()
                return UnifiedLineageImpactRead(
                    datasource_id=datasource.id,
                    focus_node_id=str(focus["node_id"]),
                    focus_node_kind=str(focus["node_kind"]),
                    focus_label=str(focus["qualified_name"]),
                    upstream=upstream,
                    downstream=downstream,
                    requested_depth=depth,
                    node_limit=node_limit,
                    upstream_truncated=upstream_truncated,
                    downstream_truncated=downstream_truncated,
                )
        except (Neo4jError, ServiceUnavailable, OSError) as exc:
            logger.warning("lineage_projection_read_failed", error=type(exc).__name__)
            return None
        finally:
            await driver.close()

    async def graph_summary(
        self, session: AsyncSession, datasource: DataSource
    ) -> GraphSummaryProjection:
        driver = AsyncGraphDatabase.driver(
            self._settings.neo4j_uri,
            auth=(self._settings.neo4j_user, self._settings.neo4j_password),
        )
        try:
            record = await driver.execute_query(
                """
                MATCH (catalog:Catalog {
                  datasource_id: $datasource_id,
                  organization_id: $organization_id
                })
                OPTIONAL MATCH (catalog)-[:HAS_SCHEMA]->(schema:Schema)
                OPTIONAL MATCH (schema)-[:HAS_TABLE]->(table:Table)
                OPTIONAL MATCH (table)-[:HAS_COLUMN]->(column:Column)
                OPTIONAL MATCH (table)-[:HAS_CONSTRAINT]->(constraint:Constraint)
                OPTIONAL MATCH (constraint)-[reference:REFERENCES]->(:Table)
                RETURN count(DISTINCT catalog) AS catalogs,
                       count(DISTINCT schema) AS schemas,
                       count(DISTINCT table) AS tables,
                       count(DISTINCT column) AS columns,
                       count(DISTINCT CASE
                         WHEN column.classification IN
                           ['PII', 'PCI', 'PHI', 'SECRET', 'CONFIDENTIAL']
                         THEN column
                       END) AS sensitive_columns
                       ,count(DISTINCT constraint) AS constraints
                       ,count(DISTINCT reference) AS foreign_key_relationships
                """,
                datasource_id=str(datasource.id),
                organization_id=str(datasource.organization_id),
                database_="neo4j",
            )
            summary = record.records[0]
        except Exception as exc:
            raise GraphStoreUnavailable("NEO4J_GRAPH_SUMMARY_UNAVAILABLE") from exc
        finally:
            await driver.close()
        return GraphSummaryProjection(
            catalogs=int(summary["catalogs"]),
            schemas=int(summary["schemas"]),
            tables=int(summary["tables"]),
            columns=int(summary["columns"]),
            sensitive_columns=int(summary["sensitive_columns"]),
            constraints=int(summary["constraints"]),
            foreign_key_relationships=int(summary["foreign_key_relationships"]),
        )


def _neo4j_impact_rows(
    records: Sequence[Mapping[str, Any]], *, node_limit: int
) -> tuple[list[UnifiedLineageImpactNodeRead], bool]:
    by_node: dict[str, dict[str, Any]] = {}
    for record in records:
        node_id = str(record["node_id"])
        current = by_node.get(node_id)
        depth = int(record["depth"])
        sources = {str(item) for item in record.get("edge_sources", []) if item}
        if current is None:
            by_node[node_id] = {
                "node_id": node_id,
                "node_kind": str(record["node_kind"]),
                "label": str(record["label"]),
                "qualified_name": str(record["qualified_name"]),
                "depth": depth,
                "sources": sources,
            }
        else:
            current["depth"] = min(int(current["depth"]), depth)
            current["sources"].update(sources)
    truncated = len(by_node) > node_limit
    selected = sorted(by_node.values(), key=lambda item: (int(item["depth"]), str(item["node_id"])))
    return (
        [
            UnifiedLineageImpactNodeRead(
                node_id=item["node_id"],
                node_kind=item["node_kind"],
                label=item["label"],
                qualified_name=item["qualified_name"],
                depth=item["depth"],
                contributing_edge_sources=sorted(item["sources"]),
            )
            for item in selected[:node_limit]
        ],
        truncated,
    )


class DisabledGraphStore(GraphStorePort):
    """Graph-store reads are off for this organization. Says so; never
    pretends an empty answer is a real one (mirrors `vector_store.DisabledVectorIndex`)."""

    name = "disabled"

    async def lineage_impact(
        self,
        session: AsyncSession,
        datasource: DataSource,
        node_id: str,
        *,
        depth: int,
        node_limit: int,
    ) -> UnifiedLineageImpactRead | None:
        raise GraphStoreUnavailable("GRAPH_STORE_DISABLED")

    async def graph_summary(
        self, session: AsyncSession, datasource: DataSource
    ) -> GraphSummaryProjection:
        raise GraphStoreUnavailable("GRAPH_STORE_DISABLED")


def build_graph_store(
    backend: GraphStoreBackend,
    settings: Settings,
    *,
    build_snapshot: SnapshotBuilder | None = None,
) -> GraphStorePort:
    """Construct the adapter for one already-resolved backend name.

    `build_snapshot` is only meaningful for `postgres` and only needed by its
    `lineage_impact` method -- `graph_summary` callers (`aida.api`) can omit
    it. Raises rather than defaulting to `postgres` on an unrecognised value:
    a typo in a persisted setting must be loud, not silently downgraded.
    """
    if backend == "disabled":
        return DisabledGraphStore()
    if backend == "neo4j":
        return Neo4jGraphStore(settings)
    if backend == "postgres":
        return PostgresGraphStore(build_snapshot=build_snapshot)
    raise GraphStoreUnavailable("UNKNOWN_GRAPH_STORE_BACKEND")


# --- Per-organization admin setting -----------------------------------------
#
# ST-05/06/07 (`Docs/40-engineering/06-refactor-plan.md` Phase 3) is splitting
# `models.py` into per-module files concurrently with this work. Declaring this
# table here rather than in the middle of that split -- and registering it on
# `Base.metadata` with one import line from `models.py` -- is the same
# collision-avoidance pattern `aida.envelope_models` already uses (see that
# module's docstring). Group J owns this table's model, migration and tests;
# nothing else in this file depends on `aida.models`.


class GraphStoreOrganizationSetting(Base, TimestampMixin):
    """One row per organization that has chosen a non-default graph-store
    backend (ADR-0020's per-organization setting). No row means "postgres",
    the default -- `resolve_graph_store_backend` below treats a missing row
    and an explicit `postgres` row identically, so seeding a row for every
    organization was never required.
    """

    __tablename__ = "graph_store_organization_setting"
    __table_args__ = (
        UniqueConstraint("organization_id"),
        CheckConstraint(
            "backend IN ('postgres', 'neo4j', 'disabled')",
            name="ck_graph_store_organization_setting_backend",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    backend: Mapped[str] = mapped_column(String(20), nullable=False, default="postgres")


async def get_organization_graph_store_backend(
    session: AsyncSession, organization_id: UUID
) -> str | None:
    """The raw persisted choice, or `None` if the organization has never set one."""
    backend = await session.scalar(
        select(GraphStoreOrganizationSetting.backend).where(
            GraphStoreOrganizationSetting.organization_id == organization_id
        )
    )
    return str(backend) if backend is not None else None


async def set_organization_graph_store_backend(
    session: AsyncSession, organization_id: UUID, backend: GraphStoreBackend
) -> GraphStoreOrganizationSetting:
    """Upsert one organization's graph-store choice. Caller commits/flushes as usual."""
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"unknown graph store backend: {backend!r}")
    existing = await session.scalar(
        select(GraphStoreOrganizationSetting).where(
            GraphStoreOrganizationSetting.organization_id == organization_id
        )
    )
    if existing is None:
        existing = GraphStoreOrganizationSetting(organization_id=organization_id, backend=backend)
        session.add(existing)
    else:
        existing.backend = backend
    await session.flush()
    return existing


async def resolve_graph_store_backend(
    session: AsyncSession, organization_id: UUID, settings: Settings
) -> GraphStoreBackend:
    """The effective backend for one organization's graph reads.

    Resolution order: the organization's own row, else the process-wide
    default (`Settings.graph_store_backend`, itself default `postgres`).

    INV-9: a `neo4j` choice additionally requires the process-wide
    `Settings.lineage_neo4j_read_enabled` operator flag. An organization can
    ask for `neo4j`; an operator who has not turned that flag on has not
    certified it, and this function will not silently promise a backend
    nothing has proven safe to serve -- it returns `postgres` instead.
    """
    configured = await get_organization_graph_store_backend(session, organization_id)
    if configured is None:
        configured = settings.graph_store_backend
    if configured not in _VALID_BACKENDS:
        logger.warning("graph_store_backend_unrecognised", configured=configured)
        return "postgres"
    if configured == "neo4j" and not settings.lineage_neo4j_read_enabled:
        return "postgres"
    return configured  # type: ignore[return-value]
