import { navigateTo } from "../lib/navigate";
import { useState } from "react";
import { fetchOrgDatasources, fetchTablesLegacy, get, postJson, putJson } from "../lib/api";
import type { DataSourceRead, SemanticModelVersionRead, SemanticMetricVersionRead } from "../lib/types";
import type { MetadataTableRead } from "../lib/ui-types";
import { Button, Field } from "./primitives";

type Suggestion = { id: string; status: string; proposed_name: string; proposed_description: string; proposed_slug: string; proposed_aggregation: string; proposed_grain: string; table_id: string; measure_column_id: string };
export function SemanticAuthor({ org, projectId, models, onSaved }: {
  org: string; projectId: string; models: SemanticModelVersionRead[]; onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  const [modelId, setModelId] = useState("");
  const [tables, setTables] = useState<MetadataTableRead[]>([]);
  const [columns, setColumns] = useState<{ id: string; name: string }[]>([]);
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<DataSourceRead[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [editableMetrics, setEditableMetrics] = useState<SemanticMetricVersionRead[]>([]);
  const [editing, setEditing] = useState<SemanticMetricVersionRead | null>(null);
  const [metric, setMetric] = useState({ slug: "", name: "", description: "", aggregation: "COUNT", grain: "", source_table_id: "", measure_column_id: "", default_time_column_id: "", allowed_dimension_column_ids: [] as string[] });
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (action: () => Promise<void>) => { setBusy(true); setMessage(""); try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); } };
  return <details className="workflow-author" onToggle={e => { if (e.currentTarget.open) void run(async () => setSources((await fetchOrgDatasources(org)).items.filter(s => s.project_id === projectId))); }}><summary>Create definitions and review metric suggestions</summary>
    <p>Business annotations describe meaning. Semantic models define reusable measures and aggregations. Create a draft, add metrics, then submit the model for independent review.</p>
    <Field label="Model name"><input value={name} onChange={e => setName(e.target.value)} /></Field>
    <Field label="Change summary"><input value={summary} onChange={e => setSummary(e.target.value)} /></Field>
    <Button disabled={busy || name.length < 2 || summary.length < 3} onClick={() => void run(async () => {
      const model = await postJson<SemanticModelVersionRead>(`/v1/projects/${projectId}/semantic-model-versions`, { name, change_summary: summary });
      setModelId(model.id); onSaved(); setMessage("Model draft created. Add metrics before submitting.");
    })}>Create model draft</Button>
    <Field label="Model to author"><select value={modelId} onChange={e => { setModelId(e.target.value); setEditing(null); setEditableMetrics([]); }}><option value="">Choose model</option>{models.map(m => <option key={m.id} value={m.id}>{m.name} v{m.version} · {m.status}</option>)}</select></Field>
    <Button disabled={busy || !modelId || name.length < 2 || summary.length < 3} onClick={() => void run(async () => {
      const model = await postJson<SemanticModelVersionRead>(`/v1/semantic-model-versions/${modelId}/clone`, { name, change_summary: summary });
      setModelId(model.id); onSaved(); setMessage("Copied to a new draft revision.");
    })}>Clone model as new revision</Button>
    <Button disabled={busy || !modelId} onClick={() => void run(async () => setEditableMetrics((await get<{ items: SemanticMetricVersionRead[] }>(`/v1/semantic-model-versions/${modelId}/metrics?limit=200`)).items))}>Load metrics to edit</Button>
    <Field label="Existing metric"><select value={editing?.id ?? ""} onChange={e => {
      const m = editableMetrics.find(v => v.id === e.target.value); if (!m) { setEditing(null); return; }
      setEditing(m); setMetric({ slug: m.metric_slug, name: m.metric_name, description: m.description, aggregation: m.aggregation, grain: m.grain, source_table_id: m.source_table_id, measure_column_id: m.measure_column_id ?? "", default_time_column_id: m.default_time_column_id ?? "", allowed_dimension_column_ids: m.allowed_dimension_column_ids });
      void run(async () => setColumns((await get<{ items: {id: string; name: string}[] }>(`/v1/tables/${m.source_table_id}/columns?limit=500`)).items));
    }}><option value="">New metric</option>{editableMetrics.map(m => <option key={m.id} value={m.id}>{m.metric_name}</option>)}</select></Field>
    <Field label="Metric datasource"><select value={sourceId} onChange={e => { setSourceId(e.target.value); setTables([]); }}><option value="">Select datasource</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></Field>
    <Field label="Search source tables"><input value={query} onChange={e => setQuery(e.target.value)} /></Field>
    <Button disabled={busy} onClick={() => void run(async () => {
      if (!sourceId) throw new Error("Choose a metric datasource first");
      const page = await fetchTablesLegacy(sourceId, { q: query, limit: 200 });
      setTables(page.items);
      setMessage("Showing up to 200 matches. Refine the search if your table is absent.");
    })}>Find metric source tables</Button>
    <Field label="Metric source table"><select value={metric.source_table_id} onChange={e => {
      const id = e.target.value; setMetric({ ...metric, source_table_id: id, measure_column_id: "", default_time_column_id: "", allowed_dimension_column_ids: [] }); setColumns([]);
      if (id) void run(async () => { setColumns((await get<{ items: { id: string; name: string }[] }>(`/v1/tables/${id}/columns?limit=500`)).items); });
    }}><option value="">Select table</option>{metric.source_table_id && !tables.some(t => t.id === metric.source_table_id) ? <option value={metric.source_table_id}>Current source ({metric.source_table_id.slice(0, 8)})</option> : null}{tables.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></Field>
    {(["slug", "name", "description", "grain"] as const).map(key => <Field key={key} label={`Metric ${key}`}><input value={metric[key]} onChange={e => setMetric({ ...metric, [key]: e.target.value })} /></Field>)}
    <Field label="Aggregation"><select value={metric.aggregation} onChange={e => setMetric({ ...metric, aggregation: e.target.value })}>{["COUNT", "SUM", "AVG", "MIN", "MAX"].map(a => <option key={a}>{a}</option>)}</select></Field>
    <Field label="Measure column"><select value={metric.measure_column_id} onChange={e => setMetric({ ...metric, measure_column_id: e.target.value })}><option value="">Count rows / choose measure</option>{columns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></Field>
    <Field label="Time column"><select value={metric.default_time_column_id} onChange={e => setMetric({ ...metric, default_time_column_id: e.target.value })}><option value="">None</option>{columns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></Field>
    <Field label="Allowed dimensions"><select multiple value={metric.allowed_dimension_column_ids} onChange={e => setMetric({ ...metric, allowed_dimension_column_ids: [...e.target.selectedOptions].map(o => o.value) })}>{columns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></Field>
    <Button disabled={busy || !modelId || !metric.source_table_id || !metric.slug || !metric.name} onClick={() => void run(async () => {
      const definition = { ...metric, measure_column_id: metric.measure_column_id || null, default_time_column_id: metric.default_time_column_id || null };
      if (editing) await putJson(`/v1/semantic-metric-versions/${editing.id}`, { definition, expected_fingerprint: editing.fingerprint });
      else await postJson(`/v1/semantic-model-versions/${modelId}/metrics`, definition);
      setEditing(null); setEditableMetrics([]);
      onSaved(); setMessage("Metric added to draft. Review the model before publication.");
    })}>{editing ? "Save metric edits" : "Add metric to draft"}</Button>
    <Button disabled={busy || !modelId} onClick={() => void run(async () => {
      await postJson(`/v1/semantic-model-versions/${modelId}/submit`, {}); onSaved(); setMessage("Model submitted for independent review.");
    })}>Submit model for review</Button>
    <hr />
    <Button disabled={busy} onClick={() => void run(async () => {
      const page = await postJson<{ items: Suggestion[] }>(`/v1/organizations/${org}/metric-suggestions/generate`, { limit: 100 });
      setSuggestions(page.items.filter(s => (s as Suggestion & { project_id: string }).project_id === projectId));
      setMessage("Suggestions use approved business annotations. Review or edit before publishing.");
    })}>Generate metric suggestions</Button>
    <Button disabled={busy} onClick={() => void run(async () => {
      const page = await get<{ items: (Suggestion & { project_id: string })[] }>(`/v1/organizations/${org}/metric-suggestions?limit=100`);
      setSuggestions(page.items.filter(s => s.project_id === projectId));
    })}>Load existing suggestions</Button>
    {suggestions.map(s => <article key={s.id}><strong>{s.proposed_name} · {s.status}</strong><p>{s.proposed_description}</p>
      <Button disabled={busy} onClick={() => {
        setMetric({ slug: s.proposed_slug, name: s.proposed_name, description: s.proposed_description, aggregation: s.proposed_aggregation, grain: s.proposed_grain, source_table_id: s.table_id, measure_column_id: s.measure_column_id, default_time_column_id: "", allowed_dimension_column_ids: [] });
        setEditing(null);
        void run(async () => { setColumns((await get<{ items: { id: string; name: string }[] }>(`/v1/tables/${s.table_id}/columns?limit=500`)).items); });
        setMessage("Suggestion copied into the metric editor. Choose a draft model, edit and add it.");
      }}>Edit suggestion as draft metric</Button>
      {s.status === "DRAFT" ? <Button disabled={busy} onClick={() => void run(async () => {
        await postJson(`/v1/metric-suggestions/${s.id}/submit`, {}); setSuggestions(prev => prev.map(x => x.id === s.id ? { ...x, status: "PENDING_APPROVAL" } : x)); setMessage("Suggestion submitted for review.");
      })}>Submit suggestion for review</Button> : null}
    </article>)}
    {message ? <p role="status">{message}</p> : null}
  </details>;
}

type BusinessProposal = { id: string; status: string; table_name: string; governance_review_id: string; promoted_tool_version_id: string | null; payload: { business_description?: string; tool_blueprint?: { recommended: boolean } } };
export function BusinessGeneration({ org, datasourceId }: { org: string; datasourceId?: string | null }) {
  const [sources, setSources] = useState<DataSourceRead[]>([]);
  const [selected, setSelected] = useState(datasourceId ?? "");
  const source = datasourceId || selected;
  const [model, setModel] = useState(false);
  const [proposals, setProposals] = useState<BusinessProposal[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const run = async (action: () => Promise<void>) => { setBusy(true); setMessage(""); try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); } };
  const load = async () => { setProposals((await get<{ items: BusinessProposal[] }>(`/v1/datasources/${source}/metadata-enrichment-proposals?limit=100`)).items); };
  return <details className="workflow-author" onToggle={e => { if (e.currentTarget.open && !datasourceId) void run(async () => setSources((await fetchOrgDatasources(org)).items)); }}>
    <summary>Generate business meaning and reusable tool blueprints</summary>
    <p>Completed scans automatically propose business meaning. Generate again on demand, review the evidence, then promote an approved blueprint into a draft tool. Publication needs a separate tool review.</p>
    {!datasourceId ? <Field label="Inference datasource"><select value={selected} onChange={e => { setSelected(e.target.value); setProposals([]); }}><option value="">Select datasource</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></Field> : null}
    <label><input type="checkbox" checked={model} onChange={e => setModel(e.target.checked)} />Use approved model assistance (requires configured model route)</label>
    <Button disabled={busy || !source} onClick={() => void run(async () => {
      const result = await postJson<{ proposal_count: number; engine_mode: string }>(`/v1/datasources/${source}/semantic-inference-runs`, { use_model: model, max_tables: 100 });
      await load(); setMessage(`${result.proposal_count} proposals generated (${result.engine_mode}). Review them before they become authoritative. This run covers up to 100 tables.`);
    })}>Generate business suggestions</Button>
    <Button disabled={busy || !source} onClick={() => void run(load)}>Load business proposals</Button>
    {proposals.map(p => <article key={p.id}><strong>{p.table_name} · {p.status}</strong><p>{p.payload.business_description}</p>
      <Button onClick={() => { navigateTo("governance", { review: p.governance_review_id, status: "PENDING" }); }}>Review proposal evidence</Button>
      {p.status === "APPROVED" && p.payload.tool_blueprint?.recommended && !p.promoted_tool_version_id ? <Button disabled={busy} onClick={() => void run(async () => {
        await postJson(`/v1/metadata-enrichment-proposals/${p.id}/promote-tool`, {}); await load(); setMessage("Draft tool created. Open Tool registry to inspect and submit it for review.");
      })}>Create draft tool from blueprint</Button> : null}
    </article>)}
    {message ? <p role="status">{message}</p> : null}
  </details>;
}
