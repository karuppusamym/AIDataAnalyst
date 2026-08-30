"""In-memory stand-ins for the infrastructure the Tier-0 suite is not allowed to need.

The suite runs in the default `pytest` invocation with no PostgreSQL, no Neo4j,
no Kafka and no source database. That constraint is deliberate: an invariant test
nobody can run is an invariant nobody checks. These doubles are what make the
remaining five invariants provable without standing any of that up.

They are written to be *strict*. A double that silently absorbs an unexpected
call turns its test into a test that cannot fail, which is worse than no test at
all -- so `ExplodingSession` raises on any attribute access, and
`RecordingGraphDriver` records statements verbatim rather than pretending to be
Neo4j.
"""

from typing import Any
from uuid import UUID, uuid4

from aida.connectors.base import ConnectorCapabilities, QueryEstimate, QueryResult
from aida.models import SourceBinding
from aida.security_types import SecurityContext


def selected_entity(statement: Any) -> Any:
    """The ORM entity a `select()` targets, or None.

    Used to tell single-column `scalars` calls apart. Once the query gateway grew an
    authorization gate there were two such calls on the same path -- the sensitive
    classification lookup and the source-binding lookup -- and both are one column
    wide, so the width-based routing these doubles use everywhere else stopped being
    enough to distinguish them.
    """
    descriptions = getattr(statement, "column_descriptions", ()) or ()
    return descriptions[0].get("entity") if descriptions else None


class ExplodingSession:
    """A database session that fails the test if it is touched at all.

    Used by the INV-5 cross-tenant harness: a tenancy check that fires *after*
    the handler has already queried the database is not tenant isolation, it is
    a filter. Handing the handler a session that raises on first use is what
    proves the denial happens before any data access.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"the handler reached session.{name} before denying a foreign tenant; "
            "the tenancy check must fire before any data access"
        )


class RecordingSession:
    """Captures everything a unit of work would persist, without a database.

    `added` holds the ORM instances passed to `session.add`, in order -- which is
    exactly the set of rows a real commit would write, and therefore exactly the
    surface INV-6 (no source values in control-plane tables) and INV-7 (an audit
    row per mutation) need to inspect.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits = 0
        self.flushes = 0

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def delete(self, instance: Any) -> None:
        self.deleted.append(instance)
        if instance in self.added:
            self.added.remove(instance)

    async def flush(self) -> None:
        self.flushes += 1
        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = uuid4()
            # Approximate what a real `session.flush()` populates via column
            # defaults (status="DRAFT", created_at=..., ...): this double never
            # touches a real engine, so SQLAlchemy's own default machinery never
            # runs, and a handler that reads a just-created row back to build its
            # response (as most of these do) would otherwise see None where a
            # live flush would have filled in the default.
            table = getattr(type(instance), "__table__", None)
            if table is None:
                continue
            for column in table.columns:
                if getattr(instance, column.name, None) is not None:
                    continue
                default = column.default
                if default is None:
                    continue
                value = default.arg(None) if default.is_callable else default.arg
                setattr(instance, column.name, value)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:  # pragma: no cover - defensive
        pass

    def added_of(self, model: type) -> list[Any]:
        return [instance for instance in self.added if isinstance(instance, model)]


class ScriptedResult:
    """The `.all()` / `.scalars()` shape SQLAlchemy returns, backed by a list."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


class ScriptedSession(RecordingSession):
    """A `RecordingSession` that also answers reads from a scripted queue.

    Queries are answered in call order rather than by inspecting the statement:
    interpreting SQLAlchemy Core expressions in a test double would be a second
    implementation of the ORM, and a wrong one. Call order is deterministic for
    the paths this suite drives, and running out of scripted answers raises
    rather than returning `None`, so a changed query sequence fails loudly
    instead of quietly producing an empty result the assertions then "pass" on.
    """

    def __init__(
        self,
        *,
        scalar_results: list[Any] | None = None,
        scalars_results: list[list[Any]] | None = None,
        execute_results: list[list[Any]] | None = None,
        get_results: dict[Any, Any] | None = None,
    ) -> None:
        super().__init__()
        self._scalar = list(scalar_results or [])
        self._scalars = list(scalars_results or [])
        self._execute = list(execute_results or [])
        self._get = dict(get_results or {})

    async def scalar(self, _statement: Any) -> Any:
        if not self._scalar:
            raise AssertionError("ScriptedSession.scalar called more times than scripted")
        return self._scalar.pop(0)

    async def scalars(self, _statement: Any) -> ScriptedResult:
        if not self._scalars:
            raise AssertionError("ScriptedSession.scalars called more times than scripted")
        return ScriptedResult(self._scalars.pop(0))

    async def execute(self, _statement: Any) -> ScriptedResult:
        if not self._execute:
            raise AssertionError("ScriptedSession.execute called more times than scripted")
        return ScriptedResult(self._execute.pop(0))

    async def get(self, _model: type, identity: Any) -> Any:
        return self._get.get(identity)


class FakeSqlExecutor:
    """A connector that returns caller-supplied rows without touching a source.

    Substituted for `open_execution_session` so the query gateway's real
    end-to-end path -- guard, estimate, cost gate, execute, mask, persist, audit,
    publish -- runs in-process against rows the test controls. The sentinel
    values planted in those rows are what INV-6 then hunts for in everything the
    gateway persisted.
    """

    def __init__(
        self,
        rows: tuple[dict[str, Any], ...],
        *,
        capabilities: ConnectorCapabilities | None = None,
        estimate: QueryEstimate | None = None,
        warehouse_query_id: str | None = "fake-warehouse-id",
    ) -> None:
        self._rows = rows
        self._capabilities = capabilities or ConnectorCapabilities(explain=True)
        self._estimate = estimate or QueryEstimate(score=1.0, kind="EXPLAIN", estimated_rows=1.0)
        self._warehouse_query_id = warehouse_query_id
        self.statements: list[str] = []

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self._capabilities

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        self.statements.append(sql)
        return self._estimate

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        self.statements.append(sql)
        return QueryResult(rows=self._rows, warehouse_query_id=self._warehouse_query_id)


class RecordingGraphSession:
    def __init__(self, log: list[tuple[str, dict[str, Any]]]) -> None:
        self._log = log

    async def run(self, statement: str, **parameters: Any) -> None:
        self._log.append((statement, parameters))

    async def __aenter__(self) -> "RecordingGraphSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class RecordingGraphDriver:
    """Captures the exact projection a projector would write to Neo4j.

    Deliberately not a Cypher interpreter. A hand-written interpreter would be a
    second, unreviewed implementation of the graph store, and a test that passes
    because two of my own approximations agree proves nothing. Recording the
    statement/parameter stream verbatim is enough for what INV-1 actually
    asserts: that the projection is a pure function of authoritative PostgreSQL
    state, so wiping the graph and replaying reproduces it byte-for-byte.
    """

    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []

    def session(self) -> RecordingGraphSession:
        return RecordingGraphSession(self.log)

    def projected_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for _statement, parameters in self.log:
            rows.extend(parameters.get("rows", []) or [])
        return rows

    def statements(self) -> list[str]:
        return [statement for statement, _ in self.log]


def security_context(
    *,
    organization_id: UUID | None,
    principal_id: str = "test-principal",
    roles: frozenset[str] = frozenset({"Analyst"}),
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="USER",
        organization_id=organization_id,
        roles=roles,
    )


class CatalogSession(RecordingSession):
    """Answers the query gateway's catalog lookups by statement *shape*.

    Routing on shape rather than call order is deliberate. `query_gateway.py` is
    under active refactor -- the validation pipeline was extracted while this
    suite was being written -- and a double that answers "first execute, then the
    second execute" breaks the moment the phases are reordered, taking an
    invariant test down with it for a reason that has nothing to do with the
    invariant.

    Three lookups exist on the path, and their SELECT lists distinguish them
    unambiguously: three columns (catalog, schema, table) is the authorised-table
    lookup, four columns (…, column) is the column-resolution lookup, and the
    single-column `scalars` call is the sensitive-classification lookup. Anything
    else raises, so a genuinely new lookup fails loudly here instead of silently
    receiving an empty result the assertions would then "pass" on.
    """

    def __init__(
        self,
        *,
        tables: list[tuple[str, str, str]],
        columns: list[tuple[str, str, str, str]],
        sensitive_columns: list[str],
        bindings: list[SourceBinding] | None = None,
    ) -> None:
        super().__init__()
        self._tables = tables
        self._columns = columns
        self._sensitive = sensitive_columns
        # No bindings by default, which resolves to no workspace at all -- the state
        # the authorization gate treats according to its unresolved posture. That is
        # the honest default for a double: these tests are about the gateway, and a
        # binding invented here would quietly assert an access grant they never made.
        self._bindings = bindings or []

    async def execute(self, statement: Any) -> ScriptedResult:
        width = len(getattr(statement, "column_descriptions", ()) or ())
        if width == 3:
            return ScriptedResult(list(self._tables))
        if width == 4:
            return ScriptedResult(list(self._columns))
        raise AssertionError(
            f"CatalogSession received an unrecognised {width}-column statement; the "
            "gateway grew a catalog lookup this double does not model"
        )

    async def scalars(self, statement: Any) -> ScriptedResult:
        if selected_entity(statement) is SourceBinding:
            return ScriptedResult(list(self._bindings))
        return ScriptedResult(list(self._sensitive))

    async def get(self, _model: type, _identity: Any) -> Any:  # pragma: no cover - unused
        return None


class ModelRoutedSession(RecordingSession):
    """Answers `session.scalars(select(Model)...)` from a per-model row table.

    Routes on the entity the statement selects rather than on call order, so the
    double keeps working if a projector reorders its loads. An unmapped model
    raises, so a projector that starts loading a new entity fails here loudly
    instead of receiving an empty list and "reconstructing" a graph with a
    silently missing node type -- which is exactly the failure INV-1's rebuild
    test exists to detect.
    """

    def __init__(
        self,
        *,
        rows_by_model: dict[type, list[Any]],
        get_results: dict[Any, Any] | None = None,
    ) -> None:
        super().__init__()
        self._rows_by_model = rows_by_model
        self._get = dict(get_results or {})

    def _entity(self, statement: Any) -> type | None:
        descriptions = getattr(statement, "column_descriptions", None) or ()
        for description in descriptions:
            entity = description.get("entity")
            if entity is not None:
                return entity  # type: ignore[no-any-return]
        return None

    async def scalars(self, statement: Any) -> ScriptedResult:
        entity = self._entity(statement)
        if entity not in self._rows_by_model:
            raise AssertionError(
                f"ModelRoutedSession has no rows configured for {entity}; the "
                "projector loads an entity this harness does not model"
            )
        return ScriptedResult(list(self._rows_by_model[entity]))

    async def execute(self, statement: Any) -> ScriptedResult:
        return await self.scalars(statement)

    async def get(self, _model: type, identity: Any) -> Any:
        return self._get.get(identity)

    async def __aenter__(self) -> "ModelRoutedSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None
