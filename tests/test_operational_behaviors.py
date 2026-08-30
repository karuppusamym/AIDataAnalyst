from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida.config import Settings
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled, reserve_analysis_run
from aida.models import AnalysisRun, AuditEvent, DataSource, Organization, OutboxEvent, ScanPolicy
from aida.operational_api import requeue_outbox_event
from aida.projectors import graph_projector
from aida.projectors.outbox_publisher import record_publish_failure, retry_delay_seconds
from aida.security import SecurityContext, enforce_organization
from aida.workflows import scheduler
from aida.workflows.scheduler import due_scan_policies_statement


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
