import pytest

from aida.integration_catalog import (
    default_transformation_metadata_integrations,
    normalized_transformation_metadata_integrations,
    transformation_metadata_integration_enabled,
)
from aida.main import app
from aida.schemas import OrganizationIntegrationPolicyWrite


def test_transformation_metadata_integrations_default_closed() -> None:
    defaults = default_transformation_metadata_integrations()

    assert defaults == {
        "dbt": False,
        "openlineage": False,
        "airflow": False,
        "generic_elt": False,
        "bi": False,
    }
    assert transformation_metadata_integration_enabled(defaults, "dbt") is False


def test_transformation_metadata_integrations_reject_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unsupported transformation metadata integrations"):
        normalized_transformation_metadata_integrations({"dbt": True, "unknown": True})


def test_integration_policy_schema_normalizes_missing_keys() -> None:
    policy = OrganizationIntegrationPolicyWrite(
        transformation_metadata_integrations={"dbt": True, "generic_elt": True}
    )

    assert policy.transformation_metadata_integrations == {
        "dbt": True,
        "openlineage": False,
        "airflow": False,
        "generic_elt": True,
        "bi": False,
    }


def test_integration_policy_paths_are_published() -> None:
    paths = app.openapi()["paths"]
    schema = app.openapi()["components"]["schemas"]["OrganizationIntegrationPolicyRead"]

    assert "/v1/organizations/{organization_id}/integration-policy" in paths
    assert "transformation_metadata_integrations" in schema["properties"]
