from aida.main import app
from aida.projectors.outbox_publisher import retry_delay_seconds


def test_outbox_retry_backoff_is_exponential_and_capped() -> None:
    assert retry_delay_seconds(1, 300) == 2
    assert retry_delay_seconds(4, 300) == 16
    assert retry_delay_seconds(20, 300) == 300


def test_outbox_retry_backoff_defends_invalid_attempt_count() -> None:
    assert retry_delay_seconds(0, 300) == 2


def test_outbox_exception_inventory_is_published_without_payload_schema() -> None:
    operation = app.openapi()["paths"]["/v1/organizations/{organization_id}/outbox-events"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Page"
    }
    outbox_fields = app.openapi()["components"]["schemas"]["OutboxEventRead"]["properties"]
    assert "payload" not in outbox_fields


def test_tenant_fleet_inventory_avoids_hierarchy_fanout() -> None:
    paths = app.openapi()["paths"]
    assert "get" in paths["/v1/organizations/{organization_id}/projects"]
    assert "get" in paths["/v1/organizations/{organization_id}/datasources"]
