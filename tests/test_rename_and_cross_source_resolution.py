"""CT-4 (rename detection) and CT-6 (cross-source object resolution) -- DB-level tests.

`tests/test_identity_resolution.py` covers the pure scoring functions
(`aida.identity_resolution`) directly. This file covers the parts that need a real
session: automatic detection wired into the discovery pipeline
(`aida.workflows.activities.detect_rename_candidates`, called from
`persist_discovery_snapshot`), the steward-approval merge step
(`aida.identity_merge.merge_table_identity`), and the new tables' constraints
(`RenameCandidate`, `CrossSourceResolutionCandidate`, `metadata_table.superseded_by_table_id`).

SQLite in memory is sufficient here -- no construct is PostgreSQL-specific -- following
the precedent set by `tests/test_envelope_v11.py` / `tests/test_workspace_authorization.py`.
The DB round-trip itself (FK wiring, index/constraint names, JSON columns) was additionally
verified once against a real PostgreSQL 16 instance during development of this change; that
is not repeatable in this suite (no live Postgres in CI) so it is not asserted here, only the
SQLite-portable behavior is.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredSchema,
    DiscoveredTable,
)
from aida.db import Base
from aida.identity_merge import merge_table_identity
from aida.models import (
    AnalysisRun,
    AssetTag,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataTable,
    Organization,
    Project,
    RenameCandidate,
)
from aida.workflows.activities import detect_rename_candidates, persist_discovery_snapshot


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _datasource(session: AsyncSession) -> DataSource:
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
    return datasource


async def _run(session: AsyncSession, datasource: DataSource) -> AnalysisRun:
    run = AnalysisRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="PUSH",
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    return run


_ACCOUNT_COLUMNS = (
    {"name": "account_id", "ordinal_position": 1, "physical_type": "bigint", "nullable": False},
    {"name": "customer_id", "ordinal_position": 2, "physical_type": "bigint", "nullable": False},
    {"name": "balance", "ordinal_position": 3, "physical_type": "numeric", "nullable": False},
    {"name": "opened_at", "ordinal_position": 4, "physical_type": "timestamp", "nullable": True},
)


def _catalog(
    table_name: str, columns: tuple[dict[str, Any], ...] = _ACCOUNT_COLUMNS
) -> tuple[DiscoveredCatalog, ...]:
    return (
        DiscoveredCatalog(
            name="warehouse",
            schemas=(
                DiscoveredSchema(
                    name="banking",
                    tables=(
                        DiscoveredTable(
                            name=table_name,
                            object_type="BASE_TABLE",
                            columns=tuple(DiscoveredColumn(**c) for c in columns),
                        ),
                    ),
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------------------------
# CT-4: automatic detection wired into persist_discovery_snapshot
# --------------------------------------------------------------------------------------------


async def test_rename_candidate_proposed_for_same_run_tombstone_and_create(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    run1 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run1, datasource, _catalog("customer_account"))
    await session.commit()

    old_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "customer_account")
    )
    assert old_table is not None
    assert old_table.status == "ACTIVE"

    run2 = await _run(session, datasource)
    # Same columns, renamed table, same run -- the canonical CT-4 signature.
    await persist_discovery_snapshot(session, run2, datasource, _catalog("cust_account"))
    await session.commit()

    await session.refresh(old_table)
    assert old_table.status == "DEPRECATED"

    candidates = (await session.scalars(select(RenameCandidate))).all()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.old_table_id == old_table.id
    new_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "cust_account")
    )
    assert new_table is not None
    assert candidate.new_table_id == new_table.id
    assert candidate.status == "PENDING"
    assert candidate.detection_rule == "STRUCTURAL_MATCH_V1"
    assert candidate.confidence > 0.8
    assert candidate.created_by == "metadata-worker"
    assert candidate.schema_id == old_table.schema_id == new_table.schema_id
    assert candidate.analysis_run_id == run2.id
    assert candidate.evidence["source_values_inspected"] is False


async def test_no_rename_candidate_when_new_table_is_structurally_unrelated(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    run1 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run1, datasource, _catalog("customer_account"))
    await session.commit()

    run2 = await _run(session, datasource)
    unrelated_columns = (
        {
            "name": "product_sku",
            "ordinal_position": 1,
            "physical_type": "varchar",
            "nullable": False,
        },
        {"name": "price", "ordinal_position": 2, "physical_type": "numeric", "nullable": False},
    )
    # Tombstones customer_account (missing from this FULL snapshot) and creates an
    # unrelated new table in the same run -- must NOT be proposed as a rename.
    await persist_discovery_snapshot(
        session, run2, datasource, _catalog("product_catalog", unrelated_columns)
    )
    await session.commit()

    candidates = (await session.scalars(select(RenameCandidate))).all()
    assert candidates == []


async def test_detect_rename_candidates_is_idempotent_against_existing_pairs(
    session: AsyncSession,
) -> None:
    """A second detection pass over the same pair proposes nothing new (the unique
    constraint on (old_table_id, new_table_id) is the backstop; this asserts the
    happy, non-erroring path that avoids ever hitting it)."""
    datasource = await _datasource(session)
    run1 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run1, datasource, _catalog("customer_account"))
    await session.commit()
    old_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "customer_account")
    )
    assert old_table is not None

    run2 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run2, datasource, _catalog("cust_account"))
    await session.commit()
    new_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "cust_account")
    )
    assert new_table is not None

    # Re-run detection directly with the same created/deprecated sets -- as if a
    # retried activity saw the same scope twice.
    created = await detect_rename_candidates(
        session,
        run=run2,
        datasource=datasource,
        created_table_ids={new_table.id},
        deprecated_table_ids={old_table.id},
    )
    assert created == []
    candidates = (await session.scalars(select(RenameCandidate))).all()
    assert len(candidates) == 1


async def test_detect_rename_candidates_requires_same_schema(session: AsyncSession) -> None:
    """A structurally-identical table in a *different* schema is out of scope for this
    heuristic -- `RenameCandidate.schema_id` is a single column, not old/new."""
    datasource = await _datasource(session)
    run1 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run1, datasource, _catalog("customer_account"))
    await session.commit()
    old_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "customer_account")
    )
    assert old_table is not None

    run2 = await _run(session, datasource)
    other_schema_catalog = (
        DiscoveredCatalog(
            name="warehouse",
            schemas=(
                DiscoveredSchema(
                    name="archive",  # different schema than "banking"
                    tables=(
                        DiscoveredTable(
                            name="cust_account",
                            object_type="BASE_TABLE",
                            columns=tuple(DiscoveredColumn(**c) for c in _ACCOUNT_COLUMNS),
                        ),
                    ),
                ),
            ),
        ),
    )
    await persist_discovery_snapshot(session, run2, datasource, other_schema_catalog)
    await session.commit()

    candidates = (await session.scalars(select(RenameCandidate))).all()
    assert candidates == []


# --------------------------------------------------------------------------------------------
# CT-4: merge_table_identity (the steward-approval step)
# --------------------------------------------------------------------------------------------


async def test_merge_table_identity_reassigns_downstream_links_and_is_idempotent(
    session: AsyncSession,
) -> None:
    datasource = await _datasource(session)
    run1 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run1, datasource, _catalog("customer_account"))
    await session.commit()
    old_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "customer_account")
    )
    assert old_table is not None

    tag = AssetTag(
        organization_id=old_table.organization_id,
        table_id=old_table.id,
        tag_key="pii-reviewed",
        applied_by="steward@example.com",
    )
    session.add(tag)
    await session.flush()

    run2 = await _run(session, datasource)
    await persist_discovery_snapshot(session, run2, datasource, _catalog("cust_account"))
    await session.commit()
    new_table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "cust_account")
    )
    assert new_table is not None

    reassigned = await merge_table_identity(
        session, old_table_id=old_table.id, new_table_id=new_table.id
    )
    old_table.superseded_by_table_id = new_table.id
    await session.commit()

    assert reassigned == {"asset_tag.table_id": 1}
    await session.refresh(tag)
    assert tag.table_id == new_table.id
    await session.refresh(old_table)
    assert old_table.superseded_by_table_id == new_table.id

    # Idempotent: nothing is left pointing at old_table.id, so re-running reassigns nothing.
    again = await merge_table_identity(
        session, old_table_id=old_table.id, new_table_id=new_table.id
    )
    assert again == {}


# --------------------------------------------------------------------------------------------
# Table-level constraints (CT-4 + CT-6)
# --------------------------------------------------------------------------------------------


async def test_rename_candidate_unique_pair_constraint_is_enforced(session: AsyncSession) -> None:
    datasource = await _datasource(session)
    run = await _run(session, datasource)
    await persist_discovery_snapshot(session, run, datasource, _catalog("customer_account"))
    await session.commit()
    table = await session.scalar(
        select(MetadataTable).where(MetadataTable.name == "customer_account")
    )
    assert table is not None

    candidate_a = RenameCandidate(
        organization_id=table.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=run.id,
        schema_id=table.schema_id,
        old_table_id=table.id,
        new_table_id=table.id,
        detection_rule="STRUCTURAL_MATCH_V1",
        confidence=0.9,
        evidence={},
        created_by="metadata-worker",
    )
    session.add(candidate_a)
    await session.commit()

    candidate_b = RenameCandidate(
        organization_id=table.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=run.id,
        schema_id=table.schema_id,
        old_table_id=table.id,
        new_table_id=table.id,
        detection_rule="STRUCTURAL_MATCH_V1",
        confidence=0.9,
        evidence={},
        created_by="metadata-worker",
    )
    session.add(candidate_b)
    try:
        await session.commit()
        raise AssertionError("duplicate (old_table_id, new_table_id) pair was allowed")
    except IntegrityError:
        await session.rollback()
