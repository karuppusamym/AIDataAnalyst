"""R11-FP02: against real PostgreSQL, a run says how much of the source its login cannot see.

A source's metadata is itself permission-filtered. PostgreSQL's `information_schema` shows a role
only what it holds some privilege on, so a login granted one schema of five discovers that schema
and reports a complete, honest-looking receipt: every count right, the estate a fifth of the real
one. `pg_class` and `pg_proc` are readable by every role, which is what lets the connector ask the
unfiltered question and the receipt say what was kept back.

The journey's private database gains a schema the platform's own login is never granted, and a
least-privilege role that can read only the sample schema. Nothing here reads a name it may not
read: the counts are counts.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.engine import make_url

from aida.connectors.postgres import PostgresConnector
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    JourneySource,
    _postgres,
)

HIDDEN = "footprint_unreadable"
READABLE = "footprint_context_sample"
#: A schema the platform's login is never granted, holding one object of each kind. PUBLIC
#: holds EXECUTE on a new function, which would otherwise make it visible to every role.
_HIDDEN_OBJECTS = """
    CREATE SCHEMA footprint_unreadable;
    CREATE TABLE footprint_unreadable.secret_ledger (id integer);
    CREATE VIEW footprint_unreadable.secret_view AS SELECT 1 AS marker;
    CREATE FUNCTION footprint_unreadable.secret_function()
        RETURNS integer LANGUAGE sql AS 'SELECT 1';
    REVOKE EXECUTE ON FUNCTION footprint_unreadable.secret_function() FROM PUBLIC;
"""


@pytest_asyncio.fixture
async def restricted(_postgres: JourneySource) -> AsyncIterator[tuple[JourneySource, str]]:  # noqa: F811
    """The journey's PostgreSQL source, plus a DSN for a login granted one schema of it.

    PostgreSQL only: it is the engine whose catalog every role may read, so it is the engine
    that can be asked this at all. SQL Server filters `sys.objects` by permission with no
    unfiltered view behind it, and answers `None` (see `Connector.count_invisible_objects`).
    """
    source = _postgres
    role = f"aida_invisible_reader_{os.getpid()}"
    password = secrets.token_hex(16)
    url = make_url(source.dsn)
    await source.execute(_HIDDEN_OBJECTS)
    await source.execute(
        f"DROP ROLE IF EXISTS {role}; "
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}'; "
        f"GRANT USAGE ON SCHEMA {READABLE} TO {role}; "
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {READABLE} TO {role};"
    )
    try:
        yield source, url.set(username=role, password=password).render_as_string(
            hide_password=False
        )
    finally:
        await source.execute(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {READABLE} FROM {role}; "
            f"REVOKE ALL ON SCHEMA {READABLE} FROM {role}; "
            f"DROP ROLE IF EXISTS {role};"
        )


async def test_a_login_granted_one_schema_is_told_how_much_it_cannot_see(
    restricted: tuple[JourneySource, str],
) -> None:
    _, dsn = restricted

    connector = PostgresConnector(dsn)
    catalogs = await connector.discover()
    tables = {
        f"{schema.name}.{table.name}".lower()
        for catalog in catalogs
        for schema in catalog.schemas
        for table in schema.tables
    }
    routines = {
        f"{schema.name}.{routine.name}".lower()
        for catalog in catalogs
        for schema in catalog.schemas
        for routine in schema.routines
    }
    invisible = await connector.count_invisible_objects()

    # The table and the view it was never granted are simply not there -- which is the problem
    # this count exists to make visible. They are not reported as absent from the source, and
    # nothing in the run says the estate is smaller than it is; that is exactly why a count is
    # needed rather than an inference.
    assert f"{HIDDEN}.secret_ledger" not in tables
    assert f"{HIDDEN}.secret_view" not in tables
    assert invisible is not None
    assert invisible.get("TABLE", 0) >= 1
    assert invisible.get("VIEW", 0) >= 1

    # The routine in that same schema *is* discovered: `pg_proc` is open to every role, so the
    # connector reads it whatever the grants say. Counting it as hidden would report a gap that
    # does not exist, so no routine kind appears here at all.
    assert f"{HIDDEN}.secret_function" in routines
    assert "FUNCTION" not in invisible and "PROCEDURE" not in invisible


async def test_the_login_that_owns_the_estate_is_told_nothing_is_hidden(
    restricted: tuple[JourneySource, str],
) -> None:
    source, _ = restricted

    owner = PostgresConnector(source.dsn)

    # Every object belongs to this login's own role, so there is nothing it may not read. The
    # answer is an empty count, not `None`: the source was asked and said none.
    assert await owner.count_invisible_objects() == {}


async def test_a_schema_the_selection_excludes_is_not_counted_as_hidden(
    restricted: tuple[JourneySource, str],
) -> None:
    _, dsn = restricted

    scoped = PostgresConnector(dsn)
    assert scoped.scope_discovery(include_schemas=[READABLE], exclude_schemas=[])

    # What a selection leaves out was never asked for, so it is out of scope rather than
    # withheld: the two are different facts and the receipt must not merge them.
    assert await scoped.count_invisible_objects() == {}
