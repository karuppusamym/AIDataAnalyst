"""catalog -- PRIVATE. SQLAlchemy models in this module's own schema
(`catalog`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).

Not importable from outside this module once the `module-privacy`
contract (tracker ST-02) is enforced.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.models`, which now re-exports these classes for backward
compatibility -- every existing `from aida.models import X` caller keeps
working unchanged. This is a Python-source-location move only: these
classes still declare no `schema=` in `__table_args__` and still live in
the single shared PostgreSQL schema. The actual database schema migration
(refactor plan Sec.5 steps 2.3/2.4) is explicitly deferred to a later,
separate pass. This module carries the refactor plan's own "Risk: medium"
flag (Sec.6 -- "catalog has many inbound callers"); the shim-at-the-old-path
technique is exactly its named mitigation ("keep the old import as a
deprecated alias for one release"), applied here from day one rather than
as a follow-up.

Owned tables (per Sec.4's register: "catalogs, schemas, tables, columns,
constraints, indexes, partitions, fingerprints, tombstones"):

* `MetadataCatalog`, `MetadataSchema`, `MetadataTable`, `MetadataColumn`,
  `MetadataConstraint`, `MetadataIndex`, `MetadataPartition` -- the
  catalog hierarchy itself. "Fingerprints" and "tombstones" are not
  separate tables: every one of the seven carries its own `fingerprint`
  column (drift detection) and represents a tombstoned object as
  `status != "ACTIVE"` plus `deprecated_at`, not a distinct record.

Explicitly NOT moved here despite living in the same neighborhood in the
old `aida.models`: `ClassificationEvidence` and its schemas
(`ClassificationEvidenceRead`, `ClassificationFeedRecord`,
`ClassificationFeedIngestRequest`, `ClassificationFeedIngestResponse`) --
"classifications" is module 05 (profiling)'s registered word, not this
module's, even though the evidence ledger references `MetadataColumn` by
ID. `aida.envelope_models`'s `MetadataViewDefinition`/`MetadataRoutine`/
etc. and `MetadataEnrichmentProposal`/`MetadataBusinessAnnotation`/
`MetadataBusinessAnnotationVersion` (module 07 semantic-layer's
"annotations") are likewise left for their own modules' extraction
passes. All stay in `aida.models` for now.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from atlas.platform.db import Base, TimestampMixin


class MetadataCatalog(Base, TimestampMixin):
    __tablename__ = "metadata_catalog"
    __table_args__ = (UniqueConstraint("datasource_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataSchema(Base, TimestampMixin):
    __tablename__ = "metadata_schema"
    __table_args__ = (UniqueConstraint("catalog_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    catalog_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_catalog.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataTable(Base, TimestampMixin):
    __tablename__ = "metadata_table"
    __table_args__ = (
        UniqueConstraint("schema_id", "name"),
        Index("ix_metadata_table_org_status", "organization_id", "status"),
        # Leading (datasource_id, status) matches list_tables' equality filters; the
        # trailing (name, id) matches its ORDER BY exactly, so the keyset predicate
        # `(name, id) > (:last_name, :last_id)` can be satisfied by a single index
        # range seek instead of a table scan, independent of how deep the cursor is.
        Index(
            "ix_metadata_table_ds_status_name_id", "datasource_id", "status", "name", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_schema.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_description: Mapped[str | None] = mapped_column(Text)
    # CT-4: set when a RenameCandidate naming this (tombstoned) row is approved and merged --
    # lets anyone still holding this stable ID resolve forward to the object it became.
    superseded_by_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )


class MetadataColumn(Base, TimestampMixin):
    __tablename__ = "metadata_column"
    __table_args__ = (
        UniqueConstraint("table_id", "name"),
        Index("ix_metadata_column_org_class", "organization_id", "classification"),
        # Mirrors ix_metadata_table_ds_status_name_id: leading (table_id, status)
        # matches list_columns' equality filters, trailing (ordinal_position, id)
        # matches its ORDER BY, so keyset paging stays a single index range seek.
        Index(
            "ix_metadata_column_table_status_ordinal_id",
            "table_id",
            "status",
            "ordinal_position",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    physical_type: Mapped[str] = mapped_column(String(255), nullable=False)
    nullable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    default_expression: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(30), default="UNCLASSIFIED", nullable=False)
    # "RULE" (deterministic name/type inference) or "EXTERNAL_AUTHORITATIVE" (a bank's own
    # classification feed — see aida.classification_feed). Once EXTERNAL_AUTHORITATIVE, rediscovery
    # must never let rule-based inference silently overwrite it again (module 05 §9 exit condition).
    classification_source: Mapped[str] = mapped_column(String(30), default="RULE", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataConstraint(Base, TimestampMixin):
    __tablename__ = "metadata_constraint"
    __table_args__ = (
        UniqueConstraint("table_id", "name"),
        Index("ix_metadata_constraint_org_type", "organization_id", "constraint_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    constraint_type: Mapped[str] = mapped_column(String(30), nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    referenced_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    referenced_columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataIndex(Base, TimestampMixin):
    __tablename__ = "metadata_index"
    __table_args__ = (
        UniqueConstraint("table_id", "name"),
        Index("ix_metadata_index_org_type", "organization_id", "index_type"),
        Index("ix_metadata_index_table_status_name_id", "table_id", "status", "name", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    index_type: Mapped[str] = mapped_column(String(30), default="UNKNOWN", nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataPartition(Base, TimestampMixin):
    __tablename__ = "metadata_partition"
    __table_args__ = (
        UniqueConstraint("table_id", "name"),
        Index("ix_metadata_partition_org_type", "organization_id", "partition_type"),
        Index(
            "ix_metadata_partition_table_status_ordinal_id",
            "table_id",
            "status",
            "ordinal_position",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    partition_type: Mapped[str] = mapped_column(String(30), default="UNKNOWN", nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    key_columns: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    high_value: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
