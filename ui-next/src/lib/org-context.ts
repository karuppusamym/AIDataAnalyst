/* ---------------------------------------------------------------------------
   The current organization id, outside React (review 2026-09-05, R05 · T22).

   THE DEFECT this module exists to remove: `org.tsx` imported
   `fetchOrganizations` from `api.ts`, and `api.ts` imported `getCurrentOrgId`
   from `org.tsx`. A cycle between a React provider and the HTTP client. It
   happened to work -- neither side touches the other's export at module-eval
   time -- but "happens to work" is the whole problem with an import cycle:
   the first person to move a top-level `const` in either file discovers it as
   a `undefined is not a function` at load, in a bundle, in production.

   THE INVARIANT: the request layer must never import the React layer. The
   org-id mirror is the only thing they genuinely share, so it lives here, on
   its own, importing nothing. `org.tsx` writes it; `api/transport.ts` reads
   it. Neither imports the other.

   WHY A MIRROR AT ALL. A handful of backend routes (observability/SLO,
   notification-rules, tool-plans) take no `{organization_id}` path segment
   and resolve the tenant purely from the `X-Organization-Id` header
   (`security.py`'s `get_security_context`). Without a module-scope value
   those routes would need the org id threaded through hundreds of call
   sites, or would 400 under a live backend while every path-scoped route
   worked -- which is exactly the failure that put the mirror here originally.

   WHY THE SETTER IS SYNCHRONOUS (F10). The mirror used to be assigned inside
   a `useEffect` in `org.tsx`, one commit *after* the state changed. Any
   request issued in that gap -- including the scope reload the org change
   itself triggers -- carried the PREVIOUS organization in its header while
   the UI already showed the new one. `setCurrentOrgId` is now called from the
   setter itself, before React re-renders, so there is no window in which the
   two disagree.

   This is a convenience for header construction. It is NOT authorization: the
   backend decides what this principal may see in this organization, and
   `enforce_organization` still runs on every request.
--------------------------------------------------------------------------- */

/** The development/fixture organization id every screen used to hard-code. */
export const DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001";

/** Where the selection is remembered between sessions. */
export const ORG_STORAGE_KEY = "atlas.org.id";

export function readStoredOrgId(): string {
  try {
    return localStorage.getItem(ORG_STORAGE_KEY) || DEFAULT_ORG_ID;
  } catch {
    /* private mode / storage disabled -- fall back to the default estate. */
    return DEFAULT_ORG_ID;
  }
}

let currentOrgId: string = readStoredOrgId();

/** The current organization id, readable synchronously from anywhere. */
export function getCurrentOrgId(): string {
  return currentOrgId;
}

/**
 * Point the mirror at `orgId` and persist it.
 *
 * Call this from the state setter, not from an effect. See the file comment:
 * an effect-updated mirror is a window in which the header and the screen
 * name two different organizations.
 */
export function setCurrentOrgId(orgId: string): void {
  currentOrgId = orgId;
  try {
    localStorage.setItem(ORG_STORAGE_KEY, orgId);
  } catch {
    /* private mode / storage disabled -- selection is simply not remembered */
  }
}
