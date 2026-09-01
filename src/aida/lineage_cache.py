"""Optional Redis cache for bounded, value-free lineage responses."""

import json
from functools import lru_cache
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)


class LineageCache:
    def __init__(self, redis_url: str) -> None:
        self._client: Redis = Redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
        )

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            value = await self._client.get(key)
        except RedisError as exc:
            logger.warning("lineage_cache_read_failed", error=type(exc).__name__)
            return None
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        try:
            await self._client.set(
                key,
                json.dumps(value, sort_keys=True, separators=(",", ":")),
                ex=ttl_seconds,
            )
        except RedisError as exc:
            logger.warning("lineage_cache_write_failed", error=type(exc).__name__)


@lru_cache(maxsize=8)
def get_lineage_cache(redis_url: str) -> LineageCache:
    return LineageCache(redis_url)
