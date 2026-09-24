"""R11-MP04: a per-route circuit breaker in the model fallback chain.

Without it, every question during a provider outage first waits on the broken
primary before the fallback answers. These tests pin the breaker's three
states, which failures count, and how the fallback loop records a skipped
route -- and that the breaker never adds a route the approved list lacks.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    KillSwitchEngaged,
    ModelCallEvidence,
    ModelGatewayError,
    ModelOutputInvalid,
    SqlGenerationOutput,
)
from aida.model_route_breaker import RouteCircuitBreaker, is_route_failure

ORG = UUID("00000000-0000-0000-0000-00000000a001")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# The breaker on its own
# ---------------------------------------------------------------------------


def test_the_breaker_opens_after_the_threshold_and_not_before() -> None:
    breaker = RouteCircuitBreaker(_Clock())
    assert not breaker.record_failure(ORG, "primary", failure_threshold=3)
    assert not breaker.record_failure(ORG, "primary", failure_threshold=3)
    assert breaker.seconds_until_retry(ORG, "primary", cooldown_seconds=60) is None
    assert breaker.record_failure(ORG, "primary", failure_threshold=3)
    assert breaker.seconds_until_retry(ORG, "primary", cooldown_seconds=60) == 60


def test_a_success_resets_the_count() -> None:
    breaker = RouteCircuitBreaker(_Clock())
    breaker.record_failure(ORG, "primary", failure_threshold=3)
    breaker.record_failure(ORG, "primary", failure_threshold=3)
    breaker.record_success(ORG, "primary")
    assert not breaker.record_failure(ORG, "primary", failure_threshold=3)


def test_half_open_allows_one_trial_and_a_failure_reopens_at_once() -> None:
    clock = _Clock()
    breaker = RouteCircuitBreaker(clock)
    for _ in range(3):
        breaker.record_failure(ORG, "primary", failure_threshold=3)
    clock.now += 61
    assert breaker.seconds_until_retry(ORG, "primary", cooldown_seconds=60) is None
    # One failure in half-open reopens; it does not need the threshold again.
    assert breaker.record_failure(ORG, "primary", failure_threshold=3)
    assert breaker.seconds_until_retry(ORG, "primary", cooldown_seconds=60) == 60


def test_state_is_per_organization_and_per_route() -> None:
    breaker = RouteCircuitBreaker(_Clock())
    other_org = uuid4()
    for _ in range(3):
        breaker.record_failure(ORG, "primary", failure_threshold=3)
    assert breaker.seconds_until_retry(other_org, "primary", cooldown_seconds=60) is None
    assert breaker.seconds_until_retry(ORG, "fallback", cooldown_seconds=60) is None


@pytest.mark.parametrize(
    ("status", "generic", "counts"),
    [
        (429, False, True),
        (503, False, True),
        (404, False, True),
        (None, True, True),  # timeout or network failure
        (400, False, False),
        (401, False, False),
        (None, False, False),  # invalid output, kill switch, route not approved
    ],
)
def test_only_failures_about_the_route_count(
    status: int | None, generic: bool, counts: bool
) -> None:
    assert is_route_failure(status, generic_gateway_error=generic) is counts


# ---------------------------------------------------------------------------
# In the fallback loop
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


def _route(key: str) -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key=key,
        provider_type="OPENAI",
        model_id="test-model",
        endpoint_alias="",
        credential_reference="env://OPENAI_API_KEY",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


def _answer(route_key: str) -> tuple[SqlGenerationOutput, ModelCallEvidence]:
    return (
        SqlGenerationOutput(sql="SELECT 1", confidence=0.9, rationale_codes=["FAKE"]),
        ModelCallEvidence(
            route=route_key,
            provider_type="OPENAI",
            model_id="test-model",
            endpoint_alias="",
            input_fingerprint="in",
            output_fingerprint="out",
            input_size_bytes=1,
            output_size_bytes=1,
            schema_name="SqlGenerationOutput",
        ),
    )


class _ByRouteGateway:
    """Answers per route: a route in `failing` raises its exception, any other
    route answers. Records which routes were actually called."""

    def __init__(self, failing: dict[str, Exception]) -> None:
        self.failing = failing
        self.called: list[str] = []

    async def structured_completion(self, **kwargs: Any) -> Any:
        key = kwargs["route"].route_key
        self.called.append(key)
        if key in self.failing:
            raise self.failing[key]
        return _answer(key)


def _orchestrator(gateway: _ByRouteGateway, **settings: Any) -> GovernedAgentOrchestrator:
    orchestrator = GovernedAgentOrchestrator(
        Settings(
            environment="test",
            identity_provider="development",
            model_generation_enabled=True,
            model_route="primary",
            model_route_fallbacks="fallback",
            _env_file=None,
            **settings,
        )
    )
    orchestrator.model_gateway = gateway  # type: ignore[assignment]
    orchestrator.route_breaker = RouteCircuitBreaker(_Clock())
    return orchestrator


async def _run(orchestrator: GovernedAgentOrchestrator, session: AsyncSession) -> Any:
    return await orchestrator._generate_with_fallback(
        session=session,
        organization_id=ORG,
        approved_routes=[_route("primary"), _route("fallback")],
        system_instruction="ignored",
        payload={},
    )


@pytest.mark.asyncio
async def test_an_open_primary_is_skipped_without_a_call(session: AsyncSession) -> None:
    gateway = _ByRouteGateway({"primary": ModelGatewayError("down", provider_status_code=503)})
    orchestrator = _orchestrator(gateway)
    for _ in range(3):
        await _run(orchestrator, session)
    assert gateway.called == ["primary", "fallback"] * 3

    gateway.called.clear()
    _output, evidence, attempts = await _run(orchestrator, session)
    assert gateway.called == ["fallback"]
    assert evidence.route == "fallback"
    assert attempts[0]["outcome"] == "SKIPPED_CIRCUIT_OPEN"
    assert attempts[0]["retry_in_seconds"] == 60
    assert attempts[1]["outcome"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_the_attempt_that_opens_the_breaker_says_so(session: AsyncSession) -> None:
    gateway = _ByRouteGateway({"primary": ModelGatewayError("down", provider_status_code=429)})
    orchestrator = _orchestrator(gateway)
    runs = [await _run(orchestrator, session) for _ in range(3)]
    assert "circuit_opened" not in runs[1][2][0]
    assert runs[2][2][0]["circuit_opened"] is True


@pytest.mark.asyncio
async def test_a_timeout_falls_back_and_counts_toward_the_breaker(session: AsyncSession) -> None:
    gateway = _ByRouteGateway({"primary": ModelGatewayError("model route timed out")})
    orchestrator = _orchestrator(gateway)
    _output, evidence, attempts = await _run(orchestrator, session)
    assert evidence.route == "fallback"
    assert [a["outcome"] for a in attempts] == ["FAILED", "SUCCEEDED"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ModelGatewayError("bad request", provider_status_code=400),
        ModelOutputInvalid("model output failed its structured contract"),
        KillSwitchEngaged("kill switch engaged (organization-wide): drill"),
    ],
)
async def test_request_failures_neither_fall_back_nor_open_the_breaker(
    session: AsyncSession, error: ModelGatewayError
) -> None:
    gateway = _ByRouteGateway({"primary": error})
    orchestrator = _orchestrator(gateway)
    for _ in range(4):
        with pytest.raises(type(error)):
            await _run(orchestrator, session)
    # Every run still tried the primary: nothing opened.
    assert gateway.called == ["primary"] * 4


@pytest.mark.asyncio
async def test_every_route_open_is_a_503_with_the_chain(session: AsyncSession) -> None:
    down = ModelGatewayError("down", provider_status_code=503)
    gateway = _ByRouteGateway({"primary": down, "fallback": down})
    orchestrator = _orchestrator(gateway)
    for _ in range(3):
        with pytest.raises(ModelGatewayError):
            await _run(orchestrator, session)

    gateway.called.clear()
    with pytest.raises(ModelGatewayError, match="cooling down") as raised:
        await _run(orchestrator, session)
    assert gateway.called == []
    assert raised.value.provider_status_code == 503
    outcomes = [a["outcome"] for a in raised.value.model_call_attempts]  # type: ignore[attr-defined]
    assert outcomes == ["SKIPPED_CIRCUIT_OPEN", "SKIPPED_CIRCUIT_OPEN"]


@pytest.mark.asyncio
async def test_a_zero_threshold_turns_the_breaker_off(session: AsyncSession) -> None:
    gateway = _ByRouteGateway({"primary": ModelGatewayError("down", provider_status_code=503)})
    orchestrator = _orchestrator(gateway, model_route_breaker_failure_threshold=0)
    for _ in range(5):
        await _run(orchestrator, session)
    assert gateway.called == ["primary", "fallback"] * 5
