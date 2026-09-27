"""R11-MP23: model calls and request budgets borrow process-wide clients.

A provider used to open and close an `httpx.AsyncClient` per call, and a budget
check a Redis connection per request. Both now reuse one client per event loop;
a client a caller injects still wins.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import aida.model_gateway as model_gateway
import aida.outbound_clients as outbound_clients
from aida.config import Settings
from aida.model_gateway import OpenAIResponsesProvider, SqlGenerationOutput
from aida.request_budget import consume_window_budget
from tests.test_model_gateway import approved_route

pytestmark = pytest.mark.asyncio

_ANSWER = {
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
}


async def test_one_client_per_loop_and_timeout() -> None:
    first = outbound_clients.shared_http_client(timeout=30)
    assert outbound_clients.shared_http_client(timeout=30) is first
    assert outbound_clients.shared_http_client(timeout=5) is not first
    assert first.follow_redirects is False
    await outbound_clients.close_outbound_clients()
    assert first.is_closed
    # A closed client is never handed out again.
    assert outbound_clients.shared_http_client(timeout=30) is not first
    await outbound_clients.close_outbound_clients()


async def _call(provider: OpenAIResponsesProvider) -> None:
    await provider(
        route=approved_route(),
        credential="local-test-secret",
        system_instruction="Generate SQL",
        payload={"question": "count accounts"},
        output_schema=SqlGenerationOutput.model_json_schema(),
        schema_name="SqlGenerationOutput",
        max_output_tokens=1000,
    )


async def test_a_provider_without_an_injected_client_reuses_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_ANSWER))
    )
    handed_out: list[float] = []

    def _shared(*, timeout: float) -> httpx.AsyncClient:
        handed_out.append(timeout)
        return shared

    monkeypatch.setattr(model_gateway, "shared_http_client", _shared)
    provider = OpenAIResponsesProvider(Settings(model_timeout_seconds=12, _env_file=None))
    await _call(provider)
    await _call(provider)
    # Borrowed twice, with the configured timeout, and never closed by the call.
    assert handed_out == [12, 12]
    assert not shared.is_closed
    await shared.aclose()


async def test_an_injected_client_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never(**_kwargs: Any) -> httpx.AsyncClient:
        raise AssertionError("an injected client must be used")

    monkeypatch.setattr(model_gateway, "shared_http_client", _never)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_ANSWER))
    )
    await _call(OpenAIResponsesProvider(Settings(_env_file=None), client))
    await client.aclose()


class _FakeRedis:
    closed = 0

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def eval(self, _script: str, _keys: int, key: str, _ttl: str) -> list[object]:
        self.counts[key] = self.counts.get(key, 0) + 1
        return [self.counts[key], 60]

    async def aclose(self) -> None:
        _FakeRedis.closed += 1


async def test_request_budgets_share_one_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[_FakeRedis] = []

    def _from_url(*_args: Any, **_kwargs: Any) -> _FakeRedis:
        made.append(_FakeRedis())
        return made[-1]

    monkeypatch.setattr("aida.outbound_clients.Redis.from_url", _from_url)
    _FakeRedis.closed = 0
    settings = Settings(_env_file=None)
    for _ in range(3):
        decision = await consume_window_budget(
            settings,
            namespace="test-budget",
            bucket="b",
            key_hash="k",
            limit=10,
            window_seconds=60,
            enabled=True,
        )
    assert len(made) == 1
    assert decision.used == 3 and decision.allowed
    # Not closed per request; closed once, at shutdown.
    assert _FakeRedis.closed == 0
    await outbound_clients.close_outbound_clients()
    assert _FakeRedis.closed == 1
