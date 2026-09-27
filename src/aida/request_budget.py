"""A fixed-window request budget in Redis, shared by the MCP and GraphQL surfaces.

One counter per (namespace, bucket, caller, window), incremented and given its expiry in one
Lua call so two requests cannot both see the window as fresh. The caller hash is chosen by the
surface: MCP counts per principal and per consumer, GraphQL per principal.

**When the store is unreachable.** Staging and production fail closed -- a budget that cannot be
counted is not granted -- and every other environment fails open, so a developer without Redis
is not locked out. Either way the decision says it was `degraded`, and a warning is logged.

Extracted from `aida.mcp_budget` (R11-GQL01, per-caller rate budgets for GraphQL): the MCP keys,
log event and fail-closed rule are unchanged -- `namespace="mcp-budget"` produces exactly the
keys and the `mcp_budget_store_unavailable` event it always did.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import cast

import structlog
from redis.exceptions import RedisError

from aida.outbound_clients import shared_redis
from aida.security_types import SecurityContext
from atlas.platform.config import Settings

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
class BudgetDecision:
    allowed: bool
    bucket: str
    limit: int
    used: int
    retry_after_seconds: int
    degraded: bool = False


def principal_hash(context: SecurityContext) -> str:
    """One caller within one organization, as a digest: keys never carry a principal id."""
    value = f"{context.organization_id}:{context.principal_type}:{context.principal_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def consume_window_budget(
    settings: Settings,
    *,
    namespace: str,
    bucket: str,
    key_hash: str,
    limit: int,
    window_seconds: int,
    enabled: bool,
) -> BudgetDecision:
    """Count one request against a fixed window; allowed while the count is within `limit`."""
    if not enabled:
        return BudgetDecision(
            allowed=True, bucket=bucket, limit=limit, used=0, retry_after_seconds=0
        )
    window = int(time.time()) // window_seconds
    key = f"aida:{namespace}:{bucket}:{key_hash}:{window}"
    try:
        client = shared_redis(settings.redis_url)
        raw = await cast(
            Awaitable[list[object]],
            client.eval(_INCREMENT_WITH_EXPIRY, 1, key, str(window_seconds)),
        )
        used, ttl = int(str(raw[0])), max(int(str(raw[1])), 1)
        return BudgetDecision(
            allowed=used <= limit,
            bucket=bucket,
            limit=limit,
            used=used,
            retry_after_seconds=ttl if used > limit else 0,
        )
    except RedisError as exc:
        logger.warning(
            f"{namespace.replace('-', '_')}_store_unavailable",
            bucket=bucket,
            error=type(exc).__name__,
        )
        fail_closed = settings.environment in {"staging", "production"}
        return BudgetDecision(
            allowed=not fail_closed,
            bucket=bucket,
            limit=limit,
            used=0,
            retry_after_seconds=30 if fail_closed else 0,
            degraded=True,
        )


def budget_headers(decision: BudgetDecision) -> dict[str, str]:
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
