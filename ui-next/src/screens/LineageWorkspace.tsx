import { Suspense, lazy, useCallback } from "react";
import { useUrlState } from "../lib/useUrlState";
import { useUnsavedNavigationGuard } from "../lib/unsavedChanges";
import "./LineageWorkspace.css";

/* ---------------------------------------------------------------------------
   One lineage destination — R11-S13 (M1), review 2026-09-16 §3.

   TWO SIDEBAR ITEMS FOR ONE QUESTION. "Lineage" and "Unified lineage" sat next
   to each other in the Analyst group, both scoped by `?ds=`, both selecting a
   node with `?node=`, both answering "what does this asset depend on and what
   depends on it". Which one to open was a coin toss, and the answer you got
   depended on the toss: one narrated the traversal in sentences, the other
   drew the merged graph and listed bounded impact beside it.

   THREE VIEWS OF ONE QUESTION, on one `?view=` axis:

     explain  the traversal in words, one hop at a time, with the evidence that
              produced each hop -- plus the bounded diagram of the asset's
              neighbourhood, which has a table rendering of the same edges so
              the relationships are not mouse-only.
     graph    the merged FK + suggested + dbt + OpenLineage + view/procedure
              graph, with the datasource/domain scope axis, the layer filters
              and the topology/nodes/edges sub-axis.
     impact   the same merged graph with bounded upstream/downstream impact
              LEADING the page rather than sitting in a 360px rail: the view
              for "who breaks if I change this", which is the question the
              narrow rail made hardest to read.

   WHAT THIS FILE IS. A shell. Both screens are mounted as they were -- their
   endpoints, their scope cuts (`UnifiedLineageScreen`'s docstring documents
   deliberate cuts against the retired graph engine; they are preserved, not
   re-litigated) and their own tests are unchanged. What is new here is the
   `view` discriminator and the two things it has to get right:

   1. EVERY LEGACY SPELLING OF `?view=` STILL RESOLVES. `lineage` already owned
      a `view` field with two values, and one of them collides with the new
      axis by name. `LINEAGE_VIEW_ALIASES` maps them, and
      `lineageViewFrom` is the only place `?view=` is read:

        (absent)          -> explain   the page `#/lineage` always opened
        view=narrated     -> explain   the value the old tab bar named
        view=graph        -> graph     "show me the graph" -- now answered by
                                       the merged graph, which is a superset of
                                       the bounded one it used to open

      A route-level alias cannot express this: `RETIRED_SCREEN_ALIASES` maps a
      PATH to a screen, and `#/lineage` is not a retired path. The value map
      has to live with the screen that reads the value, which is this one.

   2. `#/unified-lineage` OPENS THE GRAPH. That path IS retired, so it goes in
      `RETIRED_SCREEN_ALIASES` with `view=graph` -- see `lib/routes.ts`. Every
      field either screen declared (`depth`, `direction`, `dom`, `ds`, `node`,
      `scope`, `tab`) is declared on the merged screen, or `normalizeLocation`
      would drop it from the very bookmark that carried it.

   THE WRITE SURFACE IS NOT BURIED. The withheld-cross-boundary banner and the
   `CrossBoundaryGrants` request path are ADR-0017 §4's answer to an
   incomplete domain graph, and they are the only write on either screen. They
   live inside `UnifiedLineageScreen`, which both the `graph` and the `impact`
   views mount -- so they are on screen in two of the three views and behind no
   sub-tab. The `explain` view is datasource-scoped and has no domain graph, so
   there is nothing withheld there to report.
--------------------------------------------------------------------------- */

const NarratedLineageScreen = lazy(() =>
  import("./NarratedLineageScreen").then((module) => ({
    default: module.NarratedLineageScreen,
  })),
);
const UnifiedLineageScreen = lazy(() =>
  import("./UnifiedLineageScreen").then((module) => ({
    default: module.UnifiedLineageScreen,
  })),
);

export const LINEAGE_VIEWS = [
  { value: "explain", label: "Explain" },
  { value: "graph", label: "Graph" },
  { value: "impact", label: "Impact" },
] as const;

export type LineageView = (typeof LINEAGE_VIEWS)[number]["value"];

const DEFAULT_VIEW: LineageView = "explain";

/**
 * The `?view=` values this screen has ever answered to, mapped onto the axis.
 *
 * Nothing is removed from this table, for the same reason nothing is removed
 * from `RETIRED_SCREEN_ALIASES`: a 404 -- or worse, a silent redirect to the
 * default view -- on a link somebody saved is the failure the merge has to
 * avoid.
 */
export const LINEAGE_VIEW_ALIASES: Readonly<Record<string, LineageView>> = {
  // The value the retired two-tab bar on `#/lineage` wrote for its default.
  narrated: "explain",
  // `#/lineage?view=graph` opened the bounded per-datasource diagram. The
  // merged graph answers the same request with strictly more evidence, so the
  // spelling is kept and the destination improved rather than broken.
  graph: "graph",
};

/** Parse `?view=`, accepting every spelling that has ever worked. An unknown
 *  value opens the default view rather than rendering nothing: a link from the
 *  future is still a link to lineage. */
export function lineageViewFrom(raw: string | null): LineageView {
  if (!raw) return DEFAULT_VIEW;
  const direct = LINEAGE_VIEWS.find((entry) => entry.value === raw);
  if (direct) return direct.value;
  return LINEAGE_VIEW_ALIASES[raw] ?? DEFAULT_VIEW;
}

export function LineageWorkspace() {
  const [params, setParams] = useUrlState();
  const view = lineageViewFrom(params.get("view"));
  const confirmSwitch = useUnsavedNavigationGuard();

  const active = LINEAGE_VIEWS.find((entry) => entry.value === view)!;

  const show = useCallback(
    (next: LineageView) => {
      if (next === view) return;
      /* A tab switch is `patchQuery`, which deliberately bypasses the shell's
         own navigation guard (`App.tsx`). Neither lineage view holds an
         editable form today, so this asks and gets `true`; it is wired anyway
         because the next thing added to one of these views should inherit the
         guard rather than need someone to remember it. */
      if (!confirmSwitch()) return;
      /* Only `view` is written. `ds`, `node`, `depth`, `direction`, `scope`,
         `dom` and `tab` are the question, not the view of it -- carrying them
         across is what makes these three views of ONE question rather than
         three screens that happen to share a URL. Explain and Impact both read
         `depth`; Graph and Impact both read `node`. */
      setParams({ view: next === DEFAULT_VIEW ? null : next });
    },
    [view, confirmSwitch, setParams],
  );

  return (
    <div className="linws">
      <div className="linws__tabs" role="tablist" aria-label="Lineage views">
        {LINEAGE_VIEWS.map((entry) => (
          <button
            key={entry.value}
            type="button"
            role="tab"
            aria-selected={view === entry.value}
            className={`linws__tab${view === entry.value ? " linws__tab--active" : ""}`}
            onClick={() => show(entry.value)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div className="linws__panel">
        <Suspense
          fallback={
            <div className="linws__loading" role="status">
              Loading {active.label.toLowerCase()}…
            </div>
          }
        >
          {view === "explain" ? (
            <NarratedLineageScreen />
          ) : (
            /* One element type across `graph` and `impact`, deliberately:
               React reconciles it rather than remounting, so switching between
               them keeps the loaded graph, the layer chips and the asset
               filters instead of re-fetching the estate to change which pane
               leads. */
            <UnifiedLineageScreen lead={view === "impact" ? "impact" : "graph"} />
          )}
        </Suspense>
      </div>
    </div>
  );
}
