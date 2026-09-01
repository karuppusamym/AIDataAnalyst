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
  traceable fact, each carrying a `source`), and a structured `diff` -- SM-7's
  own `compose_governance_review_diff` (`aida.semantic_api`), reused directly,
  never reimplemented, so this surface and `GET
  /v1/governance/reviews/{id}/diff` cannot disagree on what is diffable for a
  given review or what its diff looks like.
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

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AssetDescriptionDraft,
    GlossaryLinkProposal,
    GovernanceReview,
    MetadataEnrichmentProposal,
    SemanticMetricProposal,
    TermSemanticBinding,
)
from aida.review_queue_schemas import ReviewQueueProposalRead
from aida.schemas import EvidenceItemRead
from aida.semantic_api import GovernanceReviewDiffRead, compose_governance_review_diff


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


def _metadata_enrichment_evidence(proposal: MetadataEnrichmentProposal) -> list[EvidenceItemRead]:
    source = f"metadata_enrichment_proposal:{proposal.id}"
    items = [
        EvidenceItemRead(
            category="BUSINESS_SEMANTICS_PROPOSAL",
            claim=(
                f"Proposed by the {proposal.engine_type} engine "
                f"(version {proposal.engine_version})"
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


async def compose_review_queue(
    session: AsyncSession, reviews: Sequence[GovernanceReview]
) -> list[ReviewQueueProposalRead]:
    """Compose one `ReviewQueueProposalRead` per review in `reviews`, in a
    fixed number of batched queries independent of `len(reviews)` for the
    confidence/evidence side (one query per distinct proposal type present in
    the batch, following `aida.catalog_read_model`'s idiom). The diff side
    calls SM-7's own `compose_governance_review_diff` once per review --
    unavoidable to reuse it unchanged rather than reimplement it in batched
    form, and each call is itself already O(1) queries.
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
    term_bindings = await _term_semantic_bindings_by_id(
        session, ids_by_type.get("TERM_SEMANTIC_BINDING", [])
    )

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
                evidence = _dict_evidence_items(
                    draft.evidence,
                    category="DESCRIPTION_DRAFT",
                    source=f"asset_description_draft:{draft.id}.evidence",
                )
        elif review.object_type == "TERM_SEMANTIC_BINDING" and object_id is not None:
            binding = term_bindings.get(object_id)
            if binding is not None:
                evidence = _term_binding_evidence(binding)

        diff: GovernanceReviewDiffRead = await compose_governance_review_diff(session, review)
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
    )
