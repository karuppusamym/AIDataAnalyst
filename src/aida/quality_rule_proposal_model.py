"""ADR-0029: a quality rule an agent proposes and a person decides.

`QualityRule` (DQ-4) has no proposal state: a rule exists and runs, or it does
not. The quality agent's output therefore needs a row of its own that holds a
suggested rule until a person decides it through the governance review queue.
On approval the decision adapter (`aida.quality_rule_proposals`) creates the
`QualityRule`; a rejected proposal stays as the record that somebody said no,
so the same rule is never proposed again.

Declared outside `aida.models` for the reason `envelope_models` gives: that
module is under concurrent edit. It registers on the same `Base`, and
`migrations/env.py` imports it so autogenerate sees it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base
from aida.models import TimestampMixin

PROPOSAL_PENDING = "PENDING_APPROVAL"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_REJECTED = "REJECTED"


class QualityRuleProposal(Base, TimestampMixin):
    """One suggested DQ-4 rule, the evidence it rests on, and its decision.

    `(table_id, rule_type, column_id)` is the rule key the quality agent never
    proposes twice; it is indexed rather than unique because a key proposed,
    approved and later retired is still one history, not a conflict.
    `evidence` carries only counts and rates the profiler already stored
    (INV-6).
    """

    __tablename__ = "quality_rule_proposal"
    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('TABLE_ROW_COUNT_MIN', 'TABLE_ROW_COUNT_MAX', 'COLUMN_NULL_RATE_MAX')",
            name="rule_type_is_supported",
        ),
        CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED')",
            name="status_is_supported",
        ),
        Index("ix_quality_rule_proposal_org_status", "organization_id", "status"),
        Index("ix_quality_rule_proposal_rule_key", "table_id", "rule_type", "column_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), index=True
    )
    rule_type: Mapped[str] = mapped_column(String(30), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    #: The name the approved rule is created with, and the label its incidents
    #: carry.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: How much profile history the rule rests on (`profiles_used / window`).
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default=PROPOSAL_PENDING, nullable=False)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
    #: The rule approval created. A rule deleted later leaves the proposal, and
    #: with it the record that this key was already decided.
    applied_rule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("quality_rule.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
