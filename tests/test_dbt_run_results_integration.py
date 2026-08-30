"""
Full-endpoint integration coverage for LN-6 (dbt run_results.json operational evidence).

`import_dbt_manifest` (the actual `POST /dbt-projects/{id}/artifact-imports` handler) is
called directly -- following this repo's established convention (see
`tests/test_dbt_quality_bridge.py`) of exercising async endpoint functions with a hand-built
`SecurityContext` rather than spinning up real HTTP/DB infrastructure, which this repo does
not have a fixture for.

Because the endpoint performs a nontrivial sequence of `session.get` / `session.scalar` /
`session.execute` / `session.add` / `session.flush` / `session.commit` calls that must share
state, a bare `AsyncMock` is not enough. `FakeAsyncSession` below is a small in-memory
`AsyncSession` double, scoped to exactly the session operations this endpoint (and the
`_require_dbt_integration` / `_catalog_matches` / `reconcile_dbt_test_quality` helpers it
calls) actually issues:

- `add(obj)` records the object (assigning a uuid `id` if one was not already set, mimicking
  the ORM's python-side `default=uuid4` behavior that only fires on a real flush).
- `get(model, pk)` / `scalar(select(...))` / `execute(select(...))` answer by filtering the
  objects recorded so far (seeded fixtures + everything added during the call), using the
  compiled statement's `column_descriptions` (to find the mapped class(es)) and a small
  recursive walk of `whereclause` (to find `Table.column == value` equality filters). The
  three-entity join used by `_catalog_matches` is handled as a special case since it is the
  only multi-entity query in this code path.
- `flush()` / `commit()` / `refresh()` / `rollback()` are no-ops -- there is no real
  transaction to manage.

This keeps the double generic enough to answer every query the endpoint issues without
becoming a general ORM emulator.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import uuid4

import pytest

from aida.dbt_api import import_dbt_manifest
from aida.dbt_quality_bridge import dbt_incident_fingerprint
from aida.models import (
    AuditEvent,
    DataQualityIncident,
    DataSource,
    DbtArtifactImport,
    DbtProject,
    DbtResource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    OrganizationIntegrationPolicy,
    OutboxEvent,
)
from aida.schemas import DbtArtifactImportRequest
from aida.security import SecurityContext


class _ExecResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


def _equality_filters(whereclause: Any) -> list[tuple[str, str, Any]]:
    """Recursively pull (table_name, column_name, value) equalities out of a whereclause."""
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None)
    if clauses is not None:
        filters: list[tuple[str, str, Any]] = []
        for clause in clauses:
            filters.extend(_equality_filters(clause))
        return filters
    left = getattr(whereclause, "left", None)
    right = getattr(whereclause, "right", None)
    table = getattr(left, "table", None)
    col_name = getattr(left, "key", None) or getattr(left, "name", None)
    if left is None or right is None or table is None or col_name is None:
        return []
    value = getattr(right, "value", right)
    return [(table.name, col_name, value)]


class FakeAsyncSession:
    """Minimal in-memory AsyncSession double for the dbt artifact-import endpoint."""

    def __init__(self) -> None:
        self._store: dict[type, dict[Any, Any]] = defaultdict(dict)
        self.added: list[Any] = []
        self.committed = False

    # -- test setup helpers -------------------------------------------------
    def seed(self, obj: Any) -> Any:
        self._assign_id(obj)
        self._store[type(obj)][obj.id] = obj
        return obj

    def added_of(self, cls: type) -> list[Any]:
        return [obj for obj in self.added if isinstance(obj, cls)]

    @staticmethod
    def _assign_id(obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()

    # -- AsyncSession surface used by import_dbt_manifest / its helpers -----
    def add(self, obj: Any) -> None:
        self._assign_id(obj)
        self._store[type(obj)][obj.id] = obj
        self.added.append(obj)

    async def get(self, model: type, pk: Any) -> Any | None:
        return self._store.get(model, {}).get(pk)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def refresh(self, obj: Any) -> None:
        return None

    def _rows_for(self, stmt: Any) -> list[Any]:
        model = stmt.column_descriptions[0]["type"]
        filters = _equality_filters(stmt.whereclause)
        candidates = list(self._store.get(model, {}).values())
        table_name = model.__table__.name
        for filter_table, col, value in filters:
            if filter_table != table_name:
                continue
            candidates = [obj for obj in candidates if getattr(obj, col) == value]
        return candidates

    async def scalar(self, stmt: Any) -> Any | None:
        rows = self._rows_for(stmt)
        return rows[0] if rows else None

    async def scalars(self, stmt: Any) -> _ScalarsResult:
        return _ScalarsResult(self._rows_for(stmt))

    async def execute(self, stmt: Any) -> _ExecResult:
        entities = [d["type"] for d in stmt.column_descriptions]
        if len(entities) == 1:
            return _ExecResult([(row,) for row in self._rows_for(stmt)])
        # Special-cased: the only multi-entity query on this path is
        # `_catalog_matches`' MetadataTable/MetadataSchema/MetadataCatalog join.
        table_model, schema_model, catalog_model = entities
        filters = _equality_filters(stmt.whereclause)

        def matches(obj: Any, table_name: str) -> bool:
            return all(
                getattr(obj, col) == value
                for ftable, col, value in filters
                if ftable == table_name
            )

        rows = []
        for table in self._store.get(table_model, {}).values():
            if not matches(table, table_model.__table__.name):
                continue
            schema = self._store.get(schema_model, {}).get(table.schema_id)
            if schema is None or not matches(schema, schema_model.__table__.name):
                continue
            catalog = self._store.get(catalog_model, {}).get(schema.catalog_id)
            if catalog is None or not matches(catalog, catalog_model.__table__.name):
                continue
            rows.append((table, schema, catalog))
        return _ExecResult(rows)


def manifest_fixture() -> dict[str, Any]:
    """One MODEL depended on by two TESTs (one will pass, one will fail/error)."""
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.10.0",
            "generated_at": "2026-08-29T12:00:00Z",
            "invocation_id": "integration-fixture",
        },
        "nodes": {
            "model.bank.customer_summary": {
                "resource_type": "model",
                "package_name": "bank",
                "name": "customer_summary",
                "alias": "customer_summary",
                "database": "bank",
                "schema": "analytics",
                "relation_name": '"bank"."analytics"."customer_summary"',
                "original_file_path": "models/customer_summary.sql",
                "description": "Curated customer summary",
                "config": {"materialized": "table"},
                "columns": {
                    "customer_id": {
                        "name": "customer_id",
                        "description": "Surrogate primary key for customer",
                        "data_type": "integer",
                    }
                },
                "tags": ["customer", "certified"],
                "depends_on": {"nodes": []},
            },
            "test.bank.customer_summary_not_null": {
                "resource_type": "test",
                "package_name": "bank",
                "name": "not_null_customer_id",
                "depends_on": {"nodes": ["model.bank.customer_summary"]},
            },
            "test.bank.customer_balance_positive": {
                "resource_type": "test",
                "package_name": "bank",
                "name": "customer_balance_positive",
                "depends_on": {"nodes": ["model.bank.customer_summary"]},
            },
        },
    }


def run_results_fixture() -> dict[str, Any]:
    return {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/run_results/v4.json"},
        "results": [
            {
                "unique_id": "test.bank.customer_summary_not_null",
                "status": "pass",
                "failures": 0,
                "execution_time": 0.31,
                "message": None,
            },
            {
                "unique_id": "test.bank.customer_balance_positive",
                "status": "fail",
                "failures": 14,
                "execution_time": 0.82,
                "message": "Got 14 results, configured to fail if != 0",
            },
        ],
    }


class _Scenario:
    """Seeds a datasource + matching catalog table + dbt project into a FakeAsyncSession."""

    def __init__(self) -> None:
        self.organization_id = uuid4()
        self.session = FakeAsyncSession()

        self.session.seed(
            OrganizationIntegrationPolicy(
                organization_id=self.organization_id,
                transformation_metadata_integrations={
                    "dbt": True,
                    "openlineage": False,
                    "airflow": False,
                    "generic_elt": False,
                },
            )
        )

        self.datasource = self.session.seed(
            DataSource(
                organization_id=self.organization_id,
                line_of_business_id=uuid4(),
                project_id=uuid4(),
                name="bank-warehouse",
                connector_type="POSTGRES",
                dialect="postgres",
                environment="PROD",
                credential_reference="secret://bank-warehouse",
            )
        )

        # NOTE: these objects never go through a real flush, so SQLAlchemy's python-side
        # column defaults (e.g. status="ACTIVE") never fire -- pass them explicitly, since
        # `_catalog_matches` filters on `status == "ACTIVE"`.
        catalog = self.session.seed(
            MetadataCatalog(
                organization_id=self.organization_id,
                datasource_id=self.datasource.id,
                name="bank",
                status="ACTIVE",
                fingerprint="fp-catalog",
            )
        )
        schema = self.session.seed(
            MetadataSchema(
                organization_id=self.organization_id,
                catalog_id=catalog.id,
                name="analytics",
                status="ACTIVE",
                fingerprint="fp-schema",
            )
        )
        self.table = self.session.seed(
            MetadataTable(
                organization_id=self.organization_id,
                datasource_id=self.datasource.id,
                schema_id=schema.id,
                name="customer_summary",
                object_type="TABLE",
                status="ACTIVE",
                fingerprint="fp-table",
            )
        )

        self.dbt_project = self.session.seed(
            DbtProject(
                organization_id=self.organization_id,
                project_id=uuid4(),
                datasource_id=self.datasource.id,
                project_key="bank-dbt",
                display_name="Bank dbt project",
                target_name="prod",
                status="ACTIVE",
                created_by="dbt-bot@bank.internal",
            )
        )

        self.context = SecurityContext(
            principal_id="admin@bank.internal",
            principal_type="USER",
            roles=frozenset({"PlatformAdmin"}),
            organization_id=self.organization_id,
        )

    async def import_manifest(
        self, *, with_run_results: bool = True
    ) -> DbtArtifactImport:
        body = DbtArtifactImportRequest(
            manifest=manifest_fixture(),
            catalog=None,
            run_results=run_results_fixture() if with_run_results else None,
        )
        return await import_dbt_manifest(
            self.dbt_project.id,
            body,
            context=self.context,
            session=self.session,
        )


@pytest.mark.asyncio
async def test_import_dbt_manifest_persists_resources_and_test_evidence() -> None:
    scenario = _Scenario()

    artifact = await scenario.import_manifest()

    assert isinstance(artifact, DbtArtifactImport)
    assert artifact.model_count == 1
    assert artifact.test_count == 2
    assert artifact.resource_count == 3
    assert artifact.matched_resource_count == 1
    assert artifact.unmatched_resource_count == 0

    added_resources = {
        r.unique_id: r for r in scenario.session.added_of(DbtResource)
    }
    assert set(added_resources) == {
        "model.bank.customer_summary",
        "test.bank.customer_summary_not_null",
        "test.bank.customer_balance_positive",
    }

    model_resource = added_resources["model.bank.customer_summary"]
    assert model_resource.matched_table_id == scenario.table.id
    assert model_resource.test_status is None

    pass_test = added_resources["test.bank.customer_summary_not_null"]
    assert pass_test.test_status == "PASS"
    assert pass_test.test_failures == 0
    assert pass_test.test_execution_time == pytest.approx(0.31)

    fail_test = added_resources["test.bank.customer_balance_positive"]
    assert fail_test.test_status == "FAIL"
    assert fail_test.test_failures == 14
    assert fail_test.test_execution_time == pytest.approx(0.82)


@pytest.mark.asyncio
async def test_import_dbt_manifest_reconciles_run_results_into_quality_incidents() -> None:
    scenario = _Scenario()

    await scenario.import_manifest()

    incidents = scenario.session.added_of(DataQualityIncident)
    assert len(incidents) == 1
    incident = incidents[0]

    expected_fingerprint = dbt_incident_fingerprint(
        scenario.organization_id,
        scenario.datasource.id,
        scenario.table.id,
        "test.bank.customer_balance_positive",
    )
    assert incident.fingerprint == expected_fingerprint
    assert incident.status == "OPEN"
    assert incident.table_id == scenario.table.id
    assert incident.datasource_id == scenario.datasource.id
    assert incident.evidence["test_unique_id"] == "test.bank.customer_balance_positive"
    assert incident.evidence["failures"] == 14

    # No incident was opened for the passing test.
    no_incident_fingerprint = dbt_incident_fingerprint(
        scenario.organization_id,
        scenario.datasource.id,
        scenario.table.id,
        "test.bank.customer_summary_not_null",
    )
    assert no_incident_fingerprint != incident.fingerprint


@pytest.mark.asyncio
async def test_import_dbt_manifest_writes_audit_and_outbox_records() -> None:
    scenario = _Scenario()

    artifact = await scenario.import_manifest()

    audit_actions = {evt.action for evt in scenario.session.added_of(AuditEvent)}
    assert "dbt_artifact.import" in audit_actions
    assert "data_quality.incident.open" in audit_actions

    outbox_events = {evt.event_type for evt in scenario.session.added_of(OutboxEvent)}
    assert "dbt_artifact.imported.v1" in outbox_events
    assert "data_quality.incident_opened" in outbox_events

    import_event = next(
        evt
        for evt in scenario.session.added_of(OutboxEvent)
        if evt.event_type == "dbt_artifact.imported.v1"
    )
    assert import_event.payload["artifact_import_id"] == str(artifact.id)
    assert scenario.session.committed is True


@pytest.mark.asyncio
async def test_import_dbt_manifest_without_run_results_skips_quality_reconciliation() -> None:
    scenario = _Scenario()

    await scenario.import_manifest(with_run_results=False)

    added_resources = {
        r.unique_id: r for r in scenario.session.added_of(DbtResource)
    }
    for resource in added_resources.values():
        assert resource.test_status is None
    assert scenario.session.added_of(DataQualityIncident) == []


@pytest.mark.asyncio
async def test_import_dbt_manifest_is_idempotent_on_repeat_import() -> None:
    scenario = _Scenario()

    first = await scenario.import_manifest()
    resource_count_after_first = len(scenario.session.added_of(DbtResource))
    incident_count_after_first = len(scenario.session.added_of(DataQualityIncident))
    assert resource_count_after_first == 3
    assert incident_count_after_first == 1

    second = await scenario.import_manifest()

    assert second.id == first.id
    assert isinstance(second, DbtArtifactImport)
    # No new resources or incidents were created on the repeat, identical-fingerprint import.
    assert len(scenario.session.added_of(DbtResource)) == resource_count_after_first
    assert len(scenario.session.added_of(DataQualityIncident)) == incident_count_after_first
    assert len(scenario.session.added_of(DbtArtifactImport)) == 1


def test_reconciled_evidence_fingerprint_matches_bridge_helper() -> None:
    org_id = uuid4()
    ds_id = uuid4()
    table_id = uuid4()
    uid = "test.bank.customer_balance_positive"

    fp = dbt_incident_fingerprint(org_id, ds_id, table_id, uid)
    assert fp == dbt_incident_fingerprint(org_id, ds_id, table_id, uid)
    assert isinstance(fp, str)
    assert len(fp) == 64
