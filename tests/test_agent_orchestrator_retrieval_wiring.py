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
from datetime import UTC, datetime
from itertools import count
from typing import Any
from uuid import UUID, uuid4

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
    DataQualityIncident,
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
    QueryExecution,
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


# ---------------------------------------------------------------------------
# RT-6 / RT-7: quality_trust and usage_popularity are real ranking factors,
# not the raw_score=0.5 placeholder both carried in `hybrid_retrieve_enhanced`
# Stage 4 before this wave.
# ---------------------------------------------------------------------------


async def _run_to_clarification(
    orchestrator: GovernedAgentOrchestrator, scenario: _Scenario, *, correlation_id: str
) -> None:
    """Drives one `GovernedAgentOrchestrator.run()` call through to its
    CLARIFICATION rejection -- retrieval and planning have already completed
    and `agent_run.retrieval_evidence` is persisted by the time this raises,
    same shape as `test_orchestrator_run_surfaces_graph_and_vector_evidence_
    from_real_retrieval` above.
    """
    with pytest.raises(AgentClarificationRequired):
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.steward(),
            correlation_id=correlation_id,
            question="orders",
            candidate_sql=None,
            preferred_tool_version_id=scenario.tool_version.id,
            tool_parameters={},
            requested_limit=None,
        )


async def _latest_fact_orders_hit(
    scenario: _Scenario, seen_run_ids: set[UUID]
) -> dict[str, Any]:
    """Returns the `fact_orders` retrieval-evidence hit from whichever
    `AgentRun` was created since the last call -- lets a test drive multiple
    real `orchestrator.run()` calls against the same datasource and compare
    the *same table's* fused score/factors across them.
    """
    rows = (
        await scenario.db.execute(
            select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
        )
    ).scalars().all()
    fresh = [row for row in rows if row.id not in seen_run_ids]
    assert len(fresh) == 1, "expected exactly one new AgentRun since the last check"
    seen_run_ids.add(fresh[0].id)
    hits_by_id = {hit["object_id"]: hit for hit in fresh[0].retrieval_evidence}
    return hits_by_id[str(scenario.fact_orders.id)]


async def test_orchestrator_run_demotes_table_with_open_quality_incident(
    scenario: _Scenario,
) -> None:
    """RT-7: `quality_coupling.demote_in_retrieval` -- already real, tested, and
    reachable from TL-3's tool gate and AG-6's answer trust warning
    (`tests/test_quality_runtime_coupling.py`) -- now also feeds
    `fusion_ranking`'s `quality_trust` signal for real, through the live
    `GovernedAgentOrchestrator.run()` path RT-1/RT-2/RT-3 wired up. Before this
    change, `hybrid_retrieve_enhanced` Stage 4 gave every candidate a hardcoded
    `raw_score=0.5` for `quality_trust` regardless of its actual incidents.

    Same table (`fact_orders`), same question, same everything else across two
    real `run()` calls -- no embedding provider configured, so the vector
    stage is skipped and doesn't confound the comparison (mirrors
    `test_quality_runtime_coupling.py`'s use of a bare `Settings()`). The only
    variable between the two calls is whether `fact_orders` carries an OPEN
    CRITICAL `DataQualityIncident`.
    """
    orchestrator = GovernedAgentOrchestrator(Settings())
    seen_run_ids: set[UUID] = set()

    await _run_to_clarification(orchestrator, scenario, correlation_id="corr-rt7-baseline")
    baseline_hit = await _latest_fact_orders_hit(scenario, seen_run_ids)
    baseline_factors = {
        f["signal"]: f for f in baseline_hit["metadata"]["retrieval_evidence"]["factors"]
    }
    assert baseline_factors["quality_trust"]["raw_score"] == 1.0

    incident = DataQualityIncident(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        table_id=scenario.fact_orders.id,
        fingerprint="fp-rt7-incident",
        anomaly_type="NULL_RATE_SHIFT",
        severity="CRITICAL",
        status="OPEN",
        summary="Null rate spiked outside the governed baseline.",
        first_observed_at=datetime.now(UTC),
        last_observed_at=datetime.now(UTC),
    )
    scenario.db.add(incident)
    await scenario.db.flush()

    await _run_to_clarification(orchestrator, scenario, correlation_id="corr-rt7-demoted")
    demoted_hit = await _latest_fact_orders_hit(scenario, seen_run_ids)
    demoted_factors = {
        f["signal"]: f for f in demoted_hit["metadata"]["retrieval_evidence"]["factors"]
    }
    # An OPEN CRITICAL incident demotes to 0.3 (quality_coupling.demote_in_retrieval).
    assert demoted_factors["quality_trust"]["raw_score"] == 0.3

    # Every other signal for `fact_orders` against this question is unaffected by
    # adding the incident (lexical/graph scoring never reads `DataQualityIncident`),
    # so the fused score drop is attributable to the quality_trust demotion alone.
    for signal in ("lexical", "usage_popularity"):
        if signal in baseline_factors and signal in demoted_factors:
            assert (
                baseline_factors[signal]["raw_score"] == demoted_factors[signal]["raw_score"]
            )
    assert demoted_hit["score"] < baseline_hit["score"], (
        "fact_orders' fused rank should drop once it carries an open CRITICAL "
        "quality incident -- quality_trust is a real signal now, not a fixed 0.5"
    )


async def test_orchestrator_run_promotes_table_with_real_execution_history(
    scenario: _Scenario,
) -> None:
    """RT-6: usage/popularity is now a real ranking factor derived from
    `QueryExecution.referenced_tables` -- genuine, already-persisted execution
    history (the same rows AG-6 reads off `gateway_result.execution.
    referenced_tables` once a query finishes), not a new tracking mechanism
    and not the hardcoded `raw_score=0.5` `hybrid_retrieve_enhanced` Stage 4
    gave every candidate before this change.

    Same table (`fact_orders`), same question, same everything else across two
    real `GovernedAgentOrchestrator.run()` calls; the only variable is whether
    a handful of completed `QueryExecution` rows already reference
    `fact_orders` by the time retrieval runs.

    `dim_customer` (also a retrieval candidate here, via graph expansion) is
    seeded with a *fixed* 2 completed executions before either `run()` call --
    a stable middle popularity value (0.2) both runs see unchanged. Without
    it, `fact_orders` and every other 0-execution candidate tie for
    usage_popularity in the baseline run, and ties can resolve in
    `fact_orders`'s favour by pure insertion-order luck, making a fused-score
    comparison prove nothing. With `dim_customer` fixed at 0.2, `fact_orders`
    starts ranked *below* it (0.0 < 0.2) and must rank *above* it (0.5 > 0.2)
    once its own execution history lands -- a real rank crossing, not a
    coin flip.
    """
    orchestrator = GovernedAgentOrchestrator(Settings())
    seen_run_ids: set[UUID] = set()

    async def _add_completed_executions(table_name: str, count: int, *, prefix: str) -> None:
        for i in range(count):
            scenario.db.add(
                QueryExecution(
                    organization_id=scenario.organization.id,
                    datasource_id=scenario.datasource.id,
                    principal_id="analyst-1",
                    status="COMPLETED",
                    dialect=scenario.datasource.dialect,
                    sql_hash=f"fake-sql-hash-{prefix}-{i}",
                    referenced_tables=[table_name],
                )
            )
        await scenario.db.flush()

    await _add_completed_executions("dim_customer", 2, prefix="dim-customer-fixed")

    await _run_to_clarification(orchestrator, scenario, correlation_id="corr-rt6-baseline")
    baseline_hit = await _latest_fact_orders_hit(scenario, seen_run_ids)
    baseline_factors = {
        f["signal"]: f for f in baseline_hit["metadata"]["retrieval_evidence"]["factors"]
    }
    assert baseline_factors["usage_popularity"]["raw_score"] == 0.0

    await _add_completed_executions("fact_orders", 5, prefix="fact-orders")

    await _run_to_clarification(orchestrator, scenario, correlation_id="corr-rt6-popular")
    popular_hit = await _latest_fact_orders_hit(scenario, seen_run_ids)
    popular_factors = {
        f["signal"]: f for f in popular_hit["metadata"]["retrieval_evidence"]["factors"]
    }
    # 5 completed executions / a saturation point of 10 -> 0.5 (retrieval.py's
    # `_USAGE_POPULARITY_SATURATION`).
    assert popular_factors["usage_popularity"]["raw_score"] == 0.5

    for signal in ("lexical", "quality_trust"):
        if signal in baseline_factors and signal in popular_factors:
            assert (
                baseline_factors[signal]["raw_score"] == popular_factors[signal]["raw_score"]
            )
    assert popular_hit["score"] > baseline_hit["score"], (
        "fact_orders' fused rank should rise once real execution history "
        "references it -- usage_popularity is a real signal now, not a fixed 0.5"
    )
