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

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.models import AccessPolicy, BusinessAssignment, BusinessNode
from aida.policy_engine import PolicyRecord

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
    """The full set of node ids a policy may match this object on: assigned + ancestors."""
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

    Coverage, documentation completeness and lineage density all roll up the same
    way; this is the shape every one of those reports is built on.
    """
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
    existing = await session.scalars(
        select(BusinessAssignment).where(
            BusinessAssignment.business_node_id == business_node_id,
            BusinessAssignment.target_type == target_type,
            BusinessAssignment.target_id == target_id,
            _live_assignments(organization_id, moment),
        )
    )
    for row in existing.all():
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
