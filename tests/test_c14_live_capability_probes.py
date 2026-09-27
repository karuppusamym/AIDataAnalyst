"""R11-C14: LIVE capability probes, one per (engine, flag), against real PostgreSQL and SQL Server.

INV-9 says a connector advertises only behaviour that is implemented and passing its
certification. These are the LIVE half of that certification: every one of the 17
`ConnectorCapabilities` flags is *observed* against a real running engine, through the
connector's own public methods, and the observation is what
`scripts/certify_connector_capabilities.py` records in
`src/aida/connectors/capability_certification.json`.

**A probe observes; it does not vouch.** Each returns a verdict --

* `WORKS`: the behaviour was exercised and the engine's real answer matched what the fixture
  put there (a constraint that exists comes back as a constraint; an EXPLAIN returns a cost);
* `ABSENT`: the behaviour was exercised and the connector does not provide it (an index that
  exists comes back as no index; `get_query_history` fails closed).

-- and a probe fails only when the engine answered *wrongly* (a constraint discovered with the
wrong columns), which is a defect and turns the suite red. It is never the claim that decides
which verdict is right, so the same probe serves a flag the connector claims and one it does
not, and an under-claim shows up as `WORKS` against a flag that is `False` today.

**Nothing is certified from an empty result.** The fixture below builds one of every object
kind, so `WORKS` means "found the specific thing that was put there", not "found nothing and
raised no error".

**Each engine's source is private.** PostgreSQL gets `aida_c14_scratch_<random>` on the
configured server; SQL Server gets a database and a per-run login inside the local sample
container, created as `sa` through `docker exec ... sqlcmd` with the container's own password
(never read or printed here -- see `tests/test_footprint_journey.py`). The login may only
SELECT, read definitions and show plans. Both are dropped afterwards. An engine is skipped when
its server is not reachable, so this file is a no-op in CI: the *committed artifact*, not this
file, is what CI reads.

Recorded per test through `record_property`: `probe`, `verdict`, `evidence` and
`environment.*` (server and driver versions). The runner reads those.
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import metadata
from typing import Any
from urllib.parse import quote

import asyncpg
import pytds
import pytest
from sqlalchemy.engine import make_url

from aida.config import get_settings
from aida.connectors.base import (
    ConnectorQueryHistoryUnsupported,
    ConnectorValueProfilingUnsupported,
    DiscoveredCatalog,
    DiscoveredSchema,
    DiscoveredTable,
    ProfileFacetStatus,
)
from aida.connectors.capability_certification import CAPABILITY_FLAGS
from aida.connectors.postgres import PostgresConnector
from aida.connectors.sql_execution import SqlExecutor
from aida.connectors.sqlserver import SqlServerConnector
from tests.test_footprint_journey import MSSQL_HOST_PORT, _mssql_execute, _pg_execute

ENGINES = ("postgres", "sqlserver")
SCHEMA = "c14_probe"

WORKS = "WORKS"
ABSENT = "ABSENT"


@dataclass(frozen=True, slots=True)
class Observation:
    verdict: str
    evidence: str


@dataclass(frozen=True, slots=True)
class Probe:
    engine: str
    flag: str
    #: What is probed, in one sentence. The runner publishes it.
    what: str
    run: Callable[[LiveEnv], Observation]


LIVE_PROBES: dict[tuple[str, str], Probe] = {}


def probe(engine: str, flag: str, what: str) -> Callable[[Callable[[LiveEnv], Observation]], Any]:
    def register(run: Callable[[LiveEnv], Observation]) -> Callable[[LiveEnv], Observation]:
        assert (engine, flag) not in LIVE_PROBES, f"duplicate probe {engine}.{flag}"
        LIVE_PROBES[(engine, flag)] = Probe(engine, flag, what, run)
        return run

    return register


# --- the live environments -----------------------------------------------------------------


class _Unreachable(Exception):
    """The engine's server could not be reached: a skip, never a failure."""


@dataclass
class LiveEnv:
    engine: str
    connector: SqlExecutor
    #: The login the connector authenticates as, to show it never acts as anyone else.
    login: str
    catalogs: tuple[DiscoveredCatalog, ...]
    server_version: str
    driver: str
    teardown: Callable[[], None]
    notes: dict[str, str] = field(default_factory=dict)

    def schema(self) -> DiscoveredSchema:
        for catalog in self.catalogs:
            for schema in catalog.schemas:
                if schema.name == SCHEMA:
                    return schema
        raise AssertionError(
            f"schema {SCHEMA!r} is missing from discover(): "
            f"{[s.name for c in self.catalogs for s in c.schemas]}"
        )

    def table(self, name: str) -> DiscoveredTable:
        for table in self.schema().tables:
            if table.name == name:
                return table
        raise AssertionError(f"table {name!r} is missing: {[t.name for t in self.schema().tables]}")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_PG_SETUP = f"""
CREATE SCHEMA {SCHEMA};
COMMENT ON SCHEMA {SCHEMA} IS 'C14 probe schema';
CREATE TABLE {SCHEMA}.customer (
    customer_id BIGINT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email TEXT UNIQUE,
    segment TEXT
);
COMMENT ON TABLE {SCHEMA}.customer IS 'C14 customer table';
COMMENT ON COLUMN {SCHEMA}.customer.segment IS 'C14 segment column';
INSERT INTO {SCHEMA}.customer (customer_id, customer_name, email, segment)
SELECT g, 'Customer ' || g, 'c' || g || '@example.invalid',
       CASE WHEN g <= 8 THEN 'alpha' WHEN g <= 12 THEN 'beta'
            WHEN g <= 14 THEN 'gamma' ELSE 'delta' END
FROM generate_series(1, 16) AS g;
CREATE TABLE {SCHEMA}.order_fact (
    order_id BIGINT NOT NULL,
    customer_id BIGINT NOT NULL REFERENCES {SCHEMA}.customer (customer_id),
    order_date DATE NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (order_id, order_date)
) PARTITION BY RANGE (order_date);
CREATE TABLE {SCHEMA}.order_fact_2025 PARTITION OF {SCHEMA}.order_fact
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE {SCHEMA}.order_fact_2026 PARTITION OF {SCHEMA}.order_fact
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
INSERT INTO {SCHEMA}.order_fact
    VALUES (9001, 1, '2025-06-01', 125.00), (9002, 2, '2026-02-01', 500);
CREATE INDEX order_fact_customer_idx ON {SCHEMA}.order_fact (customer_id);
CREATE VIEW {SCHEMA}.customer_orders AS
    SELECT c.customer_id, COUNT(o.order_id) AS order_count
    FROM {SCHEMA}.customer c LEFT JOIN {SCHEMA}.order_fact o ON o.customer_id = c.customer_id
    GROUP BY c.customer_id;
CREATE MATERIALIZED VIEW {SCHEMA}.customer_orders_mv AS SELECT * FROM {SCHEMA}.customer_orders;
CREATE FUNCTION {SCHEMA}.order_total(p_customer_id BIGINT) RETURNS NUMERIC LANGUAGE sql AS $$
    SELECT COALESCE(SUM(amount), 0) FROM {SCHEMA}.order_fact WHERE customer_id = p_customer_id
$$;
CREATE PROCEDURE {SCHEMA}.touch_customer(p_customer_id BIGINT) LANGUAGE sql AS $$
    UPDATE {SCHEMA}.customer SET segment = segment WHERE customer_id = p_customer_id
$$;
CREATE SEQUENCE {SCHEMA}.invoice_seq START WITH 100 INCREMENT BY 5;
CREATE FUNCTION {SCHEMA}.touch_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RETURN NEW; END
$$;
CREATE TRIGGER customer_audit BEFORE UPDATE ON {SCHEMA}.customer
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.touch_audit();
GRANT SELECT ON {SCHEMA}.customer TO PUBLIC;
"""  # noqa: S608 -- SCHEMA is a module constant


def _build_postgres() -> LiveEnv:
    server = make_url(get_settings().database_url)
    maintenance = server.set(database="postgres")
    name = f"aida_c14_scratch_{secrets.token_hex(4)}"
    try:
        _run(_pg_execute(maintenance, f"CREATE DATABASE {name}"))
    except (OSError, asyncpg.PostgresError) as exc:
        raise _Unreachable(f"no reachable PostgreSQL server: {type(exc).__name__}") from exc
    database = server.set(database=name)

    def teardown() -> None:
        _run(_pg_execute(maintenance, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))

    try:
        _run(_pg_execute(database, _PG_SETUP))
        dsn = database.set(drivername="postgresql").render_as_string(hide_password=False)
        connector = PostgresConnector(dsn)
        catalogs = _run(connector.discover())
        version = _run(_pg_first_value(database, "SELECT version()"))
    except BaseException:
        teardown()
        raise
    return LiveEnv(
        engine="postgres",
        connector=connector,
        login=str(server.username),
        catalogs=catalogs,
        server_version=" ".join(str(version).split()[:2]),
        driver=f"asyncpg {metadata.version('asyncpg')}",
        teardown=teardown,
    )


async def _pg_first_value(url: Any, sql: str) -> Any:
    connection = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        return await connection.fetchval(sql)
    finally:
        await connection.close()


# `CREATE SCHEMA/VIEW/FUNCTION/PROCEDURE/TRIGGER` must each begin a batch, hence the GO lines.
_MSSQL_SETUP = f"""
CREATE SCHEMA {SCHEMA};
GO
CREATE TABLE {SCHEMA}.customer (
    customer_id int NOT NULL CONSTRAINT pk_customer PRIMARY KEY,
    customer_name varchar(100) NOT NULL,
    email varchar(100) NULL CONSTRAINT uq_customer_email UNIQUE,
    segment varchar(20) NOT NULL
);
CREATE TABLE {SCHEMA}.order_fact (
    order_id int NOT NULL CONSTRAINT pk_order_fact PRIMARY KEY,
    customer_id int NOT NULL CONSTRAINT fk_order_customer
        REFERENCES {SCHEMA}.customer (customer_id),
    amount decimal(12, 2) NOT NULL
);
CREATE INDEX ix_order_customer ON {SCHEMA}.order_fact (customer_id);
INSERT INTO {SCHEMA}.customer (customer_id, customer_name, email, segment)
SELECT n, 'Customer ' + CAST(n AS varchar(10)), 'c' + CAST(n AS varchar(10)) + '@example.invalid',
       CASE WHEN n <= 8 THEN 'alpha' WHEN n <= 12 THEN 'beta'
            WHEN n <= 14 THEN 'gamma' ELSE 'delta' END
FROM (VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10),(11),(12),(13),(14),(15),(16)) AS v(n);
INSERT INTO {SCHEMA}.order_fact VALUES (9001, 1, 125.00), (9002, 2, 500.00);
GO
CREATE VIEW {SCHEMA}.customer_orders AS
    SELECT c.customer_id, COUNT(o.order_id) AS order_count
    FROM {SCHEMA}.customer c LEFT JOIN {SCHEMA}.order_fact o ON o.customer_id = c.customer_id
    GROUP BY c.customer_id;
GO
CREATE FUNCTION {SCHEMA}.order_total(@customer_id int) RETURNS decimal(12, 2) AS
BEGIN
    RETURN (SELECT COALESCE(SUM(amount), 0) FROM {SCHEMA}.order_fact
            WHERE customer_id = @customer_id);
END;
GO
CREATE PROCEDURE {SCHEMA}.touch_customer @customer_id int AS
    UPDATE {SCHEMA}.customer SET segment = segment WHERE customer_id = @customer_id;
GO
CREATE SEQUENCE {SCHEMA}.invoice_seq AS int START WITH 100 INCREMENT BY 5;
GO
CREATE TRIGGER {SCHEMA}.customer_audit ON {SCHEMA}.customer AFTER UPDATE AS
BEGIN
    SET NOCOUNT ON;
END;
GO
EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'C14 customer table',
    @level0type = N'SCHEMA', @level0name = N'{SCHEMA}',
    @level1type = N'TABLE', @level1name = N'customer';
EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'C14 segment column',
    @level0type = N'SCHEMA', @level0name = N'{SCHEMA}',
    @level1type = N'TABLE', @level1name = N'customer',
    @level2type = N'COLUMN', @level2name = N'segment';
EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'C14 probe schema',
    @level0type = N'SCHEMA', @level0name = N'{SCHEMA}';
GO
CREATE PARTITION FUNCTION pf_c14 (int) AS RANGE LEFT FOR VALUES (100, 200);
CREATE PARTITION SCHEME ps_c14 AS PARTITION pf_c14 ALL TO ([PRIMARY]);
CREATE TABLE {SCHEMA}.part_t (id int NOT NULL, note varchar(10) NULL) ON ps_c14 (id);
GO
"""  # noqa: S608 -- SCHEMA is a module constant


def _build_sqlserver() -> LiveEnv:
    token = secrets.token_hex(4)
    name = f"aida_c14_scratch_{token}"
    login = f"aida_c14_reader_{token}"
    drop = (
        f"IF DB_ID(N'{name}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE [{name}]; END;\n"
        f"IF SUSER_ID(N'{login}') IS NOT NULL DROP LOGIN [{login}];"
    )
    password = f"Jr1!{secrets.token_hex(16)}"
    try:
        _run(_mssql_execute("master", f"CREATE DATABASE [{name}];"))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise _Unreachable(
            f"no reachable SQL Server sample container: {type(exc).__name__}"
        ) from exc

    def teardown() -> None:
        _run(_mssql_execute("master", drop))

    try:
        _run(
            _mssql_execute(
                "master",
                f"CREATE LOGIN [{login}] WITH PASSWORD = N'{password}', CHECK_POLICY = OFF;",
            )
        )
        _run(_mssql_execute(name, _MSSQL_SETUP))
        # The platform's login may read rows and definitions and estimate plans; nothing else.
        # The object-level grant is what the grants facet (`sys.database_permissions`,
        # class 1) reads, so it has something real to find.
        _run(
            _mssql_execute(
                name,
                f"CREATE USER [{login}] FOR LOGIN [{login}];\n"
                f"GRANT SELECT ON SCHEMA::{SCHEMA} TO [{login}];\n"
                f"GRANT SELECT ON {SCHEMA}.customer TO [{login}];\n"
                f"GRANT VIEW DEFINITION TO [{login}];\n"
                f"GRANT SHOWPLAN TO [{login}];",
            )
        )
        dsn = f"mssql://{login}:{quote(password, safe='')}@localhost:{MSSQL_HOST_PORT}/{name}"
        connector = SqlServerConnector(dsn)
        catalogs = _run(connector.discover())
        version = _mssql_version(name, login, password)
    except BaseException:
        teardown()
        raise
    return LiveEnv(
        engine="sqlserver",
        connector=connector,
        login=login,
        catalogs=catalogs,
        server_version=version,
        driver=f"python-tds {metadata.version('python-tds')}",
        teardown=teardown,
    )


def _mssql_version(database: str, login: str, password: str) -> str:
    connection = pytds.connect(
        server="localhost",
        port=MSSQL_HOST_PORT,
        database=database,
        user=login,
        password=password,
        timeout=20,
        login_timeout=15,
        as_dict=True,
        autocommit=True,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(32)) AS v, "
            "CAST(SERVERPROPERTY('Edition') AS varchar(64)) AS e"
        )
        row = cursor.fetchone()
        return f"SQL Server {row['v']} ({row['e']})"
    finally:
        connection.close()


_BUILDERS: dict[str, Callable[[], LiveEnv]] = {
    "postgres": _build_postgres,
    "sqlserver": _build_sqlserver,
}
_ENVIRONMENTS: dict[str, LiveEnv] = {}
_UNREACHABLE: dict[str, str] = {}


def _environment(engine: str) -> LiveEnv:
    if engine in _UNREACHABLE:
        pytest.skip(_UNREACHABLE[engine])
    if engine not in _ENVIRONMENTS:
        try:
            _ENVIRONMENTS[engine] = _BUILDERS[engine]()
        except _Unreachable as exc:
            _UNREACHABLE[engine] = str(exc)
            pytest.skip(str(exc))
    return _ENVIRONMENTS[engine]


@pytest.fixture(scope="module", autouse=True)
def _drop_the_scratch_databases() -> Iterator[None]:
    yield
    for env in _ENVIRONMENTS.values():
        env.teardown()
    _ENVIRONMENTS.clear()
    _UNREACHABLE.clear()


# --- the probes: shared shapes --------------------------------------------------------------


def _works(evidence: str) -> Observation:
    return Observation(WORKS, evidence)


def _absent(evidence: str) -> Observation:
    return Observation(ABSENT, evidence)


def _profile(env: LiveEnv, *, sample_rows: int) -> Any:
    return _run(
        env.connector.profile_table(
            SCHEMA,
            "customer",
            ("customer_id", "segment"),
            sample_rows=sample_rows,
            column_batch_size=10,
            timeout_seconds=30,
        )
    )


def _shared_discovery_probes(engine: str) -> None:
    """The probes whose observation is the same shape on both engines."""

    @probe(engine, "catalogs", "discover() returns the connected database as its catalog")
    def catalogs(env: LiveEnv) -> Observation:
        assert len(env.catalogs) == 1, f"expected one catalog, got {len(env.catalogs)}"
        return _works(
            f"discover() returned 1 catalog holding {len(env.catalogs[0].schemas)} schemas"
        )

    @probe(engine, "schemas", "discover() returns the fixture schema, with its tables, inside it")
    def schemas(env: LiveEnv) -> Observation:
        tables = sorted(t.name for t in env.schema().tables)
        assert {"customer", "order_fact"} <= set(tables), tables
        return _works(
            f"schema {SCHEMA} came back with {len(tables)} objects, e.g. customer, order_fact"
        )

    @probe(
        engine,
        "constraints",
        "discover() returns the PRIMARY KEY, UNIQUE and FOREIGN KEY constraints the fixture made",
    )
    def constraints(env: LiveEnv) -> Observation:
        customer = env.table("customer")
        by_type = {c.constraint_type: c for c in customer.constraints}
        assert {"PRIMARY_KEY", "UNIQUE"} <= set(by_type), customer.constraints
        assert by_type["PRIMARY_KEY"].columns == ("customer_id",), by_type["PRIMARY_KEY"]
        assert by_type["UNIQUE"].columns == ("email",), by_type["UNIQUE"]
        fks = [c for c in env.table("order_fact").constraints if c.constraint_type == "FOREIGN_KEY"]
        assert len(fks) == 1, env.table("order_fact").constraints
        assert fks[0].referenced_table == "customer", fks[0]
        assert fks[0].referenced_columns == ("customer_id",), fks[0]
        return _works(
            "customer: PRIMARY_KEY(customer_id) and UNIQUE(email); "
            "order_fact: FOREIGN_KEY(customer_id) -> customer(customer_id)"
        )

    @probe(engine, "views", "discover() returns the view's definition text, not just its name")
    def views(env: LiveEnv) -> Observation:
        view = env.table("customer_orders")
        assert view.view_definition is not None, "the view has no definition envelope"
        assert view.view_definition.unavailable_reason is None, view.view_definition
        text = view.view_definition.definition_sql or ""
        assert "order_fact" in text, f"the definition does not read the base table: {text!r}"
        if engine != "postgres":
            return _works("customer_orders: definition_sql present and reads order_fact")
        mv = env.table("customer_orders_mv")
        assert mv.object_type == "MATERIALIZED_VIEW", mv.object_type
        assert mv.view_definition is not None and mv.view_definition.is_materialized
        return _works(
            "customer_orders: definition_sql present and reads order_fact; "
            "customer_orders_mv came back flagged is_materialized"
        )

    @probe(
        engine,
        "routines",
        "discover() returns a function and a procedure with bodies and parameters",
    )
    def routines(env: LiveEnv) -> Observation:
        by_name = {r.name: r for r in env.schema().routines}
        assert {"order_total", "touch_customer"} <= set(by_name), sorted(by_name)
        function, procedure = by_name["order_total"], by_name["touch_customer"]
        assert function.routine_type == "FUNCTION", function.routine_type
        assert procedure.routine_type == "PROCEDURE", procedure.routine_type
        assert function.body_sql and "order_fact" in function.body_sql, function.unavailable_reason
        assert procedure.body_sql, procedure.unavailable_reason
        assert len(function.parameters) >= 1, function.parameters
        return _works(
            f"order_total FUNCTION ({len(function.parameters)} parameter) and touch_customer "
            "PROCEDURE, both with body text"
        )

    @probe(
        engine,
        "object_comments",
        "discover() returns the schema, table and column comments the fixture attached",
    )
    def object_comments(env: LiveEnv) -> Observation:
        customer = env.table("customer")
        assert customer.source_description == "C14 customer table", customer.source_description
        segment = next(c for c in customer.columns if c.name == "segment")
        assert segment.source_description == "C14 segment column", segment.source_description
        assert env.schema().source_description == "C14 probe schema", (
            env.schema().source_description
        )
        return _works(
            "schema, table customer and column customer.segment each returned their comment"
        )

    @probe(
        engine,
        "triggers",
        "discover() returns the trigger the fixture created, with table and event",
    )
    def triggers(env: LiveEnv) -> Observation:
        found = {t.name: t for t in env.schema().triggers}
        assert "customer_audit" in found, sorted(found)
        trigger = found["customer_audit"]
        assert trigger.table_name == "customer", trigger.table_name
        assert "UPDATE" in trigger.events, trigger.events
        assert trigger.timing in {"BEFORE", "AFTER"}, trigger.timing
        return _works(f"customer_audit on customer: {trigger.timing} {'/'.join(trigger.events)}")

    @probe(engine, "sequences", "discover() returns the sequence's declaration, never its position")
    def sequences(env: LiveEnv) -> Observation:
        found = {s.name: s for s in env.schema().sequences}
        assert "invoice_seq" in found, sorted(found)
        sequence = found["invoice_seq"]
        assert sequence.start_with == "100", sequence
        assert sequence.increment_by == "5", sequence
        return _works("invoice_seq declared start 100, increment 5")

    @probe(
        engine,
        "query_history",
        "get_query_history() is called on the live connector to see whether it reads history",
    )
    def query_history(env: LiveEnv) -> Observation:
        since = datetime.now(UTC) - timedelta(days=1)
        try:
            entries = _run(
                env.connector.get_query_history(since=since, limit=5, timeout_seconds=10)
            )
        except ConnectorQueryHistoryUnsupported:
            return _absent("get_query_history() fails closed with ConnectorQueryHistoryUnsupported")
        return _works(f"get_query_history() returned {len(entries)} entries")

    @probe(
        engine,
        "delegated_identity",
        "the connector runs as some identity; probe whether it is ever anyone but its own login",
    )
    def delegated_identity(env: LiveEnv) -> Observation:
        who = "SELECT current_user AS u" if engine == "postgres" else "SELECT SUSER_SNAME() AS u"
        result = _run(env.connector.execute_read_query(who, timeout_seconds=15))
        acting = str(result.rows[0]["u"])
        assert acting == env.login, f"connector ran as {acting!r}, not its own login {env.login!r}"
        return _absent(
            "the session runs as the DSN's own login; the connector takes no per-caller identity"
        )

    @probe(
        engine,
        "explain",
        "estimate_read_query() returns a real plan cost for a statement, without running it",
    )
    def explain(env: LiveEnv) -> Observation:
        estimate = _run(
            env.connector.estimate_read_query(
                f"SELECT customer_id, segment FROM {SCHEMA}.customer WHERE customer_id = 3",  # noqa: S608
                timeout_seconds=30,
            )
        )
        assert estimate.score > 0, estimate
        assert estimate.kind in {"EXPLAIN_COST", "SHOWPLAN_XML"}, estimate.kind
        assert estimate.evidence, "the estimate carries no plan evidence"
        rows = "?" if estimate.estimated_rows is None else f"{estimate.estimated_rows:g}"
        return _works(
            f"{estimate.kind} returned plan cost {estimate.score:.4g}, estimated rows {rows}"
        )

    @probe(
        engine,
        "approximate_statistics",
        "profile_table() returns bounded value-free counts matching the fixture, and the scope",
    )
    def approximate_statistics(env: LiveEnv) -> Observation:
        full = _profile(env, sample_rows=1000)
        segment = next(c for c in full.columns if c.name == "segment")
        assert (segment.null_count, segment.non_null_count) == (0, 16), segment
        assert segment.approximate_distinct_count == 4, segment
        assert full.sampled_row_count == 16, full.sampled_row_count
        assert full.observation_scope == "FULL", full.observation_scope
        assert full.row_count_estimate is not None and full.row_count_estimate >= 16
        bounded = _profile(env, sample_rows=5)
        assert bounded.sampled_row_count == 5, bounded.sampled_row_count
        assert bounded.observation_scope == "SAMPLE", bounded.observation_scope
        return _works(
            "segment: 4 distinct, 0 null, 16 non-null over a 16-row scan reported FULL; "
            "a 5-row bound sampled 5 and reported SAMPLE"
        )


def _sqlserver_absence_probes() -> None:
    @probe(
        "sqlserver",
        "indexes",
        "discover() is asked for the index on order_fact (ix_order_customer) the fixture created",
    )
    def indexes(env: LiveEnv) -> Observation:
        table = env.table("order_fact")
        if table.indexes:
            return _works(f"discover() returned indexes {[i.name for i in table.indexes]}")
        return _absent("order_fact has ix_order_customer, and discover() returned no index for it")

    @probe(
        "sqlserver",
        "partitions",
        "discover() is asked for the partitions of c14_probe.part_t, which the fixture partitioned",
    )
    def partitions(env: LiveEnv) -> Observation:
        table = env.table("part_t")
        if table.partitions:
            return _works(f"discover() returned {len(table.partitions)} partitions")
        return _absent("part_t is partitioned into 3 ranges, and discover() returned no partition")

    @probe(
        "sqlserver",
        "grants",
        "discover() returns the object-level SELECT grant the fixture gave the connector's login",
    )
    def grants(env: LiveEnv) -> Observation:
        found = [
            g
            for g in env.schema().grants
            if g.object_name == "customer" and g.privilege == "SELECT" and g.grantee == env.login
        ]
        assert found, f"no SELECT grant on customer for {env.login}: {env.schema().grants}"
        return _works("SELECT on customer granted to the connector's own login came back")

    @probe(
        "sqlserver",
        "value_range_profiling",
        "profile_column_values() is called to see if the connector returns ranges and top values",
    )
    def value_range(env: LiveEnv) -> Observation:
        try:
            _run(
                env.connector.profile_column_values(
                    SCHEMA, "customer", ("segment",), sample_rows=100, top_n=2, timeout_seconds=15
                )
            )
        except ConnectorValueProfilingUnsupported:
            return _absent(
                "profile_column_values() fails closed with ConnectorValueProfilingUnsupported"
            )
        return _works("profile_column_values() returned value ranges")

    @probe(
        "sqlserver",
        "distribution_entropy_profiling",
        "profile_table() is asked for the entropy of customer.segment",
    )
    def entropy(env: LiveEnv) -> Observation:
        column = next(c for c in _profile(env, sample_rows=1000).columns if c.name == "segment")
        if column.frequency_entropy_bits is not None:
            return _works(f"entropy {column.frequency_entropy_bits:.4f} bits")
        status = _entropy_status(column.facet_status)
        assert status is not None, (
            "no entropy value and no entropy facet status: an unexplained gap"
        )
        return _absent(f"frequency_entropy_bits is None and the facet status is {status}")


def _entropy_status(statuses: tuple[ProfileFacetStatus, ...]) -> str | None:
    for status in statuses:
        if status.facet == "ENTROPY":
            return f"{status.status}/{status.reason_code}"
    return None


def _postgres_specific_probes() -> None:
    @probe("postgres", "indexes", "discover() returns the secondary index the fixture created")
    def indexes(env: LiveEnv) -> Observation:
        table = env.table("order_fact")
        found = [i for i in table.indexes if i.name == "order_fact_customer_idx"]
        assert found and found[0].columns == ("customer_id",), table.indexes
        return _works("order_fact_customer_idx on (customer_id) came back")

    @probe("postgres", "partitions", "discover() returns the range partitions of order_fact")
    def partitions(env: LiveEnv) -> Observation:
        table = env.table("order_fact")
        names = {p.name for p in table.partitions}
        assert names == {"order_fact_2025", "order_fact_2026"}, table.partitions
        assert all(
            p.partition_type == "RANGE" and p.key_columns == ("order_date",)
            for p in table.partitions
        )
        return _works("order_fact: 2 RANGE partitions keyed on order_date")

    @probe(
        "postgres",
        "grants",
        "discover() returns the SELECT grant to PUBLIC that the fixture made on customer",
    )
    def grants(env: LiveEnv) -> Observation:
        found = [
            g
            for g in env.schema().grants
            if g.object_name == "customer" and g.grantee == "PUBLIC" and g.privilege == "SELECT"
        ]
        assert found, env.schema().grants
        return _works("SELECT on customer granted to PUBLIC came back")

    @probe(
        "postgres",
        "value_range_profiling",
        "profile_column_values() returns min, max and top values that match the fixture",
    )
    def value_range(env: LiveEnv) -> Observation:
        snapshots = _run(
            env.connector.profile_column_values(
                SCHEMA, "customer", ("segment",), sample_rows=1000, top_n=2, timeout_seconds=30
            )
        )
        assert len(snapshots) == 1, snapshots
        snap = snapshots[0]
        assert (snap.min_value, snap.max_value) == ("alpha", "gamma"), snap
        assert snap.top_values == (("alpha", 8), ("beta", 4)), snap.top_values
        return _works("segment: min alpha, max gamma, top values alpha x8 then beta x4")

    @probe(
        "postgres",
        "distribution_entropy_profiling",
        "profile_table() returns the Shannon entropy of customer.segment, matching the analytic",
    )
    def entropy(env: LiveEnv) -> Observation:
        column = next(c for c in _profile(env, sample_rows=1000).columns if c.name == "segment")
        # Counts 8/4/2/2 of 16: -(1/2 log2 1/2 + 1/4 log2 1/4 + 2 * 1/8 log2 1/8) = 1.75 bits.
        assert column.frequency_entropy_bits is not None, column.facet_status
        assert abs(column.frequency_entropy_bits - 1.75) < 1e-6, column.frequency_entropy_bits
        return _works(
            f"segment entropy {column.frequency_entropy_bits:.4f} bits, analytic value 1.75"
        )


for _engine in ENGINES:
    _shared_discovery_probes(_engine)
_postgres_specific_probes()
_sqlserver_absence_probes()


# --- the tests -------------------------------------------------------------------------------


def test_every_engine_has_a_probe_for_every_capability_flag() -> None:
    """CI-safe: the certification cannot silently skip a flag on a live engine.

    A flag added to `ConnectorCapabilities` without a probe here would otherwise produce no
    row for PostgreSQL or SQL Server, and `verify_result` would only notice at the next run.
    """
    missing = sorted(
        (engine, flag)
        for engine in ENGINES
        for flag in CAPABILITY_FLAGS
        if (engine, flag) not in LIVE_PROBES
    )
    assert missing == [], f"no live probe for {missing}"
    stray = sorted(set(LIVE_PROBES) - {(e, f) for e in ENGINES for f in CAPABILITY_FLAGS})
    assert stray == [], f"probes for flags or engines that do not exist: {stray}"


@pytest.mark.parametrize(
    ("engine", "flag"),
    [(engine, flag) for engine in ENGINES for flag in CAPABILITY_FLAGS],
    ids=[f"{engine}-{flag}" for engine in ENGINES for flag in CAPABILITY_FLAGS],
)
def test_live_probe(engine: str, flag: str, record_property: Any) -> None:
    """Run one probe against the real engine and record what it observed."""
    env = _environment(engine)
    the_probe = LIVE_PROBES[(engine, flag)]
    record_property("probe", the_probe.what)
    record_property("environment.server_version", env.server_version)
    record_property("environment.driver", env.driver)
    observation = the_probe.run(env)
    assert observation.verdict in {WORKS, ABSENT}, observation
    record_property("verdict", observation.verdict)
    record_property("evidence", observation.evidence)
