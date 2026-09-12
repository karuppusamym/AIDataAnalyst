import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode, RefObject } from "react";
import { createPortal } from "react-dom";

import { ApiError } from "../lib/http";
import { buildLink, type LinkTarget } from "../lib/routes";
import "./primitives.css";

/* Small, unopinionated primitives. Deliberately not a component library —
   every one of these encodes a rule from Module 21 and nothing else. */

export type Tone = "ok" | "warn" | "bad" | "info" | "mute" | "accent";

export function Pill({ tone = "mute", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pill pill--${tone}`}>{children}</span>;
}

/** Module 21 §6: state must be readable at a glance, so certification and
 *  quality get a shape (the stripe) as well as a colour — colour alone fails
 *  for the ~8% of male users with a colour vision deficiency. */
export function StateDot({ tone, title }: { tone: Tone; title: string }) {
  return <span className={`sdot sdot--${tone}`} title={title} aria-label={title} role="img" />;
}

export function Button({
  children,
  onClick,
  variant = "quiet",
  disabled,
  type = "button",
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "quiet";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
}) {
  return (
    <button
      className={`btn btn--${variant}`}
      onClick={onClick}
      disabled={disabled}
      type={type}
      title={title}
    >
      {children}
    </button>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
    </label>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty">
      <div className="empty__t">{title}</div>
      {hint ? <div className="empty__h">{hint}</div> : null}
    </div>
  );
}

/** Errors say what happened and what to do, never "something went wrong".
 *  `title` defaults to the Catalog screen's original copy (UX-11) so every
 *  existing call site is unchanged; UX-15's other screens pass their own. */
export function ErrorState({
  detail,
  onRetry,
  title = "The catalog could not be loaded",
}: {
  detail: string;
  onRetry: () => void;
  title?: string;
}) {
  return (
    <div className="errbox" role="alert">
      <div className="errbox__t">{title}</div>
      <div className="errbox__d">{detail}</div>
      <Button onClick={onRetry}>Try again</Button>
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Shared interaction primitives (review 2026-09-05, F21 · T17).

   THE DEFECT these exist to remove: the app declared accessibility rather
   than implementing it. The command palette carried `role="dialog"` and
   `aria-modal="true"`, autofocused its input and closed on Escape -- and then
   let Tab walk straight out of it into the page behind, never gave focus back
   to whatever opened it, and left the rest of the document reachable by both
   keyboard and screen reader. ARIA attributes describe a modal; they do not
   make one. Everything below is the behaviour those attributes were
   promising.

   Screens also hand-rolled loading/empty/error triads, copy-to-clipboard,
   confirmation prompts and 422 field errors one at a time, so each one had a
   different set of gaps -- most visibly a `void navigator.clipboard.writeText`
   with no `.catch`, which is silent failure on an insecure origin or a denied
   permission: the user sees "Link copied" and pastes the *previous* clipboard
   contents into a ticket.

   THE INVARIANT: an interaction behaviour is implemented once here. A screen
   that needs a modal, an async triad, a confirmation, a toast, a copy button
   or a field-error summary calls these rather than re-deriving one.
--------------------------------------------------------------------------- */

/** Elements that can hold focus, in DOM order. */
const FOCUSABLE = [
  "a[href]",
  "area[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "button:not([disabled])",
  "iframe",
  "[tabindex]:not([tabindex='-1'])",
  "[contenteditable='true']",
  "summary",
].join(",");

/**
 * Deliberately does NOT filter on `offsetParent`/`getClientRects`, the usual
 * visibility test: jsdom reports every element as having no layout, so that
 * test would make the trap a no-op under the very tests that are supposed to
 * prove it works. Attribute-level hiding is what a dialog's own content
 * actually uses, and it is checkable in both environments.
 */
function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute("hidden") && el.getAttribute("aria-hidden") !== "true",
  );
}

/**
 * Make everything outside `keep` unreachable, and undo it exactly.
 *
 * `aria-hidden` alone hides content from a screen reader but leaves it in the
 * Tab order; `inert` removes it from both but is not in every browser this
 * app must run in. Setting both is the only combination that is correct
 * everywhere, and the returned function restores the previous value of each
 * attribute rather than deleting it -- another dialog, or the app shell, may
 * legitimately have set one already.
 */
function makeBackgroundInert(keep: Element): () => void {
  const restores: Array<() => void> = [];
  for (const sibling of Array.from(document.body.children)) {
    if (sibling === keep || sibling.contains(keep)) continue;
    const previousAria = sibling.getAttribute("aria-hidden");
    const previousInert = sibling.getAttribute("inert");
    sibling.setAttribute("aria-hidden", "true");
    sibling.setAttribute("inert", "");
    restores.push(() => {
      if (previousAria === null) sibling.removeAttribute("aria-hidden");
      else sibling.setAttribute("aria-hidden", previousAria);
      if (previousInert === null) sibling.removeAttribute("inert");
      else sibling.setAttribute("inert", previousInert);
    });
  }
  return () => {
    for (const restore of restores) restore();
  };
}

export interface DialogProps {
  /** The accessible name. Rendered as the dialog's heading. */
  title: string;
  /** Optional one-line description, announced with the title. */
  description?: string;
  /** Escape, the close button, and a backdrop click all call this. */
  onClose: () => void;
  children: ReactNode;
  /** Action row, rendered after the body. */
  footer?: ReactNode;
  /** Focused on open. Defaults to the first focusable element in the body. */
  initialFocusRef?: RefObject<HTMLElement>;
  /** Set false for a confirmation the user must answer with a button. */
  dismissOnBackdrop?: boolean;
  /** Extra class on the dialog surface. */
  className?: string;
}

/**
 * A real modal dialog: focus trapped inside, focus restored on close, Escape
 * closes, and the rest of the document is inert while it is open.
 *
 * Rendered through a portal onto `document.body` so a dialog opened from deep
 * inside a scrolled, `overflow: hidden` pane is not clipped by it -- and so
 * "everything outside the dialog" is a simple sibling walk rather than a
 * guess about the tree above the call site.
 */
export function Dialog({
  title,
  description,
  onClose,
  children,
  footer,
  initialFocusRef,
  dismissOnBackdrop = true,
  className,
}: DialogProps) {
  const surfaceRef = useRef<HTMLDivElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  // Captured on the render that opened the dialog, not in the effect: by the
  // time effects run the browser may already have moved focus.
  const openerRef = useRef<Element | null>(
    typeof document === "undefined" ? null : document.activeElement,
  );

  useEffect(() => {
    const backdrop = backdropRef.current;
    const surface = surfaceRef.current;
    if (!backdrop || !surface) return;
    const releaseBackground = makeBackgroundInert(backdrop);
    const opener = openerRef.current;

    const target = initialFocusRef?.current ?? focusableWithin(surface)[0] ?? surface;
    target.focus();

    return () => {
      releaseBackground();
      // Restoring focus is the half that is always forgotten. Without it the
      // keyboard user is returned to the top of the document and has to walk
      // back to the control they pressed.
      if (opener instanceof HTMLElement && document.contains(opener)) opener.focus();
    };
  }, [initialFocusRef]);

  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const surface = surfaceRef.current;
      if (!surface) return;
      const items = focusableWithin(surface);
      if (items.length === 0) {
        event.preventDefault();
        surface.focus();
        return;
      }
      const first = items[0]!;
      const last = items[items.length - 1]!;
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === surface)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  return createPortal(
    <div
      className="dlg__backdrop"
      ref={backdropRef}
      onMouseDown={(event) => {
        if (dismissOnBackdrop && event.target === backdropRef.current) onClose();
      }}
    >
      <div
        className={`dlg${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        ref={surfaceRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <header className="dlg__head">
          <h2 className="dlg__title" id={titleId}>
            {title}
          </h2>
          <button className="dlg__x" type="button" onClick={onClose} aria-label={`Close ${title}`}>
            ×
          </button>
        </header>
        {description ? (
          <p className="dlg__desc" id={descriptionId}>
            {description}
          </p>
        ) : null}
        <div className="dlg__body">{children}</div>
        {footer ? <footer className="dlg__foot">{footer}</footer> : null}
      </div>
    </div>,
    document.body,
  );
}

/**
 * A confirmation the user answers with a button, replacing `window.confirm`
 * and `window.prompt`.
 *
 * `window.prompt` was how this app collected review rationale. It cannot be
 * styled, cannot be labelled, cannot show validation, and is blocked outright
 * by some browsers -- in which case the caller receives `null` and reads it as
 * "cancelled", so the reviewer's decision silently disappeared.
 */
export function ConfirmDialog({
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  requireReason = false,
  reasonLabel = "Reason",
  busy = false,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  /** Ask for, and require, a written rationale before confirming. */
  requireReason?: boolean;
  reasonLabel?: string;
  busy?: boolean;
  error?: string | null;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const reasonFieldId = useId();
  const reasonHintId = useId();
  const blocked = requireReason && reason.trim().length === 0;
  return (
    <Dialog
      title={title}
      description={description}
      onClose={onCancel}
      dismissOnBackdrop={!destructive}
      initialFocusRef={requireReason ? reasonRef : undefined}
      footer={
        <>
          <Button onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button
            variant="primary"
            disabled={busy || blocked}
            onClick={() => onConfirm(reason.trim())}
          >
            {busy ? "Working…" : confirmLabel}
          </Button>
        </>
      }
    >
      {requireReason ? (
        /* R11-C2: the hint is a DESCRIPTION, not part of the name.
         *
         * This was a `<label>` wrapping the textarea, the label text and the
         * hint, so the field's accessible name was the whole lot concatenated
         * -- "Reason A written rationale is recorded with this decision" --
         * and, because the hint is rendered only while the box is empty, that
         * name silently changed to "Reason" as soon as the reviewer typed a
         * character. A control that renames itself mid-edit is exactly the
         * kind of thing `getByRole(name:)` catches and a snapshot does not.
         * `aria-describedby` is what announces a hint after the name. */
        <div className="field">
          <label className="field__label" htmlFor={reasonFieldId}>
            {reasonLabel}
          </label>
          <textarea
            id={reasonFieldId}
            ref={reasonRef}
            className="dlg__reason"
            rows={4}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            aria-describedby={blocked ? reasonHintId : undefined}
          />
          {blocked ? (
            <span className="dlg__hint" id={reasonHintId}>
              A written rationale is recorded with this decision.
            </span>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p className="dlg__err" role="alert">
          {error}
        </p>
      ) : null}
    </Dialog>
  );
}

/** What an `ApiError` means for a specific object, in words a user can act on. */
export function describeError(error: unknown, subject = "this record"): string {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return `You do not have access to ${subject}. Ask its owner for access, or switch to the organization it belongs to.`;
    if (error.status === 404)
      return `${subject.charAt(0).toUpperCase()}${subject.slice(1)} no longer exists, or was never visible to you.`;
    if (error.isUnauthenticated) return "Your session has expired. Sign in again to continue.";
    if (error.isConflict)
      return `${error.detail} Someone else changed this record; reload before deciding again.`;
    return error.detail;
  }
  if (error instanceof Error) return error.message;
  return "The request failed.";
}

/** The support reference to quote, when the server gave us one. */
export function errorReference(error: unknown): string | null {
  return error instanceof ApiError ? error.supportReference : null;
}

/**
 * The loading / empty / error triad, with the correlation id attached.
 *
 * A deep link to an object the viewer cannot see is the case this exists for:
 * it must say *why* the screen is blank ("you do not have access", "it no
 * longer exists") rather than rendering an empty list that reads as "there is
 * nothing here".
 */
export function AsyncState({
  loading,
  error,
  empty,
  subject = "this record",
  loadingLabel = "Loading…",
  emptyTitle = "Nothing to show",
  emptyHint,
  errorTitle = "This could not be loaded",
  onRetry,
  children,
}: {
  loading?: boolean;
  error?: unknown;
  /** True when the request succeeded and returned nothing. */
  empty?: boolean;
  /** Named in the 403/404 message, e.g. "this incident". */
  subject?: string;
  loadingLabel?: string;
  emptyTitle?: string;
  emptyHint?: string;
  errorTitle?: string;
  onRetry?: () => void;
  children?: ReactNode;
}) {
  if (error) {
    const reference = errorReference(error);
    const retryable = !(error instanceof ApiError) || error.retryable;
    return (
      <div className="errbox" role="alert">
        <div className="errbox__t">{errorTitle}</div>
        <div className="errbox__d">{describeError(error, subject)}</div>
        {reference ? (
          <div className="errbox__ref">
            Quote reference <code>{reference}</code> in a support request.
          </div>
        ) : null}
        {onRetry && retryable ? <Button onClick={onRetry}>Try again</Button> : null}
      </div>
    );
  }
  if (loading) {
    return (
      <div className="asyncload" role="status">
        {loadingLabel}
      </div>
    );
  }
  if (empty) return <Empty title={emptyTitle} hint={emptyHint} />;
  return <>{children}</>;
}

/**
 * The validation summary for a 422, plus a lookup for marking single fields.
 *
 * `ApiError.fieldErrors` already carries FastAPI's `loc`/`msg` pairs; before
 * this they were flattened into one semicolon-joined sentence in a banner and
 * the form had no idea which of its inputs the server was complaining about.
 */
export function fieldErrorMap(error: unknown): ReadonlyMap<string, string> {
  const map = new Map<string, string>();
  if (!(error instanceof ApiError)) return map;
  for (const item of error.fieldErrors) {
    // `body.name` and `name` should both find the message: forms know their
    // own field names, not the server's request-body path.
    map.set(item.field, item.message);
    const leaf = item.field.split(".").pop();
    if (leaf && !map.has(leaf)) map.set(leaf, item.message);
  }
  return map;
}

export function FormErrors({
  error,
  title = "This could not be saved",
}: {
  error: unknown;
  title?: string;
}) {
  /* R11-C2: the summary takes focus the moment it appears.
   *
   * `role="alert"` alone announces the text, which is necessary and not
   * sufficient: on a long form the submit button is usually far below the
   * fields the server rejected, so a keyboard user hears "this could not be
   * saved" and is left at the bottom of the page with no idea which field to
   * go back to. Moving focus here puts them at the list of what to fix, which
   * is WCAG 3.3.1's whole point. Hooks run before the early return below --
   * a conditional hook is not allowed, and the effect's own guard is what
   * makes it a no-op when there is nothing to report. */
  const summaryRef = useRef<HTMLDivElement>(null);
  const hadError = useRef(false);
  useEffect(() => {
    // Only on the transition into failure: re-focusing on every re-render
    // would trap the user in the summary while they try to fix the form.
    if (error && !hadError.current) summaryRef.current?.focus();
    hadError.current = Boolean(error);
  }, [error]);

  if (!error) return null;
  const fields = error instanceof ApiError ? error.fieldErrors : [];
  const reference = errorReference(error);
  return (
    <div className="formerr" role="alert" ref={summaryRef} tabIndex={-1}>
      <div className="formerr__t">{title}</div>
      {fields.length > 0 ? (
        <ul className="formerr__list">
          {fields.map((item) => (
            <li key={item.field}>
              <span className="formerr__field">{item.field.split(".").pop()}</span> {item.message}
            </li>
          ))}
        </ul>
      ) : (
        <div className="formerr__d">{describeError(error)}</div>
      )}
      {reference ? (
        <div className="formerr__ref">
          Reference <code>{reference}</code>
        </div>
      ) : null}
    </div>
  );
}

export type ToastTone = "ok" | "bad" | "info";

export interface ToastMessage {
  readonly id: number;
  readonly text: string;
  readonly tone: ToastTone;
}

/**
 * Transient confirmation and failure messages, announced.
 *
 * Self-hosting (its own portal and live region) rather than requiring a
 * provider in the shell: this had to be adoptable by a screen without editing
 * `App.tsx`, which another lane owns.
 */
export function useToast() {
  const [messages, setMessages] = useState<readonly ToastMessage[]>([]);
  const nextId = useRef(1);
  const timers = useRef<number[]>([]);

  useEffect(
    () => () => {
      for (const timer of timers.current) window.clearTimeout(timer);
    },
    [],
  );

  const show = useCallback((text: string, tone: ToastTone = "ok") => {
    const id = nextId.current++;
    setMessages((prev) => [...prev, { id, text, tone }]);
    // Failures stay longer: a message the user has to act on must not
    // disappear while they are still reading it.
    const timer = window.setTimeout(
      () => setMessages((prev) => prev.filter((m) => m.id !== id)),
      tone === "bad" ? 9000 : 4000,
    );
    timers.current.push(timer);
  }, []);

  const node =
    typeof document === "undefined"
      ? null
      : createPortal(
          <div className="toasts" aria-live="polite" aria-atomic="false">
            {messages.map((message) => (
              <div key={message.id} className={`toast toast--${message.tone}`} role="status">
                {message.text}
              </div>
            ))}
          </div>,
          document.body,
        );

  return { show, node, messages } as const;
}

/**
 * Copy to the clipboard, and say so -- including when it fails.
 *
 * `navigator.clipboard` is undefined on an insecure origin and rejects when
 * the permission is denied. Every call site in this app was
 * `void navigator.clipboard?.writeText(x)` with no `.catch`, so both cases
 * rendered "Link copied" over an unchanged clipboard and the user pasted
 * whatever they had copied before into a ticket. On failure this exposes the
 * text so it can be selected and copied by hand.
 */
export function useCopy() {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback(async (text: string) => {
    setFailed(null);
    try {
      const writeText = navigator.clipboard?.writeText;
      if (!writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 2500);
      return true;
    } catch {
      setCopied(false);
      setFailed(text);
      return false;
    }
  }, []);

  const reset = useCallback(() => {
    setCopied(false);
    setFailed(null);
  }, []);

  return { copied, failed, copy, reset } as const;
}

/**
 * The one "Copy link" control.
 *
 * Takes a `LinkTarget`, never a string, so a share URL cannot be assembled
 * out of `location.origin + location.pathname` again and lose its screen
 * (F08). `buildLink` always writes the hash.
 */
export function CopyLinkButton({
  target,
  label = "Copy link",
  title,
}: {
  target: LinkTarget;
  label?: string;
  title?: string;
}) {
  const { copied, failed, copy, reset } = useCopy();
  const url = buildLink(target);
  // "Link copied" must not survive a change of selection: the button would
  // otherwise claim the *new* object's link is on the clipboard when the old
  // one is.
  useEffect(() => reset(), [url, reset]);
  return (
    <span className="copylink">
      <Button onClick={() => void copy(url)} title={title ?? url}>
        {copied ? "Link copied" : label}
      </Button>
      {failed ? (
        <span className="copylink__fallback" role="alert">
          <span className="copylink__msg">
            This browser refused clipboard access. Copy the link by hand:
          </span>
          <input
            className="copylink__url"
            readOnly
            value={failed}
            aria-label="Link to copy"
            onFocus={(event) => event.currentTarget.select()}
          />
        </span>
      ) : null}
    </span>
  );
}

/**
 * Warn before losing unsaved edits.
 *
 * Covers both ways the edit can be lost: closing or reloading the tab (the
 * browser's own prompt, the only thing allowed there) and navigating away
 * inside the app (our own confirmation). Screens previously did neither, so a
 * half-written description died on a stray click.
 */
export function useUnsavedChanges(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  /** Call before an in-app navigation that would discard the edit. */
  return useCallback(
    (message = "Discard your unsaved changes?") => !dirty || window.confirm(message),
    [dirty],
  );
}
