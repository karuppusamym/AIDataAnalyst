import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError

from aida.config import Settings
from aida.secrets import SecretResolutionError, SecretResolver

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)
SUPPORTED_MODEL_PROVIDERS = frozenset({"OPENAI", "GOOGLE_GEMINI"})


class SqlGenerationOutput(BaseModel):
    sql: str = Field(min_length=1, max_length=200_000)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_codes: list[str] = Field(min_length=1, max_length=20)
    referenced_evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class ModelGatewayError(RuntimeError):
    pass


class ModelRouteNotApproved(ModelGatewayError):
    pass


class ModelOutputInvalid(ModelGatewayError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovedModelRoute:
    route_key: str
    provider_type: str
    model_id: str
    endpoint_alias: str
    credential_reference: str
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int


class StructuredModelProvider(Protocol):
    async def __call__(
        self,
        *,
        route: ApprovedModelRoute,
        credential: str,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, Any]: ...


async def _post_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    attempts: int,
) -> dict[str, Any]:
    response: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            if attempt + 1 == attempts:
                raise ModelGatewayError("model provider network request failed") from exc
            await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
            continue
        if response.status_code < 400:
            try:
                value = response.json()
            except ValueError as exc:
                raise ModelOutputInvalid("model provider returned invalid JSON") from exc
            if not isinstance(value, dict):
                raise ModelOutputInvalid("model provider response has an invalid shape")
            return value
        retryable = response.status_code in {408, 409, 429, 500, 502, 503, 504}
        if not retryable or attempt + 1 == attempts:
            raise ModelGatewayError(
                f"model provider request failed with HTTP {response.status_code}"
            )
        retry_after = response.headers.get("retry-after")
        try:
            delay = min(float(retry_after), 2.0) if retry_after else min(0.25 * (2**attempt), 2.0)
        except ValueError:
            delay = min(0.25 * (2**attempt), 2.0)
        await asyncio.sleep(delay)
    raise ModelGatewayError("model provider request failed")


def _openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a pydantic JSON schema rewritten to satisfy OpenAI's strict
    structured-output contract: every object node must set additionalProperties=False and
    list every one of its properties in "required" (OpenAI rejects schemas that omit this,
    even for fields that are optional at the Python/pydantic level)."""

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or isinstance(node.get("properties"), dict):
                node["additionalProperties"] = False
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["required"] = list(properties.keys())
                    for value in properties.values():
                        _walk(value)
            items = node.get("items")
            if isinstance(items, dict):
                _walk(items)
            for defs_key in ("$defs", "definitions"):
                definitions = node.get(defs_key)
                if isinstance(definitions, dict):
                    for value in definitions.values():
                        _walk(value)
            for combinator_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
                branches = node.get(combinator_key)
                if isinstance(branches, list):
                    for branch in branches:
                        _walk(branch)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    schema_copy: dict[str, Any] = json.loads(json.dumps(schema))
    _walk(schema_copy)
    return schema_copy


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter with strict JSON-schema output."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def __call__(
        self,
        *,
        route: ApprovedModelRoute,
        credential: str,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        body = {
            "model": route.model_id,
            "instructions": system_instruction,
            "input": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "max_output_tokens": max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name.lower(),
                    "strict": True,
                    "schema": _openai_strict_schema(output_schema),
                }
            },
        }
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            response = await _post_with_retry(
                client=client,
                url=f"{self.settings.openai_base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
                body=body,
                attempts=self.settings.model_provider_max_attempts,
            )
        finally:
            if owned_client:
                await client.aclose()
        for output in response.get("output", []):
            if not isinstance(output, dict):
                continue
            for content in output.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    try:
                        parsed = json.loads(str(content.get("text", "")))
                    except json.JSONDecodeError as exc:
                        raise ModelOutputInvalid("OpenAI structured output was not JSON") from exc
                    if isinstance(parsed, dict):
                        return parsed
        raise ModelOutputInvalid("OpenAI response did not contain structured output")


class GeminiGenerateContentProvider:
    """Gemini generateContent adapter with a JSON response schema."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def __call__(
        self,
        *,
        route: ApprovedModelRoute,
        credential: str,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        del schema_name
        body = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(payload, sort_keys=True, separators=(",", ":"))}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": output_schema,
            },
        }
        model_id = quote(route.model_id.removeprefix("models/"), safe="-_.")
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            response = await _post_with_retry(
                client=client,
                url=(
                    f"{self.settings.gemini_base_url.rstrip('/')}/models/{model_id}:generateContent"
                ),
                headers={"x-goog-api-key": credential, "Content-Type": "application/json"},
                body=body,
                attempts=self.settings.model_provider_max_attempts,
            )
        finally:
            if owned_client:
                await client.aclose()
        try:
            text = response["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ModelOutputInvalid("Gemini response did not contain structured output") from exc
        if not isinstance(parsed, dict):
            raise ModelOutputInvalid("Gemini structured output has an invalid shape")
        return parsed


def build_model_providers(settings: Settings) -> dict[str, StructuredModelProvider]:
    return {
        "OPENAI": OpenAIResponsesProvider(settings),
        "GOOGLE_GEMINI": GeminiGenerateContentProvider(settings),
    }


def route_adapter_available(
    *, provider_type: str, credential_reference: str | None, settings: Settings
) -> bool:
    if provider_type not in SUPPORTED_MODEL_PROVIDERS or not credential_reference:
        return False
    try:
        _resolve_model_credential(credential_reference, settings, SecretResolver(settings))
    except SecretResolutionError:
        return False
    return True


def _resolve_model_credential(reference: str, settings: Settings, resolver: SecretResolver) -> str:
    local_keys = {
        "env://OPENAI_API_KEY": settings.openai_api_key,
        "env://GEMINI_API_KEY": settings.gemini_api_key,
    }
    configured = local_keys.get(reference)
    if configured is not None:
        value = configured.get_secret_value()
        if value and not value.startswith("replace-"):
            return value
        raise SecretResolutionError("configured local model credential is a placeholder")
    return resolver.resolve(reference)


@dataclass(frozen=True, slots=True)
class ModelCallEvidence:
    route: str
    provider_type: str
    model_id: str
    endpoint_alias: str
    input_fingerprint: str
    output_fingerprint: str
    input_size_bytes: int
    output_size_bytes: int
    schema_name: str


class ProviderNeutralModelGateway:
    """Enforces selected/approved routes, budgets, credentials, timeout, and output contracts."""

    def __init__(
        self,
        settings: Settings,
        providers: dict[str, StructuredModelProvider] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers or build_model_providers(settings)
        self.secret_resolver = secret_resolver or SecretResolver(settings)

    async def structured_completion(
        self,
        *,
        route: ApprovedModelRoute | None,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: type[StructuredModel],
    ) -> tuple[StructuredModel, ModelCallEvidence]:
        if not self.settings.model_generation_enabled or not self.settings.model_route:
            raise ModelRouteNotApproved("no policy-approved model route is configured")
        if route is None or route.route_key != self.settings.model_route:
            raise ModelRouteNotApproved(
                "selected model route is not approved for this organization"
            )
        provider = self.providers.get(route.provider_type)
        if provider is None:
            raise ModelRouteNotApproved("approved model route has no registered provider adapter")
        try:
            credential = _resolve_model_credential(
                route.credential_reference, self.settings, self.secret_resolver
            )
        except SecretResolutionError as exc:
            raise ModelRouteNotApproved("approved model route credential is unavailable") from exc
        serialized_input = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        estimated_tokens = max(1, len(serialized_input) // 4)
        input_budget = min(self.settings.model_max_input_tokens, route.max_input_tokens)
        output_budget = min(self.settings.model_max_output_tokens, route.max_output_tokens)
        if estimated_tokens > input_budget:
            raise ModelGatewayError("model input exceeds the approved token budget")
        try:
            raw = await asyncio.wait_for(
                provider(
                    route=route,
                    credential=credential,
                    system_instruction=system_instruction,
                    payload=payload,
                    output_schema=output_schema.model_json_schema(),
                    schema_name=output_schema.__name__,
                    max_output_tokens=output_budget,
                ),
                timeout=min(self.settings.model_timeout_seconds, route.timeout_seconds),
            )
            output = output_schema.model_validate(raw)
        except TimeoutError as exc:
            raise ModelGatewayError("model route timed out") from exc
        except ValidationError as exc:
            raise ModelOutputInvalid("model output failed its structured contract") from exc
        serialized_output = json.dumps(output.model_dump(mode="json"), sort_keys=True)
        evidence = ModelCallEvidence(
            route=route.route_key,
            provider_type=route.provider_type,
            model_id=route.model_id,
            endpoint_alias=route.endpoint_alias,
            input_fingerprint=hashlib.sha256(serialized_input.encode()).hexdigest(),
            output_fingerprint=hashlib.sha256(serialized_output.encode()).hexdigest(),
            input_size_bytes=len(serialized_input.encode()),
            output_size_bytes=len(serialized_output.encode()),
            schema_name=output_schema.__name__,
        )
        return output, evidence


class DeterministicTestProvider:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    async def __call__(
        self,
        *,
        route: ApprovedModelRoute,
        credential: str,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        del route, credential, system_instruction, payload, output_schema, schema_name
        del max_output_tokens
        return self.response
