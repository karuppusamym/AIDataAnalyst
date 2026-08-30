import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import ParseError

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_RESOURCES = 25_000
MAX_EDGES = 100_000
MAX_COMPILED_SQL_CHARS = 2_000_000
SUPPORTED_RESOURCE_TYPES = frozenset(
    {
        "analysis",
        "exposure",
        "metric",
        "model",
        "saved_query",
        "seed",
        "semantic_model",
        "snapshot",
        "source",
        "test",
    }
)


class DbtArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedDbtTestResult:
    unique_id: str
    status: str
    failures: int | None = None
    message: str | None = None
    execution_time: float | None = None


@dataclass(frozen=True)
class ParsedDbtResource:
    unique_id: str
    resource_type: str
    package_name: str
    name: str
    database_name: str | None
    schema_name: str | None
    relation_name: str | None
    materialization: str | None
    original_file_path: str | None
    description: str | None
    compiled_sql_hash: str | None
    compiled_sql_redacted: str | None
    sql_parse_status: str
    column_names: list[str]
    tags: list[str]
    depends_on_unique_ids: list[str]
    column_descriptions: dict[str, str] = field(default_factory=dict)
    column_types: dict[str, str] = field(default_factory=dict)
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDbtArtifact:
    fingerprint: str
    dbt_schema_version: str
    dbt_version: str | None
    invocation_id: str | None
    generated_at: datetime | None
    resources: list[ParsedDbtResource]
    edges: list[tuple[str, str]]


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _required_text(value: Any, field: str, limit: int) -> str:
    text = _optional_text(value, limit)
    if not text:
        raise DbtArtifactError(f"dbt resource {field} is required")
    return text


def _parse_generated_at(value: Any) -> datetime | None:
    text = _optional_text(value, 100)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _redact_compiled_sql(sql: str | None, dialect: str) -> tuple[str | None, str | None, str]:
    if not sql or not sql.strip():
        return None, None, "NOT_PRESENT"
    fingerprint = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    if len(sql) > MAX_COMPILED_SQL_CHARS:
        return fingerprint, None, "TOO_LARGE"
    try:
        statements = parse(sql, read=dialect)
        redacted = []
        for statement in statements:
            if statement is None:
                continue
            safe_statement = statement.transform(
                lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node
            )
            redacted.append(safe_statement.sql(dialect=dialect, comments=False, pretty=True))
        return fingerprint, ";\n\n".join(redacted), "PARSED"
    except (ParseError, ValueError):
        return fingerprint, None, "UNPARSEABLE"


def _resource_from_manifest(
    unique_id: str,
    payload: dict[str, Any],
    dialect: str,
) -> ParsedDbtResource:
    resource_type = _required_text(payload.get("resource_type"), "resource_type", 30).lower()
    if resource_type not in SUPPORTED_RESOURCE_TYPES:
        raise DbtArtifactError(f"unsupported dbt resource type: {resource_type}")
    raw_config = payload.get("config")
    config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
    raw_depends_on = payload.get("depends_on")
    depends_on: dict[str, Any] = raw_depends_on if isinstance(raw_depends_on, dict) else {}
    raw_dependency_nodes = depends_on.get("nodes")
    dependency_nodes: list[Any] = (
        raw_dependency_nodes if isinstance(raw_dependency_nodes, list) else []
    )
    raw_columns = payload.get("columns")
    columns: dict[str, Any] = raw_columns if isinstance(raw_columns, dict) else {}
    raw_tags = payload.get("tags")
    tags: list[Any] = raw_tags if isinstance(raw_tags, list) else []
    compiled_sql = payload.get("compiled_code") or payload.get("compiled_sql")
    sql_hash, redacted_sql, parse_status = _redact_compiled_sql(
        str(compiled_sql) if compiled_sql is not None else None,
        dialect,
    )
    unique_id_parts = unique_id.split(".", 2)
    package_name = payload.get("package_name") or (
        unique_id_parts[1] if len(unique_id_parts) > 1 else "unknown"
    )
    physical_name = payload.get("alias") or payload.get("identifier") or payload.get("name")

    column_names: list[str] = []
    column_descriptions: dict[str, str] = {}
    column_types: dict[str, str] = {}
    for raw_name, raw_col in list(columns.items())[:2000]:
        col_name = str(raw_name)[:255]
        column_names.append(col_name)
        if isinstance(raw_col, dict):
            desc = _optional_text(raw_col.get("description"), 4000)
            if desc:
                column_descriptions[col_name] = desc
            dtype = _optional_text(raw_col.get("data_type") or raw_col.get("type"), 255)
            if dtype:
                column_types[col_name] = dtype

    extra_metadata: dict[str, Any] = {}
    if resource_type == "exposure":
        owner = payload.get("owner")
        if isinstance(owner, dict):
            if owner.get("name"):
                extra_metadata["owner_name"] = _optional_text(owner.get("name"), 255)
            if owner.get("email"):
                extra_metadata["owner_email"] = _optional_text(owner.get("email"), 255)
        if payload.get("url"):
            extra_metadata["url"] = _optional_text(payload.get("url"), 1000)
        if payload.get("maturity"):
            extra_metadata["maturity"] = _optional_text(payload.get("maturity"), 50)
        if payload.get("type"):
            extra_metadata["exposure_type"] = _optional_text(payload.get("type"), 50)
    elif resource_type in {"semantic_model", "metric"}:
        if payload.get("type"):
            extra_metadata["type"] = _optional_text(payload.get("type"), 100)
        if payload.get("label"):
            extra_metadata["label"] = _optional_text(payload.get("label"), 255)

    return ParsedDbtResource(
        unique_id=_required_text(unique_id, "unique_id", 500),
        resource_type=resource_type.upper(),
        package_name=_required_text(package_name, "package_name", 255),
        name=_required_text(physical_name, "name", 255),
        database_name=_optional_text(payload.get("database"), 255),
        schema_name=_optional_text(payload.get("schema"), 255),
        relation_name=_optional_text(payload.get("relation_name"), 1000),
        materialization=_optional_text(config.get("materialized"), 100),
        original_file_path=_optional_text(payload.get("original_file_path"), 1000),
        description=_optional_text(payload.get("description"), 4000),
        compiled_sql_hash=sql_hash,
        compiled_sql_redacted=redacted_sql,
        sql_parse_status=parse_status,
        column_names=column_names,
        tags=[str(tag)[:100] for tag in tags][:100],
        depends_on_unique_ids=[str(node)[:500] for node in dependency_nodes][:5000],
        column_descriptions=column_descriptions,
        column_types=column_types,
        extra_metadata=extra_metadata,
    )


def parse_dbt_manifest(manifest: dict[str, Any], dialect: str) -> ParsedDbtArtifact:
    """Parse a dbt manifest into bounded, value-safe resources and lineage edges."""
    try:
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise DbtArtifactError("dbt manifest must be valid JSON data") from exc
    if len(canonical.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise DbtArtifactError("dbt manifest exceeds the 32 MiB ingestion limit")
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise DbtArtifactError("dbt manifest metadata is required")
    schema_version = _required_text(metadata.get("dbt_schema_version"), "schema version", 255)
    collections: list[dict[str, Any]] = []
    for key in ("nodes", "sources", "exposures", "metrics", "semantic_models", "saved_queries"):
        value = manifest.get(key, {})
        if not isinstance(value, dict):
            raise DbtArtifactError(f"dbt manifest {key} must be an object")
        collections.append(value)
    total = sum(len(collection) for collection in collections)
    if total > MAX_RESOURCES:
        raise DbtArtifactError(f"dbt manifest exceeds the {MAX_RESOURCES} resource limit")
    resources: list[ParsedDbtResource] = []
    for collection in collections:
        for unique_id, raw_resource in collection.items():
            if not isinstance(raw_resource, dict):
                raise DbtArtifactError(f"dbt resource {unique_id} must be an object")
            resource_type = str(raw_resource.get("resource_type", "")).lower()
            if resource_type not in SUPPORTED_RESOURCE_TYPES:
                continue
            resources.append(_resource_from_manifest(str(unique_id), raw_resource, dialect))
    known_ids = {resource.unique_id for resource in resources}
    edges = [
        (dependency_id, resource.unique_id)
        for resource in resources
        for dependency_id in resource.depends_on_unique_ids
        if dependency_id in known_ids and dependency_id != resource.unique_id
    ]
    edges = list(dict.fromkeys(edges))
    if len(edges) > MAX_EDGES:
        raise DbtArtifactError(f"dbt manifest exceeds the {MAX_EDGES} lineage edge limit")
    return ParsedDbtArtifact(
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        dbt_schema_version=schema_version,
        dbt_version=_optional_text(metadata.get("dbt_version"), 50),
        invocation_id=_optional_text(metadata.get("invocation_id"), 255),
        generated_at=_parse_generated_at(metadata.get("generated_at")),
        resources=resources,
        edges=edges,
    )


def parse_dbt_catalog(catalog: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Parse a dbt catalog.json mapping unique_id -> {column_name: physical_data_type}."""
    if not isinstance(catalog, dict):
        raise DbtArtifactError("dbt catalog must be a valid JSON object")
    result: dict[str, dict[str, str]] = {}
    for key in ("nodes", "sources"):
        collection = catalog.get(key, {})
        if not isinstance(collection, dict):
            continue
        for unique_id, raw_node in collection.items():
            if not isinstance(raw_node, dict):
                continue
            cols = raw_node.get("columns", {})
            if not isinstance(cols, dict):
                continue
            col_types: dict[str, str] = {}
            for col_name, col_data in cols.items():
                if isinstance(col_data, dict):
                    raw_type = col_data.get("type") or col_data.get("data_type")
                    dtype = _optional_text(raw_type, 255)
                    if dtype:
                        col_types[str(col_name)[:255]] = dtype
            if col_types:
                result[str(unique_id)] = col_types
    return result


def parse_dbt_run_results(run_results: dict[str, Any]) -> dict[str, ParsedDbtTestResult]:
    """Parse a dbt run_results.json mapping unique_id -> ParsedDbtTestResult."""
    if not isinstance(run_results, dict):
        raise DbtArtifactError("dbt run_results must be a valid JSON object")
    raw_results = run_results.get("results")
    if not isinstance(raw_results, list):
        raise DbtArtifactError("dbt run_results must contain a 'results' list")

    parsed: dict[str, ParsedDbtTestResult] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        unique_id = _optional_text(item.get("unique_id"), 500)
        status_val = _optional_text(item.get("status"), 50)
        if not unique_id or not status_val:
            continue
        failures = item.get("failures")
        failures_int = (
            int(failures)
            if isinstance(failures, int | float) and not isinstance(failures, bool)
            else None
        )
        exec_time = item.get("execution_time")
        exec_time_float = (
            float(exec_time)
            if isinstance(exec_time, int | float) and not isinstance(exec_time, bool)
            else None
        )
        parsed[unique_id] = ParsedDbtTestResult(
            unique_id=unique_id,
            status=status_val.upper(),
            failures=failures_int,
            message=_optional_text(item.get("message"), 4000),
            execution_time=exec_time_float,
        )
    return parsed
