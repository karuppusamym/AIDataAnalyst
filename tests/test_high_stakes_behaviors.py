from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request

import aida.api as api_module
from aida.config import Settings
from aida.connectors.base import DiscoveredCatalog, DiscoveredTable
from aida.models import (
    AnalysisRun,
    AuditEvent,
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    OutboxEvent,
)
from aida.security import SecurityContext
from aida.stewardship_service import build_stewardship_coverage
from aida.workflows import discovery
from aida.workflows.activities import (
    ChangeTracker,
    SnapshotScope,
    _get_or_create_catalog,
    _get_or_create_table,
    deprecate_missing_snapshot,
    missing_snapshot_scope,
)


def test_stewardship_coverage_scores_actual_evidence_and_reports_unowned_tables() -> None:
    organization_id = uuid4()
    datasource_id = uuid4()
    first, second = uuid4(), uuid4()
    computed_at = datetime(2026, 8, 28, tzinfo=UTC)

    coverage = build_stewardship_coverage(
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=None,
        line_of_business_id=None,
        table_ids={first, second},
        evidence_sets={
            "documented": {first, second},
            "owned": {first},
            "classified": {second},
            "certified": set(),
            "quality_monitored": {first, second},
            "semantically_mapped": {first},
            "documented_but_out_of_scope": {uuid4()},
        },
        computed_at=computed_at,
    )

    assert coverage.table_count == 2
    assert coverage.dimensions["documented"].percentage == 100.0
    assert coverage.dimensions["owned"].percentage == 50.0
    assert coverage.dimensions["certified"].percentage == 0.0
    assert coverage.overall_score == 58.33
    assert coverage.unowned_table_ids == [second]
    assert coverage.computed_at == computed_at


def test_full_snapshot_reconciliation_tombstones_only_missing_inventory() -> None:
    observed_catalog, missing_catalog = uuid4(), uuid4()
    observed_schema, missing_schema = uuid4(), uuid4()
    observed_table, missing_table = uuid4(), uuid4()
    observed_column, missing_column = uuid4(), uuid4()
    observed_constraint, missing_constraint = uuid4(), uuid4()
    existing = SnapshotScope(
        catalog_ids={observed_catalog, missing_catalog},
        schema_ids={observed_schema, missing_schema},
        table_ids={observed_table, missing_table},
        column_ids={observed_column, missing_column},
        constraint_ids={observed_constraint, missing_constraint},
    )
    observed = SnapshotScope(
        catalog_ids={observed_catalog},
        schema_ids={observed_schema},
        table_ids={observed_table},
        column_ids={observed_column},
        constraint_ids={observed_constraint},
    )

    missing = missing_snapshot_scope(existing, observed)

    assert missing.catalog_ids == {missing_catalog}
    assert missing.schema_ids == {missing_schema}
    assert missing.table_ids == {missing_table}
    assert missing.column_ids == {missing_column}
    assert missing.constraint_ids == {missing_constraint}
    assert observed_table not in missing.table_ids


class DeprecationSession:
    def __init__(self, inventories: list[set[UUID]]) -> None:
        self.inventories = inventories
        self.statements: list[object] = []

    async def scalars(self, _statement: object) -> set[UUID]:
        return self.inventories.pop(0)

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return type("Result", (), {"rowcount": 1})()


async def test_catalog_tombstoning_executes_updates_for_each_missing_object_level() -> None:
    observed_ids = [uuid4() for _ in range(5)]
    missing_ids = [uuid4() for _ in range(5)]
    inventories = [
        {observed_ids[index], missing_ids[index]}
        for index in range(5)
    ]
    session = DeprecationSession(inventories)
    datasource = DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="Governed warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="restricted",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
        max_concurrency=2,
        capabilities={},
    )
    observed = SnapshotScope(
        catalog_ids={observed_ids[0]},
        table_ids={observed_ids[1]},
        schema_ids={observed_ids[2]},
        column_ids={observed_ids[3]},
        constraint_ids={observed_ids[4]},
    )

    deprecated = await deprecate_missing_snapshot(
        session,  # type: ignore[arg-type]
        datasource,
        observed,
    )

    assert deprecated == 5
    assert len(session.statements) == 5
    targeted: dict[str, set[UUID]] = {}
    for statement in session.statements:
        table_name = statement.table.name  # type: ignore[attr-defined]
        compiled = statement.compile()
        ids = {
            value
            for parameter in compiled.params.values()
            if isinstance(parameter, list | set | tuple)
            for value in parameter
            if isinstance(value, UUID)
        }
        targeted[table_name] = ids
    assert targeted == {
        "metadata_catalog": {missing_ids[0]},
        "metadata_schema": {missing_ids[2]},
        "metadata_table": {missing_ids[1]},
        "metadata_column": {missing_ids[3]},
        "metadata_constraint": {missing_ids[4]},
    }
    assert not any(observed_id in ids for ids in targeted.values() for observed_id in observed_ids)


class ReactivationSession:
    """A fake session whose `scalar()` returns a single, preset existing row.

    Mirrors the lookup-then-mutate shape of `_get_or_create_catalog`/`_get_or_create_table`:
    each calls `session.scalar(select(...))` exactly once to find (or miss) an existing row.
    """

    def __init__(self, existing: object) -> None:
        self.existing = existing
        self.added: list[object] = []

    async def scalar(self, _statement: object) -> object:
        return self.existing

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


def _sample_datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="Governed warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="restricted",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
        max_concurrency=2,
        capabilities={},
    )


async def test_rediscovered_catalog_reactivates_a_previously_tombstoned_record() -> None:
    datasource = _sample_datasource()
    existing_catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        name="warehouse",
        status="DEPRECATED",
        deprecated_at=datetime(2026, 8, 1, tzinfo=UTC),
        fingerprint="stale-fingerprint",
    )
    discovered = DiscoveredCatalog(name="warehouse", schemas=())
    tracker = ChangeTracker()
    session = ReactivationSession(existing_catalog)

    catalog = await _get_or_create_catalog(
        session,  # type: ignore[arg-type]
        datasource,
        discovered,
        tracker,
    )

    assert catalog is existing_catalog
    assert catalog.status == "ACTIVE"
    assert catalog.deprecated_at is None
    assert catalog.fingerprint != "stale-fingerprint"
    assert tracker.created == 0
    assert tracker.changed == 1
    assert session.added == []


async def test_rediscovered_table_reactivates_a_previously_tombstoned_record() -> None:
    datasource = _sample_datasource()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=datasource.organization_id,
        catalog_id=uuid4(),
        name="finance",
        status="ACTIVE",
        fingerprint="schema-fingerprint",
    )
    existing_table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="payments",
        object_type="TABLE",
        status="DEPRECATED",
        deprecated_at=datetime(2026, 8, 1, tzinfo=UTC),
        fingerprint="stale-fingerprint",
        source_description="old description",
    )
    discovered = DiscoveredTable(
        name="payments",
        object_type="TABLE",
        columns=(),
        source_description="current description",
    )
    tracker = ChangeTracker()
    session = ReactivationSession(existing_table)

    table = await _get_or_create_table(
        session,  # type: ignore[arg-type]
        datasource,
        schema,
        discovered,
        tracker,
    )

    assert table is existing_table
    assert table.status == "ACTIVE"
    assert table.deprecated_at is None
    assert table.source_description == "current description"
    assert table.fingerprint != "stale-fingerprint"
    assert tracker.created == 0
    assert tracker.changed == 1
    assert session.added == []


class RecordingSession:
    def __init__(self, previous: AnalysisRun) -> None:
        self.previous = previous
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def get(self, model: type[object], identity: UUID) -> AnalysisRun | None:
        if model is AnalysisRun and identity == self.previous.id:
            return self.previous
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.timeline.append("commit")


async def test_resume_creates_a_linked_run_and_emits_audit_and_outbox(monkeypatch: Any) -> None:
    organization_id = uuid4()
    datasource_id = uuid4()
    previous = AnalysisRun(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        mode="FULL",
        trigger_type="MANUAL",
        priority=83,
        status="FAILED",
        temporal_workflow_id=f"discovery-{datasource_id}-failed",
    )
    resumed = AnalysisRun(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        resumed_from_run_id=previous.id,
        mode="FULL",
        trigger_type="RESUME",
        priority=83,
        status="QUEUED",
        temporal_workflow_id=f"discovery-{datasource_id}-resumed",
    )
    session = RecordingSession(previous)
    reservation: dict[str, object] = {}

    async def reserve(_session: object, _settings: Settings, **kwargs: object) -> AnalysisRun:
        reservation.update(kwargs)
        return resumed

    async def submit(
        _request: Request, _session: object, _settings: Settings, submitted: AnalysisRun
    ) -> None:
        assert submitted is resumed
        session.timeline.append("submit")

    monkeypatch.setattr(api_module, "reserve_analysis_run", reserve)
    monkeypatch.setattr(api_module, "_submit_analysis_workflow", submit)
    context = SecurityContext(
        principal_id="metadata-admin",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"MetadataAdmin"}),
    )

    result = await api_module.resume_analysis_run(
        previous.id,
        Request({"type": "http", "app": object()}),
        context,
        session,  # type: ignore[arg-type]
        Settings(temporal_enabled=True, _env_file=None),
    )

    assert result is resumed
    assert reservation == {
        "datasource_id": datasource_id,
        "mode": "FULL",
        "trigger_type": "RESUME",
        "priority": 83,
        "resumed_from_run_id": previous.id,
    }
    assert session.timeline == ["commit", "submit"]
    audit = next(value for value in session.added if isinstance(value, AuditEvent))
    outbox = next(value for value in session.added if isinstance(value, OutboxEvent))
    assert audit.action == "analysis_run.resume"
    assert audit.details == {"resumed_from_run_id": str(previous.id)}
    assert outbox.event_type == "analysis_run.resumed.v1"
    assert outbox.payload["run_id"] == str(resumed.id)
    assert outbox.payload["resumed_from_run_id"] == str(previous.id)


async def test_discovery_workflow_heartbeats_retryable_stages_and_aggregates_profiles(
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, object, dict[str, object]]] = []

    async def execute_activity(name: str, argument: object, **kwargs: object) -> object:
        calls.append((name, argument, kwargs))
        if name == "plan_profile_tasks":
            return {"table_ids": ["table-1", "table-2", "table-3"], "max_concurrency": 2}
        if name == "profile_table_task":
            return {"profiled_tables": 1, "profiled_columns": 4}
        if name == "finalize_profile_tasks":
            return argument
        return {}

    monkeypatch.setattr(discovery.workflow, "execute_activity", execute_activity)

    result = await discovery.DatasourceDiscoveryWorkflow().run("run-123")

    assert result == {
        "run_id": "run-123",
        "profiled_tables": 3,
        "profiled_columns": 12,
    }
    heartbeat_calls = [
        call
        for call in calls
        if call[0] in {"discover_datasource", "plan_profile_tasks", "profile_table_task"}
    ]
    assert len([call for call in calls if call[0] == "profile_table_task"]) == 3
    assert all(call[2]["heartbeat_timeout"] == timedelta(seconds=30) for call in heartbeat_calls)
    assert all("retry_policy" in call[2] for call in heartbeat_calls)
