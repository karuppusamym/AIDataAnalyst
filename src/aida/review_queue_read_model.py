"""UX-17: review-queue read model -- run summary plus each proposal's diff,
confidence and evidence, composed in a fixed, page-size-independent number of
batched queries (module 21 experience shell, built on SM-7/UX-12/UX-13).

Scoping note (read this before extending object-type coverage)
----------------------------------------------------------------
The tracker row asks for "a run's proposals" -- a batch of governance-queue
proposals from one inference/scan pass. That grouping genuinely exists in the
data model for exactly one proposal type: `MetadataEnrichmentProposal` carries
`inference_run_id` -> `SemanticInferenceRun`, a real persisted run. No other
proposal type feeding the unified `GovernanceReview` queue is grouped this
way -- `GlossaryLinkProposal`, `SemanticMetricProposal`, `AssetDescriptionDraft`
and `TermSemanticBinding` are each submitted and reviewed one at a time, with
`governance_review_id` a 1:1 pointer to a single review, not a batch key; a
`SEMANTIC_MODEL_VERSION`/`GLOSSARY_TERM_VERSION` review (the only two types
SM-7 can diff) is created by an ad-hoc `submit_for_review` call, never a scan
pass. Building a uniform cross-type "run" would need a new persisted grouping
field on several tables, which is out of scope here (no `models.py` edit, no
migration -- see `Docs/60-delivery/03-tracker.md` UX-17's claim note).

So this module composes at two granularities instead of inventing a fake
unifying one:

* `compose_review_queue` -- the general case. Given any list of
  `GovernanceReview` rows a caller already selected (by organization, status,
  object type, or -- for the one type that has a real run -- by
  `inference_run_id`; see `review_queue_api.get_review_queue`), returns one
  composed row per review: its own status/decision fields, a numeric
  `confidence` where the proposal type carries one, `evidence` in
  `aida.asset_evidence`'s established `EvidenceItemRead` shape (one item per
  traceable fact, each carrying a `source`), and a structured `diff`.
* `compose_review_queue_diffs` -- F16. Until 2026-09-06 the diff for each row
  came from SM-7's own `compose_governance_review_diff` (`aida.semantic_api`),
  called once per review: correct, and the one remaining query path whose cost
  scaled with page size (up to five statements per diffable row, so up to five
  thousand for a 1,000-row page). Diffs are now composed one *type* at a time
  from batched snapshot loads. Reuse-by-calling was the old guarantee that this
  surface and `GET /v1/governance/reviews/{id}/diff` could not disagree; the
  new guarantee is `tests/test_review_queue_read_model.py::
  test_embedded_diff_matches_sm7_endpoint_directly`, which asserts the two
  composers return the same object for the same review -- a check that fails
  the build, where the old one relied on nobody forking the call.
* `summarize_review_queue` -- F16's other half. Counts by status, object type
  and requested action from a single grouped `COUNT(*)`, composing nothing, so
  the Overview screen can render a number without asking for a page of a
  thousand fully-composed reviews to count its length.
* The API layer (`review_queue_api.py`) additionally accepts an
  `inference_run_id` filter, which is the genuine "a run's proposals" view
  for `METADATA_ENRICHMENT_PROPOSAL` reviews -- the one case where "run"
  is not a euphemism for "whatever the caller filtered by."

Confidence and evidence per proposal type
------------------------------------------
``METADATA_ENRICHMENT_PROPOSAL``
    `MetadataEnrichmentProposal.confidence` (native field); evidence items for
    the engine that produced it (rules-only vs. model-assisted), the inference
    run it came from, and each entry of `evidence["evidence_ids"]` (the rule
    tags / metadata fingerprints `semantic_inference.infer_table_semantics`
    already records).
``GLOSSARY_LINK_PROPOSAL``
    `.confidence`; one evidence item per key in `.evidence` (matched label,
    match strategy, annotation version -- `stewardship_api`'s
    `GlossaryLinkProposal.evidence` payload).
``SEMANTIC_METRIC_PROPOSAL`` / ``ASSET_DESCRIPTION_DRAFT``
    `.overall_score` as the numeric confidence (GL-9's evidence-scored gate,
    the same score that gates submission -- `ensure_reviewable`); one evidence
    item per key in `.evidence` (`metric_suggestion_service.evidence_payload`
    / `asset_description_service.evidence_payload`).
``TERM_SEMANTIC_BINDING``
    No confidence field -- a steward's own request, not a scored proposal
    (`confidence=None`); evidence is the binding's own term/object identity.
``QUALITY_RULE_PROPOSAL``
    `.confidence` -- how much profile history the rule rests on. The proposed
    rule itself comes first, name, type and threshold, because the queue has
    no diff for it; then one item per key in `.evidence`
    (`quality_rule_proposals`).
``SEMANTIC_MODEL_VERSION`` / ``GLOSSARY_TERM_VERSION``
    No confidence field either (human-authored content submitted for review,
    not an inference); `confidence=None`, `evidence=[]` -- the *diff* carries
    the content for these two, which is exactly what SM-7 built for them.
Anything else in the queue (``BULK_STEWARDSHIP_OPERATION``,
``GLOSSARY_CONFLICT``, ``ASSET_DOCUMENTATION_VERSION``, AI-registry/tool/
marketplace review types, ...) still gets a row -- `confidence=None`,
`evidence=[]`, `diffable=False` via SM-7's own fallback -- rather than being
silently dropped from the queue.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AssetDescriptionDraft,
    ColumnDescriptionDraft,
    GlossaryLinkProposal,
    GlossaryTermVersion,
    GovernanceReview,
    MetadataEnrichmentProposal,
    SemanticMetric,
    SemanticMetricProposal,
    SemanticMetricVersion,
    SemanticModelVersion,
    TermSemanticBinding,
)
from aida.quality_rule_proposal_model import QualityRuleProposal
from aida.review_queue_schemas import ReviewQueueProposalRead
from aida.schemas import EvidenceItemRead
from aida.semantic_api import GovernanceReviewDiffRead, SemanticFieldDeltaRead
from aida.semantic_diff import diff_semantic_object


def _parse_object_id(review: GovernanceReview) -> UUID | None:
    try:
        return UUID(review.object_id)
    except (ValueError, AttributeError):
        return None


async def _metadata_enrichment_proposals_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, MetadataEnrichmentProposal]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(MetadataEnrichmentProposal).where(MetadataEnrichmentProposal.id.in_(ids))
    )
    return {row.id: row for row in rows.all()}


async def _glossary_link_proposals_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, GlossaryLinkProposal]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(GlossaryLinkProposal).where(GlossaryLinkProposal.id.in_(ids))
    )
    return {row.id: row for row in rows.all()}


async def _semantic_metric_proposals_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, SemanticMetricProposal]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(SemanticMetricProposal).where(SemanticMetricProposal.id.in_(ids))
    )
    return {row.id: row for row in rows.all()}


async def _asset_description_drafts_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, AssetDescriptionDraft]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(AssetDescriptionDraft).where(AssetDescriptionDraft.id.in_(ids))
    )
    return {row.id: row for row in rows.all()}


async def _column_description_drafts_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, ColumnDescriptionDraft]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(ColumnDescriptionDraft).where(ColumnDescriptionDraft.id.in_(ids))
    )
    return {row.id: row for row in rows.all()}


async def _quality_rule_proposals_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, QualityRuleProposal]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(QualityRuleProposal).where(QualityRuleProposal.id.in_(ids))
    )
    return {row.id: row for row in rows.all()}


async def _term_semantic_bindings_by_id(
    session: AsyncSession, ids: Sequence[UUID]
) -> dict[UUID, TermSemanticBinding]:
    if not ids:
        return {}
    rows = await session.scalars(select(TermSemanticBinding).where(TermSemanticBinding.id.in_(ids)))
    return {row.id: row for row in rows.all()}


def _dict_evidence_items(
    evidence: dict[str, object], *, category: str, source: str
) -> list[EvidenceItemRead]:
    """One `EvidenceItemRead` per key in a flat JSON-safe evidence payload
    (the shape every `evidence_payload()` helper in this codebase already
    produces -- `metric_suggestion_service.evidence_payload`,
    `asset_description_service.evidence_payload`, and the inline dicts
    `stewardship_api`/`semantic_inference` build for their own proposals).
    Deterministic key order keeps the composed list stable across calls.
    """
    return [
        EvidenceItemRead(category=category, claim=f"{key}: {value}", source=source)
        for key, value in sorted(evidence.items(), key=lambda item: item[0])
    ]


def _proposed_text_item(text: str, *, source: str) -> EvidenceItemRead:
    """The drafted text itself, as the first thing a reviewer reads.

    Description drafts have no field diff -- `compose_review_queue_diffs`
    gives them `_NOT_DIFFABLE_MESSAGE` -- and their evidence payloads record the
    *signals* a draft was built from, not the draft. Before this item existed a
    reviewer working from the queue approved table-description text the queue
    never showed them. `_dict_evidence_items` sorts keys, so this cannot be
    folded into that dict and still come first.
    """
    return EvidenceItemRead(
        category="DESCRIPTION_DRAFT", claim=f"proposed_description: {text}", source=source
    )


def _metadata_enrichment_evidence(proposal: MetadataEnrichmentProposal) -> list[EvidenceItemRead]:
    source = f"metadata_enrichment_proposal:{proposal.id}"
    items = [
        EvidenceItemRead(
            category="BUSINESS_SEMANTICS_PROPOSAL",
            claim=(
                f"Proposed by the {proposal.engine_type} engine (version {proposal.engine_version})"
            ),
            source=source,
            occurred_at=proposal.created_at,
        ),
        EvidenceItemRead(
            category="BUSINESS_SEMANTICS_PROPOSAL",
            claim=f"From inference run {proposal.inference_run_id}",
            source="semantic_inference_run",
        ),
    ]
    evidence = proposal.evidence or {}
    for evidence_id in evidence.get("evidence_ids", []):
        items.append(
            EvidenceItemRead(
                category="BUSINESS_SEMANTICS_PROPOSAL",
                claim=f"Evidence: {evidence_id}",
                source=f"{source}.evidence.evidence_ids",
            )
        )
    for key, value in sorted(evidence.items(), key=lambda item: item[0]):
        if key in ("evidence_ids", "model_call"):
            continue
        items.append(
            EvidenceItemRead(
                category="BUSINESS_SEMANTICS_PROPOSAL",
                claim=f"{key}: {value}",
                source=f"{source}.evidence",
            )
        )
    return items


def _term_binding_evidence(binding: TermSemanticBinding) -> list[EvidenceItemRead]:
    return [
        EvidenceItemRead(
            category="TERM_BINDING",
            claim=(
                f"Bind glossary term {binding.term_id} to "
                f"{binding.semantic_object_type} {binding.semantic_object_id}"
            ),
            source=f"term_semantic_binding:{binding.id}",
            occurred_at=binding.created_at,
        )
    ]


MODEL_VERSION_TYPE = "SEMANTIC_MODEL_VERSION"
GLOSSARY_TERM_VERSION_TYPE = "GLOSSARY_TERM_VERSION"

# Wording reproduced verbatim from `semantic_api.compose_governance_review_diff`'s
# non-diffable branch. `test_review_queue_read_model.py::
# test_embedded_diff_matches_sm7_endpoint_directly` compares the two composers'
# output field by field, so a divergence here fails the build rather than
# quietly giving one surface different words than the other.
_NOT_DIFFABLE_MESSAGE = (
    "structured diffs are not yet available for {object_type}; "
    "the raw proposed object is still reachable through its own read endpoint"
)


def _metric_snapshot(
    metric_version: SemanticMetricVersion, metric: SemanticMetric
) -> dict[str, Any]:
    return {
        "name": metric_version.name,
        "description": metric_version.description,
        "aggregation": metric_version.aggregation,
        "grain": metric_version.grain,
        "source_table_id": str(metric_version.source_table_id),
        "measure_column_id": (
            str(metric_version.measure_column_id) if metric_version.measure_column_id else None
        ),
        "default_time_column_id": (
            str(metric_version.default_time_column_id)
            if metric_version.default_time_column_id
            else None
        ),
        "allowed_dimension_column_ids": sorted(metric_version.allowed_dimension_column_ids),
    }


async def _semantic_model_snapshots(
    session: AsyncSession, version_ids: set[UUID]
) -> tuple[dict[UUID, dict[str, Any]], dict[UUID, UUID | None]]:
    """Batched equivalent of `semantic_api._semantic_model_version_snapshot`
    plus `_published_semantic_model_version_id`, for a whole page at once.

    Three queries regardless of how many versions are asked for: the requested
    versions, their projects' published counterparts, and every metric version
    belonging to either set. Returns `(snapshot by version id, published
    counterpart id by requested version id)`.

    The counterpart lookup orders by `version` descending where the single-row
    original took whichever row the database returned first. That is strictly
    more determinstic, not less: a project is only ever meant to have one
    PUBLISHED version, and if that invariant is ever broken the batched form
    picks the newest instead of an arbitrary one.
    """
    if not version_ids:
        return {}, {}
    versions = (
        await session.scalars(
            select(SemanticModelVersion).where(SemanticModelVersion.id.in_(version_ids))
        )
    ).all()
    by_id = {version.id: version for version in versions}
    project_ids = {version.project_id for version in versions}
    published = (
        await session.scalars(
            select(SemanticModelVersion)
            .where(
                SemanticModelVersion.project_id.in_(project_ids),
                SemanticModelVersion.status == "PUBLISHED",
            )
            .order_by(SemanticModelVersion.version.desc())
        )
    ).all()
    published_by_project: dict[UUID, list[SemanticModelVersion]] = {}
    for candidate in published:
        published_by_project.setdefault(candidate.project_id, []).append(candidate)

    counterpart: dict[UUID, UUID | None] = {}
    needed: set[UUID] = set(by_id)
    for version_id, version in list(by_id.items()):
        match = next(
            (
                candidate
                for candidate in published_by_project.get(version.project_id, [])
                if candidate.id != version_id
            ),
            None,
        )
        counterpart[version_id] = match.id if match is not None else None
        if match is not None:
            by_id.setdefault(match.id, match)
            needed.add(match.id)

    metric_rows = (
        await session.execute(
            select(SemanticMetricVersion, SemanticMetric)
            .join(SemanticMetric, SemanticMetricVersion.metric_id == SemanticMetric.id)
            .where(SemanticMetricVersion.semantic_model_version_id.in_(needed))
        )
    ).all()
    metrics_by_version: dict[UUID, dict[str, Any]] = {version_id: {} for version_id in needed}
    for metric_version, metric in metric_rows:
        metrics_by_version[metric_version.semantic_model_version_id][metric.slug] = (
            _metric_snapshot(metric_version, metric)
        )

    snapshots = {
        version_id: {
            "name": by_id[version_id].name,
            "change_summary": by_id[version_id].change_summary,
            "metrics": metrics_by_version.get(version_id, {}),
        }
        for version_id in needed
    }
    return snapshots, counterpart


async def _glossary_term_snapshots(
    session: AsyncSession, version_ids: set[UUID]
) -> tuple[dict[UUID, dict[str, Any]], dict[UUID, UUID | None]]:
    """Batched equivalent of `semantic_api._glossary_term_version_snapshot`
    plus `_published_glossary_term_version_id`. Two queries for a whole page.
    """
    if not version_ids:
        return {}, {}
    versions = (
        await session.scalars(
            select(GlossaryTermVersion).where(GlossaryTermVersion.id.in_(version_ids))
        )
    ).all()
    by_id = {version.id: version for version in versions}
    term_ids = {version.term_id for version in versions}
    approved = (
        await session.scalars(
            select(GlossaryTermVersion)
            .where(
                GlossaryTermVersion.term_id.in_(term_ids),
                GlossaryTermVersion.status == "APPROVED",
            )
            .order_by(GlossaryTermVersion.version.desc())
        )
    ).all()
    approved_by_term: dict[UUID, list[GlossaryTermVersion]] = {}
    for candidate in approved:
        approved_by_term.setdefault(candidate.term_id, []).append(candidate)

    counterpart: dict[UUID, UUID | None] = {}
    for version_id, version in list(by_id.items()):
        match = next(
            (
                candidate
                for candidate in approved_by_term.get(version.term_id, [])
                if candidate.id != version_id
            ),
            None,
        )
        counterpart[version_id] = match.id if match is not None else None
        if match is not None:
            by_id.setdefault(match.id, match)

    snapshots = {
        version_id: {
            "display_name": version.display_name,
            "definition": version.definition,
            "synonyms": sorted(version.synonyms),
            "owner_principal": version.owner_principal,
        }
        for version_id, version in by_id.items()
    }
    return snapshots, counterpart


def _diff_read(
    review: GovernanceReview,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    message: str | None,
) -> GovernanceReviewDiffRead:
    diff = diff_semantic_object(before, after) if after is not None else None
    return GovernanceReviewDiffRead(
        review_id=review.id,
        object_type=review.object_type,
        object_id=review.object_id,
        diffable=diff is not None,
        before=before,
        after=after,
        entries=[
            SemanticFieldDeltaRead(
                field=entry.field,
                change=entry.change,
                before=entry.before,
                after=entry.after,
            )
            for entry in (diff.entries if diff is not None else [])
        ],
        message=message,
    )


async def compose_review_queue_diffs(
    session: AsyncSession, reviews: Sequence[GovernanceReview]
) -> dict[UUID, GovernanceReviewDiffRead]:
    """F16: every review's diff in at most five queries, whatever the page size.

    The composer this replaces on the list path
    (`semantic_api.compose_governance_review_diff`) is correct and stays the
    single-review endpoint's implementation. It is simply per-review: up to five
    statements for each diffable row, so a 1,000-row page issued up to five
    thousand. This groups the page by object type first and loads each type's
    snapshots in one pass, which is the only way "the queue endpoint's cost does
    not depend on how many rows you asked for" can be true.

    Reusing SM-7's function verbatim was the previous anti-divergence
    mechanism; the replacement is stronger and is a test rather than a comment:
    `test_embedded_diff_matches_sm7_endpoint_directly` asserts this function and
    `compose_governance_review_diff` return the same object for the same review.
    """
    model_ids: dict[UUID, UUID] = {}
    term_ids: dict[UUID, UUID] = {}
    for review in reviews:
        object_id = _parse_object_id(review)
        if object_id is None:
            continue
        if review.object_type == MODEL_VERSION_TYPE:
            model_ids[review.id] = object_id
        elif review.object_type == GLOSSARY_TERM_VERSION_TYPE:
            term_ids[review.id] = object_id

    model_snapshots, model_counterparts = await _semantic_model_snapshots(
        session, set(model_ids.values())
    )
    term_snapshots, term_counterparts = await _glossary_term_snapshots(
        session, set(term_ids.values())
    )

    composed: dict[UUID, GovernanceReviewDiffRead] = {}
    for review in reviews:
        if review.id in model_ids:
            object_id = model_ids[review.id]
            if object_id not in model_snapshots:
                # Same 409 the single-review composer raises: a queue row whose
                # target vanished is a conflict, not an empty diff.
                raise HTTPException(status_code=409, detail="review target is unavailable")
            counterpart = model_counterparts.get(object_id)
            composed[review.id] = _diff_read(
                review,
                before=model_snapshots[counterpart] if counterpart is not None else {},
                after=model_snapshots[object_id],
                message=None,
            )
        elif review.id in term_ids:
            object_id = term_ids[review.id]
            if object_id not in term_snapshots:
                raise HTTPException(status_code=409, detail="review target is unavailable")
            counterpart = term_counterparts.get(object_id)
            composed[review.id] = _diff_read(
                review,
                before=term_snapshots[counterpart] if counterpart is not None else {},
                after=term_snapshots[object_id],
                message=None,
            )
        else:
            composed[review.id] = _diff_read(
                review,
                before=None,
                after=None,
                message=_NOT_DIFFABLE_MESSAGE.format(object_type=review.object_type),
            )
    return composed


@dataclass(frozen=True, slots=True)
class ReviewQueueSummary:
    """F16: the counts the Overview screen needs, without composing a single row.

    The finding was that the Overview asked for a page of up to 1,000 fully
    composed reviews -- diffs, evidence, snapshots -- in order to render a
    number. This is that number, from one grouped `COUNT(*)`: nothing is loaded,
    nothing is diffed, and the cost does not move when the queue grows.

    `by_queue` groups by `requested_action`, which is what distinguishes the
    work queues a reviewer actually chooses between (approve a publish, approve
    a withdrawal, ...) within one object type.
    """

    organization_id: UUID
    total: int
    by_status: dict[str, int]
    by_object_type: dict[str, int]
    by_queue: dict[str, int]


async def summarize_review_queue(
    session: AsyncSession,
    *,
    organization_id: UUID,
    status: str | None = None,
    object_type: str | None = None,
) -> ReviewQueueSummary:
    """Aggregate counts for one organization's governance-review queue.

    Exactly one statement, always: a `GROUP BY status, object_type,
    requested_action` whose result set is bounded by the product of those three
    small vocabularies, not by the number of reviews. `status`/`object_type`
    narrow it the same way `get_review_queue`'s filters do, so a caller can ask
    "how many PENDING glossary links" without fetching one.
    """
    filters = [GovernanceReview.organization_id == organization_id]
    if status:
        filters.append(GovernanceReview.status == status)
    if object_type:
        filters.append(GovernanceReview.object_type == object_type)
    rows = (
        await session.execute(
            select(
                GovernanceReview.status,
                GovernanceReview.object_type,
                GovernanceReview.requested_action,
                func.count(),
            )
            .where(*filters)
            .group_by(
                GovernanceReview.status,
                GovernanceReview.object_type,
                GovernanceReview.requested_action,
            )
        )
    ).all()
    by_status: Counter[str] = Counter()
    by_object_type: Counter[str] = Counter()
    by_queue: Counter[str] = Counter()
    total = 0
    for row_status, row_object_type, row_action, count in rows:
        count = int(count)
        total += count
        by_status[str(row_status)] += count
        by_object_type[str(row_object_type)] += count
        by_queue[str(row_action)] += count
    return ReviewQueueSummary(
        organization_id=organization_id,
        total=total,
        by_status=dict(by_status),
        by_object_type=dict(by_object_type),
        by_queue=dict(by_queue),
    )


async def compose_review_queue(
    session: AsyncSession, reviews: Sequence[GovernanceReview]
) -> list[ReviewQueueProposalRead]:
    """Compose one `ReviewQueueProposalRead` per review in `reviews`, in a
    fixed number of queries independent of `len(reviews)`.

    Confidence/evidence: one query per distinct proposal type present in the
    batch, following `aida.catalog_read_model`'s idiom. Diffs: one batched pass
    per diffable type (`compose_review_queue_diffs`) -- F16's fix for the last
    remaining per-row query path, which used to call SM-7's single-review
    composer once for every row on the page.
    """
    ids_by_type: dict[str, list[UUID]] = {}
    object_ids: dict[UUID, UUID] = {}
    for review in reviews:
        object_id = _parse_object_id(review)
        if object_id is None:
            continue
        object_ids[review.id] = object_id
        ids_by_type.setdefault(review.object_type, []).append(object_id)

    enrichment = await _metadata_enrichment_proposals_by_id(
        session, ids_by_type.get("METADATA_ENRICHMENT_PROPOSAL", [])
    )
    glossary_links = await _glossary_link_proposals_by_id(
        session, ids_by_type.get("GLOSSARY_LINK_PROPOSAL", [])
    )
    metric_proposals = await _semantic_metric_proposals_by_id(
        session, ids_by_type.get("SEMANTIC_METRIC_PROPOSAL", [])
    )
    description_drafts = await _asset_description_drafts_by_id(
        session, ids_by_type.get("ASSET_DESCRIPTION_DRAFT", [])
    )
    column_drafts = await _column_description_drafts_by_id(
        session, ids_by_type.get("COLUMN_DESCRIPTION_DRAFT", [])
    )
    term_bindings = await _term_semantic_bindings_by_id(
        session, ids_by_type.get("TERM_SEMANTIC_BINDING", [])
    )
    quality_rules = await _quality_rule_proposals_by_id(
        session, ids_by_type.get("QUALITY_RULE_PROPOSAL", [])
    )
    diffs = await compose_review_queue_diffs(session, reviews)

    composed: list[ReviewQueueProposalRead] = []
    for review in reviews:
        confidence: float | None = None
        evidence: list[EvidenceItemRead] = []
        object_id = object_ids.get(review.id)

        if review.object_type == "METADATA_ENRICHMENT_PROPOSAL" and object_id is not None:
            proposal = enrichment.get(object_id)
            if proposal is not None:
                confidence = proposal.confidence
                evidence = _metadata_enrichment_evidence(proposal)
        elif review.object_type == "GLOSSARY_LINK_PROPOSAL" and object_id is not None:
            link = glossary_links.get(object_id)
            if link is not None:
                confidence = link.confidence
                evidence = _dict_evidence_items(
                    link.evidence,
                    category="GLOSSARY_LINK_PROPOSAL",
                    source=f"glossary_link_proposal:{link.id}.evidence",
                )
        elif review.object_type == "SEMANTIC_METRIC_PROPOSAL" and object_id is not None:
            metric_proposal = metric_proposals.get(object_id)
            if metric_proposal is not None:
                confidence = metric_proposal.overall_score
                evidence = _dict_evidence_items(
                    metric_proposal.evidence,
                    category="METRIC_PROPOSAL",
                    source=f"semantic_metric_proposal:{metric_proposal.id}.evidence",
                )
        elif review.object_type == "ASSET_DESCRIPTION_DRAFT" and object_id is not None:
            draft = description_drafts.get(object_id)
            if draft is not None:
                confidence = draft.overall_score
                evidence = [
                    _proposed_text_item(
                        draft.drafted_text,
                        source=f"asset_description_draft:{draft.id}.drafted_text",
                    ),
                    *_dict_evidence_items(
                        draft.evidence,
                        category="DESCRIPTION_DRAFT",
                        source=f"asset_description_draft:{draft.id}.evidence",
                    ),
                ]
        elif review.object_type == "COLUMN_DESCRIPTION_DRAFT" and object_id is not None:
            column_draft = column_drafts.get(object_id)
            if column_draft is not None:
                confidence = column_draft.overall_score
                evidence = [
                    _proposed_text_item(
                        column_draft.drafted_text,
                        source=f"column_description_draft:{column_draft.id}.drafted_text",
                    ),
                    *_dict_evidence_items(
                        column_draft.evidence,
                        category="DESCRIPTION_DRAFT",
                        source=f"column_description_draft:{column_draft.id}.evidence",
                    ),
                ]
        elif review.object_type == "TERM_SEMANTIC_BINDING" and object_id is not None:
            binding = term_bindings.get(object_id)
            if binding is not None:
                evidence = _term_binding_evidence(binding)
        elif review.object_type == "QUALITY_RULE_PROPOSAL" and object_id is not None:
            rule_proposal = quality_rules.get(object_id)
            if rule_proposal is not None:
                confidence = rule_proposal.confidence
                evidence = [
                    EvidenceItemRead(
                        category="QUALITY_RULE_PROPOSAL",
                        claim=(
                            f"proposed rule: {rule_proposal.name} "
                            f"({rule_proposal.rule_type} {rule_proposal.threshold:g})"
                        ),
                        source=f"quality_rule_proposal:{rule_proposal.id}",
                    ),
                    *_dict_evidence_items(
                        rule_proposal.evidence,
                        category="QUALITY_RULE_PROPOSAL",
                        source=f"quality_rule_proposal:{rule_proposal.id}.evidence",
                    ),
                ]

        diff: GovernanceReviewDiffRead = diffs[review.id]
        composed.append(
            ReviewQueueProposalRead(
                review_id=review.id,
                organization_id=review.organization_id,
                object_type=review.object_type,
                object_id=review.object_id,
                requested_action=review.requested_action,
                status=review.status,
                requested_by=review.requested_by,
                decided_by=review.decided_by,
                decision_reason=review.decision_reason,
                decided_at=review.decided_at,
                created_at=review.created_at,
                confidence=confidence,
                evidence=evidence,
                diff=diff,
            )
        )
    return composed


def confidence_bearing_object_types() -> Iterable[str]:
    """Object types this module composes a real `confidence` for -- exposed
    for tests that want to assert coverage without hard-coding the list
    twice.
    """
    return (
        "METADATA_ENRICHMENT_PROPOSAL",
        "GLOSSARY_LINK_PROPOSAL",
        "SEMANTIC_METRIC_PROPOSAL",
        "ASSET_DESCRIPTION_DRAFT",
        "COLUMN_DESCRIPTION_DRAFT",
        "QUALITY_RULE_PROPOSAL",
    )
