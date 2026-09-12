import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { MeRead } from "./lib/types";
import { axeViolations, unnamedFocusableElements } from "./test/a11y";

/* ---------------------------------------------------------------------------
   R11-C2 — every navigable screen, swept (carrying F21 · UX-5 · TS-9).

   WHY A SWEEP AND NOT 44 HAND-WRITTEN CASES. UX-5's remediation was done
   against `ui/`, the previous UI, which no longer exists in this tree. The
   work was real and is recorded in the capability register; none of it
   survives into `ui-next`, and nothing was checking whether the replacement
   had inherited any of it. A per-screen suite that has to be remembered is a
   suite that a 45th screen is added without — so this one derives its subject
   list from the shell's own navigation, and a new screen is swept the day it
   is reachable.

   WHAT IT PROVES. Each screen is opened the way a user opens it (by URL,
   through the real shell, with its real providers and its lazy chunk) and
   then held to two checks:

     * no WCAG 2.1 A/AA violation that axe-core can detect, and
     * no focusable control without an accessible name -- which axe does not
       fully cover, because `button-name` and `link-name` say nothing about a
       focusable `role="button"` on an SVG `<g>`, and this app draws both of
       its lineage diagrams that way.

   WHAT IT DOES NOT PROVE, and must not be read as proving:

     * **Populated states.** These run against the bundled demo estate, so a
       screen that needs a datasource chosen is swept in its empty or prompt
       state. The markup behind a filled table is covered only where that
       screen's own test file renders it.
     * **Anything requiring rendering.** No CSS is loaded in jsdom, so
       contrast, focus visibility, reflow at 400% zoom and the off-screen
       drawer fix are all outside these assertions, by construction.
     * **That a screen reader makes sense of it.** A correct accessibility
       tree is necessary and not sufficient; see
       `Docs/60-delivery/24-accessibility-acceptance-2026-09-12.md`.
--------------------------------------------------------------------------- */

const fetchMe = vi.fn<() => Promise<MeRead>>();
vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return { ...actual, fetchMe: () => fetchMe() };
});

/**
 * `[screen id, the label the shell gives its region, query]`, from `App.tsx`'s NAV.
 *
 * R11-S10 added the third column. Two screens absorbed other screens and now
 * select between them with a query field, so sweeping only their default view
 * would quietly drop surfaces this suite used to cover: the parsed-lineage
 * queue was its own route, and so were the lineage and quality agent consoles.
 * They are rows here rather than routes, and are swept exactly as before.
 */
const SCREENS: ReadonlyArray<readonly [string, string, string?]> = [
  ["home", "Overview"],
  ["inbox", "Agent inbox"],
  ["analyst", "Ask Atlas"],
  ["catalog", "Catalog"],
  ["semantics", "Semantic layer"],
  ["tools", "Tool registry"],
  ["tool-plans", "Tool plans"],
  ["lineage", "Lineage"],
  ["unified-lineage", "Unified lineage"],
  ["marketplace", "Marketplace"],
  ["portfolio-analytics", "Portfolio analytics"],
  ["context", "Context products"],
  ["developer", "Agent gateway"],
  ["stewardship", "Stewardship"],
  ["worklist", "Documentation worklist"],
  ["task-agents", "Task agents"],
  ["task-agents", "Task agents", "?agent=lineage"],
  ["task-agents", "Task agents", "?agent=quality"],
  ["playbooks", "Playbooks"],
  ["negative-knowledge", "Negative knowledge"],
  ["meaning", "Business meaning"],
  ["description-drafts", "Description drafts"],
  ["data-dictionaries", "Data dictionaries"],
  ["relationships", "Relationships"],
  ["cross-source", "Cross-source"],
  ["transformations", "Transformations"],
  ["quality", "Data quality"],
  ["studio", "Studio"],
  ["governance", "Review queue"],
  ["governance", "Review queue", "?queue=parsed-lineage"],
  ["refusals", "Policy refusals"],
  ["reviewer-agent", "Reviewer agent"],
  ["sources", "Sources"],
  ["operations", "Operations"],
  ["agents", "AI governance"],
  ["ai", "AI registry"],
  ["agent-roster", "Agent roster"],
  ["access-policies", "Access policies"],
  ["workspace-access", "Workspace access"],
  ["delegations", "Delegations"],
  ["reliability", "Reliability"],
  ["administration", "Administration"],
  ["audit", "Audit ledger"],
  ["compliance", "Compliance packs"],
];

beforeEach(() => {
  fetchMe.mockReset();
  vi.resetModules();
});

/** How long either wait below may take before the case is called a failure.
 *
 * One constant for both, because they wait on the two halves of the same
 * race -- the lazy chunk arriving, then its first data render finishing --
 * and letting them drift apart is what produced the intermittent failure
 * this value was measured against. See the note at the `waitFor`. */
const WAIT = 15000;

describe("every navigable screen is accessible", () => {
  it.each(SCREENS.map((row) => [`${row[0]}${row[2] ?? ""}`, ...row] as const))(
    "%s has no detectable WCAG A/AA violation and names every focusable control",
    async (_title, id, label, query = "") => {
      history.replaceState(null, "", `/${query}#/${id}`);
      fetchMe.mockReturnValue(new Promise(() => {}));
      const { default: App } = await import("./App");
      const { container } = render(<App />);

      // `findByRole`, not `getByRole`: every screen but two arrives as a lazy
      // chunk, so on the tick `render` returns, the region is a Suspense
      // fallback. This is the race the brief for this row warns about, and
      // asserting here without awaiting is how it becomes intermittent.
      const region = await screen.findByRole("region", { name: label }, { timeout: WAIT });
      // Waiting out the lazy chunk, and the reason the budget is `WAIT` and
      // not `@testing-library`'s 1s default (R11-D13, 2026-09-12).
      //
      // This `waitFor` was the one place in the file that took the default,
      // while the `findByRole` above it had always asked for 5s against the
      // very same race. The vitest 2 -> 5 upgrade turned that inconsistency
      // into a failing gate: vitest 5 overlaps test files more aggressively
      // than vitest 2 did (this suite reports ~151s of environment time
      // inside a ~65s run), and this file is the heaviest in the suite -- it
      // mounts the whole application 44 times and runs axe-core over each.
      // Measured, not guessed: on vitest 2 three consecutive full-suite runs
      // were green; on vitest 5, 1s failed twice in three runs and 5s still
      // failed once, each time on a *different* screen (`catalog`,
      // `reliability`, then `relationships`) and never when this file ran
      // alone, which is the signature of contention rather than a broken
      // screen.
      //
      // So the assertions are untouched and the window is sized for the
      // worst case instead of the quiet case. It is still a bounded wait: a
      // screen that genuinely never loads fails here exactly as before, and
      // the per-test timeout at the foot of this block caps the whole case.
      await waitFor(
        () => {
          expect(within(region).queryByText(/^Loading /)).toBeNull();
        },
        { timeout: WAIT },
      );

      const violations = await axeViolations(container);
      expect(
        violations.map(
          (violation) =>
            `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
        ),
      ).toEqual([]);

      expect(
        unnamedFocusableElements(container).map(
          (element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`,
        ),
      ).toEqual([]);
    },
    // Two `WAIT`s plus an axe-core pass, with headroom; raised from 20000
    // when `WAIT` went to 15000 so that the case reports the assertion that
    // actually failed rather than a bare per-test timeout.
    45000,
  );
});
