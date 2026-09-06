import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { fetchOrganizations } from "./api";
import type { OrganizationRead } from "./types";

/* ---------------------------------------------------------------------------
   Shared organization selection.

   Every screen historically hard-coded a single organization id. That worked
   in fixture mode (the fixtures use the same id) but made a live backend
   unusable: the real API has no such organization, so every screen queried a
   non-existent tenant and came back empty. This provider lifts the choice into
   the shell so a real, seeded organization can be selected once and read by
   every screen.

   `useOrgId()` resolves to `DEFAULT_ORG_ID` when used outside a provider — the
   state a bare-rendered unit test is in — so the existing fixture-backed tests
   keep resolving to the id their fixtures use, with no change.
--------------------------------------------------------------------------- */

import {
  DEFAULT_ORG_ID,
  getCurrentOrgId,
  readStoredOrgId,
  setCurrentOrgId,
} from "./org-context";

/* The org-id mirror `api.ts` reads lives in `./org-context`, which imports
 * nothing -- see that file for why. Re-exported here so the existing
 * `import { DEFAULT_ORG_ID } from "./org"` call sites keep working. */
export { DEFAULT_ORG_ID, getCurrentOrgId } from "./org-context";

export interface OrgSelection {
  orgId: string;
  organizations: OrganizationRead[];
  setOrgId: (id: string) => void;
  addOrganization: (organization: OrganizationRead) => void;
  loading: boolean;
  error: string | null;
}

const OrgContext = createContext<OrgSelection | null>(null);

export function OrgProvider({ children }: { children: ReactNode }) {
  const [organizations, setOrganizations] = useState<OrganizationRead[]>([]);
  const [orgId, setOrgIdState] = useState<string>(readStoredOrgId);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /* F10: the mirror is updated *in the setter*, not in an effect.
   *
   * When it was an effect, the module-level org id changed one commit after
   * the state did, so every request issued in that gap -- including the scope
   * reload the organization change itself triggers -- carried the PREVIOUS
   * organization in its `X-Organization-Id` header while the UI already
   * showed the new one. Out-of-order responses could then populate a screen
   * belonging to a tenant the user had just left. Backend enforcement still
   * decides what may be seen; this closes the window in which the client
   * asks the wrong question. */
  const setOrgId = useCallback((id: string) => {
    setCurrentOrgId(id);
    setOrgIdState(id);
  }, []);

  const addOrganization = useCallback(
    (organization: OrganizationRead) => {
      setOrganizations((items) =>
        items.some((item) => item.id === organization.id) ? items : [...items, organization],
      );
      setOrgId(organization.id);
    },
    [setOrgId],
  );

  useEffect(() => {
    const controller = new AbortController();
    fetchOrganizations(controller.signal)
      .then((orgs) => {
        setOrganizations(orgs);
        setError(null);
        // If the stored id is not among the real organizations, adopt the first
        // one so a fresh browser lands on a selectable estate rather than a
        // dead id that renders every screen empty.
        const resolved = orgs.some((o) => o.id === getCurrentOrgId())
          ? getCurrentOrgId()
          : (orgs[0]?.id ?? getCurrentOrgId());
        setOrgId(resolved);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [setOrgId]);

  const value = useMemo<OrgSelection>(
    () => ({ orgId, organizations, setOrgId, addOrganization, loading, error }),
    [orgId, organizations, setOrgId, addOrganization, loading, error],
  );

  return <OrgContext.Provider value={value}>{children}</OrgContext.Provider>;
}

/** The current organization id. Resolves to `DEFAULT_ORG_ID` outside a
 *  provider so screens rendered bare (unit tests) behave exactly as before. */
export function useOrgId(): string {
  return useContext(OrgContext)?.orgId ?? DEFAULT_ORG_ID;
}

/** Full selection state for the shell's picker. `null` outside a provider, so
 *  the caller can choose to render nothing rather than guess. */
export function useOrgSelection(): OrgSelection | null {
  return useContext(OrgContext);
}
