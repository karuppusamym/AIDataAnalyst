"""TL-1: tool certification corpus and workflow

Revision ID: dcb1fe4dfe6a
Revises: 8a7f3c1d4b22, d81e6c0f2a14
Create Date: 2026-08-30 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dcb1fe4dfe6a"
down_revision: str | Sequence[str] | None = ("8a7f3c1d4b22", "d81e6c0f2a14")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_certification_case",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("case_key", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("expectation", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tool_id"], ["governed_tool.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_id", "case_key", name="uq_tool_certification_case_key"),
    )
    for column in ("organization_id", "tool_id"):
        op.create_index(
            op.f(f"ix_tool_certification_case_{column}"),
            "tool_certification_case",
            [column],
        )
    op.create_index(
        "ix_tool_certification_case_tool_status",
        "tool_certification_case",
        ["tool_id", "status"],
    )

    op.create_table(
        "tool_certification_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("tool_version_id", sa.Uuid(), nullable=False),
        sa.Column("suite_version", sa.String(50), nullable=False),
        sa.Column("corpus_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("executed_by", sa.String(255), nullable=False),
        sa.Column("certified_by", sa.String(255)),
        sa.Column("decision_reason", sa.String(2000)),
        sa.Column("issued_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tool_id"], ["governed_tool.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tool_version_id"], ["governed_tool_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "tool_id", "tool_version_id"):
        op.create_index(
            op.f(f"ix_tool_certification_run_{column}"),
            "tool_certification_run",
            [column],
        )
    op.create_index(
        "ix_tool_certification_run_tool_created",
        "tool_certification_run",
        ["tool_id", "created_at"],
    )
    op.create_index(
        "ix_tool_certification_run_org_status",
        "tool_certification_run",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_tool_certification_run_version_status",
        "tool_certification_run",
        ["tool_version_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("tool_certification_run")
    op.drop_table("tool_certification_case")
