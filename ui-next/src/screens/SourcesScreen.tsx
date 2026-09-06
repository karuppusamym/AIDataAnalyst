import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DataSourceRead, ConnectorHealthScoreRead } from "../lib/types";
import {
  ApiError,
  downloadDatasourceContextSnapshot,
  downloadProjectContextSnapshot,
  fetchDatasourceHealth,
  fetchOrgDatasources,
} from "../lib/api";
import { downloadDatasourceModelWorkbook } from "../lib/_column_documentation_api";
import { WorkbookImport } from "../components/WorkbookImport";
import { useScopeSelection } from "../lib/scope";
import { useSession } from "../lib/session";
import { useUrlState } from "../lib/useUrlState";
import { VirtualList } from "../components/VirtualList";
import { CrossLinks } from "../components/CrossLinks";
import { Button, CopyLinkButton, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./SourcesScreen.css";

/* ---------------------------------------------------------------------------
   Sources — nav id `sources`, the fleet console for every datasource the
   platform can see (UX-15/UX-16, `Docs/60-delivery/03-tracker.md` §M).

   Built on the Catalog pattern the same way every migrated screen is
   (`CatalogScreen`/`MarketplaceScreen`): URL-held filter/selection state, one
   abortable request in flight per view, a virtualized list, and a
   permalinkable evidence-style detail pane -- but assembled from the real
   already-merged endpoints listed below, not an invented "sources" API.

   1. List: reuses `fetchOrgDatasources` (`api.ts`, already called by
      `NarratedLineageScreen`'s datasource picker) against
      `GET /v1/organizations/{org}/datasources` -- no second copy of that
      call. That function fetches one page of up to 500 sources and has no
      free-text/status query parameter, so name/status filtering here is
      client-side over the loaded fleet, the same way `NarratedLineageScreen`
      client-filters catalog rows by datasource name. A fleet past 500
      sources would need `fetchOrgDatasources` itself to grow cursor/offset
      paging first -- an honest, stated gap, not silently truncated data.

      Known pre-existing type note (tracker UX-15's own comment on this same
      function, UX-20's context): `fetchOrgDatasources` is typed as
      `PageOf<DataSourceRead>`, but the real endpoint
      (`operational_api.py::list_organization_datasources`) returns
      `DataSourceSummaryRead` items (`connectivity/schemas.py:58`), which is
      `DataSourceRead` minus `credential_reference`. Every field this screen
      reads (`id`/`name`/`connector_type`/`dialect`/`environment`/
      `network_zone`/`status`/`max_concurrency`/`updated_at`) is present in
      both shapes, so this renders off the real response correctly; the
      shared type itself is left untouched per UX-20's own note not to "fix"
      it without the same context that row had.

   2. Health: `GET /v1/datasources/{id}/health` (new `fetchDatasourceHealth`
      below) -- fetched ONLY for the selected source, not fanned out per
      visible row. For a large fleet, N unbounded parallel health calls (one
      per row, eager on load) would be the wrong default; health appears in
      the detail pane once a source is selected, permalinkable via `?source=`
      exactly like `EvidencePane`'s `?asset=`.

   Scope cuts, stated rather than silently dropped: no connector
   capability-matrix reference panel and no per-source connector-certification
   list (`ingestion_api.py`'s two optional secondary endpoints) -- both would
   have added a second and third data fetch/URL-state axis to a screen this
   task scoped around the health read model; the health factor breakdown
   below is the one this row's tracker context leads with.
--------------------------------------------------------------------------- */

import { useOrgId } from "../lib/org";

const statusTone = (status: string): Tone =>
  status === "ACTIVE" ? "ok" : status === "DISABLED" ? "bad" : "mute";

const healthTone = (status: string): Tone =>
  status === "HEALTHY" ? "ok" : status === "DEGRADED" ? "warn" : status === "CRITICAL" ? "bad" : "mute";

const BLOCKER_LABEL: Record<string, string> = {
  NO_RUN_HISTORY: "No run history yet",
  NO_SUCCESSFUL_RUN: "No successful run recorded",
  DATASOURCE_DISABLED: "Administratively disabled",
  REPEATED_FAILURES: "Repeated recent failures",
};

function SourceRow({
  source,
  selected,
  onSelect,
}: {
  source: DataSourceRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`src${selected ? " src--sel" : ""}`} aria-label={source.name}>
      <button className="src__click" onClick={onSelect} aria-current={selected}>
        <div className="src__head">
          <span className="src__name" title={source.name}>{source.name}</span>
          <Pill tone={statusTone(source.status)}>{source.status.toLowerCase()}</Pill>
        </div>
        <div className="src__meta">
          <span>{source.connector_type.toLowerCase()}</span>
          <span>·</span>
          <span>{source.environment.toLowerCase()}</span>
          <span>·</span>
          <span>{source.network_zone ?? "default"}</span>
          <span>·</span>
          <span>concurrency {source.max_concurrency}</span>
        </div>
      </button>
    </article>
  );
}

function SourceDetailsPane({
  source,
  onClose,
}: {
  source: DataSourceRead;
  onClose: () => void;
}) {
  const roles = useSession().me?.roles;
  const canImportWorkbook =
    roles === undefined ||
    roles.some((role) =>
      ["PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward"].includes(role),
    );
  const [health, setHealth] = useState<ConnectorHealthScoreRead | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [generating, setGenerating] = useState<"markdown" | "json" | null>(null);
  const [generateNotice, setGenerateNotice] = useState<string | null>(null);
  const [exportingWorkbook, setExportingWorkbook] = useState(false);

  const exportWorkbook = useCallback(async () => {
    setExportingWorkbook(true);
    setGenerateNotice(null);
    try {
      await downloadDatasourceModelWorkbook(source.id, source.name);
      setGenerateNotice(
        "Workbook downloaded. Edit only the fields identified in its README sheet, then upload the saved file here.",
      );
    } catch (e) {
      setGenerateNotice(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setExportingWorkbook(false);
    }
  }, [source]);

  const generateSnapshot = useCallback(
    async (format: "markdown" | "json") => {
      setGenerating(format);
      setGenerateNotice(null);
      try {
        const snapshot = await downloadDatasourceContextSnapshot(source, format);
        setGenerateNotice(
          snapshot.warnings.length > 0
            ? `Downloaded with ${snapshot.warnings.length} section(s) unavailable — see the file for details.`
            : `Downloaded: ${snapshot.documented_count} documented / ${snapshot.undocumented_count} undocumented tables.`,
        );
      } catch (e) {
        setGenerateNotice(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setGenerating(null);
      }
    },
    [source],
  );

  useEffect(() => {
    const ac = new AbortController();
    setHealth(null);
    setError(null);
    fetchDatasourceHealth(source.id, ac.signal)
      .then(setHealth)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e as Error);
      });
    return () => ac.abort();
  }, [source.id]);



  return (
    <aside className="evp" aria-label={`Source details for ${source.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={source.name}>{source.name}</div>
          <div className="evp__path">
            {source.connector_type.toLowerCase()} · {source.dialect} · {source.environment.toLowerCase()}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close source details">×</button>
      </header>

      <div className="evp__body">
        {error ? (
          <div className="evp__error" role="alert">
            {error instanceof ApiError && error.status === 403
              ? "You are not authorized to view this source's health."
              : error instanceof ApiError && error.status === 404
                ? "This datasource no longer exists."
                : `Health could not be loaded: ${
                    error instanceof ApiError ? error.detail : error.message
                  }`}
          </div>
        ) : health === null ? (
          <div className="evp__load" role="status">Loading health…</div>
        ) : (
          <>
            <div className="src__score">
              <div className={`src__scorenum src__scorenum--${healthTone(health.status)}`}>
                {health.score}
              </div>
              <div className="src__scoremeta">
                <Pill tone={healthTone(health.status)}>{health.status.toLowerCase()}</Pill>
                <span className="src__computed">
                  computed {new Date(health.computed_at).toLocaleString()}
                </span>
              </div>
            </div>

            {health.blockers.length > 0 ? (
              <div className="src__blockers" role="alert">
                {health.blockers.map((b) => (
                  <Pill key={b} tone="warn">{BLOCKER_LABEL[b] ?? b}</Pill>
                ))}
              </div>
            ) : null}

          </>
        )}

        <section className="src__model" aria-labelledby="src-model-heading">
          <div className="evp__sub" id="src-model-heading">Model workbook</div>
          <div className="src__modelaction">
            <Button
              disabled={exportingWorkbook}
              onClick={() => void exportWorkbook()}
              title="Download every table, column and relationship for this source as an Excel workbook, for offline bulk review"
            >
              {exportingWorkbook ? "Exporting…" : "Download model (.xlsx)"}
            </Button>
            <span>This is a manual round trip; saving in Excel does not upload automatically.</span>
          </div>
          <p className="src__modellede">
            Tables, columns and data types are populated by discovery scans. Use the
            Catalog link below to inspect them. The workbook is for bulk business-description
            review, not for creating the physical schema.
          </p>
          <ol className="src__modelsteps">
            <li>Download the current model.</li>
            <li>Edit it in Excel or another spreadsheet app and save the file.</li>
            <li>Upload the saved file here to preview the changes.</li>
          </ol>
          {canImportWorkbook ? (
            <WorkbookImport datasourceId={source.id} />
          ) : (
            <p className="src__modelreadonly">
              Analysts can download and inspect this workbook. Uploading changes requires
              Data Steward, Metadata Admin, Data Admin or Platform Admin access.
            </p>
          )}
        </section>

        {health ? (
          <section className="src__factors" aria-labelledby="src-factors-heading">
            <div className="evp__sub" id="src-factors-heading">Factor breakdown</div>
            <ol className="evl">
              {health.factors.map((f) => (
                <li key={f.name} className={`evi evi--${healthTone(health.status) === "mute" ? "info" : healthTone(health.status)}`}>
                  <div className="evi__label">
                    {f.name.replace(/_/g, " ")} · {f.score}/{f.maximum}
                  </div>
                  <div className="evi__value">{f.reason}</div>
                  {Object.keys(f.evidence).length > 0 ? (
                    <div className="evi__source">
                      {Object.entries(f.evidence)
                        .map(([k, v]) => `${k}: ${v === null ? "—" : String(v)}`)
                        .join(" · ")}
                    </div>
                  ) : null}
                </li>
              ))}
            </ol>
          </section>
        ) : null}

        <div className="evp__links">
        {/* Everything downstream of a source is scoped by its id. Sources was
            a leaf screen; these are the places an operator goes next. */}
        <CrossLinks
          label="This source in"
          links={[
            { screen: "catalog", label: "Tables & columns", params: { ds: source.id }, title: "Discovered tables, column definitions and descriptions for this source" },
            { screen: "operations", label: "Operations", params: { ds: source.id, batch_ds: source.id }, title: "Analysis runs and ingestion batches for this source" },
            { screen: "quality", label: "Quality", params: { ds: source.id }, title: "Open incidents for this source" },
            { screen: "relationships", label: "Relationships", params: { ds: source.id }, title: "Key and relationship candidates" },
            { screen: "lineage", label: "Lineage", params: { ds: source.id }, title: "Narrated lineage for this source" },
          ]}
        />
        </div>
      </div>

      <footer className="evp__foot">
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/sources`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
        <CopyLinkButton
          target={{ screen: "sources", params: { source: source.id } }}
          label="Copy source link"
        />
        <Button
          disabled={generating !== null}
          onClick={() => void generateSnapshot("markdown")}
          title="Generate a Markdown context document for this datasource — table inventory, business meaning, quality and health, on demand"
        >
          {generating === "markdown" ? "Generating…" : "Generate context (.md)"}
        </Button>
        <Button
          disabled={generating !== null}
          onClick={() => void generateSnapshot("json")}
          title="Same snapshot as JSON, for programmatic use"
        >
          {generating === "json" ? "Generating…" : "Generate context (.json)"}
        </Button>
        <span className="evp__hint">Context snapshots</span>
      </footer>
      {generateNotice && (
        <p className="evp__notice" role="status">
          {generateNotice}
        </p>
      )}
    </aside>
  );
}

export function SourcesScreen() {
  const ORG = useOrgId();
  const scope = useScopeSelection();
  const [params, setParams] = useUrlState();
  const q = params.get("q") ?? "";
  const statusFilter = params.get("status") ?? "ALL";
  const selectedId = params.get("source");

  const [sources, setSources] = useState<DataSourceRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draftQ, setDraftQ] = useState(q);
  const [projectGenerating, setProjectGenerating] = useState<"markdown" | "json" | null>(null);
  const [projectNotice, setProjectNotice] = useState<string | null>(null);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchOrgDatasources(ORG, ac.signal);
      if (seq !== reqSeq.current) return;
      setSources(page.items);
      setTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (draftQ !== q) setParams({ q: draftQ || null });
    }, 250);
    return () => clearTimeout(t);
  }, [draftQ, q, setParams]);

  const statuses = useMemo(
    () => [...new Set(sources.map((s) => s.status))].sort(),
    [sources],
  );

  const filtered = useMemo(() => {
    let items = sources;
    if (q.trim()) {
      const needle = q.trim().toLowerCase();
      items = items.filter((s) => s.name.toLowerCase().includes(needle));
    }
    if (statusFilter !== "ALL") items = items.filter((s) => s.status === statusFilter);
    return items;
  }, [sources, q, statusFilter]);

  const selected = useMemo(
    () => sources.find((s) => s.id === selectedId) ?? null,
    [sources, selectedId],
  );

  const activeCount = sources.filter((s) => s.status === "ACTIVE").length;

  // Project rollup: the current scope's project, and the datasources within
  // it that this screen already loaded (org-wide) — filtered client-side,
  // no second fetch. `scope` is `null` for exactly one tick before
  // `ScopeProvider` resolves; every derived value below degrades to "no
  // project selected" rather than throwing.
  const currentProject = useMemo(
    () => scope?.projects.find((p) => p.id === scope.projectId) ?? null,
    [scope],
  );
  const projectDatasources = useMemo(
    () => (currentProject ? sources.filter((s) => s.project_id === currentProject.id) : []),
    [sources, currentProject],
  );

  const generateProjectSnapshot = useCallback(
    async (format: "markdown" | "json") => {
      if (!currentProject || projectDatasources.length === 0) return;
      setProjectGenerating(format);
      setProjectNotice(null);
      try {
        const snapshot = await downloadProjectContextSnapshot(currentProject, projectDatasources, format);
        setProjectNotice(
          snapshot.warnings.length > 0
            ? `Downloaded — ${snapshot.warnings.length} of ${snapshot.datasource_count} datasource(s) could not be included.`
            : `Downloaded: ${snapshot.datasource_count} datasource(s), ${snapshot.documented_count} documented / ${snapshot.undocumented_count} undocumented tables.`,
        );
      } catch (e) {
        setProjectNotice(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setProjectGenerating(null);
      }
    },
    [currentProject, projectDatasources],
  );

  return (
    <div className="srcscreen">
      <header className="srcscreen__head">
        <div>
          <h1 className="srcscreen__h1">Sources</h1>
          <p className="srcscreen__lede">
            Every datasource the platform can see, with a per-source health score
            you can drill into — computed from real run history, not a status flag.
          </p>
        </div>
        <div className="srcscreen__stats">
          <span><b className="tnum">{total !== null ? total : "—"}</b> sources</span>
          <span><b className="tnum">{activeCount}</b> active</span>
        </div>
      </header>

      {currentProject && (
        <div className="srcscreen__project" role="group" aria-label="Project context">
          <span className="srcscreen__projectlabel">
            Project <b>{currentProject.name}</b> — {projectDatasources.length} datasource(s) in scope
          </span>
          <Button
            disabled={projectGenerating !== null || projectDatasources.length === 0}
            onClick={() => void generateProjectSnapshot("markdown")}
            title="Generate one combined Markdown context document across every datasource in this project"
          >
            {projectGenerating === "markdown" ? "Generating…" : "Generate project context (.md)"}
          </Button>
          <Button
            disabled={projectGenerating !== null || projectDatasources.length === 0}
            onClick={() => void generateProjectSnapshot("json")}
            title="Same rollup as JSON"
          >
            {projectGenerating === "json" ? "Generating…" : "Generate project context (.json)"}
          </Button>
          {projectNotice && <span className="srcscreen__projectnotice">{projectNotice}</span>}
        </div>
      )}

      <div className="srcscreen__filters">
        <Field label="Search">
          <input
            type="search"
            value={draftQ}
            placeholder="datasource name…"
            onChange={(e) => setDraftQ(e.target.value)}
          />
        </Field>
        <Field label="Status">
          <select
            value={statusFilter}
            onChange={(e) => setParams({ status: e.target.value === "ALL" ? null : e.target.value })}
          >
            <option value="ALL">All</option>
            {statuses.map((s) => (
              <option key={s} value={s}>{s.toLowerCase()}</option>
            ))}
          </select>
        </Field>
      </div>

      <div className="srcscreen__main">
        {error ? (
          <ErrorState title="Sources could not be loaded" detail={error} onRetry={() => void load()} />
        ) : loading ? (
          <div className="srcscreen__skeleton" role="status" aria-live="polite">
            Loading sources…
          </div>
        ) : (
          <VirtualList
            items={filtered}
            getKey={(s) => s.id}
            ariaLabel="Datasources"
            estimateSize={78}
            totalCount={filtered.length}
            emptyState={
              <Empty
                title={sources.length === 0 ? "No datasources registered" : "No sources match these filters"}
                hint={sources.length === 0 ? undefined : "Try clearing the search or status filter."}
              />
            }
            renderItem={(s) => (
              <SourceRow source={s} selected={s.id === selectedId} onSelect={() => setParams({ source: s.id })} />
            )}
          />
        )}
        {selected ? (
          <SourceDetailsPane source={selected} onClose={() => setParams({ source: null })} />
        ) : selectedId ? (
          <aside className="evp evp--idle" aria-label="Source details">
            <Empty
              title="Source not in the loaded fleet"
              hint="This permalink points at a source outside the current 500-source page."
            />
          </aside>
        ) : (
          <aside className="evp evp--idle" aria-label="Source details">
            <Empty
              title="Select a source"
              hint="Its health score and factor breakdown — where every point came from — appears here."
            />
          </aside>
        )}
      </div>
    </div>
  );
}
