import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DataSourceRead, ConnectorHealthScoreRead } from "../lib/types";
import { ApiError, fetchDatasourceHealth, fetchOrgDatasources } from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
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

function HealthPane({
  source,
  onClose,
}: {
  source: DataSourceRead;
  onClose: () => void;
}) {
  const [health, setHealth] = useState<ConnectorHealthScoreRead | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const ac = new AbortController();
    setHealth(null);
    setError(null);
    setCopied(false);
    fetchDatasourceHealth(source.id, ac.signal)
      .then(setHealth)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e as Error);
      });
    return () => ac.abort();
  }, [source.id]);

  const permalink = `${location.origin}${location.pathname}?source=${source.id}`;

  return (
    <aside className="evp" aria-label={`Health for ${source.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={source.name}>{source.name}</div>
          <div className="evp__path">
            {source.connector_type.toLowerCase()} · {source.dialect} · {source.environment.toLowerCase()}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close health">×</button>
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

            <div className="evp__sub" style={{ marginTop: 14 }}>Factor breakdown</div>
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
          </>
        )}
      </div>

      <footer className="evp__foot">
        <Button
          onClick={() => {
            void navigator.clipboard?.writeText(permalink);
            setCopied(true);
          }}
        >
          {copied ? "Link copied" : "Copy source link"}
        </Button>
        <span className="evp__hint">Per-source · not fanned out to every row</span>
      </footer>
    </aside>
  );
}

export function SourcesScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const q = params.get("q") ?? "";
  const statusFilter = params.get("status") ?? "ALL";
  const selectedId = params.get("source");

  const [sources, setSources] = useState<DataSourceRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draftQ, setDraftQ] = useState(q);

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
          <HealthPane source={selected} onClose={() => setParams({ source: null })} />
        ) : selectedId ? (
          <aside className="evp evp--idle" aria-label="Health">
            <Empty
              title="Source not in the loaded fleet"
              hint="This permalink points at a source outside the current 500-source page."
            />
          </aside>
        ) : (
          <aside className="evp evp--idle" aria-label="Health">
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
