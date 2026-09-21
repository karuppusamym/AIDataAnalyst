// Read-only rehearsal of the multi-user demo: every screen in each demo user's own menu, opened as
// that user, against the per-user UIs that `scripts/demo-users.ps1 -Action Start` runs.
//
//   .\scripts\demo-users.ps1 -Action Start          (then wait ten seconds)
//   cd e2e && node scripts/demo-rehearsal.mjs [out-dir]
//
// Why it exists. `scripts/live_role_matrix.py` proves the API refuses and admits the right roles. It
// cannot tell whether the SCREEN a persona is sent to survives that refusal: the Auditor menu once
// held a screen whose only read route refused Auditors, and a user who cannot list organizations
// cannot pick one. This opens each screen in a real Chromium, as the user, and records
//
//   * every /v1 response that is not 2xx (a 403 on a screen's own read is a role/screen mismatch, a
//     5xx is a defect; a 404 for a route that takes an id is usually just an empty estate);
//   * any visible "could not be loaded" or "role is required" text;
//   * a screenshot per screen.
//
// It only navigates: it clicks nothing, submits nothing and writes nothing to the stack. Each user
// starts from an empty browser, exactly as a presenter's first visit does, so it also checks what
// the launcher promises: the shell opens as that user's persona, and every request goes to the
// Northwind organization rather than to the placeholder id. `--preset-org` restores the old
// behaviour (the organization pasted into localStorage) for comparison.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(process.argv[2] ?? "demo-rehearsal");
const api = process.env.ATLAS_API ?? "http://localhost:8000";

const routes = fs.readFileSync(path.join(here, "..", "..", "ui-next", "src", "lib", "routes.ts"), "utf8");
const journeyBlock = routes.match(/export const SCREEN_JOURNEY[^{]*\{([\s\S]*?)\n\};/)[1];
const journey = {};
for (const m of journeyBlock.matchAll(/^\s*"?([\w-]+)"?:\s*"(\w+)"/gm)) journey[m[1]] = m[2];
const screensOf = (group) => Object.keys(journey).filter((id) => journey[id] === group);

// user, port, persona the shell should open as, screens to open (the user's own persona menu, then
// anything the demo script sends them to)
const USERS = [
  ["alex.operator", 5181, "Operator", [...screensOf("operator"), "audit", "governance"]],
  ["dana.steward", 5182, "Steward", [...screensOf("steward"), "governance"]],
  ["riya.reviewer", 5183, "Reviewer", screensOf("reviewer")],
  ["omar.auditor", 5184, "Auditor", screensOf("auditor")],
  ["ana.analyst", 5185, "Analyst", [...screensOf("analyst"), ...screensOf("consumer")]],
  ["vic.viewer", 5186, "Analyst", ["audit", "catalog"]],
  ["ravi.dataadmin", 5187, "Operator", ["sources", "quality", "refusals"]],
  ["sam.agentdev", 5188, "Analyst", [...screensOf("developer"), ...screensOf("inbox"), "tools", "tool-plans"]],
];
const PLACEHOLDER_ORG = "00000000-0000-0000-0000-000000000001";
const presetOrg = process.argv.includes("--preset-org");

async function sampleOrg() {
  const res = await fetch(`${api}/v1/organizations`, {
    headers: { "X-Principal-Id": "rehearsal", "X-Roles": "PlatformAdmin" },
  });
  const items = (await res.json()).items;
  const org = items.find((o) => o.slug === "sample-bank");
  if (!org) throw new Error("no sample-bank organization");
  return org.id;
}

const ERROR_TEXT =
  /could not be loaded|role is required|roles is required|You do not have access|Something went wrong|Cannot read properties|is not a function|Route error/i;

const orgId = await sampleOrg();
fs.mkdirSync(outDir, { recursive: true });
const browser = await chromium.launch();
const report = [];

for (const [user, port, persona, screens] of USERS) {
  const base = `http://localhost:${port}`;
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  if (presetOrg) {
    await context.addInitScript((id) => {
      try {
        localStorage.setItem("atlas.org.id", id);
      } catch {
        /* storage blocked */
      }
    }, orgId);
  }
  fs.mkdirSync(path.join(outDir, user), { recursive: true });
  for (const id of [...new Set(screens)]) {
    const page = await context.newPage();
    const failures = [];
    page.on("response", (r) => {
      const url = r.url();
      if (url.includes("/v1/") && r.status() >= 400) failures.push(`${r.status()} ${new URL(url).pathname}`);
    });
    const consoleErrors = [];
    const orgsSent = new Set();
    page.on("request", (req) => {
      const url = req.url();
      if (!url.includes("/v1/")) return;
      const header = req.headers()["x-organization-id"];
      if (header) orgsSent.add(header);
      const inPath = url.match(/\/organizations\/([0-9a-f-]{36})/i);
      if (inPath) orgsSent.add(inPath[1]);
    });
    page.on("pageerror", (e) => consoleErrors.push(String(e).slice(0, 120)));
    let reachable = true;
    try {
      await page.goto(`${base}/#/${journey[id]}/${id}`, { waitUntil: "load", timeout: 20000 });
      await page.waitForSelector(".sview", { timeout: 15000 }).catch(() => {});
      await page.waitForTimeout(1800);
    } catch {
      reachable = false;
    }
    const text = reachable ? await page.evaluate(() => document.body.innerText) : "";
    const shown = [...new Set([...text.matchAll(new RegExp(ERROR_TEXT, "gi"))].map((m) => m[0]))];
    const shownPersona = reachable ? await page.locator("#persona").inputValue().catch(() => null) : null;
    const wrongOrg = [...orgsSent].filter((o) => o !== orgId);
    const shot = path.join(outDir, user, `${id}.png`);
    if (reachable) await page.screenshot({ path: shot });
    report.push({
      user, screen: id, reachable, failures: [...new Set(failures)], shown, consoleErrors,
      persona: shownPersona, personaWrong: shownPersona !== null && shownPersona !== persona,
      wrongOrg: wrongOrg.map((o) => (o === PLACEHOLDER_ORG ? `${o} (placeholder)` : o)),
    });
    await page.close();
  }
  await context.close();
}
await browser.close();

// Refusals that are the point of the demo, or that the shell handles, or that are recorded debt.
// They are printed as `known` with the reason, and do not fail the run. Anything NOT listed here
// is an issue: that is what makes the exit code mean something.
const KNOWN = [
  // The shell always asks for the organization list; only four roles may have it, and the shell
  // falls back to the organization in the caller's identity.
  { pattern: /^403 \/v1\/organizations$/, why: "organization list is admin-only; the shell uses the identity's organization" },
  // Roles without workspace access see this line in the scope panel, and browse without a workspace.
  { pattern: /^403 \/v1\/organizations\/[0-9a-f-]+\/workspaces$/, why: "role has no workspace access; the scope line says so" },
  { pattern: /^text: could not be loaded$/, screen: /.*/, user: /^(omar\.auditor|vic\.viewer)$/, why: "the scope line: no access to workspaces" },
  // Act 5 of the demo script: a Viewer refused on the audit ledger, by name, on purpose.
  { user: /^vic\.viewer$/, screen: /^audit$/, pattern: /audit-events|roles? is required|could not be loaded/, why: "least privilege on purpose (demo Act 5)" },
  // The BI integration is a feature flag that is off.
  { user: /^alex\.operator$/, screen: /^workspace-access$/, pattern: /bi-connections|could not be loaded/, why: "BI integration is disabled" },
  // Recorded debt (tracker R11-AUD01): the screen calls a route this role bundle is not admitted to.
  { user: /^sam\.agentdev$/, screen: /^context$/, pattern: /ontology-versions|roles? is required/, why: "AgentDeveloper cannot read ontology versions (R11-AUD01)" },
  { user: /^sam\.agentdev$/, screen: /^home$/, pattern: /reviews\/queue\/summary/, why: "Home reads the review-queue summary, refused to non-reviewers (R11-AUD01)" },
];
const knownFor = (r, item) => KNOWN.find((k) =>
  k.pattern.test(item) && (!k.user || k.user.test(r.user)) && (!k.screen || k.screen.test(r.screen)));
for (const r of report) {
  r.known = [];
  const keep = (item) => {
    const k = knownFor(r, item);
    if (k) r.known.push(`${item} -- ${k.why}`);
    return !k;
  };
  r.failures = r.failures.filter(keep);
  r.shown = r.shown.filter((t) => keep(`text: ${t}`));
}
const isBad = (r) =>
  !r.reachable || r.failures.length || r.shown.length || r.consoleErrors.length || r.personaWrong || r.wrongOrg.length;
const problems = report.filter(isBad);
for (const r of report) {
  const bad = isBad(r);
  const detail = [
    !r.reachable && "UNREACHABLE",
    r.personaWrong && `persona shows ${r.persona}`,
    ...r.wrongOrg.map((o) => `wrong org ${o}`),
    ...r.failures,
    ...r.shown.map((s) => `text: ${s}`),
    ...r.consoleErrors,
  ];
  const knownNote = r.known.length ? `  [known: ${[...new Set(r.known)].length}]` : "";
  console.log(`${bad ? "ISSUE" : "ok   "}  ${r.user.padEnd(15)} ${r.screen.padEnd(20)} ${detail.filter(Boolean).join(" | ")}${knownNote}`);
}
const knownAll = [...new Set(report.flatMap((r) => r.known.map((k) => k.split(" -- ")[1])))];
if (knownAll.length) console.log(`\nknown and tolerated (${knownAll.length}): ${knownAll.join("; ")}`);
fs.writeFileSync(path.join(outDir, "report.json"), JSON.stringify(report, null, 1));
console.log(`\n${report.length} screens opened, ${problems.length} with an issue. Screenshots and report.json in ${outDir}`);
process.exit(problems.length ? 1 : 0);
