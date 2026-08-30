from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
