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
table (AT-D2/AT-D5) is populated by `sql_lineage_parser.parse_procedure_lineage`
via `view_lineage_api.py`'s raw-SQL parse endpoint -- a flat, non-procedure-
aware parse with no identity back to a specific `MetadataRoutine` at all
(AT-19 documents this as the reason `PROCEDURE_DEFINITION` unified-lineage
edges cannot carry a `transformation_reference` the way `VIEW_DEFINITION`
edges do). Overloading that same table with statement-ordinal, control-flow,
UNPARSED-marker and routine-identity columns this module's richer parse
needs would either break its existing natural-key uniqueness (AT-D2) and its
existing callers, or require touching `models.py`'s already-declared class
body -- the highest-collision-risk kind of edit for a module under
concurrent edit. A new, dedicated table with a real `routine_id` foreign key
is both safer to add and strictly more capable: `DeepProcedureLineageEdge`
is the identity-bearing procedure lineage table AT-19 wished existed.
"""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
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
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ProcedureToolGenerationRecord(Base, TimestampMixin):
    """Provenance for one N12 "procedure -> governed tool" draft: which
    routine it was generated from, the exact redacted-body hash the
    read-only proof was computed against, and how many statements that proof
    covered. `GovernedToolVersion` (`models.py`) carries no pointer back to
    the routine it came from and this table is deliberately never that
    pointer's replacement -- it is the audit trail proving *this specific*
    draft passed N12's eligibility gate (fully parsed, zero writes, exactly
    one terminal result statement), independent of anything that later
    happens to the tool version itself (edited, republished, deprecated).
    """

    __tablename__ = "procedure_tool_generation_record"
    __table_args__ = (
        Index("ix_procedure_tool_generation_record_routine", "routine_id"),
        Index("ix_procedure_tool_generation_record_tool_version", "tool_version_id"),
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
    tool_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sql_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    statement_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
