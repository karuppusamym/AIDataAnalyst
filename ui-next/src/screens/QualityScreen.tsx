import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  DataQualityIncidentRead,
  DataQualityIncidentTriageRead,
  DataQualitySummaryRead,
  FreshnessConfigRead,
  FreshnessStatusRead,
} from "../lib/types";
import type { MetadataTableRead } from "../lib/ui-types";
import {
  ApiError,
  approveFreshnessConfig,
  fetchFreshnessConfigs,
  fetchFreshnessStatus,
  fetchQualityIncidentTriage,
  fetchQualityIncidents,
  fetchQualitySummary,
  fetchTablesLegacy,
  transitionQualityIncident,
  upsertFreshnessConfig,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { VirtualList } from "../components/VirtualList";
import { CrossLinks } from "../components/CrossLinks";
import { Button, ConfirmDialog, CopyLinkButton, Empty, ErrorState, Field, Pill } from "../components/primitives";
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

import { useOrgId } from "../lib/org";

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
/* FRESH is good, STALE is bad, and the two "we are not measuring this"
   states are deliberately NOT green -- an unapproved contract measures
   nothing, and colouring it as if it did is the exact misreading ADR-0016
   exists to prevent. */
const freshnessTone = (status: string): Tone =>
  status === "FRESH" ? "ok" : status === "STALE" ? "bad" : status === "AWAITING_APPROVAL" ? "warn" : "mute";

function humanize(s: string): string {
  return s.toLowerCase().replace(/_/g, " ");
}

function TriagePanel({ incidentId }: { incidentId: string }) {
  const [triage, setTriage] = useState<DataQualityIncidentTriageRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    fetchQualityIncidentTriage(incidentId, ac.signal)
      .then((result) => {
        setTriage(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if ((err as Error)?.name === "AbortError") return;
        setError(err instanceof ApiError ? err.message : String(err));
        setLoading(false);
      });
    return () => ac.abort();
  }, [incidentId]);

  if (loading) return <p className="qinc__triageload" role="status">Suggesting a root cause…</p>;
  if (error) return <p className="qinc__triageerror" role="alert">{error}</p>;
  if (!triage) return null;

  return (
    <div className="qinc__triage" aria-label="Suggested root cause">
      <div className="qinc__triagesection">
        <span className="qinc__triagelabel">Likely cause</span>
        <ul>
          {triage.likely_causes.map((cause) => (
            <li key={cause}>{cause}</li>
          ))}
        </ul>
      </div>
      <div className="qinc__triagesection">
        <span className="qinc__triagelabel">Suggested next step</span>
        <ul>
          {triage.recommended_next_steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ul>
      </div>
      {triage.basis.length > 0 && (
        <p className="qinc__triagebasis">
          Based on: {triage.basis.join(", ")} — check these fields in the incident's own evidence.
        </p>
      )}
    </div>
  );
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
  const [triageOpen, setTriageOpen] = useState(false);
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
      {/* An incident is about a table. Before this, reading one and then
          looking at that table meant re-finding it by name in the Catalog. */}
      <CrossLinks
        label="Open the affected asset in"
        links={[
          { screen: "catalog", label: "Catalog", params: { asset: incident.table_id }, title: `Evidence for ${incident.table_name}` },
          { screen: "lineage", label: "Lineage", params: { ds: incident.datasource_id, node: incident.table_id }, title: "What feeds this table, and what it feeds" },
          { screen: "meaning", label: "Business meaning", params: { ds: incident.datasource_id, asset: incident.table_id } },
        ]}
      />
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
        <Button onClick={() => setTriageOpen((open) => !open)} title="A deterministic root-cause hint, computed on demand — never stored">
          {triageOpen ? "Hide suggested cause" : "Suggest root cause"}
        </Button>
      </div>
      {triageOpen && <TriagePanel incidentId={incident.id} />}
    </article>
  );
}

/* ---------------------------------------------------------------------------
   DQ-2 watermark contracts (R11-B8).

   `quality_api.py` has had all four of these routes for some time and no
   screen called any of them. The consequence was not a missing panel, it was
   a dead feature: a contract can only be created through the maker route and
   can only leave PENDING_APPROVAL through the checker route, so with neither
   reachable every table in the product reported AWAITING_APPROVAL forever and
   the scheduled evaluation this row adds would have found nothing approved.

   Maker-checker is enforced SERVER-SIDE (`approve_freshness_config` refuses
   the contract's own author with 403). This panel deliberately shows the
   Approve button to everyone and surfaces that 403 verbatim, rather than
   hiding the action from whoever authored the contract. Hiding it would teach
   the rule only to people who already know it, and would quietly imply the
   client is the thing enforcing it -- an approval gate you cannot see refuse
   is an approval gate nobody trusts.
--------------------------------------------------------------------------- */

/** One page of contracts, and the cap on the per-table status calls below. */
const FRESHNESS_PAGE_LIMIT = 25;

function FreshnessPanel({ datasourceId }: { datasourceId: string }) {
  const [configs, setConfigs] = useState<FreshnessConfigRead[]>([]);
  const [total, setTotal] = useState(0);
  const [states, setStates] = useState<Record<string, FreshnessStatusRead>>({});
  const [tables, setTables] = useState<MetadataTableRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [tableId, setTableId] = useState("");
  const [watermarkColumn, setWatermarkColumn] = useState("");
  const [threshold, setThreshold] = useState("60");
  const [busyTableId, setBusyTableId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
      const [page, tablePage] = await Promise.all([
        fetchFreshnessConfigs(datasourceId, { limit: FRESHNESS_PAGE_LIMIT }, ac.signal),
        fetchTablesLegacy(datasourceId, { limit: 200 }, ac.signal),
      ]);
      if (seq !== reqSeq.current) return;
      setConfigs(page.items);
      setTotal(page.total);
      setTables(tablePage.items);
      /* N+1, and named rather than hidden: `list_freshness_configs` returns
         the CONTRACTS, and the evaluated verdict for a table is a route of
         its own (`get_freshness_status`) -- there is no bulk evaluate route
         to call instead. Bounded by the page limit above, so the panel makes
         at most FRESHNESS_PAGE_LIMIT of them and never grows with the
         estate. A bulk route would be the right fix; it needs a new response
         DTO, which is another session's file this round. */
      const evaluated = await Promise.all(
        page.items.map((config) =>
          fetchFreshnessStatus(datasourceId, config.table_id, ac.signal).catch(() => null),
        ),
      );
      if (seq !== reqSeq.current) return;
      const byTable: Record<string, FreshnessStatusRead> = {};
      evaluated.forEach((state) => {
        if (state) byTable[state.table_id] = state;
      });
      setStates(byTable);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [datasourceId]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const tableName = useCallback(
    (id: string) => tables.find((t) => t.id === id)?.name ?? id,
    [tables],
  );

  const save = useCallback(async () => {
    const minutes = Number(threshold);
    if (!tableId) {
      setActionError("Pick the table this contract is about.");
      return;
    }
    if (!watermarkColumn.trim()) {
      setActionError("Name the column that carries the data's own timestamp.");
      return;
    }
    if (!Number.isInteger(minutes) || minutes < 1) {
      setActionError("The threshold is a whole number of minutes, at least 1.");
      return;
    }
    setBusyTableId(tableId);
    setActionError(null);
    setNotice(null);
    try {
      await upsertFreshnessConfig(datasourceId, tableId, {
        watermark_column: watermarkColumn.trim(),
        threshold_minutes: minutes,
      });
      setNotice(
        `Saved for ${tableName(tableId)}. It stays pending until a second principal approves it — ` +
          "freshness is not evaluated before then.",
      );
      setFormOpen(false);
      setWatermarkColumn("");
      await load();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusyTableId(null);
    }
  }, [datasourceId, tableId, watermarkColumn, threshold, tableName, load]);

  const approve = useCallback(
    async (config: FreshnessConfigRead) => {
      setBusyTableId(config.table_id);
      setActionError(null);
      setNotice(null);
      try {
        await approveFreshnessConfig(datasourceId, config.table_id);
        setNotice(
          `Approved for ${tableName(config.table_id)}. Freshness is evaluated against its watermark from now on.`,
        );
        await load();
      } catch (e) {
        // 403 (the author cannot approve their own contract) and 409 (not
        // pending) both land here and are shown as the server worded them.
        setActionError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setBusyTableId(null);
      }
    },
    [datasourceId, tableName, load],
  );

  return (
    <section className="qfr" aria-label="Freshness watermarks">
      <header className="qfr__head">
        <div>
          <h2 className="qfr__h2">Freshness watermarks</h2>
          <p className="qfr__lede">
            How late this table&rsquo;s <em>data</em> is, from a column it carries — not when
            Atlas last scanned it. A contract is evaluated only after a second principal
            approves it, and a scheduled violation opens an incident in the list below.
          </p>
        </div>
        <Button onClick={() => setFormOpen((open) => !open)}>
          {formOpen ? "Cancel" : "Configure a watermark"}
        </Button>
      </header>

      {formOpen ? (
        <div className="qfr__form">
          <Field label="Table">
            <select value={tableId} onChange={(e) => setTableId(e.target.value)}>
              <option value="">Select a table…</option>
              {tables.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Watermark column">
            <input
              value={watermarkColumn}
              placeholder="updated_at"
              onChange={(e) => setWatermarkColumn(e.target.value)}
            />
          </Field>
          <Field label="Threshold (minutes)">
            <input
              type="number"
              min={1}
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
            />
          </Field>
          <Button variant="primary" disabled={busyTableId !== null} onClick={() => void save()}>
            Save contract
          </Button>
        </div>
      ) : null}

      {actionError ? (
        <p className="qfr__error" role="alert">{actionError}</p>
      ) : null}
      {notice ? (
        <p className="qfr__notice" role="status">{notice}</p>
      ) : null}

      {error ? (
        <ErrorState
          title="Freshness contracts could not be loaded"
          detail={error}
          onRetry={() => void load()}
        />
      ) : loading ? (
        <p className="qfr__loading" role="status">Loading freshness contracts…</p>
      ) : configs.length === 0 ? (
        <Empty
          title="No table here has a freshness contract"
          hint="Configure a watermark column and a threshold, then have a second principal approve it."
        />
      ) : (
        <>
          <ul className="qfr__list">
            {configs.map((config) => {
              const state = states[config.table_id];
              const pending = config.status === "PENDING_APPROVAL";
              return (
                <li className="qfr__row" key={config.id}>
                  <div className="qfr__rowmain">
                    <span className="qfr__name">{tableName(config.table_id)}</span>
                    <span className="qfr__cfg">
                      {config.watermark_column} · {nf.format(config.threshold_minutes)}m threshold
                    </span>
                  </div>
                  <div className="qfr__badges">
                    <Pill tone={pending ? "warn" : "ok"}>{humanize(config.status)}</Pill>
                    {state ? (
                      <Pill tone={freshnessTone(state.status)}>{humanize(state.status)}</Pill>
                    ) : null}
                    {state?.age_minutes !== null && state?.age_minutes !== undefined ? (
                      <span className="qfr__age tnum">
                        watermark {nf.format(Math.round(state.age_minutes))}m old
                      </span>
                    ) : null}
                  </div>
                  <div className="qfr__act">
                    {config.approved_by ? (
                      <span className="qfr__approved">approved by {config.approved_by}</span>
                    ) : null}
                    {pending ? (
                      <Button
                        disabled={busyTableId === config.table_id}
                        onClick={() => void approve(config)}
                        title="A different principal than the one who saved it — the server refuses self-approval"
                      >
                        Approve
                      </Button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
          {total > configs.length ? (
            <p className="qfr__more">
              Showing {nf.format(configs.length)} of {nf.format(total)} contracts.
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}

export function QualityScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const statusFilter = params.get("status") ?? "ALL";
  const severityFilter = params.get("severity") ?? "ALL";
  const selectedId = params.get("incident");

  const { datasources, error: dsPickerError, preferredDatasourceId } = useDatasourcePicker(ORG);
  const dsId = params.get("ds") ?? preferredDatasourceId;

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

  /* Both transitions are recorded with a written reason -- the endpoint
     requires one of at least three characters. It was collected with
     `window.prompt`, which cannot be labelled, cannot show the length rule,
     and returns `null` when a browser blocks it, which this screen could not
     tell apart from "the user cancelled" (review 2026-09-05, F21). */
  const [transitionRequest, setTransitionRequest] = useState<{
    incidentId: string;
    status: "ACKNOWLEDGED" | "RESOLVED";
  } | null>(null);
  const [transitionError, setTransitionError] = useState<string | null>(null);

  const transition = useCallback(
    async (incidentId: string, status: "ACKNOWLEDGED" | "RESOLVED", reason: string) => {
      if (reason.trim().length < 3) {
        setTransitionError("Give at least three characters of reason; the server requires one.");
        return;
      }
      setTransitioningId(incidentId);
      setTransitionError(null);
      try {
        await transitionQualityIncident(incidentId, { status, reason: reason.trim() });
        setTransitionRequest(null);
        await load();
      } catch (e) {
        setTransitionError(e instanceof ApiError ? e.detail : (e as Error).message);
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

          <FreshnessPanel datasourceId={dsId} />

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
                    onTransition={(status) =>
                      setTransitionRequest({ incidentId: incident.id, status })
                    }
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
                  {selected.status !== "RESOLVED" ? (
                    <div className="qual__coupling">
                      <div className="evp__sub">Runtime coupling (DQ-3)</div>
                      <p className="qual__couplingtext">
                        While {humanize(selected.status)}, this incident
                        {selected.severity === "CRITICAL"
                          ? " blocks governed tools that depend on this table and refuses agent answers grounded in it (fail-closed), and heavily demotes it in retrieval ranking"
                          : " demotes this table in retrieval ranking and flags (but does not block) governed tools that depend on it"}
                        , attaches a trust warning to any agent answer that uses it, and shows on this
                        table&rsquo;s lineage impact graph — not just here.
                      </p>
                    </div>
                  ) : null}
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
                          onClick={() =>
                            setTransitionRequest({
                              incidentId: selected.id,
                              status: "ACKNOWLEDGED",
                            })
                          }
                        >
                          Acknowledge
                        </Button>
                      ) : null}
                      <Button
                        variant="primary"
                        disabled={transitioningId === selected.id}
                        onClick={() =>
                          setTransitionRequest({ incidentId: selected.id, status: "RESOLVED" })
                        }
                      >
                        Resolve
                      </Button>
                    </>
                  ) : null}
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/quality`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
                  <CopyLinkButton
                    target={{ screen: "quality", params: { ds: dsId, incident: selected.id } }}
                    label="Copy permalink"
                  />
                </footer>
              </aside>
            ) : null}
          </div>
        </>
      )}

      {transitionRequest ? (
        <ConfirmDialog
          title={
            transitionRequest.status === "ACKNOWLEDGED"
              ? "Acknowledge this incident"
              : "Resolve this incident"
          }
          description={
            transitionRequest.status === "ACKNOWLEDGED"
              ? "Acknowledging records that someone has picked this up. The incident stays open."
              : "Resolving closes the incident. Downstream tool gates that were failing closed on it are released."
          }
          reasonLabel="What did you find? (at least three characters)"
          requireReason
          confirmLabel={
            transitionRequest.status === "ACKNOWLEDGED" ? "Acknowledge" : "Resolve incident"
          }
          busy={transitioningId === transitionRequest.incidentId}
          error={transitionError}
          onCancel={() => {
            setTransitionRequest(null);
            setTransitionError(null);
          }}
          onConfirm={(reason) =>
            void transition(transitionRequest.incidentId, transitionRequest.status, reason)
          }
        />
      ) : null}
    </div>
  );
}
