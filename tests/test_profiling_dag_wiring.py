"""Wiring PR-1/PR-3 into the actual analysis DAG (`aida.workflows.activities`).

Two internal seams are proven here against real behaviour, not just read:

* `_get_or_create_column` -- discovery-time classification -- writes a
  `classification_evidence` row for every rule-based decision, and never lets
  rediscovery's rule inference silently overwrite a column an authoritative
  feed already spoke for (module 05 sec 9 exit condition, PR-3).
* `_infer_and_persist_key_candidates` -- the DAG's "infer keys" stage run
  from `finalize_profile_tasks` -- derives `key_inference_candidate` rows
  purely from `table_profile`/`column_profile` statistics, skips a column set
  a declared PRIMARY KEY already covers, and never re-proposes a candidate
  already on record from an earlier run (PR-1).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.classification_feed import CLASSIFICATION_SOURCE_EXTERNAL, CLASSIFICATION_SOURCE_RULE
from aida.connectors.base import DiscoveredColumn
from aida.db import Base
from aida.main import app
from aida.models import (
    AnalysisRun,
    ClassificationEvidence,
    ColumnProfile,
    DataSource,
    KeyInferenceCandidate,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    Organization,
    TableProfile,
)
from aida.schemas import KeyInferenceCandidateDecision
from aida.workflows.activities import (
    ChangeTracker,
    _get_or_create_column,
    _infer_and_persist_key_candidates,
)

# ---------------------------------------------------------------------------
# _get_or_create_column: rule-classification evidence + external-feed protection
# ---------------------------------------------------------------------------


@dataclass
class _EvidenceSession:
    """Mirrors the lookup-then-mutate shape `_get_or_create_column` uses:
    one `scalar()` to find (or miss) the existing column, then `execute()`
    (superseding prior evidence) and `add()` for whatever it writes."""

    existing: Any | None = None
    added: list[Any] = field(default_factory=list)
    executed: list[Any] = field(default_factory=list)

    async def scalar(self, _statement: object) -> Any | None:
        return self.existing

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)


def _sample_datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        name="Warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )


def _discovered(name: str) -> DiscoveredColumn:
    return DiscoveredColumn(name=name, ordinal_position=1, physical_type="varchar", nullable=True)


async def test_new_column_gets_rule_classification_and_evidence() -> None:
    datasource = _sample_datasource()
    table = MetadataTable(id=uuid4(), organization_id=datasource.organization_id)
    session = _EvidenceSession(existing=None)
    tracker = ChangeTracker()

    column = await _get_or_create_column(
        session, datasource, table, _discovered("customer_email_address"), tracker
    )

    assert column.classification == "PII"
    assert column.classification_source == CLASSIFICATION_SOURCE_RULE
    assert len(session.added) == 2  # the column itself, plus one evidence row
    evidence = next(item for item in session.added if isinstance(item, ClassificationEvidence))
    assert evidence.classification == "PII"
    assert evidence.source_type == CLASSIFICATION_SOURCE_RULE
    assert evidence.rule_id == "NAME_PATTERN_PII_V1"
    assert evidence.is_current is True
    assert evidence.column_id == column.id
    assert evidence.matched_signal["actual_values_inspected"] is False


async def test_rediscovery_refines_an_unclassified_column() -> None:
    datasource = _sample_datasource()
    table = MetadataTable(id=uuid4(), organization_id=datasource.organization_id)
    existing = MetadataColumn(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=table.id,
        name="tax_id",
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        classification="UNCLASSIFIED",
        classification_source=CLASSIFICATION_SOURCE_RULE,
        fingerprint="old",
    )
    session = _EvidenceSession(existing=existing)
    tracker = ChangeTracker()

    column = await _get_or_create_column(session, datasource, table, _discovered("tax_id"), tracker)

    assert column is existing
    assert column.classification == "PII"
    evidence_rows = [item for item in session.added if isinstance(item, ClassificationEvidence)]
    assert len(evidence_rows) == 1
    assert evidence_rows[0].rule_id == "NAME_PATTERN_PII_V1"


async def test_rediscovery_never_overwrites_an_externally_authoritative_column() -> None:
    """module 05 sec 9 exit condition: once EXTERNAL_AUTHORITATIVE, rediscovery
    must never let rule-based inference silently reclassify the column again --
    even in the edge case where the feed's own classification was UNCLASSIFIED."""
    datasource = _sample_datasource()
    table = MetadataTable(id=uuid4(), organization_id=datasource.organization_id)
    existing = MetadataColumn(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=table.id,
        name="customer_email_address",
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        classification="UNCLASSIFIED",
        classification_source=CLASSIFICATION_SOURCE_EXTERNAL,
        fingerprint="old",
    )
    session = _EvidenceSession(existing=existing)
    tracker = ChangeTracker()

    column = await _get_or_create_column(
        session, datasource, table, _discovered("customer_email_address"), tracker
    )

    assert column.classification == "UNCLASSIFIED"
    assert column.classification_source == CLASSIFICATION_SOURCE_EXTERNAL
    assert session.added == []  # no evidence row appended; nothing was re-decided


# ---------------------------------------------------------------------------
# _infer_and_persist_key_candidates: derives candidates from profile stats
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


async def _seeded_profile(
    session: AsyncSession,
    *,
    row_count: int = 1000,
    columns: list[tuple[str, int, int]],  # (name, null_count, approx_distinct)
    primary_key_columns: list[str] | None = None,
) -> tuple[AnalysisRun, TableProfile]:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    datasource = DataSource(
        organization_id=organization.id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="Warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()
    table = MetadataTable(
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=uuid4(),
        name="account",
        object_type="BASE_TABLE",
        fingerprint="t",
    )
    session.add(table)
    await session.flush()
    run = AnalysisRun(
        organization_id=organization.id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="PROFILING",
    )
    session.add(run)
    await session.flush()
    profile = TableProfile(
        organization_id=organization.id,
        analysis_run_id=run.id,
        datasource_id=datasource.id,
        table_id=table.id,
        row_count_estimate=row_count,
        sampled_row_count=row_count,
    )
    session.add(profile)
    await session.flush()
    for index, (name, null_count, distinct) in enumerate(columns):
        column = MetadataColumn(
            organization_id=organization.id,
            table_id=table.id,
            name=name,
            ordinal_position=index,
            physical_type="varchar",
            nullable=null_count > 0,
            classification="UNCLASSIFIED",
            fingerprint=f"col-{name}",
        )
        session.add(column)
        await session.flush()
        session.add(
            ColumnProfile(
                organization_id=organization.id,
                table_profile_id=profile.id,
                column_id=column.id,
                null_count=null_count,
                non_null_count=row_count - null_count,
                approximate_distinct_count=distinct,
            )
        )
    if primary_key_columns:
        session.add(
            MetadataConstraint(
                organization_id=organization.id,
                datasource_id=datasource.id,
                table_id=table.id,
                name="pk_account",
                constraint_type="PRIMARY_KEY",
                columns=primary_key_columns,
                fingerprint="pk",
            )
        )
    await session.flush()
    return run, profile


async def test_key_candidates_are_derived_from_profile_statistics(
    session: AsyncSession,
) -> None:
    run, profile = await _seeded_profile(
        session,
        row_count=1000,
        columns=[("account_id", 0, 999), ("balance", 0, 40)],
    )

    created = await _infer_and_persist_key_candidates(session, run)
    await session.commit()

    assert created == 1
    rows = (
        await session.scalars(
            select(KeyInferenceCandidate).where(KeyInferenceCandidate.table_id == profile.table_id)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].column_names == ["account_id"]
    assert rows[0].evidence["actual_values_inspected"] is False
    assert rows[0].status == "PENDING"


async def test_declared_primary_key_is_not_reproposed_by_the_dag_step(
    session: AsyncSession,
) -> None:
    run, profile = await _seeded_profile(
        session,
        row_count=1000,
        columns=[("account_id", 0, 999)],
        primary_key_columns=["account_id"],
    )

    created = await _infer_and_persist_key_candidates(session, run)

    assert created == 0
    rows = (
        await session.scalars(
            select(KeyInferenceCandidate).where(KeyInferenceCandidate.table_id == profile.table_id)
        )
    ).all()
    assert rows == []


async def test_rerunning_finalize_never_duplicates_an_existing_candidate(
    session: AsyncSession,
) -> None:
    run, profile = await _seeded_profile(
        session,
        row_count=1000,
        columns=[("account_id", 0, 999)],
    )

    first = await _infer_and_persist_key_candidates(session, run)
    await session.commit()
    second = await _infer_and_persist_key_candidates(session, run)
    await session.commit()

    assert first == 1
    assert second == 0
    rows = (
        await session.scalars(
            select(KeyInferenceCandidate).where(KeyInferenceCandidate.table_id == profile.table_id)
        )
    ).all()
    assert len(rows) == 1


async def test_table_with_no_near_unique_column_gets_no_candidate(
    session: AsyncSession,
) -> None:
    run, profile = await _seeded_profile(
        session,
        row_count=1000,
        columns=[("status", 0, 3)],
    )

    created = await _infer_and_persist_key_candidates(session, run)

    assert created == 0


# ---------------------------------------------------------------------------
# API contract: key-candidate endpoints, decision validation
# ---------------------------------------------------------------------------


def test_key_candidate_endpoints_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "post" in paths["/v1/datasources/{datasource_id}/key-candidates/discover"]
    assert "get" in paths["/v1/datasources/{datasource_id}/key-candidates"]
    assert "post" in paths["/v1/key-candidates/{candidate_id}/decision"]


def test_key_candidate_rejection_requires_a_reason() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="reason is required"):
        KeyInferenceCandidateDecision(decision="REJECT")

    # Approval never requires one.
    KeyInferenceCandidateDecision(decision="APPROVE")
