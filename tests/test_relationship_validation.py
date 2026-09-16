"""R11-FP06: a proposed join is validated before it can be approved.

* Matching names and types never approve a join on their own. The single, bulk and composite
  decisions refuse it, and the candidate stays pending.
* A match between two keys, or on a bare ``id``, is not evidence either: any table carries one.
* An approval keeps the validation it rested on. The read compares today's validation with that
  record, so a key dropped since approval shows as lost corroboration.
* Cardinality, direction, optionality and observation bounds come from declared constraints,
  approved keys and value-free profile counts. Nothing queries the source.
* Composite-key discovery persists on a real session: four NOT NULL columns were never set, which
  only the in-memory test double tolerated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.composite_key_api import (
    decide_composite_key_candidate,
    discover_composite_key_candidates,
)
from aida.config import Settings
from aida.db import Base
from aida.intelligence_api import (
    bulk_decide_relationship_candidates,
    decide_composite_relationship_candidate,
    decide_relationship_candidate,
)
from aida.models import (
    ColumnProfile,
    CompositeKeyCandidate,
    DataSource,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    Organization,
    OutboxEvent,
    RelationshipCandidate,
    RelationshipCandidateGroup,
    RelationshipCandidateGroupMember,
    TableProfile,
)
from aida.relationship_validation import (
    CORROBORATED,
    NAME_MATCH_ONLY,
    NAME_MATCH_ONLY_CODE,
    ColumnFacts,
    DeclaredForeignKey,
    ProfileBounds,
    RelationshipFacts,
    RelationshipValidation,
    TableFacts,
    assess_relationship,
    validation_drift,
    with_recorded_validation,
)
from aida.relationship_validation_api import get_relationship_candidate_validation
from aida.schemas import (
    CompositeKeyCandidateDecision,
    RelationshipCandidateBulkDecisionRequest,
    RelationshipCandidateDecision,
)
from tests.test_relationship_intelligence_review import (
    _context,
    _datasource,
    _domain,
    _lob,
    _org,
    _project,
    _table_with_column,
)

SOURCE_TABLE, TARGET_TABLE = uuid4(), uuid4()


def _col(
    name: str,
    *,
    nullable: bool = False,
    physical_type: str = "INTEGER",
    nulls: int | None = None,
    non_null: int | None = None,
    distinct: int | None = None,
) -> ColumnFacts:
    return ColumnFacts(
        column_id=uuid4(),
        name=name,
        physical_type=physical_type,
        nullable=nullable,
        null_count=nulls,
        non_null_count=non_null,
        approximate_distinct_count=distinct,
    )


def _facts(
    source: ColumnFacts,
    target: ColumnFacts,
    *,
    source_table: TableFacts | None = None,
    target_table: TableFacts | None = None,
    rule: str = "EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
    observed: int = 0,
) -> RelationshipFacts:
    return RelationshipFacts(
        source=source_table or TableFacts(SOURCE_TABLE),
        target=target_table or TableFacts(TARGET_TABLE),
        pairs=((source, target),),
        detection_rule=rule,
        observed_join_count=observed,
    )


def _classes(validation: RelationshipValidation) -> dict[str, bool]:
    return {item.name: item.corroborating for item in validation.evidence_classes}


def _keyed(*columns: str) -> TableFacts:
    return TableFacts(TARGET_TABLE, declared_keys=(frozenset(columns),))


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------


def test_a_name_and_type_match_alone_is_not_evidence_of_a_join() -> None:
    validation = assess_relationship(_facts(_col("customer_id"), _col("customer_id")))

    assert validation.outcome == NAME_MATCH_ONLY and not validation.approvable
    assert _classes(validation) == {"NAME_MATCH": False, "TYPE_MATCH": False}
    assert (validation.cardinality, validation.direction) == ("UNKNOWN", "UNDETERMINED")
    assert "FAN_OUT_POSSIBLE" in validation.grain_warnings


def test_a_key_on_the_target_alone_makes_a_corroborated_many_to_one_join() -> None:
    validation = assess_relationship(
        _facts(_col("customer_id"), _col("customer_id"), target_table=_keyed("customer_id"))
    )

    assert validation.outcome == CORROBORATED
    assert _classes(validation)["DECLARED_KEY"] is True
    assert (validation.cardinality, validation.direction) == (
        "MANY_TO_ONE",
        "SOURCE_REFERENCES_TARGET",
    )
    assert validation.join_condition == "source.customer_id = target.customer_id"
    assert validation.grain_warnings == ()


def test_two_keys_sharing_a_name_and_a_key_on_a_bare_id_are_not_evidence() -> None:
    both_keys = assess_relationship(
        _facts(
            _col("account_id"),
            _col("account_id"),
            source_table=TableFacts(SOURCE_TABLE, declared_keys=(frozenset({"account_id"}),)),
            target_table=_keyed("account_id"),
        )
    )
    bare_id = assess_relationship(_facts(_col("id"), _col("ID"), target_table=_keyed("id")))

    assert (both_keys.outcome, both_keys.cardinality) == (NAME_MATCH_ONLY, "ONE_TO_ONE")
    assert bare_id.outcome == NAME_MATCH_ONLY
    assert "GENERIC_COLUMN_NAME" in bare_id.grain_warnings


def test_profiled_uniqueness_from_a_sample_corroborates_but_stays_sample_bounded() -> None:
    profile = ProfileBounds(
        uuid4(),
        datetime(2026, 9, 14, tzinfo=UTC),
        sampled_row_count=1_000,
        row_count_estimate=50_000,
    )
    target = _col("customer_id", nulls=0, non_null=1_000, distinct=995)

    validation = assess_relationship(
        _facts(_col("customer_id"), target, target_table=TableFacts(TARGET_TABLE, profile=profile))
    )

    assert validation.outcome == CORROBORATED
    (profiled,) = [item for item in validation.evidence_classes if item.name == "PROFILED_UNIQUE"]
    assert profiled.corroborating and profiled.sample_bounded
    assert validation.target_uniqueness.basis == "PROFILED"
    assert "UNIQUENESS_SAMPLE_BOUNDED" in validation.grain_warnings
    assert validation.as_evidence()["target_observation"]["scope"] == "SAMPLE"


def test_a_foreign_key_declared_the_other_way_reverses_the_direction() -> None:
    source_table = TableFacts(SOURCE_TABLE, declared_keys=(frozenset({"customer_id"}),))
    target_table = TableFacts(
        TARGET_TABLE,
        foreign_keys=(DeclaredForeignKey(("customer_id",), SOURCE_TABLE, ("customer_id",)),),
    )
    referencing = _col("customer_id", nullable=True, nulls=4, non_null=996)

    validation = assess_relationship(
        _facts(
            _col("customer_id"), referencing, source_table=source_table, target_table=target_table
        )
    )

    assert _classes(validation)["DECLARED_FOREIGN_KEY"] is True
    assert (validation.cardinality, validation.direction, validation.referencing_side) == (
        "ONE_TO_MANY",
        "TARGET_REFERENCES_SOURCE",
        "TARGET",
    )
    assert validation.optionality == "OPTIONAL"
    assert "DIRECTION_REVERSED" in validation.grain_warnings


def test_joins_observed_in_query_history_corroborate_a_join() -> None:
    validation = assess_relationship(
        _facts(
            _col("cust_ref"),
            _col("customer_id", physical_type="VARCHAR(20)"),
            rule="QUERY_LOG_JOIN_V1",
            observed=7,
        )
    )

    assert validation.outcome == CORROBORATED
    assert _classes(validation) == {"OBSERVED_QUERY_JOIN": True}
    assert {"TYPE_FAMILY_MISMATCH", "FAN_OUT_POSSIBLE"} <= set(validation.grain_warnings)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_col("customer_id", nullable=False), "MANDATORY"),
        (_col("customer_id", nullable=True, nulls=3, non_null=997), "OPTIONAL"),
        (_col("customer_id", nullable=True, nulls=0, non_null=1_000), "NULLABLE_NONE_OBSERVED"),
        (_col("customer_id", nullable=True), "UNKNOWN"),
    ],
)
def test_optionality_of_the_referencing_columns_is_declared_and_observed(
    source: ColumnFacts, expected: str
) -> None:
    validation = assess_relationship(
        _facts(source, _col("customer_id"), target_table=_keyed("customer_id"))
    )

    assert validation.optionality == expected


def test_drift_compares_todays_conclusions_with_the_ones_approved() -> None:
    keyed = _keyed("customer_id")
    approved = assess_relationship(
        _facts(_col("customer_id"), _col("customer_id"), target_table=keyed)
    )
    evidence = with_recorded_validation({"signals": []}, approved, datetime.now(UTC))
    reprofiled = assess_relationship(
        _facts(
            _col("customer_id"),
            _col("customer_id"),
            target_table=TableFacts(
                TARGET_TABLE,
                declared_keys=keyed.declared_keys,
                profile=ProfileBounds(uuid4(), datetime.now(UTC), 10, 10),
            ),
        )
    )
    nulls_appeared = assess_relationship(
        _facts(
            _col("customer_id", nullable=True, nulls=5, non_null=5),
            _col("customer_id"),
            target_table=keyed,
        )
    )
    key_dropped = assess_relationship(_facts(_col("customer_id"), _col("customer_id")))

    assert evidence["signals"] == [] and evidence["validation"]["validated_at"]
    assert validation_drift({}, approved) == "NOT_RECORDED"
    assert validation_drift(evidence, reprofiled) == "UNCHANGED"
    assert validation_drift(evidence, nulls_appeared) == "CHANGED"
    assert validation_drift(evidence, key_dropped) == "CORROBORATION_LOST"


def test_validation_runs_no_source_query_and_says_the_inclusion_check_was_not_run() -> None:
    record = assess_relationship(_facts(_col("customer_id"), _col("customer_id"))).as_evidence()

    assert (record["source_queries_executed"], record["values_inspected"]) == (0, False)
    assert record["inclusion_check_status"] == "NOT_RUN"


# --------------------------------------------------------------------------
# Decisions and reads against a real session
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _source(session: AsyncSession) -> tuple[Organization, DataSource]:
    org = await _org(session)
    lob = await _lob(session, org)
    domain = await _domain(session, org, lob)
    project = await _project(session, org, lob, domain)
    return org, await _datasource(session, org, lob, domain, project, name="core-banking")


async def _key(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    table: MetadataTable,
    *columns: str,
) -> None:
    session.add(
        MetadataConstraint(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=table.id,
            name=f"pk_{table.name}",
            constraint_type="PRIMARY_KEY",
            columns=list(columns),
            fingerprint="f" * 8,
        )
    )
    await session.flush()


async def _column(
    session: AsyncSession, org: Organization, table: MetadataTable, name: str, ordinal: int
) -> MetadataColumn:
    column = MetadataColumn(
        organization_id=org.id,
        table_id=table.id,
        name=name,
        ordinal_position=ordinal,
        physical_type="INTEGER",
        nullable=False,
        fingerprint="f" * 8,
    )
    session.add(column)
    await session.flush()
    return column


async def _candidate(
    session: AsyncSession, org: Organization, datasource: DataSource, *, target_key: bool
) -> tuple[RelationshipCandidate, MetadataTable]:
    _, source = await _table_with_column(
        session,
        org,
        datasource,
        table_name=f"orders_{uuid4().hex[:6]}",
        column_name="customer_id",
        physical_type="INTEGER",
    )
    target_table, target = await _table_with_column(
        session,
        org,
        datasource,
        table_name=f"customers_{uuid4().hex[:6]}",
        column_name="customer_id",
        physical_type="INTEGER",
    )
    if target_key:
        await _key(session, org, datasource, target_table, "customer_id")
    candidate = RelationshipCandidate(
        organization_id=org.id,
        datasource_id=datasource.id,
        target_datasource_id=datasource.id,
        source_table_id=source.table_id,
        source_column_id=source.id,
        target_table_id=target_table.id,
        target_column_id=target.id,
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.9,
        evidence={"column_name_match": "EXACT"},
        created_by="maker",
    )
    session.add(candidate)
    await session.flush()
    return candidate, target_table


async def test_approving_a_join_that_rests_on_a_name_match_is_refused(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, _ = await _candidate(session, org, datasource, target_key=False)

    with pytest.raises(HTTPException) as refused:
        await decide_relationship_candidate(
            candidate.id,
            RelationshipCandidateDecision(decision="APPROVE"),
            context=_context(org, "reviewer"),
            session=session,
            settings=Settings(),
        )

    assert refused.value.status_code == 409
    detail = refused.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == NAME_MATCH_ONLY_CODE
    assert detail["outcome"] == NAME_MATCH_ONLY
    assert "validation" not in detail, "the evidence is served only by the gated read"
    assert candidate.status == "PENDING" and "validation" not in candidate.evidence
    assert (await session.scalars(select(OutboxEvent))).all() == []


async def test_an_approval_keeps_its_validation_and_a_dropped_key_shows_as_lost(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    candidate, target_table = await _candidate(session, org, datasource, target_key=True)
    reviewer = _context(org, "reviewer")

    approved = await decide_relationship_candidate(
        candidate.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=reviewer,
        session=session,
        settings=Settings(),
    )

    recorded = approved.evidence["validation"]
    assert approved.status == "APPROVED"
    assert approved.evidence["column_name_match"] == "EXACT", "detection evidence is kept"
    assert (recorded["outcome"], recorded["cardinality"], recorded["source_queries_executed"]) == (
        CORROBORATED,
        "MANY_TO_ONE",
        0,
    )
    assert recorded["validated_at"]
    unchanged = await get_relationship_candidate_validation(
        candidate.id, context=reviewer, session=session, settings=Settings()
    )
    assert (unchanged.drift, unchanged.recorded_fingerprint) == (
        "UNCHANGED",
        recorded["fingerprint"],
    )

    key = await session.scalar(
        select(MetadataConstraint).where(MetadataConstraint.table_id == target_table.id)
    )
    assert key is not None
    key.status = "DEPRECATED"
    await session.flush()
    lost = await get_relationship_candidate_validation(
        candidate.id, context=reviewer, session=session, settings=Settings()
    )
    assert (lost.drift, lost.approvable) == ("CORROBORATION_LOST", False)


async def test_a_bulk_approval_fails_only_the_join_without_evidence(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    supported, _ = await _candidate(session, org, datasource, target_key=True)
    unsupported, _ = await _candidate(session, org, datasource, target_key=False)

    result = await bulk_decide_relationship_candidates(
        RelationshipCandidateBulkDecisionRequest(
            candidate_ids=[supported.id, unsupported.id], decision="APPROVE"
        ),
        context=_context(org, "reviewer"),
        session=session,
        settings=Settings(),
    )

    by_id = {item.candidate_id: item for item in result.results}
    assert by_id[str(supported.id)].status == "SUCCEEDED"
    assert by_id[str(unsupported.id)].status == "FAILED"
    assert (by_id[str(unsupported.id)].reason or "").startswith(NAME_MATCH_ONLY_CODE)
    assert (supported.status, unsupported.status) == ("APPROVED", "PENDING")


async def test_a_composite_join_is_approved_only_once_a_key_covers_its_columns(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    source_table, source_branch = await _table_with_column(
        session,
        org,
        datasource,
        table_name="loan",
        column_name="branch_code",
        physical_type="INTEGER",
    )
    target_table, target_branch = await _table_with_column(
        session,
        org,
        datasource,
        table_name="account",
        column_name="branch_code",
        physical_type="INTEGER",
    )
    source_account = await _column(session, org, source_table, "account_no", 2)
    target_account = await _column(session, org, target_table, "account_no", 2)
    group = RelationshipCandidateGroup(
        organization_id=org.id,
        datasource_id=datasource.id,
        source_table_id=source_table.id,
        target_table_id=target_table.id,
        member_fingerprint="c" * 64,
        member_count=2,
        detection_rule="COMPOSITE_EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.7,
        evidence={},
        created_by="maker",
    )
    session.add(group)
    await session.flush()
    pairs = ((source_branch, target_branch), (source_account, target_account))
    for ordinal, (source, target) in enumerate(pairs):
        session.add(
            RelationshipCandidateGroupMember(
                group_id=group.id,
                ordinal=ordinal,
                source_column_id=source.id,
                target_column_id=target.id,
            )
        )
    await session.flush()
    reviewer = _context(org, "reviewer")

    with pytest.raises(HTTPException) as refused:
        await decide_composite_relationship_candidate(
            group.id,
            RelationshipCandidateDecision(decision="APPROVE"),
            context=reviewer,
            session=session,
            settings=Settings(),
        )
    assert refused.value.status_code == 409

    await _key(session, org, datasource, target_table, "branch_code", "account_no")
    approved = await decide_composite_relationship_candidate(
        group.id,
        RelationshipCandidateDecision(decision="APPROVE"),
        context=reviewer,
        session=session,
        settings=Settings(),
    )

    assert approved.status == "APPROVED"
    assert approved.evidence["validation"]["join_condition"] == (
        "source.branch_code = target.branch_code AND source.account_no = target.account_no"
    )


async def test_composite_key_discovery_persists_and_an_approved_key_corroborates_a_join(
    session: AsyncSession,
) -> None:
    org, datasource = await _source(session)
    table, region = await _table_with_column(
        session,
        org,
        datasource,
        table_name="customers",
        column_name="region_code",
        physical_type="INTEGER",
    )
    sequence = await _column(session, org, table, "sequence_no", 2)
    profile = TableProfile(
        organization_id=org.id,
        analysis_run_id=uuid4(),
        datasource_id=datasource.id,
        table_id=table.id,
        sampled_row_count=1_000,
        row_count_estimate=5_000,
        status="COMPLETED",
    )
    session.add(profile)
    await session.flush()
    for column, distinct in ((region, 950), (sequence, 1_000)):
        session.add(
            ColumnProfile(
                organization_id=org.id,
                table_profile_id=profile.id,
                column_id=column.id,
                null_count=0,
                non_null_count=1_000,
                approximate_distinct_count=distinct,
            )
        )
    await session.flush()

    page = await discover_composite_key_candidates(
        table.id, context=_context(org, "maker"), session=session
    )

    stored = list((await session.scalars(select(CompositeKeyCandidate))).all())
    assert len(stored) == page.total > 0
    for row in stored:
        assert row.column_count == len(row.column_ids) == len(row.column_names)
        assert len(row.key_fingerprint) == 64
        assert 0 < row.estimated_distinctness_ratio <= 1
        assert row.table_profile_id == profile.id
    (sequence_key,) = [row for row in stored if row.column_names == ["sequence_no"]]
    await decide_composite_key_candidate(
        sequence_key.id,
        CompositeKeyCandidateDecision(decision="APPROVE"),
        context=_context(org, "reviewer"),
        session=session,
    )

    _, orders_sequence = await _table_with_column(
        session,
        org,
        datasource,
        table_name="orders",
        column_name="sequence_no",
        physical_type="INTEGER",
    )
    candidate = RelationshipCandidate(
        organization_id=org.id,
        datasource_id=datasource.id,
        target_datasource_id=datasource.id,
        source_table_id=orders_sequence.table_id,
        source_column_id=orders_sequence.id,
        target_table_id=table.id,
        target_column_id=sequence.id,
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.9,
        evidence={},
        created_by="maker",
    )
    session.add(candidate)
    await session.flush()

    validation = await get_relationship_candidate_validation(
        candidate.id, context=_context(org, "reviewer"), session=session, settings=Settings()
    )

    assert validation.approvable
    assert validation.target_uniqueness.basis == "APPROVED_KEY"
