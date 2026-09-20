"""R11-SQL01: the receipt a reviewed SQL statement runs on -- one additive table.

Kept out of `aida.models` for the reason `okf_store_models` gives: `models.py` is under concurrent
edit, and one new table for one new capability is easier to review as its own file registered on
the same `Base`.

**What a receipt is.** A person drafts SQL -- generated from a question without running it, or
pasted -- and the query gateway validates it without executing it. A valid statement gets a
receipt: proof that *this* statement, with *this* row limit, under *this* context product version
and workspace, passed validation for *this* caller, until it expires. Run presents the statement
again with the receipt; the gateway then re-authorizes and re-validates it in full, so an earlier
receipt never outlives a revoked grant, a changed definition or a narrowed product.

**No statement text (INV-6, ADR-0014).** Pasted SQL carries whatever literals its author typed, so
the table keeps a *keyed* digest of the exact statement and its bindings (`statement_digest`) and
only the *redacted* shape for display (`redacted_sql`, from
`aida.sql_redaction.redact_for_storage`, or nothing when redaction could not guarantee it). The
caller holds the text and sends it back at
Run; an edit changes the digest, which is what makes "edited SQL requires revalidation" true by
construction rather than by a flag someone has to remember to clear.

**Runs once.** `status` moves VALIDATED -> EXECUTING -> EXECUTED or FAILED, and the first move is
a conditional update, so two Run requests racing on one receipt execute at most one statement;
the loser is told which execution the receipt produced.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aida.models import TimestampMixin
from atlas.platform.db import Base

ORIGINS = ("GENERATED", "PASTED")
STATUSES = ("VALIDATED", "EXECUTING", "EXECUTED", "FAILED")


class SqlDraftReceipt(Base, TimestampMixin):
    """One validated SQL draft, runnable once by the caller who validated it, until it expires."""

    __tablename__ = "sql_draft_receipt"
    __table_args__ = (
        CheckConstraint("origin IN ('GENERATED', 'PASTED')", name="origin"),
        CheckConstraint(
            "status IN ('VALIDATED', 'EXECUTING', 'EXECUTED', 'FAILED')",
            name="status",
        ),
        Index("ix_sql_draft_receipt_principal", "organization_id", "principal_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="VALIDATED")
    #: Keyed digest (the deployment signer's `sign_value`) of the exact statement text and every
    #: binding a Run must repeat. Keyed because it is stored beside `redacted_sql`: an unkeyed
    #: hash would confirm a guessed literal. Rows before that change hold a bare sha256.
    statement_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The value-free shape, for display only; None when redaction could not guarantee one.
    redacted_sql: Mapped[str | None] = mapped_column(Text)
    redaction_status: Mapped[str] = mapped_column(String(20), nullable=False)
    context_product_version_id: Mapped[UUID | None] = mapped_column()
    workspace_id: Mapped[UUID | None] = mapped_column()
    max_rows: Mapped[int | None] = mapped_column(Integer)
    applied_row_limit: Mapped[int | None] = mapped_column(Integer)
    referenced_tables: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Validation finding codes -- the warnings a valid statement still carried.
    finding_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: The dry-run estimate: plan cost, kind, rows and bytes. Numbers, never values.
    estimate: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: The generation-only Ask run a GENERATED draft came from.
    agent_run_id: Mapped[UUID | None] = mapped_column()
    query_execution_id: Mapped[UUID | None] = mapped_column()
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
