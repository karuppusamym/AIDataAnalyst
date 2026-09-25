"""R11-MP26: Ask conversations

Revision ID: c3d7e9a1f4b6
Revises: a8c4f1e2b7d9
Create Date: 2026-09-24

A follow-up question carries the earlier turns of its conversation
(`aida.conversations`). One table: a conversation one person owns on one
datasource, its turns a bounded JSON list holding each question in redacted form
(never raw text, never a value). The reaper deletes a conversation 30 days after
its last turn. No backfill: existing runs belong to no conversation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d7e9a1f4b6"
down_revision: str | Sequence[str] | None = "a8c4f1e2b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ask_conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("principal_type", sa.String(length=50), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("turns", sa.JSON(), nullable=False),
        sa.Column("last_turn_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ask_conversation_owner",
        "ask_conversation",
        ["organization_id", "principal_type", "principal_id", "last_turn_at"],
    )
    op.create_index("ix_ask_conversation_last_turn", "ask_conversation", ["last_turn_at"])
    op.create_index(
        op.f("ix_ask_conversation_datasource_id"), "ask_conversation", ["datasource_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ask_conversation_datasource_id"), table_name="ask_conversation")
    op.drop_index("ix_ask_conversation_last_turn", table_name="ask_conversation")
    op.drop_index("ix_ask_conversation_owner", table_name="ask_conversation")
    op.drop_table("ask_conversation")
