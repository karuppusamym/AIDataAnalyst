import { useEffect, useMemo, useState } from "react";
import type { Persona } from "../lib/ui-types";
import { Button, Pill } from "./primitives";
import "./OnboardingWizard.css";

/* ---------------------------------------------------------------------------
   UX-8: guided onboarding per persona — setup wizards.

   Branching is driven by the SAME persona set module 21 §5 already
   establishes (`Persona`, `ui-types.ts`) and `App.tsx` already derives from
   `GET /v1/me` (OIDC groups) or the dev switcher — this wizard invents no new
   backend concept, per this item's own scope constraint. There is no
   role/persona-specific "recommended first steps" model anywhere in
   `src/aida` to read from either (checked: `persona_api.py`/`security.py`
   define *who a principal is*, never *what a new principal of that persona
   should do first*), so the five checklists below are UI-owned content,
   hand-written from what each persona's screens in this shell actually do —
   not fetched, not fabricated as if the server curated them.

   Every checklist item points at a real nav entry in `App.tsx`'s own `NAV`
   list, and is honest about which of those are migrated (`ready`) versus
   still served by the legacy portal — the same strangle-seam distinction the
   nav's own "legacy" pill already draws, so onboarding never promises a
   screen this shell does not yet have.

   Completion state is `localStorage`-only (`atlas.onboarding.*`): there is
   no backend field for "has this principal finished onboarding" and this
   item's scope does not permit adding one (ui-next-only, no `src/aida`
   edits) — so progress is a per-browser convenience, not a durable,
   cross-device record. It degrades safely: a viewer who clears storage, or
   opens a private window, simply sees the wizard again.
--------------------------------------------------------------------------- */

export interface ChecklistItem {
  navId: string;
  label: string;
  ready: boolean;
  blurb: string;
}

export const PERSONA_CHECKLISTS: Record<Persona, ChecklistItem[]> = {
  Analyst: [
    { navId: "catalog", label: "Catalog", ready: true, blurb: "Find certified tables before you query anything — certification and quality state are visible before you commit to a table." },
    { navId: "marketplace", label: "Marketplace", ready: true, blurb: "Request access to a governed data product instead of asking around for a table you may not be entitled to." },
    { navId: "lineage", label: "Lineage, narrated", ready: true, blurb: "Ask a root-cause question about one asset and get the traversal explained hop by hop, not a graph to decode yourself." },
  ],
  Steward: [
    { navId: "catalog", label: "Catalog", ready: true, blurb: "Bulk-describe and certify the undocumented/uncertified assets this screen surfaces first." },
    { navId: "governance", label: "Review queue", ready: true, blurb: "Decide the proposals waiting on a person — every one carries its diff, confidence and evidence." },
    { navId: "studio", label: "Studio change sets", ready: true, blurb: "Author and submit governed changes to metrics, tools, terms and context products." },
    { navId: "context", label: "Context products", ready: true, blurb: "Package governed context for a specific audience, policy, and delivery target." },
  ],
  Reviewer: [
    { navId: "governance", label: "Review queue", ready: true, blurb: "This is your primary surface — maker-checker decisions with the diff and evidence right there, no separate lookup." },
    { navId: "refusals", label: "Lineage refusals", ready: true, blurb: "See what an agent run refused to do and why, when a decision needs to be checked against the record." },
  ],
  Operator: [
    { navId: "sources", label: "Sources", ready: true, blurb: "Review connector health and configuration across the active source fleet." },
    { navId: "operations", label: "Operations", ready: true, blurb: "Monitor analysis runs, ingestion batches, and delivery exceptions." },
    { navId: "catalog", label: "Catalog", ready: true, blurb: "Check what the platform can currently see once a source is connected." },
    { navId: "administration", label: "Administration", ready: true, blurb: "Set up organizations, lines of business, projects, and governed source connections." },
  ],
  Auditor: [
    { navId: "refusals", label: "Lineage refusals", ready: true, blurb: "Every recorded refusal decision and the control that produced it — a real, queryable record, not a narrative." },
    { navId: "governance", label: "Review queue", ready: true, blurb: "Decided proposals carry who decided, when, and why." },
    { navId: "audit", label: "Audit ledger", ready: true, blurb: "Inspect the full event ledger with evidence and decision history." },
  ],
};

const STORAGE_PREFIX = "atlas.onboarding";

function loadDone(persona: string): Set<string> {
  try {
    const raw = localStorage.getItem(`${STORAGE_PREFIX}.${persona}.done`);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set(); // private window / storage blocked -- degrade to "nothing marked done yet"
  }
}

function saveDone(persona: string, done: Set<string>) {
  try {
    localStorage.setItem(`${STORAGE_PREFIX}.${persona}.done`, JSON.stringify([...done]));
  } catch {
    /* best-effort only -- a viewer whose browser blocks storage just re-sees the checklist next time */
  }
}

export function OnboardingWizard({
  persona,
  onNavigate,
  compact = false,
}: {
  /** `null` while `/v1/me` is loading, or when OIDC derived no persona for
   *  this principal's groups -- the wizard renders a persona-agnostic
   *  welcome rather than guessing, the same fail-closed default
   *  `PersonaNav` applies. */
  persona: Persona | null;
  onNavigate: (navId: string) => void;
  compact?: boolean;
}) {
  const [done, setDone] = useState<Set<string>>(() => (persona ? loadDone(persona) : new Set()));

  useEffect(() => {
    setDone(persona ? loadDone(persona) : new Set());
  }, [persona]);

  const checklist = useMemo(() => (persona ? PERSONA_CHECKLISTS[persona] : []), [persona]);

  const toggle = (navId: string) => {
    if (!persona) return;
    setDone((prev) => {
      const next = new Set(prev);
      if (next.has(navId)) next.delete(navId);
      else next.add(navId);
      saveDone(persona, next);
      return next;
    });
  };

  const reset = () => {
    if (!persona) return;
    setDone(new Set());
    saveDone(persona, new Set());
  };

  if (!persona) {
    return (
      <div className={`onb${compact ? " onb--compact" : ""}`}>
        <header className="onb__head">
          {compact ? <><span className="onb__eyebrow">Next steps</span><h2 className="onb__h1">Get started</h2></> : <h1 className="onb__h1">Get started</h1>}
          <p className="onb__lede">
            No persona is mapped for your account yet, so there is no persona-specific
            checklist to show. Once your identity provider reports one (or you pick one
            from the dev switcher), your setup steps appear here.
          </p>
        </header>
      </div>
    );
  }

  const completedCount = checklist.filter((c) => done.has(c.navId)).length;

  return (
    <div className={`onb${compact ? " onb--compact" : ""}`}>
      <header className="onb__head">
        {compact ? (
          <><span className="onb__eyebrow">Your progress</span><h2 className="onb__h1">{persona} setup</h2></>
        ) : (
          <><h1 className="onb__h1">Get started, {persona}</h1><p className="onb__lede">Everything below opens a working area, not a preview. Steps marked <Pill tone="mute">legacy</Pill> stay in the existing portal until they migrate.</p></>
        )}
        <div className="onb__progress" role="status">
          <div className="onb__bar">
            <div
              className="onb__fill"
              style={{ width: checklist.length ? `${(completedCount / checklist.length) * 100}%` : "0%" }}
            />
          </div>
          <span className="onb__pcount tnum">{completedCount}/{checklist.length} done</span>
          {completedCount > 0 ? (
            <button className="onb__reset" onClick={reset}>Reset</button>
          ) : null}
        </div>
      </header>

      <ol className="onb__list">
        {checklist.map((item, i) => {
          const isDone = done.has(item.navId);
          return (
            <li key={item.navId} className={`onb__item${isDone ? " onb__item--done" : ""}`}>
              <label className="onb__check">
                <input
                  type="checkbox"
                  checked={isDone}
                  onChange={() => toggle(item.navId)}
                  aria-label={`Mark "${item.label}" as done`}
                />
              </label>
              <div className="onb__body">
                <div className="onb__title">
                  <span className="onb__n">{i + 1}</span>
                  <span>{item.label}</span>
                  {!item.ready ? <Pill tone="mute">legacy</Pill> : null}
                </div>
                <p className="onb__blurb">{item.blurb}</p>
              </div>
              <Button variant={compact ? undefined : "primary"} onClick={() => onNavigate(item.navId)}>Open</Button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
