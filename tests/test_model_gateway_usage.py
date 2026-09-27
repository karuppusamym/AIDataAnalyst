"""A model call records what the provider billed, beside the gateway's estimate.

The gateway estimated tokens at four bytes each and nothing more: OpenAI's
`usage` and Gemini's `usageMetadata` were read by no one, so every budget figure
downstream was an estimate (the accomplishment log's "no provider adapter
reports billable usage"). The adapters now return what the provider reports,
and the call's evidence carries it beside the estimate. The estimate stays,
because the input cap is checked against it before the call.
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
    ApprovedModelRoute,
    DeterministicTestProvider,
    GeminiGenerateContentProvider,
    OpenAIResponsesProvider,
    ProviderNeutralModelGateway,
    ProviderUsage,
    SqlGenerationOutput,
    gemini_usage,
    openai_usage,
)
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider

_ANSWER = {
    "sql": "SELECT account_id FROM retail.account",
    "confidence": 0.9,
    "rationale_codes": ["CATALOG"],
    "referenced_evidence_ids": ["table-1"],
}


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Schema-complete, because `structured_completion` reads `kill_switch_state`."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


def _route(provider_type: str = "OPENAI") -> ApprovedModelRoute:
    return ApprovedModelRoute(
        route_key="test-private-route",
        provider_type=provider_type,
        model_id="approved-model",
        endpoint_alias="private-model-endpoint",
        credential_reference="vault://model-key",
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
    )


def _gateway(provider: DeterministicTestProvider) -> ProviderNeutralModelGateway:
    settings = Settings(
        model_generation_enabled=True,
        model_route="test-private-route",
        credential_provider="vault",
        _env_file=None,
    )
    resolver = SecretResolver(
        settings,
        {"vault": StaticTestSecretProvider({("model-key", None): ResolvedSecret("secret")})},
    )
    return ProviderNeutralModelGateway(settings, {"OPENAI": provider}, resolver)


async def _adapter_call(adapter_class: Any, response: dict[str, Any], provider_type: str) -> Any:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    )
    adapter = adapter_class(Settings(_env_file=None), client)
    try:
        return await adapter(
            route=_route(provider_type),
            credential="local-test-secret",
            system_instruction="Generate SQL",
            payload={"question": "count accounts"},
            output_schema=SqlGenerationOutput.model_json_schema(),
            schema_name="SqlGenerationOutput",
            max_output_tokens=1000,
        )
    finally:
        await client.aclose()


async def _complete(gateway: ProviderNeutralModelGateway, session: AsyncSession) -> Any:
    _output, evidence = await gateway.structured_completion(
        session=session,
        organization_id=uuid4(),
        route=_route(),
        system_instruction="Generate read-only SQL",
        payload={"evidence_ids": ["table-1"]},
        output_schema=SqlGenerationOutput,
    )
    return evidence


@pytest.mark.asyncio
async def test_openai_reports_the_tokens_it_billed() -> None:
    completion = await _adapter_call(
        OpenAIResponsesProvider,
        {
            "output": [{"content": [{"type": "output_text", "text": json.dumps(_ANSWER)}]}],
            "usage": {
                "input_tokens": 412,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 57,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 469,
            },
        },
        "OPENAI",
    )

    assert completion.output["confidence"] == 0.9
    assert completion.usage == ProviderUsage(input_tokens=412, output_tokens=57)


@pytest.mark.asyncio
async def test_gemini_counts_thinking_tokens_as_output() -> None:
    completion = await _adapter_call(
        GeminiGenerateContentProvider,
        {
            "candidates": [{"content": {"parts": [{"text": json.dumps(_ANSWER)}]}}],
            "usageMetadata": {
                "promptTokenCount": 380,
                "candidatesTokenCount": 44,
                "thoughtsTokenCount": 120,
                "totalTokenCount": 544,
            },
        },
        "GOOGLE_GEMINI",
    )

    assert completion.output["confidence"] == 0.9
    assert completion.usage == ProviderUsage(input_tokens=380, output_tokens=164)


def test_missing_or_malformed_usage_is_not_invented() -> None:
    assert openai_usage({}) is None
    assert openai_usage({"usage": {"input_tokens": 10}}) is None
    assert openai_usage({"usage": {"input_tokens": "10", "output_tokens": 2}}) is None
    assert openai_usage({"usage": {"input_tokens": True, "output_tokens": 2}}) is None
    assert openai_usage({"usage": {"input_tokens": -1, "output_tokens": 2}}) is None
    assert gemini_usage({"usageMetadata": {"promptTokenCount": 5}}) is None
    assert gemini_usage(
        {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}
    ) == ProviderUsage(input_tokens=5, output_tokens=3)


@pytest.mark.asyncio
async def test_the_call_evidence_carries_billed_tokens_beside_the_estimate(session) -> None:
    billed = ProviderUsage(input_tokens=90, output_tokens=12)

    evidence = await _complete(_gateway(DeterministicTestProvider(_ANSWER, billed)), session)

    assert (evidence.provider_input_tokens, evidence.provider_output_tokens) == (90, 12)
    assert evidence.estimated_input_tokens > 0
    assert evidence.estimated_output_tokens > 0


@pytest.mark.asyncio
async def test_a_provider_that_reports_nothing_leaves_billed_tokens_unknown(session) -> None:
    evidence = await _complete(_gateway(DeterministicTestProvider(_ANSWER)), session)

    assert evidence.provider_input_tokens is None
    assert evidence.provider_output_tokens is None
    assert evidence.estimated_input_tokens > 0
