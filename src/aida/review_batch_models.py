"""R11-REV01: a frozen selection of governance reviews, and what became of each member.

Kept out of `aida.models` for the reason `governed_execution_models` and
`sql_workspace_models` give: `models.py` is under concurrent edit, and two additive
tables are easier to review on their own.

**Why freeze at all.** A batch decision over a live filter decides whatever the filter
matches at the moment the button is pressed -- including items that arrived, or changed,
after the reviewer last looked. The design (`Docs/10-architecture/22-context-enrichment-
and-review-workspace.md`, "Review at estate scale") asks for the opposite: a batch binds
explicit review ids *and the evidence version each was inspected at*. `ReviewBatch` is
that binding; `ReviewBatchItem` is one member, its frozen evidence fingerprint, and --
once decided -- its own outcome and reason code.

**Not a second decision engine.** Nothing here decides a review. Applying a batch walks its
members through `governance_decision_service.decide_review`, the same compare-and-set claim,
maker-checker guard and agent-oversight guard the single, bulk and sample-review endpoints
use (`aida.review_batches.decide_review_batch`). A batch row is a ledger of *which* reviews a
reviewer bound together and *what happened to each*, nothing more.

**Value-free (INV-6).** Every column is an id, a code, a count or a SHA-256 fingerprint. No
evidence text, no draft text, no rationale and no exception message is persisted here: the
reviewer's rationale lands on `governance_review.decision_reason` exactly as it does for
every other decision path, and a refusal is recorded as a reason code.

**Tenancy (INV-5).** Both tables carry `organization_id` (RESTRICT) and every read in
`aida.review_batches` restates it.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aida.models import TimestampMixin
from atlas.platform.db import Base

#: A batch is FROZEN until one decision is applied to it, then DECIDED. The transition is a
#: compare-and-set on this column, so a double-submitted decision cannot apply twice.
BATCH_STATUSES = ("FROZEN", "DECIDED")

#: ELIGIBLE members are re-checked and, if still current, decided. EXCLUDED members were
#: refused when the batch was frozen (not found, not pending, maker-checker, stale) and are
#: never decided through this batch; they are kept so the preview can say why.
ITEM_ELIGIBILITY = ("ELIGIBLE", "EXCLUDED")

#: PENDING until the batch is decided; then APPLIED, REFUSED (re-check or decision refused)
#: or SKIPPED (excluded at freeze time).
ITEM_OUTCOMES = ("PENDING", "APPLIED", "REFUSED", "SKIPPED")


class ReviewBatch(Base, TimestampMixin):
    """One reviewer's frozen selection of governance reviews."""

    __tablename__ = "review_batch"
    __table_args__ = (
        CheckConstraint("status IN ('FROZEN', 'DECIDED')", name="status"),
        CheckConstraint(
            "decision IS NULL OR decision IN ('APPROVE', 'REJECT')", name="decision"
        ),
        Index("ix_review_batch_org_created_by", "organization_id", "created_by"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The principal that froze the batch. Only it may decide the batch: the binding is to
    #: what *this* reviewer inspected, and handing it to someone else would decide evidence
    #: they never saw.
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_type: Mapped[str] = mapped_column(String(30), nullable=False)
    #: EXPLICIT (ids the reviewer selected, across pages) or FILTER (a snapshot of the
    #: filtered queue at freeze time, capped and reported as truncated when it was).
    selection_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    selection_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="FROZEN")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: SHA-256 over the ordered (review id, evidence fingerprint) pairs: the batch's own
    #: version. Two batches with the same fingerprint bound the same evidence.
    selection_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(10))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: reason code -> count, written once when the batch is decided. Codes only.
    outcome_counts: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ReviewBatchItem(Base, TimestampMixin):
    """One member of a frozen batch: the review, the evidence version it was bound at, and
    its outcome."""

    __tablename__ = "review_batch_item"
    __table_args__ = (
        UniqueConstraint("batch_id", "review_id", name="uq_review_batch_item_member"),
        UniqueConstraint("batch_id", "position", name="uq_review_batch_item_position"),
        CheckConstraint("eligibility IN ('ELIGIBLE', 'EXCLUDED')", name="eligibility"),
        CheckConstraint(
            "outcome IN ('PENDING', 'APPLIED', 'REFUSED', 'SKIPPED')", name="outcome"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_batch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: No foreign key, deliberately: a member the caller named that does not exist in this
    #: organization is recorded as EXCLUDED/NOT_FOUND so the preview can report it, and such
    #: an id has no row to reference.
    review_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    #: 0-based order of selection; the keyset for paging a batch's members.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(100))
    review_family: Mapped[str | None] = mapped_column(String(30))
    #: The review's status when frozen (PENDING for every eligible member).
    frozen_status: Mapped[str | None] = mapped_column(String(30))
    #: SHA-256 of the reviewed content (`aida.review_batches.item_fingerprint`) at the
    #: version the reviewer inspected. NULL only for a NOT_FOUND member.
    evidence_fingerprint: Mapped[str | None] = mapped_column(String(64))
    eligibility: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Why an EXCLUDED member was excluded (NOT_FOUND, NOT_PENDING, MAKER_CHECKER, ...).
    exclusion_code: Mapped[str | None] = mapped_column(String(50))
    #: Why APPROVE would be refused for this member even while current
    #: (EVIDENCE_NOT_SHOWN, INDIVIDUAL_DECISION_REQUIRED); NULL when approvable. REJECT is
    #: not gated on it.
    approve_gate_code: Mapped[str | None] = mapped_column(String(50))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    reason_code: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: For an APPLIED member whose object type has a governed correction: the subject that
    #: correction acts on (e.g. TABLE + table id for a description withdrawal). Ids only.
    correction_subject_type: Mapped[str | None] = mapped_column(String(30))
    correction_subject_id: Mapped[str | None] = mapped_column(String(100))
