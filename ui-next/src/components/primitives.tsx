import type { ReactNode } from "react";
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
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "quiet";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  return (
    <button className={`btn btn--${variant}`} onClick={onClick} disabled={disabled} type={type}>
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
