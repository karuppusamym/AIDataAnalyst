"""BI tool lineage parsing (LN-4): report -> metric -> column edges.

Mirrors the deterministic, value-free ingestion shape already established by
`aida.openlineage` (OpenLineage RunEvents) and `aida.dbt_artifacts` (dbt
manifests): parse a vendor metadata artifact into bounded, redacted dataclasses,
never persist raw source-data values, and let the API layer own catalog
matching and storage.

Tableau's Metadata API (GraphQL) response shape and Power BI's Scanner API
(`admin/workspaces/getInfo`) response shape are both fully implemented —
per the roadmap's "minimum credible investment" principle for entry-ticket
gaps (`Docs/60-delivery/01-roadmap.md` S8/S9). Looker is a pluggable
addition: add a `_parse_looker(...)` function with the same return type and
register it in `_PARSERS` below.
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

# BI tools this ingestion boundary recognizes. TABLEAU and POWER_BI have
# parsers today; LOOKER is named here so the API/schema layer can accept and
# store the intent (a registered connection) ahead of a parser landing for it.
SUPPORTED_BI_TOOLS = frozenset({"TABLEAU", "POWER_BI", "LOOKER"})
IMPLEMENTED_BI_TOOLS = frozenset({"TABLEAU", "POWER_BI"})

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

# Power BI Scanner API (`admin/workspaces/getInfo`) shape constant. A visual's
# field well references either a measure (DAX, dataset-level) or a plain
# table column; both surface in `columns[]`/`measures[]` on each dataset table.
SUPPORTED_POWER_BI_FIELD_KINDS = frozenset({"measure", "column"})


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


def _power_bi_table_lookup(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """table name -> raw table payload, for resolving one dataset's field wells."""
    raw_tables = dataset.get("tables")
    tables: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_tables, list):
        return tables
    for raw_table in raw_tables[:2000]:
        if not isinstance(raw_table, dict):
            continue
        table_name = _optional_text(raw_table.get("name"), 255)
        if table_name:
            tables[table_name] = raw_table
    return tables


def _power_bi_field_kind_and_name(raw_field: dict[str, Any]) -> tuple[str, str] | None:
    """A visual field-well entry names exactly one of a measure or a column."""
    measure_name = _optional_text(raw_field.get("measure"), 500)
    if measure_name:
        return "measure", measure_name
    column_name = _optional_text(raw_field.get("column"), 500)
    if column_name:
        return "column", column_name
    return None


def _power_bi_field_from_payload(
    field_kind: str,
    field_name: str,
    table_payload: dict[str, Any],
    dataset_name: str | None,
) -> tuple[ParsedBiMetric, list[ParsedBiColumnRef]] | None:
    """Resolve one visual field-well reference against its dataset table.

    `table_payload` is the raw Power BI dataset table object (`name`, `schema`,
    `database`, `columns[]`, `measures[]`) -- the same `name`/`schema`/
    `database` shape Tableau's table objects use, so a resolved column ref is
    built with `_column_ref_from_payload`, unchanged.
    """
    if field_kind not in SUPPORTED_POWER_BI_FIELD_KINDS:
        return None
    table_name = _optional_text(table_payload.get("name"), 255)
    if not table_name:
        return None
    external_id = f"{table_name}::{field_kind}::{field_name}"
    if field_kind == "measure":
        raw_measures = table_payload.get("measures")
        measures: list[Any] = raw_measures if isinstance(raw_measures, list) else []
        raw_measure = next(
            (
                m
                for m in measures
                if isinstance(m, dict) and _optional_text(m.get("name"), 500) == field_name
            ),
            None,
        )
        if raw_measure is None:
            return None
        formula_hash, formula_present = _formula_hash(raw_measure.get("expression"))
        metric = ParsedBiMetric(
            external_id=external_id,
            name=field_name,
            field_type="Measure",
            datasource_name=dataset_name,
            formula_hash=formula_hash,
            formula_present=formula_present,
        )
        # Unlike Tableau's Metadata API (which resolves a CalculatedField's
        # upstreamColumns server-side), the Power BI Scanner API gives no
        # dependency graph for a measure's DAX expression, and deriving one
        # would mean parsing the (never-persisted, see `_formula_hash`) DAX
        # text ourselves -- a false sense of lineage fidelity this boundary
        # does not claim. A measure therefore carries no metric-column edges.
        return metric, []
    raw_columns = table_payload.get("columns")
    columns: list[Any] = raw_columns if isinstance(raw_columns, list) else []
    raw_column = next(
        (
            c
            for c in columns
            if isinstance(c, dict) and _optional_text(c.get("name"), 255) == field_name
        ),
        None,
    )
    if raw_column is None:
        return None
    metric = ParsedBiMetric(
        external_id=external_id,
        name=field_name,
        field_type="Column",
        datasource_name=dataset_name,
        formula_hash=None,
        formula_present=False,
    )
    column_ref = _column_ref_from_payload({"name": field_name, "table": table_payload})
    return metric, ([column_ref] if column_ref is not None else [])


def _parse_power_bi_metadata(payload: dict[str, Any]) -> ParsedBiArtifact:
    """Parse a Power BI Scanner API (`admin/workspaces/getInfo`) style export.

    Shape: top-level `workspaces[]`, each with `datasets[]` (each dataset has
    `tables[]` of `columns[]`/`measures[]`) and `reports[]` (each naming its
    `datasetId` and a `pages[]` list of `visuals[]`, each visual's `fields[]`
    referencing a dataset column or measure by table + name). This mirrors
    Tableau's workbook -> sheet/dashboard -> field -> column edges as
    report -> page -> measure/column -> column, but is not a re-skin of
    Tableau's GraphQL shape: Power BI's REST export has no `data` envelope,
    and a report's field references are lightweight name refs resolved
    against the dataset schema elsewhere in the same artifact, rather than
    Tableau's fully self-describing field instances.
    """
    raw_workspaces = payload.get("workspaces")
    if not isinstance(raw_workspaces, list):
        raise BiLineageError("power bi metadata artifact requires a workspaces array")

    reports: list[ParsedBiReport] = []
    metrics_by_id: dict[str, ParsedBiMetric] = {}
    report_metric_edges: set[tuple[str, str]] = set()
    metric_column_edges: dict[str, dict[tuple[Any, ...], ParsedBiColumnRef]] = {}

    for raw_workspace in raw_workspaces:
        if not isinstance(raw_workspace, dict):
            raise BiLineageError("power bi workspace entries must be objects")
        workspace_name = _optional_text(raw_workspace.get("name"), 255)

        datasets_by_id: dict[str, tuple[str | None, dict[str, dict[str, Any]]]] = {}
        raw_datasets = raw_workspace.get("datasets")
        if isinstance(raw_datasets, list):
            for raw_dataset in raw_datasets[:2000]:
                if not isinstance(raw_dataset, dict):
                    continue
                dataset_id = _optional_text(raw_dataset.get("id"), 255)
                if not dataset_id:
                    continue
                datasets_by_id[dataset_id] = (
                    _optional_text(raw_dataset.get("name"), 255),
                    _power_bi_table_lookup(raw_dataset),
                )

        raw_reports = raw_workspace.get("reports")
        if not isinstance(raw_reports, list):
            continue
        if len(reports) + len(raw_reports) > MAX_REPORTS:
            raise BiLineageError(
                f"power bi artifact exceeds the {MAX_REPORTS} report safety boundary"
            )

        for raw_report in raw_reports:
            if not isinstance(raw_report, dict):
                raise BiLineageError("power bi report entries must be objects")
            report_id = _required_text(raw_report.get("id"), "report.id", 255)
            report_name = _required_text(raw_report.get("name"), "report.name", 500)
            reports.append(
                ParsedBiReport(
                    external_id=report_id,
                    name=report_name,
                    report_type="REPORT",
                    project_name=workspace_name,
                    parent_external_id=None,
                )
            )
            dataset_id = _optional_text(raw_report.get("datasetId"), 255)
            dataset_name, tables_by_name = (
                datasets_by_id.get(dataset_id, (None, {})) if dataset_id else (None, {})
            )

            raw_pages = raw_report.get("pages")
            pages: list[Any] = raw_pages if isinstance(raw_pages, list) else []
            if len(reports) + len(pages) > MAX_REPORTS:
                raise BiLineageError(
                    f"power bi artifact exceeds the {MAX_REPORTS} report safety boundary"
                )

            for raw_page in pages:
                if not isinstance(raw_page, dict):
                    raise BiLineageError("power bi page entries must be objects")
                page_name = _required_text(raw_page.get("name"), "page.name", 255)
                page_display = _optional_text(raw_page.get("displayName"), 500) or page_name
                page_id = f"{report_id}::{page_name}"
                reports.append(
                    ParsedBiReport(
                        external_id=page_id,
                        name=page_display,
                        report_type="PAGE",
                        project_name=workspace_name,
                        parent_external_id=report_id,
                    )
                )
                raw_visuals = raw_page.get("visuals")
                visuals: list[Any] = raw_visuals if isinstance(raw_visuals, list) else []
                for raw_visual in visuals[:2000]:
                    if not isinstance(raw_visual, dict):
                        continue
                    raw_fields = raw_visual.get("fields")
                    visual_fields: list[Any] = raw_fields if isinstance(raw_fields, list) else []
                    for raw_field in visual_fields[:2000]:
                        if not isinstance(raw_field, dict):
                            continue
                        table_name = _optional_text(raw_field.get("table"), 255)
                        if not table_name:
                            continue
                        table_payload = tables_by_name.get(table_name)
                        if table_payload is None:
                            continue
                        field_kind_and_name = _power_bi_field_kind_and_name(raw_field)
                        if field_kind_and_name is None:
                            continue
                        field_kind, field_name = field_kind_and_name
                        resolved = _power_bi_field_from_payload(
                            field_kind, field_name, table_payload, dataset_name
                        )
                        if resolved is None:
                            continue
                        metric, columns = resolved
                        metrics_by_id.setdefault(metric.external_id, metric)
                        if len(report_metric_edges) >= MAX_REPORT_METRIC_EDGES:
                            raise BiLineageError(
                                f"power bi artifact exceeds the {MAX_REPORT_METRIC_EDGES} "
                                "report-metric edge safety boundary"
                            )
                        report_metric_edges.add((page_id, metric.external_id))
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
        raise BiLineageError(f"power bi artifact exceeds the {MAX_METRICS} metric safety boundary")

    metric_column_edge_list: list[ParsedBiMetricColumnEdge] = []
    for metric_id, columns_by_key in metric_column_edges.items():
        for column_ref in columns_by_key.values():
            metric_column_edge_list.append(
                ParsedBiMetricColumnEdge(metric_external_id=metric_id, column=column_ref)
            )
    if len(metric_column_edge_list) > MAX_METRIC_COLUMN_EDGES:
        raise BiLineageError(
            f"power bi artifact exceeds the {MAX_METRIC_COLUMN_EDGES} metric-column edge "
            "safety boundary"
        )

    return ParsedBiArtifact(
        fingerprint="",  # filled in by the dispatcher, which hashes the whole payload
        bi_tool="POWER_BI",
        generated_at=_parse_generated_at(payload.get("generatedAt")),
        reports=reports,
        metrics=list(metrics_by_id.values()),
        report_metric_edges=[
            ParsedBiReportMetricEdge(report_external_id=r, metric_external_id=m)
            for r, m in sorted(report_metric_edges)
        ],
        metric_column_edges=metric_column_edge_list,
    )


# Registry of implemented parsers, keyed by bi_tool. Adding Looker support is
# additive: implement `_parse_looker` with the same
# `dict[str, Any] -> ParsedBiArtifact` signature and register it here — no
# change to the dispatcher, models, or API layer is required.
_PARSERS: dict[str, Any] = {
    "TABLEAU": _parse_tableau_metadata,
    "POWER_BI": _parse_power_bi_metadata,
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
