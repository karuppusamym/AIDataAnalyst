"""R11-REV01: request and response models for the change queue and frozen review batches.

Kept beside the router rather than in `aida.schemas` for the reason
`review_queue_schemas` gives: that module is a hot, shared file. See `aida.review_batches`
for the behavior these models describe.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from aida.review_batches import QUEUE_PAGE_MAX, REVIEW_BATCH_MAX_ITEMS, REVIEW_FAMILIES
from aida.schemas import ApiModel, EvidenceItemRead
from aida.semantic_api import GovernanceReviewDiffRead

_FINGERPRINT = r"^[0-9a-f]{64}$"


class ChangeQueueFilterRead(ApiModel):
    status: str | None
    object_types: list[str]
    families: list[str]
    change_kinds: list[str]
    object_id: str | None
    table_id: UUID | None
    decidable_only: bool


class ChangeQueueItemRead(ApiModel):
    """One queue row: enough to triage and select it, not its full evidence.

    `evidence_fingerprint` is the version a selection binds to; send it back when freezing
    a batch and a member that changed in between is excluded as `STALE_EVIDENCE`.
    `decide_blocker` is why this caller may not decide the row at all (NOT_PENDING,
    MAKER_CHECKER, UNSUPPORTED_TYPE, TARGET_UNAVAILABLE); `approve_gate` is why batch
    *approval* would be refused even so (INDIVIDUAL_DECISION_REQUIRED, NO_EVIDENCE_CONTRACT,
    EVIDENCE_NOT_SHOWN, REQUIRED_EVIDENCE_MISSING). `approve_evidence_required` names the
    facts the row's object type must have composed to be batch-approved (empty: the type has
    no contract and is reject-only in a batch); `approve_evidence_missing` names the ones
    this row lacks.
    """

    review_id: UUID
    object_type: str
    object_id: str
    review_family: str
    change_kind: str
    status: str
    requested_by: str
    created_at: datetime
    risk_tier: str
    confidence: float | None
    diffable: bool
    evidence_count: int
    evidence_preview: list[EvidenceItemRead]
    evidence_fingerprint: str
    decide_blocker: str | None
    approve_gate: str | None
    approve_evidence_required: list[str]
    approve_evidence_missing: list[str]
    target_unavailable: bool


class ChangeQueuePageRead(ApiModel):
    organization_id: UUID
    filters: ChangeQueueFilterRead
    generated_at: datetime
    limit: int
    #: Opaque keyset cursor for the next page; absent on the last page.
    next_cursor: str | None
    #: Rows matching the filters across every page (one COUNT), when requested.
    total: int | None
    items: list[ChangeQueueItemRead]


class ChangeQueueDetailRead(ApiModel):
    """Full evidence and diff for one row, fetched on demand when a reviewer opens it."""

    item: ChangeQueueItemRead
    evidence: list[EvidenceItemRead]
    diff: GovernanceReviewDiffRead | None


class ChangeQueueDetailsRead(ApiModel):
    items: list[ChangeQueueDetailRead]


class ReviewBatchSelectionWrite(ApiModel):
    review_id: UUID
    #: The fingerprint the reviewer saw on the queue page. Optional; when present, a member
    #: whose evidence moved since is excluded as STALE_EVIDENCE at freeze time.
    evidence_fingerprint: str | None = Field(default=None, pattern=_FINGERPRINT)


class ReviewBatchFilterWrite(ApiModel):
    status: str = Field(default="PENDING", max_length=30)
    object_types: list[str] = Field(default_factory=list, max_length=50)
    families: list[Literal[REVIEW_FAMILIES]] = Field(default_factory=list)  # type: ignore[valid-type]
    change_kinds: list[str] = Field(default_factory=list, max_length=50)
    object_id: str | None = Field(default=None, max_length=100)
    table_id: UUID | None = None
    decidable_only: bool = True


class ReviewBatchCreate(ApiModel):
    """Exactly one of: `items` (explicit, accumulated across queue pages) or `filter` (a
    snapshot of the filtered queue at freeze time, capped at REVIEW_BATCH_MAX_ITEMS and
    reported as truncated when the cap cut it)."""

    items: list[ReviewBatchSelectionWrite] | None = Field(
        default=None, min_length=1, max_length=REVIEW_BATCH_MAX_ITEMS
    )
    filter: ReviewBatchFilterWrite | None = None

    @model_validator(mode="after")
    def exactly_one_selection(self) -> ReviewBatchCreate:
        if (self.items is None) == (self.filter is None):
            raise ValueError("provide exactly one of 'items' or 'filter'")
        return self


class ReviewBatchCorrectionRead(ApiModel):
    kind: str
    available: bool
    method: str | None
    path: str | None
    subject_type: str | None
    subject_id: str | None
    reason_code: str | None


class ReviewBatchItemRead(ApiModel):
    review_id: UUID
    position: int
    object_type: str | None
    review_family: str | None
    frozen_status: str | None
    evidence_fingerprint: str | None
    eligibility: str
    exclusion_code: str | None
    approve_gate_code: str | None
    outcome: str
    reason_code: str | None
    decided_at: datetime | None
    correction: ReviewBatchCorrectionRead


class ReviewBatchRead(ApiModel):
    id: UUID
    organization_id: UUID
    created_by: str
    selection_mode: str
    selection_truncated: bool
    status: str
    item_count: int
    eligible_count: int
    selection_fingerprint: str
    decision: str | None
    decided_at: datetime | None
    created_at: datetime
    #: EXCLUDED members by exclusion code, ELIGIBLE members by approve-gate code, and every
    #: member by outcome -- one grouped count over the batch's members.
    exclusion_counts: dict[str, int]
    approve_gate_counts: dict[str, int]
    outcome_counts: dict[str, int]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def excluded_count(self) -> int:
        return self.item_count - self.eligible_count

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resumable(self) -> bool:
        """A decision was started and not finished: the batch is still FROZEN but its
        decision is recorded. Calling the decision again, with the same decision, resumes it
        at the first undecided member; `outcome_counts["PENDING"]` is how many remain."""
        return self.status == "FROZEN" and self.decision is not None


class ReviewBatchItemPageRead(ApiModel):
    batch_id: UUID
    next_cursor: str | None
    items: list[ReviewBatchItemRead]


class ReviewBatchDecisionCreate(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    #: Shared rationale; a per-member entry in `rationale_by_review_id` wins. A REJECT
    #: member with neither is refused as RATIONALE_REQUIRED rather than decided silently.
    reason: str | None = Field(default=None, max_length=2000)
    rationale_by_review_id: dict[UUID, str] | None = Field(
        default=None, max_length=REVIEW_BATCH_MAX_ITEMS
    )

    @model_validator(mode="after")
    def bound_rationales(self) -> ReviewBatchDecisionCreate:
        for value in (self.rationale_by_review_id or {}).values():
            if len(value) > 2000:
                raise ValueError("each rationale is at most 2000 characters")
        return self


class ReviewBatchDecisionMemberRead(ReviewBatchItemRead):
    #: The decision service's, target's or approve gate's own sentence for a refusal this
    #: call recorded, in this response only; the stored member carries the reason code alone
    #: (INV-6).
    detail: str | None = None
    #: False for a member an earlier, interrupted call of this decision had already recorded
    #: (or a concurrent caller of the same batch did): reported, not decided again.
    decided_in_this_call: bool = False


class ReviewBatchDecisionRead(ApiModel):
    batch: ReviewBatchRead
    #: SUCCESS (every member applied), PARTIAL_SUCCESS, or FAILURE (none applied).
    overall: str
    applied_count: int
    refused_count: int
    skipped_count: int
    #: This call resumed a decision an earlier call of this batch started and did not finish.
    resumed: bool
    decided_in_this_call_count: int
    members: list[ReviewBatchDecisionMemberRead]


__all__ = [
    "QUEUE_PAGE_MAX",
    "ChangeQueueDetailRead",
    "ChangeQueueDetailsRead",
    "ChangeQueueFilterRead",
    "ChangeQueueItemRead",
    "ChangeQueuePageRead",
    "ReviewBatchCorrectionRead",
    "ReviewBatchCreate",
    "ReviewBatchDecisionCreate",
    "ReviewBatchDecisionMemberRead",
    "ReviewBatchDecisionRead",
    "ReviewBatchFilterWrite",
    "ReviewBatchItemPageRead",
    "ReviewBatchItemRead",
    "ReviewBatchRead",
    "ReviewBatchSelectionWrite",
]
