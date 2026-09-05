import { useState } from "react";
import { postJson, putJson } from "../lib/api";
import type { AssetDescriptionDraftRead } from "../lib/types";
import { Button, Field } from "./primitives";

export function DescriptionEditor({ tableId, currentText = "", draft, onSaved }: {
  tableId: string; currentText?: string; draft?: AssetDescriptionDraftRead; onSaved?: () => void;
}) {
  const original = draft?.drafted_text ?? currentText;
  const [text, setText] = useState(original);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [versionId, setVersionId] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const save = async () => {
    setBusy(true); setMessage("");
    try {
      if (draft) {
        await putJson(`/v1/asset-description-drafts/${draft.id}`, { drafted_text: text, expected_text: original });
        setMessage("Edits saved. Submit the draft for independent review when ready.");
      } else {
        const version = await postJson<{ id: string }>(`/v1/metadata/tables/${tableId}/documentation-versions`, { readme: text, aliases: [] });
        setVersionId(version.id);
        setMessage("Revision saved. Submit it for independent review.");
      }
      onSaved?.();
    } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  };
  const submit = async () => {
    setBusy(true);
    try {
      await postJson(`/v1/asset-documentation-versions/${versionId}/submit`, {});
      setSubmitted(true);
      setMessage("Submitted for independent review. The approved description stays current until approval.");
    } catch (e) { setMessage((e as Error).message); }
    finally { setBusy(false); }
  };
  return <details className="workflow-author">
    <summary>{draft ? "Edit generated draft" : "Write or revise description"}</summary>
    <p>{draft ? "Generated from metadata. Human edits retain the source evidence and need review." : "Write a new documentation revision. Publication requires independent review."}</p>
    {original ? <details><summary>Previous text</summary><pre style={{ whiteSpace: "pre-wrap" }}>{original}</pre></details> : null}
    <Field label="Description"><textarea rows={7} value={text} disabled={busy || !!versionId} onChange={e => setText(e.target.value)} /></Field>
    <Button disabled={busy || text.trim().length < 10 || !!versionId} onClick={() => void save()}>{busy ? "Saving…" : "Save description revision"}</Button>
    {versionId ? <Button disabled={busy || submitted} onClick={() => void submit()}>Submit revision for review</Button> : null}
    {message ? <p role="status">{message}</p> : null}
  </details>;
}
