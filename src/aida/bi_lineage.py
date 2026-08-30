"""BI tool lineage parsing (LN-4): report -> metric -> column edges.

Mirrors the deterministic, value-free ingestion shape already established by
`aida.openlineage` (OpenLineage RunEvents) and `aida.dbt_artifacts` (dbt
manifests): parse a vendor metadata artifact into bounded, redacted dataclasses,
never persist raw source-data values, and let the API layer own catalog
matching and storage.

Only Tableau's Metadata API (GraphQL) response shape is fully implemented —
per the roadmap's "minimum credible investment" principle for entry-ticket
gaps (`Docs/60-delivery/01-roadmap.md` S8/S9). Power BI and Looker are
pluggable additions: add a `_parse_power_bi(...)` / `_parse_looker(...)`
function with the same return type and register it in `_PARSERS` below.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_REPORTS = 5_000
MAX_METRICS = 20_000
MAX_REPORT_METRIC_EDGES = 50_000
MAX_METRIC_COLUMN_EDGES = 50_000

# BI tools this ingestion boundary recognizes. Only TABLEAU has a parser today;
# the others are named here so the API/schema layer can accept and store the
# intent (a registered connection) ahead of a parser landing for them.
SUPPORTED_BI_TOOLS = frozenset({"TABLEAU", "POWER_BI", "LOOKER"})
IMPLEMENTED_BI_TOOLS = frozenset({"TABLEAU"})

_TABLEAU_REPORT_FIELD_CONTAINERS = ("sheets", "dashboards")
_TABLEAU_FIELD_INSTANCE_KEYS = {
    "sheets": "sheetFieldInstances",
    "dashboards": "dashboardFieldInstances",
}
_TABLEAU_REPORT_TYPES = {"sheets": "SHEET", "dashboards": "DASHBOARD"}
SUPPORTED_TABLEAU_FIELD_TYPES = frozenset(
    {
        "CalculatedField",
        "ColumnField",
        "GroupField",
        "BinField",
        "CombinedField",
    }
)


class BiLineageError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedBiReport:
    external_id: str
    name: str
    report_type: str
    project_name: str | None
    parent_external_id: str | None


@dataclass(frozen=True)
class ParsedBiMetric:
    external_id: str
    name: str
    field_type: str
    datasource_name: str | None
    formula_hash: str | None
    formula_present: bool


@dataclass(frozen=True)
class ParsedBiColumnRef:
    database_name: str | None
    schema_name: str | None
    table_name: str
    column_name: str


@dataclass(frozen=True)
class ParsedBiReportMetricEdge:
    report_external_id: str
    metric_external_id: str


@dataclass(frozen=True)
class ParsedBiMetricColumnEdge:
    metric_external_id: str
    column: ParsedBiColumnRef


@dataclass(frozen=True)
class ParsedBiArtifact:
    fingerprint: str
    bi_tool: str
    generated_at: datetime | None
    reports: list[ParsedBiReport] = field(default_factory=list)
    metrics: list[ParsedBiMetric] = field(default_factory=list)
    report_metric_edges: list[ParsedBiReportMetricEdge] = field(default_factory=list)
    metric_column_edges: list[ParsedBiMetricColumnEdge] = field(default_factory=list)


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _required_text(value: Any, field_name: str, limit: int) -> str:
    text = _optional_text(value, limit)
    if not text:
        raise BiLineageError(f"bi artifact {field_name} is required")
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


def _formula_hash(formula: Any) -> tuple[str | None, bool]:
    """Never persist the raw formula text — only its identity hash.

    Tableau's calculation language is not SQL, so `aida.dbt_artifacts`'
    literal-redaction approach (parse + strip literals, keep structure)
    cannot be applied with confidence here. To stay value-free without a
    false sense of redaction fidelity, the raw expression is not retained
    at all — only whether one was present and a stable hash of it.
    """
    if formula is None:
        return None, False
    text = str(formula).strip()
    if not text:
        return None, False
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), True


def _column_ref_from_payload(payload: dict[str, Any]) -> ParsedBiColumnRef | None:
    column_name = _optional_text(payload.get("name"), 255)
    raw_table = payload.get("table")
    if not column_name or not isinstance(raw_table, dict):
        return None
    table_name = _optional_text(raw_table.get("name"), 255)
    if not table_name:
        return None
    schema_name = _optional_text(raw_table.get("schema"), 255)
    raw_database = raw_table.get("database")
    database_name = None
    if isinstance(raw_database, dict):
        database_name = _optional_text(raw_database.get("name"), 255)
    return ParsedBiColumnRef(
        database_name=database_name,
        schema_name=schema_name,
        table_name=table_name,
        column_name=column_name,
    )


def _tableau_field_from_payload(
    payload: dict[str, Any],
    datasource_name: str | None,
) -> tuple[ParsedBiMetric, list[ParsedBiColumnRef]] | None:
    field_type = payload.get("__typename")
    if field_type not in SUPPORTED_TABLEAU_FIELD_TYPES:
        return None
    external_id = _optional_text(payload.get("id"), 255)
    name = _optional_text(payload.get("name"), 500)
    if not external_id or not name:
        return None
    formula_hash, formula_present = _formula_hash(payload.get("formula"))
    metric = ParsedBiMetric(
        external_id=external_id,
        name=name,
        field_type=str(field_type),
        datasource_name=datasource_name,
        formula_hash=formula_hash,
        formula_present=formula_present,
    )
    raw_upstream_columns = payload.get("upstreamColumns")
    upstream_columns: list[Any] = (
        raw_upstream_columns if isinstance(raw_upstream_columns, list) else []
    )
    columns: list[ParsedBiColumnRef] = []
    for raw_column in upstream_columns[:2000]:
        if not isinstance(raw_column, dict):
            continue
        column_ref = _column_ref_from_payload(raw_column)
        if column_ref is not None:
            columns.append(column_ref)
    return metric, columns


def _parse_tableau_metadata(payload: dict[str, Any]) -> ParsedBiArtifact:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BiLineageError("tableau metadata artifact requires a top-level data object")
    raw_workbooks = data.get("workbooks")
    if not isinstance(raw_workbooks, list):
        raise BiLineageError("tableau metadata artifact requires a workbooks array")
    if len(raw_workbooks) > MAX_REPORTS:
        raise BiLineageError(f"tableau artifact exceeds the {MAX_REPORTS} report safety boundary")

    reports: list[ParsedBiReport] = []
    metrics_by_id: dict[str, ParsedBiMetric] = {}
    report_metric_edges: set[tuple[str, str]] = set()
    metric_column_edges: dict[str, dict[tuple[Any, ...], ParsedBiColumnRef]] = {}

    for raw_workbook in raw_workbooks:
        if not isinstance(raw_workbook, dict):
            raise BiLineageError("tableau workbook entries must be objects")
        workbook_id = _required_text(raw_workbook.get("luid"), "workbook.luid", 255)
        workbook_name = _required_text(raw_workbook.get("name"), "workbook.name", 500)
        project_name = _optional_text(raw_workbook.get("projectName"), 255)
        reports.append(
            ParsedBiReport(
                external_id=workbook_id,
                name=workbook_name,
                report_type="WORKBOOK",
                project_name=project_name,
                parent_external_id=None,
            )
        )
        for container_key in _TABLEAU_REPORT_FIELD_CONTAINERS:
            raw_children = raw_workbook.get(container_key)
            if not isinstance(raw_children, list):
                continue
            if len(reports) + len(raw_children) > MAX_REPORTS:
                raise BiLineageError(
                    f"tableau artifact exceeds the {MAX_REPORTS} report safety boundary"
                )
            report_type = _TABLEAU_REPORT_TYPES[container_key]
            field_instance_key = _TABLEAU_FIELD_INSTANCE_KEYS[container_key]
            for raw_child in raw_children:
                if not isinstance(raw_child, dict):
                    raise BiLineageError(f"tableau {container_key} entries must be objects")
                child_id = _required_text(raw_child.get("luid"), f"{container_key}.luid", 255)
                child_name = _required_text(raw_child.get("name"), f"{container_key}.name", 500)
                reports.append(
                    ParsedBiReport(
                        external_id=child_id,
                        name=child_name,
                        report_type=report_type,
                        project_name=project_name,
                        parent_external_id=workbook_id,
                    )
                )
                raw_field_instances = raw_child.get(field_instance_key)
                field_instances: list[Any] = (
                    raw_field_instances if isinstance(raw_field_instances, list) else []
                )
                for raw_instance in field_instances[:2000]:
                    if not isinstance(raw_instance, dict):
                        continue
                    raw_field = raw_instance.get("field")
                    if not isinstance(raw_field, dict):
                        continue
                    datasource_name = _optional_text(raw_field.get("datasourceName"), 255)
                    parsed_field = _tableau_field_from_payload(raw_field, datasource_name)
                    if parsed_field is None:
                        continue
                    metric, columns = parsed_field
                    metrics_by_id.setdefault(metric.external_id, metric)
                    if len(report_metric_edges) >= MAX_REPORT_METRIC_EDGES:
                        raise BiLineageError(
                            f"tableau artifact exceeds the {MAX_REPORT_METRIC_EDGES} "
                            "report-metric edge safety boundary"
                        )
                    report_metric_edges.add((child_id, metric.external_id))
                    bucket = metric_column_edges.setdefault(metric.external_id, {})
                    for column_ref in columns:
                        key = (
                            column_ref.database_name,
                            column_ref.schema_name,
                            column_ref.table_name,
                            column_ref.column_name,
                        )
                        bucket[key] = column_ref

    if len(metrics_by_id) > MAX_METRICS:
        raise BiLineageError(f"tableau artifact exceeds the {MAX_METRICS} metric safety boundary")

    metric_column_edge_list: list[ParsedBiMetricColumnEdge] = []
    for metric_id, columns_by_key in metric_column_edges.items():
        for column_ref in columns_by_key.values():
            metric_column_edge_list.append(
                ParsedBiMetricColumnEdge(metric_external_id=metric_id, column=column_ref)
            )
    if len(metric_column_edge_list) > MAX_METRIC_COLUMN_EDGES:
        raise BiLineageError(
            f"tableau artifact exceeds the {MAX_METRIC_COLUMN_EDGES} metric-column edge "
            "safety boundary"
        )

    return ParsedBiArtifact(
        fingerprint="",  # filled in by the dispatcher, which hashes the whole payload
        bi_tool="TABLEAU",
        generated_at=_parse_generated_at(data.get("generatedAt")),
        reports=reports,
        metrics=list(metrics_by_id.values()),
        report_metric_edges=[
            ParsedBiReportMetricEdge(report_external_id=r, metric_external_id=m)
            for r, m in sorted(report_metric_edges)
        ],
        metric_column_edges=metric_column_edge_list,
    )


# Registry of implemented parsers, keyed by bi_tool. Adding Power BI or Looker
# support is additive: implement `_parse_power_bi`/`_parse_looker` with the
# same `dict[str, Any] -> ParsedBiArtifact` signature and register it here —
# no change to the dispatcher, models, or API layer is required.
_PARSERS: dict[str, Any] = {
    "TABLEAU": _parse_tableau_metadata,
}


def parse_bi_artifact(bi_tool: str, payload: dict[str, Any]) -> ParsedBiArtifact:
    """Parse a BI tool's exported metadata artifact into bounded, value-free lineage."""
    if not isinstance(payload, dict):
        raise BiLineageError("bi artifact must be a JSON object")
    normalized_tool = (bi_tool or "").strip().upper()
    if normalized_tool not in SUPPORTED_BI_TOOLS:
        raise BiLineageError(
            f"unsupported bi tool: {bi_tool!r}; supported: {sorted(SUPPORTED_BI_TOOLS)}"
        )
    parser = _PARSERS.get(normalized_tool)
    if parser is None:
        raise BiLineageError(
            f"{normalized_tool} ingestion is not yet implemented; only "
            f"{sorted(IMPLEMENTED_BI_TOOLS)} are currently supported"
        )
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise BiLineageError("bi artifact must be valid JSON data") from exc
    if len(canonical.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise BiLineageError(
            f"bi artifact exceeds the {MAX_ARTIFACT_BYTES // (1024 * 1024)} MiB ingestion limit"
        )
    parsed = parser(payload)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ParsedBiArtifact(
        fingerprint=fingerprint,
        bi_tool=parsed.bi_tool,
        generated_at=parsed.generated_at,
        reports=parsed.reports,
        metrics=parsed.metrics,
        report_metric_edges=parsed.report_metric_edges,
        metric_column_edges=parsed.metric_column_edges,
    )
