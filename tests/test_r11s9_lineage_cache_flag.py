"""R11-S9: `lineage_cache_enabled` stays an opt-in, with the evidence it lacked.

The configuration triage kept this switch -- it needs the optional Redis
service, and a Redis error is a cache miss rather than a failure -- but no test
had ever exercised it, so "Opt-in" would have been a decision resting on
nothing. These pin the three behaviours the decision depends on: off never
touches the cache, a hit is served without building the graph, and a miss
builds and stores it for the configured time.
"""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aida import unified_lineage_service
from aida.config import Settings


class _Built(Exception):
    """Raised by the patched graph builder: the graph was built, not served."""


class _Cache:
    def __init__(self, cached: dict[str, Any] | None = None) -> None:
        self.cached = cached
        self.gets: list[str] = []
        self.sets: list[tuple[str, int]] = []

    async def get(self, key: str) -> dict[str, Any] | None:
        self.gets.append(key)
        return self.cached

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        self.sets.append((key, ttl))


def _datasource() -> SimpleNamespace:
    return SimpleNamespace(organization_id=uuid4(), id=uuid4())


def _patch(monkeypatch: pytest.MonkeyPatch, cache: _Cache, *, builds: bool) -> None:
    monkeypatch.setattr(unified_lineage_service, "get_lineage_cache", lambda _url: cache)

    async def _build(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        if not builds:
            raise _Built
        return SimpleNamespace(nodes={}, links=[], counts_by_source={}, truncation_reasons=[])

    monkeypatch.setattr(unified_lineage_service, "_build_unified_graph", _build)


async def test_the_cache_is_never_touched_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache(cached={"would": "be served"})
    _patch(monkeypatch, cache, builds=False)

    with pytest.raises(_Built):
        await unified_lineage_service.build_unified_lineage_graph_payload(
            None,  # type: ignore[arg-type]
            _datasource(),  # type: ignore[arg-type]
            settings=Settings(lineage_cache_enabled=False, _env_file=None),
        )

    assert (cache.gets, cache.sets) == ([], [])


async def test_a_cached_graph_is_served_without_building_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasource = _datasource()
    cached = unified_lineage_service.UnifiedLineageGraphRead(
        datasource_id=datasource.id,
        nodes=[],
        edges=[],
        counts_by_source={},
        returned_node_count=0,
        returned_edge_count=0,
        node_limit=300,
        edge_limit=1_500,
        truncated=False,
        truncation_reasons=[],
    ).model_dump(mode="json")
    cache = _Cache(cached=cached)
    _patch(monkeypatch, cache, builds=False)

    result = await unified_lineage_service.build_unified_lineage_graph_payload(
        None,  # type: ignore[arg-type]
        datasource,  # type: ignore[arg-type]
        settings=Settings(lineage_cache_enabled=True, _env_file=None),
    )

    assert result.datasource_id == datasource.id
    assert len(cache.gets) == 1 and cache.sets == []


async def test_a_miss_builds_the_graph_and_stores_it_for_the_configured_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache(cached=None)
    _patch(monkeypatch, cache, builds=True)
    settings = Settings(lineage_cache_enabled=True, _env_file=None)

    await unified_lineage_service.build_unified_lineage_graph_payload(
        None,  # type: ignore[arg-type]
        _datasource(),  # type: ignore[arg-type]
        settings=settings,
    )

    assert len(cache.gets) == 1
    assert [ttl for _, ttl in cache.sets] == [settings.lineage_cache_ttl_seconds]
