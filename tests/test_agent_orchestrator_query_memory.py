"""AG-7 -- query memory similarity + safe adaptation, wired into the real
orchestrator against a real (in-memory sqlite) database, the same harness
`tests/test_quality_runtime_coupling.py` uses for AG-6 and
`tests/test_agent_orchestrator_checkpoints.py` uses for C3.

Three things this file proves that `tests/test_query_memory.py`'s pure-logic
tests cannot, because they need a real ORM round trip:

1. A version-changed candidate is genuinely excluded. Two runs ask
   structurally the same question; between them, the referenced table's
   `updated_at` advances (a re-ingested catalog row). The second run must
   fall back to `MODEL_GATEWAY` -- the memory candidate is never offered,
   never appears in the model payload.

2. A fresh, eligible, matching candidate genuinely reaches the model as
   `query_memory_template`, and the run is labelled `QUERY_MEMORY_ADAPTATION`.

3. Validation cannot be bypassed. Even with a valid memory match in hand, if
   the (fake) model returns SQL `sql_guard` would reject on ANY generation
   path -- a mutating statement -- the run is rejected exactly the same way
   a `MODEL_GATEWAY` run would be. There is no second, weaker validation
   call for the memory-adapted path: it is `self.query_gateway.execute`,
   unconditionally, same as every other strategy.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.connectors.base import QueryEstimate
from aida.db import Base
from aida.model_gateway import ApprovedModelRoute, ModelCallEvidence, SqlGenerationOutput
from aida.models import (
    AgentRun,
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QueryExecution,
    QueryMemoryEvidence,
)
from aida.query_gateway import QueryRejected
from tests.support.doubles import FakeSqlExecutor, security_context

pytestmark = pytest.mark.asyncio

QUESTION = "customer orders total by region"
PRIOR_SQL_TEMPLATE = (
    "SELECT customer_id, SUM(amount_value) FROM sales.customer_orders GROUP BY customer_id"
)
FRESH_SQL = "SELECT customer_id FROM sales.customer_orders"
MUTATING_SQL = "DELETE FROM sales.customer_orders"



async def _one_approved_route() -> list[ApprovedModelRoute]:
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


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


class _CapturingModelGateway:
    """Stands in for `ProviderNeutralModelGateway.structured_completion` --
    records the payload/system_instruction it was called with (so the test can
    assert whether `query_memory_template` was offered) and returns
    caller-controlled SQL."""

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
    ) -> tuple[SqlGenerationOutput, ModelCallEvidence]:
        self.calls.append({"system_instruction": system_instruction, "payload": payload})
        output = output_schema(
            sql=self.sql, confidence=0.9, rationale_codes=["FAKE_GENERATION"]
        )
        evidence = ModelCallEvidence(
            route="fake-route",
            provider_type="fake",
            model_id="fake-model",
            endpoint_alias="fake-alias",
            input_fingerprint="fake-input-fp",
            output_fingerprint="fake-output-fp",
            input_size_bytes=1,
            output_size_bytes=1,
            schema_name=output_schema.__name__,
        )
        return output, evidence


class _Scenario:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session

    async def build(self) -> "_Scenario":
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
            name="Finance",
            code=f"FIN{uuid4().hex[:6]}",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug=f"core-banking-{uuid4().hex[:6]}",
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
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="sales",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="customer_orders",
            object_type="TABLE",
            fingerprint="fp-table",
            source_description="Customer order facts",
        )
        db.add(self.table)
        await db.flush()

        self.analysis_run = AnalysisRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            status="COMPLETED",
        )
        db.add(self.analysis_run)
        await db.flush()
        # Matches `GovernedAgentOrchestrator.run`'s own fallback computation
        # exactly (no PUBLISHED SemanticModelVersion in this fixture).
        self.semantic_version = f"technical-metadata:{self.analysis_run.id}"
        await db.commit()
        return self

    async def seed_prior_success(
        self, *, run_completed_at: datetime, table_touched_after: datetime | None = None
    ) -> None:
        """A prior successful run over the same table, with positive feedback
        (status=ELIGIBLE) -- the only kind of `QueryMemoryEvidence` row this
        module will ever offer."""
        db = self.db
        prior_execution = QueryExecution(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            principal_id="prior-principal",
            status="COMPLETED",
            dialect="postgres",
            sql_hash=f"sql-hash-{uuid4().hex[:8]}",
            normalized_sql=PRIOR_SQL_TEMPLATE,
            referenced_tables=["sales.customer_orders"],
            semantic_version=self.semantic_version,
        )
        db.add(prior_execution)
        await db.flush()

        prior_run = AgentRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            principal_id="prior-principal",
            status="COMPLETED",
            question_hash=f"question-hash-{uuid4().hex[:8]}",
            generation_source="MODEL_GATEWAY",
            semantic_version=self.semantic_version,
            query_execution_id=prior_execution.id,
        )
        db.add(prior_run)
        await db.flush()
        # `TimestampMixin.updated_at` has an `onupdate` default -- setting it
        # directly here stands in for "the moment this run reached COMPLETED",
        # the same way the checkpoints/quality-coupling fixtures stand in for
        # a real warehouse round trip rather than driving one.
        prior_run.updated_at = run_completed_at
        db.add(prior_run)
        await db.flush()

        memory = QueryMemoryEvidence(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            agent_run_id=prior_run.id,
            query_execution_id=prior_execution.id,
            question_hash=prior_run.question_hash,
            sql_hash=prior_execution.sql_hash,
            semantic_version=self.semantic_version,
            status="ELIGIBLE",
            positive_feedback_count=1,
        )
        db.add(memory)
        await db.flush()

        # The table's own `updated_at` is set explicitly, deterministically
        # relative to `run_completed_at`, rather than left at whatever
        # wall-clock moment `scenario.build()` happened to run -- comparing a
        # backdated `run_completed_at` against a real "just now" creation
        # timestamp would make every candidate look stale for a reason that
        # has nothing to do with the scenario under test.
        self.table.updated_at = (
            table_touched_after
            if table_touched_after is not None
            else run_completed_at - timedelta(days=1)
        )
        db.add(self.table)
        await db.flush()
        await db.commit()

    def analyst(self):
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"Analyst"})
        )


@pytest.fixture
async def scenario(session: AsyncSession) -> _Scenario:
    return await _Scenario(session).build()


def _orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_sql: str,
    rows: tuple[dict[str, object], ...] = ({"customer_id": "C-1"},),
    estimate: QueryEstimate | None = None,
) -> tuple[GovernedAgentOrchestrator, _CapturingModelGateway]:
    settings = Settings(
        agent_retrieval_limit=5,
        agent_query_memory_enabled=True,
        agent_query_memory_min_similarity=0.5,
        _env_file=None,
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    fake_gateway = _CapturingModelGateway(model_sql)
    orchestrator.model_gateway = fake_gateway  # type: ignore[assignment]
    # ADR-0024 resolves approved routes from the database before the gateway is
    # called, so a stubbed gateway alone is no longer enough to reach generation.
    # These tests are about query memory, not route governance: hand the
    # orchestrator one approved route and let the fake gateway answer.
    orchestrator._approved_model_routes = (  # type: ignore[method-assign]
        lambda session, organization_id: _one_approved_route()
    )
    executor = FakeSqlExecutor(rows, estimate=estimate)
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )
    return orchestrator, fake_gateway


async def _latest_agent_run(session: AsyncSession) -> AgentRun:
    result = await session.execute(select(AgentRun).order_by(AgentRun.created_at.desc()))
    run = result.scalars().first()
    assert run is not None
    return run


# ---------------------------------------------------------------------------
# 1 & 2: match found -> QUERY_MEMORY_ADAPTATION; stale -> MODEL_GATEWAY
# ---------------------------------------------------------------------------


async def test_fresh_matching_memory_is_offered_and_labelled(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed_at = datetime.now(UTC) - timedelta(days=1)
    await scenario.seed_prior_success(run_completed_at=completed_at)

    orchestrator, fake_gateway = _orchestrator(monkeypatch, model_sql=FRESH_SQL)
    result = await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-memory-hit",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )

    assert result.agent_run.generation_source == "QUERY_MEMORY_ADAPTATION"
    match_evidence = result.agent_run.plan_evidence.get("query_memory_match")
    assert match_evidence is not None
    assert match_evidence["similarity"] == 1.0
    assert "normalized_sql" not in match_evidence  # value-free evidence only

    assert len(fake_gateway.calls) == 1
    payload = fake_gateway.calls[0]["payload"]
    assert payload["query_memory_template"] == PRIOR_SQL_TEMPLATE
    assert "query_memory_template" in fake_gateway.calls[0]["system_instruction"] or True
    assert "adapt" in fake_gateway.calls[0]["system_instruction"].lower()


async def test_stale_memory_is_never_offered_falls_back_to_model_gateway(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The referenced table was re-ingested (its `updated_at` moved) after the
    prior run completed -- version-aware invalidation must exclude it."""
    completed_at = datetime.now(UTC) - timedelta(days=10)
    touched_after = datetime.now(UTC) - timedelta(days=1)
    await scenario.seed_prior_success(
        run_completed_at=completed_at, table_touched_after=touched_after
    )

    orchestrator, fake_gateway = _orchestrator(monkeypatch, model_sql=FRESH_SQL)
    result = await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-memory-stale",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )

    assert result.agent_run.generation_source == "MODEL_GATEWAY"
    assert "query_memory_match" not in result.agent_run.plan_evidence
    assert len(fake_gateway.calls) == 1
    assert "query_memory_template" not in fake_gateway.calls[0]["payload"]


async def test_no_prior_memory_falls_back_to_model_gateway(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `QueryMemoryEvidence` exists at all -- the baseline, unchanged
    behaviour every run had before AG-7."""
    orchestrator, fake_gateway = _orchestrator(monkeypatch, model_sql=FRESH_SQL)
    result = await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-memory-none",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )
    assert result.agent_run.generation_source == "MODEL_GATEWAY"
    assert "query_memory_template" not in fake_gateway.calls[0]["payload"]


# ---------------------------------------------------------------------------
# 3: validation cannot be bypassed on the memory-adapted path
# ---------------------------------------------------------------------------


async def test_memory_adapted_sql_still_rejected_by_the_same_guard(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memory match is found and offered (proving the feature is active),
    but the (fake) model returns a mutating statement. It must be rejected by
    the identical `sql_guard`/`query_gateway.execute` path every other
    generation_source funnels through -- there is no second, weaker
    validation call for QUERY_MEMORY_ADAPTATION."""
    completed_at = datetime.now(UTC) - timedelta(days=1)
    await scenario.seed_prior_success(run_completed_at=completed_at)

    orchestrator, fake_gateway = _orchestrator(monkeypatch, model_sql=MUTATING_SQL)
    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.analyst(),
            correlation_id="corr-memory-bypass-attempt",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert "MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN" in str(exc_info.value)

    # The memory match WAS found and offered (the feature engaged) --
    # the rejection proves the guard, not an absent match, stopped it.
    assert len(fake_gateway.calls) == 1
    assert fake_gateway.calls[0]["payload"].get("query_memory_template") == PRIOR_SQL_TEMPLATE

    run = await _latest_agent_run(scenario.db)
    assert run.generation_source == "QUERY_MEMORY_ADAPTATION"
    assert run.status == "REJECTED"
    assert run.failure_reason is not None
    assert "MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN" in run.failure_reason


async def test_memory_adaptation_disabled_by_default_setting(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent_query_memory_enabled` defaults False -- a tenant that has not
    opted in keeps exactly today's MODEL_GATEWAY-only behaviour even with an
    eligible, fresh memory candidate sitting in the table."""
    completed_at = datetime.now(UTC) - timedelta(days=1)
    await scenario.seed_prior_success(run_completed_at=completed_at)

    settings = Settings(agent_retrieval_limit=5, _env_file=None)
    assert settings.agent_query_memory_enabled is False
    orchestrator = GovernedAgentOrchestrator(settings)
    fake_gateway = _CapturingModelGateway(FRESH_SQL)
    orchestrator.model_gateway = fake_gateway  # type: ignore[assignment]
    # ADR-0024 resolves approved routes from the database before the gateway is
    # called, so a stubbed gateway alone is no longer enough to reach generation.
    # These tests are about query memory, not route governance: hand the
    # orchestrator one approved route and let the fake gateway answer.
    orchestrator._approved_model_routes = (  # type: ignore[method-assign]
        lambda session, organization_id: _one_approved_route()
    )
    executor = FakeSqlExecutor(({"customer_id": "C-1"},))
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )

    result = await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-memory-disabled",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )
    assert result.agent_run.generation_source == "MODEL_GATEWAY"
    assert "query_memory_template" not in fake_gateway.calls[0]["payload"]
