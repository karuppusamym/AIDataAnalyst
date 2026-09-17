import { Suspense, lazy, useCallback } from "react";
import { useUrlState } from "../lib/useUrlState";
import { useUnsavedNavigationGuard } from "../lib/unsavedChanges";
import "./DocumentationWorkspace.css";

/* ---------------------------------------------------------------------------
   One documentation workspace — R11-S13 (M3), review 2026-09-16 §3.

   THREE SIDEBAR ITEMS FOR ONE JOB. A steward documenting an estate had
   "Documentation worklist", "Description drafts" and "Data dictionaries" as
   three peer destinations, and the order they have to be used in was written
   down nowhere: the worklist says WHICH tables matter, the drafts screen is
   where their descriptions get written and submitted, and a data dictionary
   import is the bulk way to answer the same question from a file another tool
   exported. Three names for three steps of one task is what the review's §3
   calls a journey that was never consolidated.

   THE MERGE IS A SHELL, NOT A REWRITE. Each tab renders the screen that
   already existed, unchanged and still lazily loaded, so every action, every
   endpoint and every one of their own tests keeps working. What this file adds
   is the tab axis and the two things that axis has to get right:

   1. TWO SCOPES, NOT ONE. Priorities and Drafts are ORGANIZATION-scoped
      (`useOrgId`); Imports is PROJECT-scoped (`useScopeSelection`), because a
      data dictionary belongs to a project and its rows are matched against
      that project's sources. Merging them did NOT mean picking one: each tab
      keeps reading the scope it was written against, both of which are
      shell-level providers, and the note under the tab bar says which scope
      the tab in front is reading so the answer is never a guess.

   2. AN UNSAVED DRAFT SURVIVES A TAB SWITCH, OR IS ASKED ABOUT.
      `DescriptionEditor` reports its dirty state into `lib/unsavedChanges`,
      and the shell consults that registry at ONE choke point
      (`App.tsx`'s `navigate`) before every `pushLocation`. A tab switch here
      is `patchQuery` -- a filter edit, deliberately not navigation -- so it
      does NOT pass that choke point. Left alone, switching from Drafts to
      Imports with a half-written description would discard it in silence:
      the tab unmounts, the reporter unregisters, and nothing asked. So the
      tab bar asks the same registry itself, which is exactly the "guard its
      OWN action -- closing a drawer, switching a tab" case
      `useUnsavedChanges` documents. The reporter stays registered inside the
      Drafts tab where the editor is; it is not lifted up here.

   ROUTES. `worklist` is the surviving screen id, so `#/steward/worklist` is
   unchanged. `#/description-drafts` and `#/data-dictionaries` resolve through
   `RETIRED_SCREEN_ALIASES` to this screen with the tab they named already
   selected -- see `lib/routes.ts`, whose table nothing is ever removed from.
--------------------------------------------------------------------------- */

const DocumentationWorklistScreen = lazy(() =>
  import("./DocumentationWorklistScreen").then((module) => ({
    default: module.DocumentationWorklistScreen,
  })),
);
const DescriptionDraftsScreen = lazy(() =>
  import("./DescriptionDraftsScreen").then((module) => ({
    default: module.DescriptionDraftsScreen,
  })),
);
const DataDictionariesScreen = lazy(() =>
  import("./DataDictionariesScreen").then((module) => ({
    default: module.DataDictionariesScreen,
  })),
);

/** The tab axis. `priorities` is the default, and is written as the ABSENCE of
 *  `?view=` so the canonical URL for the screen stays `#/steward/worklist`. */
export const DOCUMENTATION_VIEWS = [
  {
    value: "priorities",
    label: "Priorities",
    scope: "This organization — which tables are worth documenting next.",
  },
  {
    value: "drafts",
    label: "Drafts",
    scope: "This organization — descriptions being written and submitted for review.",
  },
  {
    value: "imports",
    label: "Imports",
    scope: "The project selected in the scope picker — a dictionary belongs to one project.",
  },
] as const;

export type DocumentationView = (typeof DOCUMENTATION_VIEWS)[number]["value"];

const DEFAULT_VIEW: DocumentationView = "priorities";

/** Parse `?view=`, tolerating anything else. An unknown value is a link from
 *  the future or a typo, and either way the workspace must open rather than
 *  render nothing. */
export function documentationViewFrom(raw: string | null): DocumentationView {
  const match = DOCUMENTATION_VIEWS.find((entry) => entry.value === raw);
  return match ? match.value : DEFAULT_VIEW;
}

export function DocumentationWorkspace() {
  const [params, setParams] = useUrlState();
  const view = documentationViewFrom(params.get("view"));
  const confirmSwitch = useUnsavedNavigationGuard();

  const active = DOCUMENTATION_VIEWS.find((entry) => entry.value === view)!;

  const show = useCallback(
    (next: DocumentationView) => {
      if (next === view) return;
      // The registry the shell asks before a real navigation. Asked here too,
      // because a tab switch is a `patchQuery` and bypasses the shell's own
      // choke point by design. Declining leaves the tab where it was.
      if (!confirmSwitch()) return;
      /* Each tab owns different query fields (`ranking`/`zero`, `focus`/`type`,
         `document`), and they do not collide -- so leaving them in place is
         what lets a steward come back to the tab they left and find their
         filter and their selected document still there. `view` is the only
         field a tab switch writes; `null` for the default keeps the canonical
         URL free of a redundant field. */
      setParams({ view: next === DEFAULT_VIEW ? null : next });
    },
    [view, confirmSwitch, setParams],
  );

  return (
    <div className="docws">
      <div className="docws__tabs" role="tablist" aria-label="Documentation views">
        {DOCUMENTATION_VIEWS.map((entry) => (
          <button
            key={entry.value}
            type="button"
            role="tab"
            aria-selected={view === entry.value}
            className={`docws__tab${view === entry.value ? " docws__tab--active" : ""}`}
            onClick={() => show(entry.value)}
          >
            {entry.label}
          </button>
        ))}
      </div>
      {/* Which scope the tab in front is reading. The two axes are a real
          difference between these surfaces, not a detail to smooth over: a
          steward who has just filtered Priorities across the organization and
          finds Imports empty is owed the reason. */}
      <p className="docws__scope">{active.scope}</p>

      <div className="docws__panel">
        <Suspense
          fallback={
            <div className="docws__loading" role="status">
              Loading {active.label.toLowerCase()}…
            </div>
          }
        >
          {view === "drafts" ? (
            <DescriptionDraftsScreen />
          ) : view === "imports" ? (
            <DataDictionariesScreen />
          ) : (
            <DocumentationWorklistScreen />
          )}
        </Suspense>
      </div>
    </div>
  );
}
