"""R11-FP02: against real PostgreSQL, a refused facet read is recorded and the rest goes on.

`tests/test_facet_refusal.py` proves the mechanism and its consequences with a fake driver
error. This file proves the thing a fake cannot: that a *real* source's refusal of a *real*
metadata read is classified from the driver's own SQLSTATE and never from its message.

The refused read is `PostgresConnector`'s own view-definition query, imported verbatim rather
than paraphrased, so what is refused here is exactly what discovery runs. `pg_get_viewdef` is
the function it calls; revoking EXECUTE on that one overload from PUBLIC makes a
least-privilege login's view-definition read fail with SQLSTATE 42501 while its roster read
keeps working -- which is the shape of the real problem: a login that can inventory a source
and cannot read one facet of it.

Everything created here is rolled back: the grant is restored and the role dropped in the
fixture's own teardown, and the database itself is the journey fixture's private one, dropped
with it (`scripts/verify_database_footprint_live.py` sets that discipline).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Sequence
from typing import Any

import asyncpg
import pytest_asyncio
from sqlalchemy.engine import URL, make_url

from aida.capability_states import (
    REASON_SOURCE_DENIED_READ,
    CapabilityState,
    is_permission_refusal,
)
from aida.connectors.discovery import (
    FACET_VIEW_DEFINITIONS,
    apply_view_definitions,
    build_table_map_from_column_rows,
    facet_read_scope,
    read_facet,
)
from aida.connectors.postgres import _VIEW_DEFINITION_SQL
from aida.discovery_receipt import DiscoveryReceipt
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    JourneySource,
    _postgres,
)

SCHEMA = "footprint_context_sample"
#: The one overload `_VIEW_DEFINITION_SQL` calls: `pg_get_viewdef(c.oid, true)`.
VIEWDEF = "pg_catalog.pg_get_viewdef(oid, boolean)"
#: The roster read, standing in for the inventory facet that keeps working throughout.
#: Written out rather than interpolated from `SCHEMA`: it is one fixed statement, and a
#: constant with nothing to inject is the only kind this file needs.
ROSTER_SQL = (
    "SELECT table_schema, table_name FROM information_schema.tables "
    "WHERE table_schema = 'footprint_context_sample' ORDER BY table_name"
)


async def _fetch(url: URL, sql: str, *, user: str, password: str) -> Sequence[Any]:
    connection = await asyncpg.connect(
        user=user,
        password=password,
        host=url.host,
        port=url.port or 5432,
        database=url.database,
    )
    try:
        return await connection.fetch(sql)
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def refused(_postgres: JourneySource) -> AsyncIterator[tuple[URL, str, str]]:  # noqa: F811
    """The journey's PostgreSQL source with one facet genuinely closed to one login.

    The login is granted the sample schema, so it inventories the source normally. EXECUTE on
    `pg_get_viewdef` is revoked from PUBLIC, so the view-definition read -- and only that read
    -- is refused. Both are undone in the teardown below.
    """
    source = _postgres
    url = make_url(source.dsn)
    role = f"aida_facet_refusal_{os.getpid()}"
    password = secrets.token_hex(16)
    await source.execute(
        f"DROP ROLE IF EXISTS {role}; "
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}'; "
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}; "
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role};"
    )
    try:
        yield url, role, password
    finally:
        await source.execute(
            f"GRANT EXECUTE ON FUNCTION {VIEWDEF} TO PUBLIC; "
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {role}; "
            f"REVOKE ALL ON SCHEMA {SCHEMA} FROM {role}; "
            f"DROP ROLE IF EXISTS {role};"
        )


async def test_a_real_refusal_costs_one_facet_and_leaves_the_rest_readable(
    refused: tuple[URL, str, str], _postgres: JourneySource  # noqa: F811
) -> None:
    url, role, password = refused

    # The control first: with the privilege in place, this login reads the definition.
    granted = await _fetch(url, _VIEW_DEFINITION_SQL, user=role, password=password)
    assert [row["table_name"] for row in granted if row["definition"]]

    await _postgres.execute(f"REVOKE EXECUTE ON FUNCTION {VIEWDEF} FROM PUBLIC")

    with facet_read_scope() as scope:
        definitions = await read_facet(
            FACET_VIEW_DEFINITIONS,
            _fetch(url, _VIEW_DEFINITION_SQL, user=role, password=password),
        )
        # The same login, the same connection settings, the facet next door: still fine.
        roster = await _fetch(url, ROSTER_SQL, user=role, password=password)

    assert list(definitions) == []
    assert [row["table_name"] for row in roster]
    assert scope.outcomes == {
        FACET_VIEW_DEFINITIONS: (
            CapabilityState.PERMISSION_DENIED,
            REASON_SOURCE_DENIED_READ,
        )
    }


async def test_the_receipt_keeps_the_code_and_not_the_drivers_words(
    refused: tuple[URL, str, str], _postgres: JourneySource  # noqa: F811
) -> None:
    """INV-6 against a real driver. PostgreSQL's own message for this refusal names the
    function; another engine's names the relation and can quote the row that provoked it.
    What reaches the receipt is the SQLSTATE's classification and a reason code."""
    url, role, password = refused
    await _postgres.execute(f"REVOKE EXECUTE ON FUNCTION {VIEWDEF} FROM PUBLIC")

    message = ""
    try:
        await _fetch(url, _VIEW_DEFINITION_SQL, user=role, password=password)
    except asyncpg.PostgresError as exc:
        message = str(exc)
        assert exc.sqlstate == "42501"
        assert is_permission_refusal(exc) is True
    assert message, "the source was expected to refuse this read"

    receipt = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={"views": True}
    )
    with facet_read_scope() as scope:
        await read_facet(
            FACET_VIEW_DEFINITIONS,
            _fetch(url, _VIEW_DEFINITION_SQL, user=role, password=password),
        )
    for facet, (state, reason) in scope.drain().items():
        receipt.record_facet_outcome(facet, state=state, reason=reason)
    body = receipt.as_json("COMPLETE")

    assert body["facets"]["view_definitions"]["state"] == "PERMISSION_DENIED"
    assert body["facets"]["view_definitions"]["reason"] == "SOURCE_DENIED_READ"
    assert message not in str(body)
    assert "pg_get_viewdef" not in str(body)


async def test_a_refused_definition_is_not_an_empty_definition(
    refused: tuple[URL, str, str], _postgres: JourneySource  # noqa: F811
) -> None:
    """The assembly half, live. A refused read returns no rows, so no view gets a
    definition attached at all -- which is why the facet's own recorded state is the only
    thing that can tell a reader the difference between "refused" and "this view has no
    body". `apply_view_definitions` records a NULL *definition column* as unavailable; it
    never sees a refused read, because a refused read has no row to carry."""
    url, role, password = refused
    await _postgres.execute(f"REVOKE EXECUTE ON FUNCTION {VIEWDEF} FROM PUBLIC")
    tables = build_table_map_from_column_rows(
        [
            {
                "table_schema": SCHEMA,
                "table_name": "customer_revenue",
                "table_type": "VIEW",
                "column_name": "customer_id",
                "ordinal_position": 1,
                "data_type": "integer",
                "is_nullable": "YES",
            }
        ]
    )

    with facet_read_scope() as scope:
        rows = await read_facet(
            FACET_VIEW_DEFINITIONS,
            _fetch(url, _VIEW_DEFINITION_SQL, user=role, password=password),
        )
    apply_view_definitions(tables, [dict(row) for row in rows])

    assert tables[SCHEMA]["customer_revenue"].view_definition is None
    assert FACET_VIEW_DEFINITIONS in scope.outcomes
