"""R11-FP02: the six real adapters route their facet queries through `read_facet`.

`tests/test_facet_refusal.py` proves the mechanism and its wiring through a connector
written for the purpose; `tests/test_facet_refusal_live.py` proves a real PostgreSQL
refusal is classified from the driver's own SQLSTATE. Between them the mechanism was
complete and unused: a grep for `read_facet` found it only in `connectors/discovery.py`,
so a real source's refusal still failed the run. These tests pin the adoption, per
engine, with each driver's own error shape.

Three things they have to keep honest at once:

* **A refused optional facet costs that facet.** The read returns no rows, the outcome
  is recorded against the facet, and the rest of the scan lands.
* **A refused inventory read still ends the run.** `RETIREMENT_BEARING_FACETS` holds
  `inventory`, so wrapping the roster buys a receipt entry and changes nothing else --
  a FULL run completing over zero objects would retire the estate.
* **Only a refusal is absorbed.** A dropped connection is recorded and re-raised, so a
  FULL run cannot reconcile against a source that stopped answering.

**And what each driver can actually report**, which is the part no fake may paper over.
`capability_states.is_permission_refusal` judges by structured codes and never by the
message, so an adapter's classification is only as good as its driver's error object.
Verified here against the installed drivers rather than assumed: asyncpg and
snowflake-connector expose a `sqlstate` field; databricks-sql-connector carries its
SQLSTATE in `exc.context["sqlState"]`; and `pytds`, `oracledb` and `google-api-core`
report none, but each carries the vendor's own code as a field -- SQL Server's error
`number`, Oracle's `_Error.code`, BigQuery's HTTP 403 with the structured reason
`accessDenied` -- which the classifier reads since R11-FP02's follow-through. The tests
below build each refusal with the driver's own error class and the code the vendor
really sends, and pair it with a failure of the same class that is *not* a refusal.

**Since the follow-through, only a refusal is absorbed on any of the six adapters.**
The four warehouse adapters used to absorb every failure of an optional axis; a
non-refusal now ends the run there too, and the tests below pin that alongside the
refusal each engine does absorb.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from aida.capability_states import (
    REASON_FACET_QUERY_FAILED,
    REASON_SOURCE_DENIED_READ,
    CapabilityState,
    is_permission_refusal,
)
from aida.connectors import bigquery, databricks, discovery, oracle, postgres, snowflake, sqlserver
from aida.connectors.discovery import (
    DISCOVERY_FACETS,
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INVENTORY,
    FACET_OBJECT_COMMENTS,
    FACET_ROUTINE_BODIES,
    FACET_TRIGGERS,
    FACET_VIEW_DEFINITIONS,
    FacetReadScope,
    facet_read_scope,
)

REFUSED = (CapabilityState.PERMISSION_DENIED, REASON_SOURCE_DENIED_READ)
UNAVAILABLE = (CapabilityState.UNAVAILABLE, REASON_FACET_QUERY_FAILED)


# ---------------------------------------------------------------------------
# Driver errors, each shaped like the driver it stands in for.
# ---------------------------------------------------------------------------


class _Refused(Exception):
    """A driver error that reports the SQLSTATE a refusal reports.

    Stands in for the two drivers that really do carry one (asyncpg's
    `PostgresError.sqlstate`, `snowflake.connector`'s `Error.sqlstate`) and, for
    the four that do not, for the day they grow one -- an adapter's wiring has to
    be provable independently of whether its driver can report the code today.
    The message quotes a row value on purpose: that is what a real driver's
    message does and what INV-6 forbids persisting.
    """

    sqlstate = "42501"

    def __init__(self) -> None:
        super().__init__("permission denied for relation customer where ssn = '123-45-6789'")


class _Dropped(Exception):
    """A failure that is not a refusal: no SQLSTATE, and the next read fails too."""


# ---------------------------------------------------------------------------
# PostgreSQL / asyncpg -- the engine a refusal is fully provable on.
# ---------------------------------------------------------------------------

_PG_ROSTER = [{"table_schema": "retail", "table_name": "customer", "table_type": "BASE TABLE"}]
_PG_COLUMNS = [
    {
        "table_schema": "retail",
        "table_name": "customer",
        "table_type": "BASE TABLE",
        "column_name": "id",
        "ordinal_position": 1,
        "data_type": "bigint",
        "is_nullable": "NO",
        "column_default": None,
    }
]
_PG_CONSTRAINTS = [
    {
        "table_schema": "retail",
        "table_name": "customer",
        "constraint_name": "customer_pkey",
        "constraint_type": "PRIMARY_KEY",
        "columns": ["id"],
        "referenced_schema": None,
        "referenced_table": None,
        "referenced_columns": None,
    }
]
_PG_GRANTS = [
    {
        "schema_name": "retail",
        "grantee": "reporting",
        "grantee_type": "ROLE",
        "privilege": "SELECT",
        "object_type": "TABLE",
        "object_name": "customer",
        "is_grantable": "NO",
    }
]


class _FakeAsyncpgConnection:
    """An asyncpg connection whose answer depends on the statement, not on call order.

    Keyed by a distinctive fragment of each query so a test names the *facet* it
    refuses rather than counting round trips -- and so this harness keeps working
    when a peer adds a query beside the ones under test.
    """

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = answers
        self.statements: list[str] = []

    def _answer(self, sql: str) -> Any:
        self.statements.append(sql)
        for fragment, answer in self.answers.items():
            if fragment in sql:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return []

    async def fetch(self, sql: str, *_arguments: Any) -> Any:
        return self._answer(sql)

    async def fetchval(self, sql: str) -> Any:
        rows = self._answer(sql)
        if not rows:
            return None
        first = rows[0]
        return next(iter(first.values())) if isinstance(first, dict) else first

    async def close(self) -> None:
        return None


def _patch_postgres(monkeypatch: pytest.MonkeyPatch, answers: Mapping[str, Any]) -> None:
    connection = _FakeAsyncpgConnection(answers)

    async def _connect(*_args: Any, **_kwargs: Any) -> _FakeAsyncpgConnection:
        return connection

    monkeypatch.setattr(postgres.asyncpg, "connect", _connect)


def _postgres_answers(**overrides: Any) -> dict[str, Any]:
    """The answers a healthy source gives, one per facet the adapter reads."""
    answers: dict[str, Any] = {
        "SELECT current_database()": [{"v": "bank"}],
        "FROM information_schema.tables t": _PG_ROSTER,
        "FROM information_schema.columns c": _PG_COLUMNS,
        "FROM pg_constraint con": _PG_CONSTRAINTS,
        "FROM information_schema.role_table_grants g": _PG_GRANTS,
    }
    answers.update(overrides)
    return answers


async def _postgres_batches(connector: postgres.PostgresConnector) -> list[Any]:
    return [batch async for batch in connector.discover_streaming(batch_size=10)]


async def test_postgres_a_refused_grants_read_costs_the_grants_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline, on the real adapter: the batch still arrives, with its table, its
    columns and its constraints, and the one facet the source refused is recorded."""
    _patch_postgres(
        monkeypatch,
        _postgres_answers(**{"FROM information_schema.role_table_grants g": _Refused()}),
    )
    connector = postgres.PostgresConnector("postgresql://u:p@h/bank")

    with facet_read_scope() as scope:
        batches = await _postgres_batches(connector)

    (catalogs,) = batches
    schema = catalogs[0].schemas[0]
    assert [table.name for table in schema.tables] == ["customer"]
    assert schema.tables[0].constraints[0].name == "customer_pkey"
    assert schema.grants == ()
    assert scope.outcomes == {FACET_GRANTS: REFUSED}


async def test_postgres_a_refused_roster_is_recorded_and_still_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The roster is the inventory, and the inventory is the run. Wrapping it does not
    make it optional -- `RETIREMENT_BEARING_FACETS` is what keeps the refusal fatal --
    it makes the INTERRUPTED receipt name the read that ended the run."""
    _patch_postgres(
        monkeypatch, _postgres_answers(**{"FROM information_schema.tables t": _Refused()})
    )
    connector = postgres.PostgresConnector("postgresql://u:p@h/bank")

    with facet_read_scope() as scope, pytest.raises(_Refused):
        await _postgres_batches(connector)

    assert scope.outcomes == {FACET_INVENTORY: REFUSED}


async def test_postgres_a_dropped_connection_is_never_absorbed_as_a_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure is not one facet's problem: the next read would fail too, and
    absorbing it would let a FULL run reconcile against a source that stopped
    answering. Recorded as UNAVAILABLE -- the under-claiming answer -- and re-raised."""
    _patch_postgres(
        monkeypatch,
        _postgres_answers(**{"FROM information_schema.role_table_grants g": _Dropped()}),
    )
    connector = postgres.PostgresConnector("postgresql://u:p@h/bank")

    with facet_read_scope() as scope, pytest.raises(_Dropped):
        await _postgres_batches(connector)

    assert scope.outcomes == {FACET_GRANTS: UNAVAILABLE}


async def test_postgres_discover_routes_the_same_facets_as_the_streaming_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`discover()` is a second, still-supported entry point (a small source, a
    connectivity check), and a refusal there must cost the same one facet. Two read
    paths with one adoption is exactly the drift this asserts against."""
    _patch_postgres(
        monkeypatch,
        _postgres_answers(**{"obj_description(n.oid, 'pg_namespace') AS description": _Refused()}),
    )
    connector = postgres.PostgresConnector("postgresql://u:p@h/bank")

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    assert [table.name for table in catalogs[0].schemas[0].tables] == ["customer"]
    assert scope.outcomes == {FACET_OBJECT_COMMENTS: REFUSED}


async def test_postgres_records_a_refused_trigger_read_and_finishes_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two native-object axes are now nameable, so they behave like the rest.

    This test was written the other way up: it asserted that `DISCOVERY_FACETS`
    named neither triggers nor sequences, and said it "is what will notice when the
    vocabulary grows". It noticed. Both names now live beside the other eight in
    `connectors.discovery` -- rather than in `discovery_receipt`, which is the drift
    that module's own comment warned about -- so a refused trigger read is recorded
    against its own facet and the scan completes, instead of taking the run down.
    """
    assert "triggers" in DISCOVERY_FACETS
    assert "sequences" in DISCOVERY_FACETS
    _patch_postgres(monkeypatch, _postgres_answers(**{"FROM pg_trigger t": _Refused()}))
    connector = postgres.PostgresConnector("postgresql://u:p@h/bank")

    with facet_read_scope() as scope:
        batches = await _postgres_batches(connector)

    # The refusal is attributed, and nothing else is.
    assert scope.outcomes == {FACET_TRIGGERS: REFUSED}
    # And the rest of the estate still came back -- one missing grant costs one
    # facet, which is the whole point of the mechanism.
    (catalogs,) = batches
    schema = catalogs[0].schemas[0]
    assert [table.name for table in schema.tables] == ["customer"]
    assert schema.tables[0].constraints[0].name == "customer_pkey"


# ---------------------------------------------------------------------------
# A DB-API cursor, shared by the three adapters that drive one.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """A DB-API cursor whose answer depends on the statement, not on call order."""

    description: Any = None

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self._rows: Any = []

    def execute(self, sql: str, *_arguments: Any) -> None:
        self.statements.append(sql)
        self._rows = []
        for fragment, answer in self.answers.items():
            if fragment in sql:
                if isinstance(answer, BaseException):
                    raise answer
                self._rows = answer
                return

    def fetchall(self) -> Any:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# SQL Server / pytds.
# ---------------------------------------------------------------------------

_MSSQL_COLUMNS = [
    {
        "table_schema": "retail",
        "table_name": "customer",
        "table_type": "BASE TABLE",
        "column_name": "id",
        "ordinal_position": 1,
        "data_type": "bigint",
        "is_nullable": "NO",
        "column_default": None,
    }
]


def _sqlserver_answers(**overrides: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "DB_NAME()": [{"catalog_name": "bank"}],
        "FROM INFORMATION_SCHEMA.COLUMNS c": _MSSQL_COLUMNS,
    }
    answers.update(overrides)
    return answers


def _patch_sqlserver(monkeypatch: pytest.MonkeyPatch, answers: Mapping[str, Any]) -> _FakeCursor:
    cursor = _FakeCursor(answers)
    monkeypatch.setattr(
        sqlserver.pytds, "connect", lambda **_kwargs: _FakeConnection(cursor)
    )
    return cursor


async def test_sqlserver_a_refused_view_definition_read_is_recorded_and_absorbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adoption itself, driver-independent: `pytds` is synchronous, so the read
    happens in a thread and its failure is replayed into `read_facet` by the coroutine
    that owns the scope. The facet is recorded and the table still arrives."""
    _patch_sqlserver(
        monkeypatch, _sqlserver_answers(**{"FROM sys.views v": _Refused()})
    )
    connector = sqlserver.SqlServerConnector("mssql://u:p@h:1433/bank")

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    table = catalogs[0].schemas[0].tables[0]
    assert table.name == "customer"
    assert table.view_definition is None
    assert scope.outcomes == {FACET_VIEW_DEFINITIONS: REFUSED}


def _pytds_error(number: int, text: str) -> Exception:
    """A `pytds` server error exactly as the driver builds one: the class it picks from
    the number, with `number` / `msg_no` set from the TDS ERROR token."""
    error = sqlserver.pytds.OperationalError(text)
    error.number = number
    error.msg_no = number
    error.severity = 14
    return error


async def test_sqlserver_error_229_is_a_refusal_read_from_the_error_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11-FP02 follow-through. A `pytds` error carries no `sqlstate` at all -- only the
    server's error `number`, a field. 229 ("the SELECT permission was denied on the
    object") is what a `DENY` on a catalog view answers, and the classifier now reads
    it, so the facet is PERMISSION_DENIED and the table still arrives. The live proof
    is `tests/test_facet_refusal_sqlserver_live.py`; this pins it without a server.
    """
    denied = _pytds_error(229, "The SELECT permission was denied on the object 'views'")
    assert not hasattr(denied, "sqlstate")
    assert is_permission_refusal(denied) is True

    _patch_sqlserver(monkeypatch, _sqlserver_answers(**{"FROM sys.views v": denied}))
    connector = sqlserver.SqlServerConnector("mssql://u:p@h:1433/bank")

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    assert catalogs[0].schemas[0].tables[0].name == "customer"
    assert scope.outcomes == {FACET_VIEW_DEFINITIONS: REFUSED}


async def test_sqlserver_an_error_number_that_is_not_a_refusal_still_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """208 is "invalid object name" -- a missing object, not a refusal -- and a `pytds`
    error built without a server number (a client-side failure) reads 0. Neither is a
    refusal, so each records UNAVAILABLE and re-raises: the under-claiming answer."""
    for failure in (
        _pytds_error(208, "Invalid object name 'sys.views'."),
        sqlserver.pytds.ProgrammingError("The SELECT permission was denied"),
    ):
        assert is_permission_refusal(failure) is False
        _patch_sqlserver(monkeypatch, _sqlserver_answers(**{"FROM sys.views v": failure}))
        connector = sqlserver.SqlServerConnector("mssql://u:p@h:1433/bank")

        with facet_read_scope() as scope, pytest.raises(type(failure)):
            await connector.discover()

        assert scope.outcomes == {FACET_VIEW_DEFINITIONS: UNAVAILABLE}


async def test_sqlserver_a_refused_roster_is_recorded_and_still_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as PostgreSQL's roster, and the reason the thread stops reading at a
    failed roster instead of running nine more doomed queries."""
    _patch_sqlserver(
        monkeypatch,
        _sqlserver_answers(**{"FROM INFORMATION_SCHEMA.COLUMNS c": _Refused()}),
    )
    connector = sqlserver.SqlServerConnector("mssql://u:p@h:1433/bank")

    with facet_read_scope() as scope, pytest.raises(_Refused):
        await connector.discover()

    assert scope.outcomes == {FACET_INVENTORY: REFUSED}


# ---------------------------------------------------------------------------
# Oracle / oracledb.
# ---------------------------------------------------------------------------

_ORACLE_COLUMNS = [
    ("BANK", "CUSTOMER", "BASE TABLE", "ID", 1, "NUMBER", "N", None),
]
_ORACLE_COLUMN_DESCRIPTION = (
    ("TABLE_SCHEMA",),
    ("TABLE_NAME",),
    ("TABLE_TYPE",),
    ("COLUMN_NAME",),
    ("ORDINAL_POSITION",),
    ("DATA_TYPE",),
    ("IS_NULLABLE",),
    ("COLUMN_DEFAULT",),
)


class _FakeOracleCursor:
    """An `oracledb` async cursor: `execute` and `fetchall` are separate awaits, and
    either half can be the one the source refuses."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self.description: Any = ()
        self._rows: Any = []

    async def execute(self, sql: str, *_arguments: Any) -> None:
        self.statements.append(sql)
        self._rows = []
        self.description = ()
        for fragment, answer in self.answers.items():
            if fragment in sql:
                if isinstance(answer, BaseException):
                    raise answer
                self.description, self._rows = answer
                return

    async def fetchall(self) -> Any:
        return self._rows

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    async def __aenter__(self) -> _FakeOracleCursor:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeOracleConnection:
    call_timeout = 0
    autocommit = False

    def __init__(self, cursor: _FakeOracleCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeOracleCursor:
        return self._cursor

    async def close(self) -> None:
        return None


def _oracle_answers(**overrides: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "SYS_CONTEXT": ((("CATALOG_NAME",),), [("BANK",)]),
        "FROM ALL_TAB_COLUMNS atc": (_ORACLE_COLUMN_DESCRIPTION, _ORACLE_COLUMNS),
    }
    answers.update(overrides)
    return answers


def _patch_oracle(monkeypatch: pytest.MonkeyPatch, answers: Mapping[str, Any]) -> None:
    cursor = _FakeOracleCursor(answers)

    async def _connect_async(**_kwargs: Any) -> _FakeOracleConnection:
        return _FakeOracleConnection(cursor)

    monkeypatch.setattr(oracle.oracledb, "connect_async", _connect_async)


def _oracle_connector() -> oracle.OracleConnector:
    return oracle.OracleConnector("oracle://u:p@h:1521/BANK")


async def test_oracle_a_refused_privilege_view_is_recorded_on_the_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ALL_TAB_PRIVS` is a dictionary view a least-privilege reader may not hold. The
    adapter already degraded to an empty axis with a reason; what adoption adds is the
    facet outcome, which is what keeps the reconciliation from retiring the grants an
    earlier, better-privileged run captured."""
    _patch_oracle(monkeypatch, _oracle_answers(**{"FROM ALL_TAB_PRIVS p": _Refused()}))

    with facet_read_scope() as scope:
        catalogs = await _oracle_connector().discover()

    assert catalogs[0].schemas[0].grants == ()
    assert scope.outcomes == {FACET_GRANTS: REFUSED}
    # The per-axis reason names the dictionary view and nothing the driver said: the
    # refusal's message quotes a row value, and none of it reaches the catalog (INV-6).
    reason = catalogs[0].attributes["envelope_v11_unavailable"]["grants"]
    assert "ALL_TAB_PRIVS" in reason
    assert "123-45-6789" not in reason
    assert "permission denied" not in reason


def _ora(code: int, text: str) -> Exception:
    """An `oracledb` server error exactly as the thin driver raises one: an `_Error`
    whose `code` is the ORA number, wrapped in the exception class it selects."""
    from oracledb import errors as oracledb_errors

    error = oracledb_errors._Error(text, code=code)
    return error.exc_type(error)


async def test_oracle_ora_01031_is_a_refusal_read_from_the_driver_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11-FP02 follow-through. `oracledb` reports no `sqlstate`; its `_Error.code` is
    the ORA number, a field the driver takes from the server's error packet (the
    `full_code` beside it is sliced out of the message, and is not read). ORA-01031 is
    a refusal anywhere, so the grants facet is PERMISSION_DENIED and absorbed."""
    denied = _ora(1031, "ORA-01031: insufficient privileges")
    assert not hasattr(denied, "sqlstate")
    assert is_permission_refusal(denied) is True

    _patch_oracle(monkeypatch, _oracle_answers(**{"FROM ALL_TAB_PRIVS p": denied}))

    with facet_read_scope() as scope:
        catalogs = await _oracle_connector().discover()

    assert catalogs[0].schemas[0].grants == ()
    assert scope.outcomes == {FACET_GRANTS: REFUSED}
    assert "grants" in catalogs[0].attributes["envelope_v11_unavailable"]


async def test_oracle_ora_00942_is_a_refusal_only_on_a_dictionary_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ORA-00942 decision. "Table or view does not exist" is also what Oracle says
    to a login holding no privilege on an object, so on its own it is *not* a refusal
    -- a genuinely missing table answers exactly the same. This adapter reads only
    `ALL_*` dictionary views, which exist on every release, so for its reads the
    missing half is ruled out and 00942 means the view was hidden from this login."""
    hidden = _ora(942, "ORA-00942: table or view does not exist")
    assert is_permission_refusal(hidden) is False
    assert is_permission_refusal(hidden, known_relations=True) is True

    _patch_oracle(monkeypatch, _oracle_answers(**{"FROM ALL_SOURCE": hidden}))

    with facet_read_scope() as scope:
        catalogs = await _oracle_connector().discover()

    assert scope.outcomes == {FACET_ROUTINE_BODIES: REFUSED}
    assert "routine_source" in catalogs[0].attributes["envelope_v11_unavailable"]


async def test_oracle_a_failure_that_is_not_a_refusal_now_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11-FP02 follow-through: the behaviour change. This adapter used to catch every
    failure of an envelope axis and turn it into a reason, so a dropped connection on
    `ALL_TAB_PRIVS` left an empty grants axis that the FULL reconciliation then read as
    "every grant was revoked". A failure no code identifies as a refusal -- a lost
    session (ORA-03113), or a driver error with no code at all -- is now recorded as
    UNAVAILABLE and re-raised, exactly as on PostgreSQL."""
    for failure in (
        _ora(3113, "ORA-03113: end-of-file on communication channel"),
        oracle.oracledb.DatabaseError("ORA-01031: insufficient privileges"),
    ):
        assert is_permission_refusal(failure, known_relations=True) is False
        _patch_oracle(monkeypatch, _oracle_answers(**{"FROM ALL_TAB_PRIVS p": failure}))

        with facet_read_scope() as scope, pytest.raises(type(failure)):
            await _oracle_connector().discover()

        assert scope.outcomes == {FACET_GRANTS: UNAVAILABLE}


async def test_oracle_a_refused_constraint_read_no_longer_costs_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ALL_CONSTRAINTS` was an unguarded read: one refusal there ended the scan. It is
    a facet like any other, and the table it belongs to still arrives without it."""
    _patch_oracle(monkeypatch, _oracle_answers(**{"FROM ALL_CONSTRAINTS ac": _Refused()}))

    with facet_read_scope() as scope:
        catalogs = await _oracle_connector().discover()

    table = catalogs[0].schemas[0].tables[0]
    assert table.name == "CUSTOMER"
    assert table.constraints == ()
    assert scope.outcomes == {FACET_CONSTRAINTS: REFUSED}


# ---------------------------------------------------------------------------
# Snowflake -- the second engine whose driver reports a SQLSTATE.
# ---------------------------------------------------------------------------

_SNOWFLAKE_COLUMNS = [
    {
        "table_schema": "PUBLIC",
        "table_name": "CUSTOMER",
        "table_type": "BASE TABLE",
        "column_name": "ID",
        "ordinal_position": 1,
        "data_type": "NUMBER(38,0)",
        "is_nullable": "NO",
        "column_default": None,
        "table_comment": None,
        "column_comment": None,
    }
]


async def test_snowflake_a_refused_show_grants_reaches_the_facet_not_just_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snowflake already recorded a refused `SHOW GRANTS` as a per-axis reason on the
    catalog. That reason is prose on one catalog's attributes; the facet outcome is what
    `footprint_gaps` counts and what `refused_facet_existing` reads, so both are kept --
    and the classification is real here, because this driver does report a SQLSTATE.
    """
    from snowflake.connector import errors as snowflake_errors

    # Snowflake's own error for this denial: error 3001, SQLSTATE 42501, and the driver
    # puts the code on the exception where `is_permission_refusal` reads it.
    denied = snowflake_errors.ProgrammingError(
        msg="Insufficient privileges to operate on schema 'PUBLIC'",
        errno=3001,
        sqlstate="42501",
    )
    assert is_permission_refusal(denied) is True

    cursor = _FakeCursor(
        {
            "FROM information_schema.columns c": _SNOWFLAKE_COLUMNS,
            "SHOW GRANTS ON SCHEMA": denied,
        }
    )
    connector = snowflake.SnowflakeConnector("snowflake://u:p@acc/TEST_DB/PUBLIC")
    monkeypatch.setattr(connector, "_get_connection", lambda: _FakeConnection(cursor))

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    assert catalogs[0].schemas[0].grants == ()
    assert scope.outcomes[FACET_GRANTS] == REFUSED
    assert "grants:PUBLIC" in catalogs[0].attributes["envelope_v11_unavailable"]


async def test_snowflake_a_refused_roster_is_recorded_and_still_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor({"FROM information_schema.columns c": _Refused()})
    connector = snowflake.SnowflakeConnector("snowflake://u:p@acc/TEST_DB/PUBLIC")
    monkeypatch.setattr(connector, "_get_connection", lambda: _FakeConnection(cursor))

    with facet_read_scope() as scope, pytest.raises(_Refused):
        await connector.discover()

    assert scope.outcomes == {FACET_INVENTORY: REFUSED}
    # The roster failed, so the thread stopped there rather than asking eleven more
    # questions of a source that had already said no to the only one that mattered.
    assert len(cursor.statements) == 1


# ---------------------------------------------------------------------------
# BigQuery -- an engine with no SQLSTATE at all.
# ---------------------------------------------------------------------------

_BQ_COLUMNS = [
    {
        "table_schema": "retail",
        "table_name": "customer",
        "table_type": "BASE TABLE",
        "column_name": "id",
        "ordinal_position": 1,
        "data_type": "INT64",
        "is_nullable": "NO",
        "column_default": None,
    }
]


class _FakeBigQueryRow:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self._row = row

    def items(self) -> Any:
        return self._row.items()


class _FakeBigQueryJob:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = rows

    def result(self) -> list[_FakeBigQueryRow]:
        return [_FakeBigQueryRow(row) for row in self._rows]


class _FakeBigQueryClient:
    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = answers
        self.statements: list[str] = []

    def query(self, sql: str, **_kwargs: Any) -> _FakeBigQueryJob:
        self.statements.append(sql)
        for fragment, answer in self.answers.items():
            if fragment in sql:
                if isinstance(answer, BaseException):
                    raise answer
                return _FakeBigQueryJob(answer)
        return _FakeBigQueryJob([])


def _bigquery_connector() -> bigquery.BigQueryConnector:
    return bigquery.BigQueryConnector(
        '{"auth_method": "workload_identity", "project_id": "bank-warehouse", '
        '"location": "US"}'
    )


def _bigquery_error(status: int, reason: str) -> Exception:
    """A BigQuery job error exactly as google-cloud-bigquery raises one: its reason
    mapped to an HTTP status, and the error result carried in `errors`."""
    from google.api_core import exceptions as google_exceptions

    return google_exceptions.from_http_status(
        status, "Access Denied: user lacks bigquery.routines.get", errors=[{"reason": reason}]
    )


async def test_bigquery_a_403_with_reason_access_denied_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11-FP02 follow-through. BigQuery does not speak SQLSTATE; a denial is `Forbidden`
    -- HTTP 403 -- carrying the structured reason `accessDenied`, which is what
    `google.cloud.bigquery.job.base._ERROR_REASON_TO_EXCEPTION` maps it from. Both fields
    are read, never the message, and the facet is PERMISSION_DENIED and absorbed."""
    denied = _bigquery_error(403, "accessDenied")
    assert not hasattr(denied, "sqlstate")
    assert is_permission_refusal(denied) is True

    client = _FakeBigQueryClient(
        {"INFORMATION_SCHEMA.COLUMNS": _BQ_COLUMNS, "INFORMATION_SCHEMA.VIEWS": denied}
    )
    connector = _bigquery_connector()
    monkeypatch.setattr(connector, "_get_client", lambda: client)

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    assert [table.name for table in catalogs[0].schemas[0].tables] == ["customer"]
    assert scope.outcomes == {FACET_VIEW_DEFINITIONS: REFUSED}


async def test_bigquery_a_403_that_is_not_a_denial_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 alone is not a refusal: BigQuery answers it for `quotaExceeded`,
    `billingNotEnabled` and `policyViolation` too, none of which a grant fixes -- and a
    `Forbidden` built with no reason at all says nothing either. Each records UNAVAILABLE
    and, since the follow-through, re-raises: the key read used to swallow every failure
    into an empty constraint list, which a FULL run then reconciled as "every key was
    dropped"."""
    from google.api_core import exceptions as google_exceptions

    for failure in (
        _bigquery_error(403, "quotaExceeded"),
        google_exceptions.Forbidden("denied"),
    ):
        assert is_permission_refusal(failure) is False
        client = _FakeBigQueryClient(
            {
                "INFORMATION_SCHEMA.COLUMNS": _BQ_COLUMNS,
                "INFORMATION_SCHEMA.KEY_COLUMN_USAGE": failure,
            }
        )
        connector = _bigquery_connector()
        monkeypatch.setattr(connector, "_get_client", lambda client=client: client)

        with facet_read_scope() as scope, pytest.raises(google_exceptions.Forbidden):
            await connector.discover()

        assert scope.outcomes == {FACET_CONSTRAINTS: UNAVAILABLE}


async def test_bigquery_a_refused_key_read_is_absorbed_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal the key read still survives -- no longer silently: the facet is
    recorded, so the reconciliation keeps the keys an earlier run captured."""
    client = _FakeBigQueryClient(
        {
            "INFORMATION_SCHEMA.COLUMNS": _BQ_COLUMNS,
            "INFORMATION_SCHEMA.KEY_COLUMN_USAGE": _bigquery_error(403, "accessDenied"),
        }
    )
    connector = _bigquery_connector()
    monkeypatch.setattr(connector, "_get_client", lambda: client)

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    assert catalogs[0].schemas[0].tables[0].constraints == ()
    assert scope.outcomes == {FACET_CONSTRAINTS: REFUSED}


# ---------------------------------------------------------------------------
# Databricks -- a driver that has the SQLSTATE and does not expose it as one.
# ---------------------------------------------------------------------------

_DATABRICKS_COLUMNS = [
    {
        "table_schema": "analytics",
        "table_name": "customer",
        "table_type": "BASE TABLE",
        "column_name": "id",
        "ordinal_position": 1,
        "data_type": "bigint",
        "is_nullable": "NO",
        "column_default": None,
        "table_comment": None,
        "column_comment": None,
    }
]
_DATABRICKS_DSN = (
    '{"server_hostname": "dbc.cloud.databricks.com", '
    '"http_path": "/sql/1.0/warehouses/abc", "access_token": "t", "catalog": "main"}'
)


async def test_databricks_refusal_classifies_from_the_sqlstate_in_its_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unity Catalog reports `42501`, and the classifier now reads it where it is.

    This test was written to record the opposite: the driver carries its SQLSTATE in
    `exc.context["sqlState"]` rather than in a `sqlstate` attribute, so a classifier
    walking attributes alone under-claimed a real refusal as UNAVAILABLE. The fix is
    still SQLSTATE and nothing else -- no message parsing, which INV-6 forbids because
    a driver message can quote a value -- only the place this driver puts it.
    """
    from databricks.sql import exc as databricks_exc

    denied = databricks_exc.ServerOperationError(
        "[INSUFFICIENT_PERMISSIONS] User does not have USE SCHEMA on analytics",
        {"sqlState": "42501"},
    )
    assert denied.context["sqlState"] == "42501"
    assert not hasattr(denied, "sqlstate")
    assert is_permission_refusal(denied) is True

    cursor = _FakeCursor(
        {
            ".information_schema.columns c": _DATABRICKS_COLUMNS,
            ".information_schema.views": denied,
        }
    )
    connector = databricks.DatabricksConnector(_DATABRICKS_DSN)
    monkeypatch.setattr(connector, "_get_connection", lambda: _FakeConnection(cursor))

    with facet_read_scope() as scope:
        catalogs = await connector.discover()

    assert [table.name for table in catalogs[0].schemas[0].tables] == ["customer"]
    # Recorded as the refusal it is, not as the "we did not get it" it used to be.
    assert scope.outcomes == {FACET_VIEW_DEFINITIONS: REFUSED}
    assert "views" in catalogs[0].attributes["envelope_v11_unavailable"]


async def test_databricks_a_refused_roster_is_recorded_and_still_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor({".information_schema.columns c": _Refused()})
    connector = databricks.DatabricksConnector(_DATABRICKS_DSN)
    monkeypatch.setattr(connector, "_get_connection", lambda: _FakeConnection(cursor))

    with facet_read_scope() as scope, pytest.raises(_Refused):
        await connector.discover()

    assert scope.outcomes == {FACET_INVENTORY: REFUSED}


# ---------------------------------------------------------------------------
# Across all six: no adapter may name a facet a receipt cannot publish.
# ---------------------------------------------------------------------------


#: The names `connectors.discovery` publishes for its facets. Filtering by *name* is
#: what keeps this from tripping over `connectors.base`'s `FACET_UNSUPPORTED` and
#: friends, which are the profile-facet vocabulary -- the same English words at a
#: different grain, as `capability_states`' docstring warns.
_DISCOVERY_FACET_NAMES = {
    name
    for name, value in vars(discovery).items()
    if name.startswith("FACET_") and isinstance(value, str) and value in DISCOVERY_FACETS
}
_ADAPTERS = [postgres, sqlserver, oracle, snowflake, bigquery, databricks]


def _adopted_facets(module: Any) -> set[str]:
    return {
        value for name, value in vars(module).items() if name in _DISCOVERY_FACET_NAMES
    }


@pytest.mark.parametrize("module", _ADAPTERS)
def test_no_adapter_names_a_facet_outside_the_published_vocabulary(module: Any) -> None:
    """The failure mode worse than an unrecorded refusal is a recorded one no surface
    publishes. `read_facet` raises `unknown discovery facet` on a bad name -- at the
    moment of the refusal, when the run is already in trouble -- so the names are
    checked here, statically, where a typo is a red test instead of a crash.
    """
    facets = _adopted_facets(module)
    assert facets, f"{module.__name__} adopts no facet at all"
    assert facets <= DISCOVERY_FACETS, facets - DISCOVERY_FACETS
    literals = {
        node.args[0].value
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "read_facet"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert literals <= DISCOVERY_FACETS, literals - DISCOVERY_FACETS


def test_every_published_facet_is_claimed_by_at_least_one_adapter() -> None:
    """A facet the receipt can publish and no adapter ever records would report
    SUPPORTED-with-nothing for every source on earth, which is the false-clean reading
    this whole feature exists to prevent. PostgreSQL alone covers all eight."""
    claimed: set[str] = set()
    for module in _ADAPTERS:
        claimed |= _adopted_facets(module)
    assert claimed == set(DISCOVERY_FACETS)


@pytest.mark.parametrize("module", _ADAPTERS)
def test_every_adapter_reads_its_facets_through_the_one_mechanism(module: Any) -> None:
    """INV-9's shape for this feature: an adapter that imports the facet names without
    routing a read through `read_facet` would look adopted and behave as it always did.
    """
    source = inspect.getsource(module)
    assert "read_facet(" in source, module.__name__


def test_the_scope_rejects_a_facet_name_no_adapter_should_invent() -> None:
    """The guard the two tests above lean on, exercised directly once.

    The name here used to be `"triggers"`, which is a real facet now -- so this
    asserts on one that is still genuinely not: a refusal has to be attributed to a
    facet a receipt can publish, or it is recorded nowhere and reads as clean.
    """
    with pytest.raises(ValueError, match="unknown discovery facet"):
        FacetReadScope().record(
            "column_histograms",
            state=CapabilityState.PERMISSION_DENIED,
            reason=REASON_SOURCE_DENIED_READ,
        )
