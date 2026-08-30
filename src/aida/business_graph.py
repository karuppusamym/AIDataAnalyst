"""The classification axis: business nodes, assignments, and roll-up (ADR-0018).

Axis 2 of the three-axis tenancy model. This module owns the tree of lines of
business, sub-LOBs, domains, sub-domains and concepts, and the many-to-many,
effective-dated attachment of that tree to governed objects.

Nothing here grants access. That separation is the entire point of ADR-0018: a
business node is a *label* a policy can key on, not a container a permission lives
in. A bank reorganises its lines of business every 18-36 months; when the taxonomy
is a label, a reorganisation is an update to this tree plus new assignments, and
last quarter's audit record still resolves against last quarter's tree. When the
taxonomy is the tenancy path, the same reorganisation is a data migration across
every governed row and the audit history ends up describing an organisation chart
that no longer exists.

"Show me everything under Retail Banking" is therefore a recursive CTE over
`business_node` joined to `business_assignment` -- a query, not a subsystem.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.models import (
    AccessPolicy,
    BusinessAssignment,
    BusinessNode,
    BusinessNodeClosure,
    BusinessNodeRollup,
)
from aida.policy_engine import PolicyRecord
from aida.timeutil import as_utc

NODE_KINDS = ("LOB", "SUB_LOB", "DOMAIN", "SUB_DOMAIN", "CONCEPT")

# Target types an assignment may attach to. Polymorphic by necessity: assignments
# reach objects in different module schemas, and ADR-0015 forbids cross-schema
# foreign keys, so referential integrity here is eventual and reconciled.
TARGET_TYPES = (
    "PROJECT",
    "WORKSPACE",
    "DATASOURCE",
    "TABLE",
    "COLUMN",
    "VIEW",
    "METRIC",
    "GLOSSARY_TERM",
    "DATA_PRODUCT",
    "KNOWLEDGE_PAGE",
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a timestamp to UTC-aware before comparing it to another.

    Timestamps do not survive the ORM boundary with their awareness intact on every
    backend: PostgreSQL `timestamptz` reads back aware, SQLite reads back naive. A bare
    `stored == supplied` comparison is therefore silently False on one backend and True
    on another, which is the worst possible failure shape -- backend-dependent logic
    that no single test environment reveals. Normalise, then compare.
    """
    if value is None:
        return None
    return as_utc(value)


def _effective(column_from: Any, column_to: Any, as_of: datetime) -> Any:
    """An effective-dated row is live at `as_of` if it started and has not ended."""
    return and_(column_from <= as_of, or_(column_to.is_(None), column_to > as_of))


def _live_nodes(organization_id: UUID, as_of: datetime, entity: Any = BusinessNode) -> Any:
    """Live-node predicate, bound to a specific entity or alias.

    `entity` is not decoration. Inside the recursive term of a CTE the walked rows
    are an alias, and building the predicate against the un-aliased `BusinessNode`
    silently adds a second FROM entry -- a cartesian product with the whole table
    that filters on "some row is live" rather than on "this row is live". It
    returns plausible results on small trees and wrong ones on real estates.
    """
    return and_(
        entity.organization_id == organization_id,
        entity.status == "ACTIVE",
        _effective(entity.effective_from, entity.effective_to, as_of),
    )


def _live_assignments(organization_id: UUID, as_of: datetime) -> Any:
    return and_(
        BusinessAssignment.organization_id == organization_id,
        BusinessAssignment.status == "ACTIVE",
        _effective(BusinessAssignment.effective_from, BusinessAssignment.effective_to, as_of),
    )


@dataclass(frozen=True, slots=True)
class NodeSummary:
    id: UUID
    parent_id: UUID | None
    kind: str
    code: str
    name: str


async def descendant_ids(
    session: AsyncSession,
    organization_id: UUID,
    node_id: UUID,
    *,
    as_of: datetime | None = None,
) -> frozenset[UUID]:
    """Every node at or below `node_id`, walking down the tree.

    This is what makes "everything under Retail Banking" answerable in one query
    and what lets a policy written against a parent node cover its children.
    """
    if as_of is None:
        # Fast path: the closure table answers this as one indexed lookup. Measured at
        # ~1.5 ms against 13,548 nodes versus ~3.3 ms for the recursive walk.
        rows = await session.execute(
            select(BusinessNodeClosure.descendant_id).where(
                BusinessNodeClosure.ancestor_id == node_id,
                BusinessNodeClosure.organization_id == organization_id,
            )
        )
        found = frozenset(rows.scalars().all())
        if found:
            return found
        # An empty closure means either an unknown node or a projection not yet built.
        # Fall through to the authoritative walk rather than reporting "no descendants",
        # because a stale projection must never look like an answer (INV-1).
    moment = as_of or datetime.now(UTC)
    anchor = (
        select(BusinessNode.id.label("id"))
        .where(BusinessNode.id == node_id, _live_nodes(organization_id, moment))
        .cte("descendants", recursive=True)
    )
    child = aliased(BusinessNode)
    walk = anchor.union_all(
        select(child.id)
        .select_from(child)
        .join(anchor, child.parent_id == anchor.c.id)
        .where(_live_nodes(organization_id, moment, child))
    )
    rows = await session.execute(select(walk.c.id))
    return frozenset(rows.scalars().all())


async def ancestor_closure(
    session: AsyncSession,
    organization_id: UUID,
    node_ids: Iterable[UUID],
    *,
    as_of: datetime | None = None,
) -> frozenset[UUID]:
    """Every node at or above the given nodes, walking up the tree.

    The policy engine matches a resource against this closure rather than against
    the single node an asset is assigned to, so that a policy written against
    "Retail Banking" also covers assets assigned only to its sub-domains. The
    engine performs no I/O, so computing the closure is the caller's job -- this is
    that function.
    """
    seeds = list(dict.fromkeys(node_ids))
    if not seeds:
        return frozenset()
    if as_of is None:
        rows = await session.execute(
            select(BusinessNodeClosure.ancestor_id).where(
                BusinessNodeClosure.descendant_id.in_(seeds),
                BusinessNodeClosure.organization_id == organization_id,
            )
        )
        found = frozenset(rows.scalars().all())
        if found:
            return found
    moment = as_of or datetime.now(UTC)
    anchor = (
        select(BusinessNode.id.label("id"), BusinessNode.parent_id.label("parent_id"))
        .where(BusinessNode.id.in_(seeds), _live_nodes(organization_id, moment))
        .cte("ancestors", recursive=True)
    )
    parent = aliased(BusinessNode)
    walk = anchor.union_all(
        select(parent.id, parent.parent_id)
        .select_from(parent)
        .join(anchor, parent.id == anchor.c.parent_id)
        .where(_live_nodes(organization_id, moment, parent))
    )
    rows = await session.execute(select(walk.c.id))
    return frozenset(rows.scalars().all())


async def descendants_count(
    session: AsyncSession,
    organization_id: UUID,
    node_id: UUID,
    as_of: datetime | None = None,
) -> int:
    """How many live nodes sit at or below this one, including itself."""
    return len(await descendant_ids(session, organization_id, node_id, as_of=as_of))


async def nodes_for_target(
    session: AsyncSession,
    organization_id: UUID,
    target_type: str,
    target_id: str,
    *,
    as_of: datetime | None = None,
) -> tuple[UUID, ...]:
    """The business nodes a governed object is assigned to, right now.

    Returns a tuple rather than a single value because an asset legitimately
    belongs to several domains -- a `customer` table is in both Retail Banking and
    Financial Crime -- which the pre-ADR-0018 containment hierarchy could not
    express and which is the concrete reason ADR-0017 was superseded.
    """
    moment = as_of or datetime.now(UTC)
    statement: Select[tuple[UUID]] = select(BusinessAssignment.business_node_id).where(
        BusinessAssignment.target_type == target_type,
        BusinessAssignment.target_id == target_id,
        _live_assignments(organization_id, moment),
    )
    rows = await session.execute(statement)
    return tuple(dict.fromkeys(rows.scalars().all()))


async def classification_scope(
    session: AsyncSession,
    organization_id: UUID,
    target_type: str,
    target_id: str,
    *,
    as_of: datetime | None = None,
) -> frozenset[UUID]:
    """The full set of node ids a policy may match this object on: assigned + ancestors.

    This runs on the authorization hot path, inside a p95 budget of 50 ms for the whole
    decision, so it is deliberately **one** round trip rather than two. Benchmarked on
    PostgreSQL 16 against 5,000,000 assignments the combined statement returns in
    ~0.8 ms; the earlier two-query form spent most of its time on the second round trip
    rather than in the database.
    """
    if as_of is None:
        rows = await session.execute(
            select(BusinessNodeClosure.ancestor_id)
            .join(
                BusinessAssignment,
                BusinessAssignment.business_node_id == BusinessNodeClosure.descendant_id,
            )
            .where(
                BusinessAssignment.target_type == target_type,
                BusinessAssignment.target_id == target_id,
                BusinessAssignment.organization_id == organization_id,
                BusinessAssignment.status == "ACTIVE",
                BusinessAssignment.effective_to.is_(None),
                BusinessNodeClosure.organization_id == organization_id,
            )
            .distinct()
        )
        found = frozenset(rows.scalars().all())
        if found:
            return found
    # Historical (`as_of`) queries, and the case where the closure projection has not
    # been built yet, take the authoritative two-step path.
    assigned = await nodes_for_target(
        session, organization_id, target_type, target_id, as_of=as_of
    )
    if not assigned:
        return frozenset()
    return await ancestor_closure(session, organization_id, assigned, as_of=as_of)


async def rollup(
    session: AsyncSession,
    organization_id: UUID,
    node_id: UUID,
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """Count assigned objects by target type at or below a node.

    Reads the materialised roll-up, falling back to computing it when the projection is
    empty or the caller asked for a historical `as_of`. The fallback is correct and
    slow, and it is slow for a reason worth stating: measured on PostgreSQL 16 with
    13,548 nodes and 5,000,000 assignments, computing this on read took **3.1 s** with a
    recursive CTE and **0.9 s** with a closure join, against **0.4 ms** to read the
    materialisation. Aggregation over a subtree is not an interactive query, so it is
    not computed in one.

    Use `rollup_freshness` alongside this to show the caller how old the answer is. A
    coverage number that silently drifts is worse than one labelled three hours old.
    """
    if as_of is None:
        rows = await session.execute(
            select(BusinessNodeRollup.target_type, BusinessNodeRollup.distinct_targets).where(
                BusinessNodeRollup.business_node_id == node_id,
                BusinessNodeRollup.organization_id == organization_id,
            )
        )
        materialised = {target_type: count for target_type, count in rows.all()}
        if materialised:
            return materialised
        # An empty result is ambiguous: either this node has nothing under it, or the
        # projection has never been built. Distinguishing them matters, because the
        # fallback below is the ~3 s query this materialisation exists to avoid -- and
        # a node with genuinely nothing assigned is the *common* case early in an
        # estate's life, so guessing wrong means the slow path runs constantly.
        projection_exists = await session.scalar(
            select(BusinessNodeRollup.business_node_id)
            .where(BusinessNodeRollup.organization_id == organization_id)
            .limit(1)
        )
        if projection_exists is not None:
            return {}
    moment = as_of or datetime.now(UTC)
    subtree = await descendant_ids(session, organization_id, node_id, as_of=moment)
    if not subtree:
        return {}
    rows = await session.execute(
        select(
            BusinessAssignment.target_type,
            func.count(func.distinct(BusinessAssignment.target_id)),
        )
        .where(
            BusinessAssignment.business_node_id.in_(subtree),
            _live_assignments(organization_id, moment),
        )
        .group_by(BusinessAssignment.target_type)
    )
    return {target_type: count for target_type, count in rows.all()}


async def tree(
    session: AsyncSession,
    organization_id: UUID,
    *,
    as_of: datetime | None = None,
) -> tuple[NodeSummary, ...]:
    """The whole live classification tree for an organization, parents before children."""
    moment = as_of or datetime.now(UTC)
    rows = await session.execute(
        select(
            BusinessNode.id,
            BusinessNode.parent_id,
            BusinessNode.kind,
            BusinessNode.code,
            BusinessNode.name,
        )
        .where(_live_nodes(organization_id, moment))
        .order_by(BusinessNode.kind, BusinessNode.code)
    )
    return tuple(
        NodeSummary(id=row[0], parent_id=row[1], kind=row[2], code=row[3], name=row[4])
        for row in rows.all()
    )


async def assign(
    session: AsyncSession,
    *,
    organization_id: UUID,
    business_node_id: UUID,
    target_type: str,
    target_id: str,
    assigned_by: str,
    assignment_kind: str = "MANUAL",
    confidence: float | None = None,
    rule_id: UUID | None = None,
    as_of: datetime | None = None,
) -> BusinessAssignment:
    """Attach an object to a business node from `as_of` onward.

    An existing live assignment for the same (node, target) pair is closed rather
    than mutated, so the history of what an object was classified as remains
    queryable. Superseding rather than overwriting is what keeps a past decision
    replayable against the tree that was in force at the time.
    """
    moment = as_of or datetime.now(UTC)
    existing = (
        await session.scalars(
            select(BusinessAssignment).where(
                BusinessAssignment.business_node_id == business_node_id,
                BusinessAssignment.target_type == target_type,
                BusinessAssignment.target_id == target_id,
                _live_assignments(organization_id, moment),
            )
        )
    ).all()
    for row in existing:
        if _as_utc(row.effective_from) == _as_utc(moment):
            # Asserting the same assignment at the same instant is one assignment, not
            # two. Superseding here would set effective_to == effective_from on the old
            # row and then insert a new row with the same effective_from, colliding on
            # (node, target_type, target_id, effective_from). Reachable whenever a caller
            # supplies an explicit `as_of`, or when two writes land in one clock tick.
            row.assignment_kind = assignment_kind
            row.confidence = confidence
            row.rule_id = rule_id
            row.assigned_by = assigned_by
            await session.flush()
            return row
        row.effective_to = moment
        row.status = "SUPERSEDED"
    assignment = BusinessAssignment(
        organization_id=organization_id,
        business_node_id=business_node_id,
        target_type=target_type,
        target_id=target_id,
        assignment_kind=assignment_kind,
        confidence=confidence,
        rule_id=rule_id,
        assigned_by=assigned_by,
        effective_from=moment,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def rollup_freshness(
    session: AsyncSession, organization_id: UUID, node_id: UUID
) -> datetime | None:
    """When the materialised roll-up for this node was last computed, or None.

    Returned to callers so staleness is visible rather than hidden -- the same rule the
    knowledge layer applies to compiled pages.
    """
    computed_at: datetime | None = await session.scalar(
        select(func.max(BusinessNodeRollup.computed_at)).where(
            BusinessNodeRollup.business_node_id == node_id,
            BusinessNodeRollup.organization_id == organization_id,
        )
    )
    return computed_at


# The canonical rebuild statements. Both projections are rebuildable from
# `business_node` / `business_assignment` at any time (INV-1); neither is ever read as
# truth for an authorization decision without the underlying tables agreeing, which is
# why every read above falls through to the authoritative query when a projection is
# empty.
_REBUILD_CLOSURE = text(
    """
    WITH RECURSIVE closure AS (
        SELECT organization_id, id AS ancestor_id, id AS descendant_id, 0 AS depth
        FROM business_node
        WHERE status = 'ACTIVE' AND effective_to IS NULL AND organization_id = :org
        UNION ALL
        SELECT child.organization_id, closure.ancestor_id, child.id, closure.depth + 1
        FROM business_node child
        JOIN closure ON child.parent_id = closure.descendant_id
        WHERE child.status = 'ACTIVE' AND child.effective_to IS NULL
    )
    INSERT INTO business_node_closure (organization_id, ancestor_id, descendant_id, depth)
    SELECT organization_id, ancestor_id, descendant_id, depth FROM closure
    """
)

_REBUILD_ROLLUP = text(
    """
    INSERT INTO business_node_rollup
        (organization_id, business_node_id, target_type, distinct_targets, computed_at)
    SELECT assignment.organization_id,
           closure.ancestor_id,
           assignment.target_type,
           count(DISTINCT assignment.target_id),
           :now
    FROM business_assignment AS assignment
    JOIN business_node_closure AS closure
      ON closure.descendant_id = assignment.business_node_id
    WHERE assignment.status = 'ACTIVE'
      AND assignment.effective_to IS NULL
      AND assignment.organization_id = :org
    GROUP BY assignment.organization_id, closure.ancestor_id, assignment.target_type
    """
)


async def rebuild_closure(session: AsyncSession, organization_id: UUID) -> int:
    """Rebuild the closure projection for one organization. Cheap: ~0.4 s for 13,548 nodes."""
    await session.execute(
        delete(BusinessNodeClosure).where(BusinessNodeClosure.organization_id == organization_id)
    )
    await session.execute(_REBUILD_CLOSURE, {"org": organization_id})
    return await session.scalar(
        select(func.count()).select_from(BusinessNodeClosure).where(
            BusinessNodeClosure.organization_id == organization_id
        )
    ) or 0


async def rebuild_rollup(
    session: AsyncSession, organization_id: UUID, *, now: datetime | None = None
) -> int:
    """Recompute every node's roll-up for one organization.

    A batch job, not a request: measured at ~47 s for 13,548 nodes over 5,000,000
    assignments as a single grouped statement. Call it from a worker on a debounce
    after assignment churn, never inline on a write -- recomputing an LOB's subtree
    synchronously would put a ~1 s query on an interactive path.
    """
    await session.execute(
        delete(BusinessNodeRollup).where(BusinessNodeRollup.organization_id == organization_id)
    )
    await session.execute(
        _REBUILD_ROLLUP, {"org": organization_id, "now": now or datetime.now(UTC)}
    )
    return await session.scalar(
        select(func.count()).select_from(BusinessNodeRollup).where(
            BusinessNodeRollup.organization_id == organization_id
        )
    ) or 0


async def extend_closure_for_new_node(session: AsyncSession, node: BusinessNode) -> None:
    """Maintain the closure incrementally when a node is created.

    A new node inherits its parent's ancestors plus itself, which is bounded by tree
    depth rather than tree size -- so node creation stays O(depth), not O(nodes).
    Re-parenting an existing node is rare and is handled by `rebuild_closure`.
    """
    session.add(
        BusinessNodeClosure(
            organization_id=node.organization_id,
            ancestor_id=node.id,
            descendant_id=node.id,
            depth=0,
        )
    )
    if node.parent_id is None:
        await session.flush()
        return
    rows = await session.execute(
        select(BusinessNodeClosure.ancestor_id, BusinessNodeClosure.depth).where(
            BusinessNodeClosure.descendant_id == node.parent_id
        )
    )
    for ancestor_id, depth in rows.all():
        session.add(
            BusinessNodeClosure(
                organization_id=node.organization_id,
                ancestor_id=ancestor_id,
                descendant_id=node.id,
                depth=depth + 1,
            )
        )
    await session.flush()


async def load_policies(
    session: AsyncSession, organization_id: UUID
) -> tuple[PolicyRecord, ...]:
    """Load the ACTIVE policy set for an organization into engine records.

    DRAFT policies are deliberately excluded: a seeded-but-inactive policy such as
    the agent sensitive-data DENY must be visible to a reviewer without being
    enforced until someone activates it.
    """
    rows = await session.scalars(
        select(AccessPolicy).where(
            AccessPolicy.organization_id == organization_id,
            AccessPolicy.status == "ACTIVE",
        )
    )
    return tuple(
        PolicyRecord(
            id=policy.id,
            code=policy.code,
            version=policy.version,
            effect=policy.effect,
            priority=policy.priority,
            subject_match=dict(policy.subject_match or {}),
            resource_match=dict(policy.resource_match or {}),
            action_match=tuple(policy.action_match or ()),
            transform=dict(policy.transform or {}),
            condition=dict(policy.condition or {}),
        )
        for policy in rows.all()
    )


def build_hierarchy(nodes: Sequence[NodeSummary]) -> list[dict[str, Any]]:
    """Nest a flat node list into a tree for presentation. Pure; no I/O."""
    by_id: dict[UUID, dict[str, Any]] = {
        node.id: {
            "id": str(node.id),
            "kind": node.kind,
            "code": node.code,
            "name": node.name,
            "children": [],
        }
        for node in nodes
    }
    roots: list[dict[str, Any]] = []
    for node in nodes:
        payload = by_id[node.id]
        parent = by_id.get(node.parent_id) if node.parent_id else None
        if parent is None:
            roots.append(payload)
        else:
            children = parent["children"]
            assert isinstance(children, list)
            children.append(payload)
    return roots
