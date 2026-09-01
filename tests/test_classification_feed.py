"""Authoritative external classification feed integration (module 05, PR-3).

Covers the three layers `aida.classification_feed` is built from:

* the deterministic rule itself, with the evidence the module spec requires
  (rule ID, matched signal);
* the pure conflict-resolution decision -- an external record always wins,
  whatever is on the column today;
* the DB-facing ingestion path, proven against a real (in-memory SQLite)
  database rather than a hand-rolled fake, since it does a join to resolve
  `{schema, table, column}` and writes an append-only evidence ledger.

SQLite in memory is sufficient here -- no construct in `ingest_classification_feed`
is PostgreSQL-specific -- following the precedent set by `tests/test_envelope_v11.py`.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.classification_feed import (
    CLASSIFICATION_SOURCE_EXTERNAL,
    CLASSIFICATION_SOURCE_RULE,
    ExternalClassificationRecord,
    classify_column_name_with_evidence,
    ingest_classification_feed,
    resolve_classification_conflict,
)
from aida.db import Base
from aida.main import app
from aida.models import (
    ClassificationEvidence,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)

# ---------------------------------------------------------------------------
# Deterministic rule: classification + evidence
# ---------------------------------------------------------------------------


def test_pci_token_is_classified_with_evidence() -> None:
    result = classify_column_name_with_evidence("payment_card_number")

    assert result.classification == "PCI"
    assert result.rule_id == "NAME_PATTERN_PCI_V1"
    assert result.matched_signal["matched_token"] == "card_number"  # noqa: S105


def test_pii_token_is_classified_with_evidence() -> None:
    result = classify_column_name_with_evidence("customer_email_address")

    assert result.classification == "PII"
    assert result.rule_id == "NAME_PATTERN_PII_V1"
    assert result.matched_signal["matched_token"] == "email"  # noqa: S105


def test_unmatched_name_is_unclassified_with_evidence() -> None:
    result = classify_column_name_with_evidence("current_balance")

    assert result.classification == "UNCLASSIFIED"
    assert result.rule_id == "NAME_PATTERN_NONE_V1"
    assert result.matched_signal["matched_token"] is None


# ---------------------------------------------------------------------------
# Pure conflict resolution: an external record always wins
# ---------------------------------------------------------------------------


def _external(classification: str = "PII") -> ExternalClassificationRecord:
    return ExternalClassificationRecord(
        schema_name="retail",
        table_name="customer",
        column_name="tax_id",
        classification=classification,
        source="bank-drp-feed",
    )


def test_external_feed_overrides_rule_classification() -> None:
    resolution = resolve_classification_conflict(
        existing_source=CLASSIFICATION_SOURCE_RULE,
        existing_classification="UNCLASSIFIED",
        incoming=_external("PII"),
    )

    assert resolution.final_classification == "PII"
    assert resolution.final_source == CLASSIFICATION_SOURCE_EXTERNAL
    assert resolution.changed is True
    assert resolution.reason == "EXTERNAL_FEED_OVERRIDES_RULE"


def test_reingesting_the_same_external_classification_reaffirms_without_change() -> None:
    resolution = resolve_classification_conflict(
        existing_source=CLASSIFICATION_SOURCE_EXTERNAL,
        existing_classification="PII",
        incoming=_external("PII"),
    )

    assert resolution.changed is False
    assert resolution.reason == "EXTERNAL_FEED_REAFFIRMED"


def test_external_feed_can_update_its_own_earlier_classification() -> None:
    resolution = resolve_classification_conflict(
        existing_source=CLASSIFICATION_SOURCE_EXTERNAL,
        existing_classification="PII",
        incoming=_external("PCI"),
    )

    assert resolution.changed is True
    assert resolution.final_classification == "PCI"
    assert resolution.reason == "EXTERNAL_FEED_UPDATED"


# ---------------------------------------------------------------------------
# DB-facing ingestion: matching, evidence provenance, unmatched reporting
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seeded_datasource_and_column(
    session: AsyncSession,
    *,
    column_name: str = "tax_id",
    initial_classification: str = "UNCLASSIFIED",
    initial_source: str = CLASSIFICATION_SOURCE_RULE,
) -> tuple[DataSource, MetadataColumn]:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    lob = LineOfBusiness(
        organization_id=organization.id, name="Retail", code=f"RTL{uuid4().hex[:4]}"
    )
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Deposits",
        code=f"DEP{uuid4().hex[:4]}",
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="Consumer warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://consumer",
        status="ACTIVE",
    )
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(
        organization_id=organization.id,
        datasource_id=datasource.id,
        name="warehouse",
        fingerprint="c",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=organization.id, catalog_id=catalog.id, name="retail", fingerprint="s"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customer",
        object_type="BASE_TABLE",
        fingerprint="t",
    )
    session.add(table)
    await session.flush()
    column = MetadataColumn(
        organization_id=organization.id,
        table_id=table.id,
        name=column_name,
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        classification=initial_classification,
        classification_source=initial_source,
        fingerprint="col",
    )
    session.add(column)
    await session.flush()
    return datasource, column


async def test_matched_record_overrides_column_and_writes_evidence(
    session: AsyncSession,
) -> None:
    datasource, column = await _seeded_datasource_and_column(session)

    result = await ingest_classification_feed(
        session,
        datasource=datasource,
        records=[
            ExternalClassificationRecord(
                schema_name="retail",
                table_name="customer",
                column_name="tax_id",
                classification="PII",
                source="bank-drp-feed",
                confidence=0.99,
            )
        ],
        created_by="steward@bank.example",
    )
    await session.commit()

    assert result.total == 1
    assert result.matched == 1
    assert result.changed == 1
    assert result.unmatched == ()
    assert str(column.id) in result.changed_column_ids

    await session.refresh(column)
    assert column.classification == "PII"
    assert column.classification_source == CLASSIFICATION_SOURCE_EXTERNAL

    evidence_rows = (
        await session.scalars(
            select(ClassificationEvidence).where(ClassificationEvidence.column_id == column.id)
        )
    ).all()
    assert len(evidence_rows) == 1
    evidence = evidence_rows[0]
    assert evidence.source_type == CLASSIFICATION_SOURCE_EXTERNAL
    assert evidence.is_current is True
    assert evidence.matched_signal["actual_values_inspected"] is False
    assert evidence.matched_signal["external_source"] == "bank-drp-feed"


async def test_unmatched_record_is_reported_and_nothing_is_written(
    session: AsyncSession,
) -> None:
    datasource, _column = await _seeded_datasource_and_column(session)

    result = await ingest_classification_feed(
        session,
        datasource=datasource,
        records=[
            ExternalClassificationRecord(
                schema_name="retail",
                table_name="customer",
                column_name="does_not_exist",
                classification="PII",
                source="bank-drp-feed",
            )
        ],
        created_by="steward@bank.example",
    )

    assert result.matched == 0
    assert result.changed == 0
    assert result.unmatched == ("retail.customer.does_not_exist",)


async def test_reingestion_supersedes_prior_evidence_row(session: AsyncSession) -> None:
    datasource, column = await _seeded_datasource_and_column(session)
    record = ExternalClassificationRecord(
        schema_name="retail",
        table_name="customer",
        column_name="tax_id",
        classification="PII",
        source="bank-drp-feed",
    )

    await ingest_classification_feed(
        session, datasource=datasource, records=[record], created_by="steward@bank.example"
    )
    await session.commit()
    updated_record = ExternalClassificationRecord(
        schema_name="retail",
        table_name="customer",
        column_name="tax_id",
        classification="PCI",
        source="bank-drp-feed",
    )
    second = await ingest_classification_feed(
        session,
        datasource=datasource,
        records=[updated_record],
        created_by="steward@bank.example",
    )
    await session.commit()

    assert second.changed == 1
    await session.refresh(column)
    assert column.classification == "PCI"

    rows = (
        await session.scalars(
            select(ClassificationEvidence)
            .where(ClassificationEvidence.column_id == column.id)
            .order_by(ClassificationEvidence.created_at)
        )
    ).all()
    assert len(rows) == 2
    assert rows[0].is_current is False
    assert rows[0].classification == "PII"
    assert rows[1].is_current is True
    assert rows[1].classification == "PCI"


async def test_rule_classified_column_scoped_to_a_different_datasource_is_not_matched(
    session: AsyncSession,
) -> None:
    datasource, _column = await _seeded_datasource_and_column(session)
    _other_datasource, _other_column = await _seeded_datasource_and_column(session)

    result = await ingest_classification_feed(
        session,
        datasource=datasource,
        records=[
            ExternalClassificationRecord(
                schema_name="retail",
                table_name="customer",
                column_name="tax_id",
                classification="PII",
                source="bank-drp-feed",
            )
        ],
        created_by="steward@bank.example",
    )

    # Exactly one column matches -- the tenant/datasource boundary in the join
    # means the identically-named column on the other datasource is untouched.
    assert result.matched == 1


# ---------------------------------------------------------------------------
# API contract: the ingest endpoint is registered and requires a source + records
# ---------------------------------------------------------------------------


def test_classification_feed_endpoint_is_exposed() -> None:
    paths = app.openapi()["paths"]
    path = "/v1/datasources/{datasource_id}/classification-feed/ingest"
    assert path in paths
    assert "post" in paths[path]
