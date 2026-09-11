import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import {
  editColumnDescriptionDraft,
  generateColumnDescriptionDrafts,
  listTableColumnDescriptionDrafts,
  submitColumnDescriptionDraft,
  submitTableColumnDescriptionDrafts,
} from "../lib/api/columnDescriptionDrafts";
import { useOrgId } from "../lib/org";
import type {
  ColumnDescriptionDraftGenerateResult,
  ColumnDescriptionDraftRead,
} from "../lib/types";
import { Button, Pill, type Tone } from "./primitives";
import "./ColumnDescriptionDrafts.css";

/* ---------------------------------------------------------------------------
   Column description drafts, where a steward looks at columns.

   The drafts are composed on the server from catalog evidence only -- dbt
   column docs, source comments, keys, approved relationships -- with no model
   call, and published only when someone other than the submitter (and anyone
   who edited it) approves each one in the review queue. This section generates
   them, lets a steward fix the wording, and submits them; it cannot publish.

   Two things it says plainly rather than leaving a steward to discover:

   - A thin draft is shown, not hidden, but it cannot be submitted. Its score
     measures catalog evidence, not prose, so editing the wording does not help
     -- the way to describe such a column is to write it, in the source model
     workbook. Hiding thin drafts would leave a steward wondering why half the
     columns never got one.
   - Editing makes you an author. The server refuses an editor as approver,
     and the item says so before the steward finds out in the queue.
--------------------------------------------------------------------------- */

const STATUS: Record<string, { label: string; tone: Tone }> = {
  DRAFT: { label: "Draft", tone: "info" },
  PENDING_APPROVAL: { label: "In review", tone: "warn" },
  APPROVED: { label: "Published", tone: "ok" },
  REJECTED: { label: "Rejected", tone: "bad" },
  SUPERSEDED: { label: "Superseded", tone: "mute" },
};

const OPEN_STATUSES = new Set(["DRAFT", "PENDING_APPROVAL"]);

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/** What a generation call did, including what it deliberately did not do. */
export function describeGeneration(result: ColumnDescriptionDraftGenerateResult): string {
  if (
    result.tables_skipped > 0 &&
    result.created === 0 &&
    result.skipped_open === 0 &&
    result.skipped_described === 0
  ) {
    return "This table could not be drafted: it is no longer active, or you cannot read its columns.";
  }
  const parts = [
    result.created === 0 ? "No new drafts." : `Drafted ${plural(result.created, "column")}.`,
  ];
  if (result.skipped_described > 0) {
    parts.push(`${result.skipped_described} already described or deliberately retired.`);
  }
  if (result.skipped_open > 0) {
    parts.push(`${result.skipped_open} already have an open draft.`);
  }
  if (result.skipped_duplicate_rejected > 0) {
    parts.push(
      `${result.skipped_duplicate_rejected} would repeat a draft a reviewer already rejected.`,
    );
  }
  if (result.below_review_threshold > 0) {
    parts.push(
      `${result.below_review_threshold} rest on too little catalog evidence to submit; ` +
        "write those in the source model workbook instead.",
    );
  }
  return parts.join(" ");
}

function editorsOf(draft: ColumnDescriptionDraftRead): string[] {
  const editors = draft.evidence?.editors;
  return Array.isArray(editors) ? editors.map(String) : [];
}

function DraftItem({
  draft,
  busy,
  onSave,
  onSubmit,
}: {
  draft: ColumnDescriptionDraftRead;
  busy: boolean;
  onSave: (draft: ColumnDescriptionDraftRead, text: string) => Promise<boolean>;
  onSubmit: (draft: ColumnDescriptionDraftRead) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(draft.drafted_text);
  useEffect(() => setText(draft.drafted_text), [draft.drafted_text]);
  const status = STATUS[draft.status] ?? { label: draft.status, tone: "mute" as Tone };
  const editors = editorsOf(draft);
  const textId = `cdd-text-${draft.id}`;

  const save = async () => {
    if (await onSave(draft, text)) setEditing(false);
  };

  return (
    <li className="cdd__item">
      <div className="cdd__row">
        <span className="cdd__col">{draft.column_name}</span>
        <Pill tone={status.tone}>{status.label}</Pill>
        <span
          className="cdd__score"
          title="How much catalog evidence this draft rests on. It orders review; it never replaces it."
        >
          {`evidence ${Math.round(draft.overall_score * 100)}%`}
        </span>
      </div>

      {editing ? (
        <div className="cdd__edit">
          <label className="cdd__label" htmlFor={textId}>
            {`Draft text for ${draft.column_name}`}
          </label>
          <textarea
            id={textId}
            rows={4}
            value={text}
            disabled={busy}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="cdd__itemactions">
            <Button
              variant="primary"
              disabled={busy || text.trim().length < 10 || text === draft.drafted_text}
              onClick={() => void save()}
            >
              Save draft
            </Button>
            <Button
              disabled={busy}
              onClick={() => {
                setText(draft.drafted_text);
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <p className="cdd__text">{draft.drafted_text}</p>
      )}

      {draft.status === "DRAFT" && !editing ? (
        <div className="cdd__itemactions">
          <Button disabled={busy} onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button
            disabled={busy || !draft.reviewable}
            onClick={() => onSubmit(draft)}
            title={
              draft.reviewable
                ? "Send this draft to the review queue. Publishing needs someone else's approval."
                : "Too little catalog evidence to submit this draft."
            }
          >
            Submit for review
          </Button>
        </div>
      ) : null}

      {draft.status === "DRAFT" && !draft.reviewable ? (
        <p className="cdd__warn">
          Too little catalog evidence to submit. Rewording does not change that: the score measures
          the evidence, not the prose. Write this column&apos;s description in the source model
          workbook instead.
        </p>
      ) : null}
      {editors.length > 0 ? (
        <p className="cdd__meta">
          {`Edited by ${editors.join(", ")}. An editor cannot approve their own edits.`}
        </p>
      ) : null}
      {draft.status === "PENDING_APPROVAL" ? (
        <p className="cdd__meta">Waiting in the review queue for someone else&apos;s decision.</p>
      ) : null}
    </li>
  );
}

export function ColumnDescriptionDrafts({ tableId }: { tableId: string }) {
  const orgId = useOrgId();
  const [drafts, setDrafts] = useState<ColumnDescriptionDraftRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setError(null);
    listTableColumnDescriptionDrafts(orgId, tableId, controller.signal)
      .then((rows) => {
        if (!controller.signal.aborted) setDrafts(rows);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted || (e as Error)?.name === "AbortError") return;
        setError(`Drafts could not be loaded: ${errorText(e)}`);
      });
  }, [orgId, tableId]);

  useEffect(() => {
    setDrafts(null);
    setNotice(null);
    load();
    return () => request.current?.abort();
  }, [load]);

  const run = async (label: string, action: () => Promise<string | null>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const message = await action();
      if (message) setNotice(message);
      load();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const generate = () =>
    run("generate", async () =>
      describeGeneration(await generateColumnDescriptionDrafts(orgId, [tableId])),
    );

  const submitAll = () =>
    run("submit-all", async () => {
      const result = await submitTableColumnDescriptionDrafts(tableId);
      const sent = result.submitted_review_ids.length;
      return (
        `Sent ${plural(sent, "draft")} to the review queue. Someone other than you has to approve ` +
        "each one before it is published." +
        (result.skipped_below_threshold > 0
          ? ` ${result.skipped_below_threshold} stayed as drafts: too little evidence to submit.`
          : "")
      );
    });

  const submitOne = (draft: ColumnDescriptionDraftRead) =>
    void run(draft.id, async () => {
      await submitColumnDescriptionDraft(draft.id);
      return `${draft.column_name} is in the review queue. Someone other than you has to approve it.`;
    });

  const saveOne = (draft: ColumnDescriptionDraftRead, text: string) =>
    run(draft.id, async () => {
      await editColumnDescriptionDraft(draft.id, text, draft.drafted_text);
      return `Saved your wording for ${draft.column_name}.`;
    });

  const open = (drafts ?? []).filter((draft) => OPEN_STATUSES.has(draft.status));
  const closed = (drafts ?? []).length - open.length;
  const ready = open.filter((draft) => draft.status === "DRAFT" && draft.reviewable).length;
  const headingId = `cdd-heading-${tableId}`;

  return (
    <section className="cdd" aria-labelledby={headingId}>
      <div className="cdd__head">
        <div className="colp__sub" id={headingId}>
          Column description drafts
        </div>
        <div className="cdd__actions">
          <Button
            disabled={busy !== null}
            onClick={() => void generate()}
            title="Compose a draft for each undescribed column from catalog evidence: dbt docs, source comments, keys and approved relationships. No model call; nothing is published without review."
          >
            {busy === "generate" ? "Drafting…" : "Draft undescribed columns"}
          </Button>
          {ready > 0 ? (
            <Button variant="primary" disabled={busy !== null} onClick={() => void submitAll()}>
              {busy === "submit-all" ? "Submitting…" : `Submit ${ready} for review`}
            </Button>
          ) : null}
        </div>
      </div>
      <p className="cdd__lede">
        Drafted from catalog evidence only, and published only after someone other than you approves
        each one in the review queue.
      </p>

      {notice ? (
        <div className="cdd__notice" role="status">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="cdd__error" role="alert">
          <span>{error}</span>
          <Button onClick={load}>Retry</Button>
        </div>
      ) : null}

      {drafts === null && !error ? (
        <div className="cdd__load" role="status">
          Loading drafts…
        </div>
      ) : null}
      {drafts !== null && open.length === 0 ? (
        <div className="cdd__none">
          No open drafts for this table.
          {closed > 0
            ? ` ${plural(closed, "earlier draft")} ${closed === 1 ? "was" : "were"} published, rejected or superseded.`
            : ""}
        </div>
      ) : null}
      {open.length > 0 ? (
        <ol className="cdd__list" aria-label="Open column description drafts">
          {open.map((draft) => (
            <DraftItem
              key={draft.id}
              draft={draft}
              busy={busy !== null}
              onSave={saveOne}
              onSubmit={submitOne}
            />
          ))}
        </ol>
      ) : null}
    </section>
  );
}
