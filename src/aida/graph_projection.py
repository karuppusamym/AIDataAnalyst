"""Bounded reading of a datasource's metadata for the Neo4j graph projection.

Two invariants live here, and nothing else does.

**Memory is bounded by the chunk size, never by the estate.** The projector
used to call a `load_projection` that materialised every catalog, schema,
table, column and constraint of a datasource into Python lists and returned
them as one dict -- so peak memory was a function of how big the customer's
warehouse is, and the only reason it had not fallen over was that no large
source had been rebuilt (review 2026-09-05, section 5, "Graph projection").
`iter_projection_chunks` replaces that with keyset pagination over each level
in parent-before-child order, yielding at most `chunk_size` rows at a time and
selecting *columns*, not ORM entities, so nothing accumulates in the session's
identity map behind the caller's back.

**Scope is the catalog hierarchy, not a denormalised column.** Every level is
reached by joining up through `metadata_schema` -> `metadata_catalog` to the
datasource, exactly the containment the previous implementation expressed with
its chain of `id.in_(...)` lists. `metadata_table` and `metadata_constraint`
also carry their own `datasource_id`, and filtering on that would be cheaper --
but it would be a *second* definition of "belongs to this datasource" that could
disagree with the first after a bad write, so it is deliberately not used.

The rows this yields carry `organization_id` **and** `datasource_id` at every
level. The projector needs both to reconcile deletions: a node whose source row
is gone is found by "same tenant, same datasource, older generation", and that
predicate cannot be written against a node that only knows its parent's id.

Tenant fairness (`TenantBudget`, `TenantFairQueue`) lives here too because it
bounds the same sweep: a single organization with a million-column estate must
not be able to hold every other tenant's projection behind it. The review is
explicit that the *policy* needs product input, so the budget is an explicit,
conservatively-defaulted, operator-settable number and the backlog it governs
is measured -- see `Docs/10-architecture/13-connection-pool-and-worker-budgets.md`.
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from aida.models import (
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
)
from aida.projection_metrics import PROJECTION_LEVELS

# How many source rows one chunk may hold. 5_000 is not a guess: it is what
# `scripts/scale_harness/gp1_measure_projection_memory.py` measured on a
# 210,051-row synthetic estate (5,000 tables x 40 columns), median of three
# runs, against the whole-estate implementation this replaced --
#
#   whole-estate load   624.7 MiB peak    8.4-12.3 s
#   chunk =  1_000        2.7 MiB peak       33.1 s
#   chunk =  5_000       10.4 MiB peak       10.8 s
#   chunk = 10_000       18.1 MiB peak        9.1 s
#
# 5_000 is the point where memory is still bounded at a small fraction of the
# estate (60x below the old peak) and the extra round trips have stopped
# costing wall time. Smaller chunks keep paying per-page query overhead for
# memory nobody needs saved. The measurement was taken against SQLite in a
# single process, so it bounds the *materialisation* cost, not a Postgres
# query plan and not Neo4j ingestion -- ADR-0020's E5 rebuild drill has still
# never run, and this default should be re-measured when it does.
DEFAULT_PROJECTION_CHUNK_SIZE = 5_000
PROJECTION_CHUNK_SIZE_ENV = "AIDA_GRAPH_PROJECTION_CHUNK_ROWS"

# Per-tenant fair-share defaults. Deliberately conservative: 8 events is a
# small enough round that no tenant waits long behind another, and large enough
# that the common case (one tenant with a burst of discovery events, nobody
# else waiting) never pays for the fairness machinery at all -- with a single
# tenant in the buffer the scheduler never rotates. `max_buffered_events`
# is the memory bound on the buffer itself; reaching it is backpressure, not
# an error, and the consumer stops fetching until the buffer drains.
DEFAULT_TENANT_EVENT_BUDGET = 8
DEFAULT_MAX_BUFFERED_EVENTS = 512
TENANT_EVENT_BUDGET_ENV = "AIDA_GRAPH_PROJECTOR_TENANT_EVENT_BUDGET"
MAX_BUFFERED_EVENTS_ENV = "AIDA_GRAPH_PROJECTOR_MAX_BUFFERED_EVENTS"


@dataclass(frozen=True, slots=True)
class ProjectionChunk:
    """One bounded page of one hierarchy level.

    `sequence` is the 0-based page number *within its level*, so a log line or
    a resumed rebuild can say where it got to without the caller counting rows.
    """

    level: str
    sequence: int
    rows: list[dict[str, Any]]


def resolve_chunk_size(environ: Mapping[str, str] | None = None) -> int:
    """The configured chunk size, clamped to a range that is still bounded.

    Read from the environment rather than from `Settings` because the chunk
    size is a projector-process tuning knob, not part of the platform's
    validated configuration contract; an out-of-range value is clamped rather
    than refused so a typo cannot stop the projector, but it can also never
    silently restore the unbounded behaviour this module exists to remove.
    """
    raw = (environ if environ is not None else os.environ).get(PROJECTION_CHUNK_SIZE_ENV)
    if not raw:
        return DEFAULT_PROJECTION_CHUNK_SIZE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PROJECTION_CHUNK_SIZE
    return max(50, min(value, 50_000))


@dataclass(frozen=True, slots=True)
class TenantBudget:
    """How much of one shared sweep a single organization may consume.

    `events_per_round` bounds *turn length*, not total throughput: a tenant
    alone in the buffer is never interrupted, because rotating to nobody would
    only add latency. `max_buffered_events` bounds the buffer's memory.
    """

    events_per_round: int = DEFAULT_TENANT_EVENT_BUDGET
    max_buffered_events: int = DEFAULT_MAX_BUFFERED_EVENTS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TenantBudget:
        source = environ if environ is not None else os.environ
        return cls(
            events_per_round=_positive_int(
                source.get(TENANT_EVENT_BUDGET_ENV), DEFAULT_TENANT_EVENT_BUDGET
            ),
            max_buffered_events=_positive_int(
                source.get(MAX_BUFFERED_EVENTS_ENV), DEFAULT_MAX_BUFFERED_EVENTS
            ),
        )


def _positive_int(raw: str | None, fallback: int) -> int:
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value >= 1 else fallback


@dataclass(frozen=True, slots=True)
class BacklogSnapshot:
    """What the fair-share buffer currently holds. The evidence a budget policy
    should be chosen from, rather than a number invented in advance."""

    events: int
    tenants: int
    oldest_age_seconds: float
    oldest_organization_id: str | None


@dataclass(slots=True)
class TenantFairQueue:
    """Round-robin scheduling of buffered projection events across tenants.

    Invariant: no organization is served more than `budget.events_per_round`
    consecutive events while another organization has work waiting. Arrival
    order is preserved *within* a tenant, so a tenant's own events are still
    projected in the order the log produced them.
    """

    budget: TenantBudget = field(default_factory=TenantBudget)
    _queues: dict[str, deque[tuple[dict[str, Any], datetime]]] = field(default_factory=dict)
    _rotation: deque[str] = field(default_factory=deque)
    _served_this_round: int = 0
    _yields: int = 0

    @property
    def yields(self) -> int:
        """How many times a tenant hit its budget and gave up the head slot."""
        return self._yields

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    @property
    def is_full(self) -> bool:
        return len(self) >= self.budget.max_buffered_events

    def offer(self, event: dict[str, Any], *, now: datetime | None = None) -> bool:
        """Buffer one event. Returns False when the buffer is already full --
        backpressure the caller must honour by draining before fetching more.
        """
        if self.is_full:
            return False
        organization_id = str(event.get("organization_id") or "")
        arrival = _event_occurred_at(event) or (now or datetime.now(UTC))
        queue = self._queues.get(organization_id)
        if queue is None:
            queue = deque()
            self._queues[organization_id] = queue
            self._rotation.append(organization_id)
        queue.append((event, arrival))
        return True

    def take(self) -> dict[str, Any] | None:
        """The next event to project, or None when the buffer is empty."""
        while self._rotation:
            head = self._rotation[0]
            queue = self._queues.get(head)
            if not queue:
                self._rotation.popleft()
                self._queues.pop(head, None)
                self._served_this_round = 0
                continue
            if self._served_this_round >= self.budget.events_per_round and len(self._rotation) > 1:
                # Another tenant is waiting and this one has had its turn.
                self._rotation.rotate(-1)
                self._served_this_round = 0
                self._yields += 1
                continue
            event, _arrival = queue.popleft()
            self._served_this_round += 1
            return event
        return None

    def backlog(self, *, now: datetime | None = None) -> BacklogSnapshot:
        """Oldest-backlog evidence, computed over what is buffered right now."""
        reference = now or datetime.now(UTC)
        oldest_age = 0.0
        oldest_org: str | None = None
        tenants = 0
        for organization_id, queue in self._queues.items():
            if not queue:
                continue
            tenants += 1
            age = (reference - queue[0][1]).total_seconds()
            if age > oldest_age:
                oldest_age = age
                oldest_org = organization_id
        return BacklogSnapshot(
            events=len(self),
            tenants=tenants,
            oldest_age_seconds=max(0.0, oldest_age),
            oldest_organization_id=oldest_org,
        )


def _event_occurred_at(event: Mapping[str, Any]) -> datetime | None:
    raw = event.get("occurred_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def event_lag_seconds(event: Mapping[str, Any], *, now: datetime | None = None) -> float | None:
    """Seconds between the event being emitted and this moment, or None when the
    envelope carries no usable `occurred_at`. Never negative: a projector clock
    running behind the emitter is a clock problem, not negative lag."""
    occurred_at = _event_occurred_at(event)
    if occurred_at is None:
        return None
    return max(0.0, ((now or datetime.now(UTC)) - occurred_at).total_seconds())


async def _keyset_pages(
    session: AsyncSession,
    statement: Select[Any],
    key_column: InstrumentedAttribute[Any],
    chunk_size: int,
) -> AsyncIterator[Sequence[Any]]:
    """Pages of `statement` ordered by `key_column`, which must also be the
    statement's first selected column. Keyset rather than OFFSET so page N
    costs the same as page 1 no matter how deep the rebuild has walked."""
    cursor: Any = None
    while True:
        page = statement if cursor is None else statement.where(key_column > cursor)
        rows = (await session.execute(page.order_by(key_column).limit(chunk_size))).all()
        if not rows:
            return
        yield rows
        if len(rows) < chunk_size:
            return
        cursor = rows[-1][0]


def _catalog_scope(datasource_id: UUID, organization_id: UUID) -> list[ColumnElement[bool]]:
    return [
        MetadataCatalog.datasource_id == datasource_id,
        MetadataCatalog.organization_id == organization_id,
    ]


async def iter_projection_chunks(
    session: AsyncSession,
    datasource_id: UUID,
    organization_id: UUID,
    *,
    tenancy_path: Mapping[str, str] | None = None,
    chunk_size: int | None = None,
) -> AsyncIterator[ProjectionChunk]:
    """Yield the datasource's metadata as bounded chunks, parents before children.

    Levels are emitted in `PROJECTION_LEVELS` order, which is the order the
    Neo4j MERGEs require: a `Schema` node's MERGE matches its `Catalog` parent,
    so the catalog must already be there. Within a level, order is by primary
    key and pages are keyset-driven.

    `tenancy_path` (ADR-0017 SS2) is merged into every row so a domain-scoped
    traversal can filter before it walks edges; it is resolved once by the
    caller rather than re-read per chunk.
    """
    size = chunk_size or resolve_chunk_size()
    path = dict(tenancy_path or {})
    common = {
        "organization_id": str(organization_id),
        "datasource_id": str(datasource_id),
        **path,
    }

    catalogs = select(MetadataCatalog.id, MetadataCatalog.name, MetadataCatalog.status).where(
        *_catalog_scope(datasource_id, organization_id)
    )
    sequence = 0
    async for rows in _keyset_pages(session, catalogs, MetadataCatalog.id, size):
        yield ProjectionChunk(
            level="catalogs",
            sequence=sequence,
            rows=[
                {"platform_id": str(row.id), "name": row.name, "status": row.status, **common}
                for row in rows
            ],
        )
        sequence += 1

    schemas = (
        select(
            MetadataSchema.id,
            MetadataSchema.catalog_id,
            MetadataSchema.name,
            MetadataSchema.status,
        )
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            *_catalog_scope(datasource_id, organization_id),
            MetadataSchema.organization_id == organization_id,
        )
    )
    sequence = 0
    async for rows in _keyset_pages(session, schemas, MetadataSchema.id, size):
        yield ProjectionChunk(
            level="schemas",
            sequence=sequence,
            rows=[
                {
                    "platform_id": str(row.id),
                    "catalog_id": str(row.catalog_id),
                    "name": row.name,
                    "status": row.status,
                    **common,
                }
                for row in rows
            ],
        )
        sequence += 1

    tables = (
        select(
            MetadataTable.id,
            MetadataTable.schema_id,
            MetadataTable.name,
            MetadataTable.object_type,
            MetadataTable.status,
        )
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            *_catalog_scope(datasource_id, organization_id),
            MetadataSchema.organization_id == organization_id,
            MetadataTable.organization_id == organization_id,
        )
    )
    sequence = 0
    async for rows in _keyset_pages(session, tables, MetadataTable.id, size):
        yield ProjectionChunk(
            level="tables",
            sequence=sequence,
            rows=[
                {
                    "platform_id": str(row.id),
                    "schema_id": str(row.schema_id),
                    "name": row.name,
                    "object_type": row.object_type,
                    "status": row.status,
                    **common,
                }
                for row in rows
            ],
        )
        sequence += 1

    columns = (
        select(
            MetadataColumn.id,
            MetadataColumn.table_id,
            MetadataColumn.name,
            MetadataColumn.ordinal_position,
            MetadataColumn.physical_type,
            MetadataColumn.classification,
            MetadataColumn.status,
        )
        .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            *_catalog_scope(datasource_id, organization_id),
            MetadataColumn.organization_id == organization_id,
        )
    )
    sequence = 0
    async for rows in _keyset_pages(session, columns, MetadataColumn.id, size):
        yield ProjectionChunk(
            level="columns",
            sequence=sequence,
            rows=[
                {
                    "platform_id": str(row.id),
                    "table_id": str(row.table_id),
                    "name": row.name,
                    "ordinal_position": row.ordinal_position,
                    "physical_type": row.physical_type,
                    "classification": row.classification,
                    "status": row.status,
                    **common,
                }
                for row in rows
            ],
        )
        sequence += 1

    constraints = (
        select(
            MetadataConstraint.id,
            MetadataConstraint.table_id,
            MetadataConstraint.name,
            MetadataConstraint.constraint_type,
            MetadataConstraint.columns,
            MetadataConstraint.referenced_table_id,
            MetadataConstraint.referenced_columns,
            MetadataConstraint.status,
        )
        .join(MetadataTable, MetadataTable.id == MetadataConstraint.table_id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            *_catalog_scope(datasource_id, organization_id),
            MetadataConstraint.organization_id == organization_id,
        )
    )
    sequence = 0
    async for rows in _keyset_pages(session, constraints, MetadataConstraint.id, size):
        yield ProjectionChunk(
            level="constraints",
            sequence=sequence,
            rows=[
                {
                    "platform_id": str(row.id),
                    "table_id": str(row.table_id),
                    "name": row.name,
                    "constraint_type": row.constraint_type,
                    "columns": row.columns,
                    "referenced_table_id": (
                        str(row.referenced_table_id) if row.referenced_table_id else None
                    ),
                    "referenced_columns": row.referenced_columns,
                    "status": row.status,
                    **common,
                }
                for row in rows
            ],
        )
        sequence += 1


__all__ = [
    "DEFAULT_MAX_BUFFERED_EVENTS",
    "DEFAULT_PROJECTION_CHUNK_SIZE",
    "DEFAULT_TENANT_EVENT_BUDGET",
    "MAX_BUFFERED_EVENTS_ENV",
    "PROJECTION_CHUNK_SIZE_ENV",
    "PROJECTION_LEVELS",
    "TENANT_EVENT_BUDGET_ENV",
    "BacklogSnapshot",
    "ProjectionChunk",
    "TenantBudget",
    "TenantFairQueue",
    "event_lag_seconds",
    "iter_projection_chunks",
    "resolve_chunk_size",
]
