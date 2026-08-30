"""Full-endpoint integration coverage for LN-5 (column-level dbt manifest lineage).

`import_dbt_manifest` (the actual `POST /dbt-projects/{id}/artifact-imports` handler)
is called directly, following this repo's established convention for exercising
async endpoint functions against a hand-built in-memory `AsyncSession` double rather
than real HTTP/DB infrastructure -- see `tests/test_dbt_run_results_integration.py`
(`FakeAsyncSession`, copied here with the pieces this test needs) and
`tests/test_dbt_quality_bridge.py`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import uuid4

import pytest

from aida.dbt_api import import_dbt_manifest
from aida.models import (
    DataSource,
    DbtArtifactImport,
    DbtLineageEdge,
    DbtProject,
    DbtResource,
    OrganizationIntegrationPolicy,
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
    """Minimal in-memory AsyncSession double for the dbt artifact-import endpoint.

    Copied (trimmed to what this test needs -- no `_catalog_matches` three-entity
    join, since these fixtures never populate a metadata catalog) from
    `tests/test_dbt_run_results_integration.py`; see that module's docstring for
    the full rationale.
    """

    def __init__(self) -> None:
        self._store: dict[type, dict[Any, Any]] = defaultdict(dict)
        self.added: list[Any] = []
        self.committed = False

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
        # `_catalog_matches`' MetadataTable/MetadataSchema/MetadataCatalog join is
        # the only multi-entity query on this path. No catalog fixtures are
        # seeded here, so this always yields zero rows -- kept for parity with
        # `tests/test_dbt_run_results_integration.py`'s double rather than
        # special-casing it away.
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
    """`customer_summary` refs `stg_customers`; compiled SQL is column-qualified
    (`c.id`, `c.name`) so `sql_lineage_parser` can attribute each column to its
    source table -- see the note in `tests/test_dbt_column_lineage.py`."""
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.10.0",
            "generated_at": "2026-08-30T12:00:00Z",
            "invocation_id": "ln5-integration-fixture",
        },
        "nodes": {
            "model.bank.stg_customers": {
                "resource_type": "model",
                "package_name": "bank",
                "name": "stg_customers",
                "alias": "stg_customers",
                "database": "analytics",
                "schema": "staging",
                "relation_name": '"analytics"."staging"."stg_customers"',
                "original_file_path": "models/staging/stg_customers.sql",
                "config": {"materialized": "view"},
                "columns": {},
                "tags": [],
                "depends_on": {"nodes": []},
                "compiled_code": "SELECT r.id, r.name FROM raw.customers AS r",
            },
            "model.bank.customer_summary": {
                "resource_type": "model",
                "package_name": "bank",
                "name": "customer_summary",
                "alias": "customer_summary",
                "database": "bank",
                "schema": "analytics",
                "relation_name": '"bank"."analytics"."customer_summary"',
                "original_file_path": "models/customer_summary.sql",
                "config": {"materialized": "table"},
                "columns": {},
                "tags": [],
                "depends_on": {"nodes": ["model.bank.stg_customers"]},
                "compiled_code": (
                    "SELECT c.id AS customer_id, c.name "
                    "FROM analytics.staging.stg_customers AS c"
                ),
            },
        },
    }


class _Scenario:
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

    async def import_manifest(self) -> DbtArtifactImport:
        body = DbtArtifactImportRequest(manifest=manifest_fixture(), catalog=None, run_results=None)
        return await import_dbt_manifest(
            self.dbt_project.id,
            body,
            context=self.context,
            session=self.session,
        )


@pytest.mark.asyncio
async def test_import_persists_both_table_and_column_level_edges() -> None:
    scenario = _Scenario()

    artifact = await scenario.import_manifest()

    resources = {r.unique_id: r for r in scenario.session.added_of(DbtResource)}
    stg = resources["model.bank.stg_customers"]
    summary = resources["model.bank.customer_summary"]

    all_edges = scenario.session.added_of(DbtLineageEdge)
    assert all(edge.artifact_import_id == artifact.id for edge in all_edges)

    table_edges = [e for e in all_edges if e.edge_type == "DEPENDS_ON"]
    column_edges = [e for e in all_edges if e.edge_type == "COLUMN_DEPENDS_ON"]

    # --- table-level edge (unchanged LN-1 behaviour) ---
    assert len(table_edges) == 1
    assert table_edges[0].source_resource_id == stg.id
    assert table_edges[0].target_resource_id == summary.id
    assert table_edges[0].source_column == ""
    assert table_edges[0].target_column == ""
    assert table_edges[0].transformation_type is None
    assert table_edges[0].confidence is None

    # --- column-level edges (LN-5) ---
    assert len(column_edges) == 2
    by_target_column = {e.target_column: e for e in column_edges}
    assert set(by_target_column) == {"customer_id", "name"}

    customer_id_edge = by_target_column["customer_id"]
    assert customer_id_edge.source_resource_id == stg.id
    assert customer_id_edge.target_resource_id == summary.id
    assert customer_id_edge.source_column == "id"
    assert customer_id_edge.transformation_type == "DIRECT"
    assert customer_id_edge.confidence == "FULL"

    name_edge = by_target_column["name"]
    assert name_edge.source_resource_id == stg.id
    assert name_edge.target_resource_id == summary.id
    assert name_edge.source_column == "name"

    # `lineage_edge_count` deliberately keeps its pre-LN-5 meaning (table-level
    # dependency edges only) -- see the LN-5 delivery note for why.
    assert artifact.lineage_edge_count == 1


@pytest.mark.asyncio
async def test_no_column_edges_when_dependency_is_unresolvable() -> None:
    """A resource whose declared dependency doesn't appear in its own compiled
    SQL (e.g. a stale/renamed ref) gets its table-level edge but no fabricated
    column edges."""
    scenario = _Scenario()
    manifest = manifest_fixture()
    manifest["nodes"]["model.bank.customer_summary"]["compiled_code"] = (
        "SELECT o.id AS customer_id FROM other_system.unrelated_table AS o"
    )
    body = DbtArtifactImportRequest(manifest=manifest, catalog=None, run_results=None)

    artifact = await import_dbt_manifest(
        scenario.dbt_project.id, body, context=scenario.context, session=scenario.session
    )

    all_edges = scenario.session.added_of(DbtLineageEdge)
    assert len([e for e in all_edges if e.edge_type == "DEPENDS_ON"]) == 1
    assert [e for e in all_edges if e.edge_type == "COLUMN_DEPENDS_ON"] == []
    assert artifact.lineage_edge_count == 1


@pytest.mark.asyncio
async def test_no_column_edges_when_compiled_sql_is_unparseable() -> None:
    scenario = _Scenario()
    manifest = manifest_fixture()
    manifest["nodes"]["model.bank.customer_summary"]["compiled_code"] = "SELECT FROM ("
    body = DbtArtifactImportRequest(manifest=manifest, catalog=None, run_results=None)

    await import_dbt_manifest(
        scenario.dbt_project.id, body, context=scenario.context, session=scenario.session
    )

    resources = {r.unique_id: r for r in scenario.session.added_of(DbtResource)}
    assert resources["model.bank.customer_summary"].sql_parse_status == "UNPARSEABLE"

    column_edges = [
        e
        for e in scenario.session.added_of(DbtLineageEdge)
        if e.edge_type == "COLUMN_DEPENDS_ON"
    ]
    assert column_edges == []
