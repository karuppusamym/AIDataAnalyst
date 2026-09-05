"""AG-10: per-agent budget attribution on the run

Revision ID: d5f2a7c81b46
Revises: c4a8d2f61e93
Create Date: 2026-09-04 22:30:00.000000

The agent inbox showed a budget cap with "usage not tracked" underneath it,
because nothing attributed model consumption to the agent that caused it.

These two columns close that. They are named `estimated_` deliberately: no
provider adapter in `build_model_providers` returns a usage block, so the only
number available is the 4-bytes-per-token heuristic the gateway already
enforces `model_max_input_tokens` against. That is the right number to show
anyway -- the cap is enforced against it, so consumption measured any other
way would not be comparable to the cap it is drawn beside.

Nullable, and NULL means something: no model call happened in that run (a
query-memory hit, or a refusal before generation). Zero would claim a call
that consumed nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f2a7c81b46"
down_revision: str | Sequence[str] | None = "c4a8d2f61e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("estimated_input_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_run", sa.Column("estimated_output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_run", "estimated_output_tokens")
    op.drop_column("agent_run", "estimated_input_tokens")
