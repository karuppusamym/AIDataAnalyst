import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DataQualityIncidentRead, DataQualitySummaryRead } from "../lib/types";
import {
  ApiError,
  fetchQualityIncidents,
  fetchQualitySummary,
  transitionQualityIncident,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./QualityScreen.css";

/* ---------------------------------------------------------------------------
   Quality — UX-15/UX-16 (tracker 03-tracker.md §M), built on the Catalog
   pattern against the real, already-merged `quality_api.py` endpoints:

     GET  /v1/datasources/{id}/quality-summary     -- the tiles at the top
     GET  /v1/datasources/{id}/quality-incidents    -- the primary list
     POST /v1/quality-incidents/{id}/transition     -- acknowledge/resolve

   Scoping, URL-held filters, one sequence-guarded pair of abortable requests
   per datasource/filter change, a virtualized list, and a permalinkable
   detail panel all follow `CatalogScreen` and `ReviewQueueScreen`.

   Honest scope note: the detail panel resolves the selected incident from
   the already-loaded list (`incidents.find`), exactly the way
   `ReviewQueueScreen`'s own focused-proposal panel does -- there is no
   `GET /v1/quality-incidents/{id}` endpoint to resolve a permalink
   independently of the current filter/page, unlike `EvidencePane`'s
   `tableId`-only resolution against UX-13's dedicated evidence route. A
   `?incident=` link only opens if the linked incident is still within the
   current status/severity filter and the (200-row) loaded page.

   Bulk transition was deliberately left out: `transition_quality_incident`
   takes one incident and always requires its own >=3-char reason
   (`DataQualityIncidentTransition`), so a bulk action would mean serializing
   N reason prompts for N incidents -- not a real bulk primitive, just a loop
   dressed up as one. A single, honest per-incident action is the complete
   MVP here, matching this screen's own transition endpoint shape.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const STATUS_OPTIONS = ["OPEN", "ACKNOWLEDGED", "RESOLVED"] as const;
const SEVERITY_OPTIONS = ["CRITICAL", "WARNING"] as const;

const nf = new Intl.NumberFormat("en-US");
const relTime = (iso: string | null): string => {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

const statusTone = (status: string): Tone =>
  status === "OPEN" ? "warn" : status === "ACKNOWLEDGED" ? "info" : status === "RESOLVED" ? "ok" : "mute";
const severityTone = (severity: string): Tone =>
  severity === "CRITICAL" ? "bad" : severity === "WARNING" ? "warn" : "mute";
const scanTone = (status: string): Tone =>
  status === "CURRENT" ? "ok" : status === "STALE" ? "warn" : "mute";

function humanize(s: string): string {
  return s.toLowerCase().replace(/_/g, " ");
}

function IncidentRow({
  incident,
  focused,
  onFocus,
  onTransition,
  transitioning,
}: {
  incident: DataQualityIncidentRead;
  focused: boolean;
  onFocus: () => void;
  onTransition: (status: "ACKNOWLEDGED" | "RESOLVED") => void;
  transitioning: boolean;
}) {
  return (
    <article
      className={`qinc${focused ? " qinc--focused" : ""}`}
      aria-label={`${incident.table_name} ${humanize(incident.anomaly_type)}`}
    >
      <header className="qinc__head">
        <div className="qinc__badges">
          <Pill tone={severityTone(incident.severity)}>{incident.severity.toLowerCase()}</Pill>
          <Pill tone={statusTone(incident.status)}>{incident.status.toLowerCase()}</Pill>
          <Pill tone="mute">{humanize(incident.anomaly_type)}</Pill>
        </div>
        <button className="qinc__title" onClick={onFocus}>
          {incident.table_name}
        </button>
      </header>
      <p className="qinc__summary">{incident.summary}</p>
      <div className="qinc__meta">
        <span>{nf.format(incident.occurrence_count)} occurrence{incident.occurrence_count === 1 ? "" : "s"}</span>
        <span>last observed {relTime(incident.last_observed_at)}</span>
      </div>
      <div className="qinc__act">
        {incident.status === "RESOLVED" ? (
          <span className="qinc__done">
            Resolved{incident.resolved_by ? ` by ${incident.resolved_by}` : ""}
            {incident.resolution_reason ? ` — ${incident.resolution_reason}` : ""}
          </span>
        ) : (
          <>
            {incident.status === "OPEN" ? (
              <Button disabled={transitioning} onClick={() => onTransition("ACKNOWLEDGED")}>
                Acknowledge
              </Button>
            ) : null}
            <Button variant="primary" disabled={transitioning} onClick={() => onTransition("RESOLVED")}>
              Resolve
            </Button>
          </>
        )}
      </div>
    </article>
  );
}

export function QualityScreen() {
  const [params, setParams] = useUrlState();
  const dsId = params.get("ds");
  const statusFilter = params.get("status") ?? "ALL";
  const severityFilter = params.get("severity") ?? "ALL";
  const selectedId = params.get("incident");

  const { datasources, error: dsPickerError } = useDatasourcePicker(ORG);

  const [summary, setSummary] = useState<DataQualitySummaryRead | null>(null);
  const [incidents, setIncidents] = useState<DataQualityIncidentRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [transitioningId, setTransitioningId] = useState<string | null>(null);

  // One in-flight pair of requests at a time. Aborting the previous pair is
  // what stops a slow load from a stale datasource/filter combination from
  // overwriting the results of a newer one (same guard as CatalogScreen's
  // `loadFirstPage`).
  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    inflight.current?.abort();
    if (!dsId) {
      setSummary(null);
      setIncidents([]);
      setTotal(null);
      setLoading(false);
      setError(null);
      return;
    }
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const [summaryResult, incidentsPage] = await Promise.all([
        fetchQualitySummary(dsId, ac.signal),
        fetchQualityIncidents(
          dsId,
          {
            status: statusFilter === "ALL" ? null : statusFilter,
            severity: severityFilter === "ALL" ? null : severityFilter,
            limit: 200,
          },
          ac.signal,
        ),
      ]);
      if (seq !== reqSeq.current) return;
      setSummary(summaryResult);
      setIncidents(incidentsPage.items);
      setTotal(incidentsPage.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [dsId, statusFilter, severityFilter]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const transition = useCallback(
    async (incidentId: string, status: "ACKNOWLEDGED" | "RESOLVED") => {
      const reason = window.prompt(
        status === "ACKNOWLEDGED"
          ? "A reason is required to acknowledge this incident:"
          : "A reason is required to resolve this incident:",
      );
      // The endpoint itself requires a non-empty (>=3 char) reason on both
      // transitions -- checked client-side too so a blank prompt doesn't
      // round-trip to a 422.
      if (!reason || reason.trim().length < 3) return;
      setTransitioningId(incidentId);
      try {
        await transitionQualityIncident(incidentId, { status, reason: reason.trim() });
        await load();
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setTransitioningId(null);
      }
    },
    [load],
  );

  const selected = useMemo(
    () => incidents.find((i) => i.id === selectedId) ?? null,
    [incidents, selectedId],
  );

  const dsLabel = datasourceName(datasources, dsId);
  const statusCounts = Object.entries(summary?.status_counts ?? {});

  return (
    <div className="qual">
      <header className="qual__head">
        <div>
          <h1 className="qual__h1">Quality</h1>
          <p className="qual__lede">
            Open incidents and scan freshness for one datasource, and the actions that
            clear them — acknowledge to say it's seen, resolve to close it out.
          </p>
        </div>
      </header>

      <div className="qual__filters">
        <Field label="Datasource">
          <select
            value={dsId ?? ""}
            onChange={(e) => setParams({ ds: e.target.value || null, incident: null })}
          >
            <option value="">Select a datasource…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
        </Field>
        <Field label="Status">
          <select
            value={statusFilter}
            onChange={(e) => setParams({ status: e.target.value === "ALL" ? null : e.target.value })}
          >
            <option value="ALL">All statuses</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>{humanize(s)}</option>
            ))}
          </select>
        </Field>
        <Field label="Severity">
          <select
            value={severityFilter}
            onChange={(e) => setParams({ severity: e.target.value === "ALL" ? null : e.target.value })}
          >
            <option value="ALL">All severities</option>
            {SEVERITY_OPTIONS.map((s) => (
              <option key={s} value={s}>{humanize(s)}</option>
            ))}
          </select>
        </Field>
      </div>

      {!dsId ? (
        <Empty
          title="Pick a datasource to see its quality signals"
          hint={dsPickerError ?? "Incidents and scan freshness are scoped per datasource."}
        />
      ) : (
        <>
          {summary ? (
            <>
              <div className="qual__tiles">
                <div className="tile">
                  <div className="tile__n tnum">
                    {nf.format(summary.observed_table_count)}<span className="tile__of">/{nf.format(summary.table_count)}</span>
                  </div>
                  <div className="tile__l">tables observed</div>
                </div>
                <div className="tile tile--warn">
                  <div className="tile__n tnum">{nf.format(summary.open_incident_count)}</div>
                  <div className="tile__l">open incidents</div>
                </div>
                <div className="tile tile--bad">
                  <div className="tile__n tnum">{nf.format(summary.critical_incident_count)}</div>
                  <div className="tile__l">critical</div>
                </div>
                <div className="tile tile--ok">
                  <div className="tile__n tnum">
                    {summary.average_quality_score !== null ? summary.average_quality_score.toFixed(1) : "—"}
                  </div>
                  <div className="tile__l">avg quality score</div>
                </div>
              </div>
              <div className="qual__scan">
                <Pill tone={scanTone(summary.metadata_scan_status)}>
                  scan {humanize(summary.metadata_scan_status)}
                </Pill>
                <span className="qual__scanhint">
                  last observed {relTime(summary.last_observed_at)}
                  {summary.metadata_scan_age_minutes !== null
                    ? ` · ${nf.format(Math.round(summary.metadata_scan_age_minutes))}m since last scan`
                    : ""}
                </span>
                {statusCounts.length > 0 ? (
                  <span className="qual__statuscounts">
                    {statusCounts.map(([status, count]) => (
                      <Pill key={status} tone="mute">{humanize(status)} {nf.format(count)}</Pill>
                    ))}
                  </span>
                ) : null}
              </div>
            </>
          ) : null}

          <div className="qual__main">
            {error ? (
              <ErrorState title="Quality could not be loaded" detail={error} onRetry={() => void load()} />
            ) : loading ? (
              <div className="qual__skeleton" role="status" aria-live="polite">
                Loading quality…
              </div>
            ) : incidents.length === 0 ? (
              <Empty
                title="No incidents match this filter"
                hint={dsLabel ? `${dsLabel} has no incidents in this status/severity.` : undefined}
              />
            ) : (
              <VirtualList
                items={incidents}
                getKey={(i) => i.id}
                ariaLabel="Quality incidents"
                estimateSize={150}
                totalCount={total}
                renderItem={(incident) => (
                  <IncidentRow
                    incident={incident}
                    focused={incident.id === selectedId}
                    onFocus={() => setParams({ incident: incident.id })}
                    onTransition={(status) => void transition(incident.id, status)}
                    transitioning={transitioningId === incident.id}
                  />
                )}
              />
            )}

            {selected ? (
              <aside className="evp qual__evidence" aria-label={`Incident detail for ${selected.table_name}`}>
                <header className="evp__head">
                  <div className="evp__title">
                    <div className="evp__name">{selected.table_name}</div>
                    <div className="evp__path">
                      {humanize(selected.anomaly_type)} · {selected.severity.toLowerCase()} · {selected.source ?? "INTERNAL"}
                    </div>
                  </div>
                  <button className="evp__x" onClick={() => setParams({ incident: null })} aria-label="Close incident detail">
                    ×
                  </button>
                </header>
                <div className="evp__body">
                  <p className="qual__evsummary">{selected.summary}</p>
                  <ol className="evl">
                    <li className="evi evi--info">
                      <div className="evi__label">first observed</div>
                      <div className="evi__value">{new Date(selected.first_observed_at).toLocaleString()}</div>
                    </li>
                    <li className="evi evi--info">
                      <div className="evi__label">last observed</div>
                      <div className="evi__value">{new Date(selected.last_observed_at).toLocaleString()}</div>
                    </li>
                    <li className="evi evi--info">
                      <div className="evi__label">occurrences</div>
                      <div className="evi__value">{nf.format(selected.occurrence_count)}</div>
                    </li>
                    {selected.acknowledged_by ? (
                      <li className="evi evi--info">
                        <div className="evi__label">acknowledged</div>
                        <div className="evi__value">
                          {selected.acknowledged_by} · {new Date(selected.acknowledged_at!).toLocaleString()}
                        </div>
                      </li>
                    ) : null}
                    {selected.resolved_by ? (
                      <li className="evi evi--info">
                        <div className="evi__label">resolved</div>
                        <div className="evi__value">
                          {selected.resolved_by} · {new Date(selected.resolved_at!).toLocaleString()}
                        </div>
                        <div className="evi__source">{selected.resolution_reason}</div>
                      </li>
                    ) : null}
                  </ol>
                  <div className="qual__evjson">
                    <div className="evp__sub">Evidence</div>
                    <pre className="qual__evpre">{JSON.stringify(selected.evidence, null, 2)}</pre>
                  </div>
                </div>
                <footer className="evp__foot">
                  {selected.status !== "RESOLVED" ? (
                    <>
                      {selected.status === "OPEN" ? (
                        <Button
                          disabled={transitioningId === selected.id}
                          onClick={() => void transition(selected.id, "ACKNOWLEDGED")}
                        >
                          Acknowledge
                        </Button>
                      ) : null}
                      <Button
                        variant="primary"
                        disabled={transitioningId === selected.id}
                        onClick={() => void transition(selected.id, "RESOLVED")}
                      >
                        Resolve
                      </Button>
                    </>
                  ) : null}
                  <Button
                    onClick={() => {
                      const permalink = `${location.origin}${location.pathname}?ds=${dsId}&incident=${selected.id}`;
                      void navigator.clipboard?.writeText(permalink);
                    }}
                  >
                    Copy permalink
                  </Button>
                </footer>
              </aside>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
