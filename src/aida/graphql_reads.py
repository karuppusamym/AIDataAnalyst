"""The metadata reads the GraphQL facade composes (R11-GQL01, design section 13).

**The same decisions REST makes, not a second catalog.** Every read here is the
read a REST route already serves, decided the same way, from the same rows, in
the same order, with the same cursor encoding:

========================================  ===========================================
GraphQL field                             REST route it answers for
========================================  ===========================================
``datasource(id)`` / ``Table.datasource``  ``GET /v1/datasources/{id}``
``datasources``                           ``GET /v1/organizations/{id}/datasources``
``DataSource.tables`` / ``tables(dsId)``  ``GET /v1/datasources/{id}/tables``
``tables`` (organization-wide)            ``GET /v1/organizations/{id}/catalog/rows``
``table(id)`` / ``Table.columns``         ``GET /v1/tables/{id}/columns``
``Table.constraints``                     ``GET /v1/tables/{id}/constraints``
``Table.description``                     ``GET /v1/tables/{id}/description``
``Column.businessDescription``            ``GET /v1/tables/{id}/column-documentation``
``contextProducts(projectId)``            ``GET /v1/projects/{id}/context-products``
``contextProductVersion(id)``             ``GET /v1/context-product-versions/{id}``
``lineageImpact(dsId, nodeId)``           ``GET /v1/datasources/{id}/unified-lineage/impact/{node}``
``lineageGraph(dsId)``                    ``GET /v1/datasources/{id}/unified-lineage/graph``
``contextProductCoverage(versionId)``     ``GET /v1/context-product-versions/{id}/compile``
                                          (its ``context.coverage`` and ``source_freshness``)
``routineParseCoverage(dsId, routineId)`` ``GET /v1/datasources/{id}/procedures/{id}/``
                                          ``parse-coverage``
``triggerParseCoverage(dsId, triggerId)`` ``GET /v1/datasources/{id}/triggers/{id}/``
                                          ``parse-coverage``
========================================  ===========================================

The authorization pieces are the shared ones those routes call --
`aida.security.enforce_organization` for the tenant boundary and
`aida.authorization_gate.gate` (action ``READ_METADATA``) for the workspace
decision, against the same resource type and id each route uses -- and the role
sets below are the ones those routes declare; `tests/test_graphql_api.py` reads
the live routes' `require_roles` closures and fails if either side moves. The
REST handlers compose these same shared checks inline, which is why there was
no route-only permission check to extract first.

**One deliberate difference, stricter than REST.** An organization-wide
`tables` listing authorizes each datasource *before* it counts or pages, so a
datasource the caller may not read contributes nothing to `totalCount` and
never shortens a page. `GET /v1/organizations/{id}/catalog/rows` drops such rows
after paging and counts them in `total`; the rows it returns are the same. (The
lineage reads were a second until R11-D28: they ask the datasource's workspace gate,
as `DataSource.tables` does, and the unified-lineage routes now ask it too. The two
parse-coverage reads ask it the same way, as their routes have since R11-D30.)

**Request-scoped, never shared.** A `ReadScope` is built per request, for one
caller, and dropped with it. Its loaders batch and cache by object id, and the
cache therefore lives exactly as long as one caller's request -- the scope *is*
part of every cache key. A loader returns rows only; every object a resolver
hands back is authorized after it is loaded, including a cache hit, so batching
never folds two objects into one decision. The one memo that exists -- a gate
decision per (resource type, resource id, datasource) -- is per caller and per
request, which is the identity the decision was made for.

**One session, one statement at a time.** graphql-core resolves sibling fields
and list items concurrently; an `AsyncSession` serves one statement at a time.
Every statement and every gate call therefore runs under the scope's lock, and
nothing awaits a loader while holding it.

**No source SQL.** Nothing here reaches a connector or the query gateway: every
statement is against Atlas' own catalog tables. `tests/test_graphql_api.py`
asserts that statically and behaviourally.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import ColumnElement, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased
from strawberry.dataloader import DataLoader

from aida.authorization_gate import AuthorizationDenied, gate
from aida.capability_states import parse_coverage_state
from aida.catalog_read_model import _latest_approved_documentation
from aida.column_documentation import current_descriptions_for_table
from aida.context_compiler import coverage_section, freshness_section
from aida.context_product_read_service import (
    COMPILER_LIFECYCLE_READERS,
    COMPILER_ROLES,
    UnresolvedReferencesError,
    decide_compiled_read,
    load_coverage_extras,
    resolve_pinned_references,
)
from aida.context_product_reads import (
    CONTEXT_PRODUCT_READERS,
    _product_read,
    _version_read,
    context_product_listing,
    read_context_product_version,
)
from aida.description_withdrawal import latest_withdrawn_table_version
from aida.envelope_models import MetadataRoutine, MetadataTrigger
from aida.events import record_audit
from aida.graphql_limits import DEFAULT_LIMITS, GraphQLLimits
from aida.models import (
    AssetDocumentationVersion,
    ColumnDocumentationVersion,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataSource,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    Organization,
)
from aida.pagination import InvalidCursor, apply_keyset, decode_cursor, encode_cursor
from aida.procedure_lineage_models import RoutineParseCoverage, TriggerParseCoverage
from aida.schemas import (
    ContextProductRead,
    ContextProductVersionRead,
    DataSourceSummaryRead,
    MetadataColumnRead,
    MetadataConstraintRead,
    MetadataTableRead,
    RoutineParseCoverageRead,
    TriggerParseCoverageRead,
    UnifiedLineageEdgeRead,
    UnifiedLineageGraphRead,
    UnifiedLineageImpactNodeRead,
    UnifiedLineageImpactRead,
    UnifiedLineageNodeRead,
)
from aida.scope_search import search_predicate
from aida.security import enforce_organization
from aida.security_types import SecurityContext
from aida.unified_lineage_service import (
    UNIFIED_LINEAGE_READER_ROLES,
    LineageNodeNotFoundError,
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
)
from atlas.platform.config import Settings
from atlas.platform.context import get_correlation_id

__all__ = [
    "CATALOG_READ_ROLES",
    "CONTEXT_COMPILER_LIFECYCLE_READERS",
    "CONTEXT_COMPILER_ROLES",
    "COVERAGE_CONSUMPTION_CHANNEL",
    "DATASOURCE_READ_ROLES",
    "GRAPHQL_ENDPOINT_ROLES",
    "ColumnDescription",
    "Page",
    "ProductCoverage",
    "ReadRefused",
    "ReadScope",
    "TableDescription",
    "column_business_description",
    "get_context_product_coverage",
    "get_context_product_version",
    "get_datasource",
    "get_lineage_graph",
    "get_lineage_impact",
    "get_routine_parse_coverage",
    "get_table",
    "get_trigger_parse_coverage",
    "list_columns",
    "list_constraints",
    "list_context_products",
    "list_coverage_items",
    "list_datasources",
    "list_datasource_tables",
    "list_lineage_graph_edges",
    "list_lineage_graph_nodes",
    "list_lineage_impact_nodes",
    "list_organization_tables",
    "list_tables",
    "open_read_scope",
    "referenced_table",
    "table_description",
]

#: The roles `GET /v1/datasources/{id}` and `GET /v1/organizations/{id}/datasources`
#: declare. A caller who can find a source by paging can find it by id here too.
DATASOURCE_READ_ROLES: tuple[str, ...] = (
    "PlatformAdmin",
    "OrganizationAdmin",
    "ProjectAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "Operations",
    "Analyst",
    "Viewer",
)
#: The roles every table-level catalog read declares: `GET /v1/datasources/{id}/tables`,
#: `/v1/tables/{id}/columns`, `/constraints`, `/description`, `/column-documentation`
#: and the catalog rows read model.
CATALOG_READ_ROLES: tuple[str, ...] = ("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
#: The roles `GET /v1/context-product-versions/{id}/compile` declares: the one REST route
#: whose answer carries a version's coverage (the Atlas-native targets' `context.coverage`,
#: and `generated_from.source_freshness` beside it), so the roles `contextProductCoverage`
#: requires. The route and this field import the one tuple from
#: `aida.context_product_read_service`; `tests/test_graphql_coverage.py` still reads it back
#: from the live route.
CONTEXT_COMPILER_ROLES: tuple[str, ...] = COMPILER_ROLES
#: Who the compile route lets read a version whatever its status, purpose or quality -- its
#: own set, not the version read's `CONTEXT_PRODUCT_LIFECYCLE_READERS`.
CONTEXT_COMPILER_LIFECYCLE_READERS: frozenset[str] = COMPILER_LIFECYCLE_READERS
#: The consumption channel a coverage read records under, beside the compile route's
#: `COMPILER` and `COMPILER_DOWNLOAD`.
COVERAGE_CONSUMPTION_CHANNEL = "GRAPHQL_COVERAGE"
#: The endpoint admits the union; each field then requires its own route's set,
#: so a DataAdmin can read a datasource here exactly as over REST and is refused
#: its tables exactly as over REST. The parse-coverage routes declare the
#: unified-lineage reader roles, member for member.
GRAPHQL_ENDPOINT_ROLES: tuple[str, ...] = tuple(
    sorted(
        set(DATASOURCE_READ_ROLES)
        | set(CATALOG_READ_ROLES)
        | set(CONTEXT_PRODUCT_READERS)
        | set(UNIFIED_LINEAGE_READER_ROLES)
        | set(CONTEXT_COMPILER_ROLES)
    )
)

_READ_METADATA = "READ_METADATA"
_ACTIVE = "ACTIVE"
_MAX_QUERY_LENGTH = 200
_MAX_FILTER_LENGTH = 30


class ReadRefused(Exception):
    """A field-level refusal. `code` is stable; `reason` is a value-free reason code
    (the gate's own, or one of this module's) and never names the object."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One keyset page. `total` is computed on the first page only, as REST does."""

    items: list[T]
    total: int | None
    end_cursor: str | None
    has_next_page: bool


@dataclass(frozen=True, slots=True)
class TableDescription:
    """The fields `GET /v1/tables/{id}/description` returns, in the same shape."""

    table_id: UUID
    name: str
    source_description: str | None
    readme: str | None
    readme_version: int | None
    approved_by: str | None
    approved_at: datetime | None
    withdrawn_readme: str | None


@dataclass(frozen=True, slots=True)
class ColumnDescription:
    """The approved-description fields `GET /v1/tables/{id}/column-documentation` adds."""

    description: str
    version: int
    approved_by: str | None
    approved_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProductCoverage:
    """A version's coverage, rendered by the compiler's own helpers -- `coverage_section` and
    `freshness_section`, which render the compile route's answer and MCP's resource read -- so
    every entry has exactly the keys and order REST gives it. A section REST leaves out
    because it is empty is an empty list here."""

    version_id: UUID
    product_key: str
    version: int
    status: str
    routines: list[dict[str, Any]]
    views: list[dict[str, Any]]
    meaning: list[dict[str, Any]]
    changed_since_published: list[dict[str, Any]]
    source_freshness: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _ChildPageKey:
    """A page of a table's children. Hashable so the loader can batch and cache it."""

    parent_id: UUID
    organization_id: UUID
    first: int
    after: str | None


@dataclass(eq=False)
class ReadScope:
    """Everything one GraphQL request reads through: its caller, its session, its
    loaders and its decisions. Built by `open_read_scope`; never reused."""

    session: AsyncSession
    context: SecurityContext
    settings: Settings
    organization_id: UUID
    limits: GraphQLLimits = DEFAULT_LIMITS
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    decisions: dict[tuple[str, str, UUID], str | None] = field(default_factory=dict)
    #: A version's coverage, or the refusal, once per request: an alias asking again is
    #: the same read, decided and recorded once.
    coverage: dict[UUID, ProductCoverage | ReadRefused] = field(default_factory=dict)
    #: The stored OKF bundles (and the recorded child reads of them) this request has read, or
    #: the refusal each got, once per request -- `aida.graphql_okf`'s memo, of the same kind.
    okf: dict[tuple[Any, ...], object] = field(default_factory=dict)
    datasources: DataLoader[UUID, DataSource | None] = field(init=False)
    tables: DataLoader[UUID, MetadataTable | None] = field(init=False)
    column_pages: DataLoader[_ChildPageKey, Page[MetadataColumnRead]] = field(init=False)
    constraint_pages: DataLoader[_ChildPageKey, Page[MetadataConstraintRead]] = field(init=False)
    approved_documentation: DataLoader[UUID, AssetDocumentationVersion | None] = field(init=False)
    column_documentation: DataLoader[UUID, dict[UUID, ColumnDocumentationVersion]] = field(
        init=False
    )

    def __post_init__(self) -> None:
        self.datasources = DataLoader(load_fn=self._load_datasources)
        self.tables = DataLoader(load_fn=self._load_tables)
        self.column_pages = DataLoader(load_fn=self._load_column_pages)
        self.constraint_pages = DataLoader(load_fn=self._load_constraint_pages)
        self.approved_documentation = DataLoader(load_fn=self._load_approved_documentation)
        self.column_documentation = DataLoader(load_fn=self._load_column_documentation)

    # --- loaders: rows only, never a decision -------------------------------

    async def _load_datasources(self, keys: list[UUID]) -> list[DataSource | None]:
        async with self.lock:
            rows = (
                await self.session.scalars(select(DataSource).where(DataSource.id.in_(keys)))
            ).all()
        found = {row.id: row for row in rows}
        return [found.get(key) for key in keys]

    async def _load_tables(self, keys: list[UUID]) -> list[MetadataTable | None]:
        async with self.lock:
            rows = (
                await self.session.scalars(select(MetadataTable).where(MetadataTable.id.in_(keys)))
            ).all()
        found = {row.id: row for row in rows}
        return [found.get(key) for key in keys]

    async def _load_column_pages(self, keys: list[_ChildPageKey]) -> list[Page[MetadataColumnRead]]:
        return await _child_pages(
            self,
            keys,
            model=MetadataColumn,
            parent=MetadataColumn.table_id,
            order_columns=(MetadataColumn.ordinal_position, MetadataColumn.id),
            coercers=(int, UUID),
            read=MetadataColumnRead.model_validate,
        )

    async def _load_constraint_pages(
        self, keys: list[_ChildPageKey]
    ) -> list[Page[MetadataConstraintRead]]:
        return await _child_pages(
            self,
            keys,
            model=MetadataConstraint,
            parent=MetadataConstraint.table_id,
            order_columns=(MetadataConstraint.name, MetadataConstraint.id),
            coercers=(str, UUID),
            read=MetadataConstraintRead.model_validate,
        )

    async def _load_approved_documentation(
        self, keys: list[UUID]
    ) -> list[AssetDocumentationVersion | None]:
        async with self.lock:
            current = await _latest_approved_documentation(self.session, list(keys))
        return [current.get(key) for key in keys]

    async def _load_column_documentation(
        self, keys: list[UUID]
    ) -> list[dict[UUID, ColumnDocumentationVersion]]:
        results: list[dict[UUID, ColumnDocumentationVersion]] = []
        for table_id in keys:
            async with self.lock:
                results.append(await current_descriptions_for_table(self.session, table_id))
        return results


def open_read_scope(
    *,
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    organization_id: UUID,
    limits: GraphQLLimits = DEFAULT_LIMITS,
) -> ReadScope:
    """A fresh scope for one request. The only constructor the endpoint uses."""
    return ReadScope(
        session=session,
        context=context,
        settings=settings,
        organization_id=organization_id,
        limits=limits,
    )


# --- decisions ---------------------------------------------------------------


def _require_roles(scope: ReadScope, roles: tuple[str, ...]) -> None:
    """The role gate of the REST route this field answers for."""
    if scope.context.roles.isdisjoint(roles):
        raise ReadRefused("FORBIDDEN", "ROLE_REQUIRED")


def _enforce_tenant(scope: ReadScope, organization_id: UUID) -> None:
    """`enforce_organization`, the call every REST read makes, as a field refusal."""
    try:
        enforce_organization(scope.context, organization_id)
    except HTTPException as refused:
        raise ReadRefused("FORBIDDEN", "CROSS_ORGANIZATION") from refused


async def _authorize(
    scope: ReadScope, *, resource_type: str, resource_id: UUID, datasource_id: UUID
) -> None:
    """The workspace gate, asked about the same resource the REST route asks about.

    Memoized per request for this caller: the same resource asked twice (an alias,
    or a table's columns and then its constraints) is one decision, and a refusal
    stays a refusal for every field that asks again.
    """
    key = (resource_type, str(datasource_id), resource_id)
    if key not in scope.decisions:
        try:
            async with scope.lock:
                await gate(
                    scope.session,
                    scope.context,
                    settings=scope.settings,
                    action=_READ_METADATA,
                    resource_type=resource_type,
                    resource_id=str(resource_id),
                    datasource_id=datasource_id,
                )
        except AuthorizationDenied as denied:
            scope.decisions[key] = denied.reason_code
        else:
            scope.decisions[key] = None
    reason = scope.decisions[key]
    if reason is not None:
        raise ReadRefused("FORBIDDEN", reason)


async def _permitted(scope: ReadScope, *, resource_type: str, datasource_id: UUID) -> bool:
    try:
        await _authorize(
            scope,
            resource_type=resource_type,
            resource_id=datasource_id,
            datasource_id=datasource_id,
        )
    except ReadRefused:
        return False
    return True


async def authorize_table(scope: ReadScope, table: MetadataTable) -> None:
    """Exactly what `GET /v1/tables/{id}/columns` decides before it reads a column."""
    _require_roles(scope, CATALOG_READ_ROLES)
    _enforce_tenant(scope, table.organization_id)
    await _authorize(
        scope, resource_type="table", resource_id=table.id, datasource_id=table.datasource_id
    )


# --- argument checks ---------------------------------------------------------


def _page_size(scope: ReadScope, first: int) -> int:
    if first < 1 or first > scope.limits.max_page_size:
        raise ReadRefused("INVALID_ARGUMENT", "PAGE_SIZE_OUT_OF_RANGE")
    return first


def _bounded(value: str | None, *, maximum: int, minimum: int = 0) -> str | None:
    if value is None:
        return None
    if len(value) > maximum or len(value) < minimum:
        raise ReadRefused("INVALID_ARGUMENT", "ARGUMENT_LENGTH_OUT_OF_RANGE")
    return value


def _decode(after: str, coercers: Sequence[Callable[[str], Any]]) -> tuple[Any, ...]:
    try:
        raw = decode_cursor(after, arity=len(coercers))
        return tuple(coerce(value) for coerce, value in zip(coercers, raw, strict=True))
    except (InvalidCursor, ValueError) as exc:
        raise ReadRefused("INVALID_CURSOR") from exc


def _cursor_of(row: Any, order_columns: Sequence[InstrumentedAttribute[Any]]) -> str:
    return encode_cursor(*(getattr(row, column.key) for column in order_columns))


# --- keyset paging, the same contract as `aida.api._list_page` ----------------


async def _keyset_page[T](
    scope: ReadScope,
    *,
    model: type[Any],
    filters: Sequence[ColumnElement[bool]],
    order_columns: Sequence[InstrumentedAttribute[Any]],
    coercers: Sequence[Callable[[str], Any]],
    read: Callable[[Any], T],
    first: int,
    after: str | None,
) -> Page[T]:
    statement = select(model).where(*filters).order_by(*order_columns)
    total: int | None = None
    async with scope.lock:
        if after is not None:
            # A mapped attribute is a column expression at runtime; `apply_keyset` is
            # annotated for `ColumnElement`, which the ORM attribute type does not name.
            keyset_columns = cast("list[ColumnElement[Any]]", list(order_columns))
            statement = apply_keyset(statement, keyset_columns, _decode(after, coercers))
        else:
            total = int(
                await scope.session.scalar(select(func.count()).select_from(model).where(*filters))
                or 0
            )
        rows = list((await scope.session.scalars(statement.limit(first + 1))).all())
    has_next = len(rows) > first
    rows = rows[:first]
    return Page(
        items=[read(row) for row in rows],
        total=total,
        end_cursor=_cursor_of(rows[-1], order_columns) if rows else None,
        has_next_page=has_next,
    )


async def _child_pages[T](
    scope: ReadScope,
    keys: list[_ChildPageKey],
    *,
    model: type[Any],
    parent: InstrumentedAttribute[UUID],
    order_columns: tuple[InstrumentedAttribute[Any], ...],
    coercers: Sequence[Callable[[str], Any]],
    read: Callable[[Any], T],
) -> list[Page[T]]:
    """First pages for many parents in one statement; later pages one by one.

    A first page is the common nested case (`tables { nodes { columns { ... } } }`),
    so those are batched: one windowed statement returns up to `first + 1` rows per
    parent in the REST route's order, and one grouped count supplies each total.
    A page after a cursor is fetched exactly as the REST route fetches it.
    """
    results: dict[_ChildPageKey, Page[T]] = {}
    by_size: dict[int, list[_ChildPageKey]] = defaultdict(list)
    for key in keys:
        if key.after is None:
            by_size[key.first].append(key)
    for first, group in by_size.items():
        pairs = sorted({(key.parent_id, key.organization_id) for key in group})
        scoped: list[ColumnElement[bool]] = [
            tuple_(parent, model.organization_id).in_(pairs),
            model.status == _ACTIVE,
        ]
        ranked = (
            select(
                model,
                func.row_number()
                .over(partition_by=parent, order_by=list(order_columns))
                .label("rank_in_parent"),
            )
            .where(*scoped)
            .subquery()
        )
        row_alias = aliased(model, ranked)
        async with scope.lock:
            rows = (
                await scope.session.scalars(
                    select(row_alias).where(ranked.c.rank_in_parent <= first + 1)
                )
            ).all()
            counted = await scope.session.execute(
                select(parent, func.count()).where(*scoped).group_by(parent)
            )
            counts: dict[UUID, int] = {row[0]: int(row[1]) for row in counted.all()}
        grouped: dict[UUID, list[Any]] = defaultdict(list)
        for row in rows:
            grouped[getattr(row, parent.key)].append(row)
        for key in group:
            children = sorted(
                grouped.get(key.parent_id, []),
                key=lambda row: tuple(getattr(row, column.key) for column in order_columns),
            )
            page_rows = children[:first]
            results[key] = Page(
                items=[read(row) for row in page_rows],
                total=int(counts.get(key.parent_id, 0)),
                end_cursor=_cursor_of(page_rows[-1], order_columns) if page_rows else None,
                has_next_page=len(children) > first,
            )
    for key in keys:
        if key.after is not None and key not in results:
            results[key] = await _keyset_page(
                scope,
                model=model,
                filters=(
                    model.organization_id == key.organization_id,
                    parent == key.parent_id,
                    model.status == _ACTIVE,
                ),
                order_columns=order_columns,
                coercers=coercers,
                read=read,
                first=key.first,
                after=key.after,
            )
    return [results[key] for key in keys]


# --- the reads -----------------------------------------------------------------


async def get_datasource(scope: ReadScope, datasource_id: UUID) -> DataSourceSummaryRead:
    """`GET /v1/datasources/{id}`: missing is NOT_FOUND, another tenant's is FORBIDDEN."""
    _require_roles(scope, DATASOURCE_READ_ROLES)
    row = await scope.datasources.load(datasource_id)
    if row is None:
        raise ReadRefused("NOT_FOUND")
    _enforce_tenant(scope, row.organization_id)
    return DataSourceSummaryRead.model_validate(row)


async def list_datasources(
    scope: ReadScope,
    *,
    first: int,
    after: str | None,
    q: str | None,
    status: str | None,
) -> Page[DataSourceSummaryRead]:
    """`GET /v1/organizations/{id}/datasources` for the caller's own organization."""
    _require_roles(scope, DATASOURCE_READ_ROLES)
    first = _page_size(scope, first)
    q = _bounded(q, maximum=_MAX_QUERY_LENGTH)
    status = _bounded(status, maximum=_MAX_FILTER_LENGTH)
    _enforce_tenant(scope, scope.organization_id)
    async with scope.lock:
        organization = await scope.session.get(Organization, scope.organization_id)
    if organization is None:
        raise ReadRefused("NOT_FOUND", "ORGANIZATION_NOT_FOUND")
    filters: list[ColumnElement[bool]] = [DataSource.organization_id == scope.organization_id]
    if status:
        filters.append(DataSource.status == status.upper())
    search = search_predicate(q, DataSource.name)
    if search is not None:
        filters.append(search)
    return await _keyset_page(
        scope,
        model=DataSource,
        filters=filters,
        order_columns=(DataSource.name, DataSource.id),
        coercers=(str, UUID),
        read=DataSourceSummaryRead.model_validate,
        first=first,
        after=after,
    )


def _table_filters(
    *, status: str, object_type: str | None, q: str | None
) -> list[ColumnElement[bool]]:
    """`list_tables`' own filters, term for term."""
    filters: list[ColumnElement[bool]] = []
    if status != "ALL":
        filters.append(MetadataTable.status == status)
    if object_type and object_type != "ALL":
        filters.append(MetadataTable.object_type == object_type)
    if q:
        normalized = q.strip().lower()
        filters.append(
            or_(
                func.lower(MetadataTable.name).contains(normalized),
                func.lower(func.coalesce(MetadataTable.source_description, "")).contains(
                    normalized
                ),
            )
        )
    return filters


async def list_tables(
    scope: ReadScope,
    datasource: DataSourceSummaryRead,
    *,
    first: int,
    after: str | None,
    q: str | None,
    object_type: str | None,
    status: str,
) -> Page[MetadataTableRead]:
    """`GET /v1/datasources/{id}/tables`: the datasource's gate, then its tables."""
    _require_roles(scope, CATALOG_READ_ROLES)
    first = _page_size(scope, first)
    q = _bounded(q, maximum=_MAX_QUERY_LENGTH, minimum=2)
    object_type = _bounded(object_type, maximum=_MAX_FILTER_LENGTH)
    status = _bounded(status, maximum=_MAX_FILTER_LENGTH) or _ACTIVE
    _enforce_tenant(scope, datasource.organization_id)
    await _authorize(
        scope,
        resource_type="datasource",
        resource_id=datasource.id,
        datasource_id=datasource.id,
    )
    filters = [
        MetadataTable.organization_id == datasource.organization_id,
        MetadataTable.datasource_id == datasource.id,
        *_table_filters(status=status, object_type=object_type, q=q),
    ]
    return await _keyset_page(
        scope,
        model=MetadataTable,
        filters=filters,
        order_columns=(MetadataTable.name, MetadataTable.id),
        coercers=(str, UUID),
        read=MetadataTableRead.model_validate,
        first=first,
        after=after,
    )


async def list_datasource_tables(
    scope: ReadScope,
    datasource_id: UUID,
    *,
    first: int,
    after: str | None,
    q: str | None,
    object_type: str | None,
    status: str,
) -> Page[MetadataTableRead]:
    """`GET /v1/datasources/{id}/tables` addressed by id, in the route's own order: its
    role gate runs before the datasource is looked up (REST's `require_roles` dependency
    refuses before the handler reads anything), so a caller without the role is refused
    the same way whether or not the id exists."""
    _require_roles(scope, CATALOG_READ_ROLES)
    datasource = await get_datasource(scope, datasource_id)
    return await list_tables(
        scope, datasource, first=first, after=after, q=q, object_type=object_type, status=status
    )


async def list_organization_tables(
    scope: ReadScope,
    *,
    first: int,
    after: str | None,
    q: str | None,
    object_type: str | None,
    status: str,
) -> Page[MetadataTableRead]:
    """Every table in the caller's organization that the caller may read.

    Forbidden datasources are removed *before* the count and the page: each
    distinct datasource holding a matching table is decided once, and only the
    permitted ones are queried. The number of decisions is bounded by
    `max_scope_datasources`; past it the listing is refused as too broad rather
    than silently truncated.
    """
    _require_roles(scope, CATALOG_READ_ROLES)
    first = _page_size(scope, first)
    q = _bounded(q, maximum=_MAX_QUERY_LENGTH, minimum=2)
    object_type = _bounded(object_type, maximum=_MAX_FILTER_LENGTH)
    status = _bounded(status, maximum=_MAX_FILTER_LENGTH) or _ACTIVE
    _enforce_tenant(scope, scope.organization_id)
    filters = [
        MetadataTable.organization_id == scope.organization_id,
        *_table_filters(status=status, object_type=object_type, q=q),
    ]
    cap = scope.limits.max_scope_datasources
    async with scope.lock:
        candidates = list(
            (
                await scope.session.scalars(
                    select(MetadataTable.datasource_id)
                    .where(*filters)
                    .distinct()
                    .order_by(MetadataTable.datasource_id)
                    .limit(cap + 1)
                )
            ).all()
        )
    if len(candidates) > cap:
        raise ReadRefused("SCOPE_TOO_BROAD", "NAME_A_DATASOURCE")
    permitted = [
        datasource_id
        for datasource_id in candidates
        if await _permitted(scope, resource_type="datasource", datasource_id=datasource_id)
    ]
    if not permitted:
        return Page(
            items=[], total=0 if after is None else None, end_cursor=None, has_next_page=False
        )
    filters.append(MetadataTable.datasource_id.in_(permitted))
    return await _keyset_page(
        scope,
        model=MetadataTable,
        filters=filters,
        order_columns=(MetadataTable.name, MetadataTable.id),
        coercers=(str, UUID),
        read=MetadataTableRead.model_validate,
        first=first,
        after=after,
    )


async def get_table(scope: ReadScope, table_id: UUID) -> MetadataTable:
    """One table, decided as `GET /v1/tables/{id}/columns` decides it."""
    _require_roles(scope, CATALOG_READ_ROLES)
    row = await scope.tables.load(table_id)
    if row is None:
        raise ReadRefused("NOT_FOUND")
    await authorize_table(scope, row)
    return row


def _discard(future: asyncio.Future[Any]) -> None:
    """Retrieve a refused object's prefetched result so it is dropped quietly."""
    if not future.cancelled():
        future.exception()


async def _decided[T](scope: ReadScope, table: MetadataTable, pending: Awaitable[T]) -> T:
    """Return a prefetched child load only once `table` is decided readable.

    Why the load is requested first: sibling tables are decided one at a time
    (each decision is statements on the one session), so a load requested
    *after* each decision would reach the loader one tick apart and never batch.
    Requesting it first puts every sibling's key in the same batch; the result
    still leaves this function only after this table's own decision, and a
    refusal discards it unread.
    """
    try:
        await authorize_table(scope, table)
    except BaseException:
        if isinstance(pending, asyncio.Future):
            pending.add_done_callback(_discard)
        raise
    return await pending


async def _undecided_row(scope: ReadScope, table: MetadataTable | UUID) -> MetadataTable:
    """The table a child read is about: loaded (batched with its siblings), not yet
    decided. Every caller decides it through `_decided` before returning anything."""
    if isinstance(table, MetadataTable):
        return table
    row = await scope.tables.load(table)
    if row is None:
        raise ReadRefused("NOT_FOUND")
    return row


async def list_columns(
    scope: ReadScope, table: MetadataTable | UUID, *, first: int, after: str | None
) -> tuple[MetadataTable, Page[MetadataColumnRead]]:
    """`GET /v1/tables/{id}/columns`, with first pages batched across tables.

    Returns the decided table with its page, because a column's own fields (its
    approved description) are decided against that same table.
    """
    _require_roles(scope, CATALOG_READ_ROLES)
    first = _page_size(scope, first)
    row = await _undecided_row(scope, table)
    pending = scope.column_pages.load(_ChildPageKey(row.id, row.organization_id, first, after))
    return row, await _decided(scope, row, pending)


async def list_constraints(
    scope: ReadScope, table: MetadataTable | UUID, *, first: int, after: str | None
) -> Page[MetadataConstraintRead]:
    """`GET /v1/tables/{id}/constraints`, with first pages batched across tables."""
    _require_roles(scope, CATALOG_READ_ROLES)
    first = _page_size(scope, first)
    row = await _undecided_row(scope, table)
    pending = scope.constraint_pages.load(_ChildPageKey(row.id, row.organization_id, first, after))
    return await _decided(scope, row, pending)


async def table_description(scope: ReadScope, table: MetadataTable | UUID) -> TableDescription:
    """`GET /v1/tables/{id}/description`: the current approved readme, or the retired one."""
    _require_roles(scope, CATALOG_READ_ROLES)
    table = await _undecided_row(scope, table)
    current = await _decided(scope, table, scope.approved_documentation.load(table.id))
    retired = None
    if current is None:
        async with scope.lock:
            retired = await latest_withdrawn_table_version(scope.session, table.id)
    return TableDescription(
        table_id=table.id,
        name=table.name,
        source_description=table.source_description,
        readme=current.readme if current else None,
        readme_version=current.version if current else None,
        approved_by=current.approved_by if current else None,
        approved_at=current.approved_at if current else None,
        withdrawn_readme=retired.readme if retired else None,
    )


async def column_business_description(
    scope: ReadScope, table: MetadataTable, column_id: UUID
) -> ColumnDescription | None:
    """The column's current approved description, as the column-documentation route reads it."""
    await authorize_table(scope, table)
    documented = (await scope.column_documentation.load(table.id)).get(column_id)
    if documented is None:
        return None
    return ColumnDescription(
        description=documented.description,
        version=documented.version,
        approved_by=documented.approved_by,
        approved_at=documented.approved_at,
    )


async def referenced_table(
    scope: ReadScope, constraint: MetadataConstraintRead
) -> MetadataTable | None:
    """The table a foreign key points at, decided on its own -- never inherited.

    A reference is an edge into another object, possibly in another datasource
    the caller may not read. It is authorized exactly as `table(id)` would be;
    a dangling reference (no such table) is simply absent.
    """
    if constraint.referenced_table_id is None:
        return None
    try:
        return await get_table(scope, constraint.referenced_table_id)
    except ReadRefused as refused:
        if refused.code == "NOT_FOUND":
            return None
        raise


# --- context products (R11-GQL01) ---------------------------------------------


def _shared_refusal(exc: HTTPException) -> ReadRefused:
    """A refusal from a decision REST shares (`aida.context_product_reads`), with its meaning.

    Its `detail` is never forwarded: some are prose, and the anti-enumeration 404 is the same
    answer for "no such product" and "not yours to see" -- a field refusal must not tell them
    apart either.
    """
    if exc.status_code == 404:
        return ReadRefused("NOT_FOUND")
    if exc.status_code == 403:
        return ReadRefused("FORBIDDEN", "CROSS_ORGANIZATION")
    if exc.status_code == 410:
        return ReadRefused("GONE", "CONTEXT_PRODUCT_VERSION_RETIRED")
    return ReadRefused("CONFLICT", "CONTEXT_PRODUCT_UNAVAILABLE")


async def list_context_products(
    scope: ReadScope,
    project_id: UUID,
    *,
    askable: bool,
    first: int,
    after: str | None,
) -> Page[ContextProductRead]:
    """`GET /v1/projects/{id}/context-products`: the same listing decision
    (`context_product_listing`) -- lifecycle view, or with `askable` only what the caller could
    ask through, and for a contracted agent only what its envelope names -- in the same
    `product_key` order, paged by keyset instead of offset."""
    _require_roles(scope, CONTEXT_PRODUCT_READERS)
    first = _page_size(scope, first)
    async with scope.lock:
        try:
            listing = await context_product_listing(
                scope.session, scope.context, project_id=project_id, askable=askable
            )
        except HTTPException as exc:
            raise _shared_refusal(exc) from exc
        statement = listing.statement.order_by(ContextProduct.product_key)
        total: int | None = None
        if after is not None:
            (last_key,) = _decode(after, [str])
            statement = statement.where(ContextProduct.product_key > last_key)
        else:
            total = int(await scope.session.scalar(listing.count_statement) or 0)
        rows = list((await scope.session.execute(statement.limit(first + 1))).all())
    has_next = len(rows) > first
    rows = rows[:first]
    return Page(
        items=[_product_read(product, version) for product, version in rows],
        total=total,
        end_cursor=encode_cursor(rows[-1][0].product_key) if rows else None,
        has_next_page=has_next,
    )


async def get_context_product_version(
    scope: ReadScope, version_id: UUID
) -> ContextProductVersionRead:
    """`GET /v1/context-product-versions/{id}`: the governed read itself
    (`read_context_product_version`) -- envelope, role, pinned-version eligibility, the
    retirement signal, purpose and quality -- and, for a consumer, the consumption edge,
    audit and outbox event, recorded under channel `GRAPHQL`."""
    _require_roles(scope, CONTEXT_PRODUCT_READERS)
    async with scope.lock:
        try:
            product, version = await read_context_product_version(
                scope.session, scope.context, version_id, channel="GRAPHQL"
            )
        except HTTPException as exc:
            raise _shared_refusal(exc) from exc
    return _version_read(product, version)


# --- unified lineage (R11-GQL01) ------------------------------------------------

#: The bounds the unified-lineage routes declare on their query parameters -- the same
#: numbers, refused the same way (REST answers 422, a field `INVALID_ARGUMENT`).
LINEAGE_DEPTH: tuple[int, int] = (1, 8)
LINEAGE_NODE_LIMIT: tuple[int, int] = (5, 2_000)
LINEAGE_EDGE_LIMIT: tuple[int, int] = (5, 10_000)
LINEAGE_SUGGESTION_STATUSES: tuple[str, ...] = ("ALL", "PENDING", "APPROVED", "REJECTED")
_SuggestionStatus = Literal["ALL", "PENDING", "APPROVED", "REJECTED"]


def _in_range(value: int, bounds: tuple[int, int], reason: str) -> int:
    low, high = bounds
    if not low <= value <= high:
        raise ReadRefused("INVALID_ARGUMENT", reason)
    return value


async def _lineage_datasource(scope: ReadScope, datasource_id: UUID) -> DataSource:
    """What the unified-lineage routes decide before they build anything -- their role
    gate, then `load_datasource_in_scope`: missing is NOT_FOUND, another tenant's is
    FORBIDDEN -- then the datasource's workspace gate, as `DataSource.tables` asks it (and,
    since R11-D28, the routes too). A lineage graph names the datasource's tables, so a
    caller refused the tables must not read their names off the graph instead."""
    _require_roles(scope, UNIFIED_LINEAGE_READER_ROLES)
    row = await scope.datasources.load(datasource_id)
    if row is None:
        raise ReadRefused("NOT_FOUND")
    _enforce_tenant(scope, row.organization_id)
    await _authorize(scope, resource_type="datasource", resource_id=row.id, datasource_id=row.id)
    return row


def _list_page[T](
    rows: Sequence[T],
    *,
    key: Callable[[T], tuple[Any, ...]],
    coercers: Sequence[Callable[[str], Any]],
    first: int,
    after: str | None,
) -> Page[T]:
    """Keyset paging over rows a builder has already bounded and returned whole: the rows
    after the cursor's key, in key order. The total is the whole list's, on the first page
    only, as every other connection gives it."""
    ordered = sorted(rows, key=key)
    if after is not None:
        last = _decode(after, coercers)
        ordered = [row for row in ordered if key(row) > last]
    page = ordered[:first]
    return Page(
        items=page,
        total=len(rows) if after is None else None,
        end_cursor=encode_cursor(*key(page[-1])) if page else None,
        has_next_page=len(ordered) > first,
    )


async def get_lineage_impact(
    scope: ReadScope, datasource_id: UUID, node_id: str, *, depth: int, node_limit: int
) -> UnifiedLineageImpactRead:
    """`GET /v1/datasources/{id}/unified-lineage/impact/{node_id}`: the route's own builder
    (`build_unified_lineage_impact_payload`) with the route's bounds and settings, so the
    traversal, its truncation, the graph-store backend and its fallback, and each table's
    quality state are REST's. An unknown node is NOT_FOUND, as the route's 404."""
    _require_roles(scope, UNIFIED_LINEAGE_READER_ROLES)
    _in_range(depth, LINEAGE_DEPTH, "DEPTH_OUT_OF_RANGE")
    _in_range(node_limit, LINEAGE_NODE_LIMIT, "NODE_LIMIT_OUT_OF_RANGE")
    datasource = await _lineage_datasource(scope, datasource_id)
    async with scope.lock:
        try:
            return await build_unified_lineage_impact_payload(
                scope.session,
                datasource,
                node_id,
                depth=depth,
                node_limit=node_limit,
                settings=scope.settings,
            )
        except LineageNodeNotFoundError as exc:
            raise ReadRefused("NOT_FOUND") from exc


async def list_lineage_impact_nodes(
    scope: ReadScope,
    impact: UnifiedLineageImpactRead,
    *,
    upstream: bool,
    first: int,
    after: str | None,
) -> Page[UnifiedLineageImpactNodeRead]:
    """One direction of an impact read, a page at a time, in the traversal's own
    `(depth, node_id)` order. Decided again before it pages, as every child field is; the
    datasource's decision is memoized for this request, so asking again costs nothing."""
    first = _page_size(scope, first)
    await _lineage_datasource(scope, impact.datasource_id)
    return _list_page(
        impact.upstream if upstream else impact.downstream,
        key=lambda row: (row.depth, row.node_id),
        coercers=(int, str),
        first=first,
        after=after,
    )


async def get_lineage_graph(
    scope: ReadScope,
    datasource_id: UUID,
    *,
    node_limit: int,
    edge_limit: int,
    suggestion_status: str,
    include_pending_edges: bool,
) -> UnifiedLineageGraphRead:
    """`GET /v1/datasources/{id}/unified-lineage/graph`: the route's own builder
    (`build_unified_lineage_graph_payload`) with its bounds, its review filter and its
    opt-in for proposed parsed edges, so the merge and its truncation are REST's."""
    _require_roles(scope, UNIFIED_LINEAGE_READER_ROLES)
    _in_range(node_limit, LINEAGE_NODE_LIMIT, "NODE_LIMIT_OUT_OF_RANGE")
    _in_range(edge_limit, LINEAGE_EDGE_LIMIT, "EDGE_LIMIT_OUT_OF_RANGE")
    if suggestion_status not in LINEAGE_SUGGESTION_STATUSES:
        raise ReadRefused("INVALID_ARGUMENT", "SUGGESTION_STATUS_UNKNOWN")
    datasource = await _lineage_datasource(scope, datasource_id)
    async with scope.lock:
        return await build_unified_lineage_graph_payload(
            scope.session,
            datasource,
            node_limit=node_limit,
            edge_limit=edge_limit,
            suggestion_status=cast(_SuggestionStatus, suggestion_status),
            settings=scope.settings,
            include_pending_edges=include_pending_edges,
        )


async def list_lineage_graph_nodes(
    scope: ReadScope, graph: UnifiedLineageGraphRead, *, first: int, after: str | None
) -> Page[UnifiedLineageNodeRead]:
    """A graph's nodes a page at a time, in REST's `qualified_name` order (the id breaks a
    tie). Decided again before it pages, as every child field is."""
    first = _page_size(scope, first)
    await _lineage_datasource(scope, graph.datasource_id)
    return _list_page(
        graph.nodes,
        key=lambda node: (node.qualified_name, node.id),
        coercers=(str, str),
        first=first,
        after=after,
    )


async def list_lineage_graph_edges(
    scope: ReadScope, graph: UnifiedLineageGraphRead, *, first: int, after: str | None
) -> Page[UnifiedLineageEdgeRead]:
    """A graph's edges a page at a time, in edge id order (REST returns them in merge order,
    which no cursor can resume). Decided again before it pages, as every child field is."""
    first = _page_size(scope, first)
    await _lineage_datasource(scope, graph.datasource_id)
    return _list_page(
        graph.edges,
        key=lambda edge: (edge.id, edge.source_node_id, edge.target_node_id),
        coercers=(str, str, str),
        first=first,
        after=after,
    )


# --- coverage (R11-GQL01) ------------------------------------------------------


async def _resolved_coverage(
    session: AsyncSession, version: ContextProductVersion
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What the compile route resolves after its decision, minus the two sections that are not
    coverage (negative knowledge and exemplars): the same `resolve_pinned_references` and
    `load_coverage_extras` the route calls, so a stale pin is the route's 409 (CONFLICT here),
    rendered by the same `coverage_section` and `freshness_section`."""
    try:
        pinned = await resolve_pinned_references(session, version)
    except UnresolvedReferencesError as unresolved:
        raise ReadRefused("CONFLICT", "CONTEXT_PRODUCT_REFERENCES_UNRESOLVED") from unresolved
    meaning, changes = await load_coverage_extras(session, version)
    return (
        coverage_section(pinned.routines, pinned.views, meaning, changes),
        freshness_section(pinned.sources),
    )


async def get_context_product_coverage(scope: ReadScope, version_id: UUID) -> ProductCoverage:
    """A context product version's coverage -- the routines and views it stands on, the meaning
    it pins, what moved since it was published, and when each source behind it was last read --
    decided as `GET /v1/context-product-versions/{id}/compile` decides it.

    Recorded as that route records a compilation, less the artifact: an audit event on every
    read, and for a PUBLISHED version a consumption edge, on channel `GRAPHQL_COVERAGE`. No
    outbox event -- the route's `context.product_compiled.v1` names an artifact hash, and
    nothing was compiled. Decided and recorded once per request, however many aliases ask.
    """
    _require_roles(scope, CONTEXT_COMPILER_ROLES)
    # Checked and filled under the lock: sibling aliases resolve concurrently, and the second
    # must wait for the first's answer rather than decide and record a second time.
    async with scope.lock:
        remembered = scope.coverage.get(version_id)
        if remembered is None:
            try:
                remembered = await _product_coverage(scope, version_id)
            except ReadRefused as refused:
                remembered = refused
            scope.coverage[version_id] = remembered
    if isinstance(remembered, ReadRefused):
        raise ReadRefused(remembered.code, remembered.reason)
    return remembered


async def _product_coverage(scope: ReadScope, version_id: UUID) -> ProductCoverage:
    """Decide, resolve and record one version's coverage. The caller holds `scope.lock`."""
    session = scope.session
    try:
        decision = await decide_compiled_read(session, scope.context, version_id)
    except HTTPException as exc:
        raise _shared_refusal(exc) from exc
    product, version, quality = decision.product, decision.version, decision.quality
    section, freshness = await _resolved_coverage(session, version)
    correlation_id = get_correlation_id()
    record_audit(
        session,
        replace(scope.context, organization_id=version.organization_id),
        action="context_product.coverage_read",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"channel": COVERAGE_CONSUMPTION_CHANNEL, "fingerprint": version.fingerprint},
    )
    if version.status == "PUBLISHED":
        session.add(
            ContextProductConsumptionEdge(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                principal_id=scope.context.principal_id,
                principal_type=scope.context.principal_type,
                channel=COVERAGE_CONSUMPTION_CHANNEL,
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality.snapshot(),
            )
        )
    await session.commit()
    return ProductCoverage(
        version_id=version.id,
        product_key=product.product_key,
        version=version.version,
        status=version.status,
        routines=list(section["routines"]),
        views=list(section["views"]),
        meaning=list(section.get("meaning", [])),
        changed_since_published=list(section.get("changed_since_published", [])),
        source_freshness=freshness,
    )


#: Each coverage section's own order -- the one `coverage_section` and `freshness_section` sort
#: it in -- which is also its cursor key.
_COVERAGE_KEYS: dict[str, tuple[str, ...]] = {
    "routines": ("id",),
    "views": ("table_id",),
    "meaning": ("kind", "version_id"),
    "changed_since_published": ("subject_kind", "subject_id", "change"),
    "source_freshness": ("datasource_id",),
}
CoverageSection = Literal[
    "routines", "views", "meaning", "changed_since_published", "source_freshness"
]


async def list_coverage_items(
    scope: ReadScope,
    coverage: ProductCoverage,
    section: CoverageSection,
    *,
    first: int,
    after: str | None,
) -> Page[dict[str, Any]]:
    """One coverage section a page at a time, in REST's order. Decided again first, as every
    child page is -- the answer is this request's own, so asking again costs nothing."""
    first = _page_size(scope, first)
    await get_context_product_coverage(scope, coverage.version_id)
    columns = _COVERAGE_KEYS[section]
    entries: list[dict[str, Any]] = getattr(coverage, section)
    return _list_page(
        entries,
        key=lambda entry: tuple(str(entry[column]) for column in columns),
        coercers=tuple(str for _ in columns),
        first=first,
        after=after,
    )


async def _parse_coverage_datasource(scope: ReadScope, datasource_id: UUID) -> DataSource:
    """What the parse-coverage routes decide before they look a routine or trigger up
    (`procedure_lineage_api._load_readable_datasource`, R11-D30), in their order: the role gate
    (the procedure-lineage reader roles, which are the unified-lineage reader roles member for
    member), the datasource in the caller's tenant, then its workspace gate (`READ_METADATA`,
    as its tables route asks) -- so a refusal says nothing about which routines exist."""
    _require_roles(scope, UNIFIED_LINEAGE_READER_ROLES)
    row = await scope.datasources.load(datasource_id)
    if row is None:
        raise ReadRefused("NOT_FOUND")
    _enforce_tenant(scope, row.organization_id)
    await _authorize(scope, resource_type="datasource", resource_id=row.id, datasource_id=row.id)
    return row


def _reason_codes(stored: str) -> list[str]:
    return stored.split(",") if stored else []


async def get_routine_parse_coverage(
    scope: ReadScope, datasource_id: UUID, routine_id: UUID
) -> RoutineParseCoverageRead:
    """`GET /v1/datasources/{id}/procedures/{routine_id}/parse-coverage`: a routine of another
    datasource or tenant is NOT_FOUND, and one no parse has measured is NOT_FOUND with reason
    `COVERAGE_NOT_MEASURED` -- "not measured" is not "fully understood", as the route says."""
    datasource = await _parse_coverage_datasource(scope, datasource_id)
    async with scope.lock:
        routine = await scope.session.get(MetadataRoutine, routine_id)
        if (
            routine is None
            or routine.datasource_id != datasource.id
            or routine.organization_id != datasource.organization_id
        ):
            raise ReadRefused("NOT_FOUND")
        coverage = (
            await scope.session.scalars(
                select(RoutineParseCoverage).where(
                    RoutineParseCoverage.organization_id == datasource.organization_id,
                    RoutineParseCoverage.datasource_id == datasource.id,
                    RoutineParseCoverage.routine_id == routine_id,
                )
            )
        ).first()
    if coverage is None:
        raise ReadRefused("NOT_FOUND", "COVERAGE_NOT_MEASURED")
    return RoutineParseCoverageRead(
        routine_id=coverage.routine_id,
        state=parse_coverage_state(
            parse_completed=coverage.parse_completed,
            statement_count=coverage.statement_count,
        ).value,
        parse_completed=coverage.parse_completed,
        is_read_only=coverage.is_read_only,
        statement_count=coverage.statement_count,
        unparsed_statement_count=coverage.unparsed_statement_count,
        unparsed_reason_codes=_reason_codes(coverage.unparsed_reason_codes),
        dialect=coverage.dialect,
        confidence=coverage.confidence,
        source_mapping_granularity=coverage.source_mapping_granularity,
        parsed_at=coverage.parsed_at,
        member_attribution=coverage.member_attribution,
        member_fallback_reason=coverage.member_fallback_reason,
    )


async def get_trigger_parse_coverage(
    scope: ReadScope, datasource_id: UUID, trigger_id: UUID
) -> TriggerParseCoverageRead:
    """`GET /v1/datasources/{id}/triggers/{trigger_id}/parse-coverage`, with the routine read's
    contract: another datasource's or tenant's trigger is NOT_FOUND, and an unmeasured one is
    NOT_FOUND with reason `COVERAGE_NOT_MEASURED`."""
    datasource = await _parse_coverage_datasource(scope, datasource_id)
    async with scope.lock:
        trigger = await scope.session.get(MetadataTrigger, trigger_id)
        if (
            trigger is None
            or trigger.datasource_id != datasource.id
            or trigger.organization_id != datasource.organization_id
        ):
            raise ReadRefused("NOT_FOUND")
        coverage = (
            await scope.session.scalars(
                select(TriggerParseCoverage).where(
                    TriggerParseCoverage.organization_id == datasource.organization_id,
                    TriggerParseCoverage.datasource_id == datasource.id,
                    TriggerParseCoverage.trigger_id == trigger_id,
                )
            )
        ).first()
    if coverage is None:
        raise ReadRefused("NOT_FOUND", "COVERAGE_NOT_MEASURED")
    return TriggerParseCoverageRead(
        trigger_id=coverage.trigger_id,
        routine_id=coverage.routine_id,
        state=parse_coverage_state(
            parse_completed=coverage.parse_completed,
            statement_count=coverage.statement_count,
        ).value,
        parse_completed=coverage.parse_completed,
        is_read_only=coverage.is_read_only,
        statement_count=coverage.statement_count,
        unparsed_statement_count=coverage.unparsed_statement_count,
        unparsed_reason_codes=_reason_codes(coverage.unparsed_reason_codes),
        dialect=coverage.dialect,
        confidence=coverage.confidence,
        source_mapping_granularity=coverage.source_mapping_granularity,
        parsed_at=coverage.parsed_at,
    )
