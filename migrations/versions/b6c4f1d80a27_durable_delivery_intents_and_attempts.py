"""Durable delivery intents and attempts for SIEM events and notifications.

Two review findings share one table (`Docs/review-2026-09-05/REVIEW.md`).

**F04.** `route_to_siem` formatted a CEF message, logged it and returned
``True`` for both configured transports without opening a socket. There was
no row anywhere recording that a security event was owed to a SOC, so there
was nothing to retry and nothing to audit.

**F12.** A governance notification was POSTed inline; failure was swallowed,
`review_requested_notified_at` was stamped regardless, and the sweep that
would have retried selects only unstamped reviews. The failed message was
permanently gone.

`delivery_intent` makes the obligation a durable fact created inside the
business transaction, and `delivery_attempt` records what each transport
attempt actually got back. `requested_at`, `attempted_at` and `delivered_at`
are three columns because they are three different facts; the watermark on
`governance_review` is now tied to the first of them.

**`dedup_key` is indexed, not unique, deliberately.** A uniqueness violation
would abort the business transaction that staged the intent -- which would
let a chat integration fail a governance decision, the exact coupling this
change exists to remove. Deduplication is enforced by the worker at delivery
time instead (see `aida.delivery_intents`).

**No backfill.** There is nothing to backfill from: the old code persisted no
record of an undelivered security event at all, and the notification rows it
did write recorded a POST whose outcome was not retained. Inventing intents
for them would manufacture obligations nobody can verify.

Revision ID: b6c4f1d80a27
Revises: a71c5e0d9f34
Create Date: 2026-09-06 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6c4f1d80a27"
down_revision: str | Sequence[str] | None = "a71c5e0d9f34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_intent",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("destination", sa.String(500), nullable=False),
        sa.Column("dedup_key", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(100), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_outcome", sa.String(30), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("claimed_by", sa.String(200), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_delivery_intent_organization_id", "delivery_intent", ["organization_id"])
    op.create_index("ix_delivery_intent_due", "delivery_intent", ["state", "next_attempt_at"])
    op.create_index(
        "ix_delivery_intent_dedup",
        "delivery_intent",
        ["organization_id", "kind", "channel", "dedup_key"],
    )
    op.create_index("ix_delivery_intent_org_state", "delivery_intent", ["organization_id", "state"])

    op.create_table(
        "delivery_attempt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "intent_id",
            sa.Uuid(),
            sa.ForeignKey("delivery_intent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("transport", sa.String(30), nullable=False),
        sa.Column("destination", sa.String(500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_delivery_attempt_intent", "delivery_attempt", ["intent_id", "attempt_number"]
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_attempt_intent", table_name="delivery_attempt")
    op.drop_table("delivery_attempt")
    op.drop_index("ix_delivery_intent_org_state", table_name="delivery_intent")
    op.drop_index("ix_delivery_intent_dedup", table_name="delivery_intent")
    op.drop_index("ix_delivery_intent_due", table_name="delivery_intent")
    op.drop_index("ix_delivery_intent_organization_id", table_name="delivery_intent")
    op.drop_table("delivery_intent")
