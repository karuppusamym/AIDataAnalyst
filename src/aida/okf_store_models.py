"""R11-OKF02: durable storage for published OKF bundles -- one file for three new tables.

Kept out of `aida.models` for the reason `change_signal_models` and `procedure_lineage_models`
give: `models.py` is under concurrent edit, and three new tables for one new capability are
easier to review as one additive file registered on the same `Base`.

**The shape, and why it publishes atomically.**

* `OkfBundlePublication` is one immutable published bundle: the frozen snapshot it was rendered
  from, its manifest, its digests and what changed since the publication before it. It is
  written once and never updated.
* `OkfBundleDocument` is one file of one publication, with the exact bytes a reader receives.
  Also written once. A document that did not change since the previous publication is written
  again with the *same bytes* and records the sequence that first rendered them
  (`rendered_in_sequence`), which is how "an unchanged document kept its hash" becomes a stored
  fact rather than a recomputed coincidence.
* `OkfBundleHead` is the pointer: which publication is current for one context product version
  under one authority. A reader resolves the head, then reads that publication's documents.

A publication's rows and the head move in one transaction, so a reader either sees the old head
with the old, complete set of documents or the new head with the new, complete set -- never half
of each. Publications are never updated in place, so a reader holding an older publication id
keeps reading a coherent bundle while a newer one is published beside it.

**Keyed on authority, never shared across it (INV-5, OKF-D).** `authority_digest` is a digest of
the product version *and the exact set of datasources the reader's own authorization admitted*.
A head is only ever served to a caller whose live authorization, evaluated on that request,
produces the same digest. A bundle built while a cross-boundary grant was ACTIVE therefore stops
being reachable by anyone the moment the grant is revoked: their digest no longer matches it, so
the stored rows are not a cache anyone can hit.

**Value-free (INV-6).** The stored snapshot is `aida.okf_export.OkfSnapshot` written out, and no
field of that type can hold a body, a definition, a default expression or a row. The document
text is the rendered bundle, which the publish policy has already checked. `aida.okf_store`
refuses to store a bundle that fails that policy, and `tests/test_okf_store.py` plants sentinel
bodies, definitions and defaults and scans every row these tables hold for them.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base
from aida.models import TimestampMixin


class OkfBundlePublication(Base, TimestampMixin):
    """One immutable published OKF bundle for one product version under one authority."""

    __tablename__ = "okf_bundle_publication"
    __table_args__ = (
        UniqueConstraint(
            "context_product_version_id",
            "authority_digest",
            "sequence",
            name="uq_okf_bundle_publication_lineage_sequence",
        ),
        Index(
            "ix_okf_bundle_publication_lineage",
            "context_product_version_id",
            "authority_digest",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    context_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Digest of the product version plus the datasources the reader's authorization admitted.
    authority_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 1 for the first publication under this authority, then +1 per content change.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Why this publication exists: INITIAL, SOURCE_CHANGE, REVALIDATION or RENDERER_CHANGE.
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    #: When the snapshot was frozen. The only clock reading in the row that is not a content
    #: event; the concept documents never carry it.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: `aida.okf_export.snapshot_to_document(snapshot)`, verbatim.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Documents rendered by this publication, and documents whose stored bytes were carried
    #: over from the previous one without being rendered at all.
    rendered_count: Mapped[int] = mapped_column(Integer, nullable=False)
    carried_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Paths added, changed and removed against the previous publication, the subjects the
    #: change marks named, and any change found without a mark. Paths are opaque digests.
    change_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: The refresh history `log.md` is rendered from, newest first, bounded.
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    built_by: Mapped[str] = mapped_column(String(255), nullable=False)


class OkfBundleDocument(Base, TimestampMixin):
    """One file of one publication, with the bytes a reader receives."""

    __tablename__ = "okf_bundle_document"
    __table_args__ = (
        UniqueConstraint("publication_id", "path", name="uq_okf_bundle_document_path"),
        Index("ix_okf_bundle_document_subject", "publication_id", "subject_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    publication_id: Mapped[UUID] = mapped_column(
        ForeignKey("okf_bundle_publication.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The opaque identity key of the object, routine, package, concept or tool the document is
    #: about. NULL for an index or a log.
    subject_key: Mapped[str | None] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: The publication sequence that rendered these exact bytes. Equal to the owning
    #: publication's sequence when rendered now; earlier when carried over unchanged.
    rendered_in_sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class OkfBundleHead(Base, TimestampMixin):
    """Which publication is current for one product version under one authority.

    The only mutable row of the three. `publication_id` moves forward in the same transaction
    that writes the publication it points at. `validated_at` and the mark fields record the
    last time the head was confirmed current without a new publication being needed -- a no-op
    revalidation, which by acceptance OKF-C must not change a hash and therefore writes no
    publication at all.
    """

    __tablename__ = "okf_bundle_head"
    __table_args__ = (
        UniqueConstraint(
            "context_product_version_id",
            "authority_digest",
            name="uq_okf_bundle_head_lineage",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    context_product_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("context_product_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    authority_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_id: Mapped[UUID] = mapped_column(
        ForeignKey("okf_bundle_publication.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The change-mark window this head was validated against, and the digest of the marks
    #: inside it. A mark that commits later -- including one whose timestamp predates the
    #: freeze because its transaction was still open -- changes the digest.
    marks_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    marks_digest: Mapped[str] = mapped_column(String(64), nullable=False)
