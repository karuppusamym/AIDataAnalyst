import { useEffect, useState } from "react";
import { createOntologyVersion, listOntologyVersions, submitOntologyVersion, type OntologyVersionRead } from "../lib/api/ontology";
import { Button, Dialog, Field } from "./primitives";
import { ApiError } from "../lib/api";

const template = {
  name: "Customer ontology", owner: "data-steward", provenance: "Describe the approved source of these definitions",
  lifecycle: "ACTIVE", concepts: [{key: "customer", name: "Customer", description: "Define the business concept", aliases: [], deprecated: false}],
  relations: [], mappings: [],
};
export function OntologyManager({organizationId, onClose}: {organizationId: string; onClose: () => void}) {
  const [rows, setRows] = useState<OntologyVersionRead[]>([]);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [key, setKey] = useState("customer");
  const [base, setBase] = useState(0);
  const [definition, setDefinition] = useState(JSON.stringify(template, null, 2));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setRows([]); setError(null);
    listOntologyVersions(organizationId, offset, controller.signal).then(value => {if (!controller.signal.aborted) setRows(value);})
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof ApiError ? e.detail : (e as Error).message); });
    return () => controller.abort();
  }, [organizationId, offset, refresh]);
  const run = async (action: () => Promise<OntologyVersionRead>, message: string) => {
    setBusy(true); setError(null); setNotice(null);
    try { await action(); setNotice(message); setRefresh(value => value + 1); }
    catch (e) { setError(e instanceof ApiError ? e.detail : (e as Error).message); }
    finally { setBusy(false); }
  };
  return <Dialog title="Governed ontology" onClose={() => { if (!busy) onClose(); }}>
    <p>Versioned concepts, typed relations and TABLE/COLUMN mappings. Drafts need independent review. These definitions do not grant data access or enable Neo4j reasoning.</p>
    <Field label="Ontology key"><input value={key} disabled={busy} onChange={e => setKey(e.target.value)} /></Field>
    <Field label="Published base version"><input type="number" min={0} value={base} disabled={busy} onChange={e => setBase(Number(e.target.value))} /></Field>
    <Field label="Ontology definition JSON"><textarea rows={14} value={definition} disabled={busy} onChange={e => setDefinition(e.target.value)} /></Field>
    <details><summary>Definition format and validation</summary><p>Concepts: key, name, description, aliases, deprecated. Relations: key, source and target concept keys, description, cardinality (ONE_TO_ONE, ONE_TO_MANY, MANY_TO_ONE, MANY_TO_MANY), deprecated. Mappings: concept, subject_type (TABLE or COLUMN), subject_id (catalog UUID). Keys and aliases must be unique; references and access are checked. Deprecate published keys instead of deleting them. Cardinalities describe intended semantics, not a validation of source records.</p></details>
    <Button disabled={busy} onClick={() => void run(() => createOntologyVersion(organizationId, {ontology_key: key, base_version: base, definition: JSON.parse(definition) as Record<string, unknown>}), "Ontology draft saved; nothing published.")}>Save ontology draft</Button>
    {error ? <div role="alert">{error}<Button onClick={() => setRefresh(value => value + 1)}>Retry ontology list</Button></div> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <h3>Version history</h3>
    {rows.map(row => <article key={row.id}>
      <p>{row.ontology_key} · v{row.version} · {row.status} {row.published_version === row.version ? "· current published version" : ""}</p>
      <Button disabled={busy} onClick={() => {setKey(row.ontology_key); setBase(row.status === "APPROVED" ? row.version : row.base_version); setDefinition(JSON.stringify(row.definition, null, 2));}}>Use as new draft</Button>
      {row.status === "DRAFT" ? <Button disabled={busy} onClick={() => void run(() => submitOntologyVersion(row.id), "Submitted to the review queue; an independent reviewer must decide.")}>Submit ontology v{row.version} for review</Button> : null}
      {row.governance_review_id ? <a href={`?review=${encodeURIComponent(row.governance_review_id)}#/governance`}>Open ontology review</a> : null}
    </article>)}
    <nav aria-label="Ontology history pages"><Button disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous versions</Button><Button disabled={busy || rows.length < 50} onClick={() => setOffset(offset + 50)}>Next versions</Button></nav>
  </Dialog>;
}
