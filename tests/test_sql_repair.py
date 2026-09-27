"""R11-MP05: one bounded repair of a generated statement, before execution.

A generated statement that the guard or catalog refuses used to end the run at
the execute stage. Now, when every blocking finding is one a model can fix --
a parse error, an unknown table or column, a wildcard, an unbounded join -- the
model is asked once more, with its own rejected statement and the findings.
A statement refused for a security or boundary reason is never offered back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
import aida.query_gateway as query_gateway_module
from aida.agent_orchestrator import REPAIRABLE_FINDING_CODES, GovernedAgentOrchestrator
from aida.agent_runtime import RuntimeState
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    ModelCallEvidence,
    ModelGatewayError,
    SqlGenerationOutput,
)
from aida.models import AgentRun, DataSource
from aida.orchestration_stages import (
    GenerationInputs,
    OrchestrationRequest,
    RunLedger,
    ValidatedStatement,
)
from aida.query_gateway import QueryExecutionGateway
from aida.sql_validation import SqlFinding
from tests.support.doubles import security_context

BAD_SQL = "SELECT acount_id FROM retail.account"
GOOD_SQL = "SELECT account_id FROM retail.account"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


def _datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="repair-fixture",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://repair",
        status="ACTIVE",
    )


def _request() -> OrchestrationRequest:
    datasource = _datasource()
    return OrchestrationRequest(
        datasource=datasource,
        context=security_context(organization_id=datasource.organization_id),
        correlation_id="c1",
        question="how many accounts",
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
    )


def _ledger() -> RunLedger:
    run = AgentRun(
        id=uuid4(),
        organization_id=uuid4(),
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


def _route() -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key="primary",
        provider_type="OPENAI",
        model_id="gpt-test",
        endpoint_alias="",
        credential_reference="env://OPENAI_API_KEY",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


def _statement(sql: str = BAD_SQL, *, with_inputs: bool = True) -> ValidatedStatement:
    return ValidatedStatement(
        sql=sql,
        generation_source="MODEL_GATEWAY",
        generation_inputs=(
            GenerationInputs(
                system_instruction="Return one SELECT.",
                payload={"question": "how many accounts", "metadata_context": {"t": 1}},
                approved_routes=(_route(),),
            )
            if with_inputs
            else None
        ),
    )


def _finding(code: str, ref: str | None = None) -> SqlFinding:
    return SqlFinding(code=code, severity="ERROR", ref=ref, hint=f"hint for {code}")


class _Findings:
    """`structural_findings` double: answers per SQL text."""

    def __init__(self, by_sql: dict[str, list[SqlFinding]]) -> None:
        self.by_sql = by_sql
        self.calls: list[str] = []

    async def __call__(self, session: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs["sql"])
        findings = self.by_sql.get(kwargs["sql"], [])
        return SimpleNamespace(blocking_findings=tuple(findings))


class _Model:
    def __init__(self, answers: list[str | Exception]) -> None:
        self.answers = list(answers)
        self.payloads: list[dict[str, Any]] = []
        self.instructions: list[str] = []

    async def structured_completion(self, **kwargs: Any) -> Any:
        self.payloads.append(kwargs["payload"])
        self.instructions.append(kwargs["system_instruction"])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return (
            SqlGenerationOutput(sql=answer, confidence=0.8, rationale_codes=["REPAIR"]),
            ModelCallEvidence(
                route="primary",
                provider_type="OPENAI",
                model_id="gpt-test",
                endpoint_alias="",
                input_fingerprint="in",
                output_fingerprint="out",
                input_size_bytes=10,
                output_size_bytes=10,
                schema_name="SqlGenerationOutput",
                estimated_input_tokens=50,
                estimated_output_tokens=10,
            ),
        )


def _orchestrator(findings: _Findings, model: _Model, **settings: Any) -> GovernedAgentOrchestrator:
    orchestrator = GovernedAgentOrchestrator(
        Settings(
            environment="test",
            identity_provider="development",
            model_generation_enabled=True,
            model_route="primary",
            model_route_breaker_failure_threshold=0,
            _env_file=None,
            **settings,
        )
    )
    orchestrator.query_gateway.structural_findings = findings  # type: ignore[method-assign]
    orchestrator.model_gateway = model  # type: ignore[assignment]
    return orchestrator


_SCREENED = SimpleNamespace(agent_contract=None)
_RETRIEVED = SimpleNamespace(context_product_scope=None)


async def _repair(
    orchestrator: GovernedAgentOrchestrator,
    session: AsyncSession,
    statement: ValidatedStatement,
    ledger: RunLedger | None = None,
) -> tuple[ValidatedStatement, RunLedger]:
    ledger = ledger or _ledger()
    repaired = await orchestrator._repair_generated_statement(
        session,
        _request(),
        ledger,
        _SCREENED,
        _RETRIEVED,
        statement,  # type: ignore[arg-type]
    )
    return repaired, ledger


@pytest.mark.asyncio
async def test_an_unknown_column_is_repaired_once_with_the_findings(
    session: AsyncSession,
) -> None:
    findings = _Findings({BAD_SQL: [_finding("UNKNOWN_COLUMN", "acount_id")]})
    model = _Model([GOOD_SQL])
    repaired, ledger = await _repair(_orchestrator(findings, model), session, _statement())

    assert repaired.sql == GOOD_SQL
    assert repaired.generation_source == "MODEL_GATEWAY"
    sent = model.payloads[0]
    assert sent["rejected_sql"] == BAD_SQL
    assert sent["rejection_findings"] == [
        {"code": "UNKNOWN_COLUMN", "ref": "acount_id", "hint": "hint for UNKNOWN_COLUMN"}
    ]
    # The first call's grounding is resent unchanged.
    assert sent["metadata_context"] == {"t": 1}
    assert "rejected_sql" in model.instructions[0]
    repair = ledger.plan_evidence["sql_repair"]
    assert repair["attempts"][0]["findings"] == ["UNKNOWN_COLUMN"]
    assert repair["attempts"][0]["result"] == "VALID"
    # The repair's tokens join the run's charge rather than replacing it.
    assert ledger.plan_evidence["budget_evidence"]["charged_tokens"] == 120 + 60
    assert ledger.agent_run.estimated_input_tokens == 150


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "codes",
    [
        ["MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN"],
        ["FORBIDDEN_FUNCTION"],
        ["CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE"],
        ["COST_CEILING_EXCEEDED"],
        # One unfixable finding is enough to withhold the repair.
        ["UNKNOWN_COLUMN", "SELECT_INTO_FORBIDDEN"],
    ],
)
async def test_a_security_or_boundary_refusal_is_never_offered_back(
    session: AsyncSession, codes: list[str]
) -> None:
    findings = _Findings({BAD_SQL: [_finding(code) for code in codes]})
    model = _Model([])
    repaired, ledger = await _repair(_orchestrator(findings, model), session, _statement())
    assert repaired.sql == BAD_SQL
    assert model.payloads == []
    assert "sql_repair" not in ledger.plan_evidence


@pytest.mark.asyncio
async def test_a_valid_statement_costs_no_model_call(session: AsyncSession) -> None:
    findings = _Findings({})
    model = _Model([])
    repaired, ledger = await _repair(_orchestrator(findings, model), session, _statement(GOOD_SQL))
    assert repaired.sql == GOOD_SQL
    assert model.payloads == []
    assert "sql_repair" not in ledger.plan_evidence


@pytest.mark.asyncio
async def test_a_failed_repair_call_keeps_the_original_statement(session: AsyncSession) -> None:
    findings = _Findings({BAD_SQL: [_finding("SQL_PARSE_ERROR")]})
    model = _Model([ModelGatewayError("down", provider_status_code=503)])
    repaired, ledger = await _repair(_orchestrator(findings, model), session, _statement())
    assert repaired.sql == BAD_SQL
    attempt = ledger.plan_evidence["sql_repair"]["attempts"][0]
    assert attempt["result"] == "REPAIR_CALL_FAILED"
    assert attempt["error_class"] == "ModelGatewayError"


@pytest.mark.asyncio
async def test_a_repair_that_is_still_refused_says_so(session: AsyncSession) -> None:
    worse = "SELECT * FROM retail.account"
    findings = _Findings(
        {
            BAD_SQL: [_finding("UNKNOWN_COLUMN", "acount_id")],
            worse: [_finding("SELECT_WILDCARD_FORBIDDEN")],
        }
    )
    model = _Model([worse])
    repaired, ledger = await _repair(_orchestrator(findings, model), session, _statement())
    # Handed on as it is: the execute stage refuses it on its own merits.
    assert repaired.sql == worse
    assert ledger.plan_evidence["sql_repair"]["attempts"][-1]["result"] == "STILL_REFUSED"
    assert len(model.payloads) == 1


@pytest.mark.asyncio
async def test_two_attempts_when_configured(session: AsyncSession) -> None:
    middle = "SELECT acct_id FROM retail.account"
    findings = _Findings(
        {
            BAD_SQL: [_finding("UNKNOWN_COLUMN", "acount_id")],
            middle: [_finding("UNKNOWN_COLUMN", "acct_id")],
        }
    )
    model = _Model([middle, GOOD_SQL])
    repaired, ledger = await _repair(
        _orchestrator(findings, model, agent_sql_repair_attempts=2), session, _statement()
    )
    assert repaired.sql == GOOD_SQL
    assert [a["result"] for a in ledger.plan_evidence["sql_repair"]["attempts"]] == [
        "REPAIRED_UNCHECKED",
        "VALID",
    ]
    assert model.payloads[1]["rejected_sql"] == middle


@pytest.mark.asyncio
async def test_zero_attempts_checks_nothing(session: AsyncSession) -> None:
    findings = _Findings({BAD_SQL: [_finding("UNKNOWN_COLUMN")]})
    model = _Model([])
    repaired, _ledger_ = await _repair(
        _orchestrator(findings, model, agent_sql_repair_attempts=0), session, _statement()
    )
    assert repaired.sql == BAD_SQL
    assert findings.calls == []


@pytest.mark.asyncio
async def test_a_statement_not_from_a_model_is_left_alone(session: AsyncSession) -> None:
    findings = _Findings({BAD_SQL: [_finding("UNKNOWN_COLUMN")]})
    model = _Model([])
    repaired, _ledger_ = await _repair(
        _orchestrator(findings, model), session, _statement(with_inputs=False)
    )
    assert repaired.sql == BAD_SQL
    assert findings.calls == []


def test_only_structural_codes_are_repairable() -> None:
    assert REPAIRABLE_FINDING_CODES == {
        "SQL_PARSE_ERROR",
        "EXACTLY_ONE_STATEMENT_REQUIRED",
        "CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN",
        "SELECT_WILDCARD_FORBIDDEN",
        "UNKNOWN_OR_UNAUTHORIZED_TABLE",
        "UNKNOWN_COLUMN",
    }


# ---------------------------------------------------------------------------
# The gateway's structural check never opens a connector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structural_findings_reads_the_catalog_and_opens_no_connector(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_connector(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("structural_findings opened a connector")

    monkeypatch.setattr(query_gateway_module, "open_execution_session", _no_connector)
    gateway = QueryExecutionGateway(Settings(_env_file=None))

    async def _allowed(*_args: Any) -> set[str]:
        return {"retail.account"}

    async def _columns(*_args: Any) -> dict[str, frozenset[str]]:
        return {"retail.account": frozenset({"account_id"})}

    async def _routines(*_args: Any) -> set[str]:
        return set()

    monkeypatch.setattr(gateway, "allowed_tables", _allowed)
    monkeypatch.setattr(gateway, "_catalog_columns", _columns)
    monkeypatch.setattr(gateway, "declared_routine_names", _routines)
    datasource = _datasource()

    clean = await gateway.structural_findings(
        session, datasource=datasource, sql=GOOD_SQL, requested_limit=None
    )
    assert not clean.blocking_findings

    wrong = await gateway.structural_findings(
        session, datasource=datasource, sql=BAD_SQL, requested_limit=None
    )
    assert {finding.code for finding in wrong.blocking_findings} == {"UNKNOWN_COLUMN"}
