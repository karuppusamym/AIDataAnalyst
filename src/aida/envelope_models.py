"""Persistence for the metadata ingestion envelope 1.1 axes (gap/02 row N1).

Envelope 1.0 stores catalogs, schemas, tables, columns and constraints. 1.1 adds
four axes that nothing in the estate could previously answer: the text a view is
defined by, the routines a schema exposes and their signatures, the descriptions
the source itself carries, and the privileges the source already grants.

**Why these tables live here and not in `aida.models`.** `models.py` is a single
2800-line module under concurrent edit and is scheduled to be split per module
schema (tracker ST-05). Declaring these classes against the same
`aida.db.Base` registers them on the same `MetaData`, so Alembic autogenerate and
`Base.metadata.create_all` both see them exactly as if they were declared there,
while the new axes arrive as one reviewable file instead of a diff in the middle
of everything else.

**Why the shape is what it is.** The two consumers queued behind this work are
view-DDL lineage parsing (gap/02 N2) and procedure-to-tool generation (N12).
Both parse text, so both need the definition back byte-for-byte, and both need to
tell three states apart that a nullable text column collapses into one:

| State | `availability` | `definition_sql` / `body_sql` | `truncated` |
|---|---|---|---|
| The source gave the full text | `AVAILABLE` | the text | `false` |
| The source gave a prefix | `AVAILABLE` | the prefix | `true` |
| The source would not give it | `UNAVAILABLE` | `NULL` | `false` |
| The object genuinely has no body | `AVAILABLE` | `''` | `false` |

A parser that cannot distinguish row 3 from row 4 either reports a view as
having no lineage when the truth is "we were not allowed to look", or retries
forever against a source that will never answer. `availability` +
`unavailable_reason` make that difference a column, not a convention -- the
envelope's honesty rule (`connectors/base.py`) survives into storage.

**Tenancy.** Every table here carries `organization_id` and `datasource_id`
(INV-5). `datasource_id` is not redundant with the parent FK: FULL-snapshot
reconciliation needs "every 1.1 row for this datasource" as one indexed query per
axis, and walking back up through `metadata_schema` -> `metadata_catalog` for
that would be a three-way join on the hottest path in ingestion.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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

#: `definition_sql` / `body_sql` holds what the source returned, including an
#: empty string when the object really is empty.
AVAILABLE = "AVAILABLE"

#: The source declined, errored, or the connector does not implement the axis.
#: `definition_sql` / `body_sql` is NULL and `unavailable_reason` says why.
UNAVAILABLE = "UNAVAILABLE"

AVAILABILITY_STATES = (AVAILABLE, UNAVAILABLE)

#: Object types `MetadataObjectDescription` accepts. `TABLE` is deliberately
#: absent: `metadata_table.source_description` already owns table comments and is
#: written by the 1.0 path, and two homes for one fact is how they diverge.
DESCRIBABLE_OBJECT_TYPES = ("CATALOG", "SCHEMA", "COLUMN")


class MetadataViewDefinition(Base, TimestampMixin):
    """The defining text of one view, one row per view.

    One-to-one with `metadata_table` rather than a column on it, because the
    definition of a large view is a multi-kilobyte text that no catalog listing,
    search projection or drift comparison ever needs to read, and because
    `models.py` is off-limits to this workstream (see the module docstring).
    """

    __tablename__ = "metadata_view_definition"
    __table_args__ = (
        UniqueConstraint("table_id"),
        CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name="availability_state",
        ),
        CheckConstraint(
            "(availability = 'AVAILABLE') = (definition_sql IS NOT NULL)",
            name="availability_matches_definition",
        ),
        Index("ix_metadata_view_definition_org_status", "organization_id", "status"),
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
    definition_sql: Mapped[str | None] = mapped_column(Text)
    is_materialized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_updatable: Mapped[bool | None] = mapped_column(Boolean)
    check_option: Mapped[str | None] = mapped_column(String(30))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    availability: Mapped[str] = mapped_column(String(20), default=AVAILABLE, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataRoutine(Base, TimestampMixin):
    """A stored procedure or function belonging to one schema.

    Keyed on `(schema_id, name, signature)` rather than `(schema_id, name)`:
    PostgreSQL permits overloads, so a name alone is not an identity, and a
    reconciliation keyed on the name would soft-delete every overload but the
    last one on each ingestion. `signature` is derived from the parameter
    physical types, so it is stable across snapshots without needing a
    source-side identifier that not every source has.
    """

    __tablename__ = "metadata_routine"
    __table_args__ = (
        UniqueConstraint("schema_id", "name", "signature"),
        CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name="availability_state",
        ),
        CheckConstraint(
            "(availability = 'AVAILABLE') = (body_sql IS NOT NULL)",
            name="availability_matches_body",
        ),
        Index("ix_metadata_routine_org_status", "organization_id", "status"),
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
    signature: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    routine_type: Mapped[str] = mapped_column(String(30), nullable=False)
    language: Mapped[str | None] = mapped_column(String(50))
    body_sql: Mapped[str | None] = mapped_column(Text)
    return_type: Mapped[str | None] = mapped_column(String(255))
    is_deterministic: Mapped[bool | None] = mapped_column(Boolean)
    security_mode: Mapped[str | None] = mapped_column(String(30))
    source_description: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    availability: Mapped[str] = mapped_column(String(20), default=AVAILABLE, nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(500))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataRoutineParameter(Base, TimestampMixin):
    """One parameter of one routine, ordered.

    A separate table rather than JSON on the routine because tool generation
    (N12) binds arguments by position and type, and a generator that reads its
    argument list out of an unconstrained JSON blob has no schema to fail
    against when a source changes shape.
    """

    __tablename__ = "metadata_routine_parameter"
    __table_args__ = (
        UniqueConstraint("routine_id", "ordinal_position"),
        Index("ix_metadata_routine_parameter_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    routine_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_routine.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(String(255))
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="IN", nullable=False)
    physical_type: Mapped[str] = mapped_column(String(255), nullable=False)
    default_expression: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataObjectDescription(Base, TimestampMixin):
    """A description the *source* carries, for an object with nowhere else to put it.

    Catalogs, schemas and columns have no description column in `models.py` and
    this workstream may not add one, so their comments land here. Exactly one of
    the three foreign keys is set, enforced by a check constraint rather than by
    a bare polymorphic `object_id`, so a deleted column takes its description
    with it instead of leaving a row pointing at nothing.

    Source descriptions are *evidence*, never authority: a steward-authored or
    model-proposed description lives in the enrichment tables and outranks this.
    """

    __tablename__ = "metadata_object_description"
    __table_args__ = (
        UniqueConstraint("catalog_id"),
        UniqueConstraint("schema_id"),
        UniqueConstraint("column_id"),
        CheckConstraint(
            "object_type IN ('CATALOG', 'SCHEMA', 'COLUMN')",
            name="object_type_is_describable",
        ),
        CheckConstraint(
            "(CASE WHEN catalog_id IS NULL THEN 0 ELSE 1 END) "
            "+ (CASE WHEN schema_id IS NULL THEN 0 ELSE 1 END) "
            "+ (CASE WHEN column_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="exactly_one_subject",
        ),
        Index("ix_metadata_object_description_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    object_type: Mapped[str] = mapped_column(String(20), nullable=False)
    catalog_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_catalog.id", ondelete="CASCADE"), index=True
    )
    schema_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_schema.id", ondelete="CASCADE"), index=True
    )
    column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MetadataSourceGrant(Base, TimestampMixin):
    """One privilege held by one grantee on one source object.

    **This grants nothing.** The policy engine does not read this table and no
    authorization decision consults it (INV-5 / ADR-0018 keep authority in the
    platform's own access policies). It exists so "who can already see this in
    the source" is answerable, and so a workspace source binding can be reviewed
    against what the source itself permits.

    `grant_key` is a SHA-256 over the natural key -- grantee, grantee type,
    privilege, object type and qualified object name -- because those five
    columns together exceed the byte budget of a B-tree unique index on real
    estates, and because a single fixed-width key keeps the reconciliation query
    an index scan.
    """

    __tablename__ = "metadata_source_grant"
    __table_args__ = (
        UniqueConstraint("schema_id", "grant_key"),
        Index("ix_metadata_source_grant_org_status", "organization_id", "status"),
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
    grant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    grantee: Mapped[str] = mapped_column(String(255), nullable=False)
    grantee_type: Mapped[str] = mapped_column(String(30), nullable=False)
    privilege: Mapped[str] = mapped_column(String(50), nullable=False)
    object_type: Mapped[str] = mapped_column(String(30), nullable=False)
    object_name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(255))
    is_grantable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
