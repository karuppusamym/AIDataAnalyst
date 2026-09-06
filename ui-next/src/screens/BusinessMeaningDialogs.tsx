import { useCallback, useState } from "react";

import { fetchCatalogRows } from "../lib/api";
import {
  createGlossaryTerm,
  linkTermToTable,
  submitGlossaryTermVersion,
  type GlossaryTermRead,
} from "../lib/_api_append";
import { Button, Dialog, Field, FormErrors } from "../components/primitives";
import "./BusinessMeaningScreen.css";

/* ---------------------------------------------------------------------------
   The two authoring dialogs of the Business meaning screen (review
   2026-09-05, R06).

   Extracted because that screen had grown to ~990 lines holding four
   independent things at once -- an annotation list, a detail pane, a glossary
   tab and these two full authoring forms -- and because both dialogs changed
   in this pass: each was a hand-rolled `role="dialog" aria-modal="true"` div
   that declared a modal without trapping focus, restoring it, honouring
   Escape or making the page behind inert. They now sit on the shared `Dialog`
   primitive, which implements all four (F21).

   They stay in `screens/` rather than `components/` on purpose: both are
   specific to this screen's glossary workflow -- a term that is created
   already submitted for review, and a link that is proposed rather than
   applied -- and nothing else in the app authors either.
--------------------------------------------------------------------------- */

export function CreateTermDialog({
  organizationId,
  businessNodeId,
  onClose,
  onCreated,
}: {
  organizationId: string;
  businessNodeId: string | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [displayName, setDisplayName] = useState("");
  const [termKey, setTermKey] = useState("");
  const [definition, setDefinition] = useState("");
  const [synonymsRaw, setSynonymsRaw] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const created = await createGlossaryTerm(organizationId, {
        term_key: termKey.trim(),
        display_name: displayName.trim(),
        definition: definition.trim(),
        business_node_id: businessNodeId ?? undefined,
        synonyms: synonymsRaw
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      // Auto-submit for review -- ADR-0001: models propose, humans decide;
      // a term that stays in DRAFT forever helps nobody, so the create
      // form leaves it in REVIEW_REQUIRED so a reviewer can see it.
      try {
        await submitGlossaryTermVersion(created.id);
      } catch {
        /* If the auto-submit fails, the term still exists in DRAFT and
         *  the reviewer can submit it manually from the term row. */
      }
      onCreated();
    } catch (e) {
      setError(e as Error);
    } finally {
      setSubmitting(false);
    }
  }, [
    organizationId,
    businessNodeId,
    termKey,
    displayName,
    definition,
    synonymsRaw,
    onCreated,
  ]);

  const canSubmit =
    !submitting &&
    displayName.trim().length >= 2 &&
    termKey.trim().length >= 2 &&
    definition.trim().length >= 10;

  /* This was a hand-rolled `role="dialog" aria-modal="true"` div: it looked
     like a modal to a screen reader and behaved like nothing to a keyboard --
     Tab walked straight out into the page behind it, Escape did nothing, and
     closing it left focus at the top of the document. `Dialog` is the same
     markup with the behaviour those attributes were claiming (review
     2026-09-05, F21). */
  return (
    <Dialog
      title="Create glossary term"
      onClose={onClose}
      className="bmdialog"
      footer={
        <>
          <Button onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} disabled={!canSubmit}>
            {submitting ? "Creating..." : "Create and submit for review"}
          </Button>
        </>
      }
    >
      <div className="bmdialog__body">
          <Field label="Display name">
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Monthly Recurring Revenue"
            />
          </Field>
          <Field label="Term key">
            <input
              value={termKey}
              onChange={(e) => setTermKey(e.target.value.toLowerCase())}
              placeholder="mrr"
            />
          </Field>
          <Field label="Definition">
            <textarea
              value={definition}
              onChange={(e) => setDefinition(e.target.value)}
              rows={4}
              placeholder="Recurring revenue normalized to a monthly cadence, excluding one-time fees."
            />
          </Field>
          <Field label="Synonyms (comma-separated, optional)">
            <input
              value={synonymsRaw}
              onChange={(e) => setSynonymsRaw(e.target.value)}
              placeholder="recurring revenue, monthly rev"
            />
          </Field>
          <FormErrors error={error} title="The term could not be created" />
        </div>
    </Dialog>
  );
}

export function LinkTermDialog({
  organizationId,
  term,
  onClose,
  onLinked,
}: {
  organizationId: string;
  term: GlossaryTermRead;
  onClose: () => void;
  onLinked: () => void;
}) {
  const [q, setQ] = useState("");
  const [candidates, setCandidates] = useState<{ id: string; name: string; schema_name: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<{ id: string; name: string } | null>(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const search = useCallback(async () => {
    if (!q.trim()) return;
    setSearching(true);
    setError(null);
    try {
      const page = await fetchCatalogRows({
        organizationId,
        q: q.trim(),
        objectType: "TABLE",
        limit: 25,
      });
      setCandidates(page.items.map((r) => ({ id: r.id, name: r.name, schema_name: r.schema_name })));
    } catch (e) {
      setError(e as Error);
    } finally {
      setSearching(false);
    }
  }, [organizationId, q]);

  const submit = useCallback(async () => {
    if (!selected) return;
    setSubmitting(true);
    setError(null);
    try {
      await linkTermToTable(organizationId, selected.id, term.term_id, {
        reason: reason.trim() || undefined,
      });
      onLinked();
    } catch (e) {
      setError(e as Error);
    } finally {
      setSubmitting(false);
    }
  }, [organizationId, selected, term.term_id, reason, onLinked]);

  return (
    <Dialog
      title={`Link “${term.display_name}” to an asset`}
      onClose={onClose}
      className="bmdialog"
      footer={
        <>
          <Button onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            disabled={submitting || !selected}
          >
            {submitting ? "Linking..." : `Link to ${selected?.name ?? "..."}`}
          </Button>
        </>
      }
    >
      <div className="bmdialog__body">
          <Field label="Search asset by name">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void search();
                }
              }}
              placeholder="orders_raw"
            />
          </Field>
          <Button onClick={() => void search()} disabled={searching || !q.trim()}>
            {searching ? "Searching..." : "Search"}
          </Button>
          {candidates.length > 0 ? (
            <ul className="bmdialog__results" role="listbox" aria-label="Matching tables">
              {candidates.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected?.id === c.id}
                    className={`bmdialog__opt${selected?.id === c.id ? " bmdialog__opt--sel" : ""}`}
                    onClick={() => setSelected({ id: c.id, name: `${c.schema_name}.${c.name}` })}
                  >
                    {c.schema_name}.{c.name}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {selected ? (
            <Field label="Reason (optional)">
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="why this term applies to this asset"
              />
            </Field>
          ) : null}
          <FormErrors error={error} title="The link could not be created" />
        </div>
    </Dialog>
  );
}
