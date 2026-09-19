"""R11-REV01: a frozen selection of governance reviews, and what became of each member --
and (`PlaybookDryRunRecord`, at the end) a stored playbook dry-run a later run binds to.

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


#: What `aida.playbook_dry_run.predicted_disposition` can answer, and what
#: `aida.playbooks.evaluate_and_run_playbook` can report.
DRY_RUN_DISPOSITIONS = ("NO_MATCHES", "AUTOMATIC", "HUMAN_REVIEW")
PLAYBOOK_RUN_OUTCOMES = ("NO_MATCHES", "AUTO_APPLIED", "QUEUED_FOR_REVIEW")


class PlaybookDryRunRecord(Base, TimestampMixin):
    """R11-REV01: a stored playbook dry-run -- what a rule matched, at which versions -- and,
    once a run is bound to it, which run that was and whether it matched the preview.

    Declared here rather than in a module of its own because this module is already
    registered on `Base.metadata` wherever the schema is built (`migrations/env.py`, the
    migration drift gate); both review-at-scale halves of R11-REV01 share it.

    **Why store a dry-run at all.** `GET /v1/playbooks/{id}/dry-run` answers "what would a run
    do now", but a steward who previews and then presses Run cannot tell whether the run did
    what they previewed: the catalog, or the rule, may have moved in between. A stored
    preview is the version a run can be bound to (`aida.playbook_dry_run.
    run_bound_to_dry_run`): the run compares its own evaluation to this record and, by
    default, refuses to act on anything the steward did not see.

    **Value-free (INV-6).** Ids, codes, counts and SHA-256 digests. `subject_versions` is the
    ordered list of `[subject id, evidence version]` pairs the preview matched -- ids and
    hashes, never the tag values, owners or classifications a preview *displays* -- so a
    later bound run can name which subjects moved, not merely that something did. The
    matcher caps a run at `CATALOG_BULK_ACTION_MAX_ITEMS`, which bounds the list.

    **Tenancy (INV-5).** `organization_id` (RESTRICT) is restated on every read. Deleting the
    playbook deletes its previews (CASCADE): a preview of a rule that no longer exists binds
    nothing, and the audit trail keeps what was previewed and run.
    """

    __tablename__ = "playbook_dry_run"
    __table_args__ = (
        CheckConstraint(
            "predicted_disposition IN ('NO_MATCHES', 'AUTOMATIC', 'HUMAN_REVIEW')",
            name="predicted_disposition",
        ),
        CheckConstraint(
            "bound_run_outcome IS NULL OR bound_run_outcome IN "
            "('NO_MATCHES', 'AUTO_APPLIED', 'QUEUED_FOR_REVIEW')",
            name="bound_run_outcome",
        ),
        CheckConstraint(
            "bound_binding_status IS NULL OR bound_binding_status IN ('MATCHES', 'DIFFERS')",
            name="bound_binding_status",
        ),
        Index("ix_playbook_dry_run_org_playbook", "organization_id", "playbook_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    playbook_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_playbook.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    #: `aida.playbook_dry_run.rule_version` of the playbook as previewed.
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 over the sorted matched subject ids: *which* subjects the rule matched.
    match_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 over the sorted (subject id, evidence version) pairs: the state each was in.
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tables_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    columns_truncated: Mapped[bool] = mapped_column(nullable=False, default=False)
    auto_apply_max_items: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    #: change code (CREATE, UPDATE, NO_CHANGE, SUPERSEDE) -> count.
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: `[[subject_id, evidence_version], ...]`, in match order. Ids and hashes only.
    subject_versions: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    evaluated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set once, when a run is bound to this preview; a preview binds at most one run.
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_by: Mapped[str | None] = mapped_column(String(255))
    bound_binding_status: Mapped[str | None] = mapped_column(String(20))
    bound_run_outcome: Mapped[str | None] = mapped_column(String(30))
    bound_bulk_action_run_id: Mapped[UUID | None] = mapped_column()
    bound_bulk_stewardship_operation_id: Mapped[UUID | None] = mapped_column()
