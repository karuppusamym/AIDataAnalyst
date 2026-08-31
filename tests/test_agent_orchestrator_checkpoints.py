"""C3 -- the last five runtime states as five independently-gated checkpoints.

Before this file, `GovernedAgentOrchestrator.run` applied `VALIDATED`,
`COSTED`, `EXECUTED`, `EXPLAINED` and `COMPLETED` in a single `for` loop
*after* `QueryExecutionGateway.execute()` had already returned
(`Docs/20-modules/13-agent-runtime.md` C3): the work each state names was
real -- it happens inside the gateway -- but the orchestrator itself never
independently re-checked any of it and could not refuse on any of the five
in its own right.

Each test below drives the real orchestrator (`GovernedAgentOrchestrator.run`,
the handler behind `POST /v1/datasources/{id}/agent-analyses`) against a real
in-memory SQLite database, with a `FakeSqlExecutor` standing in only for the
external data-source connector -- the same harness
`test_agent_orchestrator_decision_lineage.py` uses. `test_happy_path_reaches_completed`
proves the shared fixture actually reaches `COMPLETED` end to end; every
other test engineers a failure specific to exactly one checkpoint, with the
other four conditions left exactly as the happy path leaves them, and asserts
the run refuses at that checkpoint and no other -- proving independence, not
just that the pipeline can fail somewhere.
"""

import itertools
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.connectors.base import QueryEstimate
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
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.query_gateway import QueryRejected
from tests.support.doubles import FakeSqlExecutor, security_context

pytestmark = pytest.mark.asyncio

# `AuditEvent.id` is a `BigInteger` autoincrement primary key that relies in
# production on Postgres's own sequence generation; sqlite only auto-populates
# a bare `INTEGER PRIMARY KEY` (its rowid alias), which `BigInteger` does not
# compile to, so an in-memory sqlite session leaves `id` NULL. Same workaround
# as `test_agent_orchestrator_decision_lineage.py`.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


QUESTION = "show account balance summary"


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


class _Fixture:
    def __init__(self, organization: Organization, datasource: DataSource, table_id: object):
        self.organization = organization
        self.datasource = datasource
        self.table_id = table_id


async def _seed(session: AsyncSession) -> _Fixture:
    """One datasource, one table, one governed tool that matches `QUESTION`.

    Deliberately simpler than `test_agent_orchestrator_decision_lineage.py`'s
    fixture (no competing tool, no retrieval-reject candidate): these tests
    are not about retrieval ranking, they are about what happens after
    `query_gateway.execute()` returns, so the fixture only needs to reach
    `COMPLETED` reliably.
    """
    organization = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=organization.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Retail Banking",
        code=f"RB{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core Banking",
        slug=f"core-banking-{uuid4().hex[:6]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="core-warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
    )
    session.add_all([organization, lob, domain, project, datasource])
    await session.flush()

    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=organization.id,
        datasource_id=datasource.id,
        name="core",
        fingerprint="fp-catalog",
    )
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization.id,
        catalog_id=catalog.id,
        name="retail",
        fingerprint="fp-schema",
    )
    ledger_table = MetadataTable(
        id=uuid4(),
        organization_id=organization.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="settlement_ledger",
        object_type="TABLE",
        fingerprint="fp-ledger",
        source_description="Immutable record of settled transfers",
    )
    session.add_all([catalog, schema, ledger_table])
    await session.flush()
    session.add_all(
        [
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=ledger_table.id,
                name="party_ref",
                ordinal_position=1,
                physical_type="TEXT",
                nullable=False,
                fingerprint="fp-col-party-ref",
            ),
            MetadataColumn(
                id=uuid4(),
                organization_id=organization.id,
                table_id=ledger_table.id,
                name="amount_value",
                ordinal_position=2,
                physical_type="NUMERIC",
                nullable=False,
                fingerprint="fp-col-amount-value",
            ),
        ]
    )

    # AU-5/C3's own exit criterion needs a COMPLETED analysis run for `run()`
    # to get past its "has metadata ever been analysed" gate.
    session.add(
        AnalysisRun(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            status="COMPLETED",
        )
    )

    tool = GovernedTool(
        id=uuid4(), organization_id=organization.id, project_id=project.id, slug="balance_tools"
    )
    session.add(tool)
    await session.flush()

    tool_a = GovernedToolVersion(
        id=uuid4(),
        organization_id=organization.id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Account Balance Summary",
        description="Approved governed account balance summary report",
        datasource_id=datasource.id,
        sql_template="SELECT party_ref, amount_value FROM retail.settlement_ledger",
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint="fp-tool-a",
        created_by="tool-dev",
    )
    session.add(tool_a)
    await session.commit()
    return _Fixture(organization, datasource, ledger_table.id)


def _orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: tuple[dict[str, object], ...] = ({"party_ref": "PARTY-1", "amount_value": 100},),
    estimate: QueryEstimate | None = None,
) -> GovernedAgentOrchestrator:
    settings = Settings(agent_retrieval_limit=5, _env_file=None)
    orchestrator = GovernedAgentOrchestrator(settings)
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
    return orchestrator


async def _latest_agent_run(session: AsyncSession) -> AgentRun:
    result = await session.execute(select(AgentRun).order_by(AgentRun.created_at.desc()))
    run = result.scalars().first()
    assert run is not None, "expected at least one AgentRun to have been created"
    return run


async def test_happy_path_reaches_completed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: the shared fixture, unmodified, reaches COMPLETED end to end.

    Every other test in this file breaks exactly one condition relative to
    this run. Without this baseline, a refusal in one of those tests could
    not be distinguished from "the fixture never worked in the first place".
    """
    fixture = await _seed(session)
    orchestrator = _orchestrator(monkeypatch)
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    result = await orchestrator.run(
        session,
        datasource=fixture.datasource,
        context=context,
        correlation_id="corr-happy-path",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )

    assert result.agent_run.status == "COMPLETED"
    assert result.agent_run.step_trace[-1]["stage"] == "COMPLETED"
    assert result.agent_run.step_trace[-1]["control_type"] == "CHECKPOINT_COMPLETED"
    # The five checkpoints all ran and recorded their own trace entry, in order.
    checkpoint_stages = [
        entry["stage"]
        for entry in result.agent_run.step_trace
        if "CHECKPOINT" in str(entry["control_type"])
    ]
    assert checkpoint_stages == ["VALIDATED", "COSTED", "EXECUTED", "EXPLAINED", "COMPLETED"]


async def test_validated_checkpoint_refuses_on_allowlist_drift(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VALIDATED refuses when its own, independent allowlist re-check disagrees
    with what the gateway actually executed against -- engineered here by
    making the *second* call to `allowed_tables` (the checkpoint's own call)
    return an empty set, while the *first* call (inside `execute()`) sees the
    real allowlist, so the query itself executes successfully. COSTED,
    EXECUTED and EXPLAINED are never reached, but the happy-path test above
    proves they would have passed against this same fixture and data.
    """
    fixture = await _seed(session)
    orchestrator = _orchestrator(monkeypatch)
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    real_allowed_tables = orchestrator.query_gateway.allowed_tables
    calls = {"n": 0}

    async def _drifting_allowed_tables(
        session_arg: AsyncSession, datasource_arg: DataSource
    ) -> set[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_allowed_tables(session_arg, datasource_arg)
        return set()

    monkeypatch.setattr(orchestrator.query_gateway, "allowed_tables", _drifting_allowed_tables)

    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            session,
            datasource=fixture.datasource,
            context=context,
            correlation_id="corr-validated",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert "VALIDATED_TABLE_NOT_ALLOWLISTED" in str(exc_info.value)
    assert calls["n"] == 2, "expected exactly one gateway-internal call and one checkpoint call"

    run = await _latest_agent_run(session)
    assert run.status == "REJECTED"
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("VALIDATED_TABLE_NOT_ALLOWLISTED")
    assert run.step_trace[-1]["stage"] == "REJECTED"
    assert run.step_trace[-1]["control_type"] == "CHECKPOINT_VALIDATED"
    # COSTED/EXECUTED/EXPLAINED/COMPLETED never ran.
    stages = [entry["stage"] for entry in run.step_trace]
    assert "COSTED" not in stages
    assert "EXECUTED" not in stages
    assert "EXPLAINED" not in stages
    assert "COMPLETED" not in stages


async def test_costed_checkpoint_refuses_on_invalid_plan_cost_evidence(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COSTED refuses on a negative plan-cost estimate.

    `gate_query_estimate` only checks an *upper* bound (`plan_cost >
    cost_limit`), so a negative estimate sails through the gateway's own
    cost gate untouched -- the query executes successfully. The COSTED
    checkpoint is the only thing that catches this: it independently
    verifies the persisted cost evidence is finite and non-negative before
    trusting it further downstream.
    """
    fixture = await _seed(session)
    orchestrator = _orchestrator(
        monkeypatch,
        estimate=QueryEstimate(score=-5.0, kind="EXPLAIN", estimated_rows=1.0),
    )
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            session,
            datasource=fixture.datasource,
            context=context,
            correlation_id="corr-costed",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert "COSTED_EVIDENCE_INVALID" in str(exc_info.value)

    run = await _latest_agent_run(session)
    assert run.status == "REJECTED"
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("COSTED_EVIDENCE_INVALID")
    assert run.step_trace[-1]["control_type"] == "CHECKPOINT_COSTED"
    stages = [entry["stage"] for entry in run.step_trace]
    assert "VALIDATED" in stages, "VALIDATED should have passed before COSTED ran"
    assert "EXECUTED" not in stages
    assert "EXPLAINED" not in stages
    assert "COMPLETED" not in stages


async def test_executed_checkpoint_refuses_when_row_count_exceeds_bound(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXECUTED refuses when more rows come back than the caller's requested
    bound allows. `FakeSqlExecutor` returns exactly the rows it is given
    regardless of the `LIMIT` clause `SqlGuard` embedded in the rendered SQL
    -- standing in for a source that does not honour its own `LIMIT` -- so
    this is the only checkpoint that catches an over-bound result.
    """
    fixture = await _seed(session)
    orchestrator = _orchestrator(
        monkeypatch,
        rows=(
            {"party_ref": "PARTY-1", "amount_value": 100},
            {"party_ref": "PARTY-2", "amount_value": 200},
        ),
    )
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            session,
            datasource=fixture.datasource,
            context=context,
            correlation_id="corr-executed",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=1,
        )
    assert "EXECUTED_ROW_COUNT_EXCEEDS_BOUND" in str(exc_info.value)

    run = await _latest_agent_run(session)
    assert run.status == "REJECTED"
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("EXECUTED_ROW_COUNT_EXCEEDS_BOUND")
    assert run.step_trace[-1]["control_type"] == "CHECKPOINT_EXECUTED"
    stages = [entry["stage"] for entry in run.step_trace]
    assert "VALIDATED" in stages
    assert "COSTED" in stages
    assert "EXPLAINED" not in stages
    assert "COMPLETED" not in stages


async def test_explained_checkpoint_refuses_on_critical_quality_incident(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXPLAINED refuses when the answer's own source table has an open
    CRITICAL quality incident -- new behaviour this change adds. Before, the
    same incident only produced a trust warning appended to the explanation
    text; the run still completed.
    """
    fixture = await _seed(session)
    session.add(
        DataQualityIncident(
            id=uuid4(),
            organization_id=fixture.organization.id,
            datasource_id=fixture.datasource.id,
            table_id=fixture.table_id,
            fingerprint="fp-incident-critical",
            anomaly_type="FRESHNESS",
            severity="CRITICAL",
            status="OPEN",
            summary="Ledger feed has not landed in 3 days",
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
    )
    await session.commit()
    orchestrator = _orchestrator(monkeypatch)
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            session,
            datasource=fixture.datasource,
            context=context,
            correlation_id="corr-explained",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert "EXPLAINED_QUALITY_INCIDENT_BLOCK" in str(exc_info.value)

    run = await _latest_agent_run(session)
    assert run.status == "FAILED", (
        "EXECUTED->FAILED is the only transition the state machine allows"
    )
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("EXPLAINED_QUALITY_INCIDENT_BLOCK")
    assert run.step_trace[-1]["control_type"] == "CHECKPOINT_EXPLAINED"
    stages = [entry["stage"] for entry in run.step_trace]
    assert "VALIDATED" in stages
    assert "COSTED" in stages
    assert "EXECUTED" in stages
    assert "COMPLETED" not in stages


async def test_completed_checkpoint_refuses_on_missing_evidence(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMPLETED refuses when required evidence is missing from the record
    it is about to persist as the system of record -- engineered by making
    the gateway's SQL-signing step return an empty hash, which does not
    otherwise affect execution (the hash is audit evidence, not a control
    input), so VALIDATED/COSTED/EXECUTED/EXPLAINED all still pass normally.
    """
    fixture = await _seed(session)
    orchestrator = _orchestrator(monkeypatch)
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    async def _blank_sign_sql(sql: str) -> str:
        return ""

    monkeypatch.setattr(orchestrator.query_gateway, "_sign_sql", _blank_sign_sql)

    with pytest.raises(QueryRejected) as exc_info:
        await orchestrator.run(
            session,
            datasource=fixture.datasource,
            context=context,
            correlation_id="corr-completed",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert "COMPLETED_EVIDENCE_MISSING:sql_hash" in str(exc_info.value)

    run = await _latest_agent_run(session)
    assert run.status == "FAILED", (
        "EXPLAINED->FAILED is the only transition the state machine allows"
    )
    assert run.failure_reason is not None
    assert run.failure_reason.startswith("COMPLETED_EVIDENCE_MISSING:sql_hash")
    assert run.step_trace[-1]["control_type"] == "CHECKPOINT_COMPLETED"
    stages = [entry["stage"] for entry in run.step_trace]
    assert "VALIDATED" in stages
    assert "COSTED" in stages
    assert "EXECUTED" in stages
    assert "EXPLAINED" in stages
