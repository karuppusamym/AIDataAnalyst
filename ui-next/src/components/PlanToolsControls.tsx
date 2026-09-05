import { useState } from "react";
import { fetchOrgProjects, fetchTools, fetchToolPlan, get, postJson } from "../lib/api";
import { useOrgId } from "../lib/org";
import type { GovernedToolVersionRead, ProjectRead, ToolParameterDefinition, ToolPlanCreate, ToolPlanRead } from "../lib/types";
import { Button, Field } from "./primitives";

export function PlanParameterInputs({ definitions, json, onChange }: {
  definitions: ToolParameterDefinition[]; json: string; onChange: (json: string) => void;
}) {
  let values: Record<string, unknown> = {};
  try { values = JSON.parse(json || "{}"); } catch { return <p role="alert">Fix parameter JSON to use typed inputs.</p>; }
  if (!values || typeof values !== "object" || Array.isArray(values)) return null;
  const set = (name: string, value: unknown) => { const next = { ...values }; if (value === undefined) delete next[name]; else next[name] = value; onChange(JSON.stringify(next, null, 2)); };
  return <>{definitions.map(p => <Field key={p.name} label={`Parameter ${p.name}${p.required ? " (required)" : ""}`}>
    {p.allowed_values?.length ? <select required={p.required} value={values[p.name] === undefined ? "" : JSON.stringify(values[p.name])} onChange={e => set(p.name, e.target.value ? JSON.parse(e.target.value) : undefined)}>
      <option value="">Choose value</option>{p.allowed_values.map(v => <option key={JSON.stringify(v)} value={JSON.stringify(v)}>{String(v)}</option>)}
    </select> : p.parameter_type === "BOOLEAN" ? <select required={p.required} value={values[p.name] === undefined ? "" : String(values[p.name])} onChange={e => set(p.name, e.target.value === "" ? undefined : e.target.value === "true")}><option value="">Choose value</option><option value="true">True</option><option value="false">False</option></select> : <input
      required={p.required} type={p.sensitive ? "password" : p.parameter_type === "DATE" ? "date" : ["INTEGER", "NUMBER"].includes(p.parameter_type) ? "number" : "text"}
      step={p.parameter_type === "NUMBER" ? "any" : undefined} min={p.minimum ?? undefined} max={p.maximum ?? undefined} maxLength={p.max_length ?? undefined}
      value={String(values[p.name] ?? "")} onChange={e => set(p.name, e.target.value === "" ? undefined : ["INTEGER", "NUMBER"].includes(p.parameter_type) ? Number(e.target.value) : e.target.value)} />}
  </Field>)}</>;
}

export function PublishedToolPicker({ onSelect }: { onSelect: (tool: GovernedToolVersionRead) => void }) {
  const org = useOrgId();
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [tools, setTools] = useState<GovernedToolVersionRead[]>([]);
  const [error, setError] = useState("");
  const load = async () => { try { setProjects((await fetchOrgProjects(org)).items); } catch (e) { setError((e as Error).message); } };
  return <details onToggle={e => { if (e.currentTarget.open && !projects.length) void load(); }}>
    <summary>Choose a published tool</summary>
    <Field label="Tool project"><select defaultValue="" onChange={e => {
      setTools([]); setError("");
      void fetchTools(e.target.value, { status: "PUBLISHED", limit: 200 }).then(page => setTools(page.items)).catch(e => setError(e.message));
    }}><option value="" disabled>Select project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
    <Field label="Published tool"><select value="" onChange={e => { const tool = tools.find(t => t.id === e.target.value); if (tool) onSelect(tool); }}>
      <option value="">Select tool and version</option>{tools.map(t => <option key={t.id} value={t.id}>{t.name} v{t.version}</option>)}
    </select></Field>
    {error ? <p role="alert">{error}</p> : null}
  </details>;
}

export function PlanLibrary({ onSelect, onDraft }: { onSelect: (id: string) => void; onDraft: (plan: ToolPlanCreate) => void }) {
  const org = useOrgId();
  const [plans, setPlans] = useState<ToolPlanRead[]>([]);
  const [projects, setProjects] = useState<ProjectRead[]>([]);
  const [project, setProject] = useState("");
  const [prompt, setPrompt] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const run = async (action: () => Promise<void>) => { setBusy(true); setMessage(""); try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); } };
  const load = async (pageOffset = 0) => {
    const page = await get<{ items: ToolPlanRead[]; total: number }>(`/v1/tool-plans?limit=30&offset=${pageOffset}`);
    setPlans(page.items); setTotal(page.total); setOffset(pageOffset);
    setProjects((await fetchOrgProjects(org)).items);
  };
  return <details className="workflow-author" onToggle={e => { if (e.currentTarget.open) void run(() => load()); }}>
    <summary>Saved plans, templates and prompt suggestions</summary>
    <p>A plan coordinates published tools. Reuse a saved plan as an editable template, or describe steps separated by “then”. Suggestions match tool metadata; they do not execute anything or invent missing parameter values.</p>
    <Button disabled={busy} onClick={() => void run(() => load())}>Refresh saved plans</Button>
    {plans.map(p => <div key={p.id}><Button onClick={() => onSelect(p.id)}>{p.name} · {p.status}</Button><Button disabled={busy} onClick={() => void run(async () => {
      const full = await fetchToolPlan(p.id);
      onDraft({ name: `${full.name} copy`, steps: full.steps.map(s => ({ ...s })), budget: full.budget });
      setMessage("Template copied to the editor. Saving creates a separate plan.");
    })}>Use as template</Button></div>)}
    <Button disabled={busy || offset === 0} onClick={() => void run(() => load(Math.max(0, offset - 30)))}>Previous plans</Button>
    <Button disabled={busy || offset + 30 >= total} onClick={() => void run(() => load(offset + 30))}>Next plans</Button>
    <Field label="Recommendation project"><select value={project} onChange={e => setProject(e.target.value)}><option value="">Select project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
    <Field label="Describe the plan"><textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Monthly payments then overdue accounts" /></Field>
    <Button disabled={busy || !project || prompt.trim().length < 3} onClick={() => void run(async () => {
      const draft = await postJson<ToolPlanCreate>("/v1/tool-plans/recommend", { project_id: project, prompt });
      onDraft(draft); setMessage("Suggested steps loaded. Check tools, parameters and dependencies before saving.");
    })}>Suggest plan from prompt</Button>
    {message ? <p role="status">{message}</p> : null}
  </details>;
}
