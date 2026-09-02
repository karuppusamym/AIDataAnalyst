/* UX-1: persona navigation derived from the OIDC group claim.
 *
 * The backend already derives a persona from the configured OIDC groups
 * claim (module 01's `aida.oidc.context_from_claims` / `_persona_from_groups`)
 * and echoes it, alongside the active identity provider mode, from
 * `GET /v1/me` (`persona_api.py`). This module is the ui/ (legacy shell)
 * half of that wiring: it fetches `/v1/me` once at startup and decides,
 * from `identity_provider` alone -- never inferred client-side -- whether
 * to show the persona as read-only (OIDC) or fall back to the existing
 * browser <select> (development only, grants nothing beyond nav visibility).
 *
 * Fail-closed like module 01's own INV-4: if `/v1/me` is unreachable (not
 * yet deployed on this environment, network error, etc.) this falls back to
 * the dev selector rather than locking the shell out of every persona.
 */
(function initializePersonaNav() {
  const { state, $, api } = window.AtlasUI;

  const KNOWN_PERSONAS = ["all", "analyst", "steward", "operator", "auditor"];

  /* Pure: given a GET /v1/me response body, decide the persona UI mode.
   * No DOM, no network -- exercised directly by persona-nav.test.mjs. */
  function resolvePersonaMode(identity) {
    if (!identity || typeof identity !== "object") {
      return { mode: "dev-fallback", persona: null };
    }
    const provider = String(identity.identity_provider || "").toUpperCase();
    if (provider === "OIDC") {
      const persona = KNOWN_PERSONAS.includes(identity.persona) ? identity.persona : "all";
      return { mode: "oidc", persona };
    }
    return { mode: "dev-fallback", persona: null };
  }

  async function loadPersonaFromIdentity() {
    try {
      const identity = await api("/v1/me");
      return resolvePersonaMode(identity);
    } catch {
      return { mode: "dev-fallback", persona: null };
    }
  }

  /* Applies the resolved mode to shell state and the two mutually-exclusive
   * DOM affordances (read-only OIDC readout vs. dev-only <select>), then
   * calls the caller's own `applyPersona()` to re-filter nav items. */
  function applyPersonaMode(resolved, applyPersona) {
    const mode = resolved?.mode === "oidc" ? "oidc" : "dev-fallback";
    const switcher = $("#persona-switcher-dev");
    const readout = $("#persona-readonly");
    const readoutValue = $("#persona-readonly-value");
    if (mode === "oidc") {
      state.persona = resolved.persona || "all";
      state.personaLocked = true;
      if (switcher) switcher.hidden = true;
      if (readout) readout.hidden = false;
      if (readoutValue) {
        readoutValue.textContent = state.persona === "all"
          ? "All capabilities"
          : `${state.persona.charAt(0).toUpperCase()}${state.persona.slice(1)}`;
      }
    } else {
      state.personaLocked = false;
      state.persona = localStorage.getItem("aida-persona") || "all";
      if (switcher) switcher.hidden = false;
      if (readout) readout.hidden = true;
      const select = $("#persona-select");
      if (select) select.value = state.persona;
    }
    if (typeof applyPersona === "function") applyPersona();
  }

  Object.assign(window.AtlasUI, { resolvePersonaMode, loadPersonaFromIdentity, applyPersonaMode });
})();
