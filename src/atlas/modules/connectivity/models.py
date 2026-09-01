"""connectivity -- PRIVATE. SQLAlchemy models in this module's own schema
(`connectivity`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).

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
separate pass.

Owned tables (per Sec.4's register: "datasources, connection configs,
capability declarations, certification runs"):

* `DataSource` -- the connector registration itself: connection config
  (`connector_type`, `dialect`, `network_zone`, `credential_reference`),
  and declared capabilities (`capabilities` JSON).
* `ConnectorCertificationRun` -- immutable, attributable connector
  conformance evidence for one source.

Everything else in the old `aida.models` neighborhood that touches
lineage-via-connector (dbt, OpenLineage, BI) is explicitly NOT this
module's domain -- per `04-module-decomposition.md` Sec.9, "dbt is a
lineage source, not its own domain", and the same reasoning applies to
OpenLineage and BI imports; they stay in `aida.models` pending module 09
(lineage)'s own extraction pass.
"""

from __future__ import annotations

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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from atlas.platform.db import Base, TimestampMixin


class DataSource(Base, TimestampMixin):
    __tablename__ = "datasource"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    line_of_business_id: Mapped[UUID] = mapped_column(
        ForeignKey("line_of_business.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    data_domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_domain.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    dialect: Mapped[str] = mapped_column(String(50), nullable=False)
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    network_zone: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    credential_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="REGISTERED", nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ConnectorCertificationRun(Base, TimestampMixin):
    """Immutable, attributable connector conformance evidence for one source."""

    __tablename__ = "connector_certification_run"
    __table_args__ = (
        Index("ix_connector_cert_source_created", "datasource_id", "created_at"),
        Index("ix_connector_cert_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connector_type: Mapped[str] = mapped_column(String(50), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(50), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
