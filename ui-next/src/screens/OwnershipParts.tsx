import { useId } from "react";
import type { ReactNode } from "react";
import { Button, Dialog, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import { navigateTo } from "../lib/navigate";
import type { BulkStewardshipOperationRead } from "../lib/types";

/* ---------------------------------------------------------------------------
   The pieces the three Ownership panels share (R11-AUD08, part 2).

   Nothing here decides who may do what and nothing here calls an endpoint. The
   role lists sit beside the requests they guard, in each panel, so a reader of a
   request sees the roles it is admitted to (`lib/roles.ts` says why). What is
   shared is wording and layout that would otherwise be typed three times and
   drift: how an expiry reads, how a status reads, the confirmation every write
   goes through, and the answer a request that opened a review gets back.
--------------------------------------------------------------------------- */

/** "a, b or c" -- the sentence form of a role list. */
export const listOr = (items: readonly string[]): string =>
  items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;

/** `2026-09-19 08:30 UTC`. UTC and fixed-width so the same instant reads the same
 *  for every steward and in every test, whatever the browser's locale. */
export const stamp = (iso: string | null | undefined): string =>
  iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "an unknown time";

const DAY_MS = 86_400_000;

/** How close an expiry has to be to read as a warning: the server's own default,
 *  `ownership_expiry_warn_days` (14), which is also the Work queue banner's window. */
export const EXPIRY_WARN_DAYS = 14;

/**
 * An assignment's expiry, as a steward reads it.
 *
 * `expires_at` is null on rows written before P2-07 -- "no expiry", which is a
 * statement about the row and not a defect. An expiry in the PAST on a row that is
 * still listed means the row is inside the grace period: the daily sweep flips an
 * ACTIVE row to LAPSED only once `expires_at + ownership_expiry_grace_days` (30 by
 * default) has passed, so until then it still counts as an owner and can still be
 * reaffirmed. Said as "past its expiry", not as "expired" outright, because the
 * second reads as "this is no longer an owner" and, until the sweep lapses it, it
 * still is one.
 */
export function describeExpiry(
  expiresAt: string | null,
  now: number = Date.now(),
): { label: string; tone: Tone } {
  if (expiresAt === null) return { label: "no expiry set", tone: "mute" };
  const at = Date.parse(expiresAt);
  if (Number.isNaN(at)) return { label: expiresAt, tone: "mute" };
  const date = expiresAt.slice(0, 10);
  const days = Math.round((at - now) / DAY_MS);
  if (at <= now) {
    const ago = Math.max(1, Math.abs(days));
    return { label: `past its expiry (${date}, ${ago} day${ago === 1 ? "" : "s"} ago)`, tone: "bad" };
  }
  return {
    label: `expires ${date} (in ${days} day${days === 1 ? "" : "s"})`,
    tone: (at - now) / DAY_MS <= EXPIRY_WARN_DAYS ? "warn" : "mute",
  };
}

/** A bulk operation's status, in the words a steward uses and with a tone. */
export function describeOperationStatus(status: string): { label: string; tone: Tone } {
  switch (status) {
    case "REVIEW_REQUIRED":
      return { label: "waiting for review", tone: "info" };
    case "APPLIED":
      return { label: "applied", tone: "ok" };
    case "REJECTED":
      return { label: "rejected", tone: "bad" };
    default:
      return { label: status.toLowerCase().replace(/_/g, " "), tone: "mute" };
  }
}

/** A string parameter of an operation, or null -- `parameters` is `Record<string, unknown>` on the wire. */
export const stringParameter = (operation: BulkStewardshipOperationRead, key: string): string | null => {
  const value = operation.parameters[key];
  return typeof value === "string" ? value : null;
};

/** Where a decider finds the review an operation opened. */
export function openReview(operation: BulkStewardshipOperationRead): void {
  navigateTo("governance", { review: operation.governance_review_id });
}

/**
 * A labelled control with a hint under it.
 *
 * The hint is the control's DESCRIPTION, not part of its name. `Field` wraps
 * whatever it is given in the `<label>`, so a hint placed inside it becomes part
 * of the accessible name -- "Rule key Unique in this organization..." -- and, since
 * a hint that turns into a validation message changes as the steward types, a name
 * that renames itself mid-edit (`ConfirmDialog` records the same lesson, R11-C2).
 * So the hint sits beside the label, and the control points at it with
 * `aria-describedby`.
 */
export function HintedField({
  label,
  hint,
  children,
}: {
  label: string;
  hint: ReactNode;
  /** Given the id to put in the control's `aria-describedby`. */
  children: (describedBy: string) => ReactNode;
}) {
  const hintId = useId();
  return (
    <div className="own__fieldwrap">
      <Field label={label}>{children(hintId)}</Field>
      <span className="own__hint" id={hintId}>
        {hint}
      </span>
    </div>
  );
}

/** The sentence a panel shows a session whose roles cannot read what it lists. Nothing was asked for. */
export function NotApplicable({ what, roles }: { what: string; roles: readonly string[] }) {
  return (
    <p className="own__note" role="status">
      Not applicable to your roles: only sessions holding {listOr(roles)} can see {what}.
    </p>
  );
}

/**
 * The confirmation every ownership write goes through.
 *
 * Not `ConfirmDialog`, because what these confirmations owe the steward is a LIST of
 * what will and will not happen -- "nothing changes until a different reviewer
 * approves" is a separate fact from "at most 500", and folded into one description
 * paragraph the one that matters most is the one that gets skipped. `Dialog` gives
 * the same modal behaviour (focus trap, Escape, inert background, focus restored to
 * the button that opened it). A click outside does not dismiss it: a rationale typed
 * for a governed request should not be thrown away by a stray click.
 *
 * The server's refusal is shown here, verbatim, and the dialog stays open so the
 * steward can correct the request from the same place.
 */
export function OwnershipConfirm({
  title,
  summary,
  facts,
  confirmLabel,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  summary: string;
  facts: readonly ReactNode[];
  confirmLabel: string;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog
      title={title}
      description={summary}
      onClose={onCancel}
      dismissOnBackdrop={false}
      footer={
        <>
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={onConfirm} disabled={busy}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </>
      }
    >
      <ul className="own__facts">
        {facts.map((fact, index) => (
          <li key={index}>{fact}</li>
        ))}
      </ul>
      {error ? (
        <p className="dlg__err" role="alert">
          {error}
        </p>
      ) : null}
    </Dialog>
  );
}

/**
 * What a request that OPENED A REVIEW tells the steward, once it has.
 *
 * `applied_count` is on the operation and is always 0 here: this is the
 * 202 answer to a request, not to a decision. The number the steward asked
 * for ("how many were moved") is therefore not knowable yet, and the honest thing
 * is to say so and say where it will be -- rather than show a 0 that reads as "the
 * request moved nothing".
 *
 * `noun` is what one subject is: tables for a rule, ownerships for a leaver.
 * `remainder` is what the caller knows this request does NOT cover -- the leaver
 * request's `selection_truncated`, or the portfolio the preview found to be larger
 * than what was asked for -- so the steward is told what was left, not just what was sent.
 */
export function RequestedReview({
  operation,
  noun,
  headline,
  remainder,
  onDismiss,
}: {
  operation: BulkStewardshipOperationRead;
  noun: "tables" | "ownerships";
  headline: string;
  /** A sentence about what this request does NOT cover, when the caller can say. */
  remainder?: ReactNode;
  onDismiss: () => void;
}) {
  const status = describeOperationStatus(operation.status);
  const count = operation.subject_ids.length;
  return (
    <section className="own__result" role="status" aria-label="Review requested">
      <div className="own__resulthead">
        <strong>{headline}</strong>
        <Pill tone={status.tone}>{status.label}</Pill>
      </div>
      <p className="own__resultline">
        {count} {count === 1 ? noun.replace(/s$/, "") : noun} in this request.{" "}
        <strong>{operation.applied_count} changed so far.</strong> Nothing changes until a different reviewer
        approves it; after that, the Requests list below shows how many were applied and how many were
        left as they were.
      </p>
      {remainder ? <p className="own__resultline">{remainder}</p> : null}
      <div className="own__actions">
        <Button variant="primary" onClick={() => openReview(operation)}>
          Open this review
        </Button>
        <Button onClick={onDismiss}>Dismiss</Button>
      </div>
    </section>
  );
}
