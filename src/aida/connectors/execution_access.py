"""The single entry point to a connector's SQL-execution surface (INV-2, QG-7).

Importing this module is restricted to `aida.query_gateway` by the import-linter
contract "INV-2 connector SQL execution is reachable only from the query gateway"
in `pyproject.toml`. Adding an importer means editing that contract, which is a
reviewable change to the platform's central invariant rather than an ordinary
import somebody adds without noticing.

See `aida.connectors.sql_execution` for the full enforcement argument.
"""

from aida.connectors.registry import connector_registry
from aida.connectors.sql_execution import SqlExecutor


def open_execution_session(connector_type: str, dsn: str) -> SqlExecutor:
    """Return the connector for `connector_type` as a SQL executor.

    Fails closed (INV-4): a registered connector that does not implement the
    execution surface is a configuration error, not a degraded execution path.
    """
    connector = connector_registry.create(connector_type, dsn)
    if not isinstance(connector, SqlExecutor):
        raise ValueError(f"connector does not support governed read execution: {connector_type}")
    return connector
