"""R11-MP25: a per-source query bound held in Redis across replicas.

The unit tests drive the slot logic against an in-memory stand-in for the one
Lua script; the live tests (set `AIDA_SOURCE_SLOTS_TEST_REDIS_URL`) prove the
script itself against a real Redis: atomic under a race, and a slot a dead
replica never released expires.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

import aida.source_concurrency as source_concurrency
from aida.config import Settings
from aida.query_gateway import QueryRejected, SourceConcurrencyRejected
from aida.source_concurrency import (
    SourceConcurrencyDenied,
    source_limit,
    source_query_slot,
)

pytestmark = pytest.mark.asyncio


class _Slots:
    """The acquire script's semantics, with a clock the test moves."""

    def __init__(self) -> None:
        self.now_ms = 1_000_000
        self.sets: dict[str, dict[str, int]] = {}

    async def eval(self, _script: str, _keys: int, key: str, *args: str) -> int:
        limit, lease_ms, token = int(args[0]), int(args[1]), args[2]
        members = {m: exp for m, exp in self.sets.get(key, {}).items() if exp > self.now_ms}
        self.sets[key] = members
        if len(members) < limit:
            members[token] = self.now_ms + lease_ms
            return 1
        return 0

    async def zrem(self, key: str, token: str) -> int:
        return 1 if self.sets.get(key, {}).pop(token, None) is not None else 0

    def held(self, key: str) -> int:
        return sum(1 for exp in self.sets.get(key, {}).values() if exp > self.now_ms)


class _Down:
    async def eval(self, *_args: Any) -> int:
        raise RedisConnectionError("unreachable")


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "source_query_concurrency_enabled": True,
        "source_query_max_concurrent": 2,
        "source_query_queue_timeout_seconds": 0.3,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def slots(monkeypatch: pytest.MonkeyPatch) -> _Slots:
    store = _Slots()
    monkeypatch.setattr(source_concurrency, "shared_redis", lambda _url: store)
    return store


async def test_off_by_default_and_never_touches_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never(_url: str) -> Any:
        raise AssertionError("a disabled bound must not reach Redis")

    monkeypatch.setattr(source_concurrency, "shared_redis", _never)
    async with source_query_slot(Settings(_env_file=None), uuid4()):
        pass


async def test_the_limit_holds_and_a_query_past_it_is_refused_after_the_wait(
    slots: _Slots,
) -> None:
    source = uuid4()
    settings = _settings()
    async with source_query_slot(settings, source), source_query_slot(settings, source):
        assert slots.held(f"aida:source-slots:{source}") == 2
        with pytest.raises(SourceConcurrencyDenied) as denied:
            async with source_query_slot(settings, source):
                pytest.fail("a third query ran against a limit of two")
        assert denied.value.limit == 2 and denied.value.waited_seconds >= 0.3
        # Another source is not affected.
        async with source_query_slot(settings, uuid4()):
            pass
    assert slots.held(f"aida:source-slots:{source}") == 0


async def test_a_waiting_query_takes_the_slot_a_finished_one_frees(slots: _Slots) -> None:
    source = uuid4()
    settings = _settings(source_query_max_concurrent=1, source_query_queue_timeout_seconds=2)
    order: list[str] = []

    async def first() -> None:
        async with source_query_slot(settings, source):
            order.append("first-in")
            await asyncio.sleep(0.15)
            order.append("first-out")

    async def second() -> None:
        await asyncio.sleep(0.02)
        async with source_query_slot(settings, source):
            order.append("second-in")

    await asyncio.gather(first(), second())
    assert order == ["first-in", "first-out", "second-in"]


async def test_a_slot_a_dead_replica_never_released_expires(slots: _Slots) -> None:
    source = uuid4()
    settings = _settings(source_query_max_concurrent=1, query_timeout_seconds=10)
    key = f"aida:source-slots:{source}"
    # A replica took the slot and died: nothing releases it.
    assert await source_concurrency._try_acquire(settings, key, 1, "dead-replica")
    with pytest.raises(SourceConcurrencyDenied):
        async with source_query_slot(settings, source):
            pass
    slots.now_ms += (10 + source_concurrency.LEASE_MARGIN_SECONDS) * 1000 + 1
    async with source_query_slot(settings, source):
        assert slots.held(key) == 1


async def test_an_override_sets_one_source_limit() -> None:
    source = uuid4()
    settings = _settings(source_query_max_concurrent_overrides={str(source): 7})
    assert source_limit(settings, source) == 7
    assert source_limit(settings, uuid4()) == 2


@pytest.mark.parametrize(("environment", "runs"), [("development", True), ("production", False)])
async def test_an_unreachable_store_refuses_only_where_it_must(
    monkeypatch: pytest.MonkeyPatch, environment: str, runs: bool
) -> None:
    monkeypatch.setattr(source_concurrency, "shared_redis", lambda _url: _Down())
    # A copy, not a constructed production Settings: that needs a real identity provider.
    settings = _settings().model_copy(update={"environment": environment})
    ran = False
    if runs:
        async with source_query_slot(settings, uuid4()):
            ran = True
        assert ran
    else:
        with pytest.raises(SourceConcurrencyDenied) as denied:
            async with source_query_slot(settings, uuid4()):
                pytest.fail("ran without a countable bound in production")
        assert denied.value.unavailable


async def test_the_gateway_refusal_is_an_ordinary_rejection() -> None:
    source = uuid4()
    rejected = SourceConcurrencyRejected(
        SourceConcurrencyDenied(source, limit=2, waited_seconds=5.0)
    )
    assert isinstance(rejected, QueryRejected)
    assert str(rejected) == f"SOURCE_CONCURRENCY_LIMIT_EXCEEDED:{source}"
    unavailable = SourceConcurrencyRejected(
        SourceConcurrencyDenied(source, limit=2, waited_seconds=0.0, unavailable=True)
    )
    assert str(unavailable) == f"SOURCE_CONCURRENCY_UNAVAILABLE:{source}"


# ---------------------------------------------------------------------------
# Against a real Redis
# ---------------------------------------------------------------------------

_LIVE_URL = os.environ.get("AIDA_SOURCE_SLOTS_TEST_REDIS_URL")
live = pytest.mark.skipif(not _LIVE_URL, reason="set AIDA_SOURCE_SLOTS_TEST_REDIS_URL")


@live
async def test_live_the_script_is_atomic_under_a_race() -> None:
    assert _LIVE_URL is not None
    source = uuid4()
    settings = _settings(
        redis_url=_LIVE_URL, source_query_max_concurrent=3, source_query_queue_timeout_seconds=10
    )
    in_flight = 0
    peak = 0

    async def query() -> None:
        nonlocal in_flight, peak
        async with source_query_slot(settings, source):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.03)
            in_flight -= 1

    await asyncio.gather(*(query() for _ in range(24)))
    assert peak == 3
    client = Redis.from_url(_LIVE_URL)
    try:
        assert await client.zcard(f"aida:source-slots:{source}") == 0
    finally:
        await client.aclose()


@live
async def test_live_an_unreleased_slot_expires_with_its_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _LIVE_URL is not None
    monkeypatch.setattr(source_concurrency, "LEASE_MARGIN_SECONDS", 0)
    source = uuid4()
    settings = _settings(
        redis_url=_LIVE_URL, source_query_max_concurrent=1, query_timeout_seconds=1
    )
    key = f"aida:source-slots:{source}"
    assert await source_concurrency._try_acquire(settings, key, 1, "dead-replica")
    assert not await source_concurrency._try_acquire(settings, key, 1, "other")
    await asyncio.sleep(1.2)
    assert await source_concurrency._try_acquire(settings, key, 1, "other")


# ---------------------------------------------------------------------------
# In the real gateway
# ---------------------------------------------------------------------------


async def test_the_gateway_bounds_one_source_and_leaves_its_neighbour_alone(
    slots: _Slots, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aida.query_gateway import QueryExecutionGateway
    from tests.support.doubles import security_context
    from tests.test_lob_concurrency import _catalog_session, _datasource, _SlowFakeSqlExecutor

    settings = _settings(source_query_max_concurrent=1, source_query_queue_timeout_seconds=0.1)
    gateway = QueryExecutionGateway(settings)
    lob = uuid4()  # one line of business: only the source bound can tell them apart
    busy = _datasource(line_of_business_id=lob, credential_reference="vault://busy")
    quiet = _datasource(line_of_business_id=lob, credential_reference="vault://quiet")
    executors = {
        "vault://busy": _SlowFakeSqlExecutor(({"customer_id": "C-1"},), delay_seconds=0.4),
        "vault://quiet": _SlowFakeSqlExecutor(({"customer_id": "C-1"},), delay_seconds=0.02),
    }
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session", lambda _type, dsn: executors[dsn]
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda _settings: type("_R", (), {"resolve": staticmethod(lambda ref: ref)})(),
    )
    outcomes: dict[str, list[str]] = {"busy": [], "quiet": []}

    async def run(name: str, datasource: Any) -> None:
        try:
            await gateway.execute(
                _catalog_session(),
                datasource=datasource,
                context=security_context(organization_id=datasource.organization_id),
                correlation_id=f"corr-{uuid4()}",
                sql="SELECT customer_id FROM analytics.customers",
                requested_limit=10,
                semantic_version=None,
            )
            outcomes[name].append("ran")
        except SourceConcurrencyRejected as exc:
            outcomes[name].append(str(exc).split(":")[0])

    await asyncio.gather(
        *(run("busy", busy) for _ in range(3)), asyncio.sleep(0.05), run("quiet", quiet)
    )
    assert sorted(outcomes["busy"]) == ["SOURCE_CONCURRENCY_LIMIT_EXCEEDED"] * 2 + ["ran"]
    assert outcomes["quiet"] == ["ran"]
