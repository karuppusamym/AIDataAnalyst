"""CN-3: executable vendor/version fixtures for the PostgreSQL connector.

`Docs/60-delivery/03-tracker.md` row CN-3's exit criterion is "at least 2
versions per adapter", against `IN-5d`'s standing gap: no 1.1 discovery
statement on any connector had ever run against a live source -- every row
shape in `tests/test_connectors.py` is hand-built, so `PostgresConnector.discover()`
itself (the actual SQL text in `src/aida/connectors/postgres.py`) had never
executed against a real server.

Scope, honestly stated (this is the connector-adapter half of CN-3, not the
whole tracker row): PostgreSQL only, chosen per the tracker's own status
matrix as the most mature on-prem adapter. Two versions:

  * **16** -- current LTS default this repository already runs everywhere
    else (`compose.yaml`'s own app database, the `migration-drift` CI job's
    service container). Runs for real in *any* environment with a reachable
    Postgres 16 -- including, right now, this sandbox's own native
    `postgresql-16` install on `localhost:5432` (see `_pg16_fixture_dsn`
    below), with no Docker involved at all.
  * **14** -- oldest still-supported PostgreSQL major as of this writing,
    and the second leg of a genuine "does this hold across versions" check
    rather than testing the same server twice. Provided by
    `tests/fixtures/postgres_versions/compose.yml` (a real, standalone
    `postgres:14-alpine` service) and, in CI, by the `connector-version-fixtures`
    job in `.github/workflows/ci.yml` (a real GitHub Actions service
    container, the same pattern `migration-drift` already established for
    Postgres 16). **This sandbox has no Docker daemon** (`dockerd` cannot be
    started here -- confirmed, not assumed) and this branch's proxy policy
    blocks `apt.postgresql.org`, so there is no way to stand up a second,
    genuinely different Postgres major inside this specific interactive
    session. The 14 leg below is real and correctly wired -- it is not a
    stub -- but it has only actually *executed*, so far, in the CI job that
    provides its service container; it is honestly reported as SKIPPED, not
    silently passed, wherever no reachable DSN is configured.

Both legs run the exact same fixture schema
(`tests/fixtures/postgres_versions/schema.sql`) through the connector's real
`discover()` and assert on every envelope 1.1 axis the connector claims:
constraints (PK/UNIQUE/FK), indexes, partitions, views, routines, object
comments and grants -- see that file's own header comment for why one schema
covers all of them.

Building this fixture is what found a real, version-*independent* bug (not a
14-vs-16 difference: `information_schema.tables`/`.columns` exclude
materialized views on every PostgreSQL version) that no mocked-row unit test
could have caught, because every mocked-row test constructs the `tables` map
by hand rather than deriving it from a real information_schema query -- see
`_MATERIALIZED_VIEW_COLUMN_SQL` in `postgres.py` and this file's
`test_materialized_view_columns_and_definition_are_discovered`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

from aida.connectors.postgres import PostgresConnector
from atlas.platform.config import get_settings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "postgres_versions"
FIXTURE_SCHEMA_SQL = (FIXTURE_DIR / "schema.sql").read_text(encoding="utf-8")

# The schema fixture always lands in this non-`public` schema, regardless of
# which database/server it's applied to -- see schema.sql's own DROP/CREATE.
FIXTURE_SCHEMA_NAME = "cn3_pg_fixture"


def _pg16_fixture_dsn() -> str:
    """The live Postgres 16 this leg targets.

    An explicit override always wins (this is what the CI service container
    sets); otherwise this derives from the app's own default connection --
    exactly `_test_database_url`'s pattern in `tests/test_migration_orm_drift.py`
    -- so it needs no separate local setup: point Postgres 16 wherever
    `Settings.database_url` already expects it (this sandbox's native
    `postgresql-16` install included) and this leg runs.
    """
    override = os.environ.get("AIDA_CN3_POSTGRES16_FIXTURE_DATABASE_URL")
    if override:
        return override
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def _pg14_fixture_dsn() -> str | None:
    """The live Postgres 14 this leg targets, or None if none is configured.

    Deliberately has no local-instance fallback the way `_pg16_fixture_dsn`
    does: there is no Postgres 14 anywhere in this sandbox to fall back to
    (see this file's module docstring), and guessing a plausible-looking
    default (e.g. a hardcoded `localhost:5433`) would make an unconfigured
    run look like a deliberate skip of a real fixture instead of what it
    actually is -- no second version available here. Set by
    `tests/fixtures/postgres_versions/compose.yml` (`docker compose up` maps
    it to `localhost:55414`) or by the CI service container.
    """
    return os.environ.get("AIDA_CN3_POSTGRES14_FIXTURE_DATABASE_URL")


async def _apply_fixture_schema(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(FIXTURE_SCHEMA_SQL)
    finally:
        await connection.close()


async def _server_version_string(dsn: str) -> str:
    connection = await asyncpg.connect(dsn)
    try:
        return str(await connection.fetchval("SELECT version()"))
    finally:
        await connection.close()


async def _probe_reachable(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    await connection.close()


def _require_reachable(dsn: str, *, version_label: str) -> None:
    """Skip (never fail) when this version's fixture isn't reachable.

    Same rule `test_migration_orm_drift.py` follows: infrastructure absence
    is a skip, not a failure. Everything after this point in a test is a
    real assertion against a real server and must fail loudly.
    """
    try:
        asyncio.run(_probe_reachable(dsn))
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip"
        pytest.skip(
            f"Postgres {version_label} fixture not reachable at {dsn!r} "
            f"({type(exc).__name__}: {exc}); see this file's module docstring "
            "and tests/fixtures/postgres_versions/compose.yml to run it, or "
            "the connector-version-fixtures job in .github/workflows/ci.yml "
            "for where CI provides it as a real service container."
        )


async def _discover_fixture_schema(dsn: str):
    connector = PostgresConnector(dsn)
    catalogs = await connector.discover()
    assert len(catalogs) == 1, "PostgreSQL discover() must return exactly one catalog"
    catalog = catalogs[0]
    schema = next(
        (s for s in catalog.schemas if s.name == FIXTURE_SCHEMA_NAME),
        None,
    )
    assert schema is not None, (
        f"fixture schema {FIXTURE_SCHEMA_NAME!r} missing from discover() output "
        f"-- schemas found: {[s.name for s in catalog.schemas]!r}"
    )
    return schema


def _assert_fixture_schema_matches_every_axis(schema, *, version_label: str) -> None:
    """One assertion block, reused by both version legs.

    Running the identical set of assertions against 14 and 16 is the actual
    point of a *version* fixture: it isn't proving the connector works once,
    it's proving the same discovery SQL produces the same shape across major
    versions -- which is exactly the property CN-3 exists to check.
    """
    assert schema.source_description == "CN-3 executable version fixture schema"

    tables = {table.name: table for table in schema.tables}
    assert set(tables) == {
        "customer",
        "order_fact",
        "order_fact_2025",
        "order_fact_2026",
        "customer_order_summary",
        "customer_order_summary_mv",
    }, f"[{version_label}] unexpected table set: {sorted(tables)}"

    # -- constraints (PRIMARY KEY, UNIQUE, FOREIGN KEY) ----------------------
    customer = tables["customer"]
    constraint_types = {c.constraint_type for c in customer.constraints}
    assert constraint_types == {"PRIMARY_KEY", "UNIQUE"}, (
        f"[{version_label}] customer constraints: {customer.constraints!r}"
    )
    pk = next(c for c in customer.constraints if c.constraint_type == "PRIMARY_KEY")
    assert pk.columns == ("customer_id",)
    unique = next(c for c in customer.constraints if c.constraint_type == "UNIQUE")
    assert unique.columns == ("email",)

    order_fact = tables["order_fact"]
    fk = next(c for c in order_fact.constraints if c.constraint_type == "FOREIGN_KEY")
    assert fk.referenced_table == "customer"
    assert fk.referenced_schema == FIXTURE_SCHEMA_NAME
    assert fk.columns == ("customer_id",)
    assert fk.referenced_columns == ("customer_id",)

    # -- indexes (non-PK, CT-3/CN-8) ------------------------------------------
    secondary_indexes = [idx for idx in order_fact.indexes if not idx.is_primary]
    assert any(
        idx.name == "order_fact_customer_idx" and idx.columns == ("customer_id",)
        for idx in secondary_indexes
    ), f"[{version_label}] order_fact indexes: {order_fact.indexes!r}"

    # -- partitions (CT-3/CN-8) ------------------------------------------------
    assert {p.name for p in order_fact.partitions} == {
        "order_fact_2025",
        "order_fact_2026",
    }, f"[{version_label}] order_fact partitions: {order_fact.partitions!r}"
    for partition in order_fact.partitions:
        assert partition.partition_type == "RANGE"
        assert partition.key_columns == ("order_date",)
        assert partition.high_value is not None

    # -- views -----------------------------------------------------------------
    view = tables["customer_order_summary"]
    assert view.view_definition is not None
    assert view.view_definition.is_materialized is False
    assert view.view_definition.definition_sql is not None
    assert "order_fact" in view.view_definition.definition_sql
    assert view.view_definition.unavailable_reason is None

    # -- materialized views (CN-3 fix: _MATERIALIZED_VIEW_COLUMN_SQL) ---------
    mv = tables["customer_order_summary_mv"]
    assert mv.object_type == "MATERIALIZED_VIEW"
    assert {c.name for c in mv.columns} == {"customer_id", "customer_name", "order_count"}, (
        f"[{version_label}] materialized view columns: {mv.columns!r}"
    )
    assert mv.view_definition is not None
    assert mv.view_definition.is_materialized is True
    assert mv.view_definition.definition_sql is not None
    assert mv.view_definition.unavailable_reason is None

    # -- object comments (schema/table/column) ----------------------------------
    assert customer.source_description == "Fixture customer table"
    email_column = next(c for c in customer.columns if c.name == "email")
    assert email_column.source_description == "Fixture email column"

    # -- routines (function + procedure) -----------------------------------------
    routines_by_name = {r.name: r for r in schema.routines}
    assert set(routines_by_name) == {"total_order_amount", "touch_customer"}
    function = routines_by_name["total_order_amount"]
    assert function.routine_type == "FUNCTION"
    assert function.body_sql is not None
    assert "order_fact" in function.body_sql
    assert function.unavailable_reason is None
    procedure = routines_by_name["touch_customer"]
    assert procedure.routine_type == "PROCEDURE"
    assert procedure.body_sql is not None

    # -- grants -------------------------------------------------------------------
    grants = [g for g in schema.grants if g.object_name == "customer" and g.grantee == "PUBLIC"]
    assert grants, f"[{version_label}] expected a PUBLIC grant on customer: {schema.grants!r}"
    assert grants[0].privilege == "SELECT"


def test_postgres_16_version_fixture_discovers_every_axis() -> None:
    """Live PostgreSQL 16: runs for real in this sandbox (no Docker needed) --
    a native `postgresql-16` install already answers on `localhost:5432` with
    the same `aida`/`aida-local-only` credentials `Settings.database_url`
    defaults to, so this leg never skips here."""
    dsn = _pg16_fixture_dsn()
    _require_reachable(dsn, version_label="16")

    server_version = asyncio.run(_server_version_string(dsn))
    if "PostgreSQL 16" not in server_version:
        # Reachable, but not the version this fixture is about. That is
        # infrastructure absence -- the 16 fixture is not present -- and this
        # file's rule (see `_require_reachable`) is that infrastructure
        # absence skips rather than fails. Asserting here made the suite fail
        # on any machine whose local server is a different major version,
        # which says nothing about the 16 fixture either way.
        pytest.skip(
            f"AIDA_CN3_POSTGRES16_FIXTURE_DATABASE_URL points at a non-16 "
            f"server ({server_version!r}); start the 16 fixture from "
            "tests/fixtures/postgres_versions/compose.yml to run this leg."
        )

    asyncio.run(_apply_fixture_schema(dsn))
    schema = asyncio.run(_discover_fixture_schema(dsn))
    _assert_fixture_schema_matches_every_axis(schema, version_label="16")


def test_postgres_14_version_fixture_discovers_every_axis() -> None:
    """Live PostgreSQL 14. SKIPS (not a silent pass) unless
    `AIDA_CN3_POSTGRES14_FIXTURE_DATABASE_URL` points at one -- there is no
    Postgres 14 anywhere in the current sandbox (see this file's module
    docstring for exactly why: no Docker daemon, and the PGDG apt repo is
    outside this branch's proxy allowlist). Provided for real by
    `tests/fixtures/postgres_versions/compose.yml` locally and by the
    `connector-version-fixtures` CI job's service container."""
    dsn = _pg14_fixture_dsn()
    if dsn is None:
        pytest.skip(
            "AIDA_CN3_POSTGRES14_FIXTURE_DATABASE_URL is not set -- no Postgres 14 "
            "fixture is available in this environment. Run "
            "`docker compose -f tests/fixtures/postgres_versions/compose.yml up -d` "
            "and export AIDA_CN3_POSTGRES14_FIXTURE_DATABASE_URL=postgresql://"
            "aida:aida-local-only@localhost:55414/aida to run this leg locally; "
            "the connector-version-fixtures CI job runs it on every push."
        )
    _require_reachable(dsn, version_label="14")

    server_version = asyncio.run(_server_version_string(dsn))
    if "PostgreSQL 14" not in server_version:
        # Reachable, but not the version this fixture is about. That is
        # infrastructure absence -- the 14 fixture is not present -- and this
        # file's rule (see `_require_reachable`) is that infrastructure
        # absence skips rather than fails. Asserting here made the suite fail
        # on any machine whose local server is a different major version,
        # which says nothing about the 14 fixture either way.
        pytest.skip(
            f"AIDA_CN3_POSTGRES14_FIXTURE_DATABASE_URL points at a non-14 "
            f"server ({server_version!r}); start the 14 fixture from "
            "tests/fixtures/postgres_versions/compose.yml to run this leg."
        )

    asyncio.run(_apply_fixture_schema(dsn))
    schema = asyncio.run(_discover_fixture_schema(dsn))
    _assert_fixture_schema_matches_every_axis(schema, version_label="14")


def test_materialized_view_columns_and_definition_are_discovered() -> None:
    """Narrow regression test for the CN-3 fix itself, isolated from the
    full-axis assertions above so a future change that breaks *only*
    materialized-view discovery fails here with an unambiguous name rather
    than inside the middle of a large multi-axis assertion block.

    Runs against whichever Postgres 16 fixture `_pg16_fixture_dsn` resolves
    to (same reachability rule as the 16 leg above); it is not a second,
    independent live server.
    """
    dsn = _pg16_fixture_dsn()
    _require_reachable(dsn, version_label="16")

    asyncio.run(_apply_fixture_schema(dsn))
    schema = asyncio.run(_discover_fixture_schema(dsn))
    tables = {table.name: table for table in schema.tables}

    mv = tables.get("customer_order_summary_mv")
    assert mv is not None, (
        "materialized view is entirely missing from discover() output -- "
        "the CN-3 fix (_MATERIALIZED_VIEW_COLUMN_SQL in postgres.py) has "
        "regressed: information_schema.tables/.columns never list relkind "
        "'m', so without that fix this table silently never enters the "
        "discovered catalog at all."
    )
    assert len(mv.columns) == 3
    assert mv.view_definition is not None
    assert mv.view_definition.is_materialized is True
