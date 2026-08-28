import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_OPENLINEAGE_EVENT_BYTES = 1 * 1024 * 1024
MAX_OPENLINEAGE_DATASETS = 2_000
MAX_OPENLINEAGE_TABLE_EDGES = 25_000
MAX_OPENLINEAGE_COLUMN_EDGES = 100_000
DISALLOWED_DATASET_FACETS = frozenset(
    {
        "dataqualityassertions",
        "dataqualitymetrics",
        "outputstatistics",
    }
)
FORBIDDEN_FACET_KEY_FRAGMENTS = (
    "sample",
    "payload",
    "password",
    "secret",
    "token",
    "credential",
)


class OpenLineageError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedOpenLineageDataset:
    namespace: str
    name: str
    facets: dict[str, Any] = field(default_factory=dict)
    schema_fields: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedOpenLineageTableEdge:
    input_dataset_namespace: str
    input_dataset_name: str
    output_dataset_namespace: str
    output_dataset_name: str


@dataclass(frozen=True)
class ParsedOpenLineageColumnEdge:
    input_dataset_namespace: str
    input_dataset_name: str
    input_column_name: str
    output_dataset_namespace: str
    output_dataset_name: str
    output_column_name: str
    transformation_type: str | None
    transformation_subtype: str | None


@dataclass(frozen=True)
class ParsedOpenLineageEvent:
    fingerprint: str
    event_type: str
    event_time: datetime
    producer: str
    schema_url: str | None
    job_namespace: str
    job_name: str
    run_id: str
    inputs: list[ParsedOpenLineageDataset]
    outputs: list[ParsedOpenLineageDataset]
    table_edges: list[ParsedOpenLineageTableEdge]
    column_edges: list[ParsedOpenLineageColumnEdge]


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _required_text(value: Any, field: str, limit: int) -> str:
    text = _optional_text(value, limit)
    if not text:
        raise OpenLineageError(f"openlineage {field} is required")
    return text


def _parse_event_time(value: Any) -> datetime:
    text = _required_text(value, "eventTime", 100)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpenLineageError("openlineage eventTime must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _validate_facet_payload(value: Any, *, path: str, depth: int = 0) -> None:
    if depth > 12:
        raise OpenLineageError("openlineage facets exceed the maximum nesting depth")
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(fragment in lowered for fragment in FORBIDDEN_FACET_KEY_FRAGMENTS):
                raise OpenLineageError(f"openlineage facet key is not permitted: {path}.{key}")
            _validate_facet_payload(nested, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 10_000:
            raise OpenLineageError("openlineage facets exceed the maximum list size")
        for index, nested in enumerate(value):
            _validate_facet_payload(nested, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str) and len(value) > 10_000:
        raise OpenLineageError("openlineage facet text exceeds the maximum size")


def _dataset_schema_fields(facets: dict[str, Any]) -> list[str]:
    schema = facets.get("schema")
    if not isinstance(schema, dict):
        return []
    fields = schema.get("fields")
    if not isinstance(fields, list):
        return []
    result: list[str] = []
    for raw_field in fields[:2000]:
        if not isinstance(raw_field, dict):
            continue
        name = _optional_text(raw_field.get("name"), 255)
        if name:
            result.append(name)
    return result


def _dataset_from_payload(payload: dict[str, Any]) -> ParsedOpenLineageDataset:
    namespace = _required_text(payload.get("namespace"), "dataset.namespace", 500)
    name = _required_text(payload.get("name"), "dataset.name", 1000)
    raw_facets = payload.get("facets")
    facets = raw_facets if isinstance(raw_facets, dict) else {}
    lowered_keys = {str(key).lower() for key in facets}
    disallowed = sorted(DISALLOWED_DATASET_FACETS.intersection(lowered_keys))
    if disallowed:
        raise OpenLineageError(
            f"openlineage dataset facets are not permitted by the value-free contract: {', '.join(disallowed)}"
        )
    _validate_facet_payload(facets, path="dataset.facets")
    return ParsedOpenLineageDataset(
        namespace=namespace,
        name=name,
        facets=facets,
        schema_fields=_dataset_schema_fields(facets),
    )


def _column_edges_for_outputs(
    outputs: list[ParsedOpenLineageDataset],
) -> list[ParsedOpenLineageColumnEdge]:
    edges: list[ParsedOpenLineageColumnEdge] = []
    for output in outputs:
        column_lineage = output.facets.get("columnLineage")
        if not isinstance(column_lineage, dict):
            continue
        fields = column_lineage.get("fields")
        if not isinstance(fields, dict):
            continue
        for raw_output_column, lineage in list(fields.items())[:5000]:
            output_column_name = _optional_text(raw_output_column, 255)
            if not output_column_name or not isinstance(lineage, dict):
                continue
            input_fields = lineage.get("inputFields")
            if not isinstance(input_fields, list):
                continue
            for raw_input in input_fields[:5000]:
                if not isinstance(raw_input, dict):
                    continue
                input_namespace = _optional_text(raw_input.get("namespace"), 500)
                input_name = _optional_text(raw_input.get("name"), 1000)
                input_column_name = _optional_text(raw_input.get("field"), 255)
                if not input_namespace or not input_name or not input_column_name:
                    continue
                raw_transformations = raw_input.get("transformations")
                transformations = (
                    raw_transformations if isinstance(raw_transformations, list) else []
                )
                first_transformation = (
                    transformations[0]
                    if transformations and isinstance(transformations[0], dict)
                    else {}
                )
                edges.append(
                    ParsedOpenLineageColumnEdge(
                        input_dataset_namespace=input_namespace,
                        input_dataset_name=input_name,
                        input_column_name=input_column_name,
                        output_dataset_namespace=output.namespace,
                        output_dataset_name=output.name,
                        output_column_name=output_column_name,
                        transformation_type=_optional_text(first_transformation.get("type"), 100),
                        transformation_subtype=_optional_text(
                            first_transformation.get("subtype"), 100
                        ),
                    )
                )
    return edges


def parse_openlineage_run_event(event: dict[str, Any]) -> ParsedOpenLineageEvent:
    if not isinstance(event, dict):
        raise OpenLineageError("openlineage event must be a JSON object")
    try:
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise OpenLineageError("openlineage event must be valid JSON data") from exc
    if len(canonical.encode("utf-8")) > MAX_OPENLINEAGE_EVENT_BYTES:
        raise OpenLineageError("openlineage event exceeds the 1 MiB ingestion limit")

    raw_job = event.get("job")
    if not isinstance(raw_job, dict):
        raise OpenLineageError("openlineage job is required")
    raw_run = event.get("run")
    if not isinstance(raw_run, dict):
        raise OpenLineageError("openlineage run is required")
    raw_job_facets = raw_job.get("facets")
    if isinstance(raw_job_facets, dict):
        _validate_facet_payload(raw_job_facets, path="job.facets")
    raw_run_facets = raw_run.get("facets")
    if isinstance(raw_run_facets, dict):
        _validate_facet_payload(raw_run_facets, path="run.facets")

    raw_inputs = event.get("inputs") or []
    raw_outputs = event.get("outputs") or []
    if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
        raise OpenLineageError("openlineage inputs and outputs must be arrays")
    if len(raw_inputs) + len(raw_outputs) > MAX_OPENLINEAGE_DATASETS:
        raise OpenLineageError("openlineage event exceeds the dataset safety boundary")

    inputs = [_dataset_from_payload(item) for item in raw_inputs if isinstance(item, dict)]
    outputs = [_dataset_from_payload(item) for item in raw_outputs if isinstance(item, dict)]
    table_edges = [
        ParsedOpenLineageTableEdge(
            input_dataset_namespace=input_dataset.namespace,
            input_dataset_name=input_dataset.name,
            output_dataset_namespace=output_dataset.namespace,
            output_dataset_name=output_dataset.name,
        )
        for input_dataset in inputs
        for output_dataset in outputs
    ]
    if len(table_edges) > MAX_OPENLINEAGE_TABLE_EDGES:
        raise OpenLineageError("openlineage event exceeds the table-edge safety boundary")
    column_edges = _column_edges_for_outputs(outputs)
    if len(column_edges) > MAX_OPENLINEAGE_COLUMN_EDGES:
        raise OpenLineageError("openlineage event exceeds the column-edge safety boundary")
    return ParsedOpenLineageEvent(
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        event_type=_required_text(event.get("eventType"), "eventType", 30).upper(),
        event_time=_parse_event_time(event.get("eventTime")),
        producer=_required_text(event.get("producer"), "producer", 1000),
        schema_url=_optional_text(event.get("schemaURL"), 1000),
        job_namespace=_required_text(raw_job.get("namespace"), "job.namespace", 500),
        job_name=_required_text(raw_job.get("name"), "job.name", 500),
        run_id=_required_text(raw_run.get("runId"), "run.runId", 255),
        inputs=inputs,
        outputs=outputs,
        table_edges=table_edges,
        column_edges=column_edges,
    )
