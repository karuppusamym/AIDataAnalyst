// Read-only accessibility audit of a RUNNING ui-next, every screen, both themes, plus reflow.
//
//   cd e2e && npm run audit:a11y -- http://localhost:3001 audit.json
//
// Why this exists (tracker R11-C2 / UX16). `ui-next/src/a11y-sweep.test.tsx` runs axe-core in
// jsdom, which loads no CSS, so colour contrast, focus visibility and reflow are outside it "by
// construction". This runs the same rules in a real Chromium with the real stylesheet, against a
// deployed stack's real data, over every screen in `SCREEN_IDS`:
//
//   * axe-core (WCAG 2.0/2.1/2.2 A and AA, contrast included) in the light AND the dark theme;
//   * WCAG 1.4.10 reflow: at 320 CSS px (a 1280px window at 400% zoom) the page must not scroll
//     sideways -- measured as the document being wider than its viewport.
//
// It only navigates and reads: it clicks nothing, submits nothing, and writes nothing to the stack.
// It sees what the deployment renders -- a region collapsed by default (the scope picker's fields),
// or a screen showing an empty state, is not audited in its other states. The journey suite's
// `tests/accessibility.spec.ts` is the CI guard for the screens its stub can populate; this is the
// wide, deployed-data pass. The last run (2026-09-19) found `--ink-3` failing on 36 of 37 screens.
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const baseUrl = process.argv[2] ?? "http://localhost:3001";
const outFile = process.argv[3] ?? "a11y-live-audit.json";
const axeSource = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const routes = fs.readFileSync(path.join(here, "..", "..", "ui-next", "src", "lib", "routes.ts"), "utf8");

// The screen list and each screen's journey come from the application's own route table, so a
// screen added tomorrow is audited tomorrow.
const ids = [
  ...routes.match(/export const SCREEN_IDS = \[([\s\S]*?)\] as const/)[1].matchAll(/"([^"]+)"/g),
].map((m) => m[1]);
const journeyBlock = routes.match(/export const SCREEN_JOURNEY[^{]*\{([\s\S]*?)\n\};/)[1];
const journey = {};
for (const m of journeyBlock.matchAll(/^\s*"?([\w-]+)"?:\s*"(\w+)"/gm)) journey[m[1]] = m[2];
for (const id of ids) if (!journey[id]) throw new Error(`no journey for screen ${id}`);

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const browser = await chromium.launch();
const results = { baseUrl, screens: ids.length, themes: {}, reflow: [], errors: [] };

async function open(context, id) {
  const page = await context.newPage();
  await page.goto(`${baseUrl}/#/${journey[id]}/${id}`, { waitUntil: "load" });
  await page.waitForSelector(".sview", { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1800); // data-dependent regions render after the screen mounts
  return page;
}

for (const theme of ["light", "dark"]) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, colorScheme: theme });
  const perScreen = {};
  for (const id of ids) {
    let page;
    try {
      page = await open(context, id);
      const boundary = await page.locator('[data-testid="route-error"]').count();
      await page.evaluate(axeSource);
      const raw = await page.evaluate(
        async (tags) =>
          // eslint-disable-next-line no-undef
          await axe.run(document, { runOnly: { type: "tag", values: tags }, resultTypes: ["violations"] }),
        TAGS,
      );
      perScreen[id] = {
        boundary,
        violations: raw.violations.map((v) => ({
          rule: v.id,
          impact: v.impact,
          nodes: v.nodes.length,
          sample: v.nodes.slice(0, 3).map((n) => ({
            target: n.target.join(" "),
            summary: (n.any[0]?.message ?? n.failureSummary ?? "").slice(0, 200),
            data: n.any[0]?.data
              ? {
                  fg: n.any[0].data.fgColor,
                  bg: n.any[0].data.bgColor,
                  ratio: n.any[0].data.contrastRatio,
                  expected: n.any[0].data.expectedContrastRatio,
                }
              : undefined,
          })),
        })),
      };
    } catch (error) {
      results.errors.push({ theme, id, error: String(error).slice(0, 200) });
    } finally {
      await page?.close();
    }
  }
  results.themes[theme] = perScreen;
  await context.close();
}

const narrow = await browser.newContext({ viewport: { width: 320, height: 256 }, colorScheme: "light" });
for (const id of ids) {
  let page;
  try {
    page = await open(narrow, id);
    const m = await page.evaluate(() => {
      const de = document.documentElement;
      const wide = [];
      const clipped = (el) => {
        for (let n = el; n && n !== document.body; n = n.parentElement) {
          const ox = getComputedStyle(n).overflowX;
          if (ox === "auto" || ox === "scroll" || ox === "hidden") return true;
        }
        return false;
      };
      for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > de.clientWidth + 1 && getComputedStyle(el).position !== "fixed") {
          if (!clipped(el.parentElement)) wide.push(`${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}`);
        }
      }
      return { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, offenders: [...new Set(wide)].slice(0, 6) };
    });
    results.reflow.push({ id, ...m, overflows: m.scrollWidth > m.clientWidth + 1 });
  } catch (error) {
    results.errors.push({ theme: "reflow", id, error: String(error).slice(0, 200) });
  } finally {
    await page?.close();
  }
}
await narrow.close();
await browser.close();

fs.writeFileSync(outFile, JSON.stringify(results, null, 2));
const bad = (themeMap) => Object.values(themeMap).filter((s) => s.violations.length > 0).length;
console.log(
  `${ids.length} screens | light: ${bad(results.themes.light)} with violations | dark: ${bad(results.themes.dark)} with violations | ` +
    `reflow: ${results.reflow.filter((r) => r.overflows).length} scroll sideways | errors: ${results.errors.length} | written ${outFile}`,
);
process.exitCode =
  bad(results.themes.light) + bad(results.themes.dark) + results.reflow.filter((r) => r.overflows).length +
    results.errors.length >
  0
    ? 1
    : 0;
