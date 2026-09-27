import { pushLocation } from "./location";
import { resolveScreenRef } from "./routes";

/**
 * Open a screen with a selection.
 *
 * Kept as a free function (rather than a hook) because it is called from
 * click handlers in components that are not always inside the shell -- see
 * `components/CrossLinks.tsx`. It now delegates to the shared location store
 * instead of writing `history.pushState` and dispatching a synthetic
 * `hashchange` that only the shell listened to (review 2026-09-05, F09).
 *
 * R11-S13: a screen that was MERGED AWAY still opens here, on the screen that
 * absorbed it and with the filter that reproduces what it showed. This door
 * takes a `string`, not a `ScreenId` -- `CrossLinks` is deliberately not a
 * router -- so the compiler cannot catch a link to a retired id, and the test
 * that used to hold was `isScreenId`, which answers "is this LIVE". It is not:
 * `EvidencePane`'s "Impact" cross-link names `unified-lineage`, which M1
 * merged into `lineage`, and under the old check that click landed on
 * Overview. `resolveScreenRef` accepts every id that has ever been one, which
 * is the same contract `resolveHash` gives a pasted URL.
 *
 * The caller's own params are kept and the alias's are overlaid, matching
 * `normalizeLocation`: the retired route's implied filter decides the view,
 * the caller's selection decides what is selected in it.
 *
 * An id that has never existed still resolves to Overview rather than
 * throwing, which is what a stale link should do -- but it is a bug in the
 * caller, so it is loud in development.
 */
export function navigateTo(screen: string, params: Record<string, string> = {}): void {
  const resolved = resolveScreenRef(screen);
  if (!resolved) {
    if (import.meta.env?.DEV) {
      console.warn(`navigateTo: unknown screen "${screen}"; falling back to Overview.`);
    }
    pushLocation({ screen: "home" });
    return;
  }
  pushLocation({
    screen: resolved.screen,
    params: { ...params, ...(resolved.params ?? {}) },
  });
}
