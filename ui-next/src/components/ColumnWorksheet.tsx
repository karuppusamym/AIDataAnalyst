import { useState } from "react";
import { ApiError } from "../lib/api";
import { saveColumnWorksheet, type ColumnDocumentationRead, type ModelImportBatchRead } from "../lib/api/columnDocumentation";
import { Button, Dialog, Field } from "./primitives";
import { WorkbookImport } from "./WorkbookImport";
import "./ColumnWorksheet.css";

export function ColumnWorksheet({ tableId, columns, onClose }: {
  tableId: string; columns: ColumnDocumentationRead[]; onClose: () => void;
}) {
  // Pin the read versions for this editing session. A later approval must not
  // change our expected versions and turn a stale edit into a blind overwrite.
  const [base] = useState(columns);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batch, setBatch] = useState<ModelImportBatchRead | null>(null);
  const [discard, setDiscard] = useState(false);
  const changed = base.filter(column => edits[column.column_id] !== undefined && edits[column.column_id] !== (column.business_description ?? ""));
  const matches = base.filter(column => `${column.name} ${column.physical_type}`.toLowerCase().includes(query.trim().toLowerCase()));
  const save = async () => {
    if (busy || changed.length === 0) return;
    if (changed.some(column => !edits[column.column_id]!.trim())) {
      setError("Use the withdrawal action to clear an approved description."); return;
    }
    setBusy(true); setError(null);
    try {
      setBatch(await saveColumnWorksheet(tableId, changed.map(column => ({column_id: column.column_id, description: edits[column.column_id]!, expected_version: column.description_version}))));
    } catch (e) { setError(e instanceof ApiError ? e.detail : (e as Error).message); }
    finally { setBusy(false); }
  };
  return <Dialog title="Column worksheet" className="column-worksheet" onClose={() => { if (busy) return; if (!batch && changed.length) setDiscard(true); else onClose(); }}>
    <p>Edit business descriptions here. Save creates a draft and loads its review preview; it does not publish. Source names, types and comments are read-only.</p>
    {discard ? <div role="alert">Discard unsaved edits? <Button onClick={onClose}>Discard edits</Button><Button onClick={() => setDiscard(false)}>Keep editing</Button></div> : null}
    {batch ? <WorkbookImport datasourceId={batch.datasource_id} initialBatch={batch} /> : <>
      <Field label="Find worksheet columns"><input value={query} onChange={e => {setQuery(e.target.value); setPage(0);}} /></Field>
      <div className="column-worksheet__table"><table><thead><tr><th>Column / type</th><th>Source comment</th><th>Business description</th></tr></thead><tbody>
        {matches.slice(page * 50, (page + 1) * 50).map(column => <tr key={column.column_id}>
          <td>{column.name}<br/><small>{column.physical_type}</small></td><td>{column.source_description ?? "—"}</td>
          <td><textarea aria-label={`Description for ${column.name}`} rows={3} maxLength={16000} disabled={busy} value={edits[column.column_id] ?? column.business_description ?? ""} onChange={e => setEdits(current => ({...current, [column.column_id]: e.target.value}))} /></td>
        </tr>)}
      </tbody></table></div>
      {!matches.length ? <p>No columns match your search.</p> : null}
      <nav aria-label="Worksheet pages"><Button disabled={page === 0} onClick={() => setPage(page - 1)}>Previous columns</Button><span> {matches.length} matching columns </span><Button disabled={(page + 1) * 50 >= matches.length} onClick={() => setPage(page + 1)}>Next columns</Button></nav>
      <p>{changed.length} unsaved changes</p><Button variant="primary" disabled={busy || !changed.length} onClick={() => void save()}>{busy ? "Saving…" : "Save and preview changes"}</Button>
    </>}
    {error ? <p role="alert">{error}</p> : null}
  </Dialog>;
}
