"""PR-2: policy-approved range/top-value profiling by classification.

Covers, against a real SQL engine (in-memory SQLite, the same approach
`test_catalog_pagination.py` and `test_profile_plan_pagination.py` use):

* the live gate in `profile_table_task` -- value capture happens only when
  BOTH an APPROVED, unrevoked `ProfilingExceptionPolicy` exists for the
  column's classification AND the connector reports
  `capabilities.value_range_profiling`;
* the retention purge (`purge_expired_value_profile_artifacts`);
* the REST endpoints' maker-checker enforcement and classification
  validation.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from temporalio import activity
from temporalio.common import _CompositeEvent
from temporalio.converter import PayloadConverter

import aida.api as api_module
import aida.task_tracking as task_tracking
import aida.workflows.activities as activities
from aida.config import Settings
from aida.connectors.base import (
    ColumnProfileSnapshot,
    ColumnValueProfileSnapshot,
    ConnectorCapabilities,
    ConnectorValueProfilingUnsupported,
    TableProfileSnapshot,
)
from aida.db import Base
from aida.models import (
    AnalysisRun,
    AuditEvent,
    ColumnValueProfileArtifact,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    ProfilingExceptionPolicy,
    Project,
)
from aida.profiling_exceptions import approved_policy_for, purge_expired_value_profile_artifacts
from aida.schemas import (
    ProfilingExceptionDecisionRequest,
    ProfilingExceptionPolicyCreate,
    ProfilingExceptionRevokeRequest,
)
from aida.security import SecurityContext

pytestmark = pytest.mark.asyncio

# `AuditEvent.id` is a `BigInteger` autoincrement primary key relying in
# production on Postgres's identity/sequence generation. sqlite only
# auto-populates a bare `INTEGER PRIMARY KEY` (its rowid alias) -- `BigInteger`
# compiles to `BIGINT`, which sqlite does not treat as that alias -- so every
# `record_audit()` call below (both the gate paths and the REST endpoints
# under test call it) would leave `id` NULL against an in-memory sqlite
# session. Assign ids by hand for this module's sqlite engine only, exactly
# the workaround `test_relationship_intelligence_review.py` uses; nothing
# about the production model changes.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fake_activity_context():
    """`profile_table_task` calls `activity.is_cancelled()`/`activity.heartbeat()`,
    which require a live Temporal activity context -- there is none when calling
    the activity function directly in a unit test (as opposed to going through
    `workflow.execute_activity`, which every other test in this codebase that
    touches an activity does instead). Installs a minimal but real
    `temporalio.activity._Context` for the duration of each test so those calls
    behave like a genuinely-not-cancelled activity rather than raising
    `RuntimeError: Not in activity context`.
    """
    context = activity._Context(
        info=lambda: (_ for _ in ()).throw(RuntimeError("activity.info() unused by this test")),
        heartbeat=lambda *details: None,
        cancelled_event=_CompositeEvent(thread_event=threading.Event(), async_event=None),
        worker_shutdown_event=_CompositeEvent(thread_event=threading.Event(), async_event=None),
        shield_thread_cancel_exception=None,
        payload_converter_class_or_instance=PayloadConverter,
        runtime_metric_meter=None,
        client=None,
        cancellation_details=activity._ActivityCancellationDetailsHolder(),
    )
    token = activity._Context.set(context)
    try:
        yield
    finally:
        activity._Context.reset(token)


async def _seed_table_with_column(
    session: AsyncSession, *, classification: str = "PII"
) -> tuple[DataSource, AnalysisRun, MetadataTable, MetadataSchema, MetadataColumn]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customers",
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name="email",
        ordinal_position=1,
        physical_type="text",
        nullable=True,
        classification=classification,
        fingerprint="fp",
    )
    session.add(column)
    run = AnalysisRun(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, status="RUNNING"
    )
    session.add(run)
    await session.commit()
    return datasource, run, table, schema, column


@dataclass
class _FakeConnector:
    capabilities: ConnectorCapabilities
    value_snapshots: tuple[ColumnValueProfileSnapshot, ...] = ()
    raise_unsupported: bool = False
    value_calls: list[tuple[str, ...]] = field(default_factory=list)

    async def profile_table(self, schema_name, table_name, column_names, **kwargs):
        return TableProfileSnapshot(
            row_count_estimate=100,
            sampled_row_count=100,
            columns=tuple(
                ColumnProfileSnapshot(
                    name=name,
                    null_count=0,
                    non_null_count=100,
                    approximate_distinct_count=90,
                    min_length=3,
                    max_length=40,
                )
                for name in column_names
            ),
        )

    async def profile_column_values(self, schema_name, table_name, column_names, **kwargs):
        self.value_calls.append(column_names)
        if self.raise_unsupported:
            raise ConnectorValueProfilingUnsupported("simulated")
        return self.value_snapshots


def _context(
    organization_id, principal_id="requester", roles=("PlatformAdmin",)
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles),
    )


# --- the live gate in profile_table_task ------------------------------------


async def test_value_capture_requires_both_capability_and_an_approved_policy(
    session, monkeypatch
) -> None:
    datasource, run, table, _schema, column = await _seed_table_with_column(session)
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setenv("TEST_DSN", "postgresql://test")

    fake = _FakeConnector(
        capabilities=ConnectorCapabilities(value_range_profiling=False),
        value_snapshots=(ColumnValueProfileSnapshot(name="email", min_value="a", max_value="z"),),
    )
    monkeypatch.setattr(activities.connector_registry, "create", lambda *a, **k: fake)

    await activities.profile_table_task({"run_id": str(run.id), "table_id": str(table.id)})

    artifacts = (await session.scalars(select(ColumnValueProfileArtifact))).all()
    assert artifacts == [], (
        "capability False must never yield a value artifact, even with a snapshot ready"
    )
    assert fake.value_calls == [], (
        "profile_column_values must not even be called without the capability"
    )


async def test_capability_present_but_no_approved_policy_yields_no_artifact(
    session, monkeypatch
) -> None:
    datasource, run, table, _schema, column = await _seed_table_with_column(session)
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setenv("TEST_DSN", "postgresql://test")

    fake = _FakeConnector(capabilities=ConnectorCapabilities(value_range_profiling=True))
    monkeypatch.setattr(activities.connector_registry, "create", lambda *a, **k: fake)

    await activities.profile_table_task({"run_id": str(run.id), "table_id": str(table.id)})

    artifacts = (await session.scalars(select(ColumnValueProfileArtifact))).all()
    assert artifacts == []
    assert fake.value_calls == [], "no approved policy means profile_column_values is never reached"


async def test_an_approved_unrevoked_policy_plus_capability_captures_and_pins_retention(
    session, monkeypatch
) -> None:
    datasource, run, table, _schema, column = await _seed_table_with_column(
        session, classification="PII"
    )
    policy = ProfilingExceptionPolicy(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        classification="PII",
        status="APPROVED",
        retention_days=14,
        requested_by="steward-a",
        request_reason="fraud investigation",
        decided_by="steward-b",
        decided_at=datetime.now(UTC),
    )
    session.add(policy)
    await session.commit()

    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setenv("TEST_DSN", "postgresql://test")

    fake = _FakeConnector(
        capabilities=ConnectorCapabilities(value_range_profiling=True),
        value_snapshots=(
            ColumnValueProfileSnapshot(
                name="email",
                min_value="a@bank.com",
                max_value="z@bank.com",
                top_values=(("a@bank.com", 3),),
            ),
        ),
    )
    monkeypatch.setattr(activities.connector_registry, "create", lambda *a, **k: fake)

    before = datetime.now(UTC)
    result = await activities.profile_table_task({"run_id": str(run.id), "table_id": str(table.id)})
    after = datetime.now(UTC)

    assert result == {"profiled_tables": 1, "profiled_columns": 1}
    assert fake.value_calls == [("email",)]
    artifact = (await session.scalars(select(ColumnValueProfileArtifact))).one()
    assert artifact.column_id == column.id
    assert artifact.policy_id == policy.id
    assert artifact.classification == "PII"
    assert artifact.min_value == "a@bank.com"
    assert artifact.max_value == "z@bank.com"
    assert artifact.top_values == [{"value": "a@bank.com", "count": 3}]
    # Retention is pinned from the policy's retention_days at capture time.
    # (sqlite round-trips `DateTime(timezone=True)` as naive, so compare in UTC.)
    expires_at = artifact.expires_at.replace(tzinfo=UTC)
    expected_expiry_floor = before + timedelta(days=14)
    expected_expiry_ceiling = after + timedelta(days=14)
    assert expected_expiry_floor <= expires_at <= expected_expiry_ceiling


async def test_a_revoked_policy_no_longer_authorizes_capture(session, monkeypatch) -> None:
    datasource, run, table, _schema, _column = await _seed_table_with_column(
        session, classification="PCI"
    )
    policy = ProfilingExceptionPolicy(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        classification="PCI",
        status="REVOKED",
        retention_days=7,
        requested_by="steward-a",
        request_reason="was needed once",
        decided_by="steward-b",
        decided_at=datetime.now(UTC),
        revoked_by="steward-c",
        revoked_at=datetime.now(UTC),
        revocation_reason="investigation closed",
    )
    session.add(policy)
    await session.commit()
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setenv("TEST_DSN", "postgresql://test")

    fake = _FakeConnector(capabilities=ConnectorCapabilities(value_range_profiling=True))
    monkeypatch.setattr(activities.connector_registry, "create", lambda *a, **k: fake)

    await activities.profile_table_task({"run_id": str(run.id), "table_id": str(table.id)})

    assert (await session.scalars(select(ColumnValueProfileArtifact))).all() == []
    assert fake.value_calls == []


async def test_connector_that_lied_about_the_capability_fails_closed_not_the_whole_task(
    session, monkeypatch
) -> None:
    datasource, run, table, _schema, _column = await _seed_table_with_column(
        session, classification="PII"
    )
    policy = ProfilingExceptionPolicy(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        classification="PII",
        status="APPROVED",
        retention_days=30,
        requested_by="steward-a",
        request_reason="x",
        decided_by="steward-b",
        decided_at=datetime.now(UTC),
    )
    session.add(policy)
    await session.commit()
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setenv("TEST_DSN", "postgresql://test")

    fake = _FakeConnector(
        capabilities=ConnectorCapabilities(value_range_profiling=True), raise_unsupported=True
    )
    monkeypatch.setattr(activities.connector_registry, "create", lambda *a, **k: fake)

    result = await activities.profile_table_task({"run_id": str(run.id), "table_id": str(table.id)})

    assert result == {"profiled_tables": 1, "profiled_columns": 1}
    assert (await session.scalars(select(ColumnValueProfileArtifact))).all() == []


# --- the gate helper directly ------------------------------------------------


async def test_approved_policy_for_only_matches_approved_status(session) -> None:
    org_id, ds_id = uuid4(), uuid4()
    for status in ("PENDING", "REJECTED", "REVOKED"):
        session.add(
            ProfilingExceptionPolicy(
                id=uuid4(),
                organization_id=org_id,
                datasource_id=ds_id,
                classification=status,  # distinct per row to avoid the (org,ds,class) collision
                status=status,
                retention_days=30,
                requested_by="x",
                request_reason="x",
            )
        )
    approved = ProfilingExceptionPolicy(
        id=uuid4(),
        organization_id=org_id,
        datasource_id=ds_id,
        classification="PII",
        status="APPROVED",
        retention_days=30,
        requested_by="x",
        request_reason="x",
    )
    session.add(approved)
    await session.commit()

    for status in ("PENDING", "REJECTED", "REVOKED"):
        found = await approved_policy_for(
            session, organization_id=org_id, datasource_id=ds_id, classification=status
        )
        assert found is None
    found = await approved_policy_for(
        session, organization_id=org_id, datasource_id=ds_id, classification="PII"
    )
    assert found is not None
    assert found.id == approved.id


# --- retention purge ----------------------------------------------------------


async def test_purge_deletes_only_artifacts_past_their_pinned_expiry(session, monkeypatch) -> None:
    datasource, run, table, _schema, column = await _seed_table_with_column(session)
    policy = ProfilingExceptionPolicy(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        classification="PII",
        status="APPROVED",
        retention_days=1,
        requested_by="x",
        request_reason="x",
    )
    session.add(policy)
    await session.flush()

    from aida.models import ColumnProfile, TableProfile

    # Two separate analysis runs, each with its own `TableProfile` row (as two
    # separate profiling runs over the same table would produce), so both
    # `ColumnProfile` rows can reference the same column without violating
    # either `TableProfile`'s `(analysis_run_id, table_id)` uniqueness or
    # `ColumnProfile`'s `(table_profile_id, column_id)` uniqueness.
    second_run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        status="RUNNING",
    )
    session.add(second_run)
    await session.flush()
    expired_run_profile = TableProfile(
        id=uuid4(),
        organization_id=datasource.organization_id,
        analysis_run_id=run.id,
        datasource_id=datasource.id,
        table_id=table.id,
        sampled_row_count=10,
    )
    live_run_profile = TableProfile(
        id=uuid4(),
        organization_id=datasource.organization_id,
        analysis_run_id=second_run.id,
        datasource_id=datasource.id,
        table_id=table.id,
        sampled_row_count=10,
    )
    session.add_all([expired_run_profile, live_run_profile])
    await session.flush()
    now = datetime.now(UTC)
    expired_column_profile = ColumnProfile(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_profile_id=expired_run_profile.id,
        column_id=column.id,
        null_count=0,
        non_null_count=10,
        approximate_distinct_count=5,
    )
    live_column_profile = ColumnProfile(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_profile_id=live_run_profile.id,
        column_id=column.id,
        null_count=0,
        non_null_count=10,
        approximate_distinct_count=5,
    )
    session.add_all([expired_column_profile, live_column_profile])
    await session.flush()
    expired_artifact = ColumnValueProfileArtifact(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        column_id=column.id,
        column_profile_id=expired_column_profile.id,
        policy_id=policy.id,
        classification="PII",
        min_value="a",
        max_value="z",
        top_values=[],
        captured_at=now - timedelta(days=2),
        expires_at=now - timedelta(hours=1),
    )
    live_artifact = ColumnValueProfileArtifact(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        table_id=table.id,
        column_id=column.id,
        column_profile_id=live_column_profile.id,
        policy_id=policy.id,
        classification="PII",
        min_value="a",
        max_value="z",
        top_values=[],
        captured_at=now,
        expires_at=now + timedelta(days=10),
    )
    session.add_all([expired_artifact, live_artifact])
    await session.commit()

    monkeypatch.setattr("aida.profiling_exceptions.session_factory", lambda: session)
    purged = await purge_expired_value_profile_artifacts(Settings(_env_file=None), now=now)

    assert purged == 1
    remaining = (await session.scalars(select(ColumnValueProfileArtifact))).all()
    assert [row.id for row in remaining] == [live_artifact.id]


# --- REST endpoint behaviour --------------------------------------------------


async def _seed_datasource_only(session: AsyncSession) -> DataSource:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    session.add_all([org, lob, domain, project, datasource])
    await session.commit()
    return datasource


async def test_request_endpoint_rejects_a_non_sensitive_classification(session) -> None:
    datasource = await _seed_datasource_only(session)
    with pytest.raises(HTTPException) as excinfo:
        await api_module.request_profiling_exception_policy(
            datasource.id,
            ProfilingExceptionPolicyCreate(
                classification="PUBLIC", reason="need it", retention_days=30
            ),
            context=_context(datasource.organization_id),
            session=session,
        )
    assert excinfo.value.status_code == 422


async def test_request_endpoint_rejects_a_second_pending_request_for_the_same_scope(
    session,
) -> None:
    datasource = await _seed_datasource_only(session)
    await api_module.request_profiling_exception_policy(
        datasource.id,
        ProfilingExceptionPolicyCreate(classification="PII", reason="first", retention_days=30),
        context=_context(datasource.organization_id),
        session=session,
    )
    with pytest.raises(HTTPException) as excinfo:
        await api_module.request_profiling_exception_policy(
            datasource.id,
            ProfilingExceptionPolicyCreate(
                classification="PII", reason="second", retention_days=30
            ),
            context=_context(datasource.organization_id),
            session=session,
        )
    assert excinfo.value.status_code == 409


async def test_decision_endpoint_enforces_maker_checker_separation(session) -> None:
    datasource = await _seed_datasource_only(session)
    policy = await api_module.request_profiling_exception_policy(
        datasource.id,
        ProfilingExceptionPolicyCreate(classification="PII", reason="need it", retention_days=30),
        context=_context(datasource.organization_id, principal_id="alice"),
        session=session,
    )
    with pytest.raises(HTTPException) as excinfo:
        await api_module.decide_profiling_exception_policy(
            policy.id,
            ProfilingExceptionDecisionRequest(decision="APPROVE"),
            context=_context(
                datasource.organization_id, principal_id="alice", roles=("DataSteward",)
            ),
            session=session,
        )
    assert excinfo.value.status_code == 409

    approved = await api_module.decide_profiling_exception_policy(
        policy.id,
        ProfilingExceptionDecisionRequest(decision="APPROVE"),
        context=_context(datasource.organization_id, principal_id="bob", roles=("DataSteward",)),
        session=session,
    )
    assert approved.status == "APPROVED"
    assert approved.decided_by == "bob"


async def test_revoke_endpoint_only_accepts_an_approved_policy(session) -> None:
    datasource = await _seed_datasource_only(session)
    policy = await api_module.request_profiling_exception_policy(
        datasource.id,
        ProfilingExceptionPolicyCreate(classification="PII", reason="need it", retention_days=30),
        context=_context(datasource.organization_id, principal_id="alice"),
        session=session,
    )
    with pytest.raises(HTTPException) as excinfo:
        await api_module.revoke_profiling_exception_policy(
            policy.id,
            ProfilingExceptionRevokeRequest(reason="too early"),
            context=_context(
                datasource.organization_id, principal_id="bob", roles=("DataSteward",)
            ),
            session=session,
        )
    assert excinfo.value.status_code == 409

    await api_module.decide_profiling_exception_policy(
        policy.id,
        ProfilingExceptionDecisionRequest(decision="APPROVE"),
        context=_context(datasource.organization_id, principal_id="bob", roles=("DataSteward",)),
        session=session,
    )
    revoked = await api_module.revoke_profiling_exception_policy(
        policy.id,
        ProfilingExceptionRevokeRequest(reason="incident closed"),
        context=_context(datasource.organization_id, principal_id="carol", roles=("DataSteward",)),
        session=session,
    )
    assert revoked.status == "REVOKED"
    assert revoked.revoked_by == "carol"
