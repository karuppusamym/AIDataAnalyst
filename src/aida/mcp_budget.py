import hashlib
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from aida.config import Settings
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

_INCREMENT_WITH_EXPIRY = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


@dataclass(frozen=True, slots=True)
class McpBudgetDecision:
    allowed: bool
    bucket: str
    limit: int
    used: int
    retry_after_seconds: int
    degraded: bool = False


def _principal_hash(context: SecurityContext) -> str:
    value = f"{context.organization_id}:{context.principal_type}:{context.principal_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _consumer_hash(context: SecurityContext) -> str:
    """Hash that uniquely identifies a single consumer within an organization."""
    value = f"{context.organization_id}:consumer:{context.principal_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bucket_contract(settings: Settings, bucket: str) -> tuple[int, int]:
    if bucket == "REQUEST_MINUTE":
        return settings.mcp_requests_per_minute, 60
    if bucket == "TOOL_DAY":
        return settings.mcp_tool_calls_per_day, 86_400
    if bucket == "CONTEXT_DAY":
        return settings.mcp_context_reads_per_day, 86_400
    # Per-consumer buckets (CX-6)
    if bucket == "CONSUMER_REQUEST_MINUTE":
        return settings.mcp_consumer_requests_per_minute, 60
    if bucket == "CONSUMER_TOOL_DAY":
        return settings.mcp_consumer_tool_calls_per_day, 86_400
    if bucket == "CONSUMER_CONTEXT_DAY":
        return settings.mcp_consumer_context_reads_per_day, 86_400
    raise ValueError(f"unknown MCP budget bucket: {bucket}")


# Mapping from org-level buckets to per-consumer equivalents
_CONSUMER_BUCKET_MAP: dict[str, str] = {
    "REQUEST_MINUTE": "CONSUMER_REQUEST_MINUTE",
    "TOOL_DAY": "CONSUMER_TOOL_DAY",
    "CONTEXT_DAY": "CONSUMER_CONTEXT_DAY",
}


async def _check_bucket(
    settings: Settings,
    context: SecurityContext,
    bucket: str,
    key_hash: str,
) -> McpBudgetDecision:
    """Execute the atomic increment-with-expiry check for a single bucket."""
    limit, window_seconds = _bucket_contract(settings, bucket)
    if not settings.mcp_budget_enabled:
        return McpBudgetDecision(
            allowed=True,
            bucket=bucket,
            limit=limit,
            used=0,
            retry_after_seconds=0,
            degraded=False,
        )
    window = int(time.time()) // window_seconds
    key = f"aida:mcp-budget:{bucket}:{key_hash}:{window}"
    client: Redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
    )
    try:
        raw = await cast(
            Awaitable[list[object]],
            client.eval(_INCREMENT_WITH_EXPIRY, 1, key, str(window_seconds)),
        )
        used, ttl = int(str(raw[0])), max(int(str(raw[1])), 1)
        return McpBudgetDecision(
            allowed=used <= limit,
            bucket=bucket,
            limit=limit,
            used=used,
            retry_after_seconds=ttl if used > limit else 0,
        )
    except RedisError as exc:
        logger.warning("mcp_budget_store_unavailable", bucket=bucket, error=type(exc).__name__)
        fail_closed = settings.environment in {"staging", "production"}
        return McpBudgetDecision(
            allowed=not fail_closed,
            bucket=bucket,
            limit=limit,
            used=0,
            retry_after_seconds=30 if fail_closed else 0,
            degraded=True,
        )
    finally:
        await client.aclose()


async def consume_mcp_budget(
    settings: Settings,
    context: SecurityContext,
    bucket: str,
) -> McpBudgetDecision:
    """Check the org-level budget for the given bucket."""
    return await _check_bucket(settings, context, bucket, _principal_hash(context))


async def consume_mcp_consumer_budget(
    settings: Settings,
    context: SecurityContext,
    bucket: str,
) -> McpBudgetDecision:
    """Check the per-consumer budget for the given bucket.

    ``bucket`` should be an org-level bucket name (e.g. ``REQUEST_MINUTE``);
    it is automatically mapped to the consumer-level equivalent.
    """
    consumer_bucket = _CONSUMER_BUCKET_MAP.get(bucket)
    if consumer_bucket is None:
        raise ValueError(f"no consumer-level equivalent for bucket: {bucket}")
    return await _check_bucket(settings, context, consumer_bucket, _consumer_hash(context))


def budget_headers(decision: McpBudgetDecision) -> dict[str, str]:
    """Return standard rate-limit response headers for the given decision."""
    remaining = max(decision.limit - decision.used, 0)
    headers: dict[str, str] = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Bucket": decision.bucket,
    }
    if not decision.allowed:
        headers["Retry-After"] = str(max(decision.retry_after_seconds, 1))
    return headers
