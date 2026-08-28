from datetime import UTC, datetime
from uuid import uuid4

from aida.ai_governance_api import _configuration_fingerprint, _route_read
from aida.config import Settings
from aida.main import app
from aida.models import ModelRouteConfiguration
from aida.schemas import ModelRouteConfigurationCreate, ModelRouteConfigurationRead


def model_route_body(**updates: object) -> ModelRouteConfigurationCreate:
    values: dict[str, object] = {
        "route_key": "bank-sql-primary",
        "display_name": "Bank SQL generation",
        "provider_type": "ON_PREM",
        "model_id": "approved-model-1",
        "endpoint_alias": "private-ai-east-01",
        "credential_reference": "env://AIDA_LOCAL_MODEL_KEY",
        "data_residency": "US",
        "retention_policy": "ZERO_RETENTION",
        "capabilities": ["SQL_GENERATION"],
        "max_input_tokens": 8000,
        "max_output_tokens": 2000,
        "timeout_seconds": 30,
    }
    values.update(updates)
    return ModelRouteConfigurationCreate.model_validate(values)


def test_model_route_fingerprint_covers_governed_definition() -> None:
    baseline = model_route_body()
    changed = model_route_body(model_id="approved-model-2")

    assert _configuration_fingerprint(baseline) == _configuration_fingerprint(baseline)
    assert _configuration_fingerprint(baseline) != _configuration_fingerprint(changed)


def test_model_route_read_hides_credential_reference_and_fails_closed() -> None:
    now = datetime.now(UTC)
    route = ModelRouteConfiguration(
        id=uuid4(),
        organization_id=uuid4(),
        route_key="bank-sql-primary",
        version=1,
        status="APPROVED",
        display_name="Bank SQL generation",
        provider_type="ON_PREM",
        model_id="approved-model-1",
        endpoint_alias="private-ai-east-01",
        credential_reference="env://AIDA_LOCAL_MODEL_KEY",
        data_residency="US",
        retention_policy="ZERO_RETENTION",
        capabilities=["SQL_GENERATION"],
        max_input_tokens=8000,
        max_output_tokens=2000,
        timeout_seconds=30,
        fingerprint="a" * 64,
        created_by="maker",
        approved_by="checker",
        approved_at=now,
        created_at=now,
        updated_at=now,
    )
    result = _route_read(
        route,
        Settings(
            model_generation_enabled=True,
            model_route="bank-sql-primary",
            _env_file=None,
        ),
    )

    assert result.uses_credential_reference is True
    assert result.activation_status == "ADAPTER_REGISTRATION_REQUIRED"
    assert result.adapter_available is False
    assert (
        "credential_reference" not in ModelRouteConfigurationRead.model_json_schema()["properties"]
    )


def test_graph_and_model_route_paths_are_published() -> None:
    paths = app.openapi()["paths"]

    assert "/v1/datasources/{datasource_id}/knowledge-graph" in paths
    assert "/v1/organizations/{organization_id}/model-routes" in paths
    assert "/v1/model-routes/{route_id}/submit" in paths
