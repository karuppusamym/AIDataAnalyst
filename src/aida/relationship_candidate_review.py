"""N4: lineage proposal / review / negative-knowledge workflow for
``RelationshipCandidate``.

Composes four pieces already built elsewhere on this branch rather than
reimplementing any of them:

* **SM-7's structured diff** (``aida.semantic_diff.diff_semantic_object``) --
  reused verbatim, not reimplemented. A ``RelationshipCandidate`` has no
  "before" state: it is a proposal, never a version of something already
  published. That is exactly the case ``diff_semantic_object`` already
  handles when ``before`` is ``None``/``{}`` -- "a semantic object's very
  first submission, which has no published predecessor to diff against"
  (its own docstring) -- so every field of the curated snapshot below comes
  back as one ``added`` entry. No new diff representation was invented; the
  only new work is a purpose-fit snapshot function, mirroring
  ``semantic_api.py``'s own ``_semantic_model_version_snapshot`` /
  ``_glossary_term_version_snapshot`` curation, rather than dumping the raw
  ORM row's identifiers/timestamps/status bookkeeping through the diff.
* **EA.14's bounded lineage traversal**
  (``unified_lineage_api.build_unified_lineage_impact_payload``) -- reused
  verbatim, exactly the way TL-7's ``tool_impact.py`` reuses it, to produce
  a real, bounded impact score per candidate (how much of the
  already-approved graph sits behind the two endpoints this edge would
  connect) instead of an invented priority number.
* **EE.3/N16's negative-knowledge mechanism**
  (``negative_knowledge.record_negative`` / the same predicate-hash scheme
  ``check_re_proposal`` reads) -- reused verbatim. A rejected candidate's
  ``(source_column_id, target_column_id)`` edge becomes a
  ``RELATIONSHIP_REJECTED`` ``NegativeAssertionRecord``;
  ``intelligence_api.py``'s discovery endpoints consult the same predicate
  hash before creating a new candidate, so a rejected edge is not silently
  re-proposed.
* **RL-6's bulk-decision mechanism**
  (``intelligence_api.py``'s ``bulk_decide_relationship_candidates``)
  already exists for ``RelationshipCandidate`` specifically -- this module
  does not rebuild bulk decisions, it wires the reject path of both the
  single- (``decide_relationship_candidate``) and bulk-decision endpoints
  through ``record_relationship_candidate_rejection`` below so a rejection
  reaches negative knowledge regardless of which surface rejected it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    NegativeAssertionRecord,
    RelationshipCandidate,
)
from aida.negative_knowledge import (
    AssertionType,
    NegativeAssertion,
    compute_predicate_hash,
    record_negative,
)
from aida.schemas import ApiModel, RelationshipCandidateRead
from aida.semantic_diff import ChangeKind, SemanticDiff, diff_semantic_object
from aida.unified_lineage_api import LineageNodeNotFoundError, build_unified_lineage_impact_payload

#: Mirrors TL-7's own defaults (``tool_impact.DEFAULT_IMPACT_DEPTH`` /
#: ``DEFAULT_IMPACT_NODE_LIMIT``) -- the same bounded traversal, the same
#: reasoning: cheap enough to run per candidate in a review queue, still a
#: real multi-hop bound rather than a direct-reference count.
DEFAULT_IMPACT_DEPTH = 3
DEFAULT_IMPACT_NODE_LIMIT = 100

#: How many PENDING candidates get impact-scored (and are therefore eligible
#: to appear anywhere in the impact-ordered queue) per request -- the same
#: bounded-scan convention already used throughout this platform
#: (``RELATIONSHIP_CANDIDATE_BULK_DECISION_MAX_ITEMS``,
#: ``tool_impact.DEFAULT_TOOL_SCAN_LIMIT``). A datasource with more PENDING
#: candidates than this reports ``truncated=True`` rather than scanning
#: unboundedly.
REVIEW_QUEUE_SCAN_LIMIT = 200

RELATIONSHIP_REJECTED_ASSERTION_TYPE: AssertionType = "RELATIONSHIP_REJECTED"


# ---------------------------------------------------------------------------
# Diff: "nothing -> this edge"
# ---------------------------------------------------------------------------


def relationship_candidate_diff_snapshot(
    candidate: RelationshipCandidate,
    *,
    source_qualified_name: str,
    source_column_name: str,
    target_qualified_name: str,
    target_column_name: str,
) -> dict[str, Any]:
    """Curate the substantive facts about a proposed edge into a flat
    snapshot for ``diff_semantic_object``.

    Deliberately excludes ``id``/``organization_id``/``status``/
    ``created_at``/``updated_at``/``reviewed_*`` -- bookkeeping the reviewer
    is not being asked to review -- so the resulting diff reports one
    ``added`` entry per fact that actually describes the edge, not one per
    ORM column.
    """
    return {
        "source_table": source_qualified_name,
        "source_column": source_column_name,
        "target_table": target_qualified_name,
        "target_column": target_column_name,
        "detection_rule": candidate.detection_rule,
        "confidence": candidate.confidence,
        # AT-15's named, budgeted per-signal breakdown -- already stored on
        # the candidate at discovery time (`evidence["signals"]`), never
        # recomputed here.
        "confidence_signals": candidate.evidence.get("signals", []),
    }


def diff_relationship_candidate(snapshot: dict[str, Any]) -> SemanticDiff:
    """The candidate's diff: ``nothing -> this edge``.

    Reuses SM-7's ``diff_semantic_object`` with ``before=None`` rather than
    a bespoke diff engine -- see the module docstring for why that is the
    right call, not a shortcut.
    """
    return diff_semantic_object(None, snapshot)


# ---------------------------------------------------------------------------
# Negative knowledge: reject -> "known not true" -> re-proposal suppressed
# ---------------------------------------------------------------------------


def relationship_candidate_negative_subject_id(
    source_column_id: UUID, target_column_id: UUID
) -> str:
    """``kind:id:id`` -- following ``negative_knowledge._scope_predicate``'s
    own documented colon-delimited convention (its docstring's own example,
    ``"col:<table_id>:<column>"``) so a scope lookup by either column id
    still matches this subject.
    """
    return f"relationship:{source_column_id}:{target_column_id}"


def relationship_candidate_negative_predicate(
    source_column_id: UUID, target_column_id: UUID
) -> dict[str, Any]:
    """The predicate hashed for re-proposal suppression: the edge's
    identity (which two columns) -- never the detection rule or confidence
    that happened to find it, so the same edge proposed again by a
    different or improved detection rule is still recognized as the same
    rejected edge.
    """
    return {
        "source_column_id": str(source_column_id),
        "target_column_id": str(target_column_id),
    }


async def record_relationship_candidate_rejection(
    session: AsyncSession,
    candidate: RelationshipCandidate,
    *,
    rejected_by: str,
    reason: str | None,
) -> NegativeAssertionRecord:
    """The real EE.3/N16 negative-knowledge write, on reject.

    Called from both ``decide_relationship_candidate`` (single-item) and
    ``bulk_decide_relationship_candidates`` (RL-6's bulk path) so a
    rejected candidate becomes queryable "known not true" through the one
    real mechanism (``negative_knowledge.record_negative``), regardless of
    which decision surface rejected it.
    """
    assertion = NegativeAssertion(
        id=None,
        assertion_type=RELATIONSHIP_REJECTED_ASSERTION_TYPE,
        subject_id=relationship_candidate_negative_subject_id(
            candidate.source_column_id, candidate.target_column_id
        ),
        predicate=relationship_candidate_negative_predicate(
            candidate.source_column_id, candidate.target_column_id
        ),
        evidence={
            "candidate_id": str(candidate.id),
            "detection_rule": candidate.detection_rule,
            "confidence": candidate.confidence,
            "source_table_id": str(candidate.source_table_id),
            "target_table_id": str(candidate.target_table_id),
            "review_reason": reason,
        },
        rejected_by=rejected_by,
        rejected_at=datetime.now(UTC),
    )
    return await record_negative(session, candidate.organization_id, assertion)


async def load_suppressed_relationship_predicate_hashes(
    session: AsyncSession, organization_id: UUID, *, limit: int = 20_000
) -> set[str]:
    """Bulk-prefetch every actively-suppressed relationship predicate hash
    for one organization.

    Mirrors ``discover_relationship_candidates``'s own
    ``existing_candidate_pairs`` bulk-prefetch (one query up front, then an
    in-memory membership check per candidate in the scan loop) rather than
    one ``check_re_proposal`` round trip per candidate scanned.
    """
    rows = await session.scalars(
        select(NegativeAssertionRecord.material_change_hash)
        .where(
            NegativeAssertionRecord.organization_id == organization_id,
            NegativeAssertionRecord.assertion_type == RELATIONSHIP_REJECTED_ASSERTION_TYPE,
            NegativeAssertionRecord.suppression_active.is_(True),
        )
        .limit(limit)
    )
    return {row_hash for row_hash in rows.all() if row_hash is not None}


def is_relationship_pair_suppressed(
    suppressed_hashes: set[str], source_column_id: UUID, target_column_id: UUID
) -> bool:
    return (
        compute_predicate_hash(
            relationship_candidate_negative_predicate(source_column_id, target_column_id)
        )
        in suppressed_hashes
    )


# ---------------------------------------------------------------------------
# Impact: real, bounded blast radius (EA.14), not an invented priority number
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationshipCandidateImpact:
    """Real, bounded blast-radius score for a PENDING relationship
    candidate -- how much of the already-approved unified lineage graph
    sits behind the two endpoints this edge would connect.

    Deciding this candidate (approve OR reject) matters more the higher
    this is: the same score orders the review queue for both outcomes,
    since it measures the stakes of the decision, not which way it should
    go.
    """

    candidate_id: UUID
    source_table_impact: int
    target_table_impact: int
    depth: int
    node_limit: int
    truncated: bool

    @property
    def impact_score(self) -> int:
        return self.source_table_impact + self.target_table_impact


async def compute_relationship_candidate_impact(
    session: AsyncSession,
    candidate: RelationshipCandidate,
    *,
    source_datasource: DataSource,
    target_datasource: DataSource,
    depth: int = DEFAULT_IMPACT_DEPTH,
    node_limit: int = DEFAULT_IMPACT_NODE_LIMIT,
    settings: Settings | None = None,
) -> RelationshipCandidateImpact:
    """EA.14's real traversal, called at each endpoint this candidate would
    connect -- reused verbatim, the same function backing
    ``GET .../unified-lineage/impact/{node_id}`` and TL-7's deprecation
    preview, never a re-derived or invented priority number.

    A ``PENDING`` candidate's edge is not yet in the graph
    (``build_unified_lineage_impact_payload`` only ever traverses
    ``APPROVED`` edges), so this measures what is *already* reachable at
    each endpoint -- the blast radius the new edge would join together, not
    a radius that already includes it.
    """
    truncated = False

    async def _impact_at(datasource: DataSource, table_id: UUID) -> int:
        nonlocal truncated
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
            # Not (yet) a node in that datasource's unified graph -- an
            # honest zero, not a reason to fail the whole queue (same
            # fail-open posture TL-7's `compute_deprecation_impact` uses).
            return 0
        if impact.upstream_truncated or impact.downstream_truncated:
            truncated = True
        return len(impact.upstream) + len(impact.downstream)

    source_impact = await _impact_at(source_datasource, candidate.source_table_id)
    target_impact = await _impact_at(target_datasource, candidate.target_table_id)
    return RelationshipCandidateImpact(
        candidate_id=candidate.id,
        source_table_impact=source_impact,
        target_table_impact=target_impact,
        depth=depth,
        node_limit=node_limit,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# The impact-ordered, diff-based review queue
# ---------------------------------------------------------------------------


class RelationshipCandidateDiffEntryRead(ApiModel):
    """One field-level entry of a candidate's ``nothing -> this edge`` diff.
    Mirrors ``semantic_api.SemanticFieldDeltaRead``'s own wire shape (kept
    independent of ``aida.semantic_diff``'s internals, same reasoning)."""

    field: str
    change: ChangeKind
    after: Any = None


class RelationshipCandidateImpactRead(ApiModel):
    impact_score: int
    source_table_impact: int
    target_table_impact: int
    depth: int
    node_limit: int
    truncated: bool


class RelationshipCandidateReviewItemRead(ApiModel):
    candidate: RelationshipCandidateRead
    diff: list[RelationshipCandidateDiffEntryRead]
    impact: RelationshipCandidateImpactRead
    can_review: bool = True


class RelationshipCandidateReviewQueueRead(ApiModel):
    datasource_id: UUID
    items: list[RelationshipCandidateReviewItemRead]
    limit: int
    offset: int
    scanned_count: int
    total_pending_count: int
    truncated: bool


async def compose_relationship_candidate_review_queue(
    session: AsyncSession,
    datasource: DataSource,
    *,
    limit: int,
    offset: int,
    depth: int = DEFAULT_IMPACT_DEPTH,
    node_limit: int = DEFAULT_IMPACT_NODE_LIMIT,
    scan_limit: int = REVIEW_QUEUE_SCAN_LIMIT,
    settings: Settings | None = None,
    reviewer_principal_id: str | None = None,
) -> RelationshipCandidateReviewQueueRead:
    """N4: the impact-ordered, diff-based relationship-candidate review
    queue for one datasource.

    Up to ``scan_limit`` PENDING candidates are loaded and impact-scored
    (EA.14's real traversal, via ``compute_relationship_candidate_impact``);
    the queue is then sorted by that real impact score, descending
    (confidence, then id, break ties -- never an arbitrary/insertion-order
    tiebreak), and ``limit``/``offset`` page the *scored* set. A datasource
    with more PENDING candidates than ``scan_limit`` reports
    ``truncated=True`` -- the same bounded-scan-and-report-truncation
    posture every other multi-row operation in this module already takes.
    """
    total_pending = (
        await session.scalar(
            select(func.count())
            .select_from(RelationshipCandidate)
            .where(
                RelationshipCandidate.datasource_id == datasource.id,
                RelationshipCandidate.status == "PENDING",
            )
        )
        or 0
    )

    candidates = list(
        (
            await session.scalars(
                select(RelationshipCandidate)
                .where(
                    RelationshipCandidate.datasource_id == datasource.id,
                    RelationshipCandidate.status == "PENDING",
                )
                .order_by(
                    RelationshipCandidate.confidence.desc(), RelationshipCandidate.created_at
                )
                .limit(scan_limit)
            )
        ).all()
    )
    truncated = total_pending > len(candidates)

    datasources_by_id: dict[UUID, DataSource] = {datasource.id: datasource}
    other_datasource_ids = {c.target_datasource_id for c in candidates} - datasources_by_id.keys()
    if other_datasource_ids:
        for row in (
            await session.scalars(
                select(DataSource).where(DataSource.id.in_(other_datasource_ids))
            )
        ).all():
            datasources_by_id[row.id] = row

    table_ids = {c.source_table_id for c in candidates} | {c.target_table_id for c in candidates}
    tables_by_id: dict[UUID, str] = {}
    if table_ids:
        for table, schema, catalog in (
            await session.execute(
                select(MetadataTable, MetadataSchema, MetadataCatalog)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(MetadataTable.id.in_(table_ids))
            )
        ).all():
            tables_by_id[table.id] = f"{catalog.name}.{schema.name}.{table.name}"

    column_ids = {c.source_column_id for c in candidates} | {c.target_column_id for c in candidates}
    columns_by_id: dict[UUID, str] = {}
    if column_ids:
        for column in (
            await session.scalars(select(MetadataColumn).where(MetadataColumn.id.in_(column_ids)))
        ).all():
            columns_by_id[column.id] = column.name

    scored: list[tuple[RelationshipCandidateImpact, RelationshipCandidate]] = []
    for candidate in candidates:
        source_ds = datasources_by_id.get(candidate.datasource_id, datasource)
        target_ds = datasources_by_id.get(candidate.target_datasource_id, datasource)
        impact = await compute_relationship_candidate_impact(
            session,
            candidate,
            source_datasource=source_ds,
            target_datasource=target_ds,
            depth=depth,
            node_limit=node_limit,
            settings=settings,
        )
        scored.append((impact, candidate))

    scored.sort(key=lambda pair: (-pair[0].impact_score, -pair[1].confidence, str(pair[1].id)))
    page = scored[offset : offset + limit]

    items: list[RelationshipCandidateReviewItemRead] = []
    for impact, candidate in page:
        snapshot = relationship_candidate_diff_snapshot(
            candidate,
            source_qualified_name=tables_by_id.get(
                candidate.source_table_id, str(candidate.source_table_id)
            ),
            source_column_name=columns_by_id.get(
                candidate.source_column_id, str(candidate.source_column_id)
            ),
            target_qualified_name=tables_by_id.get(
                candidate.target_table_id, str(candidate.target_table_id)
            ),
            target_column_name=columns_by_id.get(
                candidate.target_column_id, str(candidate.target_column_id)
            ),
        )
        diff = diff_relationship_candidate(snapshot)
        items.append(
            RelationshipCandidateReviewItemRead(
                candidate=RelationshipCandidateRead.model_validate(candidate),
                diff=[
                    RelationshipCandidateDiffEntryRead(
                        field=entry.field, change=entry.change, after=entry.after
                    )
                    for entry in diff.entries
                ],
                impact=RelationshipCandidateImpactRead(
                    impact_score=impact.impact_score,
                    source_table_impact=impact.source_table_impact,
                    target_table_impact=impact.target_table_impact,
                    depth=impact.depth,
                    node_limit=impact.node_limit,
                    truncated=impact.truncated,
                ),
                can_review=(
                    reviewer_principal_id is None
                    or candidate.created_by != reviewer_principal_id
                ),
            )
        )

    return RelationshipCandidateReviewQueueRead(
        datasource_id=datasource.id,
        items=items,
        limit=limit,
        offset=offset,
        scanned_count=len(candidates),
        total_pending_count=total_pending,
        truncated=truncated,
    )
