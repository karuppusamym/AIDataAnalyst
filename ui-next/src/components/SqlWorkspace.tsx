import { useEffect, useRef, useState } from "react";
import type {
  QueryExecutionResponse,
  SqlDraftReceiptRead,
  SqlDraftResponse,
  SqlFindingRead,
} from "../lib/types";
import {
  createSqlDraft,
  describeSqlWorkspaceError,
  runSqlDraft,
  type SqlWorkspaceProblem,
} from "../lib/api/sqlWorkspace";
import { QueryResultTable } from "./QueryResultTable";
import { Button, Field, Pill } from "./primitives";
import "./SqlWorkspace.css";

/**
 * R11-SQL01: SQL a person reads before it runs.
 *
 * Draft from the question above (the governed Ask stages, stopping at the SQL) or paste a
 * statement; Validate runs the gateway's checks without executing anything; Run sends the exact
 * validated text back with its receipt and runs it once. Editing the text after validating
 * disables Run until it is validated again -- the server refuses an edited statement anyway,
 * and saying so up front is kinder than a refusal.
 */
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
  const [validatedSql, setValidatedSql] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<SqlDraftReceiptRead | null>(null);
  const [busy, setBusy] = useState<"draft" | "validate" | "run" | null>(null);
  const [problem, setProblem] = useState<SqlWorkspaceProblem | null>(null);
  const [execution, setExecution] = useState<QueryExecutionResponse | null>(null);
  const [executedAt, setExecutedAt] = useState<Date | null>(null);
  const controller = useRef<AbortController | null>(null);

  // A receipt names one datasource and one product; switching either spends it.
  useEffect(() => {
    setDraft(null);
    setReceipt(null);
    setValidatedSql(null);
    setExecution(null);
    setProblem(null);
  }, [datasourceId, productKey]);

  useEffect(() => () => controller.current?.abort(), []);

  const edited = validatedSql !== null && sql !== validatedSql;
  const canRun = receipt !== null && receipt.status === "VALIDATED" && !edited && busy === null;

  async function send(kind: "draft" | "validate") {
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
          ...(kind === "draft" ? { question: question.trim() } : { sql }),
          context_product_key: productKey,
        },
        active.signal,
      );
      const text = kind === "draft" ? (response.sql ?? null) : sql;
      if (text !== null) setSql(text);
      setDraft(response);
      setReceipt(response.receipt ?? null);
      setValidatedSql(response.receipt ? text : null);
    } catch (error) {
      if (active.signal.aborted) return;
      setProblem(describeSqlWorkspaceError(error));
      setDraft(null);
      setReceipt(null);
      setValidatedSql(null);
    } finally {
      if (controller.current === active) setBusy(null);
    }
  }

  async function run() {
    if (receipt === null || validatedSql === null) return;
    controller.current?.abort();
    const active = new AbortController();
    controller.current = active;
    setBusy("run");
    setProblem(null);
    try {
      const response = await runSqlDraft(
        receipt.id,
        { sql: validatedSql, context_product_key: productKey },
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
      <div className="sqlws__actions">
        {edited ? (
          <span className="sqlws__note" role="status">
            Edited since it was validated — validate again to run it.
          </span>
        ) : null}
        <Button
          onClick={() => void send("draft")}
          disabled={busy !== null || question.trim().length < 3}
          title="Draft SQL for the question above without running it"
        >
          {busy === "draft" ? "Drafting…" : "Draft from question"}
        </Button>
        <Button onClick={() => void send("validate")} disabled={busy !== null || sql.trim() === ""}>
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
    </section>
  );
}
