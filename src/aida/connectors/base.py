from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    catalogs: bool = True
    schemas: bool = True
    constraints: bool = False
    indexes: bool = False
    partitions: bool = False
    explain: bool = False
    query_history: bool = False
    delegated_identity: bool = False
    approximate_statistics: bool = False
    # Envelope 1.1 (gap/02 N1). Default False so a connector that has not
    # implemented an axis keeps reporting honestly (INV-9) without any edit.
    views: bool = False
    routines: bool = False
    object_comments: bool = False
    grants: bool = False
    # PR-2 (ADR-0014 exception path). Value-free statistics (row estimates,
    # null rates, distinct estimates, lengths) are always computed by
    # `profile_table` regardless of this flag. Actual ranges/top-values are a
    # different, much more sensitive query class -- reading real column
    # contents rather than shapes -- so a connector must opt in explicitly by
    # overriding `Connector.profile_column_values` AND setting this True.
    # Default False so every connector that has not implemented it keeps
    # reporting honestly (fail-closed, matching the `views`/`routines`/etc.
    # convention above) rather than silently claiming support it lacks.
    value_range_profiling: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredColumn:
    name: str
    ordinal_position: int
    physical_type: str
    nullable: bool
    default_expression: str | None = None
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredConstraint:
    name: str
    constraint_type: str
    columns: tuple[str, ...]
    referenced_schema: str | None = None
    referenced_table: str | None = None
    referenced_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveredIndex:
    """CT-3/CN-8. Cost-estimation-only inventory, deliberately not part of the
    envelope 1.1 axes: nothing in lineage or semantic meaning reads an index, so
    it carries none of that axis's unavailable-reason machinery and is grouped
    like a constraint instead.
    """

    name: str
    index_type: str
    columns: tuple[str, ...]
    is_unique: bool = False
    is_primary: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredPartition:
    """CT-3/CN-8. See `DiscoveredIndex` for why this is not an envelope 1.1 axis."""

    name: str
    partition_type: str
    ordinal_position: int
    key_columns: tuple[str, ...] = ()
    high_value: str | None = None


@dataclass(frozen=True, slots=True)
class QueryLogEntry:
    """CN-9. One row of a warehouse's own query history, exactly as a
    connector's `get_query_history()` surfaces it -- the connector-side
    counterpart to `aida.query_history_miner.WarehouseQueryLogEntry` (that
    module's own docstring calls this shape out by name as what a connector
    implementation "only has to produce"). Kept as its own type here rather
    than importing the miner's dataclass: `aida.connectors` is a lower layer
    than the modules that mine query history (module 02 vs. 05/07/12), and a
    connector must not depend upward on a feature module to describe what it
    itself returns. The two are structurally identical by construction; the
    call site that wires a connector's output into
    `mine_and_land_query_history_candidates` does the one-line mapping.

    `sql_text` is the query's own literal SQL text, deliberately including
    any literal values it contains -- INV-6 is not enforced by scrubbing it
    here. It is enforced by what happens to it next: this type is never
    itself a persisted model, `get_query_history()` returns it only in
    memory, and nothing downstream may write `sql_text` to any table --
    only a `query_id` reference and a value-free `QueryStructure` derived by
    parsing past every literal (`extract_query_structure`) may land in
    platform state. See CN-9's tracker row for the gap-proof test this
    invariant still needs once a connector implements this method.
    """

    query_id: str
    sql_text: str
    executed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredViewDefinition:
    """The text a view is defined by, and how much of it the source would give.

    Envelope 1.1 (gap/02 N1). This is the input to view-DDL lineage parsing
    (N2), which is the largest single lineage-coverage win available, so the
    envelope carries the definition verbatim and records honestly when it could
    not: a truncated or unavailable definition must never look like an empty
    one. `definition_sql is None` with a populated `unavailable_reason` is a
    first-class state, not an error.
    """

    definition_sql: str | None
    is_materialized: bool = False
    is_updatable: bool | None = None
    check_option: str | None = None
    truncated: bool = False
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredRoutineParameter:
    name: str | None
    ordinal_position: int
    mode: str
    physical_type: str
    default_expression: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredRoutine:
    """A stored procedure or function, with its body when the source exposes it.

    Envelope 1.1 (gap/02 N1/N3/N12). `body_sql` is what procedure-body parsing
    consumes, and what a read-only proof for procedure-to-tool generation is
    proved against. Same honesty rule as views: unavailable and empty are
    different, and `unavailable_reason` says which.
    """

    name: str
    routine_type: str
    language: str | None = None
    body_sql: str | None = None
    parameters: tuple[DiscoveredRoutineParameter, ...] = ()
    return_type: str | None = None
    is_deterministic: bool | None = None
    security_mode: str | None = None
    source_description: str | None = None
    truncated: bool = False
    unavailable_reason: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredGrant:
    """One privilege held by one grantee on one object.

    Envelope 1.1 (gap/02 N1). Source-side grants are evidence about the estate,
    never an authority in this platform: nothing here grants anything, and the
    policy engine does not read it to make a decision. It exists so that "who
    can already see this" is answerable, and so a workspace source binding can
    be reviewed against what the source itself permits.
    """

    grantee: str
    grantee_type: str
    privilege: str
    object_type: str
    object_name: str
    schema_name: str | None = None
    is_grantable: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredTable:
    name: str
    object_type: str
    columns: tuple[DiscoveredColumn, ...]
    constraints: tuple[DiscoveredConstraint, ...] = ()
    indexes: tuple[DiscoveredIndex, ...] = ()
    partitions: tuple[DiscoveredPartition, ...] = ()
    source_description: str | None = None
    view_definition: DiscoveredViewDefinition | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredSchema:
    name: str
    tables: tuple[DiscoveredTable, ...]
    routines: tuple[DiscoveredRoutine, ...] = ()
    grants: tuple[DiscoveredGrant, ...] = ()
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredCatalog:
    name: str
    schemas: tuple[DiscoveredSchema, ...]
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryResult:
    rows: tuple[dict[str, Any], ...]
    warehouse_query_id: str | None


@dataclass(frozen=True, slots=True)
class QueryEstimate:
    score: float
    kind: str
    estimated_rows: float | None = None
    estimated_bytes: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ColumnProfileSnapshot:
    name: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int
    min_length: int | None
    max_length: int | None


@dataclass(frozen=True, slots=True)
class TableProfileSnapshot:
    row_count_estimate: int | None
    sampled_row_count: int
    columns: tuple[ColumnProfileSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ColumnValueProfileSnapshot:
    """PR-2: the value-bearing counterpart to `ColumnProfileSnapshot`.

    Only produced when a policy-approved classification-specific exception
    (`ProfilingExceptionPolicy`) is APPROVED for the column's classification
    *and* the connector's `capabilities.value_range_profiling` is True --
    everywhere else the platform only ever computes `ColumnProfileSnapshot`
    (ADR-0014). Every field here is real source data and is persisted only
    into a `ColumnValueProfileArtifact` with a retention/expiry pinned at
    capture time, never onto the value-free `ColumnProfile` row.
    """

    name: str
    min_value: str | None
    max_value: str | None
    # (value, count) pairs, most frequent first, bounded to the caller's `top_n`.
    top_values: tuple[tuple[str, int], ...] = ()


class ConnectorValueProfilingUnsupported(NotImplementedError):
    """Raised by the default `Connector.profile_column_values` implementation.

    A connector that has not implemented the value-bearing query path fails
    closed with this rather than silently returning an empty/simulated
    result -- callers must treat "unsupported" and "captured nothing" as
    distinguishable outcomes.
    """


class ConnectorQueryHistoryUnsupported(NotImplementedError):
    """Raised by the default `Connector.get_query_history` implementation.

    CN-9 / INV-9: a connector that has not implemented real warehouse
    query-history extraction fails closed with this rather than silently
    returning an empty sequence -- callers (and `capabilities.query_history`,
    which must independently be `True` before this is even attempted) must
    be able to tell "unsupported" apart from "the warehouse logged nothing
    in this window."
    """


class Connector(ABC):
    """Source access with structured arguments only.

    Deliberately has no SQL-accepting member: the `estimate_read_query` /
    `execute_read_query` pair lives on `aida.connectors.sql_execution.SqlExecutor`
    so that INV-2 (one execution choke point) is enforced by the type system and
    the import graph rather than by convention. See that module for the argument.
    """

    connector_type: str
    dialect: str

    @property
    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        """CN-3/PR-5. Discovery as a sequence of bounded batches instead of one
        all-at-once return.

        Deliberately NOT `@abstractmethod`: a 100K-table source timing out
        `discover()` before it can return anything (and, downstream, before
        anything can be persisted -- see `discover_datasource`) is a real
        `PostgresConnector`-scale problem today; the other five connectors have
        no comparable scale harness exercising them, so forcing each to grow a
        real streaming implementation now would be unproven, unmotivated churn
        against passing connectors. `PostgresConnector` is the only override;
        every other connector inherits this default, which just wraps the
        existing `discover()` as a single batch -- zero behaviour change, zero
        risk to their existing tests. A caller that wants incremental
        persistence/heartbeating (`discover_datasource`) can drive any
        connector through this uniformly; `batch_size` is a hint a connector is
        free to ignore, as this default does.
        """
        yield await self.discover()

    @abstractmethod
    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        raise NotImplementedError

    async def profile_column_values(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        top_n: int,
        timeout_seconds: int,
    ) -> tuple[ColumnValueProfileSnapshot, ...]:
        """PR-2: read actual ranges/top-values for `column_names`.

        Deliberately NOT `@abstractmethod` -- unlike `profile_table`, no
        connector is required to implement this. The default fails closed
        rather than every other connector subclass needing a no-op override:
        a connector that has not implemented the real value-bearing query
        must never silently claim support it lacks (INV-9-style honesty).
        Callers must gate a call here behind both an APPROVED, unrevoked
        `ProfilingExceptionPolicy` for the column's classification AND
        `self.capabilities.value_range_profiling` -- this method does not
        itself know about policy state.
        """
        raise ConnectorValueProfilingUnsupported(
            f"{type(self).__name__} does not support value-range profiling"
        )

    async def get_query_history(
        self,
        *,
        since: datetime,
        limit: int = 5_000,
        timeout_seconds: int = 30,
    ) -> tuple[QueryLogEntry, ...]:
        """CN-9: read this warehouse's own record of queries it has run,
        bounded to `limit` rows no older than `since`.

        Deliberately NOT `@abstractmethod`, the same shape as
        `profile_column_values`: most connectors have not implemented this
        yet, and the default must fail closed rather than every connector
        subclass needing a no-op override. Callers must gate a call here
        behind `self.capabilities.query_history` -- per module 02 §11 that
        flag is derived from a certification result, never hand-declared,
        so it stays `False` (INV-9) until a real end-to-end mining run has
        been proven against a live account, not merely until this method
        has been overridden.

        Returns entries in memory only. A connector implementation must
        read only the query's own SQL text and timing -- never the rows or
        bytes that query itself returned -- and a caller must never persist
        `entry.sql_text` verbatim to any table (INV-6); only a `query_id`
        reference and structure derived by parsing past every literal may
        land in platform state, exactly as `aida.query_history_miner`
        already does for every candidate it produces.
        """
        raise ConnectorQueryHistoryUnsupported(
            f"{type(self).__name__} does not support query-history extraction"
        )
