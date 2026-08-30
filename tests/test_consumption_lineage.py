"""
Unit tests for the consumption lineage service (src/aida/consumption_lineage.py)
and its API schemas.

Follows the established project convention: pure-function and schema tests
without a database, matching the pattern in tests/test_mcp_server.py.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from pydantic import ValidationError

from aida.consumption_lineage import ConsumptionEdge
from aida.consumption_lineage_api import ConsumptionRecordPage, ConsumptionRecordRead

# ---------------------------------------------------------------------------
# ConsumptionEdge dataclass
# ---------------------------------------------------------------------------


def test_consumption_edge_creation() -> None:
    edge = ConsumptionEdge(
        consumer_id="agent-123",
        consumer_type="AGENT",
        resource_type="metadata_table",
        resource_id="abc-def",
        channel="MCP",
        correlation_id="corr-001",
        policy_decision="ALLOW",
    )
    assert edge.consumer_id == "agent-123"
    assert edge.consumer_type == "AGENT"
    assert edge.resource_type == "metadata_table"
    assert edge.resource_id == "abc-def"
    assert edge.channel == "MCP"
    assert edge.policy_decision == "ALLOW"
    assert edge.business_purpose is None
    assert edge.details is None


def test_consumption_edge_with_optional_fields() -> None:
    edge = ConsumptionEdge(
        consumer_id="user-456",
        consumer_type="USER",
        resource_type="context_product_version",
        resource_id="version-789",
        channel="REST",
        correlation_id="corr-002",
        policy_decision="ALLOW",
        business_purpose="quarterly_reporting",
        details={"product_key": "revenue_v2", "version": 3},
    )
    assert edge.business_purpose == "quarterly_reporting"
    assert edge.details == {"product_key": "revenue_v2", "version": 3}


def test_consumption_edge_to_dict() -> None:
    edge = ConsumptionEdge(
        consumer_id="svc-001",
        consumer_type="SERVICE_ACCOUNT",
        resource_type="metadata_table",
        resource_id="table-abc",
        channel="MCP",
        correlation_id="corr-003",
        policy_decision="ALLOW",
        business_purpose=None,
        details={"uri": "atlas://catalog/ds1/schema1/table1"},
    )
    d = edge.to_dict()
    assert isinstance(d, dict)
    assert d["consumer_id"] == "svc-001"
    assert d["resource_type"] == "metadata_table"
    assert d["channel"] == "MCP"
    assert d["details"] == {"uri": "atlas://catalog/ds1/schema1/table1"}


def test_consumption_edge_immutable() -> None:
    edge = ConsumptionEdge(
        consumer_id="a",
        consumer_type="AGENT",
        resource_type="t",
        resource_id="r",
        channel="MCP",
        correlation_id="c",
        policy_decision="ALLOW",
    )
    # `assert False` disappears under `python -O`, which would turn this into a test that
    # passes whether or not the dataclass is frozen.
    with pytest.raises(AttributeError):
        edge.consumer_id = "b"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


def test_consumption_record_read_schema() -> None:
    from datetime import datetime
    from uuid import uuid4

    record = ConsumptionRecordRead(
        id=uuid4(),
        organization_id=uuid4(),
        consumer_id="agent-a",
        consumer_type="AGENT",
        resource_type="metadata_table",
        resource_id=str(uuid4()),
        channel="MCP",
        correlation_id="corr-100",
        policy_decision="ALLOW",
        consumed_at=datetime.now(UTC),
    )
    assert record.consumer_id == "agent-a"
    assert record.details == {}
    assert record.business_purpose is None


def test_consumption_record_page_schema() -> None:
    page = ConsumptionRecordPage(items=[], total=0, limit=100, offset=0)
    assert page.total == 0
    assert len(page.items) == 0


def test_consumption_record_read_rejects_extra_fields() -> None:
    """ApiModel has extra='forbid' -- extra fields must be rejected."""
    from datetime import datetime
    from uuid import uuid4

    # Narrowed from a blind `Exception`: that would also pass on a TypeError from a
    # renamed field, which is the opposite of what this asserts.
    with pytest.raises(ValidationError):
        ConsumptionRecordRead(
            id=uuid4(),
            organization_id=uuid4(),
            consumer_id="a",
            consumer_type="AGENT",
            resource_type="t",
            resource_id="r",
            channel="MCP",
            correlation_id="c",
            policy_decision="ALLOW",
            consumed_at=datetime.now(UTC),
            unexpected_field="should fail",
        )
