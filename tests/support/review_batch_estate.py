"""R11-REV01 test estate: an organization, a datasource, tables, columns and the pending
description-draft reviews the change queue and frozen batches operate on.

Rows are built directly through the ORM with explicit ids, so a 1,000-table estate or a
1,000-column table seeds in a handful of flushes rather than one round trip per row.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AssetDescriptionDraft,
    AuditEvent,
    ColumnDescriptionDraft,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.security_types import SecurityContext

MAKER = "maker@example.com"
CHECKER = "checker@example.com"
OTHER_CHECKER = "second-checker@example.com"

# `AuditEvent.id` is a BIGINT primary key PostgreSQL fills from a sequence; in-memory SQLite
# does not, so ids are assigned here -- the workaround every SQLite suite in this repo uses.
_audit_event_ids = itertools.count(1_000_000)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


def reviewer(
    organization_id: UUID,
    principal: str = CHECKER,
    *,
    principal_type: str = "USER",
    roles: frozenset[str] = frozenset({"Reviewer"}),
    delegator: str | None = None,
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type=principal_type,
        organization_id=organization_id,
        roles=roles,
        active_delegation_id=uuid4() if delegator else None,
        active_delegator_principal_id=delegator,
    )


@dataclass
class Estate:
    organization: Organization
    datasource: DataSource
    schema: MetadataSchema
    clock: datetime = field(default_factory=lambda: datetime(2026, 9, 1, tzinfo=UTC))

    @property
    def organization_id(self) -> UUID:
        return self.organization.id

    def tick(self) -> datetime:
        """Strictly increasing creation times, so queue order is deterministic."""
        self.clock = self.clock + timedelta(seconds=1)
        return self.clock


async def build_estate(session: AsyncSession, *, name: str = "Bank") -> Estate:
    org = Organization(id=uuid4(), name=name, slug=f"{name.lower()}-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Retail",
        code=f"D{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:8]}",
    )
    # One flush per row, parent first: these models carry plain foreign-key columns with no
    # `relationship()`, so a single flush has nothing to order the INSERTs by, and PostgreSQL
    # (unlike this repo's SQLite) enforces the keys -- `tests/test_review_batches_postgres.py`.
    for row in (org, lob, domain, project):
        session.add(row)
        await session.flush()
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"warehouse-{uuid4().hex[:6]}",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="finance", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    return Estate(organization=org, datasource=datasource, schema=schema)


async def add_tables(
    session: AsyncSession, estate: Estate, count: int, *, prefix: str = "table"
) -> list[MetadataTable]:
    tables = [
        MetadataTable(
            id=uuid4(),
            organization_id=estate.organization_id,
            datasource_id=estate.datasource.id,
            schema_id=estate.schema.id,
            name=f"{prefix}_{index:04d}",
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint=f"fp-{prefix}-{index}",
        )
        for index in range(count)
    ]
    session.add_all(tables)
    await session.flush()
    return tables


async def add_columns(
    session: AsyncSession, table: MetadataTable, count: int, *, prefix: str = "col"
) -> list[MetadataColumn]:
    columns = [
        MetadataColumn(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            name=f"{prefix}_{index:04d}",
            ordinal_position=index,
            physical_type="VARCHAR",
            nullable=True,
            status="ACTIVE",
            fingerprint=f"fp-{prefix}-{index}",
        )
        for index in range(count)
    ]
    session.add_all(columns)
    await session.flush()
    return columns


def _scores() -> dict[str, float]:
    return {
        "accuracy_score": 0.8,
        "clarity_score": 0.8,
        "style_score": 0.8,
        "completeness_score": 0.8,
        "overall_score": 0.8,
    }


async def add_table_draft_reviews(
    session: AsyncSession,
    estate: Estate,
    tables: list[MetadataTable],
    *,
    requested_by: str = MAKER,
) -> list[GovernanceReview]:
    """One pending ASSET_DESCRIPTION_DRAFT review per table."""
    reviews: list[GovernanceReview] = []
    drafts: list[AssetDescriptionDraft] = []
    for table in tables:
        draft_id = uuid4()
        review = GovernanceReview(
            id=uuid4(),
            organization_id=estate.organization_id,
            object_type="ASSET_DESCRIPTION_DRAFT",
            object_id=str(draft_id),
            requested_action="PUBLISH_DESCRIPTION",
            status="PENDING",
            requested_by=requested_by,
            created_at=estate.tick(),
        )
        text = f"{table.name} records one row per finance event."
        drafts.append(
            AssetDescriptionDraft(
                id=draft_id,
                organization_id=estate.organization_id,
                table_id=table.id,
                drafted_text=text,
                text_fingerprint=hashlib.sha256(text.encode()).hexdigest(),
                evidence={"column_count": 3, "source_refs": [table.name]},
                status="PENDING_APPROVAL",
                governance_review_id=review.id,
                created_by=requested_by,
                **_scores(),
            )
        )
        reviews.append(review)
    session.add_all(reviews)
    await session.flush()
    session.add_all(drafts)
    await session.flush()
    return reviews


async def add_column_draft_reviews(
    session: AsyncSession,
    estate: Estate,
    table: MetadataTable,
    columns: list[MetadataColumn],
    *,
    requested_by: str = MAKER,
) -> list[GovernanceReview]:
    """One pending COLUMN_DESCRIPTION_DRAFT review per column."""
    reviews: list[GovernanceReview] = []
    drafts: list[ColumnDescriptionDraft] = []
    for column in columns:
        draft_id = uuid4()
        review = GovernanceReview(
            id=uuid4(),
            organization_id=estate.organization_id,
            object_type="COLUMN_DESCRIPTION_DRAFT",
            object_id=str(draft_id),
            requested_action="PUBLISH_DESCRIPTION",
            status="PENDING",
            requested_by=requested_by,
            created_at=estate.tick(),
        )
        text = f"{column.name} holds the {column.ordinal_position}th attribute of the event."
        drafts.append(
            ColumnDescriptionDraft(
                id=draft_id,
                organization_id=estate.organization_id,
                table_id=table.id,
                column_id=column.id,
                drafted_text=text,
                text_fingerprint=hashlib.sha256(text.encode()).hexdigest(),
                evidence={"physical_type": column.physical_type, "nullable": column.nullable},
                status="PENDING_APPROVAL",
                base_description_version=None,
                governance_review_id=review.id,
                created_by=requested_by,
                **_scores(),
            )
        )
        reviews.append(review)
    session.add_all(reviews)
    await session.flush()
    session.add_all(drafts)
    await session.flush()
    return reviews


async def add_bare_review(
    session: AsyncSession,
    estate: Estate,
    object_type: str,
    *,
    requested_by: str = MAKER,
    requested_action: str = "APPROVE",
    object_id: str | None = None,
) -> GovernanceReview:
    """A pending review whose object type the shared read model composes nothing for."""
    review = GovernanceReview(
        id=uuid4(),
        organization_id=estate.organization_id,
        object_type=object_type,
        object_id=object_id or str(uuid4()),
        requested_action=requested_action,
        status="PENDING",
        requested_by=requested_by,
        created_at=estate.tick(),
    )
    session.add(review)
    await session.flush()
    return review
