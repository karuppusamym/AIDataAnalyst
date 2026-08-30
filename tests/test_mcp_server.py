"""
Unit tests for the Atlas MCP resource handlers (src/aida/mcp_server.py).

This repo has no DB-integration test harness: there is no test database and
no fixture that spins up real Postgres-backed sessions. Tests here instead
build hand-crafted SQLAlchemy model instances (never persisted) and drive
`_handle_resources_read` against a minimal fake `AsyncSession` that returns
those instances in place of real query results.
"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from aida.mcp_server import _handle_resources_read
from aida.models import MetadataCatalog, MetadataSchema, MetadataTable, TableProfile
from aida.security_types import SecurityContext


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class FakeSession:
    """Stands in for AsyncSession: returns canned results regardless of the
    statement passed in, since these tests only need to exercise handler
    logic, not real query construction."""

    def __init__(self, *, catalog_row, columns, latest_profile):
        self._catalog_row = catalog_row
        self._columns = columns
        self._latest_profile = latest_profile

    async def execute(self, _statement):
        return _FakeResult(self._catalog_row)

    async def scalars(self, _statement):
        return _FakeScalars(self._columns)

    async def scalar(self, _statement):
        return self._latest_profile


def _build_catalog_row(organization_id):
    datasource_id = uuid4()
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        name="prod-warehouse",
        status="ACTIVE",
        fingerprint="cat-fp",
    )
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization_id,
        catalog_id=catalog.id,
        name="analytics",
        status="ACTIVE",
        fingerprint="schema-fp",
    )
    table = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        schema_id=schema.id,
        name="orders",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="table-fp",
    )
    return table, schema, catalog, datasource_id


def _read(context, session, datasource_id, schema_name, table_name):
    uri = f"atlas://catalog/{datasource_id}/{schema_name}/{table_name}"
    result = asyncio.run(_handle_resources_read({"uri": uri}, session, context))
    return json.loads(result["contents"][0]["text"])


def test_catalog_read_reports_row_count_estimate_from_table_profile() -> None:
    organization_id = uuid4()
    context = SecurityContext(
        principal_id="test-principal",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Viewer"}),
    )
    table, schema, catalog, datasource_id = _build_catalog_row(organization_id)
    profile = TableProfile(
        id=uuid4(),
        organization_id=organization_id,
        analysis_run_id=uuid4(),
        datasource_id=datasource_id,
        table_id=table.id,
        row_count_estimate=48213,
        sampled_row_count=5000,
        status="COMPLETED",
        created_at=datetime.now(UTC),
    )
    session = FakeSession(
        catalog_row=(table, schema, catalog),
        columns=[],
        latest_profile=profile,
    )

    payload = _read(context, session, datasource_id, schema.name, table.name)

    assert payload["row_count_estimate"] == 48213


def test_catalog_read_reports_null_row_count_when_no_profile_exists() -> None:
    organization_id = uuid4()
    context = SecurityContext(
        principal_id="test-principal",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Viewer"}),
    )
    table, schema, catalog, datasource_id = _build_catalog_row(organization_id)
    session = FakeSession(
        catalog_row=(table, schema, catalog),
        columns=[],
        latest_profile=None,
    )

    payload = _read(context, session, datasource_id, schema.name, table.name)

    assert payload["row_count_estimate"] is None
