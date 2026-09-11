"""Authoritative, versioned ontology definitions. Neo4j is not their authority."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base


class OntologyHead(Base):
    __tablename__ = "ontology_head"
    __table_args__ = (UniqueConstraint("organization_id", "ontology_key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"), nullable=False)
    ontology_key: Mapped[str] = mapped_column(String(100), nullable=False)
    last_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class OntologyVersion(Base):
    __tablename__ = "ontology_version"
    __table_args__ = (
        UniqueConstraint("ontology_id", "version"),
        CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED')", name="ontology_status"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organization.id"), nullable=False)
    ontology_id: Mapped[UUID] = mapped_column(ForeignKey("ontology_head.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    governance_review_id: Mapped[UUID | None] = mapped_column(ForeignKey("governance_review.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
