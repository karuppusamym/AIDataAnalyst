"""ADR-0024 -- model-route fallback proven through a real
`GovernedAgentOrchestrator.run()` call, not just `_generate_with_fallback` in
isolation.

`tests/test_model_route_fallback.py` already proves the fallback loop's own
semantics (retryable vs non-retryable, attempt bookkeeping, chain-on-exception)
against the method directly. What's still unproven is that the loop is
actually wired into the live orchestrator: that a real run -- retrieval,
planning, generation, execution, persistence -- picks up two APPROVED
`ModelRouteConfiguration` rows, iterates them on a transient provider failure,
and lands the full attempt chain on `agent_run.plan_evidence.model_call_attempts`
in the database, not just in a return value.

Fixture note: this file duplicates a trimmed copy of `_seed` from
`tests/test_agent_orchestrator_decision_lineage.py` rather than importing it.
`tests/` has no `__init__.py` and no existing test file imports another
test module's private helpers -- so a plain `from tests.test_agent_orchestrator_
decision_lineage import _seed` would depend on pytest's rootdir/import-mode
behaving a specific way that nothing else in this suite exercises, on a device
with no working interpreter to actually confirm it works. Duplicating is also
the smaller diff here: this scenario needs no `GovernedTool` at all (a
governed-tool match would take `plan.strategy` down the `GOVERNED_TOOL`
branch, not the `MODEL_GENERATION` branch this feature lives on), so the copy
drops that half of the original fixture rather than seeding tools only to
dodge them with question wording.
"""

import itertools
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_orchestrator import GovernedAgentOrchestrator, ModelRouteUnavailable
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import ModelCallEvidence, ModelGatewayError, SqlGenerationOutput
from aida.models import (
    AgentRun,
    AnalysisRun,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ModelRouteConfiguration,
    Organization,
    Project,
)
from tests.support.doubles import FakeSqlExecutor, security_context

pytestmark = pytest.mark.asyncio

# Same sqlite-autoincrement workaround `test_agent_orchestrator_decision_lineage.py`
# and `test_token_revocation.py` use: `AuditEvent.id` is a Postgres-sequence
# `BigInteger` in production; sqlite only auto-populates a bare rowid-alias
# `INTEGER PRIMARY KEY`, which `BigInteger` doesn't compile to.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


QUESTION = "reconcile settlement ledger totals"
GENERATED_SQL = "SELECT party_ref, amount_value FROM retail.settlement_ledger"
PRIMARY_ROUTE_KEY = "openai-bank-sql"
FALLBACK_ROUTE_KEY = "gemini-bank-sql"


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
    def __init__(self, organization: Organization, datasource: DataSource):
        self.organization = organization
        self.datasource = datasource


async def _seed(session: AsyncSession) -> _Fixture:
    """Trimmed duplicate of `test_agent_orchestrator_decision_lineage._seed`:
    Organization/LOB/Domain/Project/DataSource/MetadataCatalog/Schema/Table/
    Column, plus the COMPLETED `AnalysisRun` `run()` requires to get past its
    "has metadata ever been analysed" gate. No `GovernedTool` -- this scenario
    needs `plan.strategy == "MODEL_GENERATION"`, which is exactly what a
    tool-free fixture with `candidate_sql=None` produces
    (`GovernedPlanner.plan` in `src/aida/agent_intelligence.py`)."""
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
    session.add(
        AnalysisRun(
            id=uuid4(),
            organization_id=organization.id,
            datasource_id=datasource.id,
            status="COMPLETED",
        )
    )
    await session.commit()
    return _Fixture(organization, datasource)


def _route_row(
    *, org_id: UUID, key: str, provider: str, model_id: str, status: str = "APPROVED"
) -> ModelRouteConfiguration:
    """Full column set of the real `ModelRouteConfiguration` model (see
    `src/aida/models.py`) -- `tests/test_ai_governance.py` is the other place
    this exact shape is exercised end to end."""
    return ModelRouteConfiguration(
        organization_id=org_id,
        route_key=key,
        version=1,
        status=status,
        display_name=f"{key} route",
        provider_type=provider,
        model_id=model_id,
        endpoint_alias="",
        credential_reference=f"env://{provider}_API_KEY",
        data_residency="US",
        retention_policy="ZERO_RETENTION",
        capabilities=["SQL_GENERATION"],
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
        fingerprint=f"fp-{key}",
        created_by="test-harness",
        approved_by="steward",
        approved_at=datetime.now(UTC),
    )


def _fake_evidence(route_key: str) -> ModelCallEvidence:
    return ModelCallEvidence(
        route=route_key,
        provider_type="fake",
        model_id="test-model",
        endpoint_alias="",
        input_fingerprint="fp-in",
        output_fingerprint="fp-out",
        input_size_bytes=1,
        output_size_bytes=1,
        schema_name="SqlGenerationOutput",
    )


def _fake_output() -> SqlGenerationOutput:
    return SqlGenerationOutput(
        sql=GENERATED_SQL, confidence=0.9, rationale_codes=["FAKE_GENERATION"]
    )


class _QueuedGateway:
    """Same shape as `test_model_route_fallback.py`'s `_QueuedGateway`: pops
    `responses` in call order (not keyed by route), so a "primary throttled,
    fallback answers" chain is reproduced deterministically through the real
    `structured_completion(...)` call signature `_generate_with_fallback`
    uses (session, organization_id, route, system_instruction, payload,
    output_schema)."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def structured_completion(self, **kwargs: Any) -> Any:
        self.calls.append(
            {
                "route_key": kwargs["route"].route_key,
                "provider_type": kwargs["route"].provider_type,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _orchestrator(
    monkeypatch: pytest.MonkeyPatch, *, gateway: _QueuedGateway
) -> GovernedAgentOrchestrator:
    settings = Settings(
        agent_retrieval_limit=5,
        model_generation_enabled=True,
        model_route=PRIMARY_ROUTE_KEY,
        model_route_fallbacks=FALLBACK_ROUTE_KEY,
        openai_api_key="test",
        gemini_api_key="test",
        _env_file=None,
    )
    orchestrator = GovernedAgentOrchestrator(settings)
    orchestrator.model_gateway = gateway  # type: ignore[assignment]
    executor = FakeSqlExecutor(
        ({"party_ref": "PARTY-1", "amount_value": 100},),
    )
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


async def _new_agent_run_id(session: AsyncSession, known: set[UUID]) -> UUID:
    all_ids = set((await session.execute(select(AgentRun.id))).scalars().all())
    new_ids = all_ids - known
    assert len(new_ids) == 1, f"expected exactly one new AgentRun, found {new_ids}"
    return new_ids.pop()


async def test_run_falls_back_to_secondary_route_and_persists_attempt_chain(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario ADR-0024 exists for, proven through the real orchestrator:
    primary route 429s, the approved fallback route answers, and the full
    attempt chain lands on the persisted `agent_run.plan_evidence` -- not just
    on the tuple `_generate_with_fallback` returns in-process."""
    fixture = await _seed(session)
    session.add_all(
        [
            _route_row(
                org_id=fixture.organization.id,
                key=PRIMARY_ROUTE_KEY,
                provider="OPENAI",
                model_id="gpt-4o-mini",
            ),
            _route_row(
                org_id=fixture.organization.id,
                key=FALLBACK_ROUTE_KEY,
                provider="GOOGLE_GEMINI",
                model_id="gemini-1.5-pro",
            ),
        ]
    )
    await session.commit()

    gateway = _QueuedGateway(
        [
            ModelGatewayError("primary 429", provider_status_code=429),
            (_fake_output(), _fake_evidence(FALLBACK_ROUTE_KEY)),
        ]
    )
    orchestrator = _orchestrator(monkeypatch, gateway=gateway)
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )

    result = await orchestrator.run(
        session,
        datasource=fixture.datasource,
        context=context,
        correlation_id="corr-fallback-success",
        question=QUESTION,
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )

    assert result.agent_run.status == "COMPLETED", (
        "the run did not complete; the fallback/attempt assertions below prove "
        "nothing unless the full retrieval + planning + generation + execution "
        "pipeline actually ran"
    )
    assert result.agent_run.generation_source == "MODEL_GATEWAY"
    # The route that actually answered -- the fallback, not the primary.
    assert result.agent_run.model_route == FALLBACK_ROUTE_KEY

    attempts = result.agent_run.plan_evidence["model_call_attempts"]
    assert [a["outcome"] for a in attempts] == ["FAILED", "SUCCEEDED"]
    assert [a["route_key"] for a in attempts] == [PRIMARY_ROUTE_KEY, FALLBACK_ROUTE_KEY]
    assert attempts[0]["provider_status_code"] == 429
    assert attempts[0]["attempt_ordinal"] == 1
    assert attempts[1]["attempt_ordinal"] == 2

    # Read back from the database, not just the in-memory result object --
    # the exit criterion this test exists for is that the attempt chain is
    # genuinely persisted, not merely returned up the call stack.
    reloaded = await session.get(AgentRun, result.agent_run.id)
    assert reloaded is not None
    assert reloaded.status == "COMPLETED"
    assert reloaded.model_route == FALLBACK_ROUTE_KEY
    reloaded_attempts = reloaded.plan_evidence["model_call_attempts"]
    assert [a["outcome"] for a in reloaded_attempts] == ["FAILED", "SUCCEEDED"]
    assert [a["route_key"] for a in reloaded_attempts] == [
        PRIMARY_ROUTE_KEY,
        FALLBACK_ROUTE_KEY,
    ]


async def test_run_both_routes_429_rejects_but_still_persists_attempt_chain(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the primary and its only approved fallback are throttled --
    `_generate_with_fallback` exhausts and the orchestrator converts that into
    `ModelRouteUnavailable`/a REJECTED run, but the two FAILED attempts must
    still reach `plan_evidence.model_call_attempts` in the database: an
    auditor investigating a refusal needs to see that a fallback WAS tried and
    also failed, not just that the primary did."""
    fixture = await _seed(session)
    session.add_all(
        [
            _route_row(
                org_id=fixture.organization.id,
                key=PRIMARY_ROUTE_KEY,
                provider="OPENAI",
                model_id="gpt-4o-mini",
            ),
            _route_row(
                org_id=fixture.organization.id,
                key=FALLBACK_ROUTE_KEY,
                provider="GOOGLE_GEMINI",
                model_id="gemini-1.5-pro",
            ),
        ]
    )
    await session.commit()

    gateway = _QueuedGateway(
        [
            ModelGatewayError("primary 429", provider_status_code=429),
            ModelGatewayError("fallback 429", provider_status_code=429),
        ]
    )
    orchestrator = _orchestrator(monkeypatch, gateway=gateway)
    context = security_context(
        organization_id=fixture.organization.id, roles=frozenset({"Analyst"})
    )
    known_run_ids = set((await session.execute(select(AgentRun.id))).scalars().all())

    with pytest.raises(ModelRouteUnavailable) as exc_info:
        await orchestrator.run(
            session,
            datasource=fixture.datasource,
            context=context,
            correlation_id="corr-fallback-exhausted",
            question=QUESTION,
            candidate_sql=None,
            preferred_tool_version_id=None,
            tool_parameters={},
            requested_limit=None,
        )
    assert exc_info.value.provider_status_code == 429

    run_id = await _new_agent_run_id(session, known_run_ids)
    run = await session.get(AgentRun, run_id)
    assert run is not None
    assert run.status == "REJECTED"
    assert run.failure_reason == "MODEL_ROUTE_NOT_CONFIGURED"

    attempts = run.plan_evidence["model_call_attempts"]
    assert [a["outcome"] for a in attempts] == ["FAILED", "FAILED"]
    assert [a["route_key"] for a in attempts] == [PRIMARY_ROUTE_KEY, FALLBACK_ROUTE_KEY]
    assert [a["provider_status_code"] for a in attempts] == [429, 429]
