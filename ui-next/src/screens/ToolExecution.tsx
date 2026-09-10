import { useCallback, useEffect, useState } from "react";
import type { GovernedToolVersionRead, ToolExecutionResponse, ToolParameterDefinition } from "../lib/types";
import { executeToolVersion } from "../lib/api";
import { Button, ErrorState, Field } from "../components/primitives";
import { failureText } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Controlled execution: running one published tool version and rendering what
   the query gateway returned.

   One unit because the three parts only make sense together. The inputs are
   generated from the version's own `parameters` schema -- type, requiredness,
   allowed values and bounds all come from the contract, never from this file
   -- so the form cannot offer a value the SQL boundary would reject. The
   response view renders the same evidence legacy's shared `renderQueryResult()`
   shows: row count, elapsed time, plan cost, masked columns, the rows, and
   the normalized SQL, because "it returned 12 rows" is not an answer a
   reviewer can check and the normalized SQL is.

   Only a PUBLISHED version is executable (`tool_api.py:881`). The panel says
   so rather than offering a form that would 409.
--------------------------------------------------------------------------- */

function record(v: unknown, key: string): unknown {
  return v && typeof v === "object" ? (v as Record<string, unknown>)[key] : undefined;
}

function ParameterField({
  def,
  value,
  onChange,
}: {
  def: ToolParameterDefinition;
  value: string | boolean;
  onChange: (v: string | boolean) => void;
}) {
  const label = def.name.replace(/_/g, " ");
  if (def.parameter_type === "BOOLEAN") {
    return (
      <label className="trexecform__check">
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} /> {label}
      </label>
    );
  }
  if (def.allowed_values?.length) {
    return (
      <Field label={label}>
        <select required={def.required} value={String(value)} onChange={(e) => onChange(e.target.value)}>
          {def.allowed_values.map((v) => (
            <option key={String(v)} value={String(v)}>
              {String(v)}
            </option>
          ))}
        </select>
      </Field>
    );
  }
  const type =
    def.parameter_type === "INTEGER" || def.parameter_type === "NUMBER"
      ? "number"
      : def.parameter_type === "DATE"
        ? "date"
        : def.sensitive
          ? "password"
          : "text";
  return (
    <Field label={label}>
      <input
        type={type}
        required={def.required}
        step={def.parameter_type === "NUMBER" ? "any" : def.parameter_type === "INTEGER" ? "1" : undefined}
        min={def.minimum ?? undefined}
        max={def.maximum ?? undefined}
        maxLength={def.max_length ?? undefined}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  );
}

function ExecutionResultView({ result }: { result: ToolExecutionResponse }) {
  const execution = result.execution;
  const rows = execution.rows ?? [];
  const headers = rows[0] ? Object.keys(rows[0]) : [];
  const gateMessage = result.quality_gate ? String(record(result.quality_gate, "message") ?? "") : null;

  return (
    <div className="trresult">
      <p className="trresult__answer">
        {result.tool_slug} version {result.tool_version} completed.
      </p>
      {gateMessage ? (
        <div className="trresult__gate" role="alert">
          {gateMessage}
        </div>
      ) : null}
      <div className="trresult__strip">
        <span>{execution.row_count} rows</span>
        <span>{execution.elapsed_ms} ms</span>
        <span>Cost {execution.plan_cost}</span>
        <span>{execution.masked_columns.length} masked</span>
        <span>{execution.referenced_columns.length} referenced columns</span>
      </div>
      <div className="trresult__tablewrap">
        {rows.length === 0 ? (
          <p className="trresult__none">Query returned no rows.</p>
        ) : (
          <table className="trresult__table">
            <thead>
              <tr>
                {headers.map((h) => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {headers.map((h) => (
                    <td key={h}>{String(row[h] ?? "")}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <span className="trresult__mono">
        Execution {execution.execution_id} / {execution.normalized_sql}
      </span>
    </div>
  );
}

export function ExecutionPanel({ tool }: { tool: GovernedToolVersionRead | null }) {
  const [paramValues, setParamValues] = useState<Record<string, string | boolean>>({});
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<ToolExecutionResponse | null>(null);
  const [execError, setExecError] = useState<string | null>(null);

  // A previous version's result must never be read as this one's, so the
  // panel is reset by identity of the selected version, not by its contents.
  useEffect(() => {
    setResult(null);
    setExecError(null);
    if (!tool || tool.status !== "PUBLISHED") {
      setParamValues({});
      return;
    }
    const initial: Record<string, string | boolean> = {};
    tool.parameters.forEach((p) => {
      initial[p.name] = p.parameter_type === "BOOLEAN" ? Boolean(p.default) : p.default != null ? String(p.default) : "";
    });
    setParamValues(initial);
  }, [tool]);

  const canExecute = tool != null && tool.status === "PUBLISHED";

  const submit = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!tool || tool.status !== "PUBLISHED") return;
      const parameters: Record<string, unknown> = {};
      tool.parameters.forEach((def) => {
        let value: unknown = def.parameter_type === "BOOLEAN" ? Boolean(paramValues[def.name]) : paramValues[def.name];
        // An optional parameter left blank is absent, not empty string: the
        // gateway would bind '' as a literal and silently match nothing.
        if (value === "" && !def.required) return;
        if (def.parameter_type === "INTEGER") value = Number.parseInt(String(value), 10);
        if (def.parameter_type === "NUMBER") value = Number(value);
        parameters[def.name] = value;
      });
      setExecuting(true);
      setExecError(null);
      setResult(null);
      try {
        setResult(await executeToolVersion(tool.id, { parameters }));
      } catch (err) {
        setExecError(failureText(err));
      } finally {
        setExecuting(false);
      }
    },
    [tool, paramValues],
  );

  return (
    <article className="trexec">
      <header className="trexec__head">
        <p className="trexec__eyebrow">CONTROLLED EXECUTION</p>
        <h2 className="trexec__h2">
          {!tool
            ? "No published tool selected"
            : tool.status === "PUBLISHED"
              ? `${tool.name} v${tool.version}`
              : "Publish this version before execution"}
        </h2>
      </header>
      <form className="trexecform" onSubmit={(e) => void submit(e)}>
        <div className="trexecform__grid">
          {canExecute
            ? tool.parameters.map((def) => (
                <ParameterField
                  key={def.name}
                  def={def}
                  value={paramValues[def.name] ?? (def.parameter_type === "BOOLEAN" ? false : "")}
                  onChange={(v) => setParamValues((prev) => ({ ...prev, [def.name]: v }))}
                />
              ))
            : null}
        </div>
        <Button type="submit" variant="primary" disabled={!canExecute || executing}>
          {executing ? "Executing…" : "Execute tool"}
        </Button>
      </form>
      {executing ? (
        <div className="trexec__loading" role="status">
          Validating parameters and executing through the query gateway
        </div>
      ) : null}
      {execError ? (
        <ErrorState title="Tool execution stopped" detail={execError} onRetry={() => setExecError(null)} />
      ) : null}
      {result ? <ExecutionResultView result={result} /> : null}
    </article>
  );
}
