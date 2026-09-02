import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentAnalysisResponse,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
} from "../lib/types";
import type { AgentAskError, AgentAskErrorKind } from "../lib/api";
import {
  ApiError,
  classifyAgentAskError,
  fetchAgentRun,
  fetchAgentRunGroundingReceipts,
  fetchAgentRuns,
  runAgentAnalysis,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { useDatasourcePicker, datasourceName } from "../lib/useDatasourcePicker";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./AskScreen.css";

/* ---------------------------------------------------------------------------
   Ask -- UX-15/UX-16, tracker rows UX-15/UX-16.

   The real, single-shot `POST /v1/datasources/{id}/agent-analyses`
   (`run_agent_analysis`, `api.py:2912`): one JSON response carrying the
   explanation, the query that actually ran, and the evidence behind it --
   not a streaming/SSE endpoint, so there is nothing to progressively render
   beyond the ordinary in-flight/loading state every other screen already
   uses.

   AT-9: an ambiguous governed term/metric is not a bug, it is the correct
   answer -- the endpoint refuses with HTTP 409 and both competing
   definitions inlined in `detail`
   (`format_ambiguous_definition_refusal`, semantic_inference.py). This
   screen renders that as its own real refusal state (`AskRefusal` below),
   distinct from every other mapped failure (422 policy rejection, 503 model
   route unavailable, 409 disabled datasource, 502 unhandled) -- see
   `classifyAgentAskError` (../lib/api.ts) for how `detail` gets told apart
   status-for-status.

   Same Catalog pattern (UX-11) as every other migrated screen:
     1. URL state       ds (datasource), run (the open answer/history item)
     2. abortable fetch  one in-flight ask at a time; history paged
                         independently
     3. virtualization   `VirtualList` for the history list
     4. evidence pane    `AnswerPanel` -- the same `.evp` shape
                         `EvidencePane`/`LineageRefusalScreen`'s
                         `RunEvidence` use, permalinkable via `run`, closeable
                         -- but doubling as BOTH the just-answered result (its
                         explanation/execution come from the immediate POST
                         response still held in memory) and a reopened
                         history item (its explanation is not persisted
                         server-side, so the panel reads `GET /agent-runs/{id}`
                         + `GET /agent-runs/{id}/grounding-receipts` instead
                         and says so honestly rather than inventing one).
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";
const MIN_QUESTION_LEN = 3;
const MAX_QUESTION_LEN = 10000;

const statusTone = (status: string): Tone => {
  const s = status.toUpperCase();
  if (s === "SUCCEEDED" || s === "COMPLETED") return "ok";
  if (s === "FAILED" || s === "REJECTED") return "bad";
  if (s === "RUNNING" || s === "PENDING" || s === "QUEUED") return "info";
  return "mute";
};

const ERROR_TITLE: Record<Exclude<AgentAskErrorKind, "AMBIGUOUS_DEFINITION">, string> = {
  DATASOURCE_DISABLED: "This datasource is disabled",
  POLICY_REJECTED: "The generated query was rejected by policy",
  MODEL_UNAVAILABLE: "No model route is available right now",
  CLARIFICATION_NEEDED: "This question needs more information",
  SERVER_ERROR: "The analysis failed on the server",
  UNKNOWN: "The question could not be answered",
};

/** AT-9's refusal, rendered as a real, informative state -- both competing
 *  definitions (and their owners) when the detail carries them, never a
 *  generic error banner. Every other mapped failure still goes through
 *  `ErrorState`, titled by what actually happened. */
function AskRefusal({ error, onRetry }: { error: AgentAskError; onRetry: () => void }) {
  if (error.kind === "AMBIGUOUS_DEFINITION") {
    return (
      <div className="askrefusal" role="alert" aria-label="Ambiguous term refusal">
        <div className="askrefusal__t">
          This question is ambiguous — more than one governed definition applies
        </div>
        {error.alternatives.length > 0 ? (
          <>
            <p className="askrefusal__lede">Specify which business area you mean:</p>
            <ul className="askrefusal__alts">
              {error.alternatives.map((alt) => (
                <li key={alt.businessNodeId} className="askrefusal__alt">
                  <div className="askrefusal__altname">{alt.displayName}</div>
                  <div className="askrefusal__altowner">owner: {alt.owner}</div>
                  <div className="askrefusal__altdef">{alt.definition}</div>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="askrefusal__lede">{error.detail}</p>
        )}
        <Button onClick={onRetry}>Rephrase and ask again</Button>
      </div>
    );
  }
  return <ErrorState title={ERROR_TITLE[error.kind]} detail={error.detail} onRetry={onRetry} />;
}

function HistoryRow({
  run,
  focused,
  onFocus,
}: {
  run: AgentRunRead;
  focused: boolean;
  onFocus: () => void;
}) {
  return (
    <article className={`histrow${focused ? " histrow--sel" : ""}`} aria-label={`Run ${run.id}`}>
      <button className="histrow__click" onClick={onFocus}>
        <div className="histrow__badges">
          <Pill tone={statusTone(run.status)}>{run.status.toLowerCase()}</Pill>
          <Pill tone="mute">{run.generation_source.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
        <div className="histrow__meta">
          <span>{run.id}</span>
          <span aria-hidden="true">·</span>
          <time dateTime={run.created_at}>{run.created_at.slice(0, 19).replace("T", " ")}</time>
        </div>
        {run.failure_reason ? (
          <p className="histrow__fail">{run.failure_reason.replace(/_/g, " ")}</p>
        ) : null}
      </button>
    </article>
  );
}

function record(v: unknown, key: string): unknown {
  return v && typeof v === "object" ? (v as Record<string, unknown>)[key] : undefined;
}

/** The open answer/evidence panel -- either the response this session just
 *  received from `runAgentAnalysis` (`isFresh`, has `explanation`), or a
 *  history item / permalink reopened from `GET /agent-runs/{id}` +
 *  `GET /agent-runs/{id}/grounding-receipts` (no `explanation`: it is not
 *  persisted on `AgentRunRead`, so this says so rather than fabricating
 *  one). Same open/close/permalink shape as `EvidencePane`/
 *  `LineageRefusalScreen`'s `RunEvidence`, over a different data source. */
function AnswerPanel({
  runId,
  askResult,
  detail,
  receipts,
  loading,
  error,
  onClose,
}: {
  runId: string;
  askResult: AgentAnalysisResponse | null;
  detail: AgentRunRead | null;
  receipts: AgentRunGroundingReceiptsRead | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  const isFresh = askResult?.agent_run_id === runId;
  const status = isFresh ? askResult.status : (detail?.status ?? null);
  const generationSource = isFresh ? askResult.generation_source : (detail?.generation_source ?? null);
  const explanation = isFresh ? askResult.explanation : null;
  const execution = isFresh ? askResult.execution : null;
  const stepTrace = isFresh ? askResult.step_trace : (detail?.step_trace ?? []);
  const retrievalEvidence = isFresh
    ? askResult.retrieval_evidence
    : (detail?.retrieval_evidence ?? []);
  const planEvidence = isFresh ? askResult.plan_evidence : (detail?.plan_evidence ?? {});
  const failureReason = isFresh ? null : (detail?.failure_reason ?? null);

  const permalink = `${location.origin}${location.pathname}?run=${runId}`;

  return (
    <aside className="evp" aria-label={`Answer for run ${runId}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name">{runId}</div>
          <div className="evp__path">
            {status ? status.toLowerCase() : "…"}
            {generationSource ? ` · ${generationSource.toLowerCase().replace(/_/g, " ")}` : ""}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>
      <div className="evp__body">
        {loading && !isFresh ? (
          <div className="evp__load" role="status">
            Loading run…
          </div>
        ) : error ? (
          <div className="evp__error" role="alert">
            {error}
          </div>
        ) : (
          <>
            {explanation ? (
              <p className="ask__explain">{explanation}</p>
            ) : (
              <p className="ask__noexplain">
                This run's explanation text is not stored on the run record — showing the
                evidence it recorded instead.
              </p>
            )}
            {failureReason ? <Pill tone="bad">{failureReason.replace(/_/g, " ")}</Pill> : null}

            {execution ? (
              <dl className="ask__exec">
                <div>
                  <dt>Rows</dt>
                  <dd>{execution.row_count}</dd>
                </div>
                <div>
                  <dt>Elapsed</dt>
                  <dd>{execution.elapsed_ms} ms</dd>
                </div>
                <div>
                  <dt>Tables</dt>
                  <dd>{execution.referenced_tables.join(", ") || "—"}</dd>
                </div>
                {execution.masked_columns.length > 0 ? (
                  <div>
                    <dt>Masked columns</dt>
                    <dd>{execution.masked_columns.join(", ")}</dd>
                  </div>
                ) : null}
              </dl>
            ) : null}

            <details className="ask__trace">
              <summary>How this was answered</summary>
              <div className="ask__traceinner">
                <div className="evp__sub">Step trace</div>
                {stepTrace.length === 0 ? (
                  <p className="evp__load">No step trace recorded.</p>
                ) : (
                  <ol className="evl">
                    {stepTrace.map((step, i) => (
                      <li key={i} className="evi evi--info">
                        <div className="evi__label">{String(record(step, "stage") ?? `step ${i + 1}`)}</div>
                        <div className="evi__value">{JSON.stringify(step)}</div>
                      </li>
                    ))}
                  </ol>
                )}

                <div className="evp__sub" style={{ marginTop: 10 }}>
                  Retrieval evidence
                </div>
                {retrievalEvidence.length === 0 ? (
                  <p className="evp__load">No retrieval evidence recorded.</p>
                ) : (
                  <ol className="evl">
                    {retrievalEvidence.map((ev, i) => (
                      <li key={i} className="evi evi--info">
                        <div className="evi__label">{String(record(ev, "object_type") ?? "evidence")}</div>
                        <div className="evi__value">{String(record(ev, "object_id") ?? "")}</div>
                      </li>
                    ))}
                  </ol>
                )}

                <div className="evp__sub" style={{ marginTop: 10 }}>
                  Plan evidence
                </div>
                <pre className="ask__json">{JSON.stringify(planEvidence, null, 2)}</pre>
              </div>
            </details>

            <div className="evp__terms">
              <div className="evp__sub">Grounding evidence (AT-6)</div>
              {receipts && receipts.fragments.length > 0 ? (
                <ol className="evl">
                  {receipts.fragments.map((f, i) => (
                    <li key={i} className={`evi ${f.digest_verified ? "evi--ok" : "evi--warn"}`}>
                      <div className="evi__label">
                        {f.object_type}
                        {f.digest_verified ? "" : " · digest mismatch"}
                      </div>
                      <div className="evi__value">{f.business_name ?? f.object_id}</div>
                      {f.business_description ? (
                        <div className="evi__source">{f.business_description}</div>
                      ) : null}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="evp__load">No grounding fragments recorded for this run.</p>
              )}
            </div>
          </>
        )}
      </div>
      <footer className="evp__foot">
        <button
          className="btn btn--quiet"
          onClick={() => void navigator.clipboard?.writeText(permalink)}
        >
          Copy permalink
        </button>
      </footer>
    </aside>
  );
}

export function AskScreen() {
  const [params, setParams] = useUrlState();
  const dsId = params.get("ds");
  const runId = params.get("run");

  const { datasources } = useDatasourcePicker(ORG);
  const selectedDatasourceName = datasourceName(datasources, dsId);

  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [askResult, setAskResult] = useState<AgentAnalysisResponse | null>(null);
  const [askError, setAskError] = useState<AgentAskError | null>(null);

  const askInflight = useRef<AbortController | null>(null);
  const askSeq = useRef(0);

  const submitQuestion = useCallback(async () => {
    const trimmed = question.trim();
    if (!dsId || trimmed.length < MIN_QUESTION_LEN || trimmed.length > MAX_QUESTION_LEN) return;

    askInflight.current?.abort();
    const ac = new AbortController();
    askInflight.current = ac;
    const seq = ++askSeq.current;

    setAsking(true);
    setAskError(null);
    try {
      const response = await runAgentAnalysis(dsId, { question: trimmed }, ac.signal);
      if (seq !== askSeq.current) return;
      setAskResult(response);
      setParams({ run: response.agent_run_id });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== askSeq.current) return;
      if (e instanceof ApiError) {
        setAskError(classifyAgentAskError(e));
      } else {
        setAskError({ kind: "UNKNOWN", status: 0, detail: (e as Error).message, alternatives: [] });
      }
    } finally {
      if (seq === askSeq.current) setAsking(false);
    }
  }, [dsId, question, setParams]);

  // Switching datasources leaves any open answer behind -- it belonged to
  // the previous datasource's runs, and `run` is cleared by the picker's own
  // onChange below.
  useEffect(() => {
    setAskResult(null);
    setAskError(null);
  }, [dsId]);

  // History: independent from the ask flow above, its own in-flight request.
  const [historyItems, setHistoryItems] = useState<AgentRunRead[]>([]);
  const [historyTotal, setHistoryTotal] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const historyInflight = useRef<AbortController | null>(null);
  const historySeq = useRef(0);

  const loadHistory = useCallback(async () => {
    if (!dsId) {
      setHistoryItems([]);
      setHistoryTotal(null);
      return;
    }
    historyInflight.current?.abort();
    const ac = new AbortController();
    historyInflight.current = ac;
    const seq = ++historySeq.current;

    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const page = await fetchAgentRuns(dsId, { limit: 50, offset: 0 }, ac.signal);
      if (seq !== historySeq.current) return;
      setHistoryItems(page.items);
      setHistoryTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== historySeq.current) return;
      setHistoryError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === historySeq.current) setHistoryLoading(false);
    }
  }, [dsId]);

  useEffect(() => {
    void loadHistory();
    return () => historyInflight.current?.abort();
  }, [loadHistory]);

  const loadMoreHistory = useCallback(async () => {
    if (!dsId || historyLoadingMore || historyLoading) return;
    if (historyItems.length >= (historyTotal ?? 0)) return;
    setHistoryLoadingMore(true);
    try {
      const page = await fetchAgentRuns(dsId, { limit: 50, offset: historyItems.length });
      setHistoryItems((prev) => [...prev, ...page.items]);
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setHistoryLoadingMore(false);
    }
  }, [dsId, historyLoadingMore, historyLoading, historyItems.length, historyTotal]);

  // The open answer/evidence panel. `isFresh` (inside AnswerPanel) decides
  // whether this reads the in-memory POST response or goes to the network --
  // this effect only ever does the latter, and only for a run id that is not
  // the one this session just asked, which is exactly what "clicking a
  // history item loads its detail/grounding receipts without re-asking the
  // question" requires: `runAgentAnalysis` is never called from here.
  const [panelDetail, setPanelDetail] = useState<AgentRunRead | null>(null);
  const [panelReceipts, setPanelReceipts] = useState<AgentRunGroundingReceiptsRead | null>(null);
  const [panelLoading, setPanelLoading] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);

  const freshRunId = askResult?.agent_run_id ?? null;

  useEffect(() => {
    if (!runId) {
      setPanelDetail(null);
      setPanelReceipts(null);
      setPanelError(null);
      setPanelLoading(false);
      return;
    }
    const isFresh = freshRunId === runId;
    const ac = new AbortController();
    setPanelDetail(null);
    setPanelReceipts(null);
    setPanelError(null);
    setPanelLoading(true);
    Promise.all([
      isFresh ? Promise.resolve<AgentRunRead | null>(null) : fetchAgentRun(runId, ac.signal),
      fetchAgentRunGroundingReceipts(runId, ac.signal),
    ])
      .then(([d, r]) => {
        setPanelDetail(d);
        setPanelReceipts(r);
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setPanelError(e instanceof ApiError ? e.detail : (e as Error).message);
      })
      .finally(() => setPanelLoading(false));
    return () => ac.abort();
  }, [runId, freshRunId]);

  const trimmedLen = question.trim().length;
  const historyEmptyState = useMemo(
    () => <Empty title="No questions asked yet" hint="Ask one above to get started." />,
    [],
  );

  return (
    <div className="askscreen">
      <header className="askscreen__head">
        <div>
          <h1 className="askscreen__h1">Ask</h1>
          <p className="askscreen__lede">
            Ask a governed question in plain language. Every answer is grounded in this
            datasource's approved definitions and evidence — or refused with exactly why.
          </p>
        </div>
        {selectedDatasourceName ? (
          <div className="askscreen__stats">
            <span>asking against <b className="tnum">{selectedDatasourceName}</b></span>
          </div>
        ) : null}
      </header>

      <form
        className="askscreen__form"
        onSubmit={(e) => {
          e.preventDefault();
          void submitQuestion();
        }}
      >
        <Field label="Datasource">
          <select
            value={dsId ?? ""}
            onChange={(e) => setParams({ ds: e.target.value || null, run: null })}
          >
            <option value="">Select a datasource…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Question">
          <textarea
            className="askscreen__textarea"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. what was net revenue by month for the last quarter?"
            disabled={!dsId}
            rows={3}
          />
        </Field>
        <div className="askscreen__submitrow">
          <span className="askscreen__hint">
            {trimmedLen === 0
              ? `At least ${MIN_QUESTION_LEN} characters.`
              : trimmedLen < MIN_QUESTION_LEN
                ? `At least ${MIN_QUESTION_LEN} characters.`
                : `${question.length}/${MAX_QUESTION_LEN}`}
          </span>
          <Button
            type="submit"
            variant="primary"
            disabled={!dsId || asking || trimmedLen < MIN_QUESTION_LEN || question.length > MAX_QUESTION_LEN}
          >
            {asking ? "Asking…" : "Ask"}
          </Button>
        </div>
      </form>

      {askError ? <AskRefusal error={askError} onRetry={() => void submitQuestion()} /> : null}

      <div className="askscreen__main">
        <div className="askscreen__history">
          <div className="askscreen__historyhead">
            <h2 className="askscreen__h2">History</h2>
            <span className="askscreen__historycount">
              {historyTotal !== null ? historyTotal : "—"}
            </span>
          </div>
          {!dsId ? (
            <Empty title="Pick a datasource to see its history" />
          ) : historyError ? (
            <ErrorState
              title="History could not be loaded"
              detail={historyError}
              onRetry={() => void loadHistory()}
            />
          ) : historyLoading ? (
            <div className="askscreen__skeleton" role="status" aria-live="polite">
              Loading history…
            </div>
          ) : (
            <VirtualList
              items={historyItems}
              getKey={(r) => r.id}
              ariaLabel="Past questions"
              estimateSize={86}
              totalCount={historyTotal}
              onReachEnd={() => void loadMoreHistory()}
              loadingMore={historyLoadingMore}
              emptyState={historyEmptyState}
              renderItem={(r) => (
                <HistoryRow run={r} focused={r.id === runId} onFocus={() => setParams({ run: r.id })} />
              )}
            />
          )}
        </div>

        {runId ? (
          <AnswerPanel
            runId={runId}
            askResult={askResult}
            detail={panelDetail}
            receipts={panelReceipts}
            loading={panelLoading}
            error={panelError}
            onClose={() => setParams({ run: null })}
          />
        ) : null}
      </div>
    </div>
  );
}
