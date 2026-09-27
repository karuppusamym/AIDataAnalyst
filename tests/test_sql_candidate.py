"""R11-MP07: a second, independent statement from another approved route.

When `model_routes_by_purpose` names a SQL_CANDIDATE route, Ask asks it for its
own statement with the same instruction and payload, never executes it, and
records how far the two agree -- structurally, from the SQL alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.agent_runtime import RuntimeState
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    ModelCallEvidence,
    ModelGatewayError,
    SqlGenerationOutput,
)
from aida.models import AgentRun, DataSource, ModelRouteConfiguration
from aida.orchestration_stages import (
    GenerationInputs,
    OrchestrationRequest,
    RunLedger,
    ValidatedStatement,
)
from aida.sql_candidate_agreement import AgreementLevel, compare_candidates
from tests.support.doubles import security_context

PRIMARY = "SELECT account_id FROM retail.account WHERE status = 'OPEN'"

# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def test_case_whitespace_and_quoting_do_not_count_as_disagreement() -> None:
    other = "select  \"account_id\"\nfrom retail.ACCOUNT where STATUS = 'OPEN'"
    result = compare_candidates(PRIMARY, other, dialect="postgres")
    assert result.level is AgreementLevel.IDENTICAL
    assert (result.shared_tables, result.primary_only_tables, result.candidate_only_tables) == (
        1,
        0,
        0,
    )


def test_the_same_tables_and_columns_computed_differently_share_sources() -> None:
    other = "SELECT account_id FROM retail.account WHERE status IN ('OPEN')"
    result = compare_candidates(PRIMARY, other, dialect="postgres")
    assert result.level is AgreementLevel.SAME_SOURCES


def test_a_different_table_is_disagreement_and_is_counted() -> None:
    other = "SELECT a.account_id FROM retail.account a JOIN retail.customer c ON c.id = a.cid"
    result = compare_candidates(PRIMARY, other, dialect="postgres")
    assert result.level is AgreementLevel.DIFFERENT
    assert result.candidate_only_tables == 1


def test_a_cte_name_is_not_a_table() -> None:
    other = (
        "WITH open_accounts AS (SELECT account_id, status FROM retail.account) "
        "SELECT account_id FROM open_accounts WHERE status = 'OPEN'"
    )
    result = compare_candidates(PRIMARY, other, dialect="postgres")
    assert result.candidate_only_tables == 0
    assert result.level is AgreementLevel.SAME_SOURCES


def test_an_unparseable_candidate_is_named_as_such() -> None:
    result = compare_candidates(PRIMARY, "SELEC account_id FRM", dialect="postgres")
    assert result.level is AgreementLevel.UNPARSEABLE


def test_the_evidence_holds_no_identifiers_or_sql() -> None:
    evidence = compare_candidates(PRIMARY, PRIMARY, dialect="postgres").evidence()
    assert set(evidence) == {
        "level",
        "shared_tables",
        "primary_only_tables",
        "candidate_only_tables",
    }
    assert "account" not in str(evidence)


# ---------------------------------------------------------------------------
# In the orchestrator
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


ORG = uuid4()


def _request() -> OrchestrationRequest:
    datasource = DataSource(
        id=uuid4(),
        organization_id=ORG,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="candidate-fixture",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://candidate",
        status="ACTIVE",
    )
    return OrchestrationRequest(
        datasource=datasource,
        context=security_context(organization_id=ORG),
        correlation_id="c1",
        question="open accounts",
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )


def _ledger() -> RunLedger:
    run = AgentRun(
        id=uuid4(),
        organization_id=ORG,
        datasource_id=uuid4(),
        principal_id="p1",
        question_hash="0" * 64,
        generation_source="PENDING",
        estimated_input_tokens=100,
        estimated_output_tokens=20,
    )
    ledger = RunLedger(agent_run=run, state=RuntimeState(request_id=str(run.id)))
    ledger.plan_evidence["budget_evidence"] = {"charged_tokens": 120}
    return ledger


def _approved(key: str) -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key=key,
        provider_type="OPENAI",
        model_id="gpt-test",
        endpoint_alias="",
        credential_reference="env://OPENAI_API_KEY",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


def _statement() -> ValidatedStatement:
    return ValidatedStatement(
        sql=PRIMARY,
        generation_source="MODEL_GATEWAY",
        generation_inputs=GenerationInputs(
            system_instruction="Return one SELECT.",
            payload={"question": "open accounts", "metadata_context": {"t": 1}},
            approved_routes=(_approved("primary"),),
        ),
    )


def _row(org_id: UUID, key: str, capabilities: tuple[str, ...] = ("SQL_GENERATION",)) -> Any:
    return ModelRouteConfiguration(
        organization_id=org_id,
        route_key=key,
        version=1,
        status="APPROVED",
        display_name=f"Route {key}",
        provider_type="OPENAI",
        model_id="gpt-test",
        endpoint_alias="test-endpoint",
        credential_reference="env://OPENAI_API_KEY",
        data_residency="US",
        retention_policy="ZERO_RETENTION",
        capabilities=list(capabilities),
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
        fingerprint="a" * 64,
        created_by="maker",
        approved_by="checker",
        approved_at=datetime.now(UTC),
    )


class _Model:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def structured_completion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return (
            SqlGenerationOutput(sql=self.answer, confidence=0.7, rationale_codes=["C"]),
            ModelCallEvidence(
                route=kwargs["route"].route_key,
                provider_type="OPENAI",
                model_id="gpt-test",
                endpoint_alias="",
                input_fingerprint="in",
                output_fingerprint="candidate-out",
                input_size_bytes=10,
                output_size_bytes=10,
                schema_name="SqlGenerationOutput",
                estimated_input_tokens=40,
                estimated_output_tokens=10,
            ),
        )


def _orchestrator(model: _Model, **purposes: str) -> GovernedAgentOrchestrator:
    orchestrator = GovernedAgentOrchestrator(
        Settings(
            environment="test",
            identity_provider="development",
            model_generation_enabled=True,
            model_route="primary",
            model_routes_by_purpose=purposes,
            model_route_breaker_failure_threshold=0,
            _env_file=None,
        )
    )
    orchestrator.model_gateway = model  # type: ignore[assignment]
    return orchestrator


async def _compare(orchestrator: GovernedAgentOrchestrator, session: AsyncSession) -> RunLedger:
    ledger = _ledger()
    await orchestrator._compare_second_candidate(
        session,
        _request(),
        ledger,
        SimpleNamespace(agent_contract=None),  # type: ignore[arg-type]
        _statement(),
    )
    return ledger


@pytest.mark.asyncio
async def test_no_candidate_route_means_no_second_call(session: AsyncSession) -> None:
    model = _Model(PRIMARY)
    ledger = await _compare(_orchestrator(model), session)
    assert model.calls == []
    assert "sql_candidate" not in ledger.plan_evidence


@pytest.mark.asyncio
async def test_the_candidate_gets_the_same_request_and_its_agreement_is_recorded(
    session: AsyncSession,
) -> None:
    session.add(_row(ORG, "cheap-route"))
    await session.flush()
    model = _Model("SELECT account_id FROM retail.account WHERE status IN ('OPEN')")
    ledger = await _compare(_orchestrator(model, SQL_CANDIDATE="cheap-route"), session)

    assert [call["route"].route_key for call in model.calls] == ["cheap-route"]
    assert model.calls[0]["payload"] == {"question": "open accounts", "metadata_context": {"t": 1}}
    assert model.calls[0]["system_instruction"] == "Return one SELECT."
    record = ledger.plan_evidence["sql_candidate"]
    assert record["result"] == "COMPARED"
    assert record["agreement"]["level"] == "SAME_SOURCES"
    assert record["output_fingerprint"] == "candidate-out"
    assert "SELECT" not in str(record)
    # Charged to the run like any other generation.
    assert ledger.plan_evidence["budget_evidence"]["charged_tokens"] == 120 + 50


@pytest.mark.asyncio
async def test_an_unapproved_or_incapable_candidate_route_is_not_called(
    session: AsyncSession,
) -> None:
    session.add(_row(ORG, "classify-only", capabilities=("CLASSIFICATION",)))
    await session.flush()
    model = _Model(PRIMARY)
    ledger = await _compare(_orchestrator(model, SQL_CANDIDATE="classify-only"), session)
    assert model.calls == []
    assert ledger.plan_evidence["sql_candidate"]["result"] == "CANDIDATE_ROUTE_NOT_APPROVED"


@pytest.mark.asyncio
async def test_the_primary_is_not_asked_twice(session: AsyncSession) -> None:
    session.add(_row(ORG, "primary"))
    await session.flush()
    model = _Model(PRIMARY)
    ledger = await _compare(_orchestrator(model, SQL_CANDIDATE="primary"), session)
    assert model.calls == []
    assert ledger.plan_evidence["sql_candidate"]["result"] == "CANDIDATE_ROUTE_IS_THE_PRIMARY"


@pytest.mark.asyncio
async def test_a_failed_candidate_call_is_recorded_and_the_run_goes_on(
    session: AsyncSession,
) -> None:
    session.add(_row(ORG, "cheap-route"))
    await session.flush()
    model = _Model(ModelGatewayError("down", provider_status_code=503))
    ledger = await _compare(_orchestrator(model, SQL_CANDIDATE="cheap-route"), session)
    record = ledger.plan_evidence["sql_candidate"]
    assert record["result"] == "CANDIDATE_CALL_FAILED"
    assert record["error_class"] == "ModelGatewayError"


def test_the_candidate_route_is_a_selected_key_and_has_no_default() -> None:
    settings = Settings(
        model_route="primary", model_routes_by_purpose={"SQL_CANDIDATE": "cheap"}, _env_file=None
    )
    assert "cheap" in settings.selected_model_route_keys
    assert (
        Settings(model_route="primary", _env_file=None).model_routes_by_purpose.get("SQL_CANDIDATE")
        is None
    )
