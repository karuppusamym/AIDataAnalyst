import { useCallback, useSyncExternalStore } from "react";

import {
  getLocationSnapshot,
  patchQuery,
  subscribeLocation,
  type AppLocation,
} from "./location";
import type { ScreenId } from "./routes";

/* ---------------------------------------------------------------------------
   Filters and selection live in the URL so a filtered/selected view is
   shareable and survives Back/Forward.

   This hook used to seed a `useState` from `location.search` and subscribe to
   nothing, so its copy of the query string and the address bar drifted apart
   the first time the user pressed Back or followed a same-screen link (review
   2026-09-05, F09). It now reads the shared location store, so there is one
   value rather than one-per-hook -- and the six screens that had inlined
   their own copy of the old implementation can delete it and call this.
--------------------------------------------------------------------------- */

/** Subscribe to the whole location (screen + params). */
export function useAppLocation(): AppLocation {
  return useSyncExternalStore(subscribeLocation, getLocationSnapshot, getLocationSnapshot);
}

/** The screen currently named by the URL. */
export function useCurrentScreen(): ScreenId {
  return useAppLocation().screen;
}

/**
 * `[params, update]`, where `update` merges a patch into the current screen's
 * query string. `null` or `""` removes a field. Updates use `replaceState`:
 * typing in a filter box must not fill the Back button with keystrokes.
 */
export function useUrlState() {
  const params = useAppLocation().params;
  const update = useCallback((patch: Record<string, string | null>) => {
    patchQuery(patch);
  }, []);
  return [params, update] as const;
}
