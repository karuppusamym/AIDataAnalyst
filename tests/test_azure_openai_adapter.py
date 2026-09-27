"""R11-MP01: the Azure OpenAI adapter.

A bank's own Azure OpenAI resource: the route's `model_id` names the deployment, every call
pins `azure_openai_api_version`, the key travels as `api-key` (never a bearer token), and there
is no public default -- an alias `model_endpoint_urls` does not map is refused before anything
is sent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aida.config import Settings
from aida.model_gateway import (
    PRIVATE_ENDPOINT_PROVIDERS,
    SUPPORTED_MODEL_PROVIDERS,
    ModelRouteNotApproved,
    OpenAICompatibleChatProvider,
    SqlGenerationOutput,
    build_model_providers,
    route_endpoint_problem,
)
from tests.test_model_gateway import approved_route

pytestmark = pytest.mark.asyncio

_ANSWER = {
    "choices": [
        {
            "finish_reason": "stop",
            "message": {
                "content": json.dumps(
                    {
                        "sql": "SELECT branch_id, SUM(amount) FROM deposits GROUP BY branch_id",
                        "confidence": 0.8,
                        "rationale_codes": ["CATALOG"],
                        "referenced_evidence_ids": [],
                    }
                )
            },
        }
    ],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40},
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model_endpoint_urls": {"private-model-endpoint": "https://bank-aoai.openai.azure.com"},
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


async def _call(provider: OpenAICompatibleChatProvider) -> Any:
    return await provider(
        route=approved_route("AZURE_OPENAI"),
        credential="azure-test-key",
        system_instruction="Generate SQL",
        payload={"question": "deposits by branch"},
        output_schema=SqlGenerationOutput.model_json_schema(),
        schema_name="SqlGenerationOutput",
        max_output_tokens=500,
    )


async def test_a_call_addresses_the_deployment_with_the_pinned_version_and_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ANSWER)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleChatProvider(_settings(), client, provider_type="AZURE_OPENAI")
    completion = await _call(provider)
    await client.aclose()
    [request] = seen
    assert request.url.host == "bank-aoai.openai.azure.com"
    assert request.url.path == "/openai/deployments/approved-model/chat/completions"
    assert request.url.params["api-version"] == "2024-10-21"
    assert request.headers["api-key"] == "azure-test-key"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["response_format"]["type"] == "json_schema"
    assert "provider" not in body  # OpenRouter's pinning is OpenRouter's alone
    assert completion.output["confidence"] == 0.8
    assert completion.usage is not None and completion.usage.input_tokens == 120


async def test_an_unmapped_alias_is_refused_before_anything_is_sent() -> None:
    def never(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent without a mapped endpoint")

    client = httpx.AsyncClient(transport=httpx.MockTransport(never))
    provider = OpenAICompatibleChatProvider(
        _settings(model_endpoint_urls={}), client, provider_type="AZURE_OPENAI"
    )
    with pytest.raises(ModelRouteNotApproved):
        await _call(provider)
    await client.aclose()
    problem = route_endpoint_problem(
        provider_type="AZURE_OPENAI",
        endpoint_alias="private-model-endpoint",
        settings=_settings(model_endpoint_urls={}),
    )
    assert problem is not None


async def test_the_adapter_is_registered_and_has_no_public_default() -> None:
    assert "AZURE_OPENAI" in SUPPORTED_MODEL_PROVIDERS
    assert "AZURE_OPENAI" in PRIVATE_ENDPOINT_PROVIDERS
    assert "AZURE_OPENAI" in build_model_providers(_settings())
