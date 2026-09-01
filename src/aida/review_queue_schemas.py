"""UX-17: response models for the review-queue read model.

Kept out of `aida.schemas` (read-only for this row -- see
`Docs/60-delivery/03-tracker.md` UX-17) and defined locally instead, the same
way SM-7's `GovernanceReviewDiffRead`/`SemanticFieldDeltaRead` live in
`aida.semantic_api` rather than `aida.schemas`.

`ReviewQueueRead.total_proposals`/`by_status`/`by_object_type`/
`diffable_count` are Pydantic `computed_field`s derived from `proposals` at
serialization time, never independently settable fields: passing e.g.
`total_proposals=` to the constructor raises a validation error (`ApiModel`'s
`extra="forbid"` rejects it as an unknown field, since a computed field is not
part of `__init__`'s signature) -- see
`tests/test_review_queue_read_model.py::test_counts_are_not_independently_settable`.
This is UX-17's own anti-drift requirement: "counts are derived from the
returned list, never carried beside it."
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from uuid import UUID

from pydantic import Field, computed_field

from aida.schemas import ApiModel, EvidenceItemRead
from aida.semantic_api import GovernanceReviewDiffRead


class ReviewQueueProposalRead(ApiModel):
    """One governance-review-queue proposal: its own review/decision fields
    (mirrors `GovernanceReviewRead` field-for-field), a numeric `confidence`
    where the proposal type carries one (`None` for a human-authored
    submission with no inference score), the evidence each rationale cites
    (`aida.asset_evidence`'s `EvidenceItemRead` shape), and a structured
    `diff` -- SM-7's own `compose_governance_review_diff`
    (`aida.semantic_api`), reused verbatim.
    """

    review_id: UUID
    organization_id: UUID
    object_type: str
    object_id: str
    requested_action: str
    status: str
    requested_by: str
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    created_at: datetime
    confidence: float | None = None
    evidence: list[EvidenceItemRead] = Field(default_factory=list)
    diff: GovernanceReviewDiffRead


class ReviewQueueRead(ApiModel):
    """A composed batch of review-queue proposals plus the filters that
    selected it. See `aida.review_queue_read_model` module docstring for why
    this is scoped to "whatever batch of reviews the caller selected" rather
    than a single, uniform "run" concept the data model does not have for
    every proposal type -- and for the one type that does (`inference_run_id`
    on `MetadataEnrichmentProposal`), `inference_run_id_filter` reflects it
    when the caller used it.
    """

    organization_id: UUID
    status_filter: str | None
    object_type_filter: str | None
    inference_run_id_filter: UUID | None
    generated_at: datetime
    proposals: list[ReviewQueueProposalRead]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_proposals(self) -> int:
        return len(self.proposals)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def by_status(self) -> dict[str, int]:
        return dict(Counter(proposal.status for proposal in self.proposals))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def by_object_type(self) -> dict[str, int]:
        return dict(Counter(proposal.object_type for proposal in self.proposals))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def diffable_count(self) -> int:
        return sum(1 for proposal in self.proposals if proposal.diff.diffable)
