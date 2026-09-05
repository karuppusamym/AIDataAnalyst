"""NT-1: a watermark so REVIEW_REQUESTED can be relayed without a funnel

Revision ID: e6b1c390d7a2
Revises: d5f2a7c81b46
Create Date: 2026-09-04 23:10:00.000000

`REVIEW_REQUESTED` was the one NT-1 event kind that never reached a real
channel. Every other kind has a single place it happens -- the review decision
core, the kill switch, incident routing, the certification sweep -- so a hook
there covers it. Review *creation* has 27 call sites across 17 modules and no
shared entry point, and adding a 27-site funnel to deliver a notification would
be a large, risky refactor performed for the benefit of a Slack message.

So the relay sweeps instead, and this column is its watermark: NULL means "not
yet considered". That makes the sweep idempotent without depending on the
notification ledger's dedup key, and it is the same shape
`asset_certification.expiry_warning_emitted_at` already uses for the P2-08
sweep.

A sweep is not merely the cheaper option here. It sits outside the governance
transaction entirely, so a notification can never affect the decision that
caused it, and a channel that was down when a review was raised gets the event
on the next pass instead of losing it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b1c390d7a2"
down_revision: str | Sequence[str] | None = "d5f2a7c81b46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "governance_review",
        sa.Column("review_requested_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The sweep's own predicate: pending, not yet considered, oldest first.
    # Without this it is a full scan of every review ever raised on every pass.
    op.create_index(
        "ix_governance_review_notify_backlog",
        "governance_review",
        ["status", "review_requested_notified_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_governance_review_notify_backlog", table_name="governance_review")
    op.drop_column("governance_review", "review_requested_notified_at")
