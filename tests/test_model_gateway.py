import json
from collections.abc import AsyncIterator
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
    ModelOutputInvalid,
    ModelRouteNotApproved,
    OpenAIResponsesProvider,
    ProviderNeutralModelGateway,
    SqlGenerationOutput,
)
from aida.secrets import ResolvedSecret, SecretResolver, StaticTestSecretProvider


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory sqlite session, schema-complete (`kill_switch_state` included), for
    the DB-backed kill-switch check every `structured_completion` call now performs
    -- same real-engine pattern as `test_bulk_governance_decisions.py`."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def approved_route(provider_type: str = "OPENAI") -> ApprovedModelRoute:
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


def configured_gateway(response: dict[str, object]) -> ProviderNeutralModelGateway:
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
    return ProviderNeutralModelGateway(
        settings,
        {"OPENAI": DeterministicTestProvider(response)},
        resolver,
    )


@pytest.mark.asyncio
async def test_gateway_fails_closed_without_approved_route(session: AsyncSession) -> None:
    gateway = ProviderNeutralModelGateway(Settings(_env_file=None))
    with pytest.raises(ModelRouteNotApproved):
        await gateway.structured_completion(
            session=session,
            organization_id=uuid4(),
            route=None,
            system_instruction="Generate read-only SQL",
            payload={"evidence": []},
            output_schema=SqlGenerationOutput,
        )


@pytest.mark.asyncio
async def test_gateway_validates_structured_output_and_records_hashes(
    session: AsyncSession,
) -> None:
    gateway = configured_gateway(
        {
            "sql": "SELECT account_id FROM retail.account",
            "confidence": 0.91,
            "rationale_codes": ["GROUNDED_IN_CATALOG"],
            "referenced_evidence_ids": ["table-1"],
        }
    )
    output, evidence = await gateway.structured_completion(
        session=session,
        organization_id=uuid4(),
        route=approved_route(),
        system_instruction="Generate read-only SQL",
        payload={"evidence_ids": ["table-1"]},
        output_schema=SqlGenerationOutput,
    )
    assert output.confidence == 0.91
    assert len(evidence.input_fingerprint) == 64
    assert evidence.route == "test-private-route"
    assert evidence.provider_type == "OPENAI"


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_provider_output(session: AsyncSession) -> None:
    gateway = configured_gateway({"unexpected": "output"})
    with pytest.raises(ModelOutputInvalid):
        await gateway.structured_completion(
            session=session,
            organization_id=uuid4(),
            route=approved_route(),
            system_instruction="Generate read-only SQL",
            payload={"evidence_ids": []},
            output_schema=SqlGenerationOutput,
        )


@pytest.mark.asyncio
async def test_openai_adapter_uses_responses_json_schema_without_leaking_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer local-test-secret"
        body = json.loads(request.content)
        assert body["text"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "sql": "SELECT account_id FROM retail.account",
                                        "confidence": 0.9,
                                        "rationale_codes": ["CATALOG"],
                                        "referenced_evidence_ids": ["table-1"],
                                    }
                                ),
                            }
                        ]
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesProvider(Settings(_env_file=None), client)
    result = await provider(
        route=approved_route(),
        credential="local-test-secret",
        system_instruction="Generate SQL",
        payload={"question": "count accounts"},
        output_schema=SqlGenerationOutput.model_json_schema(),
        schema_name="SqlGenerationOutput",
        max_output_tokens=1000,
    )
    await client.aclose()
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_gemini_adapter_uses_generate_content_json_schema() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "local-test-secret"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "sql": "SELECT account_id FROM retail.account",
                                            "confidence": 0.8,
                                            "rationale_codes": ["CATALOG"],
                                            "referenced_evidence_ids": ["table-1"],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiGenerateContentProvider(Settings(_env_file=None), client)
    result = await provider(
        route=approved_route("GOOGLE_GEMINI"),
        credential="local-test-secret",
        system_instruction="Generate SQL",
        payload={"question": "count accounts"},
        output_schema=SqlGenerationOutput.model_json_schema(),
        schema_name="SqlGenerationOutput",
        max_output_tokens=1000,
    )
    await client.aclose()
    assert result["confidence"] == 0.8
