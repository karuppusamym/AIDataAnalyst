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

| State | `availability` | `definition_sql_redacted` / `body_sql_redacted` | `truncated` |
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
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base
from aida.models import TimestampMixin

#: `definition_sql_redacted` / `body_sql_redacted` holds the literal-redacted form of what
#: the source returned, including an
#: empty string when the object really is empty.
AVAILABLE = "AVAILABLE"

#: The source declined, errored, or the connector does not implement the axis.
#: `definition_sql_redacted` / `body_sql_redacted` is NULL and `unavailable_reason` says why.
UNAVAILABLE = "UNAVAILABLE"

#: Object types `MetadataObjectDescription` accepts. `TABLE` and `COLUMN` are
#: deliberately absent: `metadata_table.source_description` (the 1.0 path) and,
#: since IN-5e, `metadata_column.source_description` each own their comments
#: directly, and two homes for one fact is how they diverge. Column comments
#: lived here only because `models.py` was off-limits to the N1 workstream that
#: added this table -- IN-5e closed that gap once `models.py` ownership allowed it.
#:
#: `ROUTINE` is deliberately absent too, and R11-FP08 kept it that way rather
#: than widening this on its way past. A routine already owns its source comment
#: directly (`MetadataRoutine.source_description`, above), exactly as a table and
#: a column do, so this table has nothing to add for one; and what R11-FP08
#: needed was not a *source* description but an Atlas-authored one, which is a
#: different kind of fact with a different lifecycle and lives on
#: `RoutineDocumentationVersion` at the foot of this module. Recorded here so the
#: scope cut is a decision rather than an omission.
DESCRIBABLE_OBJECT_TYPES = ("CATALOG", "SCHEMA")


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
            "(availability = 'AVAILABLE') = (definition_sql_redacted IS NOT NULL)",
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
    # Literal-redacted, never raw. A view definition is SQL, and SQL carries source
    # values in its literals -- `WHERE ssn = '123-45-6789'` is a source value written in
    # a different syntax, so storing the statement stores the value (INV-6). Redaction
    # matches what the dbt path has always done; briefly, this column did not.
    definition_sql_redacted: Mapped[str | None] = mapped_column(Text)
    # Digest of the *original* text, so "has this definition changed" stays answerable
    # without keeping the thing that changed.
    definition_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # PARSED -> every literal replaced. UNPARSED -> the dialect was not understood and
    # nothing is stored; a statement this parser cannot read is not a licence to keep it.
    redaction_status: Mapped[str] = mapped_column(String(20), default="PARSED", nullable=False)
    # Deterministic prompt-risk verdict, applied at write time. View text is
    # source-controlled and reaches model context during meaning inference and tool
    # generation, which makes it an indirect-injection surface. Screening once on write is
    # cheaper and more complete than screening on every read.
    screening_status: Mapped[str] = mapped_column(String(20), default="CLEAN", nullable=False)
    screening_reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # AR-10: which classifier produced the verdict beside it. Nullable on
    # purpose -- NULL means "screened before this column existed", which is
    # exactly what a row written by the old code is, and
    # `ingest_screening.is_verdict_current` reads NULL as stale. Defaulting it
    # to the current version instead would stamp today's version onto a verdict
    # today's classifier never saw, which is the defect this column exists to
    # remove rather than relocate.
    screening_version: Mapped[str | None] = mapped_column(String(100))
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
        # R11-FP03: `package_name` joins the identity, so a standalone `SCORE(NUMBER)` and the
        # packaged `RISK_PKG.SCORE(NUMBER)` are two routines rather than one overwriting the other.
        UniqueConstraint("schema_id", "package_name", "name", "signature"),
        CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name="availability_state",
        ),
        CheckConstraint(
            "(availability = 'AVAILABLE') = (body_sql_redacted IS NOT NULL)",
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
    #: R11-FP03: the package a member subprogram belongs to; empty for a standalone routine.
    package_name: Mapped[str] = mapped_column(
        String(255), default="", server_default="", nullable=False
    )
    routine_type: Mapped[str] = mapped_column(String(30), nullable=False)
    #: The engine's finer kind beside the portable `routine_type`: SQL Server SCALAR,
    #: INLINE_TABLE or MULTI_STATEMENT_TABLE; BigQuery SCALAR_FUNCTION. NULL where none exists.
    native_subtype: Mapped[str | None] = mapped_column(String(30))
    language: Mapped[str | None] = mapped_column(String(50))
    # See the note on MetadataViewDefinition.definition_sql_redacted. A procedure body is
    # the richest literal-bearing text a source hands over, and the largest
    # indirect-injection surface envelope 1.1 introduced.
    body_sql_redacted: Mapped[str | None] = mapped_column(Text)
    body_fingerprint: Mapped[str | None] = mapped_column(String(64))
    redaction_status: Mapped[str] = mapped_column(String(20), default="PARSED", nullable=False)
    screening_status: Mapped[str] = mapped_column(String(20), default="CLEAN", nullable=False)
    screening_reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # See the note on `MetadataViewDefinition.screening_version`.
    screening_version: Mapped[str | None] = mapped_column(String(100))
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


class MetadataRoutineDefinitionVersion(Base):
    """R11-FP03: one captured definition of a routine, immutable once written.

    `MetadataRoutine` holds the *current* body and overwrites it on every rescan, so the
    definition a lineage edge, a generated tool or an approved description was built from was
    gone the moment the source changed. A version is written when a routine is first captured
    and again whenever its raw-text fingerprint or availability moves -- the same detection that
    records a change signal -- with `change_class` saying whether only literals changed. An
    identical rescan writes nothing. Nothing updates a version; a later definition is a later
    row. Value-free: the stored text is the redacted form, as on the routine itself.
    """

    __tablename__ = "metadata_routine_definition_version"
    __table_args__ = (
        UniqueConstraint("routine_id", "version_number"),
        CheckConstraint(
            "availability IN ('AVAILABLE', 'UNAVAILABLE')",
            name="availability_state",
        ),
        CheckConstraint(
            "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL')",
            name="change_class",
        ),
        CheckConstraint("version_number > 0", name="version_positive"),
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
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sql_redacted: Mapped[str | None] = mapped_column(Text)
    body_fingerprint: Mapped[str | None] = mapped_column(String(64))
    availability: Mapped[str] = mapped_column(String(20), nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(500))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    redaction_status: Mapped[str] = mapped_column(String(20), nullable=False)
    screening_status: Mapped[str] = mapped_column(String(20), nullable=False)
    #: NULL for the first captured version; otherwise what kind of change produced this one.
    change_class: Mapped[str | None] = mapped_column(String(20))
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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

    Catalogs and schemas have no description column in `models.py`, so their
    comments land here. Exactly one of the two foreign keys is set, enforced by
    a check constraint rather than by a bare polymorphic `object_id`, so a
    deleted schema takes its description with it instead of leaving a row
    pointing at nothing.

    IN-5e (2026-09-01): a third foreign key, `column_id`, used to live here for
    the same off-limits-`models.py` reason `metadata_table.source_description`
    already didn't apply to columns. That gap has since closed --
    `metadata_column.source_description` is now a real column, populated by a
    migration backfill from this table's own `object_type = 'COLUMN'` rows
    before they were deleted. `COLUMN` is no longer a valid `object_type` here.

    Source descriptions are *evidence*, never authority: a steward-authored or
    model-proposed description lives in the enrichment tables and outranks this.
    """

    __tablename__ = "metadata_object_description"
    __table_args__ = (
        UniqueConstraint("catalog_id"),
        UniqueConstraint("schema_id"),
        CheckConstraint(
            "object_type IN ('CATALOG', 'SCHEMA')",
            name="object_type_is_describable",
        ),
        CheckConstraint(
            "(CASE WHEN catalog_id IS NULL THEN 0 ELSE 1 END) "
            "+ (CASE WHEN schema_id IS NULL THEN 0 ELSE 1 END) = 1",
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


# ---------------------------------------------------------------------------
# R11-FP08: an Atlas-authored description for a routine.
#
# Tables and views already have the whole description lifecycle -- a draft, an
# append-only versioned store, a review type, a withdrawal and a reinstatement.
# A routine had none of it: `AssetDocumentation` is keyed by `table_id` and a
# routine is not a table, so the only thing the platform could say about a
# procedure was the source's own comment (`MetadataRoutine.source_description`,
# re-derived and overwritten by every rescan).
#
# **What was decided, and what was not.** R11-S4 freezes new governed-artifact
# *families*; the review authorises exactly the narrow extension here, which is
# one more parallel pair plus one draft table in the established shape, sharing
# the two tables that genuinely discriminate on a subject type
# (`GovernanceReview` and `DescriptionWithdrawal`). It is deliberately *not* the
# broad consolidation into one polymorphic description store that R11-S4 defers:
# nothing below is polymorphic, every foreign key names one real parent, and a
# deleted routine takes its description with it rather than leaving a row
# pointing at nothing.
#
# **Why these three live here and not in `aida.models`.** The same reason the
# 1.1 axes above do (see the module docstring): `models.py` is a single
# 5,300-line module under concurrent edit, and this module already owns the
# routine axis -- `MetadataRoutine`, its definition versions and its parameters
# are all declared above, and every signal a routine description stands on is
# read from them. Declaring these against the same `aida.db.Base` registers them
# on the same `MetaData`, so Alembic autogenerate and
# `Base.metadata.create_all` see them exactly as if they were declared there.
# ---------------------------------------------------------------------------


class RoutineDocumentation(Base, TimestampMixin):
    """Identity/pointer row for one routine's Atlas-authored description of record.

    The routine-level counterpart to `models.AssetDocumentation` (tables) and
    `models.ColumnDocumentation` (columns), on the same parent-identity /
    versioned-content split: content lives on the append-only
    `RoutineDocumentationVersion` below, never here, because an `AgentRun`
    grounded on a routine description has to stay replayable against exactly the
    text it saw, which in-place mutation would destroy.

    `MetadataRoutine.source_description` is a *different* thing and stays where
    it is: that is the source system's own comment, overwritten by every
    rediscovery pass. This is authored, reviewed content rediscovery must never
    touch -- the distinction `ColumnDocumentation` already draws against
    `MetadataColumn.source_description`.
    """

    __tablename__ = "routine_documentation"
    __table_args__ = (UniqueConstraint("routine_id", name="uq_routine_documentation_routine_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Denormalized from `MetadataRoutine.datasource_id` so the datasource-scoped
    #: reads (the routine pane, a context product's coverage section) filter
    #: without a second join through `metadata_routine`; `routine_id` is the key.
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    routine_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_routine.id", ondelete="CASCADE"), nullable=False, index=True
    )


class RoutineDocumentationVersion(Base, TimestampMixin):
    """Append-only content history for a `RoutineDocumentation`.

    One row per approved description. The previously `APPROVED` row (if any) is
    flipped to `SUPERSEDED` in the same transaction that inserts the new
    `APPROVED` one -- see
    `routine_description_service.publish_routine_documentation_version` -- and
    never mutated for content. A withdrawal moves it to `WITHDRAWN` instead,
    which a reader must be able to tell from a replacement
    (`aida.description_withdrawal`).

    **`source_definition_version_id` is the one thing this pair has that the
    table and column pairs do not.** A routine has a real immutable
    definition-version table (`MetadataRoutineDefinitionVersion`, above) that a
    view lacks, so a published routine description can *name* the body it was
    written against rather than only carrying a digest of it. Two consequences,
    both load-bearing: drift is detected by comparing a named version id rather
    than re-deriving and re-hashing text (stronger, because a digest collides
    across a retire-and-recapture cycle that produces identical text, and
    cheaper, because it is one integer comparison); and a reinstatement can
    refuse to republish prose about a body that has since moved. Nullable
    because a routine whose body was never captured has no version to name, and
    a description of such a routine is exactly the case that says so.
    """

    __tablename__ = "routine_documentation_version"
    __table_args__ = (
        UniqueConstraint("documentation_id", "version"),
        Index(
            "ix_routine_documentation_version_org_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    documentation_id: Mapped[UUID] = mapped_column(
        ForeignKey("routine_documentation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="APPROVED", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: The captured definition this text describes, when there was one. SET NULL
    #: on delete rather than CASCADE: losing the provenance edge must not delete
    #: a governed description.
    source_definition_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_routine_definition_version.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoutineDescriptionDraft(Base, TimestampMixin):
    """Deterministically drafted routine description; always routed through review.

    The routine-level sibling of `models.AssetDescriptionDraft` (GL-9) and
    `models.ColumnDescriptionDraft`, on the same contract: composed from
    catalog evidence already in this database, evidence-scored, and published
    only by an independent APPROVE on its `GovernanceReview`
    (`semantic_api._decide_routine_description_draft`). No model call anywhere
    on the path. Rejected drafts are retained as negative knowledge, so
    identical text is not proposed again for the same routine.

    **The body is never quoted.** A procedure body is the largest
    indirect-injection surface envelope 1.1 introduced (see the note on
    `MetadataRoutine.body_sql_redacted`), and a description is prose that every
    reader of the routine will read. `drafted_text` therefore says what *state*
    the body is in -- captured, truncated, quarantined, withheld, not captured
    -- and never a fragment of it. `tests/test_routine_description_body_states.py`
    asserts that, per state.

    `base_description_version` is the routine's description version when the
    draft was composed (None when it had none), copied from
    `ColumnDescriptionDraft` rather than from `AssetDescriptionDraft`, which
    lacks it: approval re-checks it, so a draft written against v2 cannot
    silently replace a v3 published since.

    `uq_routine_description_draft_open` allows one open draft per routine, for
    the reason the column index carries: two would split one routine's review
    into two decisions about the same text, and whichever was approved second
    would be refused on the version check anyway.
    """

    __tablename__ = "routine_description_draft"
    __table_args__ = (
        Index("ix_routine_description_draft_org_status", "organization_id", "status"),
        Index(
            "uq_routine_description_draft_open",
            "routine_id",
            unique=True,
            postgresql_where=text("status IN ('DRAFT', 'PENDING_APPROVAL')"),
            sqlite_where=text("status IN ('DRAFT', 'PENDING_APPROVAL')"),
        ),
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
    drafted_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    accuracy_score: Mapped[float] = mapped_column(Float, nullable=False)
    clarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    style_score: Mapped[float] = mapped_column(Float, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    base_description_version: Mapped[int | None] = mapped_column(Integer)
    governance_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("governance_review.id", ondelete="SET NULL"), unique=True
    )
    published_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routine_documentation_version.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
