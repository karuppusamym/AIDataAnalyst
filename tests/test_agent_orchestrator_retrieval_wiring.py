"""RT-1, RT-2, RT-3, RT-9, SM-2: retrieval is wired into the live orchestration path.

`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` Section 2 found that
`retrieval.py` (BM25 + vector similarity + graph expansion + fusion ranking, ~2,320
lines across `fusion_ranking.py`, `vector_store.py`, `embedding_provider.py`,
`graph_retrieval.py`, `vector_retrieval.py`) was fully built and unit-tested in
isolation, but never called by anything a real user request reaches --
`agent_intelligence.GovernedRetriever` ran its own narrower hand-rolled lexical
scan instead, and `retrieval.py:43-52` documented a hand-off to
`GovernedRetriever.retrieve()` that was never made.

This file proves the hand-off now happens for real, through the actual entry point
(`GovernedAgentOrchestrator.run()`, the same object `api.py`'s
`POST /datasources/{id}/agent-analyses` route constructs and calls) rather than by
calling `hybrid_retrieve`/`hybrid_retrieve_enhanced` directly and asserting on their
return value -- the exact isolation gap the audit named as the failure mode that
let this go unnoticed for six P0 tracker rows.

Scenario: a table (`fact_orders`) matches the question lexically; a second table
(`dim_customer`) shares no token with the question at all and is reachable only by
following a real `MetadataConstraint` foreign key from `fact_orders` -- so its
presence in `agent_run.retrieval_evidence` can only be explained by graph expansion
actually running, not by coincidental lexical overlap. A governed tool is planned
via `preferred_tool_version_id` with a missing required parameter, so the run
reaches `CLARIFICATION` (retrieval + planning have already completed and persisted)
without needing a real SQL warehouse or model route -- keeping the proof to the
retrieval subsystem this change is scoped to.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from itertools import count
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_orchestrator import AgentClarificationRequired, GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AgentRun,
    AnalysisRun,
    AuditEvent,
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from tests.support.doubles import security_context

# `AuditEvent.id` is a `BigInteger` autoincrement PK relying in production on
# Postgres's own sequence; sqlite only auto-populates a bare `INTEGER PRIMARY KEY`.
# Same workaround as `test_kill_switch_drill.py` / `test_bulk_governance_decisions.py`.
# `_persist_rejection` (agent_orchestrator.py) writes one on the CLARIFICATION path
# this test exercises.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Commerce",
            code="COMMERCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Commerce",
            slug="core-commerce",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
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

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="public",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        # `fact_orders` matches the question ("orders") lexically. `dim_customer`
        # shares no token with the question or with `fact_orders`'s own text -- the
        # only path to it is the foreign key below, via graph expansion.
        self.fact_orders = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_orders",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-fact-orders",
            source_description="Order fact table",
        )
        db.add(self.fact_orders)
        self.dim_customer = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="dim_customer",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-dim-customer",
            source_description="Customer reference dimension, unrelated wording",
        )
        db.add(self.dim_customer)
        await db.flush()

        fk = MetadataConstraint(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=self.fact_orders.id,
            name="fk_orders_customer",
            constraint_type="FOREIGN_KEY",
            columns=["customer_id"],
            referenced_table_id=self.dim_customer.id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp-fk-orders-customer",
        )
        db.add(fk)

        completed_analysis = AnalysisRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            status="COMPLETED",
        )
        db.add(completed_analysis)

        self.tool = GovernedTool(
            organization_id=self.organization.id, project_id=self.project.id, slug="order_lookup"
        )
        db.add(self.tool)
        await db.flush()
        self.tool_version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=self.tool.id,
            version=1,
            status="PUBLISHED",
            name="Order Lookup",
            description="Look up orders by customer",
            datasource_id=self.datasource.id,
            sql_template="SELECT 1",
            referenced_tables=[],
            # Deliberately no default -- `customer_id` is never supplied by the test
            # caller, so GovernedPlanner lands on CLARIFICATION rather than executing.
            parameter_schema=[{"name": "customer_id", "type": "string", "required": True}],
            allowed_roles=["Analyst"],
            fingerprint="fp-order-lookup",
            created_by="tool-dev",
        )
        db.add(self.tool_version)
        await db.flush()
        return self

    def steward(self):
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"Analyst"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "embedding_provider": "openai",
        "embedding_credential_reference": "env://OPENAI_API_KEY",
        "openai_api_key": "sk-test",
        "embedding_dimensions": 8,
        "model_provider_max_attempts": 1,
    }
    base.update(overrides)
    return Settings(**base)


def _basis_vector(index: int, dimensions: int = 8) -> list[float]:
    vec = [0.0] * dimensions
    vec[index % dimensions] = 1.0
    return vec


@pytest.fixture
def stub_embedding_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real network call through `embedding_provider.py`'s OpenAI adapter, answered
    by a fake transport instead of a live API -- the same pattern
    `test_embedding_provider.py` uses. Reads the actual request body so it answers
    however many texts the retrieval pipeline batches into one call, rather than a
    hardcoded count.
    """
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        vectors = [
            {"index": i, "embedding": _basis_vector(i)} for i in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"data": vectors})

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("aida.embedding_provider.httpx.AsyncClient", factory)


async def test_orchestrator_run_surfaces_graph_and_vector_evidence_from_real_retrieval(
    scenario: _Scenario, stub_embedding_transport: None
) -> None:
    settings = _settings()
    orchestrator = GovernedAgentOrchestrator(settings)

    with pytest.raises(AgentClarificationRequired):
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id="corr-rt-wiring",
            question="orders",
            candidate_sql=None,
            preferred_tool_version_id=scenario.tool_version.id,
            tool_parameters={},
            requested_limit=None,
        )

    # Retrieval evidence was set on `agent_run` before the CLARIFICATION branch was
    # reached, and is persisted by `_persist_rejection`'s commit -- read it back with
    # a fresh query (not the in-memory object `run()` built) to prove it was actually
    # written to the database, not just held in a local variable.
    agent_run = (
        await scenario.db.execute(
            select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
        )
    ).scalar_one()
    assert agent_run.status == "REJECTED"
    assert agent_run.failure_reason == "MISSING_TOOL_PARAMETERS:customer_id"

    hits_by_type: dict[str, list[dict[str, Any]]] = {}
    for hit in agent_run.retrieval_evidence:
        hits_by_type.setdefault(hit["object_type"], []).append(hit)

    # The governed tool was planned via `preferred_tool_version_id` -- proves the
    # GOVERNED_TOOL object-type/metadata contract (`allowed_roles`,
    # `required_parameters`) survived the hand-off to `hybrid_retrieve_enhanced`.
    assert hits_by_type["GOVERNED_TOOL"][0]["object_id"] == str(scenario.tool_version.id)

    # `fact_orders` is the lexical seed.
    table_hits = {hit["object_id"]: hit for hit in hits_by_type.get("TABLE", [])}
    assert str(scenario.fact_orders.id) in table_hits

    # `dim_customer` shares no token with "orders" or with `fact_orders`'s own name/
    # description -- it can only be present because graph expansion followed the real
    # `MetadataConstraint` foreign key from the seed. This is the proof the audit
    # asked for: evidence from the live orchestration path, not a direct call into
    # `retrieval.py`.
    assert str(scenario.dim_customer.id) in table_hits, (
        "dim_customer only reachable via the FOREIGN_KEY graph edge from fact_orders -- "
        "its absence means graph expansion did not run on the live path"
    )
    dim_customer_hit = table_hits[str(scenario.dim_customer.id)]
    dim_customer_signals = dim_customer_hit["metadata"]["retrieval_evidence"]["source_signals"]
    assert "graph" in dim_customer_signals
    assert dim_customer_hit["metadata"]["graph_expansion_path"] == [
        f"TABLE:{scenario.fact_orders.id}",
        f"TABLE:{scenario.dim_customer.id}",
    ]

    # The stubbed embedding provider proves the vector-similarity stage is live too --
    # every candidate the enhanced pipeline embedded carries a "vector" source signal.
    fact_orders_hit = table_hits[str(scenario.fact_orders.id)]
    fact_orders_signals = fact_orders_hit["metadata"]["retrieval_evidence"]["source_signals"]
    assert "lexical" in fact_orders_signals
    assert "vector" in fact_orders_signals
