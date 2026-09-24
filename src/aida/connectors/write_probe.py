"""R11-MP22: whether the source account this platform connects as can write.

The query gateway refuses writes by parsing every statement, and PostgreSQL also
runs each read in a read-only transaction. Every other engine relies on the parse
and on the account's grants, so an account that can write is a control resting
on one layer.

Kept apart from the connectors on purpose: a probe of the *account* is not a
connector capability, and the connectors' capability certification
(`capability_certification.json`) fingerprints their files, so adding it there
would have invalidated certification evidence the probe does not affect.

Each probe is one fixed, parameter-free catalog query -- like `test_connection`'s
`SELECT 1`, it accepts no SQL. Engines without a probe answer `NOT_PROBED`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

import asyncpg

from aida.connectors.base import Connector
from aida.connectors.postgres import PostgresConnector
from aida.connectors.sqlserver import SqlServerConnector


@dataclass(frozen=True, slots=True)
class WritePrivilegeProbe:
    """`checked` is False where no probe exists for the engine; `detail` names what
    was found, never a value."""

    checked: bool
    can_write: bool | None = None
    detail: str = ""


NOT_PROBED: Final = WritePrivilegeProbe(
    checked=False, detail="no write-privilege probe for this engine"
)

#: Database CREATE, CREATE on any user schema, or any of INSERT, UPDATE, DELETE,
#: TRUNCATE on any user table or partitioned table. `has_table_privilege` with
#: several privileges is true if any one is held.
POSTGRES_WRITE_PROBE: Final = """
SELECT
    has_database_privilege(current_database(), 'CREATE') AS database_create,
    EXISTS (
        SELECT 1 FROM pg_namespace n
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp%'
          AND has_schema_privilege(n.oid, 'CREATE')
    ) AS schema_create,
    EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
    ) AS table_write
"""

#: Database-level INSERT, UPDATE, DELETE or ALTER, or membership of db_owner,
#: db_datawriter or db_ddladmin. Object-level grants are not checked. Needed here
#: in particular: the connection's `readonly=True` sets the TDS read-only
#: application intent, a routing hint that refuses no write.
SQLSERVER_WRITE_PROBE: Final = """
SELECT
    HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS database_insert,
    HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE') AS database_update,
    HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE') AS database_delete,
    HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER') AS database_alter,
    IS_ROLEMEMBER('db_owner') AS db_owner,
    IS_ROLEMEMBER('db_datawriter') AS db_datawriter,
    IS_ROLEMEMBER('db_ddladmin') AS db_ddladmin
"""


def _from_row(row: dict[str, Any] | None) -> WritePrivilegeProbe:
    if not row:
        return WritePrivilegeProbe(checked=True, can_write=None, detail="no answer")
    found = sorted(name for name, value in row.items() if value)
    return WritePrivilegeProbe(
        checked=True, can_write=bool(found), detail=", ".join(found) if found else "read-only"
    )


async def _probe_postgres(connector: PostgresConnector) -> WritePrivilegeProbe:
    connection = await asyncpg.connect(connector._dsn, command_timeout=connector._command_timeout)
    try:
        row = await connection.fetchrow(POSTGRES_WRITE_PROBE)
    finally:
        await connection.close()
    return _from_row(dict(row) if row is not None else None)


def probe_sqlserver_sync(connector: SqlServerConnector) -> WritePrivilegeProbe:
    connection = connector._connect(timeout_seconds=connector._command_timeout, autocommit=True)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(SQLSERVER_WRITE_PROBE)
            row = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()
    return _from_row(dict(row) if row else None)


async def probe_write_privileges(connector: Connector) -> WritePrivilegeProbe:
    """Probe the connected account, where this engine has a probe."""
    if isinstance(connector, PostgresConnector):
        return await _probe_postgres(connector)
    if isinstance(connector, SqlServerConnector):
        return await asyncio.to_thread(probe_sqlserver_sync, connector)
    return NOT_PROBED
