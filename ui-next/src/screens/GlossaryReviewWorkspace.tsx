import { Suspense, lazy, useCallback, useRef } from "react";
import type { KeyboardEvent } from "react";
import { useUrlState } from "../lib/useUrlState";
import "./GlossaryReview.css";

/* ---------------------------------------------------------------------------
   Glossary review -- one workspace, two tabs (R11-AUD08).

   Conflicts and link proposals are two queues of the same job: things the
   platform found about what the glossary means, waiting for a steward to put
   in front of a reviewer. They share their readers, their writers, their
   filter, their pager and the same last step (a governance review somebody
   else decides), so they are two tabs of one destination rather than two
   sidebar items -- the shape `StewardshipWorkspace` and `DocumentationWorkspace`
   took for the same reason. Each tab is its own lazy chunk.

   WHAT THIS SHELL OWNS is the view axis (`?view=conflicts|proposals`, in the
   URL so a tab is a link and survives a reload) and the keyboard model of a
   tablist: one tab stop, Left/Right move between tabs with wrap, Home/End jump
   to the ends. WHAT IT DOES NOT OWN is any request or role: each tab asks for
   what it shows, behind its own role check.

   A tab switch drops `?status=`. The two families do not share a status
   vocabulary (a conflict is OPEN, a proposal is DRAFT), so a filter carried
   across would narrow the other tab to a value it can never hold. There is no
   unsaved-change registration, because there is no un-run form in a tab: every
   input lives in a modal dialog that the tab bar cannot be reached beneath.

   THE REVIEW QUEUE IS NOT ABSORBED. Both tabs end in a review that a different
   person decides there; nothing here decides one, and the link to it is offered
   only to sessions the queue admits.
--------------------------------------------------------------------------- */

const GlossaryConflicts = lazy(() =>
  import("./GlossaryConflicts").then((module) => ({ default: module.GlossaryConflicts })),
);
const GlossaryLinkProposals = lazy(() =>
  import("./GlossaryLinkProposals").then((module) => ({ default: module.GlossaryLinkProposals })),
);

/** The view axis. `conflicts` is the default, and is written as the ABSENCE of
 *  `?view=` so the canonical URL for the destination stays
 *  `#/steward/glossary-review`. */
export const GLOSSARY_REVIEW_VIEWS = [
  { value: "conflicts", label: "Conflicts" },
  { value: "proposals", label: "Link proposals" },
] as const;

export type GlossaryReviewView = (typeof GLOSSARY_REVIEW_VIEWS)[number]["value"];

const DEFAULT_VIEW: GlossaryReviewView = "conflicts";

/** Parse `?view=`. An unknown value opens the default rather than nothing: a
 *  link from the future is still a link to Glossary review. */
export function glossaryReviewViewFrom(params: URLSearchParams): GlossaryReviewView {
  const raw = params.get("view");
  return GLOSSARY_REVIEW_VIEWS.find((entry) => entry.value === raw)?.value ?? DEFAULT_VIEW;
}

const PANEL_ID = "glrev-panel";
const tabId = (view: GlossaryReviewView) => `glrev-tab-${view}`;

export function GlossaryReviewWorkspace() {
  const [params, setParams] = useUrlState();
  const view = glossaryReviewViewFrom(params);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);

  const show = useCallback(
    (next: GlossaryReviewView) => {
      if (next === view) return;
      setParams({ view: next === DEFAULT_VIEW ? null : next, status: null });
    },
    [view, setParams],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = GLOSSARY_REVIEW_VIEWS.length - 1;
    const target =
      event.key === "ArrowRight"
        ? index === last
          ? 0
          : index + 1
        : event.key === "ArrowLeft"
          ? index === 0
            ? last
            : index - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (target === null) return;
    event.preventDefault();
    // Focus follows the selection.
    show(GLOSSARY_REVIEW_VIEWS[target]!.value);
    tabs.current[target]?.focus();
  };

  const active = GLOSSARY_REVIEW_VIEWS.find((entry) => entry.value === view)!;

  return (
    <div className="glrev">
      <header className="glrev__pagehead">
        <h1 className="glrev__h1">Glossary review</h1>
        <p className="glrev__lede">
          Definitions that conflict, and links the platform suggests between tables and glossary terms, for a
          steward to put in front of a reviewer. Both end in a governance review that someone other than you
          decides.
        </p>
      </header>

      <div className="glrev__tabs" role="tablist" aria-label="Glossary review views">
        {GLOSSARY_REVIEW_VIEWS.map((entry, index) => {
          const selected = view === entry.value;
          return (
            <button
              key={entry.value}
              ref={(element) => {
                tabs.current[index] = element;
              }}
              type="button"
              role="tab"
              id={tabId(entry.value)}
              aria-selected={selected}
              /* Only the selected tab names the panel: the other view is not
                 mounted, and an `aria-controls` pointing at nothing is an
                 invalid reference rather than a hint. */
              aria-controls={selected ? PANEL_ID : undefined}
              tabIndex={selected ? 0 : -1}
              className={`glrev__tab${selected ? " glrev__tab--active" : ""}`}
              onClick={() => show(entry.value)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {entry.label}
            </button>
          );
        })}
      </div>

      <div className="glrev__panel" id={PANEL_ID} role="tabpanel" aria-labelledby={tabId(view)}>
        <Suspense
          fallback={
            <div className="glrev__loading" role="status">
              Loading {active.label.toLowerCase()}…
            </div>
          }
        >
          {view === "proposals" ? <GlossaryLinkProposals /> : <GlossaryConflicts />}
        </Suspense>
      </div>
    </div>
  );
}
