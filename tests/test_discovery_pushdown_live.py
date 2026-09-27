"""R11-FP01: against real PostgreSQL and SQL Server, an excluded schema is never read.

The footprint journey's private sources gain a second schema holding a view and a routine. With
the selection's schema scope pushed down, the connector's own queries never return it -- this is
what the source was asked, not what was filtered afterwards. An include list returns only the
schemas it names, and PostgreSQL's streaming path honours the scope in every batch. Each engine is
skipped when its server is not reachable.
"""

from __future__ import annotations

from collections.abc import Iterable

from aida.connectors.base import DiscoveredCatalog
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    CONNECTORS,
    JourneySource,
    _postgres,
    _sqlserver,
    source,
)

KEPT = "footprint_context_sample"
EXCLUDED = "footprint_excluded"
_EXCLUDED_OBJECTS = {
    "postgres": (
        f"CREATE SCHEMA {EXCLUDED}; "
        f"CREATE VIEW {EXCLUDED}.secret_view AS SELECT 1 AS marker; "
        f"CREATE FUNCTION {EXCLUDED}.secret_function() RETURNS integer LANGUAGE sql AS 'SELECT 1';"
    ),
    "sqlserver": (
        f"CREATE SCHEMA {EXCLUDED};\nGO\n"
        f"CREATE VIEW {EXCLUDED}.secret_view AS SELECT 1 AS marker;\nGO\n"
        f"CREATE PROCEDURE {EXCLUDED}.secret_procedure AS SELECT 1 AS marker;"
    ),
}


def _schemas(catalogs: Iterable[DiscoveredCatalog]) -> set[str]:
    return {schema.name.lower() for catalog in catalogs for schema in catalog.schemas}


async def test_an_excluded_schema_is_never_read_and_an_include_list_reads_only_its_schemas(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    await source.execute(_EXCLUDED_OBJECTS[source.connector_type])
    connect = CONNECTORS[source.connector_type]

    assert {KEPT, EXCLUDED} <= _schemas(await connect(source.dsn).discover())

    excluding = connect(source.dsn)
    assert excluding.scope_discovery(include_schemas=[], exclude_schemas=["FOOTPRINT_EXCL*"])
    excluded_run = _schemas(await excluding.discover())
    assert EXCLUDED not in excluded_run and KEPT in excluded_run

    including = connect(source.dsn)
    assert including.scope_discovery(include_schemas=[KEPT], exclude_schemas=[])
    assert _schemas(await including.discover()) == {KEPT}

    streamed: set[str] = set()
    async for batch in excluding.discover_streaming(batch_size=2):
        streamed |= _schemas(batch)
    assert EXCLUDED not in streamed and KEPT in streamed
