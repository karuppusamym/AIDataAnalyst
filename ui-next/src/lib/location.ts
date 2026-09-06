/* ---------------------------------------------------------------------------
   One location store for the whole shell (review 2026-09-05, F09 · T11).

   THE DEFECT this module exists to remove: `useUrlState` seeded a
   `useState` from `location.search` and never subscribed to anything. Once
   mounted, the hook's copy of the query string and the address bar could
   disagree forever:

     - Back/Forward changed the URL; the screen kept rendering the old
       selection, because nothing listened to `popstate`.
     - A same-screen link (Catalog row A -> Catalog row B) changed only the
       query, so the shell -- keyed on the route id -- did not remount, and
       the hook's private state still held row A.
     - `navigateTo` dispatched a synthetic `hashchange`, which the shell heard
       and the hook did not, so the two could not even be made to agree by
       shouting louder.

   THE INVARIANT: the browser URL is the single source of truth for "which
   screen, which selection". Nothing keeps a private copy. Every reader
   subscribes here through `useSyncExternalStore`, so React and the address
   bar cannot drift -- there is only one value.

   WHY A HAND-WRITTEN STORE. This app has no router dependency and adding one
   was explicitly out of scope for a correctness pass. The surface needed is
   small (a screen id, a query string, push/replace, and history events), and
   `useSyncExternalStore` is the supported React primitive for exactly this.

   NOTE ON SNAPSHOT IDENTITY: `useSyncExternalStore` calls `getSnapshot` on
   every render and will loop forever if it returns a fresh object each time.
   The cache below returns the *same* object until the URL actually changes.
--------------------------------------------------------------------------- */

import { buildRelativeLink, screenFromHash, type LinkTarget, type ScreenId } from "./routes";

export interface AppLocation {
  /** The screen named by the hash, already validated against the route table. */
  readonly screen: ScreenId;
  /** The query string, without the leading `?`. */
  readonly search: string;
  /** Parsed query params. Treat as read-only. */
  readonly params: URLSearchParams;
}

/** Fired when we change the URL ourselves; `popstate` does not cover that. */
const INTERNAL_EVENT = "atlas:locationchange";

const listeners = new Set<() => void>();

let cachedKey = "";
let cachedSnapshot: AppLocation = readLocation();

function readLocation(): AppLocation {
  const search = window.location.search.replace(/^\?/, "");
  return {
    screen: screenFromHash(window.location.hash),
    search,
    params: new URLSearchParams(search),
  };
}

function currentKey(): string {
  return `${window.location.search}${window.location.hash}`;
}

function emit(): void {
  for (const listener of listeners) listener();
}

function handleBrowserNavigation(): void {
  // Recompute eagerly so a listener reading the snapshot during this tick
  // sees the new URL rather than the cached one.
  cachedKey = currentKey();
  cachedSnapshot = readLocation();
  emit();
}

let attached = false;

function attach(): void {
  if (attached || typeof window === "undefined") return;
  attached = true;
  window.addEventListener("popstate", handleBrowserNavigation);
  window.addEventListener("hashchange", handleBrowserNavigation);
  window.addEventListener(INTERNAL_EVENT, handleBrowserNavigation);
}

export function subscribeLocation(listener: () => void): () => void {
  attach();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The current location. Referentially stable until the URL changes. */
export function getLocationSnapshot(): AppLocation {
  const key = currentKey();
  if (key !== cachedKey) {
    cachedKey = key;
    cachedSnapshot = readLocation();
  }
  return cachedSnapshot;
}

function commit(url: string, mode: "push" | "replace"): void {
  const absolute = `${window.location.pathname}${url}`;
  const target = `${window.location.search}${window.location.hash}`;
  const next = url;
  if (target === next && mode === "push") {
    // Navigating to where you already are must not add a history entry the
    // Back button then has to eat.
    return;
  }
  if (mode === "push") history.pushState(null, "", absolute);
  else history.replaceState(null, "", absolute);
  window.dispatchEvent(new Event(INTERNAL_EVENT));
}

/**
 * Go to a target, adding a history entry.
 *
 * Screen-owned fields of the screen you are leaving are dropped; estate
 * context is carried unless the caller opts out. See `lib/routes.ts`.
 */
export function pushLocation(target: LinkTarget): void {
  commit(buildRelativeLink(target, window.location.search), "push");
}

/** Go to a target without adding a history entry (filter edits, not navigation). */
export function replaceLocation(target: LinkTarget): void {
  commit(buildRelativeLink(target, window.location.search), "replace");
}

/**
 * Merge a patch into the current screen's query without adding a history
 * entry. `null` and `""` remove a field.
 *
 * This is what a filter control calls. It is deliberately *not* navigation:
 * typing in a search box must not fill the Back button with keystrokes.
 *
 * Deliberately NOT filtered through the route table's field allow-list. That
 * list exists to stop one screen's filters leaking into another screen on a
 * *screen change*; a same-screen merge is by definition writing that screen's
 * own fields, and silently dropping one here would blank a control the user
 * is currently typing into. `buildLink` is where the allow-list belongs.
 */
export function patchQuery(patch: Record<string, string | number | null | undefined>): void {
  const snapshot = getLocationSnapshot();
  const merged = new URLSearchParams(snapshot.search);
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined || value === "") merged.delete(key);
    else merged.set(key, String(value));
  }
  const query = merged.toString();
  commit(`${query ? `?${query}` : ""}#/${snapshot.screen}`, "replace");
}

/**
 * Reset the store's cached snapshot.
 *
 * Tests drive `window.location` directly (jsdom) rather than through
 * `pushLocation`, which means no event fires and the cache is stale. Call
 * this after setting the URL by hand.
 */
export function resetLocationCacheForTests(): void {
  cachedKey = currentKey();
  cachedSnapshot = readLocation();
  emit();
}
