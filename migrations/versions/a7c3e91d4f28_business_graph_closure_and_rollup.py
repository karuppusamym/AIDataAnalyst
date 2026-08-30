"""Closure and roll-up materialisation for the classification tree (ADR-0018 performance)

Added after benchmarking the ADR-0018 graph on PostgreSQL 16 with a bank-scale
taxonomy (13,548 nodes, depth 4) and 5,000,000 assignments. What the measurement
showed:

* Recursive-CTE traversal of the *tree* is fine -- ~3 ms down, ~3 ms up. The CTE
  never walks tables or columns, only the taxonomy, and the taxonomy is small.
* Roll-up is not fine. `count(DISTINCT target_id)` over a subtree ran ~3.1 s with a
  recursive CTE and ~0.9 s with a closure join. Neither is an interactive read.
* Reading a materialised roll-up runs in ~0.4 ms, and a full recompute of every node
  takes ~47 s as one grouped statement -- a batch job, not a request.

So: closure for traversal, materialisation for aggregation.

Both tables are pure projections of `business_node` / `business_assignment` and can
be dropped and rebuilt at any time (INV-1). Neither is ever read for an
authorization decision without the underlying tables agreeing.

Revision ID: a7c3e91d4f28
Revises: f1a2b3c4d5e6
Create Date: 2026-08-30 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e91d4f28"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A frozen copy of the rebuild statements as they stood on 2026-08-30. The canonical
# versions live in `aida.business_graph` and will evolve; a migration must not import
# application code, because a migration has to keep producing the same result years
# from now regardless of what the application has become.
REBUILD_CLOSURE = """
WITH RECURSIVE closure AS (
    SELECT organization_id, id AS ancestor_id, id AS descendant_id, 0 AS depth
    FROM business_node
    WHERE status = 'ACTIVE' AND effective_to IS NULL
    UNION ALL
    SELECT child.organization_id, closure.ancestor_id, child.id, closure.depth + 1
    FROM business_node child
    JOIN closure ON child.parent_id = closure.descendant_id
    WHERE child.status = 'ACTIVE' AND child.effective_to IS NULL
)
INSERT INTO business_node_closure (organization_id, ancestor_id, descendant_id, depth)
SELECT organization_id, ancestor_id, descendant_id, depth FROM closure
"""

REBUILD_ROLLUP = """
INSERT INTO business_node_rollup
    (organization_id, business_node_id, target_type, distinct_targets, computed_at)
SELECT assignment.organization_id,
       closure.ancestor_id,
       assignment.target_type,
       count(DISTINCT assignment.target_id),
       now()
FROM business_assignment AS assignment
JOIN business_node_closure AS closure
  ON closure.descendant_id = assignment.business_node_id
WHERE assignment.status = 'ACTIVE' AND assignment.effective_to IS NULL
GROUP BY assignment.organization_id, closure.ancestor_id, assignment.target_type
"""


def upgrade() -> None:
    op.create_table(
        "business_node_closure",
        sa.Column("ancestor_id", sa.Uuid(), nullable=False),
        sa.Column("descendant_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("ancestor_id", "descendant_id"),
        sa.ForeignKeyConstraint(["ancestor_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["descendant_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_business_node_closure_descendant", "business_node_closure", ["descendant_id"]
    )
    op.create_index("ix_business_node_closure_ancestor", "business_node_closure", ["ancestor_id"])
    op.create_index(
        "ix_business_node_closure_organization_id", "business_node_closure", ["organization_id"]
    )

    op.create_table(
        "business_node_rollup",
        sa.Column("business_node_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("distinct_targets", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("business_node_id", "target_type"),
        sa.ForeignKeyConstraint(["business_node_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_business_node_rollup_organization_id", "business_node_rollup", ["organization_id"]
    )

    # Seed both projections from whatever the ADR-0018 migration backfilled.
    bind = op.get_bind()
    bind.execute(sa.text(REBUILD_CLOSURE))
    bind.execute(sa.text(REBUILD_ROLLUP))


def downgrade() -> None:
    op.drop_table("business_node_rollup")
    op.drop_table("business_node_closure")
