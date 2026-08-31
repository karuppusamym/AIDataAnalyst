"""DQ-3 / TL-3 / AG-6 / RT-7: quality-runtime coupling actually wired in.

`quality_coupling.py` and `trust_scoring.py` were real, unit-tested modules
with zero call sites anywhere else in `src/aida` -- `check_tool_gate` gated
nothing, no trust warning was ever emitted, and the trust factor never
entered ranking (see `Docs/60-delivery/04-end-to-end-audit-2026-08-30.md`
section 2). This file proves the two real wiring points landed this wave:

  1. TL-3 (tool gating): `tool_api.py::execute_tool` -- the real governed-tool
     execution endpoint -- calls `quality_coupling.check_tool_gate` against
     the tool's own declared `referenced_tables`, resolved to this
     datasource's `MetadataTable` rows and checked against real
     `DataQualityIncident` rows, *before* SQL is rendered or a warehouse is
     touched. A CRITICAL open incident on a dependency genuinely blocks
     execution (HTTP 409, no `ToolExecution` row, a DENIED audit row); a
     WARNING incident allows execution through but the response's
     `quality_gate` carries the warning; no open incident leaves it null.

  2. AG-6 (answer trust warnings): `agent_orchestrator.py`'s
     `GovernedAgentOrchestrator.run` resolves the tables the *executed*
     query actually touched (`gateway_result.execution.referenced_tables`)
     against open incidents, and when any exist it computes a real
     `trust_scoring.compute_trust_score` and folds both the composite score
     and `quality_coupling.get_trust_warning` messages into
     `agent_run.plan_evidence["trust"]` (returned to the caller as
     `AgentAnalysisResponse.plan_evidence`) and into the deterministic
     explanation string itself -- a visible warning, not a buried one. As of
     C3 (`GovernedAgentOrchestrator._checkpoint_explained`), a WARNING
     incident still only warns, but a CRITICAL incident now genuinely
     refuses the run -- the same `check_quality_gate` BLOCK action TL-3 has
     always used to stop a governed tool before it runs, closing the gap
     where a model-generated or development-override answer could surface
     data from a critically incident-affected table with nothing stronger
     than a warning appended after the fact.

Both wiring points resolve incidents through the same
`quality_coupling.resolve_table_ids` / `fetch_open_incidents` helpers against
a real (in-memory sqlite) database seeded through the ORM, so this proves the
actual SQL joins line up with `DataQualityIncident.table_id`, not a
hand-simulated approximation of them.

RT-7 (ranking factor) is intentionally not exercised here: the live
retrieval/ranking path the orchestrator actually uses is
`agent_intelligence.GovernedRetriever` (simple lexical scoring wired into
`GovernedAgentOrchestrator.retriever`), not `retrieval.py::hybrid_retrieve` /
`fusion_ranking.py` -- confirmed by grepping `src/aida` for real (non-test,
non-docstring) callers of either and finding none outside their own modules.
Wiring the trust factor into a ranking path nothing actually calls would not
satisfy RT-7's exit criterion ("part of DQ-3"); see the tracker row and
`quality_coupling.py`'s module docstring for the deferral note.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from itertools import count
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.agent_orchestrator import GovernedAgentOrchestrator
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
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QueryExecution,
    ToolExecution,
)
from aida.query_gateway import GatewayResult, QueryExecutionGateway, QueryRejected
from aida.schemas import ToolExecutionRequest
from aida.tool_api import execute_tool
from tests.support.doubles import security_context

# `AuditEvent.id` is a `BigInteger` autoincrement PK relying in production on
# Postgres's own sequence; sqlite only auto-populates a bare `INTEGER PRIMARY
# KEY`. Same workaround as `test_kill_switch_drill.py` /
# `test_bulk_governance_decisions.py` / `test_relationship_intelligence_review.py`.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    """One organization, one datasource, one table -- the minimum needed to
    prove the coupling reads real `DataQualityIncident` rows through real
    SQL joins, seeded directly through the ORM."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Scenario":
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
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
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
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()
        return self

    async def incident(
        self, *, severity: str = "CRITICAL", status: str = "OPEN"
    ) -> DataQualityIncident:
        db = self.db
        incident = DataQualityIncident(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=self.table.id,
            fingerprint=f"fp-incident-{uuid4().hex[:8]}",
            anomaly_type="NULL_RATE_SHIFT",
            severity=severity,
            status=status,
            summary="Null rate spiked outside the governed baseline.",
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
        db.add(incident)
        await db.flush()
        return incident

    async def tool_version(self) -> GovernedToolVersion:
        db = self.db
        tool = GovernedTool(
            organization_id=self.organization.id,
            project_id=self.project.id,
            slug="fact-sales-lookup",
        )
        db.add(tool)
        await db.flush()
        version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=tool.id,
            version=1,
            status="PUBLISHED",
            name="Fact Sales Lookup",
            description="Reads fact_sales.",
            datasource_id=self.datasource.id,
            sql_template="SELECT 1 FROM finance.fact_sales",
            referenced_tables=["finance.fact_sales"],
            parameter_schema=[],
            allowed_roles=["Analyst"],
            fingerprint="fp-tool-v1",
            created_by="tool-maker",
        )
        db.add(version)
        await db.flush()
        return version

    async def completed_analysis(self) -> AnalysisRun:
        db = self.db
        run = AnalysisRun(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            status="COMPLETED",
        )
        db.add(run)
        await db.flush()
        return run

    def analyst(self):
        return security_context(organization_id=self.organization.id, roles=frozenset({"Analyst"}))


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def _fake_execute(
    self: QueryExecutionGateway,
    session: AsyncSession,
    *,
    datasource: DataSource,
    context,
    correlation_id: str,
    sql: str,
    requested_limit: int | None,
    semantic_version: str | None,
    workspace_id: UUID | None = None,
) -> GatewayResult:
    """Stands in for a real warehouse round-trip: gating/warning behaviour is
    what these tests prove, not `QueryExecutionGateway`'s own SQL execution
    (covered elsewhere). `row_count` is set to match `rows=()` -- a real
    `execute()` always sets it on every successful `QueryExecution` (C3's
    EXECUTED checkpoint independently re-verifies it is present and within
    bound), so leaving it `None` here would make this double describe an
    execution the real gateway never produces."""
    execution = QueryExecution(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        principal_id=context.principal_id,
        status="SUCCEEDED",
        dialect=datasource.dialect,
        sql_hash="fake-sql-hash",
        referenced_tables=["finance.fact_sales"],
        row_count=0,
    )
    session.add(execution)
    await session.flush()
    return GatewayResult(execution=execution, rows=(), masked_columns=())


# ---------------------------------------------------------------------------
# TL-3: tool gating actually blocks/warns at the real execution endpoint
# ---------------------------------------------------------------------------


async def test_execute_tool_blocks_when_dependency_has_critical_incident(
    scenario: _Scenario,
) -> None:
    version = await scenario.tool_version()
    await scenario.incident(severity="CRITICAL", status="OPEN")

    with pytest.raises(HTTPException) as exc_info:
        await execute_tool(
            version.id,
            ToolExecutionRequest(parameters={}),
            context=scenario.analyst(),
            session=scenario.db,
            settings=Settings(),
        )
    assert exc_info.value.status_code == 409
    assert "blocked" in exc_info.value.detail.lower()

    # Blocked before a ToolExecution row is ever created.
    executions = (await scenario.db.scalars(select(ToolExecution))).all()
    assert executions == []

    denied = (
        await scenario.db.scalars(
            select(AuditEvent).where(AuditEvent.outcome == "DENIED")
        )
    ).all()
    assert len(denied) == 1
    assert denied[0].details["reason"] == "QUALITY_INCIDENT_BLOCK"


async def test_execute_tool_warns_but_allows_on_a_warning_incident(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    version = await scenario.tool_version()
    await scenario.incident(severity="WARNING", status="OPEN")

    response = await execute_tool(
        version.id,
        ToolExecutionRequest(parameters={}),
        context=scenario.analyst(),
        session=scenario.db,
        settings=Settings(),
    )
    assert response.quality_gate is not None
    assert response.quality_gate["action"] == "WARN"
    assert str(scenario.table.id) in response.quality_gate["affected_assets"]

    executions = (await scenario.db.scalars(select(ToolExecution))).all()
    assert len(executions) == 1
    assert executions[0].status == "COMPLETED"


async def test_execute_tool_allows_cleanly_with_no_open_incidents(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    version = await scenario.tool_version()

    response = await execute_tool(
        version.id,
        ToolExecutionRequest(parameters={}),
        context=scenario.analyst(),
        session=scenario.db,
        settings=Settings(),
    )
    assert response.quality_gate is None


async def test_execute_tool_allows_when_incident_is_resolved(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RESOLVED incident must not gate -- only OPEN/ACKNOWLEDGED do."""
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    version = await scenario.tool_version()
    await scenario.incident(severity="CRITICAL", status="RESOLVED")

    response = await execute_tool(
        version.id,
        ToolExecutionRequest(parameters={}),
        context=scenario.analyst(),
        session=scenario.db,
        settings=Settings(),
    )
    assert response.quality_gate is None


# ---------------------------------------------------------------------------
# AG-6: answer trust warnings actually surface from the real orchestrator run
# ---------------------------------------------------------------------------


async def test_run_surfaces_trust_warning_when_answer_touches_flagged_table(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WARNING-severity incident still only warns -- `check_quality_gate`'s
    `gate_action` is `DEMOTE`/`WARN`, not `BLOCK`, so C3's EXPLAINED
    checkpoint lets the run complete. See
    `test_run_blocks_when_answer_touches_table_with_critical_incident` for
    the CRITICAL case, which now refuses instead of merely warning."""
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    await scenario.completed_analysis()
    await scenario.incident(severity="WARNING", status="OPEN")

    orchestrator = GovernedAgentOrchestrator(Settings())
    result = await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-trust-1",
        question="What is total revenue for fact_sales this quarter?",
        candidate_sql="SELECT 1 FROM finance.fact_sales",
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )

    trust = result.agent_run.plan_evidence.get("trust")
    assert trust is not None
    assert trust["warnings"], "expected at least one trust warning"
    assert trust["trust_score"] < 100
    assert "TRUST WARNING" in result.explanation


async def test_run_blocks_when_answer_touches_table_with_critical_incident(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3's EXPLAINED checkpoint: a CRITICAL open incident on a table the
    answer actually came from now refuses the run via `check_quality_gate`'s
    `BLOCK` action -- the same gate `test_execute_tool_blocks_when_dependency_has_critical_incident`
    already proves for TL-3's tool-gating path. Before this change, AG-6 only
    ever warned; this is the new deny path."""
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    await scenario.completed_analysis()
    await scenario.incident(severity="CRITICAL", status="OPEN")

    orchestrator = GovernedAgentOrchestrator(Settings())
    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            scenario.db,
            datasource=scenario.datasource,
            context=scenario.analyst(),
            correlation_id="corr-trust-block",
            question="What is total revenue for fact_sales this quarter?",
            candidate_sql="SELECT 1 FROM finance.fact_sales",
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert "EXPLAINED_QUALITY_INCIDENT_BLOCK" in str(exc_info.value)

    run = (
        await scenario.db.execute(select(AgentRun).order_by(AgentRun.created_at.desc()))
    ).scalars().first()
    assert run is not None
    assert run.status == "FAILED"
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("EXPLAINED_QUALITY_INCIDENT_BLOCK")


async def test_run_has_no_trust_warning_with_no_open_incidents(
    scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    await scenario.completed_analysis()

    orchestrator = GovernedAgentOrchestrator(Settings())
    result = await orchestrator.run(
        scenario.db,
        datasource=scenario.datasource,
        context=scenario.analyst(),
        correlation_id="corr-trust-2",
        question="What is total revenue for fact_sales this quarter?",
        candidate_sql="SELECT 1 FROM finance.fact_sales",
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )

    assert "trust" not in result.agent_run.plan_evidence
    assert "TRUST WARNING" not in result.explanation
