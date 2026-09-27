"""R11-VAL04: the last outcome of each scheduler pass, for the Operations screen

Revision ID: c4a7e2d9b815
Revises: 53558182d9fb
Create Date: 2026-09-21

Since round 12 a scheduler pass that raises no longer ends the loop: it is logged as
`scheduler_pass_failed` and counted in `aida_scheduler_pass_failures_total`. Both live in
the scheduler process, and the Operations screen reads the API, a different process, so a
pass failing on every iteration was still invisible to anyone not reading logs or a
Prometheus the local stack does not run. `scheduler_pass_status` is where the leading
replica writes each pass's outcome once per iteration: one row per pass, keyed by its name.

Platform-wide, with no `organization_id`: a pass runs over every organization, and the row
holds only a pass name, three timestamps, a consecutive-failure count and an exception
class name. The exception's message is deliberately not stored, because it can carry data.
No backfill: the first iteration after the deploy writes every row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a7e2d9b815"
down_revision: str | Sequence[str] | None = "53558182d9fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_pass_status",
        sa.Column("pass_name", sa.String(length=64), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=200), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("pass_name"),
    )


def downgrade() -> None:
    op.drop_table("scheduler_pass_status")
