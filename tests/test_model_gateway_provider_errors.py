"""A failed provider call says why, without leaking a key or the request.

`post_with_retry` used to keep only the HTTP status, so an OpenAI 400 over a
malformed output schema read as "failed with HTTP 400" and had to be reproduced
by hand outside the platform before anyone knew the cause (accomplishment log,
R20). The provider's own reason now travels on the error, cleaned.
"""

import httpx
import pytest

from aida.model_gateway import (
    PROVIDER_ERROR_MAX_CHARS,
    ModelGatewayError,
    post_with_retry,
    provider_error_summary,
)


async def _failure(response: httpx.Response) -> ModelGatewayError:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    try:
        with pytest.raises(ModelGatewayError) as excinfo:
            await post_with_retry(
                client=client,
                url="https://provider.test/v1/responses",
                headers={},
                body={},
                attempts=1,
            )
    finally:
        await client.aclose()
    return excinfo.value


@pytest.mark.asyncio
async def test_an_openai_rejection_carries_its_reason() -> None:
    error = await _failure(
        httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid schema for response_format 'ColumnDrafts': "
                    "'additionalProperties' is required to be supplied and to be false.",
                    "type": "invalid_request_error",
                    "code": "invalid_json_schema",
                }
            },
        )
    )

    assert error.provider_status_code == 400
    assert error.provider_error is not None
    assert error.provider_error.startswith(
        "invalid_json_schema/invalid_request_error: Invalid schema"
    )
    assert str(error) == f"model provider request failed with HTTP 400: {error.provider_error}"


@pytest.mark.asyncio
async def test_a_masked_openai_key_is_not_repeated() -> None:
    error = await _failure(
        httpx.Response(
            401,
            json={
                "error": {
                    "message": "Incorrect API key provided: sk-proj-****************abcd. "
                    "You can find your API key at https://platform.openai.com/account/api-keys.",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    )

    assert "sk-proj" not in str(error)
    assert "abcd" not in str(error)
    assert "Incorrect API key provided: [redacted]" in str(error)


@pytest.mark.asyncio
async def test_a_gemini_rejection_is_read_the_same_way() -> None:
    error = await _failure(
        httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "API key not valid (AIzaSyD-not-a-real-key). "
                    "Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
    )

    assert error.provider_error == (
        "400/INVALID_ARGUMENT: API key not valid [redacted] Please pass a valid API key."
    )
    assert "AIza" not in str(error)


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_leaves_only_the_status() -> None:
    error = await _failure(httpx.Response(502, text="<html><body>Bad gateway</body></html>"))

    assert str(error) == "model provider request failed with HTTP 502"
    assert error.provider_error is None
    assert error.provider_status_code == 502


@pytest.mark.asyncio
async def test_the_last_retryable_failure_keeps_its_reason() -> None:
    error = await _failure(
        httpx.Response(
            429,
            json={
                "error": {
                    "message": "Rate limit reached for gpt-4o-mini.",
                    "code": "rate_limit_exceeded",
                }
            },
        )
    )

    assert error.provider_status_code == 429
    assert error.provider_error == "rate_limit_exceeded: Rate limit reached for gpt-4o-mini."


def test_a_long_reason_is_capped() -> None:
    summary = provider_error_summary(
        httpx.Response(400, json={"error": {"message": "word " * 500}})
    )

    assert summary is not None
    assert len(summary) == PROVIDER_ERROR_MAX_CHARS
    assert summary.endswith("…")


def test_only_the_error_fields_are_read() -> None:
    body = {
        "error": {"message": "quota exhausted", "status": "RESOURCE_EXHAUSTED"},
        "echo": "SELECT *",
    }

    assert (
        provider_error_summary(httpx.Response(429, json=body))
        == "RESOURCE_EXHAUSTED: quota exhausted"
    )
    assert (
        provider_error_summary(httpx.Response(429, json=[body]))
        == "RESOURCE_EXHAUSTED: quota exhausted"
    )
    assert provider_error_summary(httpx.Response(400, json={"error": {"code": "bad"}})) is None
    assert provider_error_summary(httpx.Response(400, json={"detail": "nope"})) is None
    assert (
        provider_error_summary(httpx.Response(400, json={"error": "plain reason"}))
        == "plain reason"
    )
