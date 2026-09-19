"""R11-OKF02: key a stored OKF bundle on one datasource as well as on a product version

Revision ID: f7c2d9a4b61e
Revises: 0871649b2bb1
Create Date: 2026-09-19

Design section 14: "Source bundles are scoped exports of discovered, authorized objects."
Migration `e3a9c7d51f02` keyed every stored bundle on a context product version, with the version
column NOT NULL, so a lineage for one datasource had nowhere to live. This extends the two keyed
tables rather than adding a second store:

* `okf_bundle_publication` and `okf_bundle_head` gain a nullable `datasource_id`, and their
  `context_product_version_id` becomes nullable. A check constraint (`ck_*_one_scope`) holds that
  exactly one of the two is set, so a row is a product lineage or a source lineage and never an
  ambiguous both-or-neither.
* The source lineage gets the same uniqueness the product lineage has: one head per
  (datasource, authority) and one sequence number per (datasource, authority, sequence), which is
  what makes a concurrent publisher lose cleanly. NULLs are distinct in a unique constraint, so
  the product and source constraints never constrain each other's rows.
* `okf_bundle_document` is untouched: a document belongs to a publication, whatever its scope.

Why extend rather than add tables: the atomic publish, the optimistic head move, retention,
pinned reads and the INV-6 storage rules are one code path in `aida.okf_store`, and a second set
of tables would mean a second copy of each -- the drift R11-OKF02 exists to rule out.

`datasource_id` CASCADEs, as the version key does: a deleted datasource's bundles describe
nothing. `organization_id` stays RESTRICT (INV-5). No backfill: every existing row is a product
lineage and satisfies the check as it stands.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c2d9a4b61e"
down_revision: str | Sequence[str] | None = "0871649b2bb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PUBLICATION = "okf_bundle_publication"
_HEAD = "okf_bundle_head"
_ONE_SCOPE = "(context_product_version_id IS NULL) <> (datasource_id IS NULL)"


def upgrade() -> None:
    for table in (_PUBLICATION, _HEAD):
        op.add_column(table, sa.Column("datasource_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_datasource_id_datasource"),
            table,
            "datasource",
            ["datasource_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(op.f(f"ix_{table}_datasource_id"), table, ["datasource_id"], unique=False)
        op.alter_column(
            table, "context_product_version_id", existing_type=sa.Uuid(), nullable=True
        )
        op.create_check_constraint(op.f(f"ck_{table}_one_scope"), table, _ONE_SCOPE)
    op.create_unique_constraint(
        "uq_okf_bundle_publication_source_sequence",
        _PUBLICATION,
        ["datasource_id", "authority_digest", "sequence"],
    )
    op.create_index(
        "ix_okf_bundle_publication_source_lineage",
        _PUBLICATION,
        ["datasource_id", "authority_digest"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_okf_bundle_head_source_lineage", _HEAD, ["datasource_id", "authority_digest"]
    )


def downgrade() -> None:
    # A source lineage cannot be represented once the version key is NOT NULL again. Stored
    # bundles are derived, rebuildable data -- the next read republishes from the catalog --
    # so they are removed rather than blocking the downgrade: heads first (their publication
    # key is RESTRICT), then publications, whose documents CASCADE.
    op.execute(sa.text("DELETE FROM okf_bundle_head WHERE datasource_id IS NOT NULL"))
    op.execute(sa.text("DELETE FROM okf_bundle_publication WHERE datasource_id IS NOT NULL"))
    op.drop_constraint("uq_okf_bundle_head_source_lineage", _HEAD, type_="unique")
    op.drop_index("ix_okf_bundle_publication_source_lineage", table_name=_PUBLICATION)
    op.drop_constraint(
        "uq_okf_bundle_publication_source_sequence", _PUBLICATION, type_="unique"
    )
    for table in (_HEAD, _PUBLICATION):
        op.drop_constraint(op.f(f"ck_{table}_one_scope"), table, type_="check")
        op.alter_column(
            table, "context_product_version_id", existing_type=sa.Uuid(), nullable=False
        )
        op.drop_index(op.f(f"ix_{table}_datasource_id"), table_name=table)
        op.drop_constraint(
            op.f(f"fk_{table}_datasource_id_datasource"), table, type_="foreignkey"
        )
        op.drop_column(table, "datasource_id")
