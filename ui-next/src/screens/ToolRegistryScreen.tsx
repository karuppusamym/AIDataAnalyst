import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  DataSourceRead,
  GovernedToolVersionCreate,
  GovernedToolVersionRead,
  ProjectRead,
  ToolExecutionResponse,
  ToolParameterDefinition,
} from "../lib/types";
import {
  ApiError,
  createToolVersion,
  executeToolVersion,
  fetchOrgDatasources,
  fetchOrgProjects,
  fetchTools,
  requestToolDeprecation,
  submitToolForReview,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { useOrgId } from "../lib/org";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./ToolRegistryScreen.css";

/* ---------------------------------------------------------------------------
   Tool registry -- the legacy portal's `tools` view (`ui/index.html#tools-view`,
   nav `data-view="tools"` / sidebar "Tool registry"), ported onto the real,
   already-merged `tool_api.py` routes that view calls:

     - GET  /v1/projects/{project_id}/tools                  list_tools, tool_api.py:609
     - POST /v1/projects/{project_id}/tools                   create_tool_version, tool_api.py:348
     - POST /v1/tool-versions/{version_id}/submit              submit_tool_for_review, tool_api.py:692
     - POST /v1/tool-versions/{version_id}/deprecation-submit  submit_tool_deprecation, tool_api.py:756
     - POST /v1/tool-versions/{version_id}/execute              execute_tool, tool_api.py:881

   Three pieces, matching the legacy screen's own `tools-layout` exactly:
     1. registry     the `Version inventory` list (`#tools-table`), filterable
                      by status, one row per `GovernedToolVersion` -- project-
                      scoped, same `fetchOrgProjects` picker `SemanticsScreen`/
                      `ContextProductsScreen` use (`list_tools` takes a
                      `project_id`; there is no org-wide tool browse).
     2. detail        the selected version's contract (`#tool-detail`): SQL
                      template, data source, allowed roles, referenced tables,
                      fingerprint, plus lifecycle actions matching legacy's
                      `selectTool()` (`data-submit-tool` / `data-deprecate-tool`
                      / `data-new-version`).
     3. execution     `Controlled execution` (`#tool-form`/`#tool-parameters`/
                      `#tool-result`): parameter inputs generated from the
                      published version's own `parameters` schema, exactly
                      `parameterInput()`'s type/required/allowed_values/bounds
                      mapping in the legacy screen, then a real
                      `POST .../execute` and the returned `QueryExecutionResponse`
                      rendered the way legacy's shared `renderQueryResult()`
                      does (row count, elapsed ms, plan cost, masked columns,
                      the actual result rows, and the normalized SQL).

   `New tool version` is legacy's own `<dialog id="tool-dialog">` -- ported
   here as an always-visible rail panel (the same shape `ContextProductsScreen`
   already used for its "Create draft" panel) rather than a modal, since that
   is the established ui-next idiom for a create form and every field below
   maps 1:1 onto `#tool-author-form`'s inputs, including the parameter
   builder (`#tool-parameter-builder` / `addToolParameter()` /
   `collectToolParameters()`) and its own subsection copy ("Bind values as
   typed SQL literals. Identifiers cannot be parameters."). Selecting
   "New version" on an existing tool prefills this panel from that version
   exactly like legacy's `openToolAuthor(existing)` -- submitting under the
   same `slug` is what `_persist_tool_version_draft` (tool_api.py:201) uses
   to attach the draft to the existing `GovernedTool` as its next version,
   not a new tool.

   Left out of scope, same as the legacy screen: the multi-table blueprint
   helper (`create_multi_table_tool_blueprint`, `create_view_tool_blueprint`)
   and the certification-cases/certification-runs sub-flow
   (`tool_api.py` lines 400-524 and 1056-1400+) -- legacy's `tools-view` never
   calls any of those; they belong to a different, not-yet-ported surface.
--------------------------------------------------------------------------- */

const statusTone = (s: string): Tone =>
  s === "PUBLISHED" ? "ok" : s === "DRAFT" ? "mute" : s === "REVIEW_REQUIRED" ? "info" : s === "DEPRECATED" ? "bad" : "warn";

const splitIds = (value: string): string[] =>
  value.split(",").map((v) => v.trim()).filter(Boolean);

function record(v: unknown, key: string): unknown {
  return v && typeof v === "object" ? (v as Record<string, unknown>)[key] : undefined;
}

type Kind = "info" | "success" | "error";

/* -------------------------------- create form ------------------------------- */

interface ParameterDraft {
  name: string;
  parameter_type: ToolParameterDefinition["parameter_type"];
  required: boolean;
  sensitive: boolean;
  allowedValues: string;
  defaultJson?: string;
  minimum?: number | null;
  maximum?: number | null;
  max_length?: number | null;
}

const blankParameter = (): ParameterDraft => ({
  name: "",
  parameter_type: "STRING",
  required: true,
  sensitive: false,
  allowedValues: "",
});

interface FormState {
  slug: string;
  name: string;
  datasourceId: string;
  allowedRoles: string;
  description: string;
  sqlTemplate: string;
}

const INITIAL_FORM: FormState = {
  slug: "",
  name: "",
  datasourceId: "",
  allowedRoles: "Analyst,ToolConsumer",
  description: "",
  sqlTemplate: "",
};

function ParameterBuilder({
  parameters,
  onChange,
}: {
  parameters: ParameterDraft[];
  onChange: (next: ParameterDraft[]) => void;
}) {
  const update = (i: number, patch: Partial<ParameterDraft>) => {
    onChange(parameters.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  };
  const remove = (i: number) => onChange(parameters.filter((_, idx) => idx !== i));

  return (
    <div className="trparams">
      {parameters.map((p, i) => (
        <div className="trparams__row" key={i}>
          <label>
            Name
            <input
              required
              pattern="[a-z][a-z0-9_]{0,63}"
              value={p.name}
              onChange={(e) => update(i, { name: e.target.value })}
            />
          </label>
          <label>
            Type
            <select
              value={p.parameter_type}
              onChange={(e) => update(i, { parameter_type: e.target.value as ParameterDraft["parameter_type"] })}
            >
              {(["STRING", "INTEGER", "NUMBER", "BOOLEAN", "DATE"] as const).map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <label className="trparams__check">
            <input type="checkbox" checked={p.required} onChange={(e) => update(i, { required: e.target.checked })} />
            Required
          </label>
          <label className="trparams__check">
            <input type="checkbox" checked={p.sensitive} onChange={(e) => update(i, { sensitive: e.target.checked })} />
            Sensitive
          </label>
          <label>
            Allowed values (JSON array or comma-separated)
            <input
              placeholder="NY,NJ"
              value={p.allowedValues}
              onChange={(e) => update(i, { allowedValues: e.target.value })}
            />
          </label>
          <label>Default (JSON)<input value={p.defaultJson ?? ""} placeholder='"NY" or 10' onChange={e => update(i, { defaultJson: e.target.value })} /></label>
          {(["minimum", "maximum", "max_length"] as const).map(key => <label key={key}>{key.replace("_", " ")}<input type="number" value={p[key] ?? ""} onChange={e => update(i, { [key]: e.target.value === "" ? null : Number(e.target.value) })} /></label>)}
          <button
            type="button"
            className="trparams__remove"
            aria-label="Remove parameter"
            onClick={() => remove(i)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

function CreateToolPanel({
  projectId,
  datasourceOptions,
  form,
  setForm,
  parameters,
  setParameters,
  creating,
  onSubmit,
  editingSlug,
  onCancelEdit,
}: {
  projectId: string | null;
  datasourceOptions: DataSourceRead[];
  form: FormState;
  setForm: (f: FormState) => void;
  parameters: ParameterDraft[];
  setParameters: (p: ParameterDraft[]) => void;
  creating: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  editingSlug: string | null;
  onCancelEdit: () => void;
}) {
  const setField = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm({ ...form, [key]: value });

  return (
    <article className="trform">
      <form onSubmit={onSubmit}>
        <header className="trform__head">
          <div>
            <p className="trform__eyebrow">TOOL CONTRACT</p>
            <h2 className="trform__h2">{editingSlug ? `New version of ${editingSlug}` : "New tool version"}</h2>
            <p className="trform__lede">Bind values as typed SQL literals. Identifiers cannot be parameters.</p>
          </div>
          <Pill tone="warn">DRAFT</Pill>
        </header>

        <div className="trform__grid">
          <Field label="Stable slug">
            <input
              required
              pattern="[a-z][a-z0-9_]{1,99}"
              placeholder="customer_lookup"
              value={form.slug}
              disabled={editingSlug !== null}
              onChange={(e) => setField("slug", e.target.value)}
            />
          </Field>
          <Field label="Version name">
            <input
              required
              minLength={2}
              placeholder="Customer lookup"
              value={form.name}
              onChange={(e) => setField("name", e.target.value)}
            />
          </Field>
          <Field label="Data source">
            <select
              required
              value={form.datasourceId}
              onChange={(e) => setField("datasourceId", e.target.value)}
            >
              <option value="">
                {projectId ? (datasourceOptions.length ? "Select a data source…" : "No sources in project") : "Select a project first"}
              </option>
              {datasourceOptions.map((d) => (
                <option key={d.id} value={d.id}>{d.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Allowed roles">
            <input
              required
              placeholder="Analyst,ToolConsumer"
              value={form.allowedRoles}
              onChange={(e) => setField("allowedRoles", e.target.value)}
            />
          </Field>
          <div className="trform__span2">
            <Field label="Description">
              <textarea
                required
                minLength={3}
                rows={3}
                placeholder="Purpose, permitted use, and business owner"
                value={form.description}
                onChange={(e) => setField("description", e.target.value)}
              />
            </Field>
          </div>
          <div className="trform__span2">
            <Field label="SQL template">
              <textarea
                required
                rows={8}
                spellCheck={false}
                placeholder="SELECT customer_id FROM public.customers WHERE state = :state"
                className="trform__sql"
                value={form.sqlTemplate}
                onChange={(e) => setField("sqlTemplate", e.target.value)}
              />
            </Field>
          </div>
        </div>

        <div className="trform__subhead">
          <div>
            <h3>Parameters</h3>
            <p>Bind values as typed SQL literals. Identifiers cannot be parameters.</p>
          </div>
          <Button onClick={() => setParameters([...parameters, blankParameter()])}>Add parameter</Button>
        </div>
        <ParameterBuilder parameters={parameters} onChange={setParameters} />

        <div className="trform__actions">
          {editingSlug ? <Button onClick={onCancelEdit}>Cancel</Button> : null}
          <Button type="submit" variant="primary" disabled={creating || !projectId}>
            {creating ? "Creating…" : "Create draft version"}
          </Button>
        </div>
      </form>
    </article>
  );
}

/* -------------------------------- registry row ------------------------------- */

function ToolRow({ tool, selected, onSelect }: { tool: GovernedToolVersionRead; selected: boolean; onSelect: () => void }) {
  return (
    <article className={`trrow${selected ? " trrow--sel" : ""}`} aria-label={tool.name}>
      <button className="trrow__click" onClick={onSelect}>
        <div className="trrow__badges">
          <Pill tone={statusTone(tool.status)}>{tool.status.toLowerCase().replace(/_/g, " ")}</Pill>
        </div>
        <h3 className="trrow__title">{tool.name}</h3>
        <div className="trrow__key">{tool.slug} · version {tool.version}</div>
      </button>
    </article>
  );
}

/* -------------------------------- detail panel ------------------------------- */

function ToolDetail({
  tool,
  datasourceName,
  busy,
  onSubmitReview,
  onRequestDeprecation,
  onNewVersion,
}: {
  tool: GovernedToolVersionRead | null;
  datasourceName: string;
  busy: boolean;
  onSubmitReview: () => void;
  onRequestDeprecation: () => void;
  onNewVersion: () => void;
}) {
  if (!tool) {
    return (
      <article className="trdetail">
        <Empty title="Select a tool version" hint="Review its contract, SQL boundary, roles, and lifecycle actions." />
      </article>
    );
  }

  return (
    <article className="trdetail">
      <header className="trdetail__head">
        <div>
          <p className="trdetail__eyebrow">{tool.slug} / VERSION {tool.version}</p>
          <h2 className="trdetail__h2">{tool.name}</h2>
        </div>
        <Pill tone={statusTone(tool.status)}>{tool.status.toLowerCase().replace(/_/g, " ")}</Pill>
      </header>
      <p className="trdetail__desc">{tool.description}</p>
      <div className="trdetail__grid">
        <div><span>Data source</span><strong>{datasourceName || tool.datasource_id}</strong></div>
        <div><span>Allowed roles</span><strong>{tool.allowed_roles.join(", ")}</strong></div>
        <div><span>Parameters</span><strong>{tool.parameters.length}</strong></div>
        <div><span>Referenced tables</span><strong>{tool.referenced_tables.join(", ") || "Validated at execution"}</strong></div>
        <div><span>Maker</span><strong>{tool.created_by}</strong></div>
        <div><span>Fingerprint</span><strong>{tool.fingerprint.slice(0, 16)}</strong></div>
      </div>
      <pre className="trdetail__sql">{tool.sql_template}</pre>
      <div className="trdetail__actions">
        {tool.status === "DRAFT" ? (
          <Button disabled={busy} onClick={onSubmitReview}>{busy ? "Submitting…" : "Submit for review"}</Button>
        ) : null}
        {tool.status === "PUBLISHED" ? (
          <Button disabled={busy} onClick={onRequestDeprecation}>{busy ? "Requesting…" : "Request deprecation"}</Button>
        ) : null}
        <Button onClick={onNewVersion}>New version</Button>
      </div>
    </article>
  );
}

/* -------------------------------- execution panel ------------------------------- */

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
            <option key={String(v)} value={String(v)}>{String(v)}</option>
          ))}
        </select>
      </Field>
    );
  }
  const type = def.parameter_type === "INTEGER" || def.parameter_type === "NUMBER"
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
      <p className="trresult__answer">{result.tool_slug} version {result.tool_version} completed.</p>
      {gateMessage ? <div className="trresult__gate" role="alert">{gateMessage}</div> : null}
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
              <tr>{headers.map((h) => <th key={h}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>{headers.map((h) => <td key={h}>{String(row[h] ?? "")}</td>)}</tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <span className="trresult__mono">Execution {execution.execution_id} / {execution.normalized_sql}</span>
    </div>
  );
}

function ExecutionPanel({ tool }: { tool: GovernedToolVersionRead | null }) {
  const [paramValues, setParamValues] = useState<Record<string, string | boolean>>({});
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<ToolExecutionResponse | null>(null);
  const [execError, setExecError] = useState<string | null>(null);

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
        if (value === "" && !def.required) return;
        if (def.parameter_type === "INTEGER") value = Number.parseInt(String(value), 10);
        if (def.parameter_type === "NUMBER") value = Number(value);
        parameters[def.name] = value;
      });
      setExecuting(true);
      setExecError(null);
      setResult(null);
      try {
        const response = await executeToolVersion(tool.id, { parameters });
        setResult(response);
      } catch (err) {
        setExecError(err instanceof ApiError ? err.detail : (err as Error).message);
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
          {!tool ? "No published tool selected" : tool.status === "PUBLISHED" ? `${tool.name} v${tool.version}` : "Publish this version before execution"}
        </h2>
      </header>
      <form className="trexecform" onSubmit={(e) => void submit(e)}>
        <div className="trexecform__grid">
          {canExecute
            ? tool!.parameters.map((def) => (
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
      {executing ? <div className="trexec__loading" role="status">Validating parameters and executing through the query gateway</div> : null}
      {execError ? <ErrorState title="Tool execution stopped" detail={execError} onRetry={() => setExecError(null)} /> : null}
      {result ? <ExecutionResultView result={result} /> : null}
    </article>
  );
}

/* -------------------------------- screen -------------------------------- */

export function ToolRegistryScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const projectId = params.get("project");
  const statusFilter = params.get("status") ?? "ALL";
  const toolId = params.get("tool");

  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);

  useEffect(() => {
    const ac = new AbortController();
    fetchOrgProjects(ORG, ac.signal)
      .then((page) => setProjects(page.items))
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setProjectsError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    fetchOrgDatasources(ORG, ac.signal)
      .then((page) => setDatasources(page.items))
      .catch(() => {
        /* the datasource picker degrades to empty; the project picker above still works */
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ORG]);

  const [tools, setTools] = useState<GovernedToolVersionRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<{ text: string; kind: Kind } | null>(null);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    if (!projectId) {
      setTools([]);
      setTotal(null);
      setLoading(false);
      setError(null);
      return;
    }
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchTools(projectId, { status: statusFilter !== "ALL" ? statusFilter : null, limit: 200 }, ac.signal);
      if (seq !== reqSeq.current) return;
      setTools(page.items);
      setTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      const detail = e instanceof ApiError ? e.detail : (e as Error).message;
      setError(detail);
      setStatusMsg({ text: detail, kind: "error" });
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [projectId, statusFilter]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const selectedTool = useMemo(() => tools.find((t) => t.id === toolId) ?? null, [tools, toolId]);
  const projectDatasources = useMemo(
    () => (projectId ? datasources.filter((d) => d.project_id === projectId) : []),
    [datasources, projectId],
  );
  const datasourceName = useMemo(
    () => (selectedTool ? datasources.find((d) => d.id === selectedTool.datasource_id)?.name ?? "" : ""),
    [datasources, selectedTool],
  );

  const [busyVersionId, setBusyVersionId] = useState<string | null>(null);

  const runVersionAction = useCallback(
    async (versionId: string, verb: string, successText: string, action: (id: string) => Promise<unknown>) => {
      setBusyVersionId(versionId);
      setStatusMsg({ text: `${verb}…`, kind: "info" });
      try {
        await action(versionId);
        setStatusMsg({ text: successText, kind: "success" });
        await load();
      } catch (e) {
        setStatusMsg({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setBusyVersionId(null);
      }
    },
    [load],
  );

  const [form, setForm] = useState<FormState>(INITIAL_FORM);
  const [parameters, setParameters] = useState<ParameterDraft[]>([blankParameter()]);
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const resetForm = useCallback(() => {
    setForm(INITIAL_FORM);
    setParameters([blankParameter()]);
    setEditingSlug(null);
  }, []);

  const prefillFromTool = useCallback((tool: GovernedToolVersionRead) => {
    setForm({
      slug: tool.slug,
      name: tool.name,
      datasourceId: tool.datasource_id,
      allowedRoles: tool.allowed_roles.join(","),
      description: tool.description,
      sqlTemplate: tool.sql_template,
    });
    setParameters(
      tool.parameters.length
        ? tool.parameters.map((p) => ({
            name: p.name,
            parameter_type: p.parameter_type,
            required: p.required ?? true,
            sensitive: p.sensitive ?? false,
            allowedValues: p.allowed_values ? JSON.stringify(p.allowed_values) : "",
            defaultJson: p.default == null ? "" : JSON.stringify(p.default),
            minimum: p.minimum, maximum: p.maximum, max_length: p.max_length,
          }))
        : [blankParameter()],
    );
    setEditingSlug(tool.slug);
  }, []);

  const submitCreate = useCallback(
    async (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (!projectId) {
        setStatusMsg({ text: "Select a project before creating a tool version.", kind: "error" });
        return;
      }
      let body: GovernedToolVersionCreate;
      try {
      body = {
        slug: form.slug,
        name: form.name,
        description: form.description,
        datasource_id: form.datasourceId,
        sql_template: form.sqlTemplate,
        parameters: parameters
          .filter((p) => p.name.trim().length > 0)
          .map((p) => {
            const convert = (v: string): unknown => {
              if (p.parameter_type === "NUMBER" || p.parameter_type === "INTEGER") {
                const number = Number(v);
                if (!Number.isFinite(number)) throw new Error("Allowed numeric values must be numbers");
                return number;
              }
              if (p.parameter_type === "BOOLEAN") {
                if (!["true", "false"].includes(v)) throw new Error("Boolean values must be true or false");
                return v === "true";
              }
              return v;
            };
            const allowed: unknown[] = p.allowedValues.trim().startsWith("[") ? JSON.parse(p.allowedValues) : splitIds(p.allowedValues).map(convert);
            return {
              name: p.name,
              parameter_type: p.parameter_type,
              required: p.required,
              sensitive: p.sensitive,
              ...(allowed.length ? { allowed_values: allowed } : {}),
              default: p.defaultJson?.trim() ? JSON.parse(p.defaultJson) : null,
              minimum: p.minimum, maximum: p.maximum, max_length: p.max_length,
            };
          }),
        allowed_roles: splitIds(form.allowedRoles),
      };
      } catch (e) { setStatusMsg({ text: (e as Error).message, kind: "error" }); return; }
      setCreating(true);
      setStatusMsg({ text: "Validating SQL contract…", kind: "info" });
      try {
        await createToolVersion(projectId, body);
        resetForm();
        setStatusMsg({ text: "Governed tool draft created and SQL contract validated.", kind: "success" });
        await load();
      } catch (e) {
        setStatusMsg({ text: e instanceof ApiError ? e.detail : (e as Error).message, kind: "error" });
      } finally {
        setCreating(false);
      }
    },
    [projectId, form, parameters, load, resetForm],
  );

  return (
    <div className="trscreen">
      <header className="trscreen__head">
        <div>
          <p className="trscreen__eyebrow">SAFE REUSE</p>
          <h1 className="trscreen__h1">Governed tool registry</h1>
          <p className="trscreen__lede">
            Author parameter-bound analytical tools, route them through review, and execute published versions.
          </p>
        </div>
        <div className="trscreen__filters">
          <Field label="Project">
            <select
              value={projectId ?? ""}
              onChange={(e) => setParams({ project: e.target.value || null, tool: null })}
            >
              <option value="">Select a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </Field>
          <Button onClick={() => void load()}>Refresh</Button>
        </div>
      </header>

      {projectsError ? <p className="trscreen__pickerr" role="alert">{projectsError}</p> : null}
      {statusMsg ? <div className={`trscreen__status trscreen__status--${statusMsg.kind}`} role="status">{statusMsg.text}</div> : null}

      <div className="trscreen__body">
        <div className="trscreen__main">
          <article className="trregistry">
            <header className="trregistry__head">
              <div>
                <p className="trregistry__eyebrow">CATALOG</p>
                <h2 className="trregistry__h2">Version inventory</h2>
              </div>
              <Field label="Status">
                <select value={statusFilter} onChange={(e) => setParams({ status: e.target.value === "ALL" ? null : e.target.value })}>
                  <option value="ALL">All</option>
                  <option value="DRAFT">Draft</option>
                  <option value="REVIEW_REQUIRED">Review required</option>
                  <option value="PUBLISHED">Published</option>
                  <option value="DEPRECATED">Deprecated</option>
                </select>
              </Field>
            </header>
            {!projectId ? (
              <Empty title="Pick a project to see its tool registry" hint="Governed tools are project-scoped, same as Semantics and Context products." />
            ) : error ? (
              <ErrorState title="Tool versions could not be loaded" detail={error} onRetry={() => void load()} />
            ) : loading ? (
              <div className="trscreen__skeleton" role="status" aria-live="polite">Loading tool versions…</div>
            ) : (
              <VirtualList
                items={tools}
                getKey={(t) => t.id}
                ariaLabel="Tool versions"
                estimateSize={92}
                totalCount={total}
                emptyState={<Empty title="No tool versions match" hint="Create a parameter-bound SQL tool to begin." />}
                renderItem={(t) => (
                  <ToolRow tool={t} selected={t.id === toolId} onSelect={() => setParams({ tool: t.id })} />
                )}
              />
            )}
          </article>

          <ToolDetail
            tool={selectedTool}
            datasourceName={datasourceName}
            busy={busyVersionId === selectedTool?.id}
            onSubmitReview={() =>
              selectedTool &&
              void runVersionAction(selectedTool.id, "Submitting for review", "Tool version submitted for independent review.", submitToolForReview)
            }
            onRequestDeprecation={() =>
              selectedTool &&
              void runVersionAction(selectedTool.id, "Requesting deprecation", "Tool deprecation submitted for independent review.", requestToolDeprecation)
            }
            onNewVersion={() => selectedTool && prefillFromTool(selectedTool)}
          />

          <ExecutionPanel tool={selectedTool} />
        </div>

        <aside className="trscreen__rail">
          <CreateToolPanel
            projectId={projectId}
            datasourceOptions={projectDatasources}
            form={form}
            setForm={setForm}
            parameters={parameters}
            setParameters={setParameters}
            creating={creating}
            onSubmit={(e) => void submitCreate(e)}
            editingSlug={editingSlug}
            onCancelEdit={resetForm}
          />
        </aside>
      </div>
    </div>
  );
}
