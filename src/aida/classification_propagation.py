"""AT-11: classification propagation along data lineage.

A column's *asserted* classification (``MetadataColumn.classification`` -- a
steward decision or an authoritative external feed) is the value a policy
enforces on. This module computes a column's *derived* classification -- one
propagated to it from a more-sensitive upstream column along data lineage --
and stores it strictly separately (``ColumnDerivedClassification``), because for
us a classification is an ABAC enforcement input, not a display label: a value
the graph merely *inferred* must never silently become a value a policy
enforces on. Promotion from derived to asserted goes through the shared
maker-checker review queue, never automatically.

Three rules define correct propagation, all enforced here:

1. **Raise-only.** Propagation may only move a column to a *more* restrictive
   classification, never a less restrictive one. The total order is
   ``CLASSIFICATION_SENSITIVITY_ORDER`` below -- an explicit, documented lattice,
   not an implicit alphabetical or insertion order.

2. **Authoritative edge kinds only.** Classification propagates only along
   lineage edges whose provenance we trust deterministically -- declared
   relationships, parsed view/procedure DDL, parsed executed queries, and
   OpenLineage runs. It NEVER propagates along ``INFLUENCES`` edges (inferred
   relationship candidates): a guessed relationship must not raise an
   enforcement input. See ``PROPAGATING_EDGE_KINDS`` and the edge-kind mapping.

3. **Evidence is first-class.** Every derived value records the ordered edge
   chain it travelled (origin -> column) and the fingerprint of the lineage
   graph it was computed over, so it is auditable and reproducible.

Edge-kind vocabulary mapping
----------------------------
The tracker's exit condition names the propagating edge kinds ``DECLARED`` /
``VIEW_DDL`` / ``EXECUTED_QUERY`` / ``OPENLINEAGE`` and the non-propagating one
``INFLUENCES``. The code's own unified lineage graph
(``aida.unified_lineage_api``) records edge *provenance* under different string
literals (its ``UnifiedLink.edge_source``). ``EDGE_SOURCE_TO_PROPAGATION_KIND``
maps one to the other so callers building edges from the real graph get the
right propagation behaviour without this module hard-coding the graph's
literals. The canonical names are what this engine reasons about.

Direction convention
---------------------
This engine's ``PropagationEdge`` runs ``upstream_id -> downstream_id`` in the
direction data *flows* -- classification follows the data. Note this is the
inverse of ``aida.unified_lineage``'s ``UnifiedLink`` convention (whose
``source_id`` is the *dependent*/downstream node); a caller translating a
``UnifiedLink`` into a ``PropagationEdge`` swaps the two ids. Keeping this
engine's own convention data-flow-forward keeps the propagation reasoning
readable and independent of the graph module's storage convention.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.models import (
    ClassificationEvidence,
    ColumnDerivedClassification,
    GovernanceReview,
    MetadataColumn,
)
from aida.security_types import SecurityContext

# --------------------------------------------------------------------------- #
# 1. The sensitivity lattice (raise-only ordering)
# --------------------------------------------------------------------------- #

# An explicit total order over the classification vocabulary. Higher rank = more
# restrictive. Propagation may only raise a column toward a higher rank. This is
# an authorial ordering for a bank's data-classification scheme, documented here
# so "raise-only" has a single unambiguous meaning; it is deliberately a total
# order (PII < PCI < PHI < SECRET) rather than a partial one so the "more
# restrictive of two" question always has a definite answer.
CLASSIFICATION_SENSITIVITY_ORDER: dict[str, int] = {
    "UNCLASSIFIED": 0,
    "PUBLIC": 0,
    "INTERNAL": 10,
    "CONFIDENTIAL": 20,
    "PII": 30,
    "PCI": 40,
    "PHI": 50,
    "SECRET": 60,
}

# A classification not in the lattice contributes rank 0 (treated as
# UNCLASSIFIED) so an unknown label can never *raise* another column's
# classification. It errs toward under-propagation, never toward silently
# enforcing on a value we cannot rank.
_UNKNOWN_RANK = 0


def sensitivity_rank(classification: str) -> int:
    """Rank of a classification in the raise-only lattice (unknown -> 0)."""
    return CLASSIFICATION_SENSITIVITY_ORDER.get(classification, _UNKNOWN_RANK)


def is_more_restrictive(candidate: str, current: str) -> bool:
    """True iff ``candidate`` is strictly more restrictive than ``current``."""
    return sensitivity_rank(candidate) > sensitivity_rank(current)


# --------------------------------------------------------------------------- #
# 2. Edge-kind restriction
# --------------------------------------------------------------------------- #

# Canonical propagation edge kinds, exactly as the exit condition names them.
DECLARED = "DECLARED"
VIEW_DDL = "VIEW_DDL"
EXECUTED_QUERY = "EXECUTED_QUERY"
OPENLINEAGE = "OPENLINEAGE"
INFLUENCES = "INFLUENCES"

# Classification propagates ONLY along these kinds.
PROPAGATING_EDGE_KINDS: frozenset[str] = frozenset(
    {DECLARED, VIEW_DDL, EXECUTED_QUERY, OPENLINEAGE}
)

# Kinds classification must NEVER propagate along. INFLUENCES (an inferred
# relationship candidate) is the one the exit condition calls out by name.
NON_PROPAGATING_EDGE_KINDS: frozenset[str] = frozenset({INFLUENCES})

# Map the unified lineage graph's own edge_source literals
# (``aida.unified_lineage_api``) to the canonical propagation kinds above.
#   FOREIGN_KEY          -> DECLARED       (a declared foreign-key relationship)
#   DBT_DEPENDENCY       -> DECLARED       (a manifest-declared transformation dep)
#   VIEW_DEFINITION      -> VIEW_DDL       (parsed CREATE VIEW definition)
#   PROCEDURE_DEFINITION -> VIEW_DDL       (parsed stored-procedure body -- same
#                                           deterministic DDL-parse provenance)
#   OPENLINEAGE_ETL      -> OPENLINEAGE    (an OpenLineage run event)
#   SUGGESTED_RELATIONSHIP -> INFLUENCES   (an inferred candidate -- NON-propagating)
# ``EXECUTED_QUERY`` has no unified-graph edge_source today (query lineage lives
# in ``query_gateway.extract_column_lineage`` as QueryExecution.column_lineage,
# not yet folded into the unified graph); it is a first-class canonical kind here
# so a caller building edges from executed-query lineage propagates correctly.
EDGE_SOURCE_TO_PROPAGATION_KIND: dict[str, str] = {
    "FOREIGN_KEY": DECLARED,
    "DBT_DEPENDENCY": DECLARED,
    "VIEW_DEFINITION": VIEW_DDL,
    "PROCEDURE_DEFINITION": VIEW_DDL,
    "OPENLINEAGE_ETL": OPENLINEAGE,
    "SUGGESTED_RELATIONSHIP": INFLUENCES,
}


def propagation_kind_for_edge_source(edge_source: str) -> str:
    """Canonical propagation kind for a unified-graph ``edge_source`` literal.

    An unmapped ``edge_source`` is treated as ``INFLUENCES`` (non-propagating):
    an edge whose provenance we do not explicitly trust must not raise an
    enforcement input.
    """
    return EDGE_SOURCE_TO_PROPAGATION_KIND.get(edge_source, INFLUENCES)


# --------------------------------------------------------------------------- #
# 3. The pure propagation engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PropagationEdge:
    """One directed lineage edge in the data-flow direction.

    Data (and therefore classification) flows ``upstream_id -> downstream_id``.
    ``kind`` is a canonical propagation kind; ``edge_ref`` is an opaque,
    value-free identifier retained in the evidence chain.
    """

    upstream_id: str
    downstream_id: str
    kind: str
    edge_ref: str = ""

    def descriptor(self) -> dict[str, Any]:
        """Value-free evidence descriptor for this edge."""
        return {
            "source_id": self.upstream_id,
            "target_id": self.downstream_id,
            "kind": self.kind,
            "edge_ref": self.edge_ref,
        }


@dataclass(frozen=True, slots=True)
class DerivedAssignment:
    """A raise-only classification derived for one node, with its evidence."""

    node_id: str
    classification: str
    origin_node_id: str
    origin_classification: str
    edge_chain: tuple[dict[str, Any], ...]
    path_nodes: tuple[str, ...]


def graph_fingerprint(edges: list[PropagationEdge]) -> str:
    """Stable SHA-256 fingerprint of the lineage graph propagation ran over.

    Deterministic in edge order and independent of it (edges are sorted), so the
    same graph always yields the same version string -- the value stored as a
    derived classification's ``graph_version`` evidence.
    """
    canonical = sorted(
        (edge.upstream_id, edge.downstream_id, edge.kind, edge.edge_ref) for edge in edges
    )
    digest = hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def propagate(
    *,
    asserted: dict[str, str],
    edges: list[PropagationEdge],
) -> list[DerivedAssignment]:
    """Compute raise-only derived classifications over a lineage graph.

    ``asserted`` maps node id -> its asserted classification (the enforced
    value). Only edges whose ``kind`` is in ``PROPAGATING_EDGE_KINDS`` are
    traversed; ``INFLUENCES`` (and any other non-propagating kind) are ignored
    entirely, so a node reachable only through them derives nothing.

    For every node, the derived classification is the most restrictive asserted
    classification of any upstream origin reachable along propagating edges --
    but only returned when it is strictly more restrictive than the node's own
    asserted classification (raise-only). Ties on rank are broken deterministically
    (lowest origin id, then shortest path) so evidence is reproducible.

    Returns one ``DerivedAssignment`` per node that was actually raised, sorted
    by node id.
    """
    # Adjacency restricted to propagating edges, in the data-flow direction.
    downstream: dict[str, list[PropagationEdge]] = {}
    for edge in edges:
        if edge.kind not in PROPAGATING_EDGE_KINDS:
            continue
        downstream.setdefault(edge.upstream_id, []).append(edge)

    # Best candidate per reached node: (rank, origin_id, path_len, assignment-ish).
    best: dict[str, tuple[int, str, int, DerivedAssignment]] = {}

    for origin in sorted(asserted):
        origin_class = asserted[origin]
        origin_rank = sensitivity_rank(origin_class)
        if origin_rank <= _UNKNOWN_RANK:
            # An UNCLASSIFIED/unknown origin can never raise anything downstream.
            continue
        # BFS downstream from this origin, tracking the edge path to each node.
        # visited guards against cycles; the first time BFS reaches a node is a
        # shortest path in edges, which is the evidence chain we keep.
        visited: set[str] = {origin}
        queue: list[tuple[str, tuple[PropagationEdge, ...]]] = [(origin, ())]
        while queue:
            node, path = queue.pop(0)
            if node != origin:
                candidate = (origin_rank, origin, len(path))
                existing = best.get(node)
                # Prefer higher rank; on equal rank prefer the lower origin id,
                # then the shorter path -- a fully deterministic winner.
                if existing is None or _candidate_wins(candidate, existing):
                    assignment = DerivedAssignment(
                        node_id=node,
                        classification=origin_class,
                        origin_node_id=origin,
                        origin_classification=origin_class,
                        edge_chain=tuple(e.descriptor() for e in path),
                        path_nodes=(origin, *[e.downstream_id for e in path]),
                    )
                    best[node] = (origin_rank, origin, len(path), assignment)
            for edge in downstream.get(node, ()):
                nxt = edge.downstream_id
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append((nxt, (*path, edge)))

    results: list[DerivedAssignment] = []
    for node in sorted(best):
        _, _, _, assignment = best[node]
        own_class = asserted.get(node, "UNCLASSIFIED")
        # Raise-only: only surface a derived value strictly above the node's own
        # asserted classification.
        if is_more_restrictive(assignment.classification, own_class):
            results.append(assignment)
    return results


def _candidate_wins(candidate: tuple[int, str, int], existing: tuple[int, str, int, Any]) -> bool:
    """True iff ``candidate`` (rank, origin_id, path_len) beats the stored one.

    Higher rank wins; ties break to the lexicographically lowest origin id, then
    to the shortest path. Deterministic so the recorded edge chain is stable.
    """
    cand_rank, cand_origin, cand_len = candidate
    ex_rank, ex_origin, ex_len, _ = existing
    if cand_rank != ex_rank:
        return cand_rank > ex_rank
    if cand_origin != ex_origin:
        return cand_origin < ex_origin
    return cand_len < ex_len


# --------------------------------------------------------------------------- #
# 4. Persistence: run propagation and store derived rows (never asserted)
# --------------------------------------------------------------------------- #

CLASSIFICATION_SOURCE_DERIVED_PROMOTED = "DERIVED_PROMOTED"

PROMOTION_OBJECT_TYPE = "COLUMN_CLASSIFICATION_PROMOTION"


async def store_derived_classifications(
    session: AsyncSession,
    *,
    organization_id: UUID,
    asserted: dict[str, str],
    edges: list[PropagationEdge],
    created_by: str,
) -> list[ColumnDerivedClassification]:
    """Run propagation and persist each raised value as a current derived row.

    Node ids are ``str(MetadataColumn.id)``. For every node that is raised, any
    prior current derived row for that column is superseded (``is_current`` ->
    False) and a new current row is inserted carrying the edge chain and graph
    fingerprint as evidence. This writes ONLY derived rows; it never touches a
    column's asserted ``classification`` -- that only changes through the review
    queue (see ``apply_classification_promotion``). Returns the new rows.
    """
    version = graph_fingerprint(edges)
    assignments = propagate(asserted=asserted, edges=edges)
    stored: list[ColumnDerivedClassification] = []
    for assignment in assignments:
        column_id = UUID(assignment.node_id)
        origin_id = _maybe_uuid(assignment.origin_node_id)
        await session.execute(
            update(ColumnDerivedClassification)
            .where(
                ColumnDerivedClassification.column_id == column_id,
                ColumnDerivedClassification.is_current.is_(True),
            )
            .values(is_current=False)
        )
        row = ColumnDerivedClassification(
            organization_id=organization_id,
            column_id=column_id,
            classification=assignment.classification,
            origin_column_id=origin_id,
            origin_classification=assignment.origin_classification,
            edge_chain=list(assignment.edge_chain),
            graph_version=version,
            status="DERIVED",
            is_current=True,
            created_by=created_by,
        )
        session.add(row)
        stored.append(row)
    await session.flush()
    return stored


def _maybe_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# 5. Review gate: derived -> asserted only via the maker-checker queue
# --------------------------------------------------------------------------- #


async def submit_classification_promotion(
    session: AsyncSession,
    context: SecurityContext,
    *,
    derived: ColumnDerivedClassification,
    correlation_id: str,
) -> GovernanceReview:
    """Open a maker-checker review to promote one derived value to asserted.

    This is the ONLY entry point that can start a derived -> asserted
    transition; it does not itself change the column's asserted classification.
    It creates a ``COLUMN_CLASSIFICATION_PROMOTION`` ``GovernanceReview`` and
    marks the derived row ``PROMOTION_PENDING``. The actual promotion happens
    only when an independent reviewer approves that review
    (``apply_classification_promotion``, dispatched from
    ``semantic_api.decide_governance_review``, which enforces maker != checker).
    """
    if derived.status != "DERIVED":
        raise ValueError(
            f"only a DERIVED classification can be submitted for promotion, not {derived.status!r}"
        )
    review = GovernanceReview(
        organization_id=derived.organization_id,
        object_type=PROMOTION_OBJECT_TYPE,
        object_id=str(derived.id),
        requested_action="PROMOTE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    derived.status = "PROMOTION_PENDING"
    derived.review_id = review.id
    record_audit(
        session,
        context,
        action="classification.promotion.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "derived_classification_id": str(derived.id),
            "column_id": str(derived.column_id),
            "classification": derived.classification,
        },
    )
    record_outbox(
        session,
        organization_id=derived.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": str(derived.id),
            "column_id": str(derived.column_id),
            "classification": derived.classification,
        },
    )
    await session.flush()
    return review


async def apply_classification_promotion(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    context: SecurityContext,
    now: datetime,
) -> tuple[str, str, str, dict[str, Any]]:
    """Apply an approved/rejected classification-promotion decision.

    Dispatched from ``semantic_api._apply_governance_review_decision`` -- which
    has already enforced the maker-checker separation, PENDING-only, and
    organization-boundary preconditions -- so this is the one place a derived
    classification becomes asserted, and only under an independent APPROVE.

    On APPROVE the column's asserted ``classification`` is set to the derived
    value (raise-only guard re-checked here defensively), a current
    ``ClassificationEvidence`` row is appended recording the derived provenance,
    and the derived row is marked ``PROMOTED``. On REJECT nothing about the
    column changes and the derived row is marked ``PROMOTION_REJECTED``. Returns
    ``(event_type, aggregate_type, aggregate_id, payload)`` for the caller's
    outbox record.
    """
    derived = await session.get(ColumnDerivedClassification, UUID(review.object_id))
    if derived is None or derived.organization_id != review.organization_id:
        raise _conflict("review target is unavailable")
    if derived.status != "PROMOTION_PENDING":
        raise _conflict("derived classification is no longer pending promotion")
    column = await session.get(MetadataColumn, derived.column_id)
    if column is None or column.organization_id != review.organization_id:
        raise _conflict("review target is unavailable")

    if decision == "APPROVE":
        if not is_more_restrictive(derived.classification, column.classification):
            # The asserted value moved at/above the derived value since the row
            # was computed; promoting would not raise anything. Refuse rather
            # than lower or no-op silently.
            raise _conflict(
                "derived classification no longer raises the column's asserted classification"
            )
        await session.execute(
            update(ClassificationEvidence)
            .where(
                ClassificationEvidence.column_id == column.id,
                ClassificationEvidence.is_current.is_(True),
            )
            .values(is_current=False)
        )
        session.add(
            ClassificationEvidence(
                organization_id=column.organization_id,
                column_id=column.id,
                classification=derived.classification,
                source_type=CLASSIFICATION_SOURCE_DERIVED_PROMOTED,
                rule_id=f"CLASSIFICATION_PROPAGATION:{derived.id}",
                confidence=None,
                matched_signal={
                    "value_scope": "METADATA_ONLY",
                    "actual_values_inspected": False,
                    "origin_column_id": (
                        str(derived.origin_column_id) if derived.origin_column_id else None
                    ),
                    "origin_classification": derived.origin_classification,
                    "edge_chain": derived.edge_chain,
                    "graph_version": derived.graph_version,
                    "review_id": str(review.id),
                },
                is_current=True,
                created_by=context.principal_id,
            )
        )
        column.classification = derived.classification
        column.classification_source = CLASSIFICATION_SOURCE_DERIVED_PROMOTED
        derived.status = "PROMOTED"
        derived.promoted_by = context.principal_id
        derived.promoted_at = now
        event_type = "classification.derived.promoted.v1"
    else:
        derived.status = "PROMOTION_REJECTED"
        event_type = "classification.derived.promotion_rejected.v1"

    payload = {
        "derived_classification_id": str(derived.id),
        "column_id": str(column.id),
        "classification": derived.classification,
        "review_id": str(review.id),
    }
    return event_type, "column_derived_classification", str(derived.id), payload


def _conflict(detail: str) -> Exception:
    """A 409-mapped error, matching the governance dispatcher's own idiom.

    Imported lazily so this module carries no hard dependency on FastAPI beyond
    what the shared review path already provides.
    """
    from fastapi import HTTPException

    return HTTPException(status_code=409, detail=detail)


async def get_current_derived_classification(
    session: AsyncSession,
    *,
    organization_id: UUID,
    column_id: UUID,
) -> ColumnDerivedClassification | None:
    """The current derived classification for a column, or None.

    The queryable read side of the evidence: the returned row carries the edge
    chain and graph version. Organization-scoped so it can never read across a
    tenant boundary.
    """
    row: ColumnDerivedClassification | None = await session.scalar(
        select(ColumnDerivedClassification).where(
            ColumnDerivedClassification.organization_id == organization_id,
            ColumnDerivedClassification.column_id == column_id,
            ColumnDerivedClassification.is_current.is_(True),
        )
    )
    return row
