/* ---------------------------------------------------------------------------
   Sources — a datasource's health, and the context snapshot composed from it.

   The snapshot is not a backend endpoint: it composes reads this client
   already owns (health, business annotations, quality summary, open
   incidents, table inventory) into one downloadable document, per datasource
   or rolled up across a project. A failed section degrades to a warning
   rather than failing the whole document.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr } from "./transport";
import { fetchBusinessAnnotations, fetchTablesLegacy } from "./catalog";
import { fetchQualityIncidents, fetchQualitySummary } from "./quality";

/* ---------------------------------------------------------------------------
   Sources — UX-15/UX-16 follow-on (nav id `sources`). Reuses
   `fetchOrgDatasources` above for the fleet list (see that function's own
   comment for the `DataSourceRead`/`DataSourceSummaryRead` shape note this
   screen also relies on -- `credential_reference` is typed but not actually
   present on this endpoint's wire response; this screen never reads it). The
   only new call this screen needs is per-source health.
--------------------------------------------------------------------------- */

/** `GET /v1/datasources/{datasource_id}/health` (`operational_api.py::get_datasource_health`,
 *  `:266`) — a composite, explainable 0-100 score over the connector's recent
 *  run history (`aida.connector_health.compute_connector_health`): run success
 *  rate, staleness, failure streak, profiling coverage and datasource
 *  enablement, each its own weighted factor with a human-readable `reason` and
 *  `evidence`, plus any `blockers` (e.g. `NO_RUN_HISTORY`, `DATASOURCE_DISABLED`,
 *  `REPEATED_FAILURES`) explaining why the status is what it is. */
export function fetchDatasourceHealth(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<ConnectorHealthScoreRead> {
  return demoOr(
    async () => makeFixtureDatasourceHealth(datasourceId),
    async () => {
      return get<ConnectorHealthScoreRead>(`/v1/datasources/${datasourceId}/health`, signal);
    },
  );
}

/* ---------------------------------------------------------------------------
   Context snapshot — an on-demand, downloadable "what do we know about this
   datasource" document. Not a new backend endpoint: it composes five reads
   this file already exposes (health, quality summary, open incidents,
   approved business annotations, table inventory) into one client-side
   object, the same "assemble from what's already governed" idiom
   `agent_roster.py`'s own module docstring insists on server-side. No
   curation step, unlike Context Products — every table the datasource
   actually has is included, documented or not, so the gap itself is visible
   rather than only ever showing what someone already wrote up.

   Each of the five reads is fetched independently and can fail on its own
   (a role without quality-incident access, say) without blanking the rest
   of the snapshot — failures are recorded in `warnings`, never silently
   dropped, matching this file's own honesty convention elsewhere (see
   `PortfolioAnalyticsScreen`'s independent summary/trends error states).
--------------------------------------------------------------------------- */

//: Table inventory is capped, not silently truncated -- `truncated` on the
//: snapshot tells the caller when a datasource has more tables than this.
const CONTEXT_SNAPSHOT_TABLE_CAP = 500;

export interface ContextSnapshotDatasource {
  id: string;
  name: string;
  connector_type: string;
  dialect: string;
  environment: string;
  network_zone: string | null;
  status: string;
  project_id: string;
  organization_id: string;
}

export interface ContextSnapshotDocumentedTable {
  table_id: string;
  schema_name: string;
  table_name: string;
  domain_name: string;
  entity_name: string;
  business_name: string;
  business_description: string;
  table_role: string;
  grain_statement: string;
  synonyms: string[];
  suggested_questions: string[];
}

export interface ContextSnapshotUndocumentedTable {
  name: string;
  object_type: string;
  status: string;
}

export interface ContextSnapshotIncident {
  table_name: string;
  anomaly_type: string;
  severity: string;
  status: string;
  summary: string;
  first_observed_at: string;
}

export interface DatasourceContextSnapshot {
  generated_at: string;
  datasource: ContextSnapshotDatasource;
  health: { score: number; status: string; computed_at: string } | null;
  quality: {
    table_count: number;
    observed_table_count: number;
    open_incident_count: number;
    critical_incident_count: number;
    average_quality_score: number | null;
    metadata_scan_status: string;
  } | null;
  open_incidents: ContextSnapshotIncident[];
  documented_tables: ContextSnapshotDocumentedTable[];
  undocumented_tables: ContextSnapshotUndocumentedTable[];
  documented_count: number;
  undocumented_count: number;
  truncated: boolean;
  warnings: string[];
}

async function settleOrWarn<T>(
  label: string,
  warnings: string[],
  action: () => Promise<T>,
): Promise<T | null> {
  try {
    return await action();
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e;
    warnings.push(`${label} could not be loaded: ${e instanceof ApiError ? e.detail : (e as Error).message}`);
    return null;
  }
}

/** Assembles a `DatasourceContextSnapshot` from five independent, already-real
 *  reads. Never throws for a single section failing — see the module comment
 *  above — only for the caller's own `AbortSignal` firing. */
export async function buildDatasourceContextSnapshot(
  datasource: DataSourceRead,
  signal?: AbortSignal,
): Promise<DatasourceContextSnapshot> {
  const warnings: string[] = [];

  const [health, annotations, quality, incidents, tables] = await Promise.all([
    settleOrWarn("Health", warnings, () => fetchDatasourceHealth(datasource.id, signal)),
    settleOrWarn("Business annotations", warnings, () =>
      fetchBusinessAnnotations(
        { datasourceId: datasource.id, limit: CONTEXT_SNAPSHOT_TABLE_CAP },
        signal,
      ),
    ),
    settleOrWarn("Quality summary", warnings, () => fetchQualitySummary(datasource.id, signal)),
    settleOrWarn("Open incidents", warnings, () =>
      fetchQualityIncidents(datasource.id, { status: "OPEN", limit: 100 }, signal),
    ),
    settleOrWarn("Table inventory", warnings, () =>
      fetchTablesLegacy(datasource.id, { limit: CONTEXT_SNAPSHOT_TABLE_CAP }, signal),
    ),
  ]);

  const documentedTableIds = new Set((annotations?.items ?? []).map((a) => a.table_id));
  const documentedTables: ContextSnapshotDocumentedTable[] = (annotations?.items ?? []).map((a) => ({
    table_id: a.table_id,
    schema_name: a.schema_name,
    table_name: a.table_name,
    domain_name: a.domain_name,
    entity_name: a.entity_name,
    business_name: a.business_name,
    business_description: a.business_description,
    table_role: a.table_role,
    grain_statement: a.grain_statement,
    synonyms: a.synonyms,
    suggested_questions: a.suggested_questions,
  }));
  const undocumentedTables: ContextSnapshotUndocumentedTable[] = (tables?.items ?? [])
    .filter((t) => !documentedTableIds.has(t.id))
    .map((t) => ({ name: t.name, object_type: t.object_type, status: t.status }));

  const truncated =
    (annotations !== null && annotations.total > annotations.items.length) ||
    (tables !== null && tables.items.length >= CONTEXT_SNAPSHOT_TABLE_CAP);

  return {
    generated_at: new Date().toISOString(),
    datasource: {
      id: datasource.id,
      name: datasource.name,
      connector_type: datasource.connector_type,
      dialect: datasource.dialect,
      environment: datasource.environment,
      network_zone: datasource.network_zone ?? null,
      status: datasource.status,
      project_id: datasource.project_id,
      organization_id: datasource.organization_id,
    },
    health: health ? { score: health.score, status: health.status, computed_at: health.computed_at } : null,
    quality: quality
      ? {
          table_count: quality.table_count,
          observed_table_count: quality.observed_table_count,
          open_incident_count: quality.open_incident_count,
          critical_incident_count: quality.critical_incident_count,
          average_quality_score: quality.average_quality_score,
          metadata_scan_status: quality.metadata_scan_status,
        }
      : null,
    open_incidents: (incidents?.items ?? []).map((i) => ({
      table_name: i.table_name,
      anomaly_type: i.anomaly_type,
      severity: i.severity,
      status: i.status,
      summary: i.summary,
      first_observed_at: i.first_observed_at,
    })),
    documented_tables: documentedTables,
    undocumented_tables: undocumentedTables,
    documented_count: documentedTables.length,
    undocumented_count: undocumentedTables.length,
    truncated,
    warnings,
  };
}

/** Renders a snapshot as a readable Markdown document — the "wiki page" a
 *  steward or an incoming analyst can actually read, not a JSON dump. */
export function renderContextSnapshotMarkdown(snapshot: DatasourceContextSnapshot): string {
  const lines: string[] = [];
  const ds = snapshot.datasource;
  lines.push(`# ${ds.name}`);
  lines.push("");
  lines.push(
    `${ds.connector_type} · ${ds.dialect} · ${ds.environment}${ds.network_zone ? ` · ${ds.network_zone}` : ""} · ${ds.status.toLowerCase()}`,
  );
  lines.push("");
  lines.push(`_Generated ${snapshot.generated_at}_`);
  lines.push("");

  if (snapshot.warnings.length > 0) {
    lines.push("> **Some sections could not be loaded:**");
    for (const w of snapshot.warnings) lines.push(`> - ${w}`);
    lines.push("");
  }

  if (snapshot.health) {
    lines.push("## Health");
    lines.push(
      `Score **${snapshot.health.score}** (${snapshot.health.status.toLowerCase()}), computed ${snapshot.health.computed_at}.`,
    );
    lines.push("");
  }

  if (snapshot.quality) {
    const q = snapshot.quality;
    lines.push("## Quality");
    lines.push(
      `${q.observed_table_count}/${q.table_count} tables observed · ` +
        `${q.open_incident_count} open incidents (${q.critical_incident_count} critical) · ` +
        `average score ${q.average_quality_score === null ? "—" : q.average_quality_score} · ` +
        `metadata scan ${q.metadata_scan_status.toLowerCase()}.`,
    );
    lines.push("");
  }

  if (snapshot.open_incidents.length > 0) {
    lines.push("## Open incidents");
    for (const inc of snapshot.open_incidents) {
      lines.push(`- **${inc.table_name}** — ${inc.anomaly_type} (${inc.severity.toLowerCase()}): ${inc.summary}`);
    }
    lines.push("");
  }

  lines.push(`## Documented tables (${snapshot.documented_count})`);
  lines.push("");
  if (snapshot.documented_tables.length === 0) {
    lines.push("_No table in this datasource has an approved business annotation yet._");
    lines.push("");
  }
  for (const t of snapshot.documented_tables) {
    lines.push(`### ${t.schema_name}.${t.table_name}`);
    if (t.business_name) lines.push(`**${t.business_name}**`);
    lines.push("");
    if (t.business_description) lines.push(t.business_description);
    lines.push("");
    const facts: string[] = [];
    if (t.domain_name) facts.push(`domain: ${t.domain_name}`);
    if (t.entity_name) facts.push(`entity: ${t.entity_name}`);
    if (t.table_role) facts.push(`role: ${t.table_role}`);
    if (t.grain_statement) facts.push(`grain: ${t.grain_statement}`);
    if (facts.length > 0) lines.push(facts.join(" · "));
    if (t.synonyms.length > 0) lines.push(`Synonyms: ${t.synonyms.join(", ")}`);
    if (t.suggested_questions.length > 0) {
      lines.push("");
      lines.push("Questions this table answers:");
      for (const q of t.suggested_questions) lines.push(`- ${q}`);
    }
    lines.push("");
  }

  lines.push(`## Undocumented tables (${snapshot.undocumented_count})`);
  lines.push("");
  if (snapshot.undocumented_tables.length === 0) {
    lines.push("_Every table this snapshot saw has an approved business annotation._");
  } else {
    for (const t of snapshot.undocumented_tables) {
      lines.push(`- ${t.name} (${t.object_type.toLowerCase()}, ${t.status.toLowerCase()})`);
    }
  }
  lines.push("");

  if (snapshot.truncated) {
    lines.push(
      `> This snapshot is capped at ${CONTEXT_SNAPSHOT_TABLE_CAP} tables/annotations; this datasource has more than fit.`,
    );
  }

  return lines.join("\n");
}

/** Builds the snapshot and triggers a same-origin blob download, the same
 *  idiom `exportAssetEvidence` above uses (a bare `<a download href>` can't
 *  carry this app's identity headers). */
export async function downloadDatasourceContextSnapshot(
  datasource: DataSourceRead,
  format: "markdown" | "json",
  signal?: AbortSignal,
): Promise<DatasourceContextSnapshot> {
  const snapshot = await buildDatasourceContextSnapshot(datasource, signal);
  const slug = datasource.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const isMarkdown = format === "markdown";
  const content = isMarkdown ? renderContextSnapshotMarkdown(snapshot) : JSON.stringify(snapshot, null, 2);
  const blob = new Blob([content], { type: isMarkdown ? "text/markdown" : "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `context-${slug}.${isMarkdown ? "md" : "json"}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return snapshot;
}

/* ---------------------------------------------------------------------------
   Project context snapshot — the same idea rolled up across every datasource
   in one project, for the operator question "what do we know about this
   project as a whole" rather than one datasource at a time. Built entirely
   from `buildDatasourceContextSnapshot` above, run once per datasource in
   the project — no new fetch shape, no new backend route.
--------------------------------------------------------------------------- */

export interface ProjectContextSnapshot {
  generated_at: string;
  project: { id: string; name: string; slug: string };
  datasource_count: number;
  documented_count: number;
  undocumented_count: number;
  open_incident_count: number;
  datasources: DatasourceContextSnapshot[];
  warnings: string[];
}

/** Runs `buildDatasourceContextSnapshot` for every datasource in `datasources`
 *  and rolls the totals up. A project with 20+ datasources runs 20+ fetches
 *  in parallel per datasource (five each) — fine for the modest per-project
 *  fleet sizes this is meant for, not something to fan out at organization
 *  scale without a limit. */
export async function buildProjectContextSnapshot(
  project: ProjectRead,
  datasources: DataSourceRead[],
  signal?: AbortSignal,
): Promise<ProjectContextSnapshot> {
  const warnings: string[] = [];
  const results = await Promise.all(
    datasources.map((ds) =>
      buildDatasourceContextSnapshot(ds, signal).catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") throw e;
        warnings.push(`${ds.name}: ${e instanceof ApiError ? e.detail : (e as Error).message}`);
        return null;
      }),
    ),
  );
  const perDatasource = results.filter((r): r is DatasourceContextSnapshot => r !== null);

  return {
    generated_at: new Date().toISOString(),
    project: { id: project.id, name: project.name, slug: project.slug },
    datasource_count: datasources.length,
    documented_count: perDatasource.reduce((sum, r) => sum + r.documented_count, 0),
    undocumented_count: perDatasource.reduce((sum, r) => sum + r.undocumented_count, 0),
    open_incident_count: perDatasource.reduce((sum, r) => sum + r.open_incidents.length, 0),
    datasources: perDatasource,
    warnings,
  };
}

/** Renders a project rollup as one Markdown document: a project-level
 *  summary followed by each datasource's own section, reusing
 *  `renderContextSnapshotMarkdown`'s per-datasource body so the two documents
 *  never describe the same datasource differently. */
export function renderProjectContextSnapshotMarkdown(snapshot: ProjectContextSnapshot): string {
  const lines: string[] = [];
  lines.push(`# ${snapshot.project.name}`);
  lines.push("");
  lines.push(`_Generated ${snapshot.generated_at}_`);
  lines.push("");
  lines.push(
    `${snapshot.datasource_count} datasource(s) · ${snapshot.documented_count} documented tables · ` +
      `${snapshot.undocumented_count} undocumented tables · ${snapshot.open_incident_count} open incidents.`,
  );
  lines.push("");
  if (snapshot.warnings.length > 0) {
    lines.push("> **Some datasources could not be included:**");
    for (const w of snapshot.warnings) lines.push(`> - ${w}`);
    lines.push("");
  }
  lines.push("---");
  lines.push("");
  for (const ds of snapshot.datasources) {
    lines.push(renderContextSnapshotMarkdown(ds));
    lines.push("");
    lines.push("---");
    lines.push("");
  }
  return lines.join("\n");
}

/** Builds the project rollup and triggers a same-origin blob download,
 *  mirroring `downloadDatasourceContextSnapshot`. */
export async function downloadProjectContextSnapshot(
  project: ProjectRead,
  datasources: DataSourceRead[],
  format: "markdown" | "json",
  signal?: AbortSignal,
): Promise<ProjectContextSnapshot> {
  const snapshot = await buildProjectContextSnapshot(project, datasources, signal);
  const slug = project.slug || project.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const isMarkdown = format === "markdown";
  const content = isMarkdown ? renderProjectContextSnapshotMarkdown(snapshot) : JSON.stringify(snapshot, null, 2);
  const blob = new Blob([content], { type: isMarkdown ? "text/markdown" : "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `context-project-${slug}.${isMarkdown ? "md" : "json"}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return snapshot;
}
