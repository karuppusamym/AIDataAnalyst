import { createContext, useContext, useEffect, useMemo, useState } from "react";
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

/** The development/fixture organization id every screen used to hard-code. */
export const DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001";

const STORAGE_KEY = "atlas.org.id";

export interface OrgSelection {
  orgId: string;
  organizations: OrganizationRead[];
  setOrgId: (id: string) => void;
  loading: boolean;
  error: string | null;
}

const OrgContext = createContext<OrgSelection | null>(null);

function readStoredOrgId(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_ORG_ID;
  } catch {
    return DEFAULT_ORG_ID;
  }
}

export function OrgProvider({ children }: { children: ReactNode }) {
  const [organizations, setOrganizations] = useState<OrganizationRead[]>([]);
  const [orgId, setOrgIdState] = useState<string>(readStoredOrgId);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Persist every choice so a returning viewer lands on the same estate.
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, orgId);
    } catch {
      /* private mode / storage disabled — selection is simply not remembered */
    }
  }, [orgId]);

  useEffect(() => {
    const controller = new AbortController();
    fetchOrganizations(controller.signal)
      .then((orgs) => {
        setOrganizations(orgs);
        setError(null);
        // If the stored id is not among the real organizations, adopt the first
        // one so a fresh browser lands on a selectable estate rather than a
        // dead id that renders every screen empty.
        setOrgIdState((current) =>
          orgs.some((o) => o.id === current) ? current : (orgs[0]?.id ?? current),
        );
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
  }, []);

  const value = useMemo<OrgSelection>(
    () => ({ orgId, organizations, setOrgId: setOrgIdState, loading, error }),
    [orgId, organizations, loading, error],
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
