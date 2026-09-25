import { SaveAnalysisTool } from "../components/SaveAnalysisTool";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type {
  AgentAnalysisResponse,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  ContextProductRead,
  ConversationSummary,
  QueryLineageRead,
} from "../lib/types";
import type { AgentAskContextProductKind, AgentAskError, AgentAskErrorKind } from "../lib/api";
import { deleteConversation, fetchConversation, fetchConversations } from "../lib/api/conversations";
import {
  ApiError,
  classifyAgentAskError,
  describeLoadMoreFailure,
  fetchAgentRun,
  fetchAgentRunGroundingReceipts,
  fetchAgentRuns,
  fetchContextProducts,
  fetchQueryExecutionLineage,
  runAgentAnalysis,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { useDatasourcePicker, datasourceName } from "../lib/useDatasourcePicker";
import { QueryResultTable } from "../components/QueryResultTable";
import { SqlWorkspace } from "../components/SqlWorkspace";
import { VirtualList } from "../components/VirtualList";
import { Button, CopyLinkButton, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./AskScreen.css";
import { stalenessText, useChangesSincePublished } from "./ContextProductFreshness";

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
     1. URL state       ds (datasource), product (the context product being
                        asked through), run (the open answer/history item) --
                        all three declared on `analyst` in `lib/routes.ts`, so
                        a screen change and a pasted link keep them
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

import { useOrgId } from "../lib/org";
const MIN_QUESTION_LEN = 3;

/** R11-MP06: what the person reads while a governed stage is running. Keyed by
 *  `RuntimeStage` (agent_runtime.py); an unknown stage reads as its own name. */
const ASK_STAGE_LABELS: Record<string, string> = {
  RECEIVED: "Received",
  AUTHORIZED: "Checking access",
  SCREENED: "Screening the question",
  RESOLVED: "Finding the tables that answer it",
  PLANNED: "Choosing how to answer",
  GENERATED: "Checking the query",
  VALIDATED: "Validating against policy",
  COSTED: "Estimating the query cost",
  EXECUTED: "Reading the data",
  EXPLAINED: "Explaining the answer",
  COMPLETED: "Done",
  REJECTED: "Refused",
  FAILED: "Failed",
};

function askStageLabel(stage: string): string {
  return ASK_STAGE_LABELS[stage] ?? stage;
}
const MAX_QUESTION_LEN = 10000;

const statusTone = (status: string): Tone => {
  const s = status.toUpperCase();
  if (s === "SUCCEEDED" || s === "COMPLETED") return "ok";
  if (s === "FAILED" || s === "REJECTED") return "bad";
  if (s === "RUNNING" || s === "PENDING" || s === "QUEUED") return "info";
  return "mute";
};

const ERROR_TITLE: Record<
  Exclude<
    AgentAskErrorKind,
    "AMBIGUOUS_DEFINITION" | "AMBIGUOUS_KNOWLEDGE" | AgentAskContextProductKind
  >,
  string
> = {
  DATASOURCE_DISABLED: "This datasource is disabled",
  NOT_AUTHORIZED: "You do not have access to answer questions here",
  POLICY_REJECTED: "The generated query was rejected by policy",
  MODEL_UNAVAILABLE: "No model route is available right now",
  MODEL_THROTTLED: "The model provider is throttling us — try again in a moment",
  CLARIFICATION_NEEDED: "This question needs more information",
  SERVER_ERROR: "The analysis failed on the server",
  UNKNOWN: "The question could not be answered",
};

/* ---------------------------------------------------------------------------
   R11-FP12 (F08): one state per context-product refusal, because the three
   have three different remedies.

   All three were one kind with one title ("this context product cannot answer
   that question") and the server's own stable token as the body text, so a
   person who is simply not a consumer of the product read the literal string
   `CONTEXT_PRODUCT_CONSUMER_ROLE_REQUIRED` under a sentence about their
   question -- describing the wrong problem, and naming no way out of it.

   `remedy` is deliberately the honest one. There is NO request-access route
   for a context product (`POST /v1/marketplace/products/{version_id}/
   access-requests` grants a marketplace listing, which is a different object
   and one this screen holds no id for), so the role refusal says where a
   consumer role actually comes from rather than offering a button that would
   go nowhere.
--------------------------------------------------------------------------- */

const CONTEXT_PRODUCT_REFUSAL: Record<
  AgentAskContextProductKind,
  { title: string; lede: string; remedy: string; clearLabel: string; offerRetry: boolean }
> = {
  CONTEXT_PRODUCT_UNAVAILABLE: {
    title: "That context product cannot be asked through",
    lede:
      "It has no published version. A product is answerable only while a version of it is " +
      "PUBLISHED, so this one has been deprecated, retired or superseded since the link that " +
      "named it was made.",
    remedy: "Pick a product the picker is offering, or ask the whole datasource instead.",
    clearLabel: "Choose another product",
    offerRetry: false,
  },
  CONTEXT_PRODUCT_ROLE_REQUIRED: {
    title: "You are not one of this product's consumers",
    lede:
      "The product answers only for the consumer roles its owner listed on the published " +
      "version, and none of your roles is among them. The question itself was never run.",
    remedy:
      "There is no self-service access request for a context product: a consumer role is added " +
      "to the version by its owner, on the Context products screen. Ask the product's owner, or " +
      "pick a product you can already ask through.",
    clearLabel: "Choose another product",
    offerRetry: false,
  },
  CONTEXT_PRODUCT_OUT_OF_SCOPE: {
    title: "This product's tables cannot answer that question",
    lede:
      "Answering it would have meant reading a table the product does not name, so it was " +
      "refused rather than quietly widened to the rest of the datasource.",
    remedy:
      "Ask something the product's own tables cover, or drop the product and ask the datasource " +
      "as a whole.",
    clearLabel: "Ask without this product",
    offerRetry: true,
  },
};

function isContextProductRefusal(kind: AgentAskErrorKind): kind is AgentAskContextProductKind {
  return kind in CONTEXT_PRODUCT_REFUSAL;
}

/** AT-9's refusal, rendered as a real, informative state -- both competing
 *  definitions (and their owners) when the detail carries them, never a
 *  generic error banner. Every other mapped failure still goes through
 *  `ErrorState`, titled by what actually happened. */
/** The clarification a governed tool asks for: it matched the question but
 *  needs inputs nobody supplied. Rendering the inputs here is what makes Ask
 *  usable with model generation switched off -- the deterministic tool path is
 *  the only one open then, and it refuses until these arrive. The names come
 *  from the server's structured refusal, so this form is never guessing.
 *
 *  Values are sent as strings and the server coerces and validates them
 *  against the tool's typed parameter schema; a bad value comes back as its
 *  own refusal rather than being second-guessed here. */
function ClarificationForm({
  error,
  onSubmit,
  busy,
}: {
  error: AgentAskError;
  onSubmit: (values: Record<string, string>) => void;
  busy: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(error.requiredParameters.map((name) => [name, ""])),
  );
  const complete = error.requiredParameters.every((name) => (values[name] ?? "").trim().length > 0);

  return (
    <form
      className="askrefusal__params"
      aria-label="Supply the inputs this tool needs"
      onSubmit={(e) => {
        e.preventDefault();
        if (complete && !busy) onSubmit(values);
      }}
    >
      {error.requiredParameters.map((name) => (
        <Field key={name} label={name}>
          <input
            type="text"
            value={values[name] ?? ""}
            disabled={busy}
            onChange={(e) => setValues((prev) => ({ ...prev, [name]: e.target.value }))}
          />
        </Field>
      ))}
      <Button type="submit" disabled={!complete || busy}>
        {busy ? "Asking…" : "Ask with these values"}
      </Button>
    </form>
  );
}

function AskRefusal({ error, onRetry, onClarify, onChoose, onClearProduct, busy }: {
  error: AgentAskError;
  onRetry: () => void;
  onClarify: (values: Record<string, string>) => void;
  /** Ask again naming one of an ambiguity's candidates (R11-OKF02). */
  onChoose: (candidate: string) => void;
  /** Drop the context product from the URL, which is the one action a
   *  context-product refusal actually has available. */
  onClearProduct: () => void;
  busy: boolean;
}) {
  if (isContextProductRefusal(error.kind)) {
    const refusal = CONTEXT_PRODUCT_REFUSAL[error.kind];
    return (
      <div className="askrefusal" role="alert" aria-label="Context product refusal">
        <div className="askrefusal__t">{refusal.title}</div>
        <p className="askrefusal__lede">{refusal.lede}</p>
        <p className="askrefusal__lede">{refusal.remedy}</p>
        <div className="askscreen__submitrow">
          <Button onClick={onClearProduct}>{refusal.clearLabel}</Button>
          {refusal.offerRetry ? <Button onClick={onRetry}>Rephrase and ask again</Button> : null}
        </div>
      </div>
    );
  }
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
  if (error.kind === "AMBIGUOUS_KNOWLEDGE") {
    // R11-OKF02: the product's knowledge names two subjects equally for this question. Asked
    // again naming the chosen one, its qualified name is what breaks the tie.
    return (
      <div className="askrefusal" role="alert" aria-label="Ambiguous knowledge refusal">
        <div className="askrefusal__t">Which one do you mean?</div>
        <p className="askrefusal__lede">
          This question matches more than one table in the context product's knowledge equally,
          so it was not answered rather than answered from a guess. Pick the one you mean and it
          is asked again naming it.
        </p>
        {error.candidates.length > 0 ? (
          <div className="askscreen__submitrow" aria-label="Candidates">
            {error.candidates.map((candidate) => (
              <Button key={candidate} disabled={busy} onClick={() => onChoose(candidate)}>
                {candidate}
              </Button>
            ))}
          </div>
        ) : (
          <p className="askrefusal__lede">{error.detail}</p>
        )}
        <Button onClick={onRetry}>Rephrase and ask again</Button>
      </div>
    );
  }
  if (error.kind === "CLARIFICATION_NEEDED" && error.requiredParameters.length > 0) {
    return (
      <div className="askrefusal" role="alert" aria-label="Tool needs more input">
        <div className="askrefusal__t">This tool needs a little more to answer</div>
        <p className="askrefusal__lede">{error.detail}</p>
        <ClarificationForm error={error} onSubmit={onClarify} busy={busy} />
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

interface OkfCitedDocument {
  citation: string;
  path: string;
  title: string | null;
  sha256: string;
  hop: number;
  sections: string[];
}

/** R11-OKF02: `plan_evidence.okf_context`, read defensively -- a run from before the field
 *  existed has none, and then nothing is shown. When knowledge was consulted but not used, the
 *  run says why, and so does this. */
function okfKnowledgeUsed(
  planEvidence: unknown,
): { documents: OkfCitedDocument[]; note: string } | null {
  const okf = record(planEvidence, "okf_context");
  if (!okf || typeof okf !== "object") return null;
  const raw = record(okf, "documents");
  const documents: OkfCitedDocument[] = (Array.isArray(raw) ? raw : []).flatMap((item) => {
    const path = record(item, "path");
    const citation = record(item, "citation");
    const sha = record(item, "sha256");
    if (typeof path !== "string" || typeof citation !== "string" || typeof sha !== "string") return [];
    const title = record(item, "title");
    const hop = record(item, "hop");
    const sections = record(item, "sections");
    return [
      {
        path,
        citation,
        sha256: sha,
        title: typeof title === "string" ? title : null,
        hop: typeof hop === "number" ? hop : 0,
        sections: Array.isArray(sections) ? sections.filter((s): s is string => typeof s === "string") : [],
      },
    ];
  });
  const status = record(okf, "status");
  const note =
    record(okf, "reason") === "OKF_CONTEXT_UNAVAILABLE"
      ? "The product's knowledge bundle could not be read for this run, so the SQL was generated without it."
      : status === "NO_MATCH"
        ? "The product's knowledge holds nothing on this question."
        : "Knowledge matched, but none of it fit the budget or passed screening, so none was used.";
  return { documents: record(okf, "used") === true ? documents : [], note };
}

interface ModelReviewNote {
  tone: "warn" | "neutral" | "ok";
  label: string;
  text: string;
}

/** R11-MP27: what the governed decision model said about this answer, read defensively from
 *  `plan_evidence` (`clarification`, and `answer_review`, which carries a disputed tie-break). Advisory
 *  only -- none of it changed what ran -- so it is shown as a note, never as a refusal. */
function modelReviewNotes(planEvidence: unknown): ModelReviewNote[] {
  const notes: ModelReviewNote[] = [];
  const clarification = record(planEvidence, "clarification");
  if (clarification && record(clarification, "suggested") === true) {
    notes.push({
      tone: "warn",
      label: "may be ambiguous",
      text: "The question could be read more than one way. If this answer is not what you meant, ask again naming the measure, the period or the subject.",
    });
  }
  const review = record(planEvidence, "answer_review");
  const verdict = review ? record(review, "verdict") : null;
  const disputed = review ? record(review, "disputed") === true : false;
  if (verdict === "DOUBTFUL" || verdict === "CHECK") {
    notes.push({
      tone: verdict === "DOUBTFUL" ? "warn" : "neutral",
      label: verdict === "DOUBTFUL" ? "review: doubtful" : "review: check",
      text: disputed
        ? "A second model wrote a different query and the reviewer preferred it. Check the query before relying on the figures."
        : "The reviewer was not confident this query answers the question. Check the query before relying on the figures.",
    });
  } else if (verdict === "OK") {
    notes.push({ tone: "ok", label: "review: ok", text: "The reviewer judged that this query answers the question." });
  }
  return notes;
}

/** The open answer/evidence panel -- either the response this session just
 *  received from `runAgentAnalysis` (`isFresh`, has `explanation`), or a
 *  history item / permalink reopened from `GET /agent-runs/{id}` +
 *  `GET /agent-runs/{id}/grounding-receipts` (no `explanation`: it is not
 *  persisted on `AgentRunRead`, so this says so rather than fabricating
 *  one). Same open/close/permalink shape as `EvidencePane`/
 *  `LineageRefusalScreen`'s `RunEvidence`, over a different data source. */
/** R11-UX16: the three things an answer is, each in its own view. */
type AnswerView = "results" | "query" | "evidence";

const ANSWER_VIEWS: readonly { id: AnswerView; label: string }[] = [
  { id: "results", label: "Results" },
  { id: "query", label: "Query" },
  { id: "evidence", label: "Evidence" },
];

/** A tablist with the keyboard model the ARIA pattern expects: one tab stop, arrows move
 *  between tabs (wrapping), Home and End jump to the ends. */
function AnswerTabs({
  runId,
  view,
  onChange,
}: {
  runId: string;
  view: AnswerView;
  onChange: (view: AnswerView) => void;
}) {
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = ANSWER_VIEWS.length - 1;
    const next =
      event.key === "ArrowRight"
        ? index === last
          ? 0
          : index + 1
        : event.key === "ArrowLeft"
          ? index === 0
            ? last
            : index - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (next === null) return;
    event.preventDefault();
    onChange(ANSWER_VIEWS[next]!.id);
    tabs.current[next]?.focus();
  }
  return (
    <div role="tablist" aria-label="Answer views" className="ask__tabs">
      {ANSWER_VIEWS.map((item, index) => (
        <button
          key={item.id}
          ref={(element) => {
            tabs.current[index] = element;
          }}
          type="button"
          role="tab"
          id={`answer-${runId}-${item.id}-tab`}
          aria-controls={`answer-${runId}-${item.id}`}
          aria-selected={view === item.id}
          tabIndex={view === item.id ? 0 : -1}
          className={`ask__tab${view === item.id ? " ask__tab--on" : ""}`}
          onClick={() => onChange(item.id)}
          onKeyDown={(event) => onKeyDown(event, index)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

type PastQueryState =
  | { kind: "loading" }
  | { kind: "loaded"; lineage: QueryLineageRead }
  | { kind: "error"; detail: string };

/** A reopened run's executed query, read from the execution the run recorded. */
function PastQuery({ executionId }: { executionId: string }) {
  const [state, setState] = useState<PastQueryState>({ kind: "loading" });
  useEffect(() => {
    const active = new AbortController();
    setState({ kind: "loading" });
    fetchQueryExecutionLineage(executionId, active.signal)
      .then((lineage) => {
        if (!active.signal.aborted) setState({ kind: "loaded", lineage });
      })
      .catch((error: unknown) => {
        if (active.signal.aborted) return;
        setState({ kind: "error", detail: error instanceof Error ? error.message : String(error) });
      });
    return () => active.abort();
  }, [executionId]);
  if (state.kind === "loading") {
    return (
      <p className="evp__load" role="status">
        Loading the query this run executed…
      </p>
    );
  }
  if (state.kind === "error") {
    return (
      <p className="evp__error" role="alert">
        The query this run executed could not be read ({state.detail}).
      </p>
    );
  }
  const { lineage } = state;
  return (
    <>
      <p className="ask__noexplain">
        The statement as it ran, with its literals replaced. The values it returned are not
        retained.
      </p>
      {lineage.normalized_sql ? (
        <pre className="ask__json" aria-label="Executed query">
          {lineage.normalized_sql}
        </pre>
      ) : (
        <p className="evp__load">The gateway recorded no statement shape for this execution.</p>
      )}
      <QueryFacts
        tables={lineage.referenced_tables}
        columns={lineage.referenced_columns}
        rowCount={lineage.row_count ?? null}
        elapsedMs={lineage.elapsed_ms ?? null}
      />
    </>
  );
}

function QueryFacts({
  tables,
  columns,
  rowCount,
  elapsedMs,
}: {
  tables: string[];
  columns: string[];
  rowCount: number | null;
  elapsedMs: number | null;
}) {
  return (
    <dl className="ask__exec">
      <div>
        <dt>Tables</dt>
        <dd>{tables.join(", ") || "—"}</dd>
      </div>
      <div>
        <dt>Columns</dt>
        <dd>{columns.join(", ") || "—"}</dd>
      </div>
      <div>
        <dt>Rows</dt>
        <dd>{rowCount ?? "—"}</dd>
      </div>
      <div>
        <dt>Elapsed</dt>
        <dd>{elapsedMs !== null ? `${elapsedMs} ms` : "—"}</dd>
      </div>
    </dl>
  );
}

function AnswerPanel({
  runId,
  askResult,
  askedAt,
  contextProduct,
  detail,
  receipts,
  loading,
  error,
  onClose,
}: {
  runId: string;
  askResult: AgentAnalysisResponse | null;
  /** When this session received `askResult`. The response carries no
   *  timestamp, and a run reopened from history has no result to date. */
  askedAt: Date | null;
  /** The context product this screen is currently asking through, or `null`.
   *  Used for the fresh answer's provenance and for the permalink -- the run
   *  record itself carries the version, never the key (see below). */
  contextProduct: { key: string; name: string } | null;
  detail: AgentRunRead | null;
  receipts: AgentRunGroundingReceiptsRead | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  const isFresh = askResult?.agent_run_id === runId;
  const [view, setView] = useState<AnswerView>("results");
  // A past run's query is read the first time its view opens, then kept: switching views
  // must not re-read it.
  const [queryOpened, setQueryOpened] = useState(false);
  const openView = (next: AnswerView) => {
    setView(next);
    if (next === "query") setQueryOpened(true);
  };
  const queryExecutionId = isFresh
    ? (askResult.execution?.execution_id ?? null)
    : (detail?.query_execution_id ?? null);
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
  // R11-FP12: which published context product version the answer was scoped to, and -- since
  // the RESOLVED stage records `context_product_key` beside it -- which product that version
  // belongs to. Both read off the run's own trace rather than inferred from the request, so a
  // reopened run says what it actually stood on.
  const contextProductProvenance = (() => {
    for (const step of stepTrace) {
      const details = (step as { details?: Record<string, unknown> }).details;
      const version = details?.["context_product_version"];
      if (typeof version !== "number") continue;
      const key = details?.["context_product_key"];
      return { version, key: typeof key === "string" && key !== "" ? key : null };
    }
    return null;
  })();
  const contextProductVersion = contextProductProvenance?.version ?? null;
  // R11-FP12 remainder / F08: *which* product, not only which version of it. "version 2" alone
  // does not say what answered, and two products' v2 have nothing to do with each other.
  //
  // The key the run recorded is the authority, and the picker only ever supplies a display
  // name for it -- and only while it is showing that very product. A reopened run must never
  // be attributed to whatever the picker happens to be set to now, which is why the earlier
  // version of this line showed nothing at all for a stored run. A run whose trace carries no
  // key predates the field; for a *fresh* answer this session sent the key on that same
  // request, so its own selection is the request's own record rather than a guess.
  const recordedProductKey = contextProductProvenance?.key ?? null;
  const askedThroughProduct =
    recordedProductKey !== null
      ? {
          key: recordedProductKey,
          name: contextProduct?.key === recordedProductKey ? contextProduct.name : recordedProductKey,
        }
      : isFresh
        ? contextProduct
        : null;
  // Provenance the run pinned its answer to — which published semantic model
  // and policy version grounded it, and (for a stored run) which approved model
  // route generated the SQL. The fresh POST response omits the route, so it is
  // only shown once the run is reopened from history.
  const semanticVersion = isFresh ? askResult.semantic_version : (detail?.semantic_version ?? null);
  const policyVersion = isFresh ? askResult.policy_version : (detail?.policy_version ?? null);
  const modelRoute = isFresh ? null : (detail?.model_route ?? null);

  // DQ-3 (module 11 §9): `agent_orchestrator.py`'s EXPLAINED checkpoint folds
  // every open-incident `quality_coupling.TrustWarning` this answer's own
  // tables carry into `plan_evidence.trust` -- a machine-readable list, not
  // prose, so this reads the same `{asset_id, message, severity,
  // incident_ids}` shape directly rather than parsing it out of the
  // explanation text below.
  const okfKnowledge = okfKnowledgeUsed(planEvidence);
  const reviewNotes = modelReviewNotes(planEvidence);
  const trust = record(planEvidence, "trust");
  const trustWarningsRaw = trust ? record(trust, "warnings") : null;
  const trustWarnings = Array.isArray(trustWarningsRaw) ? trustWarningsRaw : [];
  const trustScore = trust ? record(trust, "trust_score") : null;
  const trustGrade = trust ? record(trust, "trust_grade") : null;



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
      {status === "COMPLETED" ? <SaveAnalysisTool key={runId} runId={runId} /> : null}
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
            <AnswerTabs runId={runId} view={view} onChange={openView} />
            <div
              role="tabpanel"
              id={`answer-${runId}-results`}
              aria-labelledby={`answer-${runId}-results-tab`}
              hidden={view !== "results"}
              className="ask__view"
            >
            {trustWarnings.length > 0 ? (
              <div className="ask__trustwarn" role="alert" aria-label="Quality trust warning">
                <div className="ask__trustwarn_head">
                  <Pill tone="bad">quality trust warning</Pill>
                  {typeof trustScore === "number" ? (
                    <span className="ask__trustscore">
                      trust score {trustScore.toFixed(0)}
                      {trustGrade ? ` (${String(trustGrade)})` : ""}
                    </span>
                  ) : null}
                </div>
                <ul className="ask__trustwarn_list">
                  {trustWarnings.map((w, i) => (
                    <li key={i}>{String(record(w, "message") ?? "")}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {reviewNotes.length > 0 ? (
              <ul className="ask__review" aria-label="Model review">
                {reviewNotes.map((note) => (
                  <li key={note.label} className={`ask__review_item ask__review_item--${note.tone}`}>
                    <Pill tone={note.tone === "warn" ? "warn" : note.tone === "ok" ? "ok" : "mute"}>
                      {note.label}
                    </Pill>
                    <span>{note.text}</span>
                  </li>
                ))}
              </ul>
            ) : null}

            {explanation ? (
              <p className="ask__explain">{explanation}</p>
            ) : (
              <p className="ask__noexplain">
                This run's explanation text is not stored on the run record — showing the
                evidence it recorded instead.
              </p>
            )}
            {failureReason ? <Pill tone="bad">{failureReason.replace(/_/g, " ")}</Pill> : null}

            {/* The rows themselves (F20). Present only while this session
                still holds the response that carried them: `AgentRunRead` has
                no `rows` field, so a reopened run shows its evidence and says
                so rather than implying the values were kept. */}
            {execution && askedAt ? (
              <QueryResultTable
                execution={execution}
                semanticVersion={semanticVersion}
                policyVersion={policyVersion}
                executedAt={askedAt}
              />
            ) : !isFresh && (status === "SUCCEEDED" || status === "COMPLETED") ? (
              <p className="ask__noexplain">
                This run's result values are not retained — only the question's evidence, the query
                that ran and the policy it ran under. Ask again to see current values.
              </p>
            ) : null}

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
            </div>

            <div
              role="tabpanel"
              id={`answer-${runId}-query`}
              aria-labelledby={`answer-${runId}-query-tab`}
              hidden={view !== "query"}
              className="ask__view"
            >
              <div className="evp__sub">Executed query</div>
              {execution ? (
                <>
                  <pre className="ask__json" aria-label="Executed query">
                    {execution.normalized_sql}
                  </pre>
                  <QueryFacts
                    tables={execution.referenced_tables}
                    columns={execution.referenced_columns}
                    rowCount={execution.row_count}
                    elapsedMs={execution.elapsed_ms}
                  />
                </>
              ) : queryExecutionId !== null && !isFresh ? (
                queryOpened ? <PastQuery executionId={queryExecutionId} /> : null
              ) : (
                <p className="evp__load">
                  No query ran for this run{status ? ` (${status.toLowerCase()})` : ""}: it
                  was answered or refused before anything reached the source.
                </p>
              )}
            </div>

            <div
              role="tabpanel"
              id={`answer-${runId}-evidence`}
              aria-labelledby={`answer-${runId}-evidence-tab`}
              hidden={view !== "evidence"}
              className="ask__view"
            >
            <div className="evp__sub">Provenance</div>
            <dl className="ask__exec">
              <div>
                <dt>Semantic model</dt>
                <dd>{semanticVersion ?? "raw technical metadata"}</dd>
              </div>
              <div>
                <dt>Policy version</dt>
                <dd>{policyVersion ?? "—"}</dd>
              </div>
              <div>
                <dt>Context product</dt>
                <dd>
                  {contextProductVersion === null
                    ? "not asked through one"
                    : askedThroughProduct
                      ? `${askedThroughProduct.name} · version ${contextProductVersion}`
                      : `version ${contextProductVersion} · product not recorded on the run`}
                </dd>
              </div>
              <div>
                <dt>Model route</dt>
                <dd>{modelRoute ?? (isFresh ? "shown on the saved run" : "governed tool · no model")}</dd>
              </div>
            </dl>

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
                    {retrievalEvidence.map((ev, i) => {
                      const score = record(ev, "score");
                      const scoreText =
                        typeof score === "number"
                          ? score.toFixed(2)
                          : score != null
                            ? String(score)
                            : null;
                      const reason = record(ev, "reason") ?? record(ev, "reason_codes");
                      // DQ-3 (RT-7): `retrieval.py`'s Stage 4 attaches this to
                      // any candidate whose own or a dependency table has an
                      // open incident -- the concrete reason this candidate
                      // ranked lower than it otherwise would have, not just a
                      // number.
                      const metadata = record(ev, "metadata");
                      const demotion = metadata ? record(metadata, "quality_trust_demotion") : null;
                      const worstFactor = demotion ? record(demotion, "worst_factor") : null;
                      return (
                        <li key={i} className={`evi ${demotion ? "evi--warn" : "evi--info"}`}>
                          <div className="evi__label">
                            {String(record(ev, "object_type") ?? "evidence")}
                            {scoreText ? ` · ${scoreText}` : ""}
                          </div>
                          <div className="evi__value">{String(record(ev, "object_id") ?? "")}</div>
                          {reason != null ? (
                            <div className="evi__source">
                              {Array.isArray(reason) ? reason.join(", ") : String(reason)}
                            </div>
                          ) : null}
                          {demotion ? (
                            <div className="evi__source">
                              demoted in ranking — {String(record(demotion, "reason") ?? "OPEN_QUALITY_INCIDENT")
                                .replace(/_/g, " ")
                                .toLowerCase()}
                              {typeof worstFactor === "number" ? ` (factor ${worstFactor.toFixed(2)})` : ""}
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
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

            {okfKnowledge ? (
              <div className="evp__terms">
                {/* R11-OKF02: the sections of the product's approved knowledge the SQL was
                    generated with, as the run recorded them -- citation, document, sections and
                    the document's digest. Receipts only: the run keeps no section text. */}
                <div className="evp__sub">Product knowledge (OKF)</div>
                {okfKnowledge.documents.length > 0 ? (
                  <ol className="evl">
                    {okfKnowledge.documents.map((doc) => (
                      <li key={doc.path} className="evi evi--info">
                        <div className="evi__label">
                          [{doc.citation}] {doc.hop === 0 ? "matched the question" : "linked from a match"}
                        </div>
                        <div className="evi__value" title={doc.path}>
                          {doc.title ?? doc.path}
                        </div>
                        <div className="evi__source">
                          {doc.sections.join(" · ") || "no sections"} · sha256 {doc.sha256.slice(0, 12)}
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="evp__load">{okfKnowledge.note}</p>
                )}
              </div>
            ) : null}
            </div>
          </>
        )}
      </div>
      <footer className="evp__foot">
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/analyst`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08).

            It also names the context product (R11-FP12, F08). Without it a
            shared permalink re-opened the same run beside a picker set to
            "everything this datasource governs", so the next question asked
            from that link was silently wider than the one being shared. */}
        <CopyLinkButton
          target={{
            screen: "analyst",
            params: { run: runId, product: contextProduct?.key ?? null },
          }}
          label="Copy permalink"
        />
      </footer>
    </aside>
  );
}

/**
 * What this project's askable products are, as four distinguishable answers
 * rather than one list that is empty for four different reasons (F08).
 *
 * `idle` is "no datasource picked, so there is no project to ask"; `error`
 * carries the server's own reason, because "the list could not be read" and
 * "there are none" send a reader to different places.
 */
type ProductOffer =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "loaded"; items: ContextProductRead[] }
  | { state: "error"; detail: string };

const NO_PROJECT: ProductOffer = { state: "idle" };
const NO_PRODUCTS: ContextProductRead[] = [];

export function AskScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const runId = params.get("run");

  const { datasources, error: dsPickerError, preferredDatasourceId } = useDatasourcePicker(ORG);
  const dsId = params.get("ds") ?? preferredDatasourceId;
  const selectedDatasourceName = datasourceName(datasources, dsId);
  // R11-FP12: asking *through* a published context product scopes the answer to the tables it
  // names and the tool versions it declares eligible. The key lives in the URL like the
  // datasource, so a clarification retry, a reload and a shared link all keep asking through the
  // same product rather than silently widening back to the whole datasource.
  const productKey = params.get("product");
  const projectId = datasources.find((d) => d.id === dsId)?.project_id ?? null;
  /* R11-FP12: whether what a product covers moved since it was published. An answer asked
     through it still uses the published version, so the asker is told rather than guessing. */
  const changes = useChangesSincePublished(projectId);
  const [offer, setOffer] = useState<ProductOffer>(NO_PROJECT);

  useEffect(() => {
    if (!projectId) {
      setOffer(NO_PROJECT);
      return;
    }
    const ac = new AbortController();
    setOffer({ state: "loading" });
    void (async () => {
      try {
        // F08: `askable` is the server applying the ask path's own admission rule -- PUBLISHED,
        // and naming a consumer role this caller holds. The screen used to filter the lifecycle
        // listing by status alone, which is only half of it: a steward was offered every
        // published product in the project and the ask then refused the ones whose consumer
        // roles did not include theirs. Filtering that client-side was never possible, because
        // "which roles do I hold" is not in the listing.
        const page = await fetchContextProducts(
          projectId,
          { limit: 200, askable: true },
          ac.signal,
        );
        if (ac.signal.aborted) return;
        // The status filter is kept as well, for demo/fixture mode: it serves the listing from
        // a local estate that has no role bindings to apply `askable` against.
        setOffer({
          state: "loaded",
          items: page.items.filter((p) => p.latest_version?.status === "PUBLISHED"),
        });
      } catch (e) {
        if ((e as Error)?.name === "AbortError" || ac.signal.aborted) return;
        // F08: a list that could not be read is NOT "this project publishes none". Both used to
        // render as "No published product on this project", so a 403 or a dropped connection
        // read as a settled fact about the estate. Asking without a product still works either
        // way -- which is why this does not block the form -- but the two say so differently,
        // because one is fixed by retrying or by being granted access and the other by
        // publishing a product.
        setOffer({
          state: "error",
          detail: e instanceof ApiError ? e.detail : (e as Error).message,
        });
      }
    })();
    return () => ac.abort();
  }, [projectId]);

  const products = offer.state === "loaded" ? offer.items : NO_PRODUCTS;
  /* F08: only a product the picker is actually showing as selected may be sent. The URL can
     name one this project does not offer -- moving to a datasource in another project keeps
     `product`, and so does a link built elsewhere -- and the old code sent the key anyway while
     the `<select>` matched no option and rendered blank: the screen said "no product" and the
     request said otherwise. */
  const selectedProduct = products.find((p) => p.product_key === productKey) ?? null;
  const askedThroughKey = selectedProduct?.product_key ?? null;

  /* …and once the list that decides is actually in, the stale key is removed from the URL, so a
     link copied from here cannot carry a product this screen is not honouring. Deliberately not
     done on the datasource change itself: the loaded list is what knows whether the new
     project offers it, and while the list is loading or unreadable the key is kept (the next
     load may honour it) but not sent. */
  useEffect(() => {
    if (offer.state !== "loaded" || productKey === null) return;
    if (offer.items.some((p) => p.product_key === productKey)) return;
    setParams({ product: null });
  }, [offer, productKey, setParams]);

  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  // R11-MP06: the governed stage the in-flight run has reached, from the stream.
  const [askStage, setAskStage] = useState<string | null>(null);
  const [askResult, setAskResult] = useState<AgentAnalysisResponse | null>(null);
  // Freshness of the ANSWER. Recorded here because the response has no
  // timestamp of its own, and deliberately not persisted anywhere: it is a
  // property of this session's in-memory result, like the rows themselves.
  const [askedAt, setAskedAt] = useState<Date | null>(null);
  const [askError, setAskError] = useState<AgentAskError | null>(null);
  // R11-MP26: the conversation the next question continues. Its questions are this
  // session's own text, shown back to the person who typed them; the server keeps
  // only their redacted form.
  const [conversation, setConversation] = useState<{ id: string; turns: string[] } | null>(null);

  const askInflight = useRef<AbortController | null>(null);
  const askSeq = useRef(0);

  // R11-MP26: the caller's own conversations on this datasource, to pick one up again.
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationsError, setConversationsError] = useState<string | null>(null);
  const [conversationBusy, setConversationBusy] = useState<string | null>(null);
  const loadConversations = useCallback(async () => {
    if (!dsId) {
      setConversations([]);
      return;
    }
    try {
      setConversations(await fetchConversations(dsId));
      setConversationsError(null);
    } catch (e) {
      setConversationsError((e as Error).message || "Conversations could not be listed.");
    }
  }, [dsId]);
  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);
  const continueConversation = useCallback(async (conversationId: string) => {
    setConversationBusy(conversationId);
    try {
      const found = await fetchConversation(conversationId);
      setConversation({ id: found.id, turns: found.turns.map((turn) => turn.question) });
      setConversationsError(null);
    } catch (e) {
      setConversationsError((e as Error).message || "The conversation could not be opened.");
    } finally {
      setConversationBusy(null);
    }
  }, []);
  const removeConversation = useCallback(
    async (conversationId: string) => {
      setConversationBusy(conversationId);
      try {
        await deleteConversation(conversationId);
        setConversation((current) => (current?.id === conversationId ? null : current));
        await loadConversations();
      } catch (e) {
        setConversationsError((e as Error).message || "The conversation could not be deleted.");
      } finally {
        setConversationBusy(null);
      }
    },
    [loadConversations],
  );

  const submitQuestion = useCallback(
    async (
      clarification?: { toolParameters: Record<string, string>; toolVersionId: string | null },
      /** Ask this instead of the input's current text -- a choice made in a refusal state,
       *  applied before React has re-rendered the input with it. */
      override?: string,
    ) => {
    const trimmed = (override ?? question).trim();
    if (!dsId || trimmed.length < MIN_QUESTION_LEN || trimmed.length > MAX_QUESTION_LEN) return;

    askInflight.current?.abort();
    const ac = new AbortController();
    askInflight.current = ac;
    const seq = ++askSeq.current;

    setAsking(true);
    setAskStage(null);
    setAskError(null);
    try {
      // A retry after a clarification pins the tool the server already chose:
      // re-running retrieval could select a different one, and the answer would
      // then come from a tool the person never supplied inputs for.
      const askedThrough = {
        ...(askedThroughKey ? { context_product_key: askedThroughKey } : {}),
        ...(conversation ? { conversation_id: conversation.id } : {}),
      };
      const response = await runAgentAnalysis(
        dsId,
        clarification
          ? {
              question: trimmed,
              tool_parameters: clarification.toolParameters,
              ...askedThrough,
              ...(clarification.toolVersionId
                ? { preferred_tool_version_id: clarification.toolVersionId }
                : {}),
            }
          : { question: trimmed, ...askedThrough },
        ac.signal,
        (stage) => {
          if (seq === askSeq.current) setAskStage(stage);
        },
      );
      if (seq !== askSeq.current) return;
      setAskResult(response);
      setAskedAt(new Date());
      setParams({ run: response.agent_run_id });
      const conversationId = response.conversation_id;
      if (conversationId) {
        setConversation((current) =>
          current && current.id === conversationId
            ? { id: current.id, turns: [...current.turns, trimmed] }
            : { id: conversationId, turns: [trimmed] },
        );
        void loadConversations();
      }
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== askSeq.current) return;
      if (e instanceof ApiError) {
        setAskError(classifyAgentAskError(e));
      } else {
        setAskError({
          kind: "UNKNOWN",
          status: 0,
          detail: (e as Error).message,
          alternatives: [],
          requiredParameters: [],
          toolVersionId: null,
          candidates: [],
        });
      }
    } finally {
      if (seq === askSeq.current) {
        setAsking(false);
        setAskStage(null);
      }
    }
  },
    [dsId, askedThroughKey, conversation, question, setParams, loadConversations],
  );

  // Switching datasources leaves any open answer behind -- it belonged to
  // the previous datasource's runs, and `run` is cleared by the picker's own
  // onChange below.
  useEffect(() => {
    setAskResult(null);
    setAskedAt(null);
    setAskError(null);
    // A conversation belongs to one datasource.
    setConversation(null);
  }, [dsId]);

  // History: independent from the ask flow above, its own in-flight request.
  // R11-SQL01: the review-first path -- draft or paste SQL, validate, then run it on purpose.
  const [showSqlWorkspace, setShowSqlWorkspace] = useState(false);
  const [historyItems, setHistoryItems] = useState<AgentRunRead[]>([]);
  const [showHistory, setShowHistory] = useState(true);
  const [historyTotal, setHistoryTotal] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historyLoadMoreError, setHistoryLoadMoreError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const historyInflight = useRef<AbortController | null>(null);
  const historyPageInflight = useRef<AbortController | null>(null);
  const historyExhausted = useRef(false);
  const historySeq = useRef(0);

  const loadHistory = useCallback(async () => {
    historyInflight.current?.abort();
    historyPageInflight.current?.abort();
    historyPageInflight.current = null;
    historyExhausted.current = false;
    const seq = ++historySeq.current;
    setHistoryItems([]);
    setHistoryTotal(null);
    setHistoryError(null);
    setHistoryLoadMoreError(null);
    setHistoryLoadingMore(false);
    if (!dsId) {
      setHistoryLoading(false);
      return;
    }
    const ac = new AbortController();
    historyInflight.current = ac;

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
    return () => {
      ++historySeq.current;
      historyInflight.current?.abort();
      historyPageInflight.current?.abort();
    };
  }, [loadHistory]);

  const loadMoreHistory = useCallback(async () => {
    if (!dsId || historyLoadingMore || historyLoading || historyPageInflight.current || historyExhausted.current) return;
    if (historyItems.length >= (historyTotal ?? 0)) return;
    const seq = historySeq.current;
    const ac = new AbortController();
    historyPageInflight.current = ac;
    setHistoryLoadingMore(true);
    try {
      const page = await fetchAgentRuns(dsId, { limit: 50, offset: historyItems.length }, ac.signal);
      if (seq !== historySeq.current || ac.signal.aborted) return;
      historyExhausted.current = page.items.length === 0;
      setHistoryItems((prev) => [...prev, ...page.items]);
      setHistoryTotal(page.total);
      setHistoryLoadMoreError(null);
    } catch (e) {
      if (seq !== historySeq.current || ac.signal.aborted) return;
      historyExhausted.current = true;
      // See AuditLedgerScreen: a silent stop is indistinguishable from the end
      // of the list, so a refusal reads as "there is nothing more".
      setHistoryLoadMoreError(describeLoadMoreFailure(e));
    } finally {
      if (historyPageInflight.current === ac) historyPageInflight.current = null;
      if (seq === historySeq.current) setHistoryLoadingMore(false);
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
          {/* `product` is deliberately not cleared here: the new datasource's own
              project decides whether it can still be honoured, and that answer
              arrives with the list, not with the click (see the reconciling
              effect above). Until it does, the key is not sent. */}
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
          {dsPickerError ? (
            <p className="askscreen__pickerr" role="alert">{dsPickerError}</p>
          ) : null}
        </Field>
        <Field label="Context product">
          <select
            aria-label="Context product"
            value={askedThroughKey ?? ""}
            disabled={!dsId || products.length === 0}
            onChange={(e) => setParams({ product: e.target.value || null, run: null })}
          >
            <option value="">
              {offer.state === "loading"
                ? "Loading this project's products…"
                : offer.state === "error"
                  ? "The product list could not be read"
                  : products.length === 0
                    ? "No published product you can ask through"
                    : "Everything this datasource governs"}
            </option>
            {products.map((p) => {
              const moved = p.latest_version
                ? stalenessText(
                    changes.byVersion.get(p.latest_version.id),
                    changes.meaningByVersion.get(p.latest_version.id),
                  )
                : null;
              const name = p.latest_version?.name ?? p.product_key;
              return (
                <option key={p.id} value={p.product_key}>
                  {moved ? `${name} (${moved})` : name}
                </option>
              );
            })}
          </select>
          {offer.state === "error" ? (
            <p className="askscreen__pickerr" role="alert">
              This project's context products could not be listed ({offer.detail}), so none can be
              offered — this is not the same as there being none. Asking without one still works.
            </p>
          ) : null}
          {selectedProduct?.latest_version &&
          stalenessText(
            changes.byVersion.get(selectedProduct.latest_version.id),
            changes.meaningByVersion.get(selectedProduct.latest_version.id),
          ) ? (
            <p className="askscreen__hint">
              Some of what this product stands on has changed since it was published (
              {stalenessText(
                changes.byVersion.get(selectedProduct.latest_version.id),
                changes.meaningByVersion.get(selectedProduct.latest_version.id),
              )}
              ). Answers still use the published version; its steward can see what moved on the
              Context products screen.
            </p>
          ) : null}
          <p className="askscreen__hint">
            A product answers from the tables it names and the tool versions it declares
            eligible; anything else is refused rather than quietly used.
          </p>
        </Field>
        {conversation ? (
          <section className="askscreen__conversation" aria-label="Conversation">
            <div className="askscreen__conversation_head">
              <span>
                Following up: the next question can refer to{" "}
                {conversation.turns.length === 1
                  ? "the earlier one"
                  : `the ${conversation.turns.length} earlier ones`}
                .
              </span>
              <Button type="button" onClick={() => setConversation(null)}>
                New conversation
              </Button>
            </div>
            <ol className="askscreen__conversation_turns">
              {conversation.turns.map((turn, index) => (
                <li key={index}>{turn}</li>
              ))}
            </ol>
          </section>
        ) : null}
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
        {asking && askStage ? (
          <p className="askscreen__stage" role="status" aria-live="polite">
            {askStageLabel(askStage)}
          </p>
        ) : null}
      </form>

      {askError ? (
        <AskRefusal
          error={askError}
          onRetry={() => void submitQuestion()}
          onClarify={(values) =>
            void submitQuestion({ toolParameters: values, toolVersionId: askError.toolVersionId })
          }
          onChoose={(candidate) => {
            const chosen = `${question.trim()} (${candidate})`;
            setQuestion(chosen);
            void submitQuestion(undefined, chosen);
          }}
          onClearProduct={() => {
            setParams({ product: null, run: null });
            setAskError(null);
          }}
          busy={asking}
        />
      ) : null}

      {showSqlWorkspace && dsId ? (
        <SqlWorkspace datasourceId={dsId} productKey={askedThroughKey} question={question} />
      ) : null}

      <div className="askscreen__viewtools">
        <button
          type="button"
          className="btn btn--quiet"
          aria-expanded={showSqlWorkspace}
          disabled={!dsId}
          onClick={() => setShowSqlWorkspace((shown) => !shown)}
        >
          {showSqlWorkspace ? "Hide SQL review" : "Review SQL first"}
        </button>
        <button
          type="button"
          className="btn btn--quiet"
          aria-expanded={showHistory}
          aria-controls="ask-history"
          onClick={() => setShowHistory((shown) => !shown)}
        >
          {showHistory ? "Hide history" : "Show history"}
        </button>
      </div>
      <div className="askscreen__main">
        {runId ? (
          <AnswerPanel
            key={runId}
            runId={runId}
            askResult={askResult}
            askedAt={askedAt}
            contextProduct={
              selectedProduct
                ? {
                    key: selectedProduct.product_key,
                    name: selectedProduct.latest_version?.name ?? selectedProduct.product_key,
                  }
                : null
            }
            detail={panelDetail}
            receipts={panelReceipts}
            loading={panelLoading}
            error={panelError}
            onClose={() => setParams({ run: null })}
          />
        ) : (
          <div className="askscreen__answeridle">
            <Empty title="Your answer appears here" hint="Ask a question or open a past run to inspect its evidence." />
          </div>
        )}

        <div id="ask-history" className="askscreen__history" hidden={!showHistory}>
          {dsId ? (
            <section className="askscreen__convs" aria-label="Your conversations">
              <div className="askscreen__historyhead">
                <h2 className="askscreen__h2">Conversations</h2>
                <span className="askscreen__historycount">{conversations.length}</span>
              </div>
              {conversationsError ? (
                <p className="askscreen__pickerr" role="alert">
                  {conversationsError}
                </p>
              ) : null}
              {conversations.length === 0 ? (
                <p className="askscreen__hint">
                  Each answered question starts a conversation you can follow up on. Only you can
                  see yours.
                </p>
              ) : (
                <ul className="askscreen__convlist">
                  {conversations.map((item) => (
                    <li
                      key={item.id}
                      className={
                        item.id === conversation?.id
                          ? "askscreen__conv askscreen__conv--active"
                          : "askscreen__conv"
                      }
                    >
                      <span className="askscreen__convtitle">{item.title}</span>
                      <span className="askscreen__convmeta">
                        {item.turn_count} {item.turn_count === 1 ? "question" : "questions"}
                      </span>
                      <span className="askscreen__convactions">
                        <button
                          type="button"
                          className="btn btn--quiet"
                          disabled={conversationBusy !== null || item.id === conversation?.id}
                          onClick={() => void continueConversation(item.id)}
                          aria-label={`Continue conversation: ${item.title}`}
                        >
                          {item.id === conversation?.id ? "Open" : "Continue"}
                        </button>
                        <button
                          type="button"
                          className="btn btn--quiet"
                          disabled={conversationBusy !== null}
                          onClick={() => void removeConversation(item.id)}
                          aria-label={`Delete conversation: ${item.title}`}
                        >
                          Delete
                        </button>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          ) : null}
          <div className="askscreen__historyhead">
            <h2 className="askscreen__h2">History</h2>
            <span className="askscreen__historycount">
              {historyTotal !== null ? historyTotal : "—"}
            </span>
          </div>
          {!dsId ? (
            <Empty title="Pick a datasource to see its history" hint={dsPickerError ?? undefined} />
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
              onReachEnd={showHistory ? () => void loadMoreHistory() : undefined}
              loadingMore={historyLoadingMore}
              loadMoreError={historyLoadMoreError}
              emptyState={historyEmptyState}
              renderItem={(r) => (
                <HistoryRow run={r} focused={r.id === runId} onFocus={() => setParams({ run: r.id })} />
              )}
            />
          )}
        </div>
      </div>
    </div>
  );
}
