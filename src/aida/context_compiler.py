import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aida.models import ContextProduct, ContextProductVersion
from aida.platform_schemas import ContextCompilationRead, ContextCompilerTarget


@dataclass(frozen=True, slots=True)
class ResolvedTableReference:
    table_id: str
    qualified_name: str


def _canonical_json(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
        indent=2 if pretty else None,
        ensure_ascii=True,
    )


def _artifact_payload(
    product: ContextProduct,
    version: ContextProductVersion,
    target: ContextCompilerTarget,
    tables: list[ResolvedTableReference],
) -> dict[str, Any]:
    references = {
        "tables": [
            {"id": table.table_id, "qualified_name": table.qualified_name} for table in tables
        ],
        "semantic_model_version_ids": sorted(version.semantic_model_version_ids),
        "glossary_term_version_ids": sorted(version.glossary_term_version_ids),
        "eligible_tool_version_ids": sorted(version.eligible_tool_version_ids),
    }
    common = {
        "product_key": product.product_key,
        "version": version.version,
        "fingerprint": version.fingerprint,
        "name": version.name,
        "description": version.description,
        "purpose": version.purpose,
        "owner": version.owner_principal,
        "references": references,
        "quality_requirements": version.quality_requirements,
        "policy_summary": version.policy_summary,
    }
    if target == "MCP":
        return {
            "kind": "AtlasMcpContext",
            "apiVersion": "atlas.aida/v1",
            "resourceUri": (
                f"atlas://context-products/{product.product_key}/versions/{version.version}"
            ),
            "prompt": f"atlas__context__{product.product_key}__v{version.version}",
            "eligibleToolVersionIds": references["eligible_tool_version_ids"],
            "context": common,
        }
    if target == "REST":
        return {
            "kind": "AtlasRestContext",
            "apiVersion": "atlas.aida/v1",
            "resource": f"/v1/context-product-versions/{version.id}",
            "etag": version.fingerprint,
            "context": common,
        }
    if target == "OSI":
        return {
            "specification": "OpenSemanticInterchange",
            "specificationVersion": "1.0",
            "semanticContext": common,
        }
    if target == "ODCS":
        return {
            "apiVersion": "v3.1.0",
            "kind": "DataContract",
            "id": product.product_key,
            "version": str(version.version),
            "status": "active",
            "description": {"purpose": version.purpose, "usage": version.description},
            "team": [{"username": version.owner_principal, "role": "Owner"}],
            "servers": [
                {
                    "server": table.qualified_name,
                    "type": "data-platform",
                    "tableId": table.table_id,
                }
                for table in tables
            ],
            "customProperties": [
                {"property": "atlasContextFingerprint", "value": version.fingerprint},
                {"property": "atlasPolicy", "value": version.policy_summary},
            ],
        }
    if target == "SNOWFLAKE_SEMANTIC_VIEW":
        return {
            "kind": "SnowflakeSemanticViewSpec",
            "specVersion": "1",
            "name": product.product_key,
            "comment": version.description,
            "tables": [
                {
                    "logicalName": f"table_{index + 1}",
                    "physicalName": table.qualified_name,
                    "atlasTableId": table.table_id,
                }
                for index, table in enumerate(tables)
            ],
            "semanticModelVersionIds": references["semantic_model_version_ids"],
            "governance": {
                "fingerprint": version.fingerprint,
                "purpose": version.purpose,
                "sourceValues": "GATEWAY_ONLY",
            },
        }
    if target == "DATABRICKS_METRIC_VIEW":
        return {
            "version": "1.1",
            "kind": "DatabricksMetricViewSpec",
            "name": product.product_key,
            "sourceTables": [
                {
                    "name": table.qualified_name,
                    "atlasTableId": table.table_id,
                }
                for table in tables
            ],
            "semanticModelVersionIds": references["semantic_model_version_ids"],
            "comment": version.description,
            "governance": {
                "fingerprint": version.fingerprint,
                "purpose": version.purpose,
                "sourceValues": "GATEWAY_ONLY",
            },
        }
    return {"apiVersion": "atlas.aida/v1", "kind": "ContextProduct", "spec": common}


def compile_context_product(
    product: ContextProduct,
    version: ContextProductVersion,
    target: ContextCompilerTarget,
    tables: list[ResolvedTableReference],
) -> ContextCompilationRead:
    """Compile a version-pinned product without time- or environment-dependent fields."""
    payload = _artifact_payload(
        product, version, target, sorted(tables, key=lambda item: item.table_id)
    )
    content = _canonical_json(payload)
    artifact_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ContextCompilationRead(
        target=target,
        content_type="application/yaml" if target == "YAML" else "application/json",
        content=content,
        artifact_hash=artifact_hash,
        source_fingerprint=version.fingerprint,
        generated_from={
            "context_product_id": str(product.id),
            "context_product_version_id": str(version.id),
            "product_key": product.product_key,
            "version": version.version,
        },
    )


def compilation_drift_paths(expected_content: str, deployed_content: str) -> list[str]:
    try:
        expected = json.loads(expected_content)
        deployed = json.loads(deployed_content)
    except json.JSONDecodeError:
        return [] if expected_content == deployed_content else ["$content"]

    changed: list[str] = []

    def compare(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            changed.append(path)
            return
        if isinstance(left, dict):
            for key in sorted(left.keys() | right.keys()):
                child_path = f"{path}.{key}"
                if key not in left or key not in right:
                    changed.append(child_path)
                else:
                    compare(left[key], right[key], child_path)
            return
        if isinstance(left, list):
            if left != right:
                changed.append(path)
            return
        if left != right:
            changed.append(path)

    compare(expected, deployed, "$")
    return changed[:1000]
