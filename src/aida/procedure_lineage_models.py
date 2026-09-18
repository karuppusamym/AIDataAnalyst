"""Persistence for N3 (deep, procedure-aware lineage) and N12 (procedure ->
governed tool generation).

**Why these tables live here and not in `aida.models`.** Exactly the same
reason `envelope_models.py` gives for its own tables: `models.py` is a
single, large module under concurrent edit by the ST-05/06/07 module-split
work, and every group in this Wave-2 pass is asked to keep its footprint on
shared files to genuinely additive, narrowly-scoped edits. Declaring these
classes against the same `aida.db.Base` registers them on the same
`MetaData` -- Alembic autogenerate and `Base.metadata.create_all` both see
them exactly as if they had been declared in `models.py` -- while this
module's new tables arrive as one reviewable, isolated file.

**Why a new table rather than reusing `models.ProcedureLineageEdge`.** That
table (AT-D2/AT-D5) was populated by `view_lineage_api.py`'s raw-SQL parse
endpoint -- a flat, non-procedure-aware parse with no identity back to a
specific `MetadataRoutine` at all
(AT-19 documented this as the reason `PROCEDURE_DEFINITION` unified-lineage
edges could not carry a `transformation_reference` the way `VIEW_DEFINITION`
edges do; since 2026-09-11 an edge one routine establishes through this
table does). Overloading that same table with statement-ordinal, control-flow,
UNPARSED-marker and routine-identity columns this module's richer parse
needs would either break its existing natural-key uniqueness (AT-D2) and its
existing callers, or require touching `models.py`'s already-declared class
body -- the highest-collision-risk kind of edit for a module under
concurrent edit. A new, dedicated table with a real `routine_id` foreign key
is both safer to add and strictly more capable: `DeepProcedureLineageEdge`
is the identity-bearing procedure lineage table AT-19 wished existed.

R11-X5 (2026-09-11) removed that raw-SQL parse endpoint, so nothing in this
repository writes `models.ProcedureLineageEdge` any more. The table is kept
deliberately: a deployment's existing rows are still read by the unified
lineage graph, the parsed-edge review queue and the description drafter, and
`DeepProcedureLineageEdge` here is where new procedure lineage lands.

**A third table, 2026-09-17 (R11-FP01): `TriggerLineageEdge`.** Trigger lineage
is not procedure lineage wearing a different hat, and the same argument this
docstring already makes against overloading `models.ProcedureLineageEdge`
applies to overloading `deep_procedure_lineage_edge` with it. `routine_id` there
is NOT NULL and is the table's identity: `footprint_gaps` counts distinct
routines through it, the lineage agent decides whether a routine has been parsed
by its presence, and `persist_routine_edges` replaces a routine's rows by it. A
SQL Server trigger has no routine at all, so its edges would need that column
NULL -- silently dropping them out of every one of those counts -- while a
PostgreSQL trigger's edges come from a routine that the agent must still be free
to parse in its own right, so filing them under its id would make the routine
look done. A dedicated table with a real `trigger_id` keeps both facts straight,
and `via_routine` (already there for R11-FP07) says which routine's body a
PostgreSQL trigger's edge was read from.

**Its coverage record, 2026-09-17: `TriggerParseCoverage`**, `RoutineParseCoverage`
mirrored onto the trigger axis for the same reason the edge table is separate.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base
from aida.models import TimestampMixin


class DeepProcedureLineageEdge(Base, TimestampMixin):
    """One column-level (or, for an `UNPARSED` statement, statement-level)
    lineage fact extracted by `procedure_lineage.parse_procedure_lineage`
    from one `MetadataRoutine`'s body -- see that module's docstring for the
    extraction algorithm and its explicit, code-derived limitations.
    """

    __tablename__ = "deep_procedure_lineage_edge"
    __table_args__ = (
        Index("ix_deep_procedure_lineage_edge_org_target", "organization_id", "target_table_id"),
        Index("ix_deep_procedure_lineage_edge_datasource", "datasource_id"),
        Index("ix_deep_procedure_lineage_edge_routine", "routine_id"),
        # Mirrors AT-D2's `uq_procedure_lineage_edge_natural_key`, extended
        # with `routine_id` (this table's edges are routine-identity-aware,
        # unlike `procedure_lineage_edge`) and `statement_ordinal` (the same
        # source->target pair can legitimately recur at different ordinals
        # within one procedure -- e.g. a temp table read twice) plus
        # `via_temp_table` (a direct hop and its own synthesised transitive
        # edge share every other column but must not collide).
        UniqueConstraint(
            "datasource_id",
            "routine_id",
            "statement_ordinal",
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "transformation_type",
            "via_temp_table",
            name="uq_deep_procedure_lineage_edge_natural_key",
        ),
        # ADR-0026's review lifecycle -- see `models.OpenLineageTableEdge`.
        Index("ix_deep_procedure_lineage_edge_review_status", "review_status"),
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
    statement_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_table: Mapped[str] = mapped_column(String(500), nullable=False)
    source_column: Mapped[str] = mapped_column(String(255), nullable=False)
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    target_column: Mapped[str] = mapped_column(String(255), nullable=False)
    # Real, typed signal for whether `source_table` is an actual resolved
    # name, mirroring `sql_lineage_parser.LineageEdge.source_resolved`
    # (AT-D2 defect 3) -- never inferred by string-comparing `source_table`
    # against a cosmetic sentinel like `"UNRESOLVED"`.
    source_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    source_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    target_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    target_column_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_column.id", ondelete="SET NULL"), index=True
    )
    # DIRECT / DERIVED / AGGREGATED / FILTERED / TABLE_STAR (matching
    # `sql_lineage_parser.TransformationType` exactly) or `UNPARSED` (this
    # module's own addition -- INV-9/AT-C4's explicit-degradation marker,
    # never a silently dropped statement).
    transformation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    is_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Source or target is a temp table (`#t`/`##t`) or table variable
    # (`@t`)/`SELECT ... INTO` target local to this procedure body -- not a
    # persisted catalog table the outside world can see.
    is_intermediate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    control_flow_context: Mapped[str | None] = mapped_column(String(30))
    unparsed_reason: Mapped[str | None] = mapped_column(String(400))
    # Set only on a synthesised transitive edge (temp-table hop
    # propagation): the intermediate name this source->target link was
    # resolved *through*. NULL for every direct, single-statement edge.
    via_temp_table: Mapped[str | None] = mapped_column(String(500))
    # R11-FP07: the called routine an edge was read from (`aida.routine_call_descent`);
    # NULL for an edge from the routine's own statements.
    via_routine: Mapped[str | None] = mapped_column(String(500))
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ADR-0026's review lifecycle: the same six columns the P1-05 edge tables
    # carry, added on 2026-09-11 (migration d81f5a2c9e47) so an edge here can
    # wait for a person -- the lineage agent (ADR-0029) writes only PROPOSED
    # rows. `created_by` is the maker the per-edge queue's maker-checker
    # compares; NULL on a row written before the column existed.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("deep_procedure_lineage_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


class TriggerLineageEdge(Base, TimestampMixin):
    """R11-FP01: one lineage fact read out of one trigger's body.

    Same shape and same vocabulary as `DeepProcedureLineageEdge` -- the parse is
    literally the same one (`procedure_lineage.parse_trigger_lineage` is
    `parse_procedure_lineage` with the firing row bound), so an `UNPARSED` marker
    means here exactly what it means there -- with the identity changed and one
    thing added.

    **`trigger_id` is the identity, and `routine_id` is nullable on purpose.**
    A SQL Server or Oracle trigger carries its own body and has no routine, so
    the column is NULL. A PostgreSQL trigger has no body at all: the code lives
    in the function its `action_routine` names, discovered on the routine axis,
    and that function's id goes here so a reader can see which body was actually
    read. It is not the edge's owner -- the trigger is -- which is why this is a
    table of its own; see the module docstring.

    **The source of a firing-row edge is the firing table**, resolved before any
    edge was built, because the firing table's name is nowhere in the body text.
    An engine whose firing-row reference could not be bound gets an `UNPARSED`
    marker carrying `UNRESOLVED_TRIGGER_SUBJECT` instead of edges that quietly
    claim an unknown source.
    """

    __tablename__ = "trigger_lineage_edge"
    __table_args__ = (
        Index("ix_trigger_lineage_edge_org_target", "organization_id", "target_table_id"),
        Index("ix_trigger_lineage_edge_datasource", "datasource_id"),
        Index("ix_trigger_lineage_edge_trigger", "trigger_id"),
        # `deep_procedure_lineage_edge`'s natural key with `trigger_id` in place
        # of `routine_id`, for the same reasons that one lists each part.
        UniqueConstraint(
            "datasource_id",
            "trigger_id",
            "statement_ordinal",
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "transformation_type",
            "via_temp_table",
            name="uq_trigger_lineage_edge_natural_key",
        ),
        Index("ix_trigger_lineage_edge_review_status", "review_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_trigger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The routine whose body this edge was read from -- PostgreSQL's
    #: `action_routine`. NULL on an engine whose trigger carries its own body.
    routine_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_routine.id", ondelete="SET NULL"), index=True
    )
    statement_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_table: Mapped[str] = mapped_column(String(500), nullable=False)
    source_column: Mapped[str] = mapped_column(String(255), nullable=False)
    target_table: Mapped[str] = mapped_column(String(500), nullable=False)
    target_column: Mapped[str] = mapped_column(String(255), nullable=False)
    source_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    target_table_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_table.id", ondelete="SET NULL"), index=True
    )
    transformation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    is_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_intermediate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    control_flow_context: Mapped[str | None] = mapped_column(String(30))
    unparsed_reason: Mapped[str | None] = mapped_column(String(400))
    via_temp_table: Mapped[str | None] = mapped_column(String(500))
    via_routine: Mapped[str | None] = mapped_column(String(500))
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ADR-0026's review lifecycle, exactly as the routine table carries it: an
    # agent writes PROPOSED, only ACTIVE steers retrieval and tool generation,
    # and an UNPARSED marker records a gap rather than an edge so it is never put
    # in front of a reviewer.
    review_status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE", nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    previous_edge_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trigger_lineage_edge.id", ondelete="SET NULL")
    )
    created_by: Mapped[str | None] = mapped_column(String(255))


class RoutineParseCoverage(Base, TimestampMixin):
    """How completely one routine's body was understood, per object.

    **Why this table exists.** `ProcedureParseResult.is_fully_parsed` and
    `.is_read_only` are the platform's only "every branch accounted for"
    signals, and they lived entirely in memory: they reached the parse
    endpoint's response and the agent's ledger entry, and then were gone. No
    column recorded them, so "was this routine fully understood?" had to be
    re-derived by scanning `deep_procedure_lineage_edge` for `UNPARSED` rows --
    which answers a subtly different question. A routine whose parse produced
    no edges at all, or whose markers were replaced by a later re-parse under
    review mode, reads the same as one that was fully understood.

    Finding F06.4 (review 2026-09-16) is exactly that risk stated as a rule:
    never label every path understood merely because an object was inventoried.
    This is where the answer is kept, so the engine capability matrix and the
    footprint gap register can both report coverage without re-deriving it.

    **What it does not duplicate.** `unparsed_reason` is already persisted per
    edge, and stays there: that is where a reason belongs, beside the statement
    it describes. This row carries only the distinct
    `procedure_lineage.UnparsedReason` *prefixes* the body produced -- a sorted,
    comma-joined set of codes with no per-statement detail and no suffix, so it
    is a summary of that column rather than a second copy of it, and cannot
    carry the callee name or parse-error text a suffix can (INV-6).

    One row per routine, replaced on each re-parse: this is a measurement of
    the body as last read, not an append-only history. The definition history
    that *is* append-only is `metadata_routine_definition_version`.
    """

    __tablename__ = "routine_parse_coverage"
    __table_args__ = (
        # One measurement per routine. A re-parse updates it in place, so a
        # reader never has to work out which of several rows is current.
        UniqueConstraint(
            "datasource_id", "routine_id", name="uq_routine_parse_coverage_routine"
        ),
        Index("ix_routine_parse_coverage_org_completed", "organization_id", "parse_completed"),
        # The gap register's own question: which routines in this source are
        # not fully understood?
        Index("ix_routine_parse_coverage_datasource", "datasource_id", "parse_completed"),
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
    #: `ProcedureParseResult.is_fully_parsed`: every statement chunk resolved to
    #: a concrete shape or was recognised as genuinely lineage-free. Stored as
    #: the boolean it is -- `capability_states.parse_coverage_state` renders it
    #: as SUPPORTED/PARTIAL at the reporting boundary, and nothing writes a
    #: state string here.
    parse_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: `ProcedureParseResult.is_read_only`: fully parsed *and* proven to touch
    #: no write statement. Never inferred from an empty edge list.
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    statement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: How many of those statements ended in an UNPARSED marker. Zero exactly
    #: when `parse_completed` is true, which is the invariant that makes the
    #: pair readable without consulting the edge table.
    unparsed_statement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The distinct `UnparsedReason` codes this body produced, sorted and
    #: comma-joined. Empty string for a body with none; never NULL, so "no
    #: reasons" and "not recorded" do not read alike.
    unparsed_reason_codes: Mapped[str] = mapped_column(
        String(400), nullable=False, default="", server_default=""
    )
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The positional precision the unparsed statements above are located to.
    #: `STATEMENT_ORDINAL` today, and recorded per row rather than only in the
    #: published matrix so a consumer reading one coverage record knows how
    #: precisely it can point at the source -- see the engine capability
    #: matrix's source-mapping record for why a character range is not offered.
    source_mapping_granularity: Mapped[str] = mapped_column(
        String(40), nullable=False, default="STATEMENT_ORDINAL",
        server_default="STATEMENT_ORDINAL",
    )
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The principal or agent whose parse produced this measurement.
    measured_by: Mapped[str | None] = mapped_column(String(255))


class TriggerParseCoverage(Base, TimestampMixin):
    """How completely one trigger's body was understood, per trigger.

    `RoutineParseCoverage`, column for column, with the identity swapped the way
    `TriggerLineageEdge` swaps it -- and for the same reason that table exists:
    before this, "was this trigger fully understood?" could only be re-derived
    from `trigger_lineage_edge`, and a trigger whose body writes nothing (a
    PostgreSQL function that only `RETURN NEW`s) left no row there at all, so a
    fully read trigger and one nobody had looked at read alike. The gap register
    counted every such trigger as waiting on the lineage agent forever.

    **`trigger_id` is the identity; `routine_id` is nullable**, exactly as on the
    edge table: NULL for an engine whose trigger carries its own body, and on
    PostgreSQL the function `action_routine` named, whose body was what was
    actually read. It is also what makes a trigger's lineage re-examinable when
    that function changes and the trigger row does not: the routine axis records
    the change as a `ROUTINE` change signal, and this column is the join from that
    signal to every trigger whose measurement it makes stale (see
    `lineage_agent._trigger_lineage`). NULL on PostgreSQL as well when the
    function could not be reached (not captured, or ambiguous) -- the edge table's
    marker says which -- so a later capture of it is also noticed.

    Same value-freedom as the routine row: reason *codes* only, and no column that
    could hold a statement (INV-6).
    """

    __tablename__ = "trigger_parse_coverage"
    __table_args__ = (
        UniqueConstraint(
            "datasource_id", "trigger_id", name="uq_trigger_parse_coverage_trigger"
        ),
        Index("ix_trigger_parse_coverage_org_completed", "organization_id", "parse_completed"),
        Index("ix_trigger_parse_coverage_datasource", "datasource_id", "parse_completed"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_trigger.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The routine whose body was read -- PostgreSQL's `action_routine`. NULL when
    #: the trigger carries its own body, or when that routine could not be reached.
    routine_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("metadata_routine.id", ondelete="SET NULL"), index=True
    )
    parse_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    statement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unparsed_statement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unparsed_reason_codes: Mapped[str] = mapped_column(
        String(400), nullable=False, default="", server_default=""
    )
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[str] = mapped_column(String(30), nullable=False)
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_mapping_granularity: Mapped[str] = mapped_column(
        String(40), nullable=False, default="STATEMENT_ORDINAL",
        server_default="STATEMENT_ORDINAL",
    )
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    measured_by: Mapped[str | None] = mapped_column(String(255))
