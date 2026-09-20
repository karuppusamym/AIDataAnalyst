/* ---------------------------------------------------------------------------
   What jsdom cannot see: colour contrast and reflow, in a real browser with
   the real stylesheet, in both themes (tracker R11-C2 / UX16).

   `ui-next/src/a11y-sweep.test.tsx` sweeps every screen with axe-core, and says in
   its own header what it cannot prove: no CSS is loaded in jsdom, so "contrast,
   focus visibility, reflow at 400% zoom ... are all outside these assertions, by
   construction". So they were outside every assertion, and a real-browser run on
   2026-09-19 found the consequence: `--ink-3` (tertiary text) failed WCAG AA on 36
   of 37 screens in the light theme (4.14:1 on `--sunk`, 3.86:1 on the always-dark
   sidebar), warning text (`--warn`) failed as text everywhere it was used, two
   dark-theme rows had been made quieter with `opacity`, and two regions scrolled
   sideways with no way for a keyboard to reach them.

   This holds the screens the journey suite can populate to the same standard in
   both themes, so a token that drifts under 4.5:1 is a red build rather than a
   report. It runs against the production nginx image like every other case here.

   It is a sample, not a sweep: the stub populates a handful of screens, and a
   screen the stub cannot render is one this suite cannot check. The full audit
   (all 37 screens, both themes, reflow at 320 CSS px) is a script run against a
   deployed stack -- see the R11-C2 row -- and is what found the above.

   What it does not prove: that a screen reader makes sense of anything. That is a
   human check (`Docs/60-delivery/24-accessibility-acceptance-2026-09-12.md`).
--------------------------------------------------------------------------- */

import { createRequire } from "node:module";

import type { Page } from "@playwright/test";

import { expect, expectScreen, test } from "../support/journey";
import type { Identity } from "../support/journey";

const AXE = createRequire(import.meta.url).resolve("axe-core/axe.min.js");

/** The WCAG 2.x A and AA rules axe-core knows, the same tag set the jsdom sweep uses. */
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

interface Target {
  readonly name: string;
  readonly identity: Identity;
  readonly hash: string;
  readonly screen: string;
}

const TARGETS: readonly Target[] = [
  { name: "Home", identity: "connector", hash: "/#/home", screen: "home" },
  { name: "Administration", identity: "connector", hash: "/#/administration", screen: "administration" },
  { name: "Stewardship work queue", identity: "steward", hash: "/#/steward/stewardship", screen: "stewardship" },
  { name: "Stewardship bulk actions", identity: "steward", hash: "/?view=bulk#/steward/stewardship", screen: "stewardship" },
  { name: "Stewardship automation", identity: "steward", hash: "/?view=automation#/steward/stewardship", screen: "stewardship" },
  { name: "Documentation worklist", identity: "steward", hash: "/#/description-drafts", screen: "worklist" },
  { name: "Governance review queue", identity: "reviewer", hash: "/#/governance", screen: "governance" },
  { name: "Ask", identity: "analyst", hash: "/#/analyst", screen: "analyst" },
  { name: "Audit ledger", identity: "auditor", hash: "/#/audit", screen: "audit" },
];

async function violations(page: Page): Promise<string[]> {
  await page.addScriptTag({ path: AXE });
  const found = await page.evaluate(
    async (tags) => {
      // @ts-expect-error -- `axe` is the global the injected script defines.
      const result = await axe.run(document, {
        runOnly: { type: "tag", values: tags },
        resultTypes: ["violations"],
      });
      return result.violations.map(
        (v: { id: string; nodes: { target: string[]; any: { data?: { fgColor?: string; bgColor?: string; contrastRatio?: number } }[] }[] }) =>
          `${v.id} x${v.nodes.length}: ` +
          v.nodes
            .slice(0, 3)
            .map((n) => {
              const d = n.any[0]?.data;
              const colours = d?.contrastRatio ? ` (${d.fgColor} on ${d.bgColor} = ${d.contrastRatio})` : "";
              return n.target.join(" ") + colours;
            })
            .join(" | "),
      );
    },
    TAGS,
  );
  return found;
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`accessibility in a real browser -- ${theme} theme`, () => {
    for (const target of TARGETS) {
      test.describe(target.name, () => {
        test.use({ identity: target.identity });

        test(`has no WCAG AA violation, contrast included`, async ({ page }) => {
          await page.emulateMedia({ colorScheme: theme });
          await page.goto(target.hash);
          await expectScreen(page, target.screen);
          // Data-dependent regions render after the screen mounts.
          await page.waitForLoadState("networkidle");

          expect(await violations(page)).toEqual([]);
        });
      });
    }
  });
}

test.describe("reflow at 320 CSS pixels (WCAG 1.4.10) -- the width of a 1280px window at 400% zoom", () => {
  for (const target of TARGETS) {
    test.describe(target.name, () => {
      test.use({ identity: target.identity });

      test("does not scroll the page sideways", async ({ page }) => {
        await page.setViewportSize({ width: 320, height: 256 });
        await page.goto(target.hash);
        await expectScreen(page, target.screen);
        await page.waitForLoadState("networkidle");

        const { scrollWidth, clientWidth } = await page.evaluate(() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        }));
        expect(scrollWidth, `the page is ${scrollWidth}px wide in a ${clientWidth}px viewport`).toBeLessThanOrEqual(
          clientWidth + 1,
        );
      });
    });
  }
});
