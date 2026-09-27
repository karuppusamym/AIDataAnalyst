import { useEffect, useMemo, useRef, useState } from "react";
import type {
  QueryExecutionResponse,
  SqlDraftParameter,
  SqlDraftReceiptRead,
  SqlDraftResponse,
  SqlFindingRead,
} from "../lib/types";
import {
  createSqlDraft,
  describeSqlWorkspaceError,
  listSqlDrafts,
  runSqlDraft,
  type SqlWorkspaceProblem,
} from "../lib/api/sqlWorkspace";
import { QueryResultTable } from "./QueryResultTable";
import { Button, Field, Pill } from "./primitives";
import type { Tone } from "./primitives";
import {
  bindingKey,
  parameterProblems,
  SqlParameterEditor,
  toSqlDraftParameters,
  type ParameterRow,
} from "./SqlWorkspaceParameters";
import "./SqlWorkspace.css";

/**
 * R11-SQL01: SQL a person reads before it runs.
 *
 * Draft from the question above (the governed Ask stages, stopping at the SQL) or paste a
 * statement; Validate runs the gateway's checks without executing anything; Run sends the exact
 * validated text back with its receipt and runs it once. Editing the text after validating
 * disables Run until it is validated again -- the server refuses an edited statement anyway,
 * and saying so up front is kinder than a refusal. Parameter values are part of what was
 * validated: changing one is the same as editing the text.
 */

/** What a receipt was earned for: the exact text and the exact parameters sent with it. */
interface ValidatedBinding {
  sql: string;
  parameters: SqlDraftParameter[];
}
/** A receipt's state as a person reads it: an unrun validation past its expiry is expired. */
export function receiptState(
  receipt: SqlDraftReceiptRead,
  now: Date = new Date(),
): { label: string; tone: Tone } {
  if (receipt.status === "EXECUTED") return { label: "Ran", tone: "info" };
  if (receipt.status === "FAILED") return { label: "Refused at run", tone: "bad" };
  if (receipt.status === "EXECUTING") return { label: "Running", tone: "warn" };
  if (new Date(receipt.expires_at) <= now) return { label: "Expired", tone: "mute" };
  return { label: "Validated, not run", tone: "ok" };
}

export function SqlWorkspace({
  datasourceId,
  productKey,
  question,
}: {
  datasourceId: string;
  productKey: string | null;
  question: string;
}) {
  const [sql, setSql] = useState("");
  const [draft, setDraft] = useState<SqlDraftResponse | null>(null);
  const [validated, setValidated] = useState<ValidatedBinding | null>(null);
  const [parameters, setParameters] = useState<ParameterRow[]>([]);
  const nextParameterKey = useRef(1);
  const [receipt, setReceipt] = useState<SqlDraftReceiptRead | null>(null);
  const [busy, setBusy] = useState<"draft" | "validate" | "run" | null>(null);
  const [problem, setProblem] = useState<SqlWorkspaceProblem | null>(null);
  const [execution, setExecution] = useState<QueryExecutionResponse | null>(null);
  const [executedAt, setExecutedAt] = useState<Date | null>(null);
  const controller = useRef<AbortController | null>(null);
  const [recent, setRecent] = useState<SqlDraftReceiptRead[] | null>(null);
  const [recentError, setRecentError] = useState<string | null>(null);
  const [historyTick, setHistoryTick] = useState(0);

  // A receipt names one datasource and one product; switching either spends it.
  useEffect(() => {
    setDraft(null);
    setReceipt(null);
    setValidated(null);
    setExecution(null);
    setProblem(null);
  }, [datasourceId, productKey]);

  useEffect(() => () => controller.current?.abort(), []);

  // The caller's own history on this datasource, read again after every validate and run.
  useEffect(() => {
    const active = new AbortController();
    setRecentError(null);
    listSqlDrafts(datasourceId, active.signal)
      .then((items) => {
        if (!active.signal.aborted) setRecent(items);
      })
      .catch((error: unknown) => {
        if (active.signal.aborted) return;
        setRecent([]);
        setRecentError(error instanceof Error ? error.message : String(error));
      });
    return () => active.abort();
  }, [datasourceId, historyTick]);

  const problems = useMemo(() => parameterProblems(parameters), [parameters]);
  // Only a complete set has a wire form; an incomplete one cannot be what was validated.
  const wireParameters = useMemo(
    () => (problems.size === 0 ? toSqlDraftParameters(parameters) : null),
    [parameters, problems],
  );
  const sqlEdited = validated !== null && sql !== validated.sql;
  const parametersEdited =
    validated !== null &&
    (wireParameters === null || bindingKey(wireParameters) !== bindingKey(validated.parameters));
  const edited = sqlEdited || parametersEdited;
  const canRun = receipt !== null && receipt.status === "VALIDATED" && !edited && busy === null;

  function changeParameter(key: number, change: Partial<Omit<ParameterRow, "key">>) {
    setParameters((rows) => rows.map((row) => (row.key === key ? { ...row, ...change } : row)));
  }

  function addParameter(name: string) {
    const key = nextParameterKey.current;
    nextParameterKey.current += 1;
    setParameters((rows) => [...rows, { key, name, parameterType: "STRING", value: "" }]);
  }

  function removeParameter(key: number) {
    setParameters((rows) => rows.filter((row) => row.key !== key));
  }

  async function send(kind: "draft" | "validate") {
    // A question drafts a statement; parameters bind one the person sends.
    const sent: SqlDraftParameter[] = kind === "validate" ? (wireParameters ?? []) : [];
    controller.current?.abort();
    const active = new AbortController();
    controller.current = active;
    setBusy(kind);
    setProblem(null);
    setExecution(null);
    try {
      const response = await createSqlDraft(
        datasourceId,
        {
          ...(kind === "draft"
            ? { question: question.trim() }
            : { sql, ...(sent.length > 0 ? { parameters: sent } : {}) }),
          context_product_key: productKey,
        },
        active.signal,
      );
      const text = kind === "draft" ? (response.sql ?? null) : sql;
      if (text !== null) setSql(text);
      setDraft(response);
      setReceipt(response.receipt ?? null);
      setValidated(response.receipt && text !== null ? { sql: text, parameters: sent } : null);
      if (response.receipt) setHistoryTick((tick) => tick + 1);
    } catch (error) {
      if (active.signal.aborted) return;
      setProblem(describeSqlWorkspaceError(error));
      setDraft(null);
      setReceipt(null);
      setValidated(null);
    } finally {
      if (controller.current === active) setBusy(null);
    }
  }

  async function run() {
    if (receipt === null || validated === null) return;
    controller.current?.abort();
    const active = new AbortController();
    controller.current = active;
    setBusy("run");
    setProblem(null);
    try {
      // Exactly what was validated -- the text and the values -- never the fields as they are now.
      const response = await runSqlDraft(
        receipt.id,
        {
          sql: validated.sql,
          ...(validated.parameters.length > 0 ? { parameters: validated.parameters } : {}),
          context_product_key: productKey,
        },
        active.signal,
      );
      setReceipt(response.receipt);
      setExecution(response.execution);
      setExecutedAt(new Date());
    } catch (error) {
      if (active.signal.aborted) return;
      const described = describeSqlWorkspaceError(error);
      setProblem(described);
      // Every refusal here spends or voids the receipt from the person's point of view.
      if (described.revalidate) setReceipt(null);
    } finally {
      if (controller.current === active) setBusy(null);
      setHistoryTick((tick) => tick + 1);
    }
  }

  const findings: SqlFindingRead[] = draft?.validation?.findings ?? [];
  const toolAnswers = draft?.reason === "GOVERNED_TOOL_ANSWERS";

  return (
    <section className="sqlws" aria-label="SQL review workspace">
      <div className="sqlws__head">
        <h2 className="sqlws__h2">Review SQL before it runs</h2>
        <p className="sqlws__lede">
          Draft SQL from the question above, or paste your own. Validating checks it against your
          access{productKey ? " and the context product" : ""} without running it; nothing
          executes until you press Run.
        </p>
      </div>
      <Field label="SQL">
        <textarea
          className="sqlws__sql"
          aria-label="SQL statement"
          value={sql}
          onChange={(e) => setSql(e.target.value)}
          placeholder="SELECT …"
          rows={6}
          spellCheck={false}
        />
      </Field>
      <SqlParameterEditor
        rows={parameters}
        problems={problems}
        sql={sql}
        onChange={changeParameter}
        onAdd={addParameter}
        onRemove={removeParameter}
      />
      <div className="sqlws__actions">
        {sqlEdited ? (
          <span className="sqlws__note" role="status">
            Edited since it was validated — validate again to run it.
          </span>
        ) : parametersEdited ? (
          <span className="sqlws__note" role="status">
            Parameter values changed since it was validated — validate again to run it.
          </span>
        ) : null}
        {problems.size > 0 ? (
          <span className="sqlws__note" role="status">
            Fix the parameters above to validate.
          </span>
        ) : null}
        <Button
          onClick={() => void send("draft")}
          disabled={busy !== null || question.trim().length < 3}
          title="Draft SQL for the question above without running it"
        >
          {busy === "draft" ? "Drafting…" : "Draft from question"}
        </Button>
        <Button
          onClick={() => void send("validate")}
          disabled={busy !== null || sql.trim() === "" || problems.size > 0}
        >
          {busy === "validate" ? "Validating…" : "Validate"}
        </Button>
        <Button variant="primary" onClick={() => void run()} disabled={!canRun}>
          {busy === "run" ? "Running…" : "Run"}
        </Button>
      </div>

      {problem ? (
        <div className="sqlws__problem" role="alert">
          <div className="sqlws__problemt">{problem.title}</div>
          <div className="sqlws__problemd">{problem.detail}</div>
        </div>
      ) : null}

      {toolAnswers ? (
        <div className="sqlws__problem" role="status">
          <div className="sqlws__problemt">An approved governed tool answers this question</div>
          <div className="sqlws__problemd">
            Ask it above to run that tool: its SQL is not handed out as ad-hoc SQL, because
            running it outside the tool would drop the tool's own governance.
          </div>
        </div>
      ) : null}

      {draft?.validation ? (
        <div className="sqlws__verdict" aria-label="Validation result">
          <div className="sqlws__verdicthead">
            {!draft.validation.valid ? (
              <Pill tone="bad">Cannot run</Pill>
            ) : receipt?.status === "EXECUTED" ? (
              <Pill tone="info">Ran once</Pill>
            ) : (
              <Pill tone="ok">Valid — not run</Pill>
            )}
            {draft.origin === "GENERATED" ? <Pill tone="mute">Drafted by the model</Pill> : null}
            {receipt && receipt.status === "VALIDATED" ? (
              <span className="sqlws__note">
                Run before {new Date(receipt.expires_at).toLocaleTimeString()}
              </span>
            ) : receipt?.status === "EXECUTED" ? (
              <span className="sqlws__note">Validate again to run it again.</span>
            ) : null}
          </div>
          {draft.validation.referenced_tables.length > 0 ? (
            <div className="sqlws__tables">
              Reads {draft.validation.referenced_tables.join(", ")}
            </div>
          ) : null}
          {findings.length > 0 ? (
            <ul className="sqlws__findings" aria-label="Validation findings">
              {findings.map((finding, index) => (
                <li key={`${finding.code}-${index}`} className="sqlws__finding">
                  <span className="sqlws__code">{finding.code}</span>
                  {finding.ref ? <span className="sqlws__ref"> · {finding.ref}</span> : null}
                  <div className="sqlws__hint">{finding.hint}</div>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {execution && executedAt ? (
        <QueryResultTable
          execution={execution}
          semanticVersion={null}
          policyVersion={null}
          executedAt={executedAt}
        />
      ) : null}

      <div className="sqlws__recent" aria-label="Your recent reviewed SQL">
        <h3 className="sqlws__h3">Your recent reviewed SQL</h3>
        {recentError ? (
          <p className="sqlws__note" role="alert">
            Your history could not be read ({recentError}). Validating and running still work.
          </p>
        ) : recent === null ? (
          <p className="sqlws__note">Loading…</p>
        ) : recent.length === 0 ? (
          <p className="sqlws__note">Nothing validated here yet.</p>
        ) : (
          <ul className="sqlws__recentlist">
            {recent.map((item) => {
              const state = receiptState(item);
              return (
                <li key={item.id} className="sqlws__recentitem">
                  <div className="sqlws__recenthead">
                    <Pill tone={state.tone}>{state.label}</Pill>
                    <span className="sqlws__note">
                      {item.origin === "GENERATED" ? "drafted by the model" : "your SQL"} ·{" "}
                      {new Date(item.created_at).toLocaleString()}
                    </span>
                  </div>
                  <code className="sqlws__shape">
                    {item.redacted_sql ?? "Shape withheld: it could not be redacted safely."}
                  </code>
                </li>
              );
            })}
          </ul>
        )}
        <p className="sqlws__note">
          Literals are replaced in this list, and parameter values and results are never kept:
          validate again to run a statement again.
        </p>
      </div>
    </section>
  );
}
