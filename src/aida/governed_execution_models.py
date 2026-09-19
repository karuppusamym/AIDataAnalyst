"""R11-GQL02: the durable, caller-scoped record of one requested governed execution.

Kept out of `aida.models` for the reason `okf_store_models` and `sql_workspace_models` give:
`models.py` is under concurrent edit, and one additive table is easier to review on its own.

**Why a record at all.** A GraphQL mutation is sent over a connection that can drop after the
source has run the statement and before the caller hears so. A caller that retries cannot know
whether it is asking for a second execution. The record makes the retry safe: the caller names
the request with an idempotency key, the first request with that key claims it, and every later
request with the same key and the same inputs reads the record instead of executing again --
including while the first is still running, and including when its outcome is not known.

**Unknown stays unknown.** A request whose outcome the platform did not learn (the deadline
passed mid-execution, the process died) stays `PENDING`. It is never retried as a new
execution; reconciling it is a person's decision, which is what design section 13B asks for.

**Value-free (INV-6).** Parameters are caller values, so the record holds an HMAC of the
request (`request_fingerprint`, the same `sign_value` the tool execution's own parameter
fingerprint uses) and never the values. Result rows are not retained here or anywhere: a replay
returns the receipt -- status, execution ids, row count -- and not the rows.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aida.models import TimestampMixin
from atlas.platform.db import Base

STATUSES = ("PENDING", "COMPLETED", "REJECTED", "FAILED")


class GovernedExecutionRequest(Base, TimestampMixin):
    """One idempotency key's execution, for one caller."""

    __tablename__ = "governed_execution_request"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "principal_type",
            "principal_id",
            "idempotency_key",
            name="uq_governed_execution_request_caller_key",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'REJECTED', 'FAILED')",
            name="status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: HMAC of the tool version, parameters, row limit and product key: the same key sent
    #: with different inputs is refused rather than answered with another request's receipt.
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Which surface asked, for telemetry and support: `GRAPHQL` today.
    surface: Mapped[str] = mapped_column(String(20), nullable=False)
    tool_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("governed_tool_version.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    context_product_version_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    tool_execution_id: Mapped[UUID | None] = mapped_column()
    query_execution_id: Mapped[UUID | None] = mapped_column()
    row_count: Mapped[int | None] = mapped_column(Integer)
    #: A stable code for a refusal or failure; never a message, never SQL.
    outcome_code: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
