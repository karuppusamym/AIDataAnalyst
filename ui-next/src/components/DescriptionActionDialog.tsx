import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import { requestDescriptionWithdrawal } from "../lib/_column_documentation_api";
import { Button } from "./primitives";
import "./DescriptionActionDialog.css";

/* ---------------------------------------------------------------------------
   Withdraw / reinstate a description.

   Replaces a `window.prompt`. That was functional and honest but the wrong
   affordance for a governed action, for three reasons this dialog fixes:

   1. **It shows the text being acted on.** A prompt asks "why?" about a
      description the steward has to remember. Deciding to retire a paragraph
      without re-reading it is exactly how the wrong one gets retired.
   2. **It says what will and will not happen.** Nothing is published or
      un-published here; a review is filed and someone else decides. A prompt
      cannot say that, so the platform's most important property at that moment
      was invisible.
   3. **It can be cancelled without ambiguity.** `prompt` conflates "cancel"
      with "empty reason", and the difference matters when the reason is
      required.

   Rendered inline rather than as a portal/modal: this pane is already a
   focused surface, and a full-screen overlay for a two-field form would be
   heavier than the decision warrants.
--------------------------------------------------------------------------- */

export type DescriptionActionSubject = {
  subjectType: "TABLE" | "COLUMN";
  subjectId: string;
  /** What the steward sees named in the dialog — a column or table name. */
  label: string;
  /** The words being retired, or brought back. Always shown. */
  text: string;
};

const COPY = {
  WITHDRAW: {
    title: "Withdraw this description",
    textLabel: "Text to be retired",
    reasonLabel: "Why should it be retired?",
    placeholder: "what is wrong with it…",
    confirm: "Request withdrawal",
    effect:
      "Nothing is un-published now. This files a review; the description stays live, and stays what every reader resolves, until someone other than you approves it.",
  },
  REINSTATE: {
    title: "Reinstate this description",
    textLabel: "Text to be brought back",
    reasonLabel: "Why should it come back?",
    placeholder: "why the withdrawal was wrong…",
    confirm: "Request reinstatement",
    effect:
      "Nothing is published now. This files a review; if approved, the text is republished as a new version — the withdrawn one stays withdrawn, so the history still records that it was retired.",
  },
} as const;

export function DescriptionActionDialog({
  action,
  subject,
  onClose,
  onRequested,
}: {
  action: "WITHDRAW" | "REINSTATE";
  subject: DescriptionActionSubject;
  onClose: () => void;
  onRequested: (message: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reasonInput = useRef<HTMLTextAreaElement | null>(null);
  const copy = COPY[action];

  useEffect(() => {
    reasonInput.current?.focus();
  }, []);

  // Escape closes, like any dismissible surface. Nothing has been sent at this
  // point, so cancelling is always safe.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const submit = useCallback(async () => {
    const trimmed = reason.trim();
    if (trimmed.length < 3) {
      setError("Give a reason — the reviewer needs to know why.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await requestDescriptionWithdrawal(
        subject.subjectType,
        subject.subjectId,
        trimmed,
        action,
      );
      onRequested(
        action === "WITHDRAW"
          ? `Withdrawal requested for ${subject.label}. It stays published until a reviewer approves.`
          : `Reinstatement requested for ${subject.label}. Nothing is published until a reviewer approves.`,
      );
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [reason, subject, action, onRequested, onClose]);

  return (
    <div
      className="dad"
      role="dialog"
      aria-modal="false"
      aria-label={`${copy.title}: ${subject.label}`}
    >
      <div className="dad__head">
        <span className="dad__title">{copy.title}</span>
        <span className="dad__subject">{subject.label}</span>
      </div>

      {/* Rule 1: the words are on screen while the decision is made. */}
      <div className="dad__label">{copy.textLabel}</div>
      <blockquote className="dad__text">{subject.text}</blockquote>

      <label className="dad__label" htmlFor="dad-reason">
        {copy.reasonLabel}
      </label>
      <textarea
        id="dad-reason"
        ref={reasonInput}
        className="dad__reason"
        rows={3}
        value={reason}
        placeholder={copy.placeholder}
        disabled={busy}
        onChange={(e) => setReason(e.target.value)}
      />

      {/* Rule 2: what this does, and what it does not. */}
      <p className="dad__effect">{copy.effect}</p>

      {error ? (
        <div className="dad__error" role="alert">
          {error}
        </div>
      ) : null}

      <div className="dad__actions">
        <Button variant="primary" disabled={busy} onClick={() => void submit()}>
          {busy ? "Requesting…" : copy.confirm}
        </Button>
        <Button disabled={busy} onClick={onClose}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
