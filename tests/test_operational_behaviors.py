from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida.config import Settings
from aida.fleet import RunAdmissionRejected
from aida.models import AnalysisRun, AuditEvent, OutboxEvent, ScanPolicy
from aida.projectors import graph_projector
from aida.security import SecurityContext, enforce_organization
from aida.workflows import scheduler


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
