"""R11-OKF02: durable, atomically published OKF bundles

Revision ID: e3a9c7d51f02
Revises: c5e9a2f7d314
Create Date: 2026-09-17

Three tables, declared in `aida.okf_store_models`. R11-OKF01 deliberately added no migration: an
export was frozen and rendered per request, so there was nothing to store and "atomic
publication" was true only because nothing was ever published. This row makes a bundle a stored
thing a consumer reads, and incremental rebuild needs the prior bundle to diff against.

* `okf_bundle_publication` -- one immutable bundle: the frozen snapshot it was rendered from, the
  manifest, the digests and what changed against the previous publication. Written once.
* `okf_bundle_document` -- one file of one publication, with its exact bytes. Written once.
* `okf_bundle_head` -- the pointer to the current publication for one product version under one
  authority. Moved in the same transaction that writes the publication it names, which is what
  makes publication atomic for a reader: old head and old documents, or new head and new
  documents, never a mixture.

**Keyed on authority.** `authority_digest` is a digest of the product version and the set of
datasources a reader's own authorization admitted. Two readers with the same admitted set share
one publication -- REST, MCP and the UI read the same stored bundle -- and a reader whose grant
was revoked computes a different digest, so a bundle built under that grant is not reachable
from their request at all. Nothing here is a cache keyed without the caller's authority.

**No body text, no values (INV-6).** The snapshot column holds `OkfSnapshot` written out, a type
with no field that can carry a body, a definition, a default expression or a row. The document
text is the rendered bundle after the Atlas publish policy passed it.

`organization_id` is RESTRICT on all three, as on every other axis (INV-5). The version foreign
keys CASCADE: a deleted product version's bundles have nothing left to describe. The head's
publication foreign key is RESTRICT, so a publication cannot be removed from under the pointer.

No backfill: the tables start empty and fill the first time a bundle is read.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a9c7d51f02"
down_revision: str | Sequence[str] | None = "c5e9a2f7d314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PUBLICATION = "okf_bundle_publication"
_DOCUMENT = "okf_bundle_document"
_HEAD = "okf_bundle_head"


def upgrade() -> None:
    op.create_table(
        _PUBLICATION,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("authority_digest", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=30), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("content_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("bundle_content_digest", sa.String(length=64), nullable=False),
        sa.Column("scope_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("rendered_count", sa.Integer(), nullable=False),
        sa.Column("carried_count", sa.Integer(), nullable=False),
        sa.Column("change_summary", sa.JSON(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("built_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_product_version_id"],
            ["context_product_version.id"],
            name=op.f(f"fk_{_PUBLICATION}_context_product_version_id_context_product_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_PUBLICATION}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_PUBLICATION}")),
        sa.UniqueConstraint(
            "context_product_version_id",
            "authority_digest",
            "sequence",
            name="uq_okf_bundle_publication_lineage_sequence",
        ),
    )
    op.create_index(
        "ix_okf_bundle_publication_lineage",
        _PUBLICATION,
        ["context_product_version_id", "authority_digest"],
        unique=False,
    )
    op.create_index(
        op.f(f"ix_{_PUBLICATION}_context_product_version_id"),
        _PUBLICATION,
        ["context_product_version_id"],
        unique=False,
    )
    op.create_index(
        op.f(f"ix_{_PUBLICATION}_organization_id"),
        _PUBLICATION,
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        _DOCUMENT,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("subject_key", sa.String(length=64), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("rendered_in_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_DOCUMENT}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            [f"{_PUBLICATION}.id"],
            name=op.f(f"fk_{_DOCUMENT}_publication_id_{_PUBLICATION}"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_DOCUMENT}")),
        sa.UniqueConstraint("publication_id", "path", name="uq_okf_bundle_document_path"),
    )
    op.create_index(
        "ix_okf_bundle_document_subject",
        _DOCUMENT,
        ["publication_id", "subject_key"],
        unique=False,
    )
    op.create_index(
        op.f(f"ix_{_DOCUMENT}_organization_id"), _DOCUMENT, ["organization_id"], unique=False
    )
    op.create_index(
        op.f(f"ix_{_DOCUMENT}_publication_id"), _DOCUMENT, ["publication_id"], unique=False
    )

    op.create_table(
        _HEAD,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("authority_digest", sa.String(length=64), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("marks_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("marks_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_product_version_id"],
            ["context_product_version.id"],
            name=op.f(f"fk_{_HEAD}_context_product_version_id_context_product_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_HEAD}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            [f"{_PUBLICATION}.id"],
            name=op.f(f"fk_{_HEAD}_publication_id_{_PUBLICATION}"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_HEAD}")),
        sa.UniqueConstraint(
            "context_product_version_id",
            "authority_digest",
            name="uq_okf_bundle_head_lineage",
        ),
    )
    op.create_index(
        op.f(f"ix_{_HEAD}_context_product_version_id"),
        _HEAD,
        ["context_product_version_id"],
        unique=False,
    )
    op.create_index(op.f(f"ix_{_HEAD}_organization_id"), _HEAD, ["organization_id"], unique=False)
    op.create_index(op.f(f"ix_{_HEAD}_publication_id"), _HEAD, ["publication_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_HEAD}_publication_id"), table_name=_HEAD)
    op.drop_index(op.f(f"ix_{_HEAD}_organization_id"), table_name=_HEAD)
    op.drop_index(op.f(f"ix_{_HEAD}_context_product_version_id"), table_name=_HEAD)
    op.drop_table(_HEAD)
    op.drop_index(op.f(f"ix_{_DOCUMENT}_publication_id"), table_name=_DOCUMENT)
    op.drop_index(op.f(f"ix_{_DOCUMENT}_organization_id"), table_name=_DOCUMENT)
    op.drop_index("ix_okf_bundle_document_subject", table_name=_DOCUMENT)
    op.drop_table(_DOCUMENT)
    op.drop_index(op.f(f"ix_{_PUBLICATION}_organization_id"), table_name=_PUBLICATION)
    op.drop_index(
        op.f(f"ix_{_PUBLICATION}_context_product_version_id"), table_name=_PUBLICATION
    )
    op.drop_index("ix_okf_bundle_publication_lineage", table_name=_PUBLICATION)
    op.drop_table(_PUBLICATION)
