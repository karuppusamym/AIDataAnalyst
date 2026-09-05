import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AnalysisRunRead, FleetSummaryRead, MetadataIngestionBatchRead, OutboxEventRead } from "../lib/types";
import {
  ApiError,
  fetchAnalysisRuns,
  fetchFleetSummary,
  fetchIngestionBatches,
  fetchOutboxEvents,
  requeueOutboxEvent,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./OperationsScreen.css";

import { useOrgId } from "../lib/org";
const nf = new Intl.NumberFormat("en-US");

/* ---------------------------------------------------------------------------
   Operations — UX-16, the Catalog pattern applied to `operational_api.py`'s
   fleet-wide read routes plus `ingestion_api.py`'s per-datasource batches.

   There is no single ingestion-workflow dashboard endpoint. This screen is
   composed from four genuinely org-wide, already-merged routes:

     1. tiles         GET .../fleet-summary        (datasource/run status
                       counts, scan-policy and outbox backlog, at a glance)
     2. primary list   GET .../analysis-runs        (filterable by run status
                       and datasource, URL-held, virtualized)
     3. backlog panel  GET .../outbox-events        (dead-letter events, each
                       with a real POST .../requeue action)

   The real gap, stated rather than papered over: nothing aggregates
   ingestion-batch/Temporal-workflow status across every datasource in an
   org. `GET /v1/datasources/{id}/metadata-ingestion-batches` exists and
   carries exactly that detail (status, temporal_workflow_id, expected vs.
   received vs. processed chunks, error_class/error_message) but is scoped to
   one datasource — fanning it out over every datasource in the org to fake
   an aggregate would be an unbounded N+1 with no stated limit, so this
   screen does not do that. Instead:

     4. drill-down     GET .../{datasource_id}/metadata-ingestion-batches
                       — an explicitly secondary panel, reusing
                       `useDatasourcePicker` to pick ONE datasource at a
                       time. There is still no single cross-datasource
                       ingestion-job table; this panel is a supporting view
                       into one datasource, not that table.
--------------------------------------------------------------------------- */

const RUN_STATUSES = ["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"] as const;
const OUTBOX_STATUSES = ["PENDING", "PUBLISHED", "DEAD_LETTER"] as const;

const runStatusTone = (s: string): Tone =>
  s === "SUCCEEDED" ? "ok" : s === "FAILED" ? "bad" : s === "RUNNING" ? "info" : s === "CANCELLED" ? "mute" : "warn";

const outboxStatusTone = (s: string): Tone =>
  s === "PUBLISHED" ? "ok" : s === "DEAD_LETTER" ? "bad" : "warn";

const batchStatusTone = (s: string): Tone =>
  s === "COMPLETE" ? "ok" : s === "FAILED" ? "bad" : s === "PROCESSING" ? "info" : "warn";

const sumCounts = (statuses: Record<string, number>): number =>
  Object.values(statuses).reduce((a, b) => a + b, 0);

function StatusBreakdown({ statuses }: { statuses: Record<string, number> }) {
  const entries = Object.entries(statuses).filter(([, n]) => n > 0);
  if (entries.length === 0) return <span className="ops__bd_empty">none</span>;
  return (
    <div className="ops__bd">
      {entries.map(([status, n]) => (
        <span key={status} className="ops__bd_item">
          <b className="tnum">{nf.format(n)}</b> {status.toLowerCase()}
        </span>
      ))}
    </div>
  );
}

function AnalysisRunRow({ run, datasourceLabel }: { run: AnalysisRunRead; datasourceLabel: string }) {
  return (
    <article className="oprow" aria-label={`${run.mode} run on ${datasourceLabel}`}>
      <div className="oprow__lead">
        <div className="oprow__badges">
          <Pill tone={runStatusTone(run.status)}>{run.status.toLowerCase()}</Pill>
          <Pill tone="mute">{run.mode.toLowerCase()}</Pill>
          <Pill tone="mute">{run.trigger_type.toLowerCase()}</Pill>
        </div>
        <div className="oprow__title">{datasourceLabel}</div>
        <div className="oprow__meta">
          <span>{nf.format(run.discovered_tables)} tables discovered</span>
          <span>·</span>
          <span>{nf.format(run.created_objects)} created</span>
          <span>·</span>
          <span>{nf.format(run.changed_objects)} changed</span>
          {run.deprecated_objects > 0 ? (
            <>
              <span>·</span>
              <span>{nf.format(run.deprecated_objects)} deprecated</span>
            </>
          ) : null}
        </div>
        {run.error_message ? (
          <div className="oprow__err" role="alert">
            {run.error_class ? <b>{run.error_class}: </b> : null}
            {run.error_message}
          </div>
        ) : null}
      </div>
      <div className="oprow__time">
        <time dateTime={run.updated_at}>{run.updated_at.slice(0, 16).replace("T", " ")}</time>
        {run.temporal_workflow_id ? (
          <span className="oprow__wf" title="Temporal workflow id">
            {run.temporal_workflow_id}
          </span>
        ) : null}
      </div>
    </article>
  );
}

function OutboxRow({
  event,
  onRequeue,
  requeuing,
}: {
  event: OutboxEventRead;
  onRequeue: () => void;
  requeuing: boolean;
}) {
  return (
    <article className="opbx" aria-label={event.event_type}>
      <div className="opbx__lead">
        <div className="opbx__badges">
          <Pill tone={outboxStatusTone(event.status)}>{event.status.toLowerCase().replace("_", " ")}</Pill>
          <Pill tone="mute">{event.aggregate_type}</Pill>
        </div>
        <div className="opbx__title">{event.event_type}</div>
        <div className="opbx__meta">
          <span>{event.aggregate_id}</span>
          <span>·</span>
          <span>{nf.format(event.attempt_count)} attempt{event.attempt_count === 1 ? "" : "s"}</span>
          <span>·</span>
          <time dateTime={event.occurred_at}>{event.occurred_at.slice(0, 16).replace("T", " ")}</time>
        </div>
        {event.last_error ? (
          <div className="opbx__err" role="alert">
            {event.last_error}
          </div>
        ) : null}
      </div>
      {event.status === "DEAD_LETTER" ? (
        <Button variant="primary" disabled={requeuing} onClick={onRequeue}>
          {requeuing ? "Requeuing…" : "Requeue"}
        </Button>
      ) : null}
    </article>
  );
}

export function OperationsScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const runStatus = params.get("run_status") ?? "ALL";
  const dsFilter = params.get("ds") ?? "";
  const outboxStatus = params.get("outbox_status") ?? "ALL";
  const batchDsId = params.get("batch_ds") ?? "";

  const { datasources } = useDatasourcePicker(ORG);

  /* --- 1. dashboard tiles (fleet-summary), abortable + sequence-guarded, ---
     the same pattern CatalogScreen's loadFirstPage uses. */
  const [summary, setSummary] = useState<FleetSummaryRead | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const summaryInflight = useRef<AbortController | null>(null);
  const summarySeq = useRef(0);

  const loadSummary = useCallback(async () => {
    summaryInflight.current?.abort();
    const ac = new AbortController();
    summaryInflight.current = ac;
    const seq = ++summarySeq.current;

    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const s = await fetchFleetSummary(ORG, ac.signal);
      if (seq !== summarySeq.current) return;
      setSummary(s);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== summarySeq.current) return;
      setSummaryError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === summarySeq.current) setSummaryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSummary();
    return () => summaryInflight.current?.abort();
  }, [loadSummary]);

  /* --- 2. primary list: analysis-runs, filterable by run_status/datasource_id --- */
  const [runs, setRuns] = useState<AnalysisRunRead[]>([]);
  const [runsTotal, setRunsTotal] = useState<number | null>(null);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);
  const runsInflight = useRef<AbortController | null>(null);
  const runsSeq = useRef(0);

  const loadRuns = useCallback(async () => {
    runsInflight.current?.abort();
    const ac = new AbortController();
    runsInflight.current = ac;
    const seq = ++runsSeq.current;

    setRunsLoading(true);
    setRunsError(null);
    try {
      const page = await fetchAnalysisRuns(
        {
          organizationId: ORG,
          runStatus: runStatus !== "ALL" ? runStatus : null,
          datasourceId: dsFilter || null,
          limit: 200,
        },
        ac.signal,
      );
      if (seq !== runsSeq.current) return;
      setRuns(page.items);
      setRunsTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== runsSeq.current) return;
      setRunsError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === runsSeq.current) setRunsLoading(false);
    }
  }, [runStatus, dsFilter]);

  useEffect(() => {
    void loadRuns();
    return () => runsInflight.current?.abort();
  }, [loadRuns]);

  /* --- 3. outbox / dead-letter panel, with per-event requeue --- */
  const [outbox, setOutbox] = useState<OutboxEventRead[]>([]);
  const [outboxLoading, setOutboxLoading] = useState(true);
  const [outboxError, setOutboxError] = useState<string | null>(null);
  const [requeuingId, setRequeuingId] = useState<string | null>(null);
  const outboxInflight = useRef<AbortController | null>(null);
  const outboxSeq = useRef(0);

  const loadOutbox = useCallback(async () => {
    outboxInflight.current?.abort();
    const ac = new AbortController();
    outboxInflight.current = ac;
    const seq = ++outboxSeq.current;

    setOutboxLoading(true);
    setOutboxError(null);
    try {
      const page = await fetchOutboxEvents(
        { organizationId: ORG, status: outboxStatus !== "ALL" ? outboxStatus : null, limit: 100 },
        ac.signal,
      );
      if (seq !== outboxSeq.current) return;
      setOutbox(page.items);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== outboxSeq.current) return;
      setOutboxError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === outboxSeq.current) setOutboxLoading(false);
    }
  }, [outboxStatus]);

  useEffect(() => {
    void loadOutbox();
    return () => outboxInflight.current?.abort();
  }, [loadOutbox]);

  const requeue = useCallback(
    async (eventId: string) => {
      setRequeuingId(eventId);
      setOutboxError(null);
      try {
        await requeueOutboxEvent(eventId);
        await loadOutbox();
        void loadSummary(); // dead-letter/pending tile counts just changed too
      } catch (e) {
        setOutboxError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setRequeuingId(null);
      }
    },
    [loadOutbox, loadSummary],
  );

  /* --- 4. secondary drill-down: one datasource's ingestion batches --- */
  const [batches, setBatches] = useState<MetadataIngestionBatchRead[] | null>(null);
  const [batchesLoading, setBatchesLoading] = useState(false);
  const [batchesError, setBatchesError] = useState<string | null>(null);

  useEffect(() => {
    if (!batchDsId) {
      setBatches(null);
      setBatchesError(null);
      return;
    }
    const ac = new AbortController();
    setBatchesLoading(true);
    setBatchesError(null);
    fetchIngestionBatches(batchDsId, { limit: 100 }, ac.signal)
      .then((page) => setBatches(page.items))
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setBatchesError(e instanceof ApiError ? e.detail : (e as Error).message);
      })
      .finally(() => setBatchesLoading(false));
    return () => ac.abort();
  }, [batchDsId]);

  const datasourceLabel = useCallback(
    (id: string) => datasourceName(datasources, id) ?? id,
    [datasources],
  );

  const deadLetterCount = useMemo(() => outbox.filter((e) => e.status === "DEAD_LETTER").length, [outbox]);

  return (
    <div className="ops">
      <header className="ops__head">
        <div>
          <h1 className="ops__h1">Operations</h1>
          <p className="ops__lede">
            Fleet-wide ingestion health — analysis runs, the event delivery backlog, and
            the scan policies keeping them scheduled.
          </p>
        </div>
      </header>

      {summaryError ? (
        <ErrorState title="Fleet summary could not be loaded" detail={summaryError} onRetry={() => void loadSummary()} />
      ) : summaryLoading ? (
        <div className="ops__skeleton" role="status" aria-live="polite">
          Loading fleet summary…
        </div>
      ) : summary ? (
        <div className="ops__tiles">
          <div className="tile">
            <div className="tile__n tnum">{nf.format(sumCounts(summary.datasource_statuses))}</div>
            <div className="tile__l">datasources</div>
            <StatusBreakdown statuses={summary.datasource_statuses} />
          </div>
          <div className="tile">
            <div className="tile__n tnum">{nf.format(sumCounts(summary.analysis_run_statuses))}</div>
            <div className="tile__l">analysis runs</div>
            <StatusBreakdown statuses={summary.analysis_run_statuses} />
          </div>
          <div className="tile tile--info">
            <div className="tile__n tnum">{nf.format(summary.scan_policies_enabled)}</div>
            <div className="tile__l">scan policies enabled</div>
            <div className="ops__sub">{nf.format(summary.scan_policies_due)} due now</div>
          </div>
          <div className={`tile${summary.dead_letter_outbox_events > 0 ? " tile--bad" : " tile--ok"}`}>
            <div className="tile__n tnum">{nf.format(summary.pending_outbox_events)}</div>
            <div className="tile__l">pending outbox events</div>
            <div className="ops__sub">{nf.format(summary.dead_letter_outbox_events)} dead-lettered</div>
          </div>
          <div className="ops__generated">as of {summary.generated_at.slice(0, 16).replace("T", " ")}</div>
        </div>
      ) : null}

      <section className="ops__sec">
        <div className="ops__sechead">
          <h2 className="ops__h2">Analysis runs</h2>
          <div className="ops__filters">
            <Field label="Status">
              <select
                value={runStatus}
                onChange={(e) => setParams({ run_status: e.target.value === "ALL" ? null : e.target.value })}
              >
                <option value="ALL">All</option>
                {RUN_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s.toLowerCase()}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Datasource">
              <select value={dsFilter} onChange={(e) => setParams({ ds: e.target.value || null })}>
                <option value="">All</option>
                {datasources.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </div>

        {runsError ? (
          <ErrorState title="Analysis runs could not be loaded" detail={runsError} onRetry={() => void loadRuns()} />
        ) : runsLoading ? (
          <div className="ops__skeleton" role="status" aria-live="polite">
            Loading analysis runs…
          </div>
        ) : (
          <div className="ops__runlist">
            <VirtualList
              items={runs}
              getKey={(r) => r.id}
              ariaLabel="Analysis runs"
              estimateSize={104}
              totalCount={runsTotal}
              emptyState={
                <Empty title="No analysis runs match these filters" hint="Try clearing the status or datasource filter." />
              }
              renderItem={(r) => <AnalysisRunRow run={r} datasourceLabel={datasourceLabel(r.datasource_id)} />}
            />
          </div>
        )}
      </section>

      <section className="ops__sec ops__sec--small">
        <div className="ops__sechead">
          <h2 className="ops__h2">
            Event backlog
            {deadLetterCount > 0 ? <Pill tone="bad">{deadLetterCount} dead-lettered</Pill> : null}
          </h2>
          <div className="ops__filters">
            <Field label="Status">
              <select
                value={outboxStatus}
                onChange={(e) => setParams({ outbox_status: e.target.value === "ALL" ? null : e.target.value })}
              >
                <option value="ALL">All</option>
                {OUTBOX_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s.toLowerCase().replace("_", " ")}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </div>

        {outboxError ? (
          <ErrorState title="Outbox events could not be loaded" detail={outboxError} onRetry={() => void loadOutbox()} />
        ) : outboxLoading ? (
          <div className="ops__skeleton" role="status" aria-live="polite">
            Loading outbox events…
          </div>
        ) : outbox.length === 0 ? (
          <Empty title="No outbox events match these filters" />
        ) : (
          <div className="ops__bxlist">
            {outbox.map((e) => (
              <OutboxRow
                key={e.id}
                event={e}
                requeuing={requeuingId === e.id}
                onRequeue={() => void requeue(e.id)}
              />
            ))}
          </div>
        )}
      </section>

      <section className="ops__sec ops__sec--small">
        <div className="ops__sechead">
          <h2 className="ops__h2">Ingestion batches</h2>
          <div className="ops__filters">
            <Field label="Datasource">
              <select value={batchDsId} onChange={(e) => setParams({ batch_ds: e.target.value || null })}>
                <option value="">Pick a datasource…</option>
                {datasources.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </div>
        <p className="ops__gap">
          Secondary, per-datasource view — there is no single endpoint that aggregates
          ingestion-batch/Temporal-workflow status across every datasource in the org, so
          this drills into one at a time rather than faking a fleet-wide table.
        </p>

        {!batchDsId ? (
          <Empty title="Pick a datasource above" hint="Ingestion batches are scoped per datasource." />
        ) : batchesError ? (
          <ErrorState title="Ingestion batches could not be loaded" detail={batchesError} onRetry={() => setParams({ batch_ds: batchDsId })} />
        ) : batchesLoading || batches === null ? (
          <div className="ops__skeleton" role="status" aria-live="polite">
            Loading ingestion batches…
          </div>
        ) : batches.length === 0 ? (
          <Empty title="No ingestion batches recorded for this datasource" />
        ) : (
          <div className="ops__bxlist">
            {batches.map((b) => (
              <article key={b.id} className="opbatch" aria-label={b.batch_key}>
                <div className="opbatch__badges">
                  <Pill tone={batchStatusTone(b.status)}>{b.status.toLowerCase()}</Pill>
                  <Pill tone="mute">{b.snapshot_type.toLowerCase()}</Pill>
                </div>
                <div className="opbatch__title">{b.batch_key}</div>
                <div className="opbatch__meta">
                  <span>
                    {nf.format(b.received_chunks)}/{nf.format(b.expected_chunks)} chunks received
                  </span>
                  <span>·</span>
                  <span>{nf.format(b.processed_chunks)} processed</span>
                  <span>·</span>
                  <span>{b.producer}</span>
                  {b.temporal_workflow_id ? (
                    <>
                      <span>·</span>
                      <span className="oprow__wf">{b.temporal_workflow_id}</span>
                    </>
                  ) : null}
                </div>
                {b.error_message ? (
                  <div className="opbx__err" role="alert">
                    {b.error_class ? <b>{b.error_class}: </b> : null}
                    {b.error_message}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
