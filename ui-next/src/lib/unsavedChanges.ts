import { useCallback, useEffect, useSyncExternalStore } from "react";

/* ---------------------------------------------------------------------------
   Unsaved edits, and the navigation that would discard them — R11-S10.

   THE DEFECT this removes. `useUnsavedChanges` already existed in
   `components/primitives.tsx` and did half the job: it installed a
   `beforeunload` handler, so closing or reloading the TAB warned. It also
   returned a `confirm()` function for "call before an in-app navigation that
   would discard the edit" -- and nothing in the app ever called it. Exactly
   one component used the hook at all (`DescriptionEditor`), and it discarded
   the returned function.

   So clicking any sidebar item, any section-nav item, or any palette result
   while a description was half-written threw the edit away silently. The
   shell's `navigate` had no idea a form was dirty, because there was no way
   for a form to tell it.

   THE FIX is a registry the shell can ask. A dirty form registers a reporter
   here; the shell calls `confirmNavigation()` before every `pushLocation` it
   makes. One question, asked in one place, so a new screen with a form
   inherits the guard by using the hook rather than by remembering to wire
   something into navigation.

   WHY THIS ROW. Grouping routes changes every navigation path in the app, and
   an unsaved-state regression is exactly what that change would hide.

   WHAT IT DOES NOT COVER, stated rather than implied: the BACK button. A
   `popstate` has already happened by the time a listener sees it, and the only
   way to "cancel" one is to push the old entry back -- which fights the user's
   own history and is worse than the problem. `beforeunload` covers leaving the
   document; this covers navigation the app itself initiates; a Back press out
   of a dirty form is not guarded, and pretending otherwise in a comment is how
   the next person stops checking.
--------------------------------------------------------------------------- */

/** Returns the warning to show, or null when this source is clean. */
export type DirtyReporter = () => string | null;

const reporters = new Set<DirtyReporter>();
const listeners = new Set<() => void>();

/* A revision counter rather than the set itself: `useSyncExternalStore` calls
   `getSnapshot` on every render and loops forever on a fresh value each time,
   and a Set's identity does not change when its contents do. */
let revision = 0;

function emit(): void {
  revision += 1;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getRevision(): number {
  return revision;
}

/** Register a source of unsaved state. Returns the unregister function. */
export function registerDirtyReporter(reporter: DirtyReporter): () => void {
  reporters.add(reporter);
  emit();
  return () => {
    reporters.delete(reporter);
    emit();
  };
}

/** The first outstanding warning, or null when nothing is dirty. */
export function pendingUnsavedWarning(): string | null {
  for (const reporter of reporters) {
    const message = reporter();
    if (message) return message;
  }
  return null;
}

/** True when anything on screen has unsaved edits. */
export function hasUnsavedChanges(): boolean {
  return pendingUnsavedWarning() !== null;
}

/**
 * Ask the user before discarding unsaved edits. True means "go ahead".
 *
 * `window.confirm` deliberately: this has to be able to say no to a navigation
 * that is already in a click handler, and a custom dialog cannot answer
 * synchronously. The shell would have to become a state machine with a pending
 * destination to use one, which is a bigger change than this row is, and a
 * confirm that blocks is better than an edit that vanishes.
 */
export function confirmDiscardUnsaved(): boolean {
  const message = pendingUnsavedWarning();
  if (!message) return true;
  return window.confirm(message);
}

const DEFAULT_MESSAGE = "Discard your unsaved changes?";

/**
 * Declare that this component holds unsaved edits.
 *
 * Covers both ways the edit can be lost: closing or reloading the tab (the
 * browser's own prompt, the only thing allowed there) and navigating away
 * inside the app (the shell asks the registry before it moves).
 *
 * Returns a confirm function for a component that wants to guard its OWN
 * action -- closing a drawer, switching a tab -- which is the shape the
 * original hook in `primitives.tsx` had. Callers that only need the shell's
 * guard can ignore it.
 */
export function useUnsavedChanges(dirty: boolean, message: string = DEFAULT_MESSAGE) {
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    const unregister = registerDirtyReporter(() => message);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      unregister();
    };
  }, [dirty, message]);

  return useCallback(
    (prompt: string = message) => !dirty || window.confirm(prompt),
    [dirty, message],
  );
}

/**
 * The shell's side of the contract: a function to call before navigating.
 *
 * Subscribes so the shell re-renders when dirtiness changes -- not because the
 * verdict is rendered, but because a callback captured before a form went
 * dirty must not be the one that answers afterwards.
 */
export function useUnsavedNavigationGuard(): () => boolean {
  useSyncExternalStore(subscribe, getRevision, getRevision);
  return useCallback(() => confirmDiscardUnsaved(), []);
}

/** Test seam: drop every registered reporter between cases. */
export function resetUnsavedRegistryForTests(): void {
  reporters.clear();
  emit();
}
