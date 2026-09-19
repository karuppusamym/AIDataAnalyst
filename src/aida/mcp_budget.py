"""MCP rate budgets: per principal and per consumer, per minute and per day.

The counting itself is `aida.request_budget` (a Redis fixed window, fail-closed in staging and
production), shared with the GraphQL surface; this module keeps MCP's buckets and their limits.
"""

import hashlib

from aida.config import Settings
from aida.request_budget import (
    BudgetDecision,
    budget_headers,
    consume_window_budget,
    principal_hash,
)
from aida.security import SecurityContext

#: MCP's name for the shared decision type, kept for its callers.
McpBudgetDecision = BudgetDecision

__all__ = [
    "McpBudgetDecision",
    "budget_headers",
    "consume_mcp_budget",
    "consume_mcp_consumer_budget",
]


def _principal_hash(context: SecurityContext) -> str:
    return principal_hash(context)


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
    return await consume_window_budget(
        settings,
        namespace="mcp-budget",
        bucket=bucket,
        key_hash=key_hash,
        limit=limit,
        window_seconds=window_seconds,
        enabled=settings.mcp_budget_enabled,
    )


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
