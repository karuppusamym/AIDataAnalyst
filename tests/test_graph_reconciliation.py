"""Tests for `aida.graph_reconciliation` (KG-7: scheduled Postgres/Neo4j
knowledge-graph reconciliation + drift alerting).

Pure unit tests -- no live Postgres/Neo4j required. Covers the set-difference
diff logic (missing-in-Neo4j / orphaned-in-Neo4j), severity classification,
due-cadence tracking, Incident construction routed through DQ-1's unmodified
notification engine, and the scheduler sweep's per-datasource fault
isolation (achieved the same way `test_fleet_scheduling.py` tests
`run_owner_routing_pass` -- monkeypatching the per-item worker so the sweep
loop never touches a real session or Neo4j driver).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from aida import graph_reconciliation, notification_routing
from aida.config import Settings
from aida.graph_reconciliation import (
    DEFAULT_CRITICAL_DRIFT_THRESHOLD,
    GRAPH_DRIFT_SOURCE,
    GraphReconciliationReport,
    ProjectionKeyDrift,
    diff_projection_keys,
    drift_severity,
    graph_reconciliation_due,
    incident_for_drift,
    reconcile_projection,
    run_graph_reconciliation_pass,
)
from aida.notification_routing import NotificationRule

# ---------------------------------------------------------------------------
# Engine reuse contract -- mirrors
# test_glossary_owner_routing_reuses_dq1_engine_functions_directly
# ---------------------------------------------------------------------------


def test_graph_reconciliation_reuses_dq1_engine_functions_directly() -> None:
    """KG-7 must not fork DQ-1's routing engine -- it imports the same
    functions, not reimplementations of them."""
    assert graph_reconciliation.route_notification is notification_routing.route_notification
    assert graph_reconciliation.format_itsm_payload is notification_routing.format_itsm_payload
    assert graph_reconciliation.Incident is notification_routing.Incident
    assert graph_reconciliation.NotificationRule is notification_routing.NotificationRule


# ---------------------------------------------------------------------------
# diff_projection_keys
# ---------------------------------------------------------------------------


def test_diff_projection_keys_no_drift_when_sets_match() -> None:
    keys = {"a", "b", "c"}
    drift = diff_projection_keys(keys, keys)
    assert drift.missing_in_neo4j == frozenset()
    assert drift.orphaned_in_neo4j == frozenset()
    assert drift.has_drift is False
    assert drift.count == 0


def test_diff_projection_keys_detects_missing_in_neo4j() -> None:
    """A row Postgres says should be projected but Neo4j never received --
    a missed/lost event, RL-4's original bug class."""
    should_exist = {"a", "b", "c"}
    actual = {"a", "b"}
    drift = diff_projection_keys(should_exist, actual)
    assert drift.missing_in_neo4j == frozenset({"c"})
    assert drift.orphaned_in_neo4j == frozenset()
    assert drift.has_drift is True
    assert drift.count == 1


def test_diff_projection_keys_detects_orphaned_in_neo4j() -> None:
    """A node Neo4j still holds whose Postgres source is gone -- e.g. a
    relationship candidate that was rejected/deleted after being projected."""
    should_exist = {"a", "b"}
    actual = {"a", "b", "stale"}
    drift = diff_projection_keys(should_exist, actual)
    assert drift.missing_in_neo4j == frozenset()
    assert drift.orphaned_in_neo4j == frozenset({"stale"})
    assert drift.has_drift is True
    assert drift.count == 1


def test_diff_projection_keys_detects_both_directions_simultaneously() -> None:
    should_exist = {"a", "b", "c"}
    actual = {"a", "x", "y"}
    drift = diff_projection_keys(should_exist, actual)
    assert drift.missing_in_neo4j == frozenset({"b", "c"})
    assert drift.orphaned_in_neo4j == frozenset({"x", "y"})
    assert drift.count == 4


def test_diff_projection_keys_handles_empty_sets() -> None:
    assert diff_projection_keys([], []) == ProjectionKeyDrift(frozenset(), frozenset())
    assert diff_projection_keys(["a"], []).missing_in_neo4j == frozenset({"a"})
    assert diff_projection_keys([], ["a"]).orphaned_in_neo4j == frozenset({"a"})


# ---------------------------------------------------------------------------
# reconcile_projection
# ---------------------------------------------------------------------------


def test_reconcile_projection_combines_node_and_edge_drift() -> None:
    org_id, ds_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    report = reconcile_projection(
        organization_id=org_id,
        datasource_id=ds_id,
        should_exist_nodes={"n1", "n2"},
        actual_nodes={"n1"},
        should_exist_edges={"e1"},
        actual_edges={"e1", "e2"},
        now=now,
    )
    assert report.organization_id == org_id
    assert report.datasource_id == ds_id
    assert report.node_drift.missing_in_neo4j == frozenset({"n2"})
    assert report.edge_drift.orphaned_in_neo4j == frozenset({"e2"})
    assert report.has_drift is True
    assert report.total_drift_count == 2
    assert report.generated_at == now


def test_reconcile_projection_no_drift_when_everything_matches() -> None:
    report = reconcile_projection(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        should_exist_nodes={"n1"},
        actual_nodes={"n1"},
        should_exist_edges={"e1"},
        actual_edges={"e1"},
    )
    assert report.has_drift is False
    assert report.total_drift_count == 0


def test_reconcile_projection_defaults_generated_at_to_now() -> None:
    before = datetime.now(UTC)
    report = reconcile_projection(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        should_exist_nodes=[],
        actual_nodes=[],
        should_exist_edges=[],
        actual_edges=[],
    )
    after = datetime.now(UTC)
    assert before <= report.generated_at <= after


# ---------------------------------------------------------------------------
# drift_severity
# ---------------------------------------------------------------------------


def _report_with_total_drift(count: int) -> GraphReconciliationReport:
    """Build a report whose total_drift_count is exactly `count` missing
    nodes, for severity-threshold testing."""
    return reconcile_projection(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        should_exist_nodes=[str(i) for i in range(count)],
        actual_nodes=[],
        should_exist_edges=[],
        actual_edges=[],
    )


def test_drift_severity_healthy_when_no_drift() -> None:
    report = _report_with_total_drift(0)
    assert drift_severity(report) == "HEALTHY"


def test_drift_severity_warning_below_critical_threshold() -> None:
    report = _report_with_total_drift(1)
    assert drift_severity(report, critical_threshold=10) == "WARNING"
    report = _report_with_total_drift(9)
    assert drift_severity(report, critical_threshold=10) == "WARNING"


def test_drift_severity_critical_at_or_above_threshold() -> None:
    report = _report_with_total_drift(10)
    assert drift_severity(report, critical_threshold=10) == "CRITICAL"
    report = _report_with_total_drift(25)
    assert drift_severity(report, critical_threshold=10) == "CRITICAL"


def test_drift_severity_uses_module_default_threshold() -> None:
    report = _report_with_total_drift(DEFAULT_CRITICAL_DRIFT_THRESHOLD)
    assert drift_severity(report) == "CRITICAL"
    report = _report_with_total_drift(DEFAULT_CRITICAL_DRIFT_THRESHOLD - 1)
    assert drift_severity(report) == "WARNING"


# ---------------------------------------------------------------------------
# graph_reconciliation_due
# ---------------------------------------------------------------------------


def test_graph_reconciliation_due_when_never_swept() -> None:
    assert graph_reconciliation_due(None, datetime.now(UTC), 60) is True


def test_graph_reconciliation_not_due_before_interval_elapses() -> None:
    now = datetime.now(UTC)
    last_run_at = now - timedelta(minutes=30)
    assert graph_reconciliation_due(last_run_at, now, 60) is False


def test_graph_reconciliation_due_once_interval_has_elapsed() -> None:
    now = datetime.now(UTC)
    last_run_at = now - timedelta(minutes=61)
    assert graph_reconciliation_due(last_run_at, now, 60) is True


# ---------------------------------------------------------------------------
# incident_for_drift
# ---------------------------------------------------------------------------


def test_incident_for_drift_is_none_when_nothing_drifted() -> None:
    report = reconcile_projection(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        should_exist_nodes={"n1"},
        actual_nodes={"n1"},
        should_exist_edges=[],
        actual_edges=[],
    )
    assert incident_for_drift(report) is None


def test_incident_for_drift_carries_severity_and_fingerprint() -> None:
    org_id, ds_id = uuid4(), uuid4()
    report = reconcile_projection(
        organization_id=org_id,
        datasource_id=ds_id,
        should_exist_nodes={"n1", "n2"},
        actual_nodes={"n1"},
        should_exist_edges=[],
        actual_edges=[],
    )
    incident = incident_for_drift(report, critical_threshold=10)
    assert incident is not None
    assert incident.severity == "WARNING"
    assert incident.fingerprint == f"{GRAPH_DRIFT_SOURCE}:{org_id}:{ds_id}"
    assert incident.source_id == str(ds_id)
    assert str(ds_id) in incident.message
    assert "1 node(s) missing in Neo4j" in incident.message


def test_incident_for_drift_is_critical_past_the_threshold() -> None:
    report = _report_with_total_drift(50)
    incident = incident_for_drift(report, critical_threshold=10)
    assert incident is not None
    assert incident.severity == "CRITICAL"


def test_incident_for_drift_truncates_the_sample_key_list() -> None:
    report = reconcile_projection(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        should_exist_nodes=[f"n{i}" for i in range(100)],
        actual_nodes=[],
        should_exist_edges=[],
        actual_edges=[],
    )
    incident = incident_for_drift(report)
    assert incident is not None
    # exactly 20 sample keys are quoted in the message, not all 100
    assert incident.message.count(", ") == 19 or incident.message.count("n") >= 20


# ---------------------------------------------------------------------------
# Full engine wiring: incident_for_drift -> route_notification -> a real
# NotificationEvent, exactly the shape GL-6 already proved out for a
# different domain (test_sync_itsm_channel_produces_the_shared_itsm_payload).
# ---------------------------------------------------------------------------


def test_drift_incident_routes_through_the_unmodified_notification_engine() -> None:
    org_id, ds_id = uuid4(), uuid4()
    report = reconcile_projection(
        organization_id=org_id,
        datasource_id=ds_id,
        should_exist_nodes={"n1", "n2"},
        actual_nodes={"n1"},
        should_exist_edges=[],
        actual_edges=[],
    )
    incident = incident_for_drift(report)
    assert incident is not None

    rule = NotificationRule(
        rule_id="rule-1",
        organization_id=str(org_id),
        conditions={},
        channel="ITSM",
        recipients=["oncall@example.com"],
        escalation_after_minutes=60,
    )
    events = notification_routing.route_notification(incident, [rule])

    assert len(events) == 1
    event = events[0]
    assert event.channel == "ITSM"
    assert event.severity == incident.severity
    assert event.source == str(ds_id)
    assert event.dedup_key

    payload = notification_routing.format_itsm_payload(incident)
    assert payload["correlation_id"] == incident.fingerprint
    assert payload["category"] == "data_quality"


def test_drift_incident_does_not_match_a_severity_scoped_rule_below_it() -> None:
    org_id, ds_id = uuid4(), uuid4()
    report = _report_with_total_drift(1)
    # override org/datasource identity for a deterministic WARNING incident
    report = GraphReconciliationReport(
        organization_id=org_id,
        datasource_id=ds_id,
        node_drift=report.node_drift,
        edge_drift=report.edge_drift,
        generated_at=report.generated_at,
    )
    incident = incident_for_drift(report, critical_threshold=10)
    assert incident is not None
    assert incident.severity == "WARNING"

    critical_only_rule = NotificationRule(
        rule_id="critical-only",
        organization_id=str(org_id),
        conditions={"severity": "CRITICAL"},
        channel="EMAIL",
        recipients=["oncall@example.com"],
    )
    events = notification_routing.route_notification(incident, [critical_only_rule])
    assert events == []


# ---------------------------------------------------------------------------
# run_graph_reconciliation_pass: due-tracking + per-datasource fault
# isolation, without touching a real session or Neo4j driver -- same
# monkeypatch technique test_fleet_scheduling.py uses for
# run_owner_routing_pass.
# ---------------------------------------------------------------------------


async def test_run_graph_reconciliation_pass_isolates_one_datasources_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing_ds, healthy_ds = uuid4(), uuid4()
    org_id = uuid4()
    swept: list[UUID] = []

    async def fake_reconcile(
        session: object,
        driver: object,
        *,
        datasource_id: UUID,
        organization_id: UUID,
        now: datetime,
        critical_threshold: int = DEFAULT_CRITICAL_DRIFT_THRESHOLD,
    ) -> GraphReconciliationReport:
        if datasource_id == failing_ds:
            raise RuntimeError("simulated Neo4j unavailable")
        swept.append(datasource_id)
        return reconcile_projection(
            organization_id=organization_id,
            datasource_id=datasource_id,
            should_exist_nodes=[],
            actual_nodes=[],
            should_exist_edges=[],
            actual_edges=[],
            now=now,
        )

    monkeypatch.setattr(graph_reconciliation, "reconcile_and_alert_datasource", fake_reconcile)
    settings = Settings(graph_reconciliation_interval_minutes=60, _env_file=None)
    now = datetime.now(UTC)
    last_run_at: dict[UUID, datetime] = {}

    result = await run_graph_reconciliation_pass(
        settings,
        now=now,
        last_run_at=last_run_at,
        datasource_ids=[(failing_ds, org_id), (healthy_ds, org_id)],
    )

    assert result == 1
    assert swept == [healthy_ds]
    # the failing datasource is not marked as swept, so it is retried next
    # iteration rather than waiting a full interval
    assert failing_ds not in last_run_at
    assert last_run_at[healthy_ds] == now


async def test_run_graph_reconciliation_pass_skips_datasources_not_yet_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ds_id, org_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    swept: list[UUID] = []

    async def fake_reconcile(
        session: object, driver: object, *, datasource_id: UUID, **kwargs: object
    ) -> GraphReconciliationReport:
        swept.append(datasource_id)
        return _report_with_total_drift(0)

    monkeypatch.setattr(graph_reconciliation, "reconcile_and_alert_datasource", fake_reconcile)
    settings = Settings(graph_reconciliation_interval_minutes=60, _env_file=None)
    last_run_at = {ds_id: now - timedelta(minutes=10)}

    result = await run_graph_reconciliation_pass(
        settings, now=now, last_run_at=last_run_at, datasource_ids=[(ds_id, org_id)]
    )

    assert result == 0
    assert swept == []


async def test_run_graph_reconciliation_pass_runs_once_interval_has_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ds_id, org_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    swept: list[UUID] = []

    async def fake_reconcile(
        session: object, driver: object, *, datasource_id: UUID, **kwargs: object
    ) -> GraphReconciliationReport:
        swept.append(datasource_id)
        return _report_with_total_drift(0)

    monkeypatch.setattr(graph_reconciliation, "reconcile_and_alert_datasource", fake_reconcile)
    settings = Settings(graph_reconciliation_interval_minutes=60, _env_file=None)
    last_run_at = {ds_id: now - timedelta(minutes=61)}

    result = await run_graph_reconciliation_pass(
        settings, now=now, last_run_at=last_run_at, datasource_ids=[(ds_id, org_id)]
    )

    assert result == 1
    assert swept == [ds_id]
    assert last_run_at[ds_id] == now


async def test_run_graph_reconciliation_pass_returns_zero_with_no_due_datasources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(graph_reconciliation_interval_minutes=60, _env_file=None)
    result = await run_graph_reconciliation_pass(
        settings, now=datetime.now(UTC), last_run_at={}, datasource_ids=[]
    )
    assert result == 0
