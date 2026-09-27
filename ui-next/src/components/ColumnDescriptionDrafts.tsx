import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import {
  editColumnDescriptionDraft,
  generateColumnDescriptionDrafts,
  listTableColumnDescriptionDrafts,
  submitColumnDescriptionDraft,
  submitTableColumnDescriptionDrafts,
  type ColumnDraftGenerateResult,
} from "../lib/api/columnDescriptionDrafts";
import { useOrgId } from "../lib/org";
import type { ColumnDescriptionDraftRead } from "../lib/types";
import { Button, Pill, type Tone } from "./primitives";
import "./ColumnDescriptionDrafts.css";

/* ---------------------------------------------------------------------------
   Column description drafts, where a steward looks at columns.

   Drafts come from two places, and this section never lets them be confused:

   - Catalog evidence (dbt column docs, source comments, keys, approved
     relationships), composed on the server with no model call.
   - The governed model, only when a steward asks and only for columns whose
     evidence is too thin -- labelled Model-inferred, with the metadata it
     worked from, and capped so the reviewer agent can never approve one.

   Either way a draft is published only when someone other than the submitter
   (and anyone who edited it) approves it in the review queue. This section
   generates, lets a steward fix the wording, and submits; it cannot publish.

   Three things it says plainly rather than leaving a steward to discover:

   - A thin evidence draft is shown, not hidden, but cannot be submitted. Its
     score measures catalog evidence, not prose, so rewording does not help;
     the model or the source model workbook does.
   - A model draft can be wrong in a way that reads as right. It says so on
     the draft, not in a tooltip.
   - Editing makes you an author, and the server refuses an editor as approver.
--------------------------------------------------------------------------- */

const STATUS: Record<string, { label: string; tone: Tone }> = {
  DRAFT: { label: "Draft", tone: "info" },
  PENDING_APPROVAL: { label: "In review", tone: "warn" },
  APPROVED: { label: "Published", tone: "ok" },
  REJECTED: { label: "Rejected", tone: "bad" },
  SUPERSEDED: { label: "Superseded", tone: "mute" },
};

const OPEN_STATUSES = new Set(["DRAFT", "PENDING_APPROVAL"]);

const BASIS_LABEL: Record<string, string> = {
  NAME: "name",
  TYPE: "type",
  KEY: "key",
  FOREIGN_KEY: "foreign keys",
  TABLE_CONTEXT: "table's description",
  SIBLING_COLUMNS: "neighbouring columns",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function listed(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function originOf(draft: ColumnDescriptionDraftRead): string {
  const origin = draft.evidence?.origin;
  return typeof origin === "string" ? origin : "METADATA";
}

export function isModelDraft(draft: ColumnDescriptionDraftRead): boolean {
  return originOf(draft).startsWith("MODEL_INFERRED");
}

function basisOf(draft: ColumnDescriptionDraftRead): string[] {
  const model = draft.evidence?.model;
  if (typeof model !== "object" || model === null) return [];
  const basis = (model as { basis?: unknown }).basis;
  return Array.isArray(basis)
    ? basis.map((code) => BASIS_LABEL[String(code)] ?? String(code).toLowerCase())
    : [];
}

/** What a generation call did, including what it deliberately did not do. */
export function describeGeneration(
  result: ColumnDraftGenerateResult,
  options: { modelAssist?: boolean } = {},
): string {
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
  const modelDrafted = result.model_drafted ?? 0;
  if (modelDrafted > 0) {
    parts.push(`The model wrote ${modelDrafted}; each is labelled and needs a person's approval.`);
  }
  const replaced = result.replaced_thin_drafts ?? 0;
  if (replaced > 0) parts.push(`Replaced ${plural(replaced, "thin draft")} nobody had touched.`);
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
  const withheld = result.model_withheld ?? 0;
  if (withheld > 0) parts.push(`${withheld} withheld by injection screening.`);
  const fallbacks = result.model_fallbacks ?? 0;
  if (fallbacks > 0) {
    parts.push(
      `${fallbacks} fell back to evidence-only drafts${result.model_note ? `: ${result.model_note}` : ""}.`,
    );
  }
  const thin = result.below_review_threshold;
  if (thin > 0) {
    parts.push(
      options.modelAssist
        ? `${thin} still cannot be submitted; write ${thin === 1 ? "that one" : "those"} in the source model workbook.`
        : `${thin} rest on too little catalog evidence to submit; use the model for thin columns, ` +
            "or write those in the source model workbook instead.",
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
  const model = isModelDraft(draft);
  const basis = basisOf(draft);
  const textId = `cdd-text-${draft.id}`;

  const save = async () => {
    if (await onSave(draft, text)) setEditing(false);
  };

  return (
    <li className="cdd__item">
      <div className="cdd__row">
        <span className="cdd__col">{draft.column_name}</span>
        <Pill tone={status.tone}>{status.label}</Pill>
        {model ? <Pill tone="warn">Model-inferred</Pill> : null}
        <span
          className="cdd__score"
          title={
            model
              ? "The model's own confidence, capped at 70%. It orders review; it never replaces it."
              : "How much catalog evidence this draft rests on. It orders review; it never replaces it."
          }
        >
          {`${model ? "model confidence" : "evidence"} ${Math.round(draft.overall_score * 100)}%`}
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

      {model ? (
        <p className="cdd__model">
          {`Inferred by a model from its ${basis.length > 0 ? listed(basis) : "metadata"}. ` +
            "It can be wrong in a way that reads as right: check it against the data before you submit it."}
        </p>
      ) : null}

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
                : "This draft cannot be submitted as it stands."
            }
          >
            Submit for review
          </Button>
        </div>
      ) : null}

      {draft.status === "DRAFT" && !draft.reviewable ? (
        <p className="cdd__warn">
          {model
            ? "The model was not confident enough to submit this. Write this column's description in the source model workbook instead."
            : "Too little catalog evidence to submit. Rewording does not change that: the score measures the evidence, not the prose. Use the model for thin columns, or write this column's description in the source model workbook."}
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

  const generateWithModel = () =>
    run("model", async () =>
      describeGeneration(
        await generateColumnDescriptionDrafts(orgId, [tableId], { modelAssist: true }),
        { modelAssist: true },
      ),
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
          <Button
            disabled={busy !== null}
            onClick={() => void generateWithModel()}
            title="Columns with too little catalog evidence are drafted by the governed model from metadata only; the rest are still drafted from evidence. Model drafts are labelled, capped below automatic approval, and always need a person."
          >
            {busy === "model" ? "Asking the model…" : "Use the model for thin columns"}
          </Button>
          {ready > 0 ? (
            <Button variant="primary" disabled={busy !== null} onClick={() => void submitAll()}>
              {busy === "submit-all" ? "Submitting…" : `Submit ${ready} for review`}
            </Button>
          ) : null}
        </div>
      </div>
      <p className="cdd__lede">
        Drafted from catalog evidence, or by the model where you ask for it, and published only after
        someone other than you approves each one in the review queue.
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
