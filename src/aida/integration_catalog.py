from collections.abc import Mapping

TRANSFORMATION_METADATA_INTEGRATION_KEYS = (
    "dbt",
    "openlineage",
    "airflow",
    "generic_elt",
    "bi",
)


def default_transformation_metadata_integrations() -> dict[str, bool]:
    return {
        "dbt": False,
        "openlineage": False,
        "airflow": False,
        "generic_elt": False,
        "bi": False,
    }


def normalized_transformation_metadata_integrations(
    values: Mapping[str, object] | None,
) -> dict[str, bool]:
    normalized = default_transformation_metadata_integrations()
    if values is None:
        return normalized
    unexpected = sorted(set(values) - set(TRANSFORMATION_METADATA_INTEGRATION_KEYS))
    if unexpected:
        joined = ", ".join(unexpected)
        raise ValueError(f"unsupported transformation metadata integrations: {joined}")
    for key in TRANSFORMATION_METADATA_INTEGRATION_KEYS:
        if key in values:
            normalized[key] = bool(values[key])
    return normalized


def transformation_metadata_integration_enabled(
    values: Mapping[str, object] | None,
    key: str,
) -> bool:
    return normalized_transformation_metadata_integrations(values).get(key, False)
