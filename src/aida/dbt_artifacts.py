import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from aida.artifact_parsing import optional_text, parse_generated_at
from aida.sql_redaction import contains_value_shaped_text

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

#: The manifest keys `parse_dbt_manifest` reads resources out of. Named as a
#: constant rather than inlined because the engine capability matrix publishes
#: it as the evidence for "macros are not ingested": `macros` is deliberately
#: absent, and a reader of the published matrix should be looking at the same
#: tuple the parser loops over, not a sentence about it.
MANIFEST_COLLECTION_KEYS = (
    "nodes",
    "sources",
    "exposures",
    "metrics",
    "semantic_models",
    "saved_queries",
)

#: dbt compiles `on-run-start` / `on-run-end` into nodes of this resource type.
#: It is not in `SUPPORTED_RESOURCE_TYPES`, so such a node is skipped and its
#: SQL is never parsed. F06.4: counted rather than ignored, so a project that
#: runs hooks is visibly bounded instead of silently reading as fully covered.
HOOK_RESOURCE_TYPE = "operation"

#: Where a model's own hooks live in its manifest node. dbt accepts both
#: spellings; a manifest carries the hyphenated form and some older ones the
#: underscored.
_PRE_HOOK_KEYS = ("pre-hook", "pre_hook")
_POST_HOOK_KEYS = ("post-hook", "post_hook")


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
    # F06.4 (review 2026-09-16): bounded coverage evidence, not lineage.
    #
    # How many macros this resource's SQL depends on, from the manifest's own
    # `depends_on.macros`. The *compiled* SQL a macro produced is parsed like
    # any other, so the relations an expansion resolved to are real lineage --
    # but the macro itself is not ingested (see `MANIFEST_COLLECTION_KEYS`), so
    # nothing can say which macro produced what, and a macro whose expansion
    # depends on warehouse state at compile time is not modelled at all. A
    # non-zero count here is the explicit limitation; it is never a reason to
    # treat the model as less understood than its compiled SQL shows.
    macro_dependency_count: int = 0
    # Hooks are the harder gap: their SQL is in the node's config, not in its
    # compiled code, so it is never parsed and a post-hook that writes another
    # table produces no edge at all. Counted so the gap is visible per model.
    pre_hook_count: int = 0
    post_hook_count: int = 0


@dataclass(frozen=True)
class ParsedDbtArtifact:
    fingerprint: str
    dbt_schema_version: str
    dbt_version: str | None
    invocation_id: str | None
    generated_at: datetime | None
    resources: list[ParsedDbtResource]
    edges: list[tuple[str, str]]
    # F06.4: project-level bounded coverage evidence. `macro_count` is how many
    # macros the manifest declares, all of which are skipped;
    # `project_hook_count` is how many `on-run-start`/`on-run-end` operations it
    # compiled, whose SQL is never parsed. Both default to zero so an older
    # caller constructing this by keyword is unaffected.
    macro_count: int = 0
    project_hook_count: int = 0

    @property
    def macro_dependent_resource_count(self) -> int:
        """Resources whose SQL a macro contributed to."""
        return sum(1 for resource in self.resources if resource.macro_dependency_count)

    @property
    def model_hook_count(self) -> int:
        """`pre_hook` + `post_hook` entries across every parsed resource."""
        return sum(
            resource.pre_hook_count + resource.post_hook_count for resource in self.resources
        )


def _required_text(value: Any, field: str, limit: int) -> str:
    text = optional_text(value, limit)
    if not text:
        raise DbtArtifactError(f"dbt resource {field} is required")
    return text


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
            rendered = safe_statement.sql(dialect=dialect, comments=False, pretty=True)
            # A statement sqlglot keeps as an opaque `Command` (or any node that is not an
            # `exp.Literal`) renders its values back verbatim; store nothing rather than that.
            if contains_value_shaped_text(rendered, dialect=dialect):
                return fingerprint, None, "UNPARSEABLE"
            redacted.append(rendered)
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
    raw_dependency_macros = depends_on.get("macros")
    macro_dependency_count = (
        len(raw_dependency_macros) if isinstance(raw_dependency_macros, list) else 0
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
            desc = optional_text(raw_col.get("description"), 4000)
            if desc:
                column_descriptions[col_name] = desc
            dtype = optional_text(raw_col.get("data_type") or raw_col.get("type"), 255)
            if dtype:
                column_types[col_name] = dtype

    extra_metadata: dict[str, Any] = {}
    if resource_type == "exposure":
        owner = payload.get("owner")
        if isinstance(owner, dict):
            if owner.get("name"):
                extra_metadata["owner_name"] = optional_text(owner.get("name"), 255)
            if owner.get("email"):
                extra_metadata["owner_email"] = optional_text(owner.get("email"), 255)
        if payload.get("url"):
            extra_metadata["url"] = optional_text(payload.get("url"), 1000)
        if payload.get("maturity"):
            extra_metadata["maturity"] = optional_text(payload.get("maturity"), 50)
        if payload.get("type"):
            extra_metadata["exposure_type"] = optional_text(payload.get("type"), 50)
    elif resource_type in {"semantic_model", "metric"}:
        if payload.get("type"):
            extra_metadata["type"] = optional_text(payload.get("type"), 100)
        if payload.get("label"):
            extra_metadata["label"] = optional_text(payload.get("label"), 255)

    return ParsedDbtResource(
        unique_id=_required_text(unique_id, "unique_id", 500),
        resource_type=resource_type.upper(),
        package_name=_required_text(package_name, "package_name", 255),
        name=_required_text(physical_name, "name", 255),
        database_name=optional_text(payload.get("database"), 255),
        schema_name=optional_text(payload.get("schema"), 255),
        relation_name=optional_text(payload.get("relation_name"), 1000),
        materialization=optional_text(config.get("materialized"), 100),
        original_file_path=optional_text(payload.get("original_file_path"), 1000),
        description=optional_text(payload.get("description"), 4000),
        compiled_sql_hash=sql_hash,
        compiled_sql_redacted=redacted_sql,
        sql_parse_status=parse_status,
        column_names=column_names,
        tags=[str(tag)[:100] for tag in tags][:100],
        depends_on_unique_ids=[str(node)[:500] for node in dependency_nodes][:5000],
        column_descriptions=column_descriptions,
        column_types=column_types,
        extra_metadata=extra_metadata,
        macro_dependency_count=macro_dependency_count,
        pre_hook_count=_hook_count(config, _PRE_HOOK_KEYS),
        post_hook_count=_hook_count(config, _POST_HOOK_KEYS),
    )


def _hook_count(config: dict[str, Any], keys: tuple[str, ...]) -> int:
    """How many hooks `config` declares under either accepted spelling.

    A hook is a string or a `{"sql": ..., "transaction": ...}` mapping, and dbt
    accepts a bare string where a list is expected. Only the *count* is taken:
    a hook body is arbitrary SQL that routinely carries literal values, and
    nothing here may store one (INV-6).
    """
    total = 0
    for key in keys:
        value = config.get(key)
        if value is None:
            continue
        total += len(value) if isinstance(value, list) else 1
    return total


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
    for key in MANIFEST_COLLECTION_KEYS:
        value = manifest.get(key, {})
        if not isinstance(value, dict):
            raise DbtArtifactError(f"dbt manifest {key} must be an object")
        collections.append(value)
    # F06.4: macros and project hooks are counted before the resource loop
    # skips them, so "this project runs 4 hooks and 37 macros, none of which
    # Atlas reads" is a fact the ingestion records rather than an absence a
    # reader has to notice. Nothing about either is stored beyond the count.
    raw_macros = manifest.get("macros", {})
    macro_count = len(raw_macros) if isinstance(raw_macros, dict) else 0
    project_hook_count = sum(
        1
        for collection in collections
        for raw_resource in collection.values()
        if isinstance(raw_resource, dict)
        and str(raw_resource.get("resource_type", "")).lower() == HOOK_RESOURCE_TYPE
    )
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
        dbt_version=optional_text(metadata.get("dbt_version"), 50),
        invocation_id=optional_text(metadata.get("invocation_id"), 255),
        generated_at=parse_generated_at(metadata.get("generated_at")),
        resources=resources,
        edges=edges,
        macro_count=macro_count,
        project_hook_count=project_hook_count,
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
                    dtype = optional_text(raw_type, 255)
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
        unique_id = optional_text(item.get("unique_id"), 500)
        status_val = optional_text(item.get("status"), 50)
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
            message=optional_text(item.get("message"), 4000),
            execution_time=exec_time_float,
        )
    return parsed
