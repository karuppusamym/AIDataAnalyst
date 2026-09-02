// UX-1 (Persona navigation from OIDC group claim) proof.
//
// Exercises `resolvePersonaMode` -- the pure function persona-nav.js's
// `loadPersonaFromIdentity()` calls on the real `GET /v1/me` response body
// (`persona_api.py`, `persona: str | None` + `identity_provider: str`).
// Mirrors ui/scripts/graph-engine.virtualization.test.mjs's established
// convention: no test runner exists for ui/ (a plain, un-bundled browser
// app), so this runs directly via
// `node ui/scripts/features/persona-nav.test.mjs`.
//
// Field-by-field assertions rather than assert.deepEqual on the returned
// objects: they are constructed inside a vm sandbox, a different realm with
// its own Object.prototype, which trips deepStrictEqual's prototype check
// even when every field matches.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "persona-nav.js"), "utf8");

function freshSandbox({ apiImpl = async () => ({}), storedPersona = null, elements = {} } = {}) {
  const sandbox = {
    window: { AtlasUI: { state: {}, $: (selector) => elements[selector] ?? null, api: apiImpl } },
    console,
    localStorage: { getItem: () => storedPersona, setItem: () => {} },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "persona-nav.js" });
  return sandbox;
}

function assertMode(result, expectedMode, expectedPersona, message) {
  assert.equal(result.mode, expectedMode, message);
  assert.equal(result.persona, expectedPersona, message);
}

const base = freshSandbox();
const { resolvePersonaMode, applyPersonaMode, loadPersonaFromIdentity } = base.window.AtlasUI;

assert.equal(typeof resolvePersonaMode, "function", "persona-nav.js must export resolvePersonaMode");
assert.equal(typeof applyPersonaMode, "function", "persona-nav.js must export applyPersonaMode");
assert.equal(typeof loadPersonaFromIdentity, "function", "persona-nav.js must export loadPersonaFromIdentity");

// --- OIDC mode: server-derived persona wins, never client-chosen ---
assertMode(
  resolvePersonaMode({ persona: "steward", identity_provider: "OIDC" }),
  "oidc", "steward",
  "a mapped OIDC group -> persona must be trusted verbatim"
);

assertMode(
  resolvePersonaMode({ persona: "analyst", identity_provider: "oidc" }),
  "oidc", "analyst",
  "identity_provider comparison must be case-insensitive (Settings.identity_provider.upper())"
);

// --- Unmapped/unknown persona from the server: fail to "all", never crash or grant nothing ---
assertMode(
  resolvePersonaMode({ persona: "made-up-persona", identity_provider: "OIDC" }),
  "oidc", "all",
  "a persona name outside the known catalog must fall back to 'all', not be trusted verbatim"
);

assertMode(
  resolvePersonaMode({ persona: null, identity_provider: "OIDC" }),
  "oidc", "all",
  "a null persona (no group mapped, no default configured) still renders read-only, defaulting to 'all'"
);

// --- Development mode: dev-only browser selector remains the source of truth ---
assertMode(
  resolvePersonaMode({ persona: "operator", identity_provider: "DEVELOPMENT" }),
  "dev-fallback", null,
  "DEVELOPMENT identity provider must defer to the local dev selector, even if the server also reports a persona"
);

// --- Fail-closed: unreachable /v1/me, malformed body, or no body at all ---
assertMode(resolvePersonaMode(null), "dev-fallback", null);
assertMode(resolvePersonaMode(undefined), "dev-fallback", null);
assertMode(resolvePersonaMode({}), "dev-fallback", null);
assertMode(resolvePersonaMode("not-an-object"), "dev-fallback", null);

// --- applyPersonaMode: OIDC mode hides the dev selector and shows read-only text ---
{
  const switcher = { hidden: false };
  const readout = { hidden: true };
  const readoutValue = { textContent: "" };
  const elements = { "#persona-switcher-dev": switcher, "#persona-readonly": readout, "#persona-readonly-value": readoutValue, "#persona-select": null };
  const sandbox = freshSandbox({ elements });
  let applyPersonaCalled = false;
  sandbox.window.AtlasUI.applyPersonaMode({ mode: "oidc", persona: "steward" }, () => { applyPersonaCalled = true; });
  assert.equal(switcher.hidden, true, "the dev-only <select> must be hidden under a real OIDC identity");
  assert.equal(readout.hidden, false, "the read-only persona readout must be shown under OIDC");
  assert.equal(readoutValue.textContent, "Steward");
  assert.equal(sandbox.window.AtlasUI.state.persona, "steward");
  assert.equal(sandbox.window.AtlasUI.state.personaLocked, true);
  assert.equal(applyPersonaCalled, true, "applyPersona() must still run so nav items re-filter");
}

// --- applyPersonaMode: dev-fallback mode keeps the selector visible and reads localStorage ---
{
  const switcher = { hidden: true };
  const readout = { hidden: false };
  const select = { value: "" };
  const elements = { "#persona-switcher-dev": switcher, "#persona-readonly": readout, "#persona-readonly-value": { textContent: "" }, "#persona-select": select };
  const sandbox = freshSandbox({ elements, storedPersona: "operator" });
  sandbox.window.AtlasUI.applyPersonaMode({ mode: "dev-fallback", persona: null }, () => {});
  assert.equal(switcher.hidden, false, "the dev-only <select> must be visible without a real OIDC identity");
  assert.equal(readout.hidden, true);
  assert.equal(sandbox.window.AtlasUI.state.persona, "operator", "must read the remembered dev selection from localStorage");
  assert.equal(sandbox.window.AtlasUI.state.personaLocked, false);
  assert.equal(select.value, "operator");
}

// --- loadPersonaFromIdentity: a rejected /v1/me call fails open to dev-fallback, never throws ---
{
  const sandbox = freshSandbox({ apiImpl: async () => { throw new Error("404"); } });
  const resolved = await sandbox.window.AtlasUI.loadPersonaFromIdentity();
  assertMode(resolved, "dev-fallback", null, "/v1/me being unreachable must not throw or lock out every persona");
}

// --- loadPersonaFromIdentity: a real OIDC response resolves end-to-end ---
{
  const sandbox = freshSandbox({ apiImpl: async () => ({ persona: "auditor", identity_provider: "OIDC" }) });
  const resolved = await sandbox.window.AtlasUI.loadPersonaFromIdentity();
  assertMode(resolved, "oidc", "auditor");
}

console.log("persona-nav.test.mjs: all assertions passed");
