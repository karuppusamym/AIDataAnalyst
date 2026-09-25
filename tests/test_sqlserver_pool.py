"""R11-MP24: governed SQL Server execution borrows idle connections, narrowly.

Execution only (the EXPLAIN gate's `SET SHOWPLAN_XML` never touches a pooled connection), a
connection that saw an error is closed rather than returned, a dead idle one is retried once
before the statement runs, and idle ones expire. The live test (set
`AIDA_SQLSERVER_POOL_TEST_DSN`) proves reuse and read-only behaviour against a real engine.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any
from uuid import uuid4

import pytds
import pytest

import aida.connectors.sqlserver_pool as pool_module
from aida.connectors.sqlserver import SqlServerConnector
from aida.connectors.sqlserver_pool import close_sqlserver_pools, idle_count, pool_key


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def execute_scalar(self, sql: str) -> int:
        self.connection.statements.append(sql)
        if self.connection.dead:
            raise pytds.tds_base.ClosedConnectionError()
        return self.connection.spid

    def execute(self, sql: str) -> None:
        self.connection.statements.append(sql)
        if "boom" in sql:
            raise pytds.tds_base.ProgrammingError("bad statement")

    def fetchall(self) -> list[dict[str, int]]:
        return [{"n": 1}]

    def fetchone(self) -> dict[str, str]:
        return {"plan": "<ShowPlanXML/>"}

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, spid: int) -> None:
        self.spid = spid
        self.statements: list[str] = []
        self.rolled_back = 0
        self.closed = False
        self.dead = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_pools() -> Any:
    close_sqlserver_pools()
    yield
    close_sqlserver_pools()


def _connector(monkeypatch: pytest.MonkeyPatch) -> tuple[SqlServerConnector, list[_Connection]]:
    opened: list[_Connection] = []
    connector = SqlServerConnector(f"mssql://u:secret@host:1433/db_{uuid4().hex[:6]}")

    def connect(**_kwargs: Any) -> _Connection:
        opened.append(_Connection(spid=50 + len(opened)))
        return opened[-1]

    monkeypatch.setattr(connector, "_connect", connect)
    return connector.with_pooled_reads(), opened


def _run(connector: SqlServerConnector, sql: str = "SELECT 1 AS n") -> Any:
    return asyncio.run(connector.execute_read_query(sql, timeout_seconds=9))


def test_two_executions_share_one_connection_rolled_back_between(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, opened = _connector(monkeypatch)
    first, second = _run(connector), _run(connector)
    assert len(opened) == 1
    assert first.warehouse_query_id == second.warehouse_query_id == "sqlserver-spid:50"
    assert opened[0].rolled_back == 2 and not opened[0].closed


def test_a_connection_that_saw_an_error_is_closed_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, opened = _connector(monkeypatch)
    with pytest.raises(pytds.tds_base.ProgrammingError):
        _run(connector, "SELECT boom")
    assert opened[0].closed
    _run(connector)
    assert len(opened) == 2


def test_a_dead_idle_connection_is_retried_once_before_the_statement_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, opened = _connector(monkeypatch)
    _run(connector)
    opened[0].dead = True
    result = _run(connector, "SELECT 2 AS n")
    assert result.warehouse_query_id == "sqlserver-spid:51"
    assert opened[0].closed
    # The statement ran once, on the fresh connection only.
    assert "SELECT 2 AS n" not in opened[0].statements
    assert opened[1].statements.count("SELECT 2 AS n") == 1


def test_a_second_dead_connection_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, opened = _connector(monkeypatch)
    original = connector._connect

    def always_dead(**kwargs: Any) -> _Connection:
        connection = original(**kwargs)
        connection.dead = True
        return connection

    monkeypatch.setattr(connector, "_connect", always_dead)
    with pytest.raises(pytds.tds_base.ClosedConnectionError):
        _run(connector)
    assert all(connection.closed for connection in opened)


def test_an_idle_connection_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, opened = _connector(monkeypatch)
    now = [1_000.0]
    monkeypatch.setattr(pool_module.time, "monotonic", lambda: now[0])
    _run(connector)
    now[0] += pool_module.POOL_IDLE_SECONDS + 1
    _run(connector)
    assert len(opened) == 2 and opened[0].closed


def test_the_explain_gate_never_borrows(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, opened = _connector(monkeypatch)
    _run(connector)
    # The fake plan does not parse; only who connected matters here.
    with suppress(Exception):
        asyncio.run(connector.estimate_read_query("SELECT 1", timeout_seconds=9))
    assert len(opened) == 2
    assert "SET SHOWPLAN_XML ON" not in opened[0].statements
    assert opened[1].closed


def test_the_pool_key_never_holds_the_password() -> None:
    key = pool_key("host", 1433, "db", "u", "hunter2", 9)
    assert "hunter2" not in key and len(key) == 64
    assert key != pool_key("host", 1433, "db", "u", "rotated", 9)


def test_an_unpooled_connector_opens_and_closes_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, opened = _connector(monkeypatch)
    plain = SqlServerConnector("mssql://u:secret@host:1433/db")
    monkeypatch.setattr(plain, "_connect", connector._connect)
    asyncio.run(plain.execute_read_query("SELECT 1", timeout_seconds=9))
    assert opened[0].closed


# ---------------------------------------------------------------------------
# Against a real SQL Server
# ---------------------------------------------------------------------------

_LIVE_DSN = os.environ.get("AIDA_SQLSERVER_POOL_TEST_DSN")


@pytest.mark.skipif(not _LIVE_DSN, reason="set AIDA_SQLSERVER_POOL_TEST_DSN")
def test_live_executions_reuse_a_session_and_stay_read_only() -> None:
    assert _LIVE_DSN is not None
    connector = SqlServerConnector(_LIVE_DSN).with_pooled_reads()
    first = asyncio.run(connector.execute_read_query("SELECT 1 AS n", timeout_seconds=15))
    second = asyncio.run(
        connector.execute_read_query("SELECT @@TRANCOUNT AS open_transactions", timeout_seconds=15)
    )
    assert first.warehouse_query_id == second.warehouse_query_id
    # Each borrow starts clean: the previous one's work was rolled back.
    assert second.rows == ({"open_transactions": 1},) or second.rows == ({"open_transactions": 0},)
    # A failing statement closes its connection; the next borrows a fresh session.
    with pytest.raises(pytds.tds_base.Error):
        asyncio.run(
            connector.execute_read_query("SELECT * FROM no_such_table_mp24", timeout_seconds=15)
        )
    third = asyncio.run(connector.execute_read_query("SELECT 3 AS n", timeout_seconds=15))
    assert third.rows == ({"n": 3},)
    params = connector._params
    key = pool_key(params.host, params.port, params.database, params.user, params.password, 15)
    assert idle_count(key) == 1
