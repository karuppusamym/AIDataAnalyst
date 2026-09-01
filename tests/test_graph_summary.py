"""Behavioral coverage for `get_graph_summary` (`aida.api`) -- the endpoint that
reconciles the Neo4j metadata-graph projection against the authoritative
Postgres counts. This is the one handler in the codebase that talks to a live
graph driver directly, so unlike everything else closed this session it can't
be tested by handing it a fake SQLAlchemy session alone -- the `AsyncGraphDatabase`
driver construction has to be faked too. Before this file, the endpoint had no
test coverage of any kind: not the projection-lag math, not the CURRENT/LAGGING/
NOT_PROJECTED status logic, and not the "graph unreachable" error path.
"""

from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

import aida.api as api_module
from aida.api import get_graph_summary
from aida.config import Settings
from aida.models import DataSource
from aida.security import SecurityContext

# --- Fakes for the Neo4j driver and the SQLAlchemy session ------------------


class _FakeGraphRecord:
    def __init__(self, values: dict[str, int]) -> None:
        self.records = [values]


class _FakeGraphDriver:
    def __init__(self, values: dict[str, int] | None = None, *, fails: bool = False) -> None:
        self._values = values
        self._fails = fails
        self.closed = False

    async def execute_query(self, _query: str, **_kwargs: object) -> _FakeGraphRecord:
        if self._fails:
            raise RuntimeError("neo4j connection refused")
        assert self._values is not None
        return _FakeGraphRecord(self._values)

    async def close(self) -> None:
        self.closed = True


class _FakeGraphDatabase:
    """Stands in for `neo4j.AsyncGraphDatabase` -- `.driver(...)` is called as
    a bound method on the module-level object, exactly like the real class
    method, and always hands back the one fake driver under test.
    """

    def __init__(self, driver: _FakeGraphDriver) -> None:
        self._driver = driver

    def driver(self, *_args: object, **_kwargs: object) -> _FakeGraphDriver:
        return self._driver


class _GraphSummarySession:
    """`.get()` returns the datasource; `.scalar()` pops the five authoritative
    Postgres counts (catalogs, schemas, tables, columns, constraints) in the
    exact order `get_graph_summary` awaits them.
    """

    def __init__(self, *, datasource: DataSource, scalar_results: list[int]) -> None:
        self._datasource = datasource
        self._scalar_queue = list(scalar_results)

    async def get(self, _model: type[object], _identity: object) -> DataSource:
        return self._datasource

    async def scalar(self, _statement: object) -> int:
        return self._scalar_queue.pop(0)


def _sample_datasource(*, organization_id: Any) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="core-banking",
        connector_type="POSTGRES",
        dialect="postgres",
        environment="PRODUCTION",
        credential_reference="vault://core-banking",
        status="ACTIVE",
    )


def _viewer_context(*, organization_id: Any) -> SecurityContext:
    return SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Analyst"}),
    )


# --- Tests --------------------------------------------------------------


async def test_graph_summary_is_current_when_the_projection_matches_postgres(
    monkeypatch: Any,
) -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    driver = _FakeGraphDriver(
        {
            "catalogs": 1,
            "schemas": 2,
            "tables": 5,
            "columns": 20,
            "sensitive_columns": 3,
            "constraints": 4,
            "foreign_key_relationships": 2,
        }
    )
    monkeypatch.setattr(api_module, "AsyncGraphDatabase", _FakeGraphDatabase(driver))
    session = _GraphSummarySession(
        datasource=datasource, scalar_results=[1, 2, 5, 20, 4]  # matches the graph exactly
    )

    summary = await get_graph_summary(
        datasource.id,
        _viewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    assert summary.projection_status == "CURRENT"
    assert summary.projection_lag == {
        "catalogs": 0,
        "schemas": 0,
        "tables": 0,
        "columns": 0,
        "constraints": 0,
    }
    assert driver.closed is True


async def test_graph_summary_is_lagging_when_the_graph_has_not_caught_up_to_postgres(
    monkeypatch: Any,
) -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    driver = _FakeGraphDriver(
        {
            "catalogs": 1,
            "schemas": 2,
            "tables": 3,  # Postgres has 5; the graph hasn't ingested 2 new tables yet
            "columns": 20,
            "sensitive_columns": 3,
            "constraints": 4,
            "foreign_key_relationships": 2,
        }
    )
    monkeypatch.setattr(api_module, "AsyncGraphDatabase", _FakeGraphDatabase(driver))
    session = _GraphSummarySession(
        datasource=datasource, scalar_results=[1, 2, 5, 20, 4]
    )

    summary = await get_graph_summary(
        datasource.id,
        _viewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    assert summary.projection_status == "LAGGING"
    assert summary.projection_lag["tables"] == 2
    assert summary.projection_lag["schemas"] == 0  # only the lagging dimension is nonzero


async def test_graph_summary_is_not_projected_when_the_graph_has_no_catalog_node(
    monkeypatch: Any,
) -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    driver = _FakeGraphDriver(
        {
            "catalogs": 0,
            "schemas": 0,
            "tables": 0,
            "columns": 0,
            "sensitive_columns": 0,
            "constraints": 0,
            "foreign_key_relationships": 0,
        }
    )
    monkeypatch.setattr(api_module, "AsyncGraphDatabase", _FakeGraphDatabase(driver))
    session = _GraphSummarySession(
        datasource=datasource, scalar_results=[1, 2, 5, 20, 4]  # Postgres has real data
    )

    summary = await get_graph_summary(
        datasource.id,
        _viewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    # Zero catalogs takes priority over the lag computation, even though every
    # dimension is technically "behind" -- an unprojected graph is a distinct
    # state from a merely-lagging one.
    assert summary.projection_status == "NOT_PROJECTED"


async def test_graph_summary_clamps_lag_to_zero_when_the_graph_is_ahead_of_postgres(
    monkeypatch: Any,
) -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    driver = _FakeGraphDriver(
        {
            "catalogs": 1,
            "schemas": 2,
            "tables": 5,  # graph still has rows for tables Postgres already deleted
            "columns": 20,
            "sensitive_columns": 3,
            "constraints": 4,
            "foreign_key_relationships": 2,
        }
    )
    monkeypatch.setattr(api_module, "AsyncGraphDatabase", _FakeGraphDatabase(driver))
    session = _GraphSummarySession(
        datasource=datasource, scalar_results=[1, 2, 2, 20, 4]  # Postgres now only has 2 tables
    )

    summary = await get_graph_summary(
        datasource.id,
        _viewer_context(organization_id=organization_id),
        session,  # type: ignore[arg-type]
        Settings(_env_file=None),
    )

    # Negative "lag" (graph ahead of Postgres, e.g. pending tombstone cleanup)
    # must clamp to zero, not go negative -- and must not by itself count as lag.
    assert summary.projection_lag["tables"] == 0
    assert summary.projection_status == "CURRENT"


async def test_graph_summary_returns_503_and_still_closes_the_driver_when_neo4j_is_unreachable(
    monkeypatch: Any,
) -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    driver = _FakeGraphDriver(fails=True)
    monkeypatch.setattr(api_module, "AsyncGraphDatabase", _FakeGraphDatabase(driver))
    session = _GraphSummarySession(datasource=datasource, scalar_results=[1, 2, 5, 20, 4])

    with pytest.raises(HTTPException) as failure:
        await get_graph_summary(
            datasource.id,
            _viewer_context(organization_id=organization_id),
            session,  # type: ignore[arg-type]
            Settings(_env_file=None),
        )

    assert failure.value.status_code == 503
    # The driver must be closed even when the query blows up.
    assert driver.closed is True
