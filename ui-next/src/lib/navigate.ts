import { pushLocation } from "./location";
import { isScreenId, type ScreenId } from "./routes";

/**
 * Open a screen with a selection.
 *
 * Kept as a free function (rather than a hook) because it is called from
 * click handlers in components that are not always inside the shell -- see
 * `components/CrossLinks.tsx`. It now delegates to the shared location store
 * instead of writing `history.pushState` and dispatching a synthetic
 * `hashchange` that only the shell listened to (review 2026-09-05, F09).
 *
 * An unrecognised screen id resolves to Overview rather than throwing, which
 * is what a stale bookmark should do -- but it is a bug in the caller, so it
 * is loud in development.
 */
export function navigateTo(screen: string, params: Record<string, string> = {}): void {
  if (!isScreenId(screen)) {
    if (import.meta.env?.DEV) {
      console.warn(`navigateTo: unknown screen "${screen}"; falling back to Overview.`);
    }
    pushLocation({ screen: "home" });
    return;
  }
  pushLocation({ screen: screen satisfies ScreenId, params });
}
