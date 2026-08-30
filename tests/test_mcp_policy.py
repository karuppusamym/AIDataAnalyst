"""
Unit tests for per-read policy evaluation (CX-3) and per-consumer rate limits (CX-6).

Exercises the pure, DB-free decision logic added to the MCP server and budget
module.  Follows the same test-without-database convention as
tests/test_mcp_server.py.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

from aida.mcp_budget import (
    McpBudgetDecision,
    _CONSUMER_BUCKET_MAP,
    _bucket_contract,
    _consumer_hash,
    _principal_hash,
    budget_headers,
)
from aida.mcp_server import (
    CATALOG_RESOURCE_READER_ROLES,
    _tool_role_eligible,
)
from aida.security_types import SecurityContext


def _ctx(
    roles: frozenset[str] | None = None,
    principal_id: str = "test-agent",
    principal_type: str = "AGENT",
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type=principal_type,
        organization_id=uuid4(),
        roles=roles or frozenset(),
    )


# ---------------------------------------------------------------------------
# CX-3: Catalog resource role-based access
# ---------------------------------------------------------------------------


def test_catalog_reader_roles_include_analyst() -> None:
    assert "Analyst" in CATALOG_RESOURCE_READER_ROLES


def test_catalog_reader_roles_include_viewer() -> None:
    assert "Viewer" in CATALOG_RESOURCE_READER_ROLES


def test_catalog_reader_roles_include_platform_admin() -> None:
    assert "PlatformAdmin" in CATALOG_RESOURCE_READER_ROLES


def test_catalog_read_allowed_for_analyst() -> None:
    assert _tool_role_eligible(
        frozenset({"Analyst"}), list(CATALOG_RESOURCE_READER_ROLES)
    ) is True


def test_catalog_read_allowed_for_viewer() -> None:
    assert _tool_role_eligible(
        frozenset({"Viewer"}), list(CATALOG_RESOURCE_READER_ROLES)
    ) is True


def test_catalog_read_allowed_for_platform_admin() -> None:
    assert _tool_role_eligible(
        frozenset({"PlatformAdmin"}), list(CATALOG_RESOURCE_READER_ROLES)
    ) is True


def test_catalog_read_denied_for_disjoint_roles() -> None:
    assert _tool_role_eligible(
        frozenset({"ToolConsumer"}), list(CATALOG_RESOURCE_READER_ROLES)
    ) is False


def test_catalog_read_denied_for_empty_roles() -> None:
    assert _tool_role_eligible(
        frozenset(), list(CATALOG_RESOURCE_READER_ROLES)
    ) is False


def test_catalog_read_allowed_with_multiple_roles_partial_overlap() -> None:
    assert _tool_role_eligible(
        frozenset({"ToolConsumer", "DataSteward"}), list(CATALOG_RESOURCE_READER_ROLES)
    ) is True


# ---------------------------------------------------------------------------
# CX-6: Per-consumer budget bucket mapping
# ---------------------------------------------------------------------------


def test_consumer_bucket_map_covers_all_org_buckets() -> None:
    """Every org-level bucket should have a per-consumer equivalent."""
    assert "REQUEST_MINUTE" in _CONSUMER_BUCKET_MAP
    assert "TOOL_DAY" in _CONSUMER_BUCKET_MAP
    assert "CONTEXT_DAY" in _CONSUMER_BUCKET_MAP


def test_consumer_bucket_map_values_are_distinct() -> None:
    values = list(_CONSUMER_BUCKET_MAP.values())
    assert len(values) == len(set(values))


def test_consumer_bucket_names_differ_from_org_names() -> None:
    for org_bucket, consumer_bucket in _CONSUMER_BUCKET_MAP.items():
        assert org_bucket != consumer_bucket


# ---------------------------------------------------------------------------
# CX-6: Budget contract settings for consumer buckets
# ---------------------------------------------------------------------------


def test_bucket_contract_consumer_request_minute() -> None:
    from aida.config import Settings

    s = Settings(
        identity_provider="development",
        mcp_consumer_requests_per_minute=50,
    )
    limit, window = _bucket_contract(s, "CONSUMER_REQUEST_MINUTE")
    assert limit == 50
    assert window == 60


def test_bucket_contract_consumer_tool_day() -> None:
    from aida.config import Settings

    s = Settings(
        identity_provider="development",
        mcp_consumer_tool_calls_per_day=300,
    )
    limit, window = _bucket_contract(s, "CONSUMER_TOOL_DAY")
    assert limit == 300
    assert window == 86_400


def test_bucket_contract_consumer_context_day() -> None:
    from aida.config import Settings

    s = Settings(
        identity_provider="development",
        mcp_consumer_context_reads_per_day=2000,
    )
    limit, window = _bucket_contract(s, "CONSUMER_CONTEXT_DAY")
    assert limit == 2000
    assert window == 86_400


def test_bucket_contract_unknown_raises() -> None:
    from aida.config import Settings

    import pytest

    s = Settings(identity_provider="development")
    with pytest.raises(ValueError, match="unknown MCP budget bucket"):
        _bucket_contract(s, "NONEXISTENT_BUCKET")


# ---------------------------------------------------------------------------
# CX-6: Hash functions
# ---------------------------------------------------------------------------


def test_principal_hash_is_deterministic() -> None:
    ctx = _ctx(roles=frozenset({"Analyst"}))
    assert _principal_hash(ctx) == _principal_hash(ctx)


def test_consumer_hash_differs_from_principal_hash() -> None:
    ctx = _ctx(roles=frozenset({"Analyst"}))
    assert _consumer_hash(ctx) != _principal_hash(ctx)


def test_consumer_hash_varies_by_principal_id() -> None:
    org_id = uuid4()
    ctx1 = SecurityContext(
        principal_id="agent-a", principal_type="AGENT",
        organization_id=org_id, roles=frozenset({"Analyst"}),
    )
    ctx2 = SecurityContext(
        principal_id="agent-b", principal_type="AGENT",
        organization_id=org_id, roles=frozenset({"Analyst"}),
    )
    assert _consumer_hash(ctx1) != _consumer_hash(ctx2)


# ---------------------------------------------------------------------------
# CX-6: Budget headers
# ---------------------------------------------------------------------------


def test_budget_headers_on_allowed_decision() -> None:
    decision = McpBudgetDecision(
        allowed=True, bucket="REQUEST_MINUTE", limit=120, used=5,
        retry_after_seconds=0,
    )
    headers = budget_headers(decision)
    assert headers["X-RateLimit-Limit"] == "120"
    assert headers["X-RateLimit-Remaining"] == "115"
    assert headers["X-RateLimit-Bucket"] == "REQUEST_MINUTE"
    assert "Retry-After" not in headers


def test_budget_headers_on_denied_decision() -> None:
    decision = McpBudgetDecision(
        allowed=False, bucket="CONSUMER_REQUEST_MINUTE", limit=30, used=31,
        retry_after_seconds=45,
    )
    headers = budget_headers(decision)
    assert headers["X-RateLimit-Limit"] == "30"
    assert headers["X-RateLimit-Remaining"] == "0"
    assert headers["Retry-After"] == "45"


def test_budget_headers_remaining_never_negative() -> None:
    decision = McpBudgetDecision(
        allowed=False, bucket="TOOL_DAY", limit=100, used=200,
        retry_after_seconds=10,
    )
    headers = budget_headers(decision)
    assert headers["X-RateLimit-Remaining"] == "0"


def test_budget_headers_retry_after_at_least_one() -> None:
    decision = McpBudgetDecision(
        allowed=False, bucket="CONTEXT_DAY", limit=50, used=51,
        retry_after_seconds=0,
    )
    headers = budget_headers(decision)
    assert headers["Retry-After"] == "1"
