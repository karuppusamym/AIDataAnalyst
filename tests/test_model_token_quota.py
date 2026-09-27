"""R11-MP14: the declared model-token quota refuses a call before it is made.

`model_token_daily_quota_per_organization` and `_per_datasource` existed and
nothing consumed them: spend was recorded after the fact for column drafting
only, and Ask's spend was not recorded at all. The gateway now reserves the
most a call may cost with `consume_quota` before anything is sent, settles the
windows to what was billed afterwards, and attributes every caller's spend.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.api as api_module
import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.agent_orchestrator import GovernedAgentOrchestrator
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    DeterministicTestProvider,
    ModelGatewayError,
    ModelQuotaExhausted,
    ProviderNeutralModelGateway,
    ProviderUsage,
    SqlGenerationOutput,
)
from aida.request_budget import BudgetDecision
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider
from aida.usage_quotas import (
    UsageDimension,
    consume_quota,
    settle_quota,
    source_usage,
    tenant_usage,
)
from tests.support.doubles import security_context

_ANSWER = {"sql": "SELECT 1", "confidence": 0.9, "rationale_codes": ["X"]}


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_generation_enabled": True,
        "model_route": "primary",
        "credential_provider": "vault",
        "model_max_output_tokens": 200,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _route() -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key="primary",
        provider_type="OPENAI",
        model_id="gpt-test",
        endpoint_alias="",
        credential_reference="vault://model-key",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


class _CountingProvider(DeterministicTestProvider):
    def __init__(self, response: Any, usage: ProviderUsage | None = None) -> None:
        super().__init__(response, usage)
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return await super().__call__(**kwargs)


def _gateway(settings: Settings, provider: _CountingProvider) -> ProviderNeutralModelGateway:
    resolver = SecretResolver(
        settings,
        {"vault": StaticTestSecretProvider({("model-key", None): ResolvedSecret("secret")})},
    )
    return ProviderNeutralModelGateway(settings, {"OPENAI": provider}, resolver)


async def _call(
    gateway: ProviderNeutralModelGateway, session: AsyncSession, org: UUID, ds: UUID | None
) -> Any:
    return await gateway.structured_completion(
        session=session,
        organization_id=org,
        route=_route(),
        system_instruction="Generate SQL",
        payload={"question": "q"},
        output_schema=SqlGenerationOutput,
        datasource_id=ds,
    )


async def _used(session: AsyncSession, org: UUID, ds: UUID | None = None) -> tuple[int, int]:
    # Read at call time, not import time: a long suite can cross UTC midnight
    # between collection and this test, and the windows are UTC days.
    today = dt.datetime.now(dt.UTC).date()
    tenant = await tenant_usage(
        session, organization_id=org, dimension=UsageDimension.MODEL_TOKENS, window_date=today
    )
    source = (
        await source_usage(
            session,
            organization_id=org,
            datasource_id=ds,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=today,
        )
        if ds is not None
        else 0
    )
    return tenant, source


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reservation_is_settled_down_to_what_was_billed(session: AsyncSession) -> None:
    settings = _settings(model_token_daily_quota_per_organization=10_000)
    org, ds = uuid4(), uuid4()
    assert await consume_quota(
        session,
        settings,
        organization_id=org,
        datasource_id=ds,
        dimension=UsageDimension.MODEL_TOKENS,
        amount=1_000,
    )
    await settle_quota(
        session,
        settings,
        organization_id=org,
        datasource_id=ds,
        dimension=UsageDimension.MODEL_TOKENS,
        reserved=1_000,
        actual=300,
    )
    # The capped tenant window went to 1,000 and back down to 300; the uncapped
    # source window was never reserved and simply records the 300.
    assert await _used(session, org, ds) == (300, 300)


@pytest.mark.asyncio
async def test_a_call_that_cost_more_than_reserved_is_charged_the_excess(
    session: AsyncSession,
) -> None:
    settings = _settings(model_token_daily_quota_per_datasource=10_000)
    org, ds = uuid4(), uuid4()
    await consume_quota(
        session,
        settings,
        organization_id=org,
        datasource_id=ds,
        dimension=UsageDimension.MODEL_TOKENS,
        amount=100,
    )
    await settle_quota(
        session,
        settings,
        organization_id=org,
        datasource_id=ds,
        dimension=UsageDimension.MODEL_TOKENS,
        reserved=100,
        actual=150,
    )
    assert await _used(session, org, ds) == (150, 150)


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spent_quota_refuses_before_anything_is_sent(session: AsyncSession) -> None:
    provider = _CountingProvider(_ANSWER)
    gateway = _gateway(_settings(model_token_daily_quota_per_organization=50), provider)
    with pytest.raises(ModelQuotaExhausted) as refused:
        await _call(gateway, session, uuid4(), uuid4())
    assert provider.calls == 0
    assert refused.value.provider_status_code is None
    assert "quota" in str(refused.value)


@pytest.mark.asyncio
async def test_an_admitted_call_leaves_the_billed_figure_in_the_windows(
    session: AsyncSession,
) -> None:
    provider = _CountingProvider(_ANSWER, ProviderUsage(input_tokens=40, output_tokens=10))
    gateway = _gateway(_settings(model_token_daily_quota_per_organization=100_000), provider)
    org, ds = uuid4(), uuid4()
    await _call(gateway, session, org, ds)
    assert await _used(session, org, ds) == (50, 50)


@pytest.mark.asyncio
async def test_with_no_quota_declared_every_caller_is_still_attributed(
    session: AsyncSession,
) -> None:
    provider = _CountingProvider(_ANSWER, ProviderUsage(input_tokens=40, output_tokens=10))
    gateway = _gateway(_settings(), provider)
    org, ds = uuid4(), uuid4()
    await _call(gateway, session, org, ds)
    assert await _used(session, org, ds) == (50, 50)


@pytest.mark.asyncio
async def test_a_failed_call_is_charged_its_input_and_releases_the_output_allowance(
    session: AsyncSession,
) -> None:
    provider = _CountingProvider(ModelGatewayError("down", provider_status_code=503))
    settings = _settings(model_token_daily_quota_per_organization=100_000)
    gateway = _gateway(settings, provider)
    org = uuid4()
    with pytest.raises(ModelGatewayError):
        await _call(gateway, session, org, None)
    tenant, _source = await _used(session, org)
    # The input estimate, not the input plus the 200-token output allowance.
    assert 0 < tenant < 200


# ---------------------------------------------------------------------------
# In the fallback chain: not a route failure
# ---------------------------------------------------------------------------


class _QuotaGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def structured_completion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs["route"].route_key)
        raise ModelQuotaExhausted("TENANT_QUOTA_EXHAUSTED")


@pytest.mark.asyncio
async def test_a_quota_refusal_neither_falls_back_nor_opens_the_breaker(
    session: AsyncSession,
) -> None:
    orchestrator = GovernedAgentOrchestrator(
        Settings(
            environment="test",
            identity_provider="development",
            model_generation_enabled=True,
            model_route="primary",
            model_route_fallbacks="backup",
            _env_file=None,
        )
    )
    fake = _QuotaGateway()
    orchestrator.model_gateway = fake  # type: ignore[assignment]
    org = uuid4()
    routes = [_route(), replace(_route(), route_key="backup")]
    for _ in range(4):
        with pytest.raises(ModelQuotaExhausted):
            await orchestrator._generate_with_fallback(
                session=session,
                organization_id=org,
                approved_routes=routes,
                system_instruction="x",
                payload={},
                datasource_id=uuid4(),
            )
    assert fake.calls == ["primary"] * 4


# ---------------------------------------------------------------------------
# The Ask rate budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spent_ask_budget_is_a_429_with_retry_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _spent(*_args: Any, **kwargs: Any) -> BudgetDecision:
        assert kwargs["namespace"] == "ask-budget"
        assert kwargs["enabled"] is True
        return BudgetDecision(
            allowed=False, bucket="REQUEST_MINUTE", limit=20, used=21, retry_after_seconds=30
        )

    monkeypatch.setattr(api_module, "consume_window_budget", _spent)
    with pytest.raises(HTTPException) as refused:
        await api_module._admit_ask(
            Settings(ask_budget_enabled=True, _env_file=None),
            security_context(organization_id=uuid4()),
        )
    assert refused.value.status_code == 429
    assert "20 questions a minute" in str(refused.value.detail)
    assert refused.value.headers is not None


@pytest.mark.asyncio
async def test_the_ask_budget_is_off_unless_enabled() -> None:
    # No Redis is reachable here; with the budget off nothing is contacted.
    await api_module._admit_ask(Settings(_env_file=None), security_context(organization_id=uuid4()))
