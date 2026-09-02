"""Group K / AT-9 + AT-12: scope-aware glossary terms, query-history metric
candidates

AT-9 -- `glossary_term` uniqueness widens from a bare `(organization_id,
term_key)` pair to `(organization_id, term_key, business_node_id)` with a
nullable `business_node_id` (ADR-0018's business-graph axis) standing in for
an enterprise-wide default. Two independently governed definitions of the
same term can now coexist, each scoped to a different business node --
"exposure" in Risk is not "exposure" in Retail Banking -- with
most-specific-wins resolution and refusal-on-ambiguity living in
`aida.semantic_inference.resolve_scoped_glossary_term`
(`aida.agent_orchestrator` wires the refusal into the real grounded-question
path). A plain 3-column unique constraint would let Postgres admit more than
one `business_node_id IS NULL` (enterprise-default) row per term_key, since
Postgres treats NULLs as distinct within a UNIQUE constraint -- so the
enterprise-default slot is capped separately by a partial unique index
(`postgresql_where`/`sqlite_where`, mirroring
`uq_context_product_version_one_published`). The prior 2-column constraint
is dropped by column signature rather than by a hardcoded name: it was
declared without an explicit `name=` at creation
(`ab31d7e4c920_glossary_asset_documentation.py`), so its real name in a live
database is whatever Postgres's own default-naming convention assigned
rather than the ORM's `NAMING_CONVENTION` (which only applies when SQLAlchemy
compiles DDL through `Base.metadata`, not through Alembic's unbound
`op.create_table`).

AT-12 -- adds `query_history_metric_candidate`: a bounded, evidence-backed
metric candidate mined from value-free query-log structure (an aggregation
over a measure column, grouped by a grain-column set, seen repeatedly across
a warehouse's query history -- never sampled data values, INV-6/AT-C3).
Lands in the existing unified `governance_review` maker-checker queue
(object_type `QUERY_HISTORY_METRIC_CANDIDATE`), never auto-authoritative --
see `aida.query_history_miner`. Join-relationship candidates mined from the
same query log reuse `relationship_candidate` unmodified (a new
`detection_rule` value only), so no schema change is needed for those.

Revision ID: c3f7a1b9e2d4
Revises: 7e6460d905fe
Create Date: 2026-09-02 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f7a1b9e2d4"
down_revision: str | Sequence[str] | None = "7e6460d905fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_UNIQUE = "uq_glossary_term_org_key_node"
_ENTERPRISE_DEFAULT_INDEX = "uq_glossary_term_org_key_enterprise_default"

# Postgres-only: locates the original `(organization_id, term_key)` unique
# constraint by its column signature (not its name, which was never pinned --
# see the module docstring) and drops it. A no-op if no such constraint is
# found, so this migration stays idempotent against a database whose
# constraint was somehow already renamed or removed.
_DROP_OLD_UNIQUE_BY_SIGNATURE = sa.text(
    """
    DO $$
    DECLARE
        found_name text;
    BEGIN
        SELECT tc.constraint_name INTO found_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = 'glossary_term'
          AND tc.constraint_type = 'UNIQUE'
        GROUP BY tc.constraint_name
        HAVING array_agg(kcu.column_name ORDER BY kcu.ordinal_position)
               = ARRAY['organization_id', 'term_key']
        LIMIT 1;

        IF found_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE glossary_term DROP CONSTRAINT %I', found_name);
        END IF;
    END $$;
    """
)


def upgrade() -> None:
    # --- AT-9: glossary_term business-node scoping ------------------------
    op.add_column("glossary_term", sa.Column("business_node_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_glossary_term_business_node_id"), "glossary_term", ["business_node_id"]
    )
    op.create_foreign_key(
        op.f("fk_glossary_term_business_node_id_business_node"),
        "glossary_term",
        "business_node",
        ["business_node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(_DROP_OLD_UNIQUE_BY_SIGNATURE)
    op.create_unique_constraint(
        _NEW_UNIQUE, "glossary_term", ["organization_id", "term_key", "business_node_id"]
    )
    op.create_index(
        _ENTERPRISE_DEFAULT_INDEX,
        "glossary_term",
        ["organization_id", "term_key"],
        unique=True,
        postgresql_where=sa.text("business_node_id IS NULL"),
        sqlite_where=sa.text("business_node_id IS NULL"),
    )

    # --- AT-12: query_history_metric_candidate -----------------------------
    op.create_table(
        "query_history_metric_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("measure_column_id", sa.Uuid(), nullable=False),
        sa.Column("aggregation", sa.String(length=30), nullable=False),
        sa.Column("grain_column_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("grain_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("detection_rule", sa.String(length=100), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("published_metric_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["measure_column_id"], ["metadata_column.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"], ["governance_review.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["published_metric_version_id"],
            ["semantic_metric_version.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "table_id",
            "measure_column_id",
            "aggregation",
            "grain_fingerprint",
            name="uq_query_history_metric_candidate_shape",
        ),
        sa.UniqueConstraint("governance_review_id"),
    )
    op.create_index(
        op.f("ix_query_history_metric_candidate_organization_id"),
        "query_history_metric_candidate",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_query_history_metric_candidate_project_id"),
        "query_history_metric_candidate",
        ["project_id"],
    )
    op.create_index(
        op.f("ix_query_history_metric_candidate_datasource_id"),
        "query_history_metric_candidate",
        ["datasource_id"],
    )
    op.create_index(
        op.f("ix_query_history_metric_candidate_table_id"),
        "query_history_metric_candidate",
        ["table_id"],
    )
    op.create_index(
        op.f("ix_query_history_metric_candidate_measure_column_id"),
        "query_history_metric_candidate",
        ["measure_column_id"],
    )
    op.create_index(
        op.f("ix_query_history_metric_candidate_published_metric_version_id"),
        "query_history_metric_candidate",
        ["published_metric_version_id"],
    )
    op.create_index(
        "ix_query_history_metric_candidate_org_status",
        "query_history_metric_candidate",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_query_history_metric_candidate_org_status",
        table_name="query_history_metric_candidate",
    )
    op.drop_index(
        op.f("ix_query_history_metric_candidate_published_metric_version_id"),
        table_name="query_history_metric_candidate",
    )
    op.drop_index(
        op.f("ix_query_history_metric_candidate_measure_column_id"),
        table_name="query_history_metric_candidate",
    )
    op.drop_index(
        op.f("ix_query_history_metric_candidate_table_id"),
        table_name="query_history_metric_candidate",
    )
    op.drop_index(
        op.f("ix_query_history_metric_candidate_datasource_id"),
        table_name="query_history_metric_candidate",
    )
    op.drop_index(
        op.f("ix_query_history_metric_candidate_project_id"),
        table_name="query_history_metric_candidate",
    )
    op.drop_index(
        op.f("ix_query_history_metric_candidate_organization_id"),
        table_name="query_history_metric_candidate",
    )
    op.drop_table("query_history_metric_candidate")

    op.drop_index(_ENTERPRISE_DEFAULT_INDEX, table_name="glossary_term")
    op.drop_constraint(_NEW_UNIQUE, "glossary_term", type_="unique")
    op.create_unique_constraint(
        "uq_glossary_term_organization_id_term_key",
        "glossary_term",
        ["organization_id", "term_key"],
    )
    op.drop_constraint(
        op.f("fk_glossary_term_business_node_id_business_node"),
        "glossary_term",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_glossary_term_business_node_id"), table_name="glossary_term")
    op.drop_column("glossary_term", "business_node_id")
