from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.fleet import (
    RunAdmissionRejected,
    datasource_health,
    ensure_datasource_enabled,
    fleet_health,
    reserve_analysis_run,
    tool_first_execution_rate,
)
from aida.models import (
    AgentRun,
    AnalysisRun,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    OutboxEvent,
    Project,
    ScanPolicy,
)
from aida.operational_api import (
    get_datasource_health,
    organization_fleet_health,
    organization_tool_first_rate,
    requeue_outbox_event,
)
from aida.projectors import graph_projector
from aida.projectors.outbox_publisher import record_publish_failure, retry_delay_seconds
from aida.security import SecurityContext, enforce_organization
from aida.tool_first_rate import DEFAULT_WINDOW_DAYS
from aida.workflows import scheduler
from aida.workflows.scheduler import due_scan_policies_statement
from tests.support.doubles import security_context


class SchedulerSession:
    def __init__(self, policy: ScanPolicy, timeline: list[str]) -> None:
        self.policy = policy
        self.timeline = timeline
        self.added: list[object] = []

    async def __aenter__(self) -> "SchedulerSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def scalar(self, _statement: object) -> ScanPolicy:
        return self.policy

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.timeline.append("commit")


def _due_policy(*, interval_minutes: int = 60) -> ScanPolicy:
    return ScanPolicy(
        id=uuid4(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        enabled=True,
        interval_minutes=interval_minutes,
        mode="INCREMENTAL",
        priority=70,
        next_run_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        created_by="operations-admin",
    )


async def test_scheduler_commits_run_and_evidence_before_workflow_dispatch(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 8, 28, 10, tzinfo=UTC)
    timeline: list[str] = []
    policy = _due_policy()
    session = SchedulerSession(policy, timeline)
    admitted = AnalysisRun(
        id=uuid4(),
        organization_id=policy.organization_id,
        datasource_id=policy.datasource_id,
        mode=policy.mode,
        trigger_type="SCHEDULED",
        priority=policy.priority,
        temporal_workflow_id=f"discovery-{policy.datasource_id}-scheduled",
    )
    reservation: dict[str, object] = {}

    async def reserve(_session: object, _settings: Settings, **kwargs: object) -> AnalysisRun:
        reservation.update(kwargs)
        return admitted

    async def start(_client: object, _settings: Settings, run: AnalysisRun) -> None:
        assert run is admitted
        timeline.append("dispatch")

    monkeypatch.setattr(scheduler, "session_factory", lambda: session)
    monkeypatch.setattr(scheduler, "reserve_analysis_run", reserve)
    monkeypatch.setattr(scheduler, "_start_workflow", start)

    processed = await scheduler.process_scan_policy(
        policy.id,
        object(),  # type: ignore[arg-type]
        Settings(_env_file=None),
        now=now,
    )

    assert processed is True
    assert timeline == ["commit", "dispatch"]
    assert policy.last_triggered_at == now
    assert policy.next_run_at == now + timedelta(minutes=60)
    assert reservation == {
        "datasource_id": policy.datasource_id,
        "mode": "INCREMENTAL",
        "trigger_type": "SCHEDULED",
        "priority": 70,
    }
    audit = next(value for value in session.added if isinstance(value, AuditEvent))
    outbox = next(value for value in session.added if isinstance(value, OutboxEvent))
    assert audit.action == "analysis_run.schedule"
    assert audit.organization_id == policy.organization_id
    assert audit.details == {"scan_policy_id": str(policy.id), "priority": 70}
    assert outbox.event_type == "analysis_run.scheduled.v1"
    assert outbox.payload == {
        "run_id": str(admitted.id),
        "datasource_id": str(policy.datasource_id),
        "scan_policy_id": str(policy.id),
    }


async def test_scheduler_defers_rejected_admission_without_dispatch(
    monkeypatch: Any,
) -> None:
    now = datetime(2026, 8, 28, 10, tzinfo=UTC)
    timeline: list[str] = []
    policy = _due_policy(interval_minutes=30)
    session = SchedulerSession(policy, timeline)

    async def reject(_session: object, _settings: Settings, **_kwargs: object) -> AnalysisRun:
        raise RunAdmissionRejected("datasource already has an active analysis run")

    async def unexpected_dispatch(*_args: object) -> None:
        timeline.append("dispatch")

    monkeypatch.setattr(scheduler, "session_factory", lambda: session)
    monkeypatch.setattr(scheduler, "reserve_analysis_run", reject)
    monkeypatch.setattr(scheduler, "_start_workflow", unexpected_dispatch)

    processed = await scheduler.process_scan_policy(
        policy.id,
        object(),  # type: ignore[arg-type]
        Settings(_env_file=None),
        now=now,
    )

    assert processed is False
    assert timeline == ["commit"]
    assert policy.next_run_at == now + timedelta(minutes=5)
    assert policy.last_triggered_at is None
    assert session.added == []


def test_tenant_boundary_denies_cross_organization_access() -> None:
    context = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Viewer"}),
    )

    enforce_organization(context, context.organization_id)  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as denied:
        enforce_organization(context, uuid4())

    assert denied.value.status_code == 403
    assert denied.value.detail == "cross-organization access denied"


def test_platform_admin_can_operate_across_organizations() -> None:
    context = SecurityContext(
        principal_id="platform-admin",
        principal_type="USER",
        organization_id=None,
        roles=frozenset({"PlatformAdmin"}),
    )

    enforce_organization(context, uuid4())


class GraphSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> "GraphSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def run(self, query: str, **parameters: object) -> None:
        self.calls.append((query, parameters))


class GraphDriver:
    def __init__(self) -> None:
        self.graph_session = GraphSession()

    def session(self) -> GraphSession:
        return self.graph_session


async def test_discovery_projection_builds_inventory_hierarchy_and_references(
    monkeypatch: Any,
) -> None:
    organization_id, datasource_id = uuid4(), uuid4()
    catalog_id, schema_id, table_id, column_id, constraint_id = (uuid4() for _ in range(5))
    referenced_table_id = uuid4()
    projection: dict[str, list[dict[str, object]]] = {
        "catalogs": [{"platform_id": str(catalog_id), "name": "warehouse"}],
        "schemas": [
            {
                "platform_id": str(schema_id),
                "catalog_id": str(catalog_id),
                "name": "finance",
            }
        ],
        "tables": [
            {
                "platform_id": str(table_id),
                "schema_id": str(schema_id),
                "name": "payments",
            }
        ],
        "columns": [
            {
                "platform_id": str(column_id),
                "table_id": str(table_id),
                "name": "payment_id",
            }
        ],
        "constraints": [
            {
                "platform_id": str(constraint_id),
                "table_id": str(table_id),
                "referenced_table_id": str(referenced_table_id),
                "constraint_type": "FOREIGN_KEY",
            }
        ],
    }
    loaded_scope: list[tuple[UUID, UUID]] = []

    async def load(source: UUID, organization: UUID) -> dict[str, list[dict[str, object]]]:
        loaded_scope.append((source, organization))
        return projection

    monkeypatch.setattr(graph_projector, "load_projection", load)
    driver = GraphDriver()

    await graph_projector.project_discovery(
        driver,  # type: ignore[arg-type]
        {
            "organization_id": str(organization_id),
            "payload": {"datasource_id": str(datasource_id)},
        },
    )

    assert loaded_scope == [(datasource_id, organization_id)]
    assert len(driver.graph_session.calls) == 6
    expected_rows = [
        projection["catalogs"],
        projection["schemas"],
        projection["tables"],
        projection["columns"],
        projection["constraints"],
        projection["constraints"],
    ]
    assert [parameters["rows"] for _, parameters in driver.graph_session.calls] == expected_rows
    queries = [" ".join(query.split()) for query, _ in driver.graph_session.calls]
    assert "MERGE (parent)-[:HAS_SCHEMA]->(n)" in queries[1]
    assert "MERGE (parent)-[:HAS_TABLE]->(n)" in queries[2]
    assert "MERGE (parent)-[:HAS_COLUMN]->(n)" in queries[3]
    assert "MERGE (parent)-[:HAS_CONSTRAINT]->(n)" in queries[4]
    assert "MERGE (n)-[:REFERENCES]->(referenced)" in queries[5]


def _pending_outbox_event(**overrides: Any) -> OutboxEvent:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "aggregate_type": "analysis_run",
        "aggregate_id": str(uuid4()),
        "event_type": "analysis_run.scheduled.v1",
        "payload": {},
        "status": "PENDING",
        "attempt_count": 0,
        "next_attempt_at": datetime(2026, 8, 29, tzinfo=UTC),
    }
    defaults.update(overrides)
    return OutboxEvent(**defaults)


def test_record_publish_failure_dead_letters_once_max_attempts_reached() -> None:
    event = _pending_outbox_event(attempt_count=2)
    now = datetime(2026, 8, 29, 10, tzinfo=UTC)

    record_publish_failure(
        event,
        RuntimeError("kafka unavailable"),
        max_attempts=3,
        max_backoff_seconds=300,
        now=now,
    )

    assert event.attempt_count == 3
    assert event.last_error == "RuntimeError"
    assert event.status == "DEAD_LETTER"


def test_record_publish_failure_retries_with_backoff_below_max_attempts() -> None:
    event = _pending_outbox_event(attempt_count=0)
    now = datetime(2026, 8, 29, 10, tzinfo=UTC)

    record_publish_failure(
        event,
        TimeoutError("send timed out"),
        max_attempts=5,
        max_backoff_seconds=300,
        now=now,
    )

    assert event.attempt_count == 1
    assert event.status == "PENDING"
    assert event.last_error == "TimeoutError"
    assert event.next_attempt_at == now + timedelta(seconds=retry_delay_seconds(1, 300))


class RequeueSession:
    def __init__(self, event: OutboxEvent | None) -> None:
        self.event = event
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def get(self, _model: type[object], _identity: UUID) -> OutboxEvent | None:
        return self.event

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.timeline.append("commit")


async def test_requeue_outbox_event_resets_a_dead_letter_event_to_pending() -> None:
    organization_id = uuid4()
    event = _pending_outbox_event(
        organization_id=organization_id,
        status="DEAD_LETTER",
        attempt_count=5,
        last_error="RuntimeError",
        next_attempt_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session = RequeueSession(event)
    context = SecurityContext(
        principal_id="ops-admin",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )

    result = await requeue_outbox_event(
        event.id,
        context,
        session,  # type: ignore[arg-type]
    )

    assert result is event
    assert event.status == "PENDING"
    assert event.attempt_count == 0
    assert event.last_error is None
    assert session.timeline == ["commit"]
    audit = next(value for value in session.added if isinstance(value, AuditEvent))
    assert audit.action == "outbox_event.requeue"
    assert audit.outcome == "SUCCESS"


async def test_requeue_outbox_event_rejects_events_that_are_not_dead_lettered() -> None:
    organization_id = uuid4()
    event = _pending_outbox_event(organization_id=organization_id, status="PENDING")
    session = RequeueSession(event)
    context = SecurityContext(
        principal_id="ops-admin",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )

    with pytest.raises(HTTPException) as denied:
        await requeue_outbox_event(
            event.id,
            context,
            session,  # type: ignore[arg-type]
        )

    assert denied.value.status_code == 409
    assert session.timeline == []


# ---------------------------------------------------------------------------
# Fleet scheduling: org quota, per-source admission (backpressure), and
# priority-ordered admission -- the sub-claims of "Fleet scheduling" beyond
# maintenance windows that the original audit found untested.
# ---------------------------------------------------------------------------


class ReservationSession:
    """A fake session answering `reserve_analysis_run`'s fixed call sequence:
    `session.get` for the initial datasource snapshot, then four `session.scalar`
    calls in order (organization, locked datasource, org-active count, source-active
    count) -- mirroring the queue-of-preset-results pattern already used for
    `DeprecationSession` above.
    """

    def __init__(
        self, *, datasource_snapshot: DataSource, scalar_results: list[object]
    ) -> None:
        self.datasource_snapshot = datasource_snapshot
        self.scalar_results = list(scalar_results)
        self.added: list[object] = []

    async def get(self, _model: type[object], _identity: object) -> object:
        return self.datasource_snapshot

    async def scalar(self, _statement: object) -> object:
        return self.scalar_results.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


def _sample_org_and_datasource() -> tuple[Organization, DataSource]:
    organization_id = uuid4()
    organization = Organization(
        id=organization_id, name="Acme Bank", slug="acme-bank", status="ACTIVE"
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="Core ledger",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
    )
    return organization, datasource


def test_ensure_datasource_enabled_rejects_a_disabled_datasource() -> None:
    disabled = DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="Legacy warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="DISABLED",
    )

    with pytest.raises(RunAdmissionRejected, match="datasource is disabled"):
        ensure_datasource_enabled(disabled)


async def test_reserve_analysis_run_rejects_when_organization_quota_is_exhausted() -> None:
    organization, datasource = _sample_org_and_datasource()
    session = ReservationSession(
        datasource_snapshot=datasource,
        scalar_results=[organization, datasource, 2],
    )
    settings = Settings(max_active_runs_per_organization=2, _env_file=None)

    with pytest.raises(RunAdmissionRejected, match="organization analysis-run quota is exhausted"):
        await reserve_analysis_run(
            session,  # type: ignore[arg-type]
            settings,
            datasource_id=datasource.id,
            mode="INCREMENTAL",
            trigger_type="SCHEDULED",
            priority=70,
        )
    assert session.added == []


async def test_reserve_analysis_run_rejects_when_datasource_already_has_an_active_run() -> None:
    # The platform's backpressure / per-source admission control: at most one active
    # analysis run per datasource at a time, independent of the org-wide quota above.
    organization, datasource = _sample_org_and_datasource()
    session = ReservationSession(
        datasource_snapshot=datasource,
        scalar_results=[organization, datasource, 0, 1],
    )
    settings = Settings(max_active_runs_per_organization=100, _env_file=None)

    with pytest.raises(RunAdmissionRejected, match="datasource already has an active analysis run"):
        await reserve_analysis_run(
            session,  # type: ignore[arg-type]
            settings,
            datasource_id=datasource.id,
            mode="INCREMENTAL",
            trigger_type="SCHEDULED",
            priority=70,
        )
    assert session.added == []


async def test_reserve_analysis_run_admits_and_carries_the_requested_priority() -> None:
    organization, datasource = _sample_org_and_datasource()
    session = ReservationSession(
        datasource_snapshot=datasource,
        scalar_results=[organization, datasource, 0, 0],
    )
    settings = Settings(max_active_runs_per_organization=100, _env_file=None)

    run = await reserve_analysis_run(
        session,  # type: ignore[arg-type]
        settings,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        priority=83,
    )

    assert run.priority == 83
    assert run.organization_id == datasource.organization_id
    assert run.datasource_id == datasource.id
    assert session.added == [run]


def test_due_scan_policies_statement_orders_by_priority_then_next_run_at() -> None:
    settings = Settings(scheduler_batch_size=25, _env_file=None)

    statement = due_scan_policies_statement(settings, datetime(2026, 8, 29, tzinfo=UTC))

    compiled = " ".join(str(statement.compile()).split())
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "scan_policy.priority DESC" in order_by_clause
    # Priority is the primary sort key; next_run_at is only a tiebreaker within a
    # priority tier, so it must appear after priority in the ORDER BY clause.
    assert order_by_clause.index("priority DESC") < order_by_clause.index("next_run_at")


# ---------------------------------------------------------------------------
# CN-7 -- per-connector health scoring, integration-level.
#
# Runs the real `aida.fleet.datasource_health`/`fleet_health` aggregations and
# the real `aida.operational_api` endpoint functions against an in-memory
# SQLite database, following `tests/test_asset_evidence.py`'s own rationale:
# PostgreSQL is unreachable in this sandbox, but SQLite is a real SQL engine
# that enforces the same row semantics the ranked/windowed queries rely on.
# The scoring math itself is covered exhaustively, without a database, in
# `tests/test_connector_health.py`.
# ---------------------------------------------------------------------------

_HEALTH_SETTINGS = Settings(_env_file=None)


@pytest.fixture
async def health_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_health_datasource(
    session: AsyncSession, *, status: str = "ACTIVE"
) -> DataSource:
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
        name=f"src-{uuid4().hex[:8]}",
        connector_type="snowflake",
        dialect="snowflake",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        status=status,
        capabilities={},
    )
    session.add_all([org, lob, domain, project, datasource])
    await session.flush()
    return datasource


def _health_run(
    datasource: DataSource,
    *,
    status: str,
    error_class: str | None = None,
    discovered_tables: int = 10,
    profiled_tables: int = 10,
) -> AnalysisRun:
    return AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="INCREMENTAL",
        trigger_type="SCHEDULED",
        status=status,
        temporal_workflow_id=f"discovery-{datasource.id}-{uuid4()}",
        discovered_tables=discovered_tables,
        profiled_tables=profiled_tables,
        error_class=error_class,
    )


def _health_context(datasource: DataSource, **overrides: object) -> SecurityContext:
    return security_context(
        organization_id=datasource.organization_id,
        roles=frozenset({"PlatformAdmin"}),
        **overrides,
    )


async def test_datasource_health_reflects_run_history_end_to_end(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    health_session.add_all(
        [
            _health_run(datasource, status="COMPLETED"),
            _health_run(datasource, status="COMPLETED"),
        ]
    )
    await health_session.flush()

    score = await datasource_health(health_session, datasource.id)

    assert score is not None
    assert score.datasource_id == datasource.id
    assert score.status == "HEALTHY"
    assert len(score.factors) == 5
    factor_names = {factor.name for factor in score.factors}
    assert factor_names == {
        "RUN_SUCCESS_RATE",
        "STALENESS",
        "FAILURE_STREAK",
        "PROFILING_COVERAGE",
        "DATASOURCE_ENABLEMENT",
    }


async def test_datasource_health_surfaces_repeated_failures_as_a_blocker(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    health_session.add_all(
        [
            _health_run(datasource, status="FAILED", error_class="ConnectionTimeout"),
            _health_run(datasource, status="FAILED", error_class="ConnectionTimeout"),
            _health_run(datasource, status="FAILED", error_class="ConnectionTimeout"),
        ]
    )
    await health_session.flush()

    score = await datasource_health(health_session, datasource.id)

    assert score is not None
    assert score.status == "CRITICAL"
    assert "REPEATED_FAILURES" in score.blockers
    assert "NO_SUCCESSFUL_RUN" in score.blockers


async def test_datasource_health_uses_scan_policy_interval_for_staleness(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    stale_success = _health_run(datasource, status="COMPLETED")
    health_session.add(stale_success)
    await health_session.flush()
    # Backdate the run so it reads as long overdue against a tight schedule.
    stale_success.created_at = datetime.now(UTC) - timedelta(days=10)
    stale_success.updated_at = datetime.now(UTC) - timedelta(days=10)
    health_session.add(
        ScanPolicy(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            enabled=True,
            interval_minutes=60,
            mode="INCREMENTAL",
            priority=50,
            next_run_at=datetime.now(UTC),
            created_by="test",
        )
    )
    await health_session.commit()

    score = await datasource_health(health_session, datasource.id)

    assert score is not None
    staleness = next(f for f in score.factors if f.name == "STALENESS")
    assert staleness.evidence["scan_interval_minutes"] == 60
    assert staleness.score == 0.0


async def test_datasource_health_returns_none_for_missing_datasource(health_session) -> None:
    assert await datasource_health(health_session, uuid4()) is None


async def test_datasource_health_unknown_when_never_run(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    await health_session.commit()

    score = await datasource_health(health_session, datasource.id)

    assert score is not None
    assert score.status == "UNKNOWN"
    assert "NO_RUN_HISTORY" in score.blockers


async def test_fleet_health_covers_every_datasource_without_n_plus_one_shape(
    health_session,
) -> None:
    healthy = await _seed_health_datasource(health_session)
    # Second datasource shares the same organization so both are returned by
    # one fleet_health() call.
    org_id = healthy.organization_id
    disabled = DataSource(
        id=uuid4(),
        organization_id=org_id,
        line_of_business_id=healthy.line_of_business_id,
        data_domain_id=healthy.data_domain_id,
        project_id=healthy.project_id,
        name=f"src-{uuid4().hex[:8]}",
        connector_type="oracle",
        dialect="oracle",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN_2",
        status="DISABLED",
        capabilities={},
    )
    health_session.add(disabled)
    health_session.add(_health_run(healthy, status="COMPLETED"))
    await health_session.commit()

    scores = await fleet_health(health_session, org_id)

    assert {score.datasource_id for score in scores} == {healthy.id, disabled.id}
    disabled_score = next(s for s in scores if s.datasource_id == disabled.id)
    assert "DATASOURCE_DISABLED" in disabled_score.blockers
    healthy_score = next(s for s in scores if s.datasource_id == healthy.id)
    assert healthy_score.status == "HEALTHY"


async def test_fleet_health_windows_run_history_per_datasource(health_session) -> None:
    from aida.connector_health import RUN_HISTORY_WINDOW

    datasource = await _seed_health_datasource(health_session)
    # More runs than the window: the oldest ones must not affect the score
    # via the failure-streak factor (they're outside the window entirely).
    for _ in range(RUN_HISTORY_WINDOW + 5):
        health_session.add(_health_run(datasource, status="FAILED"))
    health_session.add(_health_run(datasource, status="COMPLETED"))
    await health_session.commit()

    scores = await fleet_health(health_session, datasource.organization_id)
    single = await datasource_health(health_session, datasource.id)

    fleet_score = next(s for s in scores if s.datasource_id == datasource.id)
    # Both entry points see the same windowed history and agree.
    assert fleet_score.score == single.score
    assert fleet_score.blockers == single.blockers


async def test_get_datasource_health_endpoint_denies_cross_organization_access(
    health_session,
) -> None:
    datasource = await _seed_health_datasource(health_session)
    await health_session.commit()
    other_org_context = security_context(organization_id=uuid4(), roles=frozenset({"Viewer"}))

    with pytest.raises(HTTPException) as excinfo:
        await get_datasource_health(
            datasource.id, context=other_org_context, session=health_session
        )
    assert excinfo.value.status_code == 403


async def test_get_datasource_health_endpoint_404s_for_missing_datasource(health_session) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await get_datasource_health(
            uuid4(),
            context=security_context(organization_id=uuid4(), roles=frozenset({"PlatformAdmin"})),
            session=health_session,
        )
    assert excinfo.value.status_code == 404


async def test_get_datasource_health_endpoint_returns_explainable_factors(
    health_session,
) -> None:
    datasource = await _seed_health_datasource(health_session)
    health_session.add(_health_run(datasource, status="COMPLETED"))
    await health_session.commit()

    result = await get_datasource_health(
        datasource.id, context=_health_context(datasource), session=health_session
    )

    assert result.datasource_id == datasource.id
    assert result.status in {"HEALTHY", "DEGRADED", "CRITICAL", "UNKNOWN"}
    assert len(result.factors) == 5
    for factor in result.factors:
        assert factor.reason
        assert 0 <= factor.score <= factor.maximum


async def test_organization_fleet_health_endpoint_pages_every_datasource(
    health_session,
) -> None:
    datasource = await _seed_health_datasource(health_session)
    await health_session.commit()

    page = await organization_fleet_health(
        datasource.organization_id,
        context=_health_context(datasource),
        session=health_session,
    )

    assert page.total == 1
    assert page.items[0].datasource_id == datasource.id


# ---------------------------------------------------------------------------
# TL-6 -- tool-first execution rate metric.
#
# Runs the real `aida.fleet.tool_first_execution_rate` aggregation and the
# real `aida.operational_api.organization_tool_first_rate` endpoint against
# the same in-memory SQLite `health_session` fixture CN-7 already
# established above. The ratio math itself is covered exhaustively, without
# a database, in `tests/test_tool_first_rate.py`.
# ---------------------------------------------------------------------------


def _agent_run(
    datasource: DataSource,
    *,
    generation_source: str,
    status: str = "COMPLETED",
    created_at: datetime | None = None,
) -> AgentRun:
    run = AgentRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        principal_id="analyst-1",
        status=status,
        question_hash=uuid4().hex,
        generation_source=generation_source,
    )
    if created_at is not None:
        run.created_at = created_at
    return run


async def test_tool_first_execution_rate_computes_ratio_from_completed_runs(
    health_session,
) -> None:
    datasource = await _seed_health_datasource(health_session)
    health_session.add_all(
        [
            _agent_run(datasource, generation_source="GOVERNED_TOOL"),
            _agent_run(datasource, generation_source="GOVERNED_TOOL"),
            _agent_run(datasource, generation_source="MODEL_GATEWAY"),
        ]
    )
    await health_session.flush()

    result = await tool_first_execution_rate(health_session, datasource.organization_id)

    assert result.tool_first_executions == 2
    assert result.freeform_executions == 1
    assert result.total_executions == 3
    assert result.rate is not None and abs(result.rate - (2 / 3)) < 1e-3


async def test_tool_first_execution_rate_ignores_non_completed_runs(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    health_session.add_all(
        [
            _agent_run(datasource, generation_source="GOVERNED_TOOL"),
            _agent_run(datasource, generation_source="MODEL_GATEWAY", status="REJECTED"),
            _agent_run(datasource, generation_source="MODEL_GATEWAY", status="GENERATED"),
        ]
    )
    await health_session.flush()

    result = await tool_first_execution_rate(health_session, datasource.organization_id)

    assert result.total_executions == 1
    assert result.rate == 1.0


async def test_tool_first_execution_rate_respects_the_rolling_window(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    health_session.add_all(
        [
            _agent_run(datasource, generation_source="GOVERNED_TOOL", created_at=now),
            _agent_run(
                datasource,
                generation_source="MODEL_GATEWAY",
                created_at=now - timedelta(days=200),
            ),
        ]
    )
    await health_session.flush()

    result = await tool_first_execution_rate(
        health_session, datasource.organization_id, window_days=30, now=now
    )

    assert result.total_executions == 1
    assert result.rate == 1.0


async def test_tool_first_execution_rate_no_runs_returns_none_rate(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    await health_session.flush()

    result = await tool_first_execution_rate(health_session, datasource.organization_id)

    assert result.total_executions == 0
    assert result.rate is None
    assert result.meets_target is None


async def test_tool_first_execution_rate_is_scoped_to_the_organization(health_session) -> None:
    datasource = await _seed_health_datasource(health_session)
    other_datasource = await _seed_health_datasource(health_session)
    health_session.add_all(
        [
            _agent_run(datasource, generation_source="GOVERNED_TOOL"),
            _agent_run(other_datasource, generation_source="MODEL_GATEWAY"),
        ]
    )
    await health_session.flush()

    result = await tool_first_execution_rate(health_session, datasource.organization_id)

    assert result.total_executions == 1
    assert result.tool_first_executions == 1


async def test_organization_tool_first_rate_endpoint_returns_explainable_breakdown(
    health_session,
) -> None:
    datasource = await _seed_health_datasource(health_session)
    health_session.add_all(
        [
            _agent_run(datasource, generation_source="GOVERNED_TOOL"),
            _agent_run(datasource, generation_source="GOVERNED_TOOL"),
            _agent_run(datasource, generation_source="DEVELOPMENT_OVERRIDE"),
        ]
    )
    await health_session.commit()

    result = await organization_tool_first_rate(
        datasource.organization_id,
        window_days=DEFAULT_WINDOW_DAYS,
        context=_health_context(datasource),
        session=health_session,
    )

    assert result.organization_id == datasource.organization_id
    assert result.tool_first_executions == 2
    assert result.freeform_executions == 1
    assert result.total_executions == 3
    assert result.by_source == {"DEVELOPMENT_OVERRIDE": 1, "GOVERNED_TOOL": 2}
    assert result.rate is not None and abs(result.rate - (2 / 3)) < 1e-3


async def test_organization_tool_first_rate_endpoint_denies_cross_organization_access(
    health_session,
) -> None:
    datasource = await _seed_health_datasource(health_session)
    await health_session.commit()
    other_org_context = security_context(organization_id=uuid4(), roles=frozenset({"Operations"}))

    with pytest.raises(HTTPException) as excinfo:
        await organization_tool_first_rate(
            datasource.organization_id, context=other_org_context, session=health_session
        )
    assert excinfo.value.status_code == 403
