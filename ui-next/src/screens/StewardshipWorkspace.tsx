import { Suspense, lazy, useCallback, useRef } from "react";
import type { KeyboardEvent } from "react";
import { CrossLinks } from "../components/CrossLinks";
import type { CrossLink } from "../components/CrossLinks";
import { useUrlState } from "../lib/useUrlState";
import { useUnsavedNavigationGuard } from "../lib/unsavedChanges";
import "./StewardshipWorkspace.css";

/* ---------------------------------------------------------------------------
   One stewardship workspace — R11-S13, user items 15/17, design 21 §17.

   THE JOB, NOT THE ENDPOINT. "Stewardship" and "Playbooks" were two sidebar
   items for one kind of change: apply tag / classify / own / certify to every
   table a filter matches. Stewardship ran it once, by hand; a playbook runs
   the same four actions on a schedule. And the one Stewardship page put that
   bulk form side by side with the unowned-owner backlog, which is a queue of
   work rather than a change -- so the page led with neither. The design asks
   for three views led by what a steward is doing: Work queue, Bulk actions,
   Automation.

   A SHELL, NOT A REWRITE. Each view renders a component that already existed
   and keeps its own tests: the backlog and the bulk form are the two panels of
   the old page (`StewardshipScreen.tsx`, split, not changed), and Automation
   is `PlaybooksScreen` exactly as its own route rendered it. No endpoint,
   request body, role check or control's enablement moved -- the action map in
   `Docs/10-architecture/23-stewardship-action-map.md` lists every one.

   WHAT IS DELIBERATELY NOT HERE:
     * Task agents. Bounded agent execution is a different contract from
       human-authored playbook configuration, so Automation LINKS to the
       task-agent console instead of absorbing it.
     * Documentation and Business Meaning. They are workspaces of their own;
       nesting their tab bars under this one is what design §17 rules out.
       The Work queue links to Documentation's priorities.
     * The review queue. Maker-checker separation means no shortcut from here
       decides anything that queue owns.

   THREE THINGS THIS FILE HAS TO GET RIGHT:

   1. EVERY OLD LINK. `#/playbooks` resolves through `RETIRED_SCREEN_ALIASES`
      to `?view=automation`. `#/stewardship?action=certify&pattern=raw_%` was
      a link to the bulk form written before `view` existed, so its bulk
      fields are read as "Bulk actions" when no `view` says otherwise
      (`stewardshipViewFrom`). That inference is also why switching back to
      the Work queue while those fields are in the URL writes `view=queue`
      rather than dropping `view`: dropping it would re-infer Bulk actions.
      `?ids=` (17B, `CatalogScreen`'s row selection) is read the same way --
      it is the explicit-selection alternative to `field`/`pattern`, not an
      addition to it.

   2. AN UNRUN BULK ACTION SURVIVES A TAB SWITCH, OR IS ASKED ABOUT. A tab
      switch is `patchQuery`, which by design bypasses the shell's navigation
      guard (`App.tsx`), so the tab bar asks `lib/unsavedChanges` itself --
      the Documentation workspace's pattern. The bulk form reports an edited,
      unrun action into that registry; the Playbooks create form does not yet
      report, and is covered the day it does.

   3. THE KEYBOARD. One tab stop; Left/Right move between views (wrapping),
      Home/End jump to the ends, and a move that the unsaved-change prompt
      declines leaves focus where it was -- the ARIA tabs pattern `AskScreen`'s
      answer views already follow.
--------------------------------------------------------------------------- */

const StewardshipWorkQueue = lazy(() =>
  import("./StewardshipScreen").then((module) => ({ default: module.StewardshipWorkQueue })),
);
const StewardshipBulkActions = lazy(() =>
  import("./StewardshipScreen").then((module) => ({ default: module.StewardshipBulkActions })),
);
const PlaybooksScreen = lazy(() =>
  import("./PlaybooksScreen").then((module) => ({ default: module.PlaybooksScreen })),
);

/** The view axis. `queue` is the default, and is written as the ABSENCE of
 *  `?view=` so the canonical URL for the destination stays
 *  `#/steward/stewardship`. */
export const STEWARDSHIP_VIEWS = [
  {
    value: "queue",
    label: "Work queue",
    scope:
      "This organization — tables with no assigned owner and their escalation, and your own ownerships that are about to lapse.",
  },
  {
    value: "bulk",
    label: "Bulk actions",
    scope:
      "One datasource per run, from the sources the scope picker reaches. An action applies when you run it.",
  },
  {
    value: "automation",
    label: "Automation",
    scope:
      "This organization — a playbook may target any source in the tenant, and runs on its schedule or when run now.",
  },
] as const;

export type StewardshipView = (typeof STEWARDSHIP_VIEWS)[number]["value"];

const DEFAULT_VIEW: StewardshipView = "queue";

/** The bulk form's own filter fields. A link carrying any of them was written
 *  for the bulk form; `ds` is deliberately absent, because it is estate
 *  context inherited from every datasource-scoped screen and implies nothing
 *  about which view was meant. `ids` (17B) is the explicit-selection
 *  alternative to `field`/`pattern` that a Catalog row selection writes. */
const BULK_FIELDS = ["action", "field", "ids", "pattern"] as const;

function carriesBulkFilter(params: URLSearchParams): boolean {
  return BULK_FIELDS.some((field) => params.has(field));
}

/** Parse `?view=`, accepting every link that has ever opened this screen. An
 *  unknown value opens a view rather than nothing: a link from the future is
 *  still a link to stewardship. */
export function stewardshipViewFrom(params: URLSearchParams): StewardshipView {
  const raw = params.get("view");
  const direct = STEWARDSHIP_VIEWS.find((entry) => entry.value === raw);
  if (direct) return direct.value;
  return carriesBulkFilter(params) ? "bulk" : DEFAULT_VIEW;
}

/** Contextual links per view: where the adjacent job lives, on its own terms.
 *  Each target keeps its own scope and authorization; a link is a request. */
const VIEW_LINKS: Record<StewardshipView, { label: string; links: CrossLink[] }> = {
  queue: {
    label: "Related work",
    links: [
      {
        screen: "worklist",
        label: "Documentation priorities",
        title: "Which tables are worth documenting next, in the Documentation workspace.",
      },
      {
        screen: "negative-knowledge",
        label: "Rejected and suppressed assertions",
        title: "What reviewers already rejected, and whether each suppression is still in force.",
      },
    ],
  },
  bulk: { label: "Related work", links: [] },
  automation: {
    label: "Agent runs",
    links: [
      {
        screen: "task-agents",
        label: "Steward agent",
        params: { agent: "steward" },
        title: "Bounded agent runs are supervised in Task agents; playbooks here are rules a person wrote.",
      },
      {
        screen: "task-agents",
        label: "Lineage agent",
        params: { agent: "lineage" },
        title: "Bounded agent runs are supervised in Task agents; playbooks here are rules a person wrote.",
      },
      {
        screen: "task-agents",
        label: "Quality agent",
        params: { agent: "quality" },
        title: "Bounded agent runs are supervised in Task agents; playbooks here are rules a person wrote.",
      },
    ],
  },
};

const PANEL_ID = "stewws-panel";
const tabId = (view: StewardshipView) => `stewws-tab-${view}`;

export function StewardshipWorkspace() {
  const [params, setParams] = useUrlState();
  const view = stewardshipViewFrom(params);
  const confirmSwitch = useUnsavedNavigationGuard();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);

  const active = STEWARDSHIP_VIEWS.find((entry) => entry.value === view)!;
  const related = VIEW_LINKS[view];

  /** Switch views. False when the unsaved-change prompt was declined. */
  const show = useCallback(
    (next: StewardshipView): boolean => {
      if (next === view) return true;
      // The registry the shell asks before a real navigation. Asked here too,
      // because a tab switch is a `patchQuery` and bypasses the shell's own
      // choke point by design. Declining leaves the view where it was.
      if (!confirmSwitch()) return false;
      /* Only `view` is written. The bulk filter stays in the URL so coming
         back to Bulk actions finds it. Which is also why the default cannot
         always be spelled as an absent `view`: with a bulk filter present,
         absence reads as Bulk actions (see `stewardshipViewFrom`). */
      const spellDefaultAsAbsent = next === DEFAULT_VIEW && !carriesBulkFilter(params);
      setParams({ view: spellDefaultAsAbsent ? null : next });
      return true;
    },
    [view, confirmSwitch, params, setParams],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = STEWARDSHIP_VIEWS.length - 1;
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
    // Focus follows the selection, so a declined switch keeps both where they
    // were rather than leaving focus on a tab that is not the one shown.
    if (show(STEWARDSHIP_VIEWS[target]!.value)) tabs.current[target]?.focus();
  };

  return (
    <div className="stewws">
      <div className="stewws__tabs" role="tablist" aria-label="Stewardship views">
        {STEWARDSHIP_VIEWS.map((entry, index) => {
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
              /* Only the selected tab names the panel: the other views are not
                 mounted, and an `aria-controls` pointing at nothing is an
                 invalid reference rather than a hint. */
              aria-controls={selected ? PANEL_ID : undefined}
              tabIndex={selected ? 0 : -1}
              className={`stewws__tab${selected ? " stewws__tab--active" : ""}`}
              onClick={() => show(entry.value)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {entry.label}
            </button>
          );
        })}
      </div>
      {/* Which population the view in front reads. The three are not the same
          -- a bulk run is one datasource from the scope picker, a playbook may
          name any source in the tenant -- and a steward who finds a source
          missing from one of them is owed the reason. */}
      <p className="stewws__scope">{active.scope}</p>
      {related.links.length > 0 ? (
        <div className="stewws__links">
          <CrossLinks label={related.label} links={related.links} />
        </div>
      ) : null}

      <div className="stewws__panel" id={PANEL_ID} role="tabpanel" aria-labelledby={tabId(view)}>
        <Suspense
          fallback={
            <div className="stewws__loading" role="status">
              Loading {active.label.toLowerCase()}…
            </div>
          }
        >
          {view === "bulk" ? (
            <StewardshipBulkActions />
          ) : view === "automation" ? (
            <PlaybooksScreen />
          ) : (
            <StewardshipWorkQueue />
          )}
        </Suspense>
      </div>
    </div>
  );
}
