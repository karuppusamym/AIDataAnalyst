"""F01: the context-product boundary is enforced where every execution path passes.

A first guard landed with R11-FP12: the orchestrator refused a generated statement that
read past the product it was asked through, and re-checked it after execution. The F01
review recorded that "the gateway itself remains datasource-scoped", and reconnaissance
found six residual gaps this file pins one at a time:

* **G1** -- MCP `tools/call` resolved a product, filtered tool eligibility with it and then
  built the orchestrator without it, so neither enforcement point ran on that surface.
* **G2** -- `POST /v1/datasources/{id}/query-executions` had no product concept at all, so
  the principal who was product-scoped through Ask could submit the same SQL unscoped.
* **G3** -- the GOVERNED_TOOL strategy was exempt outright, and nothing at any lifecycle
  point ever compared an eligible tool version's tables with the product's.
* **G4** -- an unresolved reference passed the boundary, because the test was
  `any(resolved_id not in scope)` over a resolution that silently dropped what it could
  not resolve.
* **G5** -- a leaf-name collision across schemas refused a statement over a table it never
  read, and a wrong schema qualifier resolved to the right leaf name and passed.
* **G6** -- a CTE declared inside a subquery shadowed an identically-named physical table
  elsewhere in the statement, dropping it from `referenced_tables` and therefore out of
  the reach of the catalog allowlist, the ABAC axes and this boundary at once.
  (The guard-level proof for G6 lives in `tests/test_sql_guard.py`; the end-to-end
  consequence is pinned here.)

Every test drives a real surface -- the gateway's own `validate`/`execute`, the
orchestrator's `run`, the MCP handler, the HTTP route -- rather than the resolver behind
them, because in each of these six cases the defect *was* that the boundary never asked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_orchestrator import (
    CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE,
    CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE,
    AgentPolicyRejected,
    GovernedAgentOrchestrator,
)
from aida.config import Settings, get_settings
from aida.context_product_execution_scope import (
    ContextProductExecutionScope,
    load_execution_scope,
    resolve_scope_names,
)
from aida.db import Base, get_session

# Imported at module scope so every router is registered before `create_all`.
from aida.main import app
from aida.mcp_server import _handle_tools_call
from aida.model_gateway import ApprovedModelRoute, ModelCallEvidence, SqlGenerationOutput
from aida.models import (
    AgentRun,
    AnalysisRun,
    AuditEvent,
    ContextProduct,
    ContextProductVersion,
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QueryExecution,
    QueryMemoryEvidence,
)
from aida.query_gateway import QueryExecutionGateway, QueryRejected
from aida.security import SecurityContext
from aida.sql_validation import (
    FINDING_CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE,
    FINDING_CONTEXT_PRODUCT_TABLE_UNRESOLVED,
    FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE,
)
from tests.support.doubles import FakeSqlExecutor, security_context

pytestmark = pytest.mark.asyncio

PRODUCT_KEY = "orders-context"
QUESTION = "orders"


# ---------------------------------------------------------------------------
# Scenario: one datasource whose catalog contains a deliberate leaf-name
# collision (`retail.orders` and `staging.orders`), which is what G5 needs and
# what no existing fixture has.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        yield active
    await engine.dispose()


# `AuditEvent.id` is a BigInteger autoincrement PK that relies on Postgres's own
# sequence in production; sqlite only auto-populates a bare INTEGER PRIMARY KEY.
# Same workaround as `tests/test_agent_orchestrator_retrieval_wiring.py`.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(_mapper: object, _connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here resolves a real secret; the connector itself is doubled per test."""
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )


class _Scenario:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()
        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
        )
        db.add(self.lob)
        await db.flush()
        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Commerce",
            code=f"COM{uuid4().hex[:6]}",
        )
        db.add(self.domain)
        await db.flush()
        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Commerce",
            slug=f"core-commerce-{uuid4().hex[:6]}",
        )
        db.add(self.project)
        await db.flush()
        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="postgres",
            dialect="postgres",
            environment="TEST",
            credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
            status="ACTIVE",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="warehouse",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()
        self.retail = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="retail",
            fingerprint="fp-retail",
        )
        self.staging = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="staging",
            fingerprint="fp-staging",
        )
        db.add_all((self.retail, self.staging))
        await db.flush()

        self.orders = await self._table(self.retail, "orders", "Order facts for retail")
        self.customer = await self._table(self.retail, "customer", "Retail customer dimension")
        self.ledger = await self._table(self.retail, "secret_ledger", "Restricted ledger")
        # The collision G5 needs: a second `orders`, in another schema, that no
        # product here names and that nothing in these statements reads.
        self.staging_orders = await self._table(self.staging, "orders", "Raw order landing")

        db.add(
            AnalysisRun(
                organization_id=self.organization.id,
                datasource_id=self.datasource.id,
                status="COMPLETED",
            )
        )
        await db.flush()
        self.analysis_run = await db.scalar(
            select(AnalysisRun).where(AnalysisRun.datasource_id == self.datasource.id)
        )
        assert self.analysis_run is not None
        self.semantic_version = f"technical-metadata:{self.analysis_run.id}"

        self.tool = GovernedTool(
            organization_id=self.organization.id,
            project_id=self.project.id,
            slug="order_lookup",
        )
        db.add(self.tool)
        await db.flush()
        await db.commit()
        return self

    async def _table(
        self, schema: MetadataSchema, name: str, description: str
    ) -> MetadataTable:
        table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name=name,
            object_type="TABLE",
            status="ACTIVE",
            fingerprint=f"fp-{schema.name}-{name}",
            source_description=description,
        )
        self.db.add(table)
        await self.db.flush()
        return table

    async def tool_version(
        self, *, referenced_tables: Sequence[str], sql_template: str = "SELECT 1"
    ) -> GovernedToolVersion:
        version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=self.tool.id,
            version=1,
            status="PUBLISHED",
            name="Order Lookup",
            description="Look up orders",
            datasource_id=self.datasource.id,
            sql_template=sql_template,
            referenced_tables=list(referenced_tables),
            parameter_schema=[],
            allowed_roles=["Analyst"],
            fingerprint=f"fp-tool-{uuid4().hex[:8]}",
            created_by="tool-dev",
        )
        self.db.add(version)
        await self.db.flush()
        return version

    async def product(
        self,
        *,
        table_ids: Iterable[UUID],
        key: str = PRODUCT_KEY,
        tool_version_ids: Iterable[UUID] = (),
        consumer_roles: Sequence[str] = ("Analyst",),
        status: str = "PUBLISHED",
        version_number: int = 1,
    ) -> ContextProductVersion:
        product = await self.db.scalar(
            select(ContextProduct).where(ContextProduct.product_key == key)
        )
        if product is None:
            product = ContextProduct(
                organization_id=self.organization.id,
                project_id=self.project.id,
                product_key=key,
                created_by="steward-1",
            )
            self.db.add(product)
            await self.db.flush()
        version = ContextProductVersion(
            organization_id=self.organization.id,
            product_id=product.id,
            version=version_number,
            status=status,
            name="Order context",
            description="What an agent needs to answer order questions.",
            purpose="Answer order questions.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            table_ids=[str(table_id) for table_id in table_ids],
            eligible_tool_version_ids=[str(value) for value in tool_version_ids],
            allowed_consumer_roles=list(consumer_roles),
            fingerprint=f"fp-{uuid4().hex[:8]}",
            created_by="steward-1",
        )
        self.db.add(version)
        await self.db.flush()
        return version

    def analyst(self) -> SecurityContext:
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"Analyst"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


def _scope(version: ContextProductVersion) -> ContextProductExecutionScope:
    return ContextProductExecutionScope(
        version_id=version.id,
        version=version.version,
        table_ids=frozenset(version.table_ids),
    )


# ---------------------------------------------------------------------------
# G4 / G5: the resolver the boundary is decided on
# ---------------------------------------------------------------------------


async def test_a_leaf_name_collision_across_schemas_does_not_refuse_the_named_table(
    scenario: _Scenario,
) -> None:
    """G5: the statement reads `retail.orders`; `staging.orders` is not its business.

    The permissive ABAC resolver returned *both* ids for the leaf name `orders`, so a
    product naming `retail.orders` refused this statement -- and the post-execution
    checkpoint named `staging.orders`'s uuid as the offending table.
    """
    resolution = await resolve_scope_names(
        scenario.db,
        scenario.datasource,
        ["retail.orders"],
        table_ids=[str(scenario.orders.id)],
    )

    assert resolution.admitted
    assert resolution.in_scope == ("retail.orders",)


async def test_a_bare_name_that_several_schemas_answer_to_is_refused(
    scenario: _Scenario,
) -> None:
    """Ambiguity is refused, not resolved by luck.

    `orders` exists in two schemas here, so the platform cannot say which table this
    reads -- and a boundary that cannot name the table it admitted has not made a
    decision. Same rule `allowed_tables` and `quality_coupling.resolve_table_ids` apply
    to a bare name, applied to the membership test too.
    """
    resolution = await resolve_scope_names(
        scenario.db,
        scenario.datasource,
        ["orders"],
        table_ids=[str(scenario.orders.id)],
    )

    assert not resolution.admitted
    assert resolution.unresolved == ("orders",)


async def test_an_unambiguous_bare_name_resolves(scenario: _Scenario) -> None:
    resolution = await resolve_scope_names(
        scenario.db,
        scenario.datasource,
        ["customer"],
        table_ids=[str(scenario.customer.id)],
    )

    assert resolution.admitted


async def test_a_wrong_schema_qualifier_no_longer_resolves_to_the_right_leaf_name(
    scenario: _Scenario,
) -> None:
    """G5, the other direction: `nonexistent_schema.orders` used to pass the boundary.

    The old resolver discarded everything before the last `.`, so a qualifier naming a
    schema that does not exist resolved to whatever `orders` it could find -- including
    the in-scope one.
    """
    resolution = await resolve_scope_names(
        scenario.db,
        scenario.datasource,
        ["nonexistent_schema.orders"],
        table_ids=[str(scenario.orders.id)],
    )

    assert not resolution.admitted
    assert resolution.unresolved == ("nonexistent_schema.orders",)


async def test_a_partially_unresolvable_statement_is_refused(scenario: _Scenario) -> None:
    """G4: one in-scope table plus one name that resolves to nothing.

    The old test -- `any(resolved_id not in scope)` -- was false over a resolution that
    had silently dropped the unknown name, so this shape passed the product boundary and
    was refused later, if at all, as a datasource-wide `UNKNOWN_OR_UNAUTHORIZED_TABLE`.
    """
    resolution = await resolve_scope_names(
        scenario.db,
        scenario.datasource,
        ["retail.orders", "retail.no_such_table"],
        table_ids=[str(scenario.orders.id)],
    )

    assert not resolution.admitted
    assert resolution.in_scope == ("retail.orders",)
    assert resolution.unresolved == ("retail.no_such_table",)


async def test_a_table_less_statement_is_admitted(scenario: _Scenario) -> None:
    """`SELECT 1` reads nothing, so there is nothing for a product to narrow."""
    resolution = await resolve_scope_names(
        scenario.db, scenario.datasource, [], table_ids=[str(scenario.orders.id)]
    )

    assert resolution.admitted


async def test_a_product_naming_no_table_admits_no_table_read(scenario: _Scenario) -> None:
    """Decided, not inherited: an empty `table_ids` refuses every statement that reads.

    The table axis of a product is an allowlist, not a pinned set -- `ContextProductScope
    .admits` treats a product naming no table as admitting no table candidate, and the
    boundary agrees. A product that names nothing grounds no data read.
    """
    resolution = await resolve_scope_names(
        scenario.db, scenario.datasource, ["retail.orders"], table_ids=[]
    )

    assert not resolution.admitted
    assert resolution.out_of_scope == ("retail.orders",)


# ---------------------------------------------------------------------------
# The architectural change: the boundary lives in the gateway
# ---------------------------------------------------------------------------


def _gateway(monkeypatch: pytest.MonkeyPatch) -> tuple[QueryExecutionGateway, list[str]]:
    """A gateway whose connector records that it was opened at all.

    `open_execution_session` is the choke point INV-2 protects, and F01's acceptance
    criterion is that an out-of-scope statement is refused *before* it opens. So the
    double records each open rather than just answering: a refusal that happened after
    one would still return the right error and be the wrong behaviour.
    """
    opened: list[str] = []

    def _open(connector_type: str, dsn: str) -> Any:
        opened.append(connector_type)
        return FakeSqlExecutor(({"order_id": "O-1"},))

    monkeypatch.setattr("aida.query_gateway.open_execution_session", _open)
    return QueryExecutionGateway(Settings(_env_file=None)), opened


async def _execute(
    scenario: _Scenario,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    *,
    scope: ContextProductExecutionScope | None,
) -> tuple[Exception | None, list[str]]:
    gateway, opened = _gateway(monkeypatch)
    try:
        await gateway.execute(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.analyst(),
            correlation_id=f"corr-{uuid4().hex[:8]}",
            sql=sql,
            requested_limit=None,
            semantic_version=scenario.semantic_version,
            context_product_scope=scope,
        )
    except Exception as exc:  # noqa: BLE001 -- the refusal is the subject
        return exc, opened
    return None, opened


async def test_the_gateway_refuses_an_out_of_scope_read_before_opening_a_session(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion, at the choke point every execution path shares.

    Tables A and B are both otherwise authorized -- both are ACTIVE in this datasource's
    catalog binding, so `allowed_tables` admits both. A product containing only A refuses
    a statement reading B, and no execution session is opened (INV-2).
    """
    product = await scenario.product(table_ids=[scenario.orders.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT l.amount FROM retail.secret_ledger AS l",
        scope=_scope(product),
    )

    assert isinstance(failure, QueryRejected)
    assert FINDING_CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE in str(failure)
    assert opened == [], "the boundary must refuse before a connector is opened"


async def test_the_gateway_is_unchanged_for_a_request_with_no_product(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Product-free requests retain their existing behaviour: the same read runs."""
    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT l.amount FROM retail.secret_ledger AS l",
        scope=None,
    )

    assert failure is None
    assert opened == ["postgres"]


async def test_validate_tells_an_agent_what_execute_will_refuse(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pipeline, two entry points (review item N14) -- including this boundary.

    A rule that only exists on the execute path is a rule an agent cannot iterate
    against, so the product boundary is a finding rather than a bespoke raise.
    """
    product = await scenario.product(table_ids=[scenario.orders.id])
    gateway, opened = _gateway(monkeypatch)

    report = await gateway.validate(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-validate",
        sql="SELECT l.amount FROM retail.secret_ledger AS l",
        requested_limit=None,
        context_product_scope=_scope(product),
    )

    assert not report.valid
    assert FINDING_CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE in report.codes()
    finding = next(
        item
        for item in report.findings
        if item.code == FINDING_CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE
    )
    # F01 asks for the version receipt to survive the decision, not just the decision.
    assert finding.ref == "retail.secret_ledger"
    assert finding.detail["context_product_version_id"] == str(product.id)
    assert finding.detail["context_product_version"] == 1
    assert opened == []


async def test_a_join_onto_a_table_outside_the_product_is_refused(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = await scenario.product(table_ids=[scenario.orders.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT o.order_id FROM retail.orders AS o "
        "JOIN retail.secret_ledger AS l ON l.order_id = o.order_id",
        scope=_scope(product),
    )

    assert isinstance(failure, QueryRejected)
    assert "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE:retail.secret_ledger" in str(failure)
    assert opened == []


async def test_a_join_within_the_product_is_admitted(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides named, so the boundary is not what stops it -- it runs."""
    product = await scenario.product(table_ids=[scenario.orders.id, scenario.customer.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT o.order_id FROM retail.orders AS o "
        "JOIN retail.customer AS c ON c.customer_id = o.customer_id",
        scope=_scope(product),
    )

    assert failure is None
    assert opened == ["postgres"]


async def test_a_cte_over_a_table_outside_the_product_is_refused(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CTE is a name, not a licence: the boundary follows it to its source."""
    product = await scenario.product(table_ids=[scenario.orders.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "WITH hidden AS (SELECT l.amount FROM retail.secret_ledger AS l) "
        "SELECT h.amount FROM hidden AS h",
        scope=_scope(product),
    )

    assert isinstance(failure, QueryRejected)
    assert "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE:retail.secret_ledger" in str(failure)
    assert opened == []


async def test_a_nested_cte_cannot_hide_a_table_from_the_boundary(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G6, end to end: a shadowing CTE used to delete the table from the statement.

    A CTE named `secret_ledger` inside the subquery's own `WITH` shadowed the unqualified
    `secret_ledger` the outer query reads, so the guard dropped the physical table from
    `referenced_tables` -- and every control downstream reads only that list. The table
    escaped the datasource allowlist, the ABAC axes and this boundary simultaneously.
    """
    product = await scenario.product(table_ids=[scenario.orders.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT l.amount FROM secret_ledger AS l "
        "JOIN (WITH secret_ledger AS (SELECT 1 AS k) SELECT k FROM secret_ledger) AS s "
        "ON s.k = l.order_id",
        scope=_scope(product),
    )

    assert isinstance(failure, QueryRejected)
    assert "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE:secret_ledger" in str(failure)
    assert opened == []


async def test_a_qualified_reference_to_a_cte_name_is_treated_as_a_physical_table(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`retail.hidden` is the schema's table, not the CTE -- and it does not exist.

    So the boundary refuses it as unresolved rather than silently reading it as the CTE,
    and the catalog's own finding is still present to say the object is unknown.
    """
    product = await scenario.product(table_ids=[scenario.orders.id])
    gateway, opened = _gateway(monkeypatch)

    report = await gateway.validate(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-cte-qualified",
        sql=(
            "WITH hidden AS (SELECT o.order_id FROM retail.orders AS o) "
            "SELECT h.order_id FROM retail.hidden AS h"
        ),
        requested_limit=None,
        context_product_scope=_scope(product),
    )

    assert FINDING_CONTEXT_PRODUCT_TABLE_UNRESOLVED in report.codes()
    # Nothing is hidden by attributing the message to the product boundary: the
    # datasource-level finding for the same name is still in the report.
    assert FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE in report.codes()
    assert report.rejection_reason() == "CONTEXT_PRODUCT_TABLE_UNRESOLVED:retail.hidden"
    assert opened == []


async def test_a_union_with_one_out_of_scope_branch_is_refused(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = await scenario.product(table_ids=[scenario.orders.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT o.order_id FROM retail.orders AS o "
        "UNION SELECT l.order_id FROM retail.secret_ledger AS l",
        scope=_scope(product),
    )

    assert isinstance(failure, QueryRejected)
    assert "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE:retail.secret_ledger" in str(failure)
    assert opened == []


async def test_a_derived_table_outside_the_product_is_refused(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = await scenario.product(table_ids=[scenario.orders.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT s.amount FROM (SELECT l.amount FROM retail.secret_ledger AS l) AS s",
        scope=_scope(product),
    )

    assert isinstance(failure, QueryRejected)
    assert "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE:retail.secret_ledger" in str(failure)
    assert opened == []


async def test_an_unqualified_reference_inside_the_product_is_admitted(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unambiguous bare name resolves, so the boundary does not refuse it."""
    product = await scenario.product(table_ids=[scenario.customer.id])

    failure, opened = await _execute(
        scenario,
        monkeypatch,
        "SELECT c.customer_id FROM customer AS c",
        scope=_scope(product),
    )

    assert failure is None
    assert opened == ["postgres"]


# ---------------------------------------------------------------------------
# G2: the direct-SQL route
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http(scenario: _Scenario) -> AsyncIterator[httpx.AsyncClient]:
    """The application, talking to the scenario's session.

    `app` is process-wide, so the overrides are restored as they were found rather than
    cleared -- clearing would remove whatever another test had installed.
    """
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield scenario.db

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://f01.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(scenario: _Scenario) -> dict[str, str]:
    return {
        "X-Principal-Id": "direct-analyst",
        "X-Principal-Type": "USER",
        "X-Roles": "Analyst",
        "X-Business-Purpose": "Submit SQL through a context product",
        "X-Organization-Id": str(scenario.organization.id),
    }


async def _post_sql(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    *,
    sql: str,
    product_key: str | None,
) -> httpx.Response:
    body: dict[str, Any] = {"sql": sql}
    if product_key is not None:
        body["context_product_key"] = product_key
    return await http.post(
        f"/v1/datasources/{scenario.datasource.id}/query-executions",
        json=body,
        headers=_headers(scenario),
    )


async def test_the_direct_sql_route_holds_submitted_sql_to_the_product(
    http: httpx.AsyncClient, scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G2: "direct SQL" is named in F01's acceptance criterion, and had no product concept.

    The same principal, submitting the same statement, was product-scoped through Ask and
    unscoped here -- which made the boundary a property of the surface rather than of the
    request.
    """
    await scenario.product(table_ids=[scenario.orders.id])
    _gateway(monkeypatch)  # a connector double, so an admitted statement would run

    response = await _post_sql(
        http,
        scenario,
        sql="SELECT l.amount FROM retail.secret_ledger AS l",
        product_key=PRODUCT_KEY,
    )

    assert response.status_code == 422
    assert "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE" in response.json()["detail"]


async def test_the_direct_sql_route_is_unchanged_without_a_product_key(
    http: httpx.AsyncClient, scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new field is optional and omitting it changes nothing: the read succeeds."""
    await scenario.product(table_ids=[scenario.orders.id])
    _gateway(monkeypatch)

    response = await _post_sql(
        http,
        scenario,
        sql="SELECT l.amount FROM retail.secret_ledger AS l",
        product_key=None,
    )

    assert response.status_code == 200


async def test_the_direct_sql_route_refuses_a_product_this_caller_may_not_ask_through(
    http: httpx.AsyncClient, scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404, the same answer an unknown key gets -- a product's existence is not disclosed
    to a principal outside its consumer roles."""
    await scenario.product(table_ids=[scenario.orders.id], consumer_roles=["DataSteward"])
    _gateway(monkeypatch)

    response = await _post_sql(
        http,
        scenario,
        sql="SELECT o.order_id FROM retail.orders AS o",
        product_key=PRODUCT_KEY,
    )

    assert response.status_code == 404
    unknown = await _post_sql(
        http, scenario, sql="SELECT o.order_id FROM retail.orders AS o", product_key="no-such"
    )
    assert unknown.status_code == 404
    assert response.json()["detail"] == unknown.json()["detail"]


async def test_the_route_scope_loader_agrees_with_asks_own_lookup(
    scenario: _Scenario,
) -> None:
    """A draft version is not something either surface may be asked through."""
    await scenario.product(table_ids=[scenario.orders.id], status="DRAFT")

    scope = await load_execution_scope(
        scenario.db,
        organization_id=scenario.organization.id,
        product_key=PRODUCT_KEY,
        roles=frozenset({"Analyst"}),
    )

    assert scope is None


# ---------------------------------------------------------------------------
# The orchestrator: every generation strategy, and G3
# ---------------------------------------------------------------------------


class _FakeModelGateway:
    """Returns caller-controlled SQL in place of a model route."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.calls: list[dict[str, Any]] = []

    async def structured_completion(
        self,
        *,
        session: AsyncSession,
        organization_id: UUID,
        route: Any,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: type[SqlGenerationOutput],
        datasource_id: UUID | None = None,
    ) -> tuple[SqlGenerationOutput, ModelCallEvidence]:
        self.calls.append({"payload": payload})
        output = output_schema(sql=self.sql, confidence=0.9, rationale_codes=["FAKE"])
        evidence = ModelCallEvidence(
            route="fake-route",
            provider_type="fake",
            model_id="fake-model",
            endpoint_alias="fake-alias",
            input_fingerprint="in-fp",
            output_fingerprint="out-fp",
            input_size_bytes=1,
            output_size_bytes=1,
            schema_name=output_schema.__name__,
        )
        return output, evidence


async def _one_route() -> list[ApprovedModelRoute]:
    return [
        ApprovedModelRoute(
            route_key="test-route",
            provider_type="OPENAI",
            model_id="approved-model",
            endpoint_alias="private-model-endpoint",
            credential_reference="vault://model-key",
            max_input_tokens=8000,
            max_output_tokens=2000,
            timeout_seconds=30,
        )
    ]


def _orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_sql: str | None = None,
    memory: bool = False,
    development_sql: bool = False,
) -> tuple[GovernedAgentOrchestrator, _FakeModelGateway | None]:
    orchestrator = GovernedAgentOrchestrator(
        Settings(
            _env_file=None,
            agent_retrieval_limit=10,
            agent_query_memory_enabled=memory,
            agent_query_memory_min_similarity=0.5,
            allow_development_sql_override=development_sql,
        )
    )
    model: _FakeModelGateway | None = None
    if model_sql is not None:
        model = _FakeModelGateway(model_sql)
        orchestrator.model_gateway = model  # type: ignore[assignment]
        orchestrator._approved_model_routes = (  # type: ignore[method-assign]
            lambda session, organization_id: _one_route()
        )
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: FakeSqlExecutor(({"order_id": "O-1"},)),
    )
    return orchestrator, model


async def _ask(
    orchestrator: GovernedAgentOrchestrator,
    scenario: _Scenario,
    *,
    product_key: str | None = PRODUCT_KEY,
    candidate_sql: str | None = None,
    preferred_tool_version_id: UUID | None = None,
) -> Any:
    return await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id=f"corr-{uuid4().hex[:8]}",
        question=QUESTION,
        candidate_sql=candidate_sql,
        preferred_tool_version_id=preferred_tool_version_id,
        tool_parameters={},
        requested_limit=None,
        context_product_key=product_key,
    )


async def _latest_run(scenario: _Scenario) -> AgentRun:
    run = await scenario.db.scalar(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(1))
    assert run is not None
    return run


async def test_model_generated_sql_is_held_to_the_product(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MODEL_GATEWAY strategy: scoping retrieval decided what the model *saw*.

    The only generated-SQL coverage before F01 went through `candidate_sql`
    (DEVELOPMENT_SQL). What a model writes is the case the acceptance criterion names,
    and a model that was shown only the product's tables can still name another one.
    """
    await scenario.product(table_ids=[scenario.orders.id])
    orchestrator, model = _orchestrator(
        monkeypatch, model_sql="SELECT l.amount FROM retail.secret_ledger AS l"
    )

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(orchestrator, scenario)

    assert str(refused.value) == CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE
    run = await _latest_run(scenario)
    assert run.failure_reason == CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE
    # The model route really was asked, and no memory candidate stood in for it, so this
    # is the plain MODEL_GATEWAY path rather than QUERY_MEMORY_ADAPTATION.
    assert model is not None and len(model.calls) == 1
    assert "query_memory_match" not in run.plan_evidence


async def test_memory_adapted_sql_is_held_to_the_product(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The QUERY_MEMORY_ADAPTATION strategy reaches the same boundary.

    A prior successful answer is grounding for the next one, never an authorization: the
    product a question is asked through decides what may be read, whatever shaped the SQL.
    """
    await scenario.product(table_ids=[scenario.orders.id])
    completed_at = datetime.now(UTC) - timedelta(days=1)
    prior_execution = QueryExecution(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        principal_id="prior-principal",
        status="COMPLETED",
        dialect="postgres",
        sql_hash=f"sql-hash-{uuid4().hex[:8]}",
        normalized_sql="SELECT order_id FROM retail.orders",
        referenced_tables=["retail.orders"],
        semantic_version=scenario.semantic_version,
    )
    scenario.db.add(prior_execution)
    await scenario.db.flush()
    prior_run = AgentRun(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        principal_id="prior-principal",
        status="COMPLETED",
        question_hash=f"q-{uuid4().hex[:8]}",
        generation_source="MODEL_GATEWAY",
        semantic_version=scenario.semantic_version,
        query_execution_id=prior_execution.id,
    )
    scenario.db.add(prior_run)
    await scenario.db.flush()
    prior_run.updated_at = completed_at
    scenario.db.add(
        QueryMemoryEvidence(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            agent_run_id=prior_run.id,
            query_execution_id=prior_execution.id,
            question_hash=prior_run.question_hash,
            sql_hash=prior_execution.sql_hash,
            semantic_version=scenario.semantic_version,
            status="ELIGIBLE",
            positive_feedback_count=1,
        )
    )
    scenario.orders.updated_at = completed_at - timedelta(days=1)
    scenario.db.add(scenario.orders)
    await scenario.db.flush()

    orchestrator, model = _orchestrator(
        monkeypatch,
        model_sql="SELECT l.amount FROM retail.secret_ledger AS l",
        memory=True,
    )

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(orchestrator, scenario)

    assert str(refused.value) == CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE
    run = await _latest_run(scenario)
    # `generation_source` is stamped only once the statement clears this stage, so the
    # memory match in the run's own evidence is what identifies the path that produced
    # the refused SQL.
    assert "query_memory_match" in run.plan_evidence
    assert model is not None and model.calls
    assert "query_memory_template" in model.calls[0]["payload"]


async def test_development_sql_is_held_to_the_product(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenario.product(table_ids=[scenario.orders.id])
    orchestrator, _ = _orchestrator(monkeypatch, development_sql=True)

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(
            orchestrator,
            scenario,
            candidate_sql="SELECT l.amount FROM retail.secret_ledger AS l",
        )

    assert str(refused.value) == CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE


async def test_an_eligible_tools_declared_dependency_must_fit_the_product(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: eligibility selects a tool; it does not widen the table scope.

    The GOVERNED_TOOL strategy was exempt from the boundary outright, on the stated ground
    that "the product declared that version eligible" -- while nothing at product version
    create/update, product version approval, tool version approval or tool draft creation
    ever compared the tool's tables with the product's. So declaring a tool eligible
    silently widened the product to whatever that tool read.
    """
    version = await scenario.tool_version(
        referenced_tables=["retail.secret_ledger"],
        sql_template="SELECT l.amount FROM retail.secret_ledger AS l",
    )
    await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])
    orchestrator, _ = _orchestrator(monkeypatch)

    with pytest.raises(AgentPolicyRejected) as refused:
        await _ask(orchestrator, scenario, preferred_tool_version_id=version.id)

    assert str(refused.value) == CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE
    run = await _latest_run(scenario)
    assert run.failure_reason == (
        f"{CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE}:retail.secret_ledger"
    )


async def test_an_eligible_tool_within_the_product_still_runs(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract narrows; it does not break a product that named its tool's tables."""
    version = await scenario.tool_version(
        referenced_tables=["retail.orders"],
        sql_template="SELECT o.order_id FROM retail.orders AS o",
    )
    await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])
    orchestrator, _ = _orchestrator(monkeypatch)

    result = await _ask(orchestrator, scenario, preferred_tool_version_id=version.id)

    assert result.agent_run.generation_source == "GOVERNED_TOOL"
    assert result.agent_run.status == "COMPLETED"


async def test_the_post_execution_recheck_names_the_product_boundary(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VALIDATED_TABLE_OUTSIDE_CONTEXT_PRODUCT` had no test at all before F01.

    Driven by making the two pre-execution checks disagree with the checkpoint the way a
    defect would: the run is scoped to a product, and the product's table is retired
    between the boundary check and the re-check, so the executed statement's table is no
    longer inside the scope the checkpoint re-derives. This is the drift the independent
    re-check exists to catch (C3).
    """
    product = await scenario.product(table_ids=[scenario.orders.id])
    orchestrator, _ = _orchestrator(monkeypatch, development_sql=True)
    scope_holder: dict[str, Any] = {}
    original = orchestrator._checkpoint_validated

    async def _checkpoint(session: Any, **kwargs: Any) -> str | None:
        # Narrow the scope to nothing at the moment the checkpoint runs -- the
        # in-flight equivalent of a product version edited underneath a run.
        kwargs["scope"] = kwargs["scope"].__class__(
            version_id=product.id,
            version=1,
            table_ids=frozenset(),
            tool_version_ids=frozenset(),
            routine_ids=frozenset(),
            ontology_version_ids=frozenset(),
            glossary_term_version_ids=frozenset(),
            semantic_model_version_ids=frozenset(),
        )
        scope_holder["called"] = True
        return await original(session, **kwargs)

    orchestrator._checkpoint_validated = _checkpoint  # type: ignore[method-assign]

    with pytest.raises(QueryRejected) as refused:
        await _ask(
            orchestrator,
            scenario,
            candidate_sql="SELECT o.order_id FROM retail.orders AS o",
        )

    assert scope_holder["called"]
    assert "VALIDATED_TABLE_OUTSIDE_CONTEXT_PRODUCT:retail.orders" in str(refused.value)


# ---------------------------------------------------------------------------
# G1: the MCP tools/call surface
# ---------------------------------------------------------------------------


async def test_mcp_tools_call_forwards_the_product_it_resolved(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1: the resolved product filtered tool eligibility and was then dropped.

    `request.context_product_key` was None and `retrieved.context_product_scope` was
    None, so neither the pre-execution check nor the post-execution re-check ran on this
    surface at all: a curated product bounded which tool could be called here, and
    nothing about which tables its SQL read. The run's RESOLVED trace records the version
    only when a product actually resolved, so it is the receipt that proves the forward.
    """
    version = await scenario.tool_version(
        referenced_tables=["retail.orders"],
        sql_template="SELECT o.order_id FROM retail.orders AS o",
    )
    await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: FakeSqlExecutor(({"order_id": "O-1"},)),
    )

    result = await _handle_tools_call(
        {
            "name": "atlas__order_lookup",
            "arguments": {},
            "contextProductUri": f"atlas://context-products/{PRODUCT_KEY}/versions/1",
        },
        scenario.db,
        scenario.analyst(),
        Settings(_env_file=None),
        "corr-mcp-scoped",
    )

    assert not result.get("isError"), result
    run = await _latest_run(scenario)
    resolved = [step for step in run.step_trace if step.get("stage") == "RESOLVED"]
    assert resolved and resolved[-1]["details"]["context_product_version"] == 1


async def test_mcp_tools_call_refuses_a_tool_reaching_past_the_product(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence of forwarding it: the boundary now applies on this surface."""
    version = await scenario.tool_version(
        referenced_tables=["retail.secret_ledger"],
        sql_template="SELECT l.amount FROM retail.secret_ledger AS l",
    )
    await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: FakeSqlExecutor(({"amount": 1},)),
    )

    result = await _handle_tools_call(
        {
            "name": "atlas__order_lookup",
            "arguments": {},
            "contextProductUri": f"atlas://context-products/{PRODUCT_KEY}/versions/1",
        },
        scenario.db,
        scenario.analyst(),
        Settings(_env_file=None),
        "corr-mcp-refused",
    )

    assert result.get("isError")
    assert CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE in result["content"][0]["text"]


async def test_mcp_tools_call_without_a_product_uri_is_unchanged(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No product asked for, no product enforced -- the same tool call still runs."""
    version = await scenario.tool_version(
        referenced_tables=["retail.secret_ledger"],
        sql_template="SELECT l.amount FROM retail.secret_ledger AS l",
    )
    await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: FakeSqlExecutor(({"amount": 1},)),
    )

    result = await _handle_tools_call(
        {"name": "atlas__order_lookup", "arguments": {}},
        scenario.db,
        scenario.analyst(),
        Settings(_env_file=None),
        "corr-mcp-unscoped",
    )

    assert not result.get("isError"), result
    run = await _latest_run(scenario)
    resolved = [step for step in run.step_trace if step.get("stage") == "RESOLVED"]
    assert resolved and "context_product_version" not in resolved[-1]["details"]
