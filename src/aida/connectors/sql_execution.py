"""The connector SQL-execution surface (INV-2, ADR-0004, tracker QG-7).

`Connector` (`aida.connectors.base`) carries the operations that reach a source
with *structured* arguments only -- capability reporting, connection tests,
structural discovery and bounded profiling. None of them accepts caller-supplied
SQL, so none of them can be used to run an arbitrary statement.

The two operations that *do* accept a SQL string live here instead, on a separate
`SqlExecutor` type. That split is what makes INV-2 mechanically enforceable rather
than merely asserted, and it is enforced three ways:

1. `ConnectorRegistry.create` is annotated as returning `Connector`, which has no
   `estimate_read_query` / `execute_read_query` member. Calling either one on a
   registry-produced object is a type error under `mypy --strict`, which runs in CI.
2. The only function that returns a `SqlExecutor` is
   `aida.connectors.execution_access.open_execution_session`, and the import-linter
   contract "INV-2 connector SQL execution is reachable only from the query gateway"
   (`pyproject.toml`) forbids every module except `aida.query_gateway` from
   importing it.
3. `tests/test_tier0_invariants.py::test_no_connector_execution_outside_gateway`
   statically scans every module under `src/aida` for a call to either method,
   catching any dynamic bypass that the two static checks above cannot see.

Widening this surface -- adding another SQL-accepting method, or returning a
`SqlExecutor` from anywhere else -- breaks the platform's central invariant. Do not
do it without an ADR.
"""

from abc import abstractmethod

from aida.connectors.base import Connector, QueryEstimate, QueryResult


class SqlExecutor(Connector):
    """A connector that can be asked to cost and run a SQL statement.

    Every concrete connector implements this; the type exists to keep the
    SQL-accepting surface off `Connector`, which is what the rest of the platform
    is handed.
    """

    @abstractmethod
    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        raise NotImplementedError

    @abstractmethod
    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        raise NotImplementedError
