"""R11-MP24: governed PostgreSQL reads borrow from a bounded pool per source.

Unit tests cover the switch and the one retry on a connection the server has
closed; the live tests (set `AIDA_POSTGRES_POOL_TEST_DATABASE_URL` to a
throwaway server) prove reuse, isolation between borrows and a fresh pool for a
changed credential.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest

import aida.connectors.postgres as postgres_module
from aida.connectors import postgres_pool
from aida.connectors.execution_access import with_pooled_reads
from aida.connectors.postgres import PostgresConnector
from tests.support.doubles import FakeSqlExecutor


def test_the_gateway_switch_pools_only_postgres() -> None:
    connector = PostgresConnector("postgresql://u:p@h/db")
    pooled = with_pooled_reads(connector, enabled=True)
    assert isinstance(pooled, PostgresConnector) and pooled._pooled_reads
    assert not connector._pooled_reads
    assert with_pooled_reads(connector, enabled=False) is connector
    other = FakeSqlExecutor(())
    assert with_pooled_reads(other, enabled=True) is other  # type: ignore[arg-type]


def test_the_pool_key_never_holds_the_dsn() -> None:
    key = postgres_pool._key("postgresql://user:hunter2@host/db", 60)
    assert "hunter2" not in key and len(key) == 64
    assert key != postgres_pool._key("postgresql://user:rotated@host/db", 60)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Connection:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail

    def transaction(self, *, readonly: bool) -> _Transaction:
        assert readonly
        return _Transaction()

    async def execute(self, _sql: str) -> None:
        if self.fail is not None:
            raise self.fail

    async def fetchval(self, _sql: str) -> int:
        return 4242

    async def fetch(self, _sql: str) -> list[dict[str, int]]:
        return [{"n": 1}]


def _borrow_sequence(monkeypatch: pytest.MonkeyPatch, connections: list[_Connection]) -> list[int]:
    borrowed: list[int] = []

    @asynccontextmanager
    async def _borrow(_dsn: str, *, command_timeout: float) -> AsyncIterator[_Connection]:
        borrowed.append(int(command_timeout))
        yield connections.pop(0)

    monkeypatch.setattr(postgres_module, "borrow", _borrow)
    return borrowed


async def test_a_connection_the_server_closed_is_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrowed = _borrow_sequence(
        monkeypatch,
        [
            _Connection(asyncpg.exceptions.ConnectionDoesNotExistError("closed")),
            _Connection(),
        ],
    )
    connector = PostgresConnector("postgresql://u:p@h/db").with_pooled_reads()
    result = await connector.execute_read_query("SELECT 1 AS n", timeout_seconds=9)
    assert result.rows == ({"n": 1},)
    assert result.warehouse_query_id == "postgres-backend:4242"
    assert borrowed == [9, 9]


async def test_a_second_stale_connection_or_any_other_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = asyncpg.exceptions.ConnectionDoesNotExistError("closed")
    _borrow_sequence(monkeypatch, [_Connection(stale), _Connection(stale)])
    connector = PostgresConnector("postgresql://u:p@h/db").with_pooled_reads()
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await connector.execute_read_query("SELECT 1", timeout_seconds=9)

    borrowed = _borrow_sequence(
        monkeypatch, [_Connection(asyncpg.exceptions.ReadOnlySQLTransactionError("ro"))]
    )
    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        await connector.execute_read_query("SELECT 1", timeout_seconds=9)
    assert borrowed == [9]


async def test_an_unpooled_connector_still_opens_its_own_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unpooled read must not borrow")

    monkeypatch.setattr(postgres_module, "borrow", _never)
    opened: list[float] = []
    closed: list[bool] = []

    class _Own(_Connection):
        async def close(self) -> None:
            closed.append(True)

    async def _connect(_dsn: str, *, command_timeout: float) -> _Own:
        opened.append(command_timeout)
        return _Own()

    monkeypatch.setattr(postgres_module.asyncpg, "connect", _connect)
    connector = PostgresConnector("postgresql://u:p@h/db")
    await connector.execute_read_query("SELECT 1", timeout_seconds=7)
    assert opened == [7] and closed == [True]


# ---------------------------------------------------------------------------
# Against a real PostgreSQL
# ---------------------------------------------------------------------------

_LIVE_URL = os.environ.get("AIDA_POSTGRES_POOL_TEST_DATABASE_URL")
live = pytest.mark.skipif(not _LIVE_URL, reason="set AIDA_POSTGRES_POOL_TEST_DATABASE_URL")


@live
async def test_live_reads_reuse_a_connection_and_leak_nothing_between_borrows() -> None:
    assert _LIVE_URL is not None
    connector = PostgresConnector(_LIVE_URL).with_pooled_reads()
    try:
        first = await connector.execute_read_query("SELECT 1 AS n", timeout_seconds=11)
        await connector.estimate_read_query("SELECT 1", timeout_seconds=11)
        second = await connector.execute_read_query(
            "SELECT current_setting('statement_timeout') AS t,"
            " current_setting('transaction_read_only') AS ro",
            timeout_seconds=11,
        )
        # One backend served all three: the connection was reused.
        assert first.warehouse_query_id == second.warehouse_query_id
        # Each borrow still runs read-only under its own statement timeout.
        assert second.rows == ({"t": "11s", "ro": "on"},)
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await connector.execute_read_query(
                "CREATE TABLE mp24_should_not_exist (id int)", timeout_seconds=11
            )
        # The refused write left nothing behind.
        plain = await asyncpg.connect(_LIVE_URL)
        try:
            assert await plain.fetchval("SELECT to_regclass('mp24_should_not_exist')") is None
        finally:
            await plain.close()
        assert postgres_pool.pool_count() == 1
    finally:
        await postgres_pool.close_postgres_pools()


@live
async def test_live_a_changed_credential_gets_its_own_pool() -> None:
    assert _LIVE_URL is not None
    role = f"mp24_{secrets.token_hex(3)}"
    password = secrets.token_hex(8)
    admin = await asyncpg.connect(_LIVE_URL)
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        parsed = _LIVE_URL.split("://", 1)[1].split("@", 1)[1]
        rotated = f"postgresql://{role}:{password}@{parsed}"
        await (
            PostgresConnector(_LIVE_URL)
            .with_pooled_reads()
            .execute_read_query("SELECT 1", timeout_seconds=5)
        )
        result = (
            await PostgresConnector(rotated)
            .with_pooled_reads()
            .execute_read_query("SELECT current_user AS u", timeout_seconds=5)
        )
        assert result.rows == ({"u": role},)
        assert postgres_pool.pool_count() == 2
    finally:
        await postgres_pool.close_postgres_pools()
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()
