"""R11-MP01: Anthropic, OpenRouter and private OpenAI-compatible model routes.

The gateway could call two providers, OpenAI and Gemini. Five more provider
types could be *declared* on a route and approved, and then answered
ADAPTER_REGISTRATION_REQUIRED forever. These tests pin the three adapter paths
that close most of that gap -- Anthropic's Messages API, OpenRouter, and the
private chat-completions endpoints (OPENAI_COMPATIBLE_PRIVATE, ON_PREM) -- and
the two residency refusals that come with them: a private alias with no URL is
never sent anywhere, and an OpenRouter route without pinned upstreams is refused
rather than left to OpenRouter's choice.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ANTHROPIC_API_VERSION,
    SUPPORTED_MODEL_PROVIDERS,
    AnthropicMessagesProvider,
    ApprovedModelRoute,
    DeterministicTestProvider,
    ModelOutputInvalid,
    ModelRouteNotApproved,
    OpenAICompatibleChatProvider,
    ProviderNeutralModelGateway,
    ProviderUsage,
    SqlGenerationOutput,
    anthropic_usage,
    build_model_providers,
    chat_completions_usage,
    route_adapter_available,
    route_endpoint_problem,
)
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider

_ANSWER = {
    "sql": "SELECT account_id FROM retail.account",
    "confidence": 0.9,
    "rationale_codes": ["CATALOG"],
    "referenced_evidence_ids": ["table-1"],
}


def _route(provider_type: str, alias: str = "bank-endpoint") -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key="test-route",
        provider_type=provider_type,
        model_id="approved-model",
        endpoint_alias=alias,
        credential_reference="vault://model-key",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


class _Recorder:
    """A mock transport that keeps every request and answers with `response`."""

    def __init__(self, response: dict[str, Any], status: int = 200) -> None:
        self.response = response
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.response)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


async def _call(adapter: Any, route: ApprovedModelRoute) -> Any:
    return await adapter(
        route=route,
        credential="local-test-secret",
        system_instruction="Generate SQL",
        payload={"question": "count accounts"},
        output_schema=SqlGenerationOutput.model_json_schema(),
        schema_name="SqlGenerationOutput",
        max_output_tokens=1000,
    )


def _anthropic_answer(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "content": [
            {"type": "text", "text": "Here you go."},
            {"type": "tool_use", "name": "SqlGenerationOutput", "input": _ANSWER},
        ],
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 40,
            "output_tokens": 25,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        },
    }
    answer.update(overrides)
    return answer


def _chat_answer(**usage_extra: Any) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(_ANSWER)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, **usage_extra},
    }


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_forces_one_tool_call_and_caches_the_system_prefix() -> None:
    recorder = _Recorder(_anthropic_answer())
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        completion = await _call(
            AnthropicMessagesProvider(Settings(_env_file=None), client), _route("ANTHROPIC")
        )

    request = recorder.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "local-test-secret"
    assert request.headers["anthropic-version"] == ANTHROPIC_API_VERSION
    assert "authorization" not in request.headers
    body = recorder.body
    assert body["tool_choice"] == {"type": "tool", "name": "SqlGenerationOutput"}
    assert [tool["name"] for tool in body["tools"]] == ["SqlGenerationOutput"]
    assert body["tools"][0]["input_schema"] == SqlGenerationOutput.model_json_schema()
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["max_tokens"] == 1000
    assert completion.output == _ANSWER


def test_anthropic_usage_counts_cache_reads_and_writes_as_billed_input() -> None:
    usage = anthropic_usage(
        {
            "usage": {
                "input_tokens": 40,
                "output_tokens": 25,
                "cache_creation_input_tokens": 60,
                "cache_read_input_tokens": 900,
            }
        }
    )
    assert usage == ProviderUsage(input_tokens=1000, output_tokens=25, cached_input_tokens=900)


def test_anthropic_usage_without_counts_is_unreported() -> None:
    assert anthropic_usage({}) is None
    assert anthropic_usage({"usage": {"input_tokens": "40", "output_tokens": 1}}) is None


@pytest.mark.asyncio
async def test_anthropic_output_cut_off_at_the_cap_is_invalid_not_partial() -> None:
    recorder = _Recorder(_anthropic_answer(stop_reason="max_tokens"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        with pytest.raises(ModelOutputInvalid, match="cut off"):
            await _call(
                AnthropicMessagesProvider(Settings(_env_file=None), client), _route("ANTHROPIC")
            )


@pytest.mark.asyncio
async def test_anthropic_answer_without_the_named_tool_is_invalid() -> None:
    recorder = _Recorder(
        _anthropic_answer(content=[{"type": "tool_use", "name": "other_tool", "input": _ANSWER}])
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        with pytest.raises(ModelOutputInvalid):
            await _call(
                AnthropicMessagesProvider(Settings(_env_file=None), client), _route("ANTHROPIC")
            )


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


def _openrouter_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "openrouter_provider_order": {"eu-openrouter": ["Mistral", "Nebius"]},
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_openrouter_pins_upstreams_and_refuses_data_collection() -> None:
    recorder = _Recorder(_chat_answer(cost=0.00042))
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        completion = await _call(
            OpenAICompatibleChatProvider(
                _openrouter_settings(), client, provider_type="OPENROUTER"
            ),
            _route("OPENROUTER", "eu-openrouter"),
        )

    assert str(recorder.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    body = recorder.body
    assert body["provider"] == {
        "data_collection": "deny",
        "require_parameters": True,
        "order": ["Mistral", "Nebius"],
        "allow_fallbacks": False,
    }
    assert body["usage"] == {"include": True}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert completion.output == _ANSWER
    assert completion.usage.reported_cost_usd == pytest.approx(0.00042)


@pytest.mark.asyncio
async def test_openrouter_route_without_pinned_upstreams_sends_nothing() -> None:
    recorder = _Recorder(_chat_answer())
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        with pytest.raises(ModelRouteNotApproved, match="pinned upstream"):
            await _call(
                OpenAICompatibleChatProvider(
                    _openrouter_settings(), client, provider_type="OPENROUTER"
                ),
                _route("OPENROUTER", "unpinned-alias"),
            )
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_openrouter_pinning_can_be_waived_explicitly() -> None:
    recorder = _Recorder(_chat_answer())
    settings = _openrouter_settings(openrouter_require_pinned_provider=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        await _call(
            OpenAICompatibleChatProvider(settings, client, provider_type="OPENROUTER"),
            _route("OPENROUTER", "unpinned-alias"),
        )
    # Still never lets an upstream keep the prompt.
    assert recorder.body["provider"] == {"data_collection": "deny", "require_parameters": True}


def test_chat_usage_reads_cached_tokens_and_a_stated_cost() -> None:
    usage = chat_completions_usage(
        _chat_answer(prompt_tokens_details={"cached_tokens": 100}, cost=0.001)
    )
    assert usage == ProviderUsage(
        input_tokens=120, output_tokens=30, cached_input_tokens=100, reported_cost_usd=0.001
    )


@pytest.mark.parametrize("cost", [None, "0.1", -1, True])
def test_chat_usage_ignores_a_cost_that_is_not_one(cost: Any) -> None:
    usage = chat_completions_usage(_chat_answer(cost=cost))
    assert usage is not None
    assert usage.reported_cost_usd is None


# ---------------------------------------------------------------------------
# Private OpenAI-compatible endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["OPENAI_COMPATIBLE_PRIVATE", "ON_PREM"])
async def test_private_route_posts_only_to_its_mapped_endpoint(provider_type: str) -> None:
    recorder = _Recorder(_chat_answer())
    settings = Settings(
        model_endpoint_urls={"bank-endpoint": "https://llm.bank.internal/v1"}, _env_file=None
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        completion = await _call(
            OpenAICompatibleChatProvider(settings, client, provider_type=provider_type),
            _route(provider_type),
        )
    assert str(recorder.requests[0].url) == "https://llm.bank.internal/v1/chat/completions"
    assert recorder.requests[0].headers["authorization"] == "Bearer local-test-secret"
    assert "provider" not in recorder.body
    assert completion.output == _ANSWER


@pytest.mark.asyncio
async def test_private_route_with_no_mapped_endpoint_sends_nothing() -> None:
    recorder = _Recorder(_chat_answer())
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        with pytest.raises(ModelRouteNotApproved, match="no endpoint URL"):
            await _call(
                OpenAICompatibleChatProvider(
                    Settings(_env_file=None), client, provider_type="ON_PREM"
                ),
                _route("ON_PREM"),
            )
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_chat_completion_cut_off_at_the_cap_is_invalid() -> None:
    answer = _chat_answer()
    answer["choices"][0]["finish_reason"] = "length"
    recorder = _Recorder(answer)
    settings = Settings(
        model_endpoint_urls={"bank-endpoint": "https://llm.bank.internal/v1"}, _env_file=None
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        with pytest.raises(ModelOutputInvalid, match="cut off"):
            await _call(OpenAICompatibleChatProvider(settings, client), _route("ON_PREM"))


# ---------------------------------------------------------------------------
# Activation status and registration
# ---------------------------------------------------------------------------


def test_every_supported_provider_type_has_a_registered_adapter() -> None:
    assert set(build_model_providers(Settings(_env_file=None))) == set(SUPPORTED_MODEL_PROVIDERS)


def test_endpoint_problems_are_answered_before_any_call() -> None:
    settings = _openrouter_settings(
        model_endpoint_urls={"bank-endpoint": "https://llm.bank.internal/v1"}
    )
    assert (
        route_endpoint_problem(provider_type="ON_PREM", endpoint_alias="other", settings=settings)
        == "PRIVATE_ENDPOINT_NOT_MAPPED"
    )
    assert (
        route_endpoint_problem(
            provider_type="ON_PREM", endpoint_alias="bank-endpoint", settings=settings
        )
        is None
    )
    assert (
        route_endpoint_problem(
            provider_type="OPENROUTER", endpoint_alias="other", settings=settings
        )
        == "OPENROUTER_UPSTREAM_NOT_PINNED"
    )
    assert (
        route_endpoint_problem(
            provider_type="OPENROUTER", endpoint_alias="eu-openrouter", settings=settings
        )
        is None
    )
    assert (
        route_endpoint_problem(
            provider_type="ANTHROPIC", endpoint_alias="anything", settings=settings
        )
        is None
    )


def test_an_unmapped_private_route_is_not_adapter_available() -> None:
    settings = Settings(credential_provider="vault", _env_file=None)
    assert not route_adapter_available(
        provider_type="ON_PREM",
        credential_reference="vault://model-key",
        settings=settings,
        endpoint_alias="bank-endpoint",
    )


# ---------------------------------------------------------------------------
# The gateway carries the new usage fields into the call's evidence
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_carries_cached_tokens_and_the_stated_cost(session: AsyncSession) -> None:
    settings = Settings(
        model_generation_enabled=True,
        model_route="test-route",
        credential_provider="vault",
        _env_file=None,
    )
    resolver = SecretResolver(
        settings,
        {"vault": StaticTestSecretProvider({("model-key", None): ResolvedSecret("secret")})},
    )
    provider = DeterministicTestProvider(
        _ANSWER,
        ProviderUsage(
            input_tokens=1000, output_tokens=20, cached_input_tokens=900, reported_cost_usd=0.002
        ),
    )
    gateway = ProviderNeutralModelGateway(settings, {"ANTHROPIC": provider}, resolver)
    _output, evidence = await gateway.structured_completion(
        session=session,
        organization_id=uuid4(),
        route=_route("ANTHROPIC"),
        system_instruction="Generate read-only SQL",
        payload={"evidence_ids": ["table-1"]},
        output_schema=SqlGenerationOutput,
    )
    assert evidence.provider_input_tokens == 1000
    assert evidence.provider_cached_input_tokens == 900
    assert evidence.provider_reported_cost_usd == pytest.approx(0.002)
