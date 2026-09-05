import { useState } from "react";
import { createToolVersion, postJson } from "../lib/api";
import type { GovernedToolVersionCreate } from "../lib/types";
import { Button, Field } from "./primitives";

export function SaveAnalysisTool({ runId }: { runId: string }) {
  const [draft, setDraft] = useState<GovernedToolVersionCreate | null>(null);
  const [project, setProject] = useState("");
  const [parameters, setParameters] = useState("[]");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const run = async (action: () => Promise<void>) => { setBusy(true); setMessage(""); try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); } };
  return <details className="workflow-author"><summary>Save analysis as a reusable tool</summary>
    <p>Prepare a draft from this successful analysis. Stored SQL has redacted values: supply parameter types and names before saving. The tool must pass validation and independent review before publication.</p>
    <Button disabled={busy} onClick={() => void run(async () => {
      const result = await postJson<{ project_id: string; definition: GovernedToolVersionCreate }>(`/v1/agent-runs/${runId}/tool-blueprint`, {});
      setProject(result.project_id); setDraft(result.definition); setParameters(JSON.stringify(result.definition.parameters, null, 2)); setReviewed(false); setSaved(null);
    })}>Prepare draft tool</Button>
    {draft ? <>
      {(["slug", "name", "description", "sql_template"] as const).map(key => <Field key={key} label={`Tool ${key.replace("_", " ")}`}><textarea value={draft[key]} disabled={!!saved} onChange={e => { setDraft({ ...draft, [key]: e.target.value }); setReviewed(false); }} /></Field>)}
      <Field label="Parameter definitions (JSON)"><textarea rows={7} value={parameters} disabled={!!saved} onChange={e => { setParameters(e.target.value); setReviewed(false); }} /></Field>
      <label><input type="checkbox" checked={reviewed} onChange={e => setReviewed(e.target.checked)} />I checked the SQL and parameter types; no source values are used as defaults.</label>
      <Button disabled={busy || !reviewed || !!saved} onClick={() => void run(async () => {
        const definitions: unknown = JSON.parse(parameters);
        if (!Array.isArray(definitions)) throw new Error("Parameter definitions must be an array");
        const tool = await createToolVersion(project, { ...draft, parameters: definitions });
        setSaved(tool.id); setMessage("Tool draft saved. Open the registry to submit it for independent review.");
      })}>Save as draft tool</Button>
      {saved ? <Button onClick={() => { location.hash = `/tool-registry?project=${project}&tool=${saved}`; }}>Open saved tool</Button> : null}
    </> : null}
    {message ? <p role="status">{message}</p> : null}
  </details>;
}
