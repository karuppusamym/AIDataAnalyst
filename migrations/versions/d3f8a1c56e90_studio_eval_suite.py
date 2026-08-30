"""studio: usage-derived eval question corpus and change-set regression gate (ST-A8)

Revision ID: d3f8a1c56e90
Revises: b5249498ee93
Create Date: 2026-08-30 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f8a1c56e90"
down_revision: str | Sequence[str] | None = "b5249498ee93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_eval_question",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=50), nullable=False),
        sa.Column("object_id", sa.String(length=100), nullable=False),
        sa.Column("evidence_source", sa.String(length=30), nullable=False),
        sa.Column("evidence_edge_id", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False),
        sa.Column("mined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_studio_eval_question_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_eval_question")),
        sa.UniqueConstraint(
            "organization_id",
            "object_type",
            "object_id",
            name="uq_studio_eval_question_org_object",
        ),
        sa.CheckConstraint(
            "evidence_source IN ('CONSUMPTION', 'BI')",
            name="ck_studio_eval_question_evidence_source",
        ),
    )
    op.create_index(
        op.f("ix_studio_eval_question_organization_id"),
        "studio_eval_question",
        ["organization_id"],
    )
    op.create_index(
        "ix_studio_eval_question_org_object",
        "studio_eval_question",
        ["organization_id", "object_type", "object_id"],
    )

    op.create_table(
        "studio_eval_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_studio_eval_run_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["studio_change_set.id"],
            name=op.f("fk_studio_eval_run_change_set_id_studio_change_set"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_eval_run")),
    )
    op.create_index(
        op.f("ix_studio_eval_run_organization_id"), "studio_eval_run", ["organization_id"]
    )
    op.create_index(
        "ix_studio_eval_run_change_set", "studio_eval_run", ["change_set_id", "started_at"]
    )

    op.create_table(
        "studio_eval_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("eval_run_id", sa.Uuid(), nullable=False),
        sa.Column("eval_question_id", sa.Uuid(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_studio_eval_result_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["eval_run_id"],
            ["studio_eval_run.id"],
            name=op.f("fk_studio_eval_result_eval_run_id_studio_eval_run"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["eval_question_id"],
            ["studio_eval_question.id"],
            name=op.f("fk_studio_eval_result_eval_question_id_studio_eval_question"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_eval_result")),
    )
    op.create_index(
        op.f("ix_studio_eval_result_organization_id"),
        "studio_eval_result",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_studio_eval_result_eval_run_id"), "studio_eval_result", ["eval_run_id"]
    )
    op.create_index(
        op.f("ix_studio_eval_result_eval_question_id"),
        "studio_eval_result",
        ["eval_question_id"],
    )
    op.create_index(
        "ix_studio_eval_result_run", "studio_eval_result", ["eval_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_studio_eval_result_run", table_name="studio_eval_result")
    op.drop_index(
        op.f("ix_studio_eval_result_eval_question_id"), table_name="studio_eval_result"
    )
    op.drop_index(op.f("ix_studio_eval_result_eval_run_id"), table_name="studio_eval_result")
    op.drop_index(
        op.f("ix_studio_eval_result_organization_id"), table_name="studio_eval_result"
    )
    op.drop_table("studio_eval_result")

    op.drop_index("ix_studio_eval_run_change_set", table_name="studio_eval_run")
    op.drop_index(op.f("ix_studio_eval_run_organization_id"), table_name="studio_eval_run")
    op.drop_table("studio_eval_run")

    op.drop_index("ix_studio_eval_question_org_object", table_name="studio_eval_question")
    op.drop_index(
        op.f("ix_studio_eval_question_organization_id"), table_name="studio_eval_question"
    )
    op.drop_table("studio_eval_question")
