import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import KillSwitchState
from aida.secrets import SecretResolutionError, SecretResolver

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)
SUPPORTED_MODEL_PROVIDERS = frozenset({"OPENAI", "GOOGLE_GEMINI"})

# Sentinel `route_key` for an organization-wide kill switch row in `KillSwitchState`
# (MG-2), as opposed to a row scoped to one specific route_key.
GLOBAL_KILL_SWITCH_SCOPE = "*"


class SqlGenerationOutput(BaseModel):
    sql: str = Field(min_length=1, max_length=200_000)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_codes: list[str] = Field(min_length=1, max_length=20)
    referenced_evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class ModelGatewayError(RuntimeError):
    """Model provider call could not be completed.

    ``provider_status_code`` carries the underlying HTTP status when the
    failure was a provider response (as opposed to a local timeout/network
    error); the API layer branches on it so a 429 (throttled) can surface as
    HTTP 429 to the client instead of a generic 503, and the UI can render
    "provider throttled, try again" instead of "no model route configured".
    """

    def __init__(
        self,
        message: str,
        *,
        provider_status_code: int | None = None,
        provider_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_status_code = provider_status_code
        # The provider's own reason ("model not found", "invalid schema"), cleaned
        # by `provider_error_summary`. None when the body carried no usable reason.
        self.provider_error = provider_error


class ModelRouteNotApproved(ModelGatewayError):
    pass


class ModelOutputInvalid(ModelGatewayError):
    pass


class KillSwitchEngaged(ModelGatewayError):
    """Raised by `ProviderNeutralModelGateway.structured_completion` when an
    organization-wide or route-scoped kill switch (MG-2) is engaged. Checked first,
    before route/credential/adapter/budget conditions, so it fails closed even when
    every other activation condition is otherwise satisfied."""


#: Bytes of serialized JSON the platform counts as one token. A heuristic,
#: not a tokenizer, and a number derived from one vendor's tokenizer would be
#: no more accurate for the others. It is what the gateway checks a request
#: against *before* the call, when nothing has been billed yet; what the
#: provider reports it billed afterwards is carried separately
#: (`ProviderUsage`). Named and exported so contract-budget enforcement
#: (`aida.agent_budget`) bounds the *same* quantity this gateway measures,
#: rather than a second estimate that could drift from it.
BYTES_PER_ESTIMATED_TOKEN = 4


def estimate_serialized_tokens(serialized: str) -> int:
    """Estimated tokens for an already-serialized string. Never zero: a call
    that happened cost something, and a zero would make a budget check pass
    for free."""
    return max(1, len(serialized) // BYTES_PER_ESTIMATED_TOKEN)


def estimate_payload_tokens(payload: dict[str, Any]) -> int:
    """Estimated input tokens for a request payload, serialized exactly as
    `structured_completion` serializes it -- same `sort_keys`/`separators`, so
    the pre-flight estimate and the recorded one cannot disagree."""
    return estimate_serialized_tokens(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


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


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Tokens a provider reports it billed for one call."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """A provider adapter's answer: the structured output and, when the
    provider reports it, what the call was billed. An adapter may instead return
    the bare output dict, as test and fixture providers do; the gateway treats
    that as a call that reported no usage."""

    output: dict[str, Any]
    usage: ProviderUsage | None = None


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
    ) -> dict[str, Any] | ProviderCompletion: ...


def _token_count(value: Any) -> int | None:
    """A provider-reported count, or None for anything that is not one."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def openai_usage(response: dict[str, Any]) -> ProviderUsage | None:
    """`usage` from an OpenAI Responses API answer. Its `output_tokens`
    already includes reasoning tokens, which are billed as output."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _token_count(usage.get("input_tokens"))
    output_tokens = _token_count(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    return ProviderUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def gemini_usage(response: dict[str, Any]) -> ProviderUsage | None:
    """`usageMetadata` from a Gemini generateContent answer. Thinking tokens
    are billed as output but reported apart from `candidatesTokenCount`, so
    they are added to it."""
    usage = response.get("usageMetadata")
    if not isinstance(usage, dict):
        return None
    prompt = _token_count(usage.get("promptTokenCount"))
    candidates = _token_count(usage.get("candidatesTokenCount"))
    if prompt is None or candidates is None:
        return None
    thoughts = _token_count(usage.get("thoughtsTokenCount")) or 0
    return ProviderUsage(input_tokens=prompt, output_tokens=candidates + thoughts)


#: Longest provider reason a `ModelGatewayError` carries: enough for "Invalid
#: schema for response_format ...", short of echoing a request back.
PROVIDER_ERROR_MAX_CHARS = 300
# OpenAI keys start "sk-" (its 401 names a masked one); Gemini keys "AIza".
_KEY_PREFIXES = ("sk-", "AIza")


def provider_error_summary(response: httpx.Response) -> str | None:
    """The provider's own reason for a failed call, safe to show and to store.

    OpenAI and Gemini both answer errors with ``{"error": {"message": ...}}``;
    OpenAI adds ``code``/``type`` and Gemini ``code``/``status``. Only those
    fields are read, never the rest of the body. Whitespace is collapsed, any
    word shaped like an API key is replaced, and the result is capped at
    ``PROVIDER_ERROR_MAX_CHARS``. A body that is not JSON, or has no message,
    gives None, and the status code is all the caller learns.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    labels: list[str] = []
    if isinstance(error, dict):
        message = error.get("message")
        labels = [str(error[key]) for key in ("code", "type", "status") if error.get(key)]
    else:
        message = error
    if not isinstance(message, str) or not message.strip():
        return None
    words = [
        "[redacted]" if word.lstrip("'\"(").startswith(_KEY_PREFIXES) else word
        for word in message.split()
    ]
    summary = " ".join(words)
    if labels:
        summary = f"{'/'.join(dict.fromkeys(labels))}: {summary}"
    if len(summary) > PROVIDER_ERROR_MAX_CHARS:
        summary = summary[: PROVIDER_ERROR_MAX_CHARS - 1].rstrip() + "…"
    return summary


async def post_with_retry(
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
            reason = provider_error_summary(response)
            raise ModelGatewayError(
                f"model provider request failed with HTTP {response.status_code}"
                + (f": {reason}" if reason else ""),
                provider_status_code=response.status_code,
                provider_error=reason,
            )
        retry_after = response.headers.get("retry-after")
        try:
            # 2026-09-03: retry_after ceiling raised 2s -> 30s so a provider that
            # signals a real back-off window (OpenAI/Gemini quota-reset headers
            # commonly return 20-60s) is honored instead of being re-hit at 2s
            # intervals and guaranteed to 429 again. The exp-backoff branch
            # (no retry_after header) keeps its original 2s cap since it is
            # only picking a delay in the absence of provider guidance.
            delay = min(float(retry_after), 30.0) if retry_after else min(0.25 * (2**attempt), 2.0)
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
    ) -> ProviderCompletion:
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
        base_url = _resolve_endpoint_base_url(route, self.settings, self.settings.openai_base_url)
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            response = await post_with_retry(
                client=client,
                url=f"{base_url.rstrip('/')}/responses",
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
                        return ProviderCompletion(parsed, openai_usage(response))
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
    ) -> ProviderCompletion:
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
        base_url = _resolve_endpoint_base_url(route, self.settings, self.settings.gemini_base_url)
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.settings.model_timeout_seconds)
        try:
            response = await post_with_retry(
                client=client,
                url=f"{base_url.rstrip('/')}/models/{model_id}:generateContent",
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
        return ProviderCompletion(parsed, gemini_usage(response))


def _resolve_endpoint_base_url(route: ApprovedModelRoute, settings: Settings, default: str) -> str:
    """MG-3: route a call through its approved route's private endpoint, if one is
    configured for that `endpoint_alias`, instead of always the public default.

    `settings.model_endpoint_urls` is keyed by `endpoint_alias`, the same
    maker-checker-approved, non-secret label already carried on
    `ModelRouteConfiguration`. An alias with no entry falls back to the provider's
    public default unchanged, so this is additive: no previously-approved route's
    behavior changes until an operator explicitly maps its alias to a private URL.
    """
    return settings.model_endpoint_urls.get(route.endpoint_alias, default)


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


async def kill_switch_blocking_state(
    session: AsyncSession, organization_id: UUID, route_key: str | None
) -> KillSwitchState | None:
    """The engaged `KillSwitchState` row (organization-wide or matching `route_key`)
    that blocks generation for this organization right now, or `None` if neither is
    engaged. A live, per-request query against the governed table -- not a cached or
    eventually-consistent read -- so a just-engaged switch blocks the very next call.
    """
    scopes = [GLOBAL_KILL_SWITCH_SCOPE]
    if route_key:
        scopes.append(route_key)
    rows = (
        await session.scalars(
            select(KillSwitchState).where(
                KillSwitchState.organization_id == organization_id,
                KillSwitchState.route_key.in_(scopes),
                KillSwitchState.engaged.is_(True),
            )
        )
    ).all()
    if not rows:
        return None
    for row in rows:
        if row.route_key == GLOBAL_KILL_SWITCH_SCOPE:
            return row
    return rows[0]


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
    #: Tokens *estimated* by the same 4-bytes-per-token heuristic this gateway
    #: enforces `model_max_input_tokens` against before the call. Kept even when
    #: the provider reports usage: the cap is checked against this number, so it
    #: stays the like-for-like comparison, and every surface that renders it
    #: says "estimated".
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    #: What the provider reports it billed (`ProviderUsage`), or None when it
    #: reported nothing: a fixture provider, or an answer without usage.
    provider_input_tokens: int | None = None
    provider_output_tokens: int | None = None


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
        session: AsyncSession,
        organization_id: UUID,
        route: ApprovedModelRoute | None,
        system_instruction: str,
        payload: dict[str, Any],
        output_schema: type[StructuredModel],
    ) -> tuple[StructuredModel, ModelCallEvidence]:
        # Checked first, ahead of every other activation condition (MG-2): a kill
        # switch engaged through the governed API is a live DB read on this call,
        # not cached config, so it blocks the very next generation request.
        blocking = await kill_switch_blocking_state(
            session, organization_id, route.route_key if route is not None else None
        )
        if blocking is not None:
            scope_desc = (
                "organization-wide"
                if blocking.route_key == GLOBAL_KILL_SWITCH_SCOPE
                else f"route {blocking.route_key!r}"
            )
            raise KillSwitchEngaged(
                f"kill switch engaged ({scope_desc}): {blocking.reason or 'no reason given'}"
            )
        allowed_routes = {
            key
            for key in (self.settings.model_route, *self.settings.model_route_fallback_keys)
            if key
        }
        if not self.settings.model_generation_enabled or not allowed_routes:
            raise ModelRouteNotApproved("no policy-approved model route is configured")
        if route is None or route.route_key not in allowed_routes:
            raise ModelRouteNotApproved(
                "selected model route is not approved for this deployment"
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
        estimated_tokens = estimate_payload_tokens(payload)
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
            completion = raw if isinstance(raw, ProviderCompletion) else ProviderCompletion(raw)
            output = output_schema.model_validate(completion.output)
        except TimeoutError as exc:
            raise ModelGatewayError("model route timed out") from exc
        except ValidationError as exc:
            raise ModelOutputInvalid("model output failed its structured contract") from exc
        serialized_output = json.dumps(output.model_dump(mode="json"), sort_keys=True)
        usage = completion.usage
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
            estimated_input_tokens=estimated_tokens,
            estimated_output_tokens=estimate_serialized_tokens(serialized_output),
            provider_input_tokens=usage.input_tokens if usage is not None else None,
            provider_output_tokens=usage.output_tokens if usage is not None else None,
        )
        return output, evidence


class DeterministicTestProvider:
    def __init__(self, response: dict[str, Any], usage: ProviderUsage | None = None) -> None:
        self.response = response
        self.usage = usage

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
    ) -> dict[str, Any] | ProviderCompletion:
        del route, credential, system_instruction, payload, output_schema, schema_name
        del max_output_tokens
        if self.usage is None:
            return self.response
        return ProviderCompletion(self.response, self.usage)
