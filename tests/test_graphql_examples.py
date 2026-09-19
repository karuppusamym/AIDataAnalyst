"""R11-GQL01 (GQL-E): the Agent Gateway examples cannot rot.

`Docs/90-reference/graphql-examples.md` is read here, not copied: every `### Name` section's
`graphql` document, `json` variables and caller roles. Each operation is admitted against the
served schema at the default limits, and then run -- in page order, through the real
`POST /graphql` -- against a seeded estate, with the placeholders filled from it. An example
that stops validating, stops being admitted or starts answering with an error fails here.
The page must also keep covering what an agent needs: catalog, lineage, context products,
coverage and the governed execution mutation.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from graphql import FieldNode, OperationDefinitionNode, parse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.connectors.base import QueryResult
from aida.envelope_models import MetadataRoutine
from aida.graphql_limits import DEFAULT_LIMITS, admit_document
from aida.graphql_schema import metadata_schema
from aida.main import app
from aida.models import ContextProductRoleBinding, MetadataConstraint
from aida.procedure_lineage_models import RoutineParseCoverage
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.doubles import FakeSqlExecutor
from tests.test_f01_context_product_execution_boundary import _Scenario

EXAMPLES_PATH = (
    Path(__file__).resolve().parents[1] / "Docs" / "90-reference" / "graphql-examples.md"
)

_SECTION = re.compile(r"^### (?P<name>\S+)\s*$", re.MULTILINE)
_ROLES = re.compile(r"\*\*Caller roles:\*\* `(?P<roles>[^`]+)`")
_BLOCK = re.compile(r"```(?P<lang>graphql|json)\n(?P<body>.*?)```", re.DOTALL)

#: What an agent needs, by the root fields the examples must between them select.
_REQUIRED_ROOTS = {
    "datasources",  # catalog
    "table",
    "lineageImpact",  # lineage
    "lineageGraph",
    "contextProducts",  # context products
    "contextProductVersion",
    "contextProductCoverage",  # coverage
    "routineParseCoverage",
    "executeGovernedTool",  # the governed execution mutation (R11-GQL02)
    "governedExecutions",
}


@dataclass(frozen=True)
class Example:
    name: str
    roles: str
    query: str
    variables: dict[str, Any]


def _examples() -> list[Example]:
    text = EXAMPLES_PATH.read_text(encoding="utf-8")
    headings = list(_SECTION.finditer(text))
    examples: list[Example] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end() : end]
        roles = _ROLES.search(section)
        blocks = {match["lang"]: match["body"] for match in _BLOCK.finditer(section)}
        assert roles is not None, f"{heading['name']} names no caller roles"
        assert set(blocks) == {"graphql", "json"}, f"{heading['name']} needs one of each block"
        examples.append(
            Example(
                name=heading["name"],
                roles=roles["roles"],
                query=blocks["graphql"],
                variables=json.loads(blocks["json"]),
            )
        )
    return examples


def _operation(example: Example) -> OperationDefinitionNode:
    (operation,) = [
        definition
        for definition in parse(example.query).definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    return operation


# --- static: the page is well formed and admitted ---------------------------------------


def test_the_page_names_each_example_by_its_operation() -> None:
    examples = _examples()
    assert len(examples) >= len(_REQUIRED_ROOTS) - 1
    names = [example.name for example in examples]
    assert len(names) == len(set(names)), "two examples share an operation name"
    for example in examples:
        operation = _operation(example)
        assert operation.name is not None and operation.name.value == example.name


def test_the_examples_cover_what_an_agent_needs() -> None:
    roots = {
        selection.name.value
        for example in _examples()
        for selection in _operation(example).selection_set.selections
        if isinstance(selection, FieldNode)
    }
    assert _REQUIRED_ROOTS <= roots, sorted(_REQUIRED_ROOTS - roots)


@pytest.mark.parametrize("example", _examples(), ids=lambda example: example.name)
def test_each_example_is_admitted_at_the_default_limits(example: Example) -> None:
    """Parsed, bounded and validated exactly as the endpoint admits a document, before any
    estate exists: the operation name, depth, aliases, page sizes and object budget."""
    _, cost = admit_document(
        query=example.query,
        operation_name=example.name,
        variables=example.variables,
        schema=metadata_schema._schema,
        limits=DEFAULT_LIMITS,
    )
    assert cost.estimated_nodes <= DEFAULT_LIMITS.max_nodes
    if example.name == "CatalogOverview":
        # The page states this price; hold it to it.
        assert cost.estimated_nodes == 112


# --- behavioural: every example runs against a seeded estate ------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture(autouse=True)
def _doubled_source(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No real secret and no real connector: the execution example runs against a double
    that records what it executes."""
    executed: list[str] = []

    class _Recording(FakeSqlExecutor):
        def __init__(self) -> None:
            super().__init__(({"order_id": "O-1"}, {"order_id": "O-2"}))

        async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
            executed.append(sql)
            return await super().execute_read_query(sql, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: _Recording(),
    )
    return executed


@pytest_asyncio.fixture
async def placeholders(db: AsyncSession) -> dict[str, str]:
    """The estate the examples run against, as the values their placeholders stand for."""
    scenario = await _Scenario(db).build()
    tool = await scenario.tool_version(
        referenced_tables=["retail.orders"],
        sql_template="SELECT o.order_id FROM retail.orders AS o",
    )
    routine = MetadataRoutine(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        schema_id=scenario.retail.id,
        name="rebuild_orders",
        signature="()",
        routine_type="PROCEDURE",
        body_sql_redacted="BEGIN INSERT INTO retail.customer SELECT * FROM retail.orders; END",
        fingerprint="fp-rebuild-orders",
    )
    db.add(routine)
    await db.flush()
    db.add_all(
        [
            RoutineParseCoverage(
                organization_id=scenario.organization.id,
                datasource_id=scenario.datasource.id,
                routine_id=routine.id,
                parse_completed=True,
                is_read_only=False,
                statement_count=1,
                unparsed_statement_count=0,
                unparsed_reason_codes="",
                dialect="postgres",
                confidence="HIGH",
                sql_hash="r" * 64,
                parsed_at=datetime(2026, 9, 18, tzinfo=UTC),
            ),
            # A declared key, so the lineage examples have an edge to follow.
            MetadataConstraint(
                id=uuid4(),
                organization_id=scenario.organization.id,
                datasource_id=scenario.datasource.id,
                table_id=scenario.orders.id,
                name="fk_orders_customer",
                constraint_type="FOREIGN_KEY",
                columns=["customer_id"],
                referenced_table_id=scenario.customer.id,
                referenced_columns=["customer_id"],
                fingerprint="fp-fk",
            ),
        ]
    )
    version = await scenario.product(
        table_ids=[scenario.orders.id, scenario.customer.id], tool_version_ids=[tool.id]
    )
    version.routine_ids = [str(routine.id)]
    # What the publish flow writes beside a version: the consumer role an ask resolves on.
    db.add(
        ContextProductRoleBinding(
            organization_id=scenario.organization.id,
            context_product_version_id=version.id,
            role_name="Analyst",
        )
    )
    await db.commit()
    return {
        "<organization-id>": str(scenario.organization.id),
        "<datasource-id>": str(scenario.datasource.id),
        "<table-id>": str(scenario.orders.id),
        "<project-id>": str(scenario.project.id),
        "<context-product-version-id>": str(version.id),
        "<routine-id>": str(routine.id),
        "<tool-version-id>": str(tool.id),
    }


@pytest_asyncio.fixture
async def http(db: AsyncSession, placeholders: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://examples.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _filled(value: Any, placeholders: dict[str, str]) -> Any:
    if isinstance(value, str):
        return placeholders.get(value, value)
    if isinstance(value, dict):
        return {key: _filled(item, placeholders) for key, item in value.items()}
    if isinstance(value, list):
        return [_filled(item, placeholders) for item in value]
    return value


async def test_every_example_runs_against_the_endpoint_without_an_error(
    http: httpx.AsyncClient, placeholders: dict[str, str], _doubled_source: list[str]
) -> None:
    answered: dict[str, dict[str, Any]] = {}
    for example in _examples():
        variables = _filled(example.variables, placeholders)
        unfilled = re.findall(r"<[a-z-]+>", json.dumps(variables))
        assert not unfilled, f"{example.name} uses a placeholder the test cannot fill: {unfilled}"
        response = await http.post(
            "/graphql",
            json={"query": example.query, "operationName": example.name, "variables": variables},
            headers={
                "X-Principal-Id": "gateway-agent-developer",
                "X-Principal-Type": "USER",
                "X-Roles": example.roles,
                "X-Organization-Id": placeholders["<organization-id>"],
            },
        )
        body = response.json()
        assert response.status_code == 200, (example.name, body)
        assert "errors" not in body, (example.name, body)
        assert all(value is not None for value in body["data"].values()), (example.name, body)
        answered[example.name] = body["data"]

    # The examples answer with something, not merely without an error.
    catalog = answered["CatalogOverview"]["datasources"]
    assert catalog["totalCount"] == 1 and catalog["nodes"][0]["tables"]["totalCount"] == 4
    assert answered["TableWithMeaning"]["table"]["constraints"]["nodes"][0]["referencedTable"]
    impact = answered["LineageImpact"]["lineageImpact"]
    assert impact["upstream"]["totalCount"] + impact["downstream"]["totalCount"] >= 1
    assert answered["LineageGraph"]["lineageGraph"]["returnedEdgeCount"] >= 1
    assert answered["AskableContextProducts"]["contextProducts"]["totalCount"] == 1
    assert answered["ReadContextProductVersion"]["contextProductVersion"]["status"] == "PUBLISHED"
    coverage = answered["ContextProductCoverage"]["contextProductCoverage"]
    assert [node["qualifiedName"] for node in coverage["routines"]["nodes"]] == [
        "warehouse.retail.rebuild_orders"
    ]
    assert answered["RoutineParseCoverage"]["routineParseCoverage"]["state"] == "SUPPORTED"
    execution = answered["ExecuteGovernedTool"]["executeGovernedTool"]
    assert execution["receipt"]["status"] == "COMPLETED"
    assert execution["result"]["rows"] == [["O-1"], ["O-2"]]
    assert len(_doubled_source) == 1, "the mutation executed once; no query executed anything"
    receipts = answered["MyExecutionReceipts"]["governedExecutions"]
    assert [node["id"] for node in receipts["nodes"]] == [execution["receipt"]["id"]]
