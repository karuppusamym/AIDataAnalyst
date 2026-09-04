import { useCallback, useEffect, useState } from "react";
import { useOrgId } from "../lib/org";
import {
  ApiError,
  bulkReaffirmOwnershipAssignments,
  fetchOwnershipAssignments,
  reaffirmOwnershipAssignment,
  type OwnershipAssignmentRead,
} from "../lib/api";

/* ---------------------------------------------------------------------------
   P2-07: OwnershipExpiryBanner screen -- "you have N ownerships expiring
   soon", per-row Reaffirm, and a Reaffirm-all button that hits the bulk
   endpoint.

   Deliberately a dedicated screen rather than a modification of
   StewardshipScreen.tsx (per the P2-07 ticket, "if StewardshipScreen
   structure is complex, at minimum add the api.ts function + type + a small
   dedicated OwnershipExpiryBannerScreen.tsx"). The router or Home shell
   embeds this component; it computes "expiring soon" client-side against
   a window (`WARN_DAYS`, default 14) matching the server's
   `settings.ownership_expiry_warn_days`.

   The current principal id is read from `import.meta.env.VITE_DEV_PRINCIPAL_ID`
   the same way `identityHeaders()` in `lib/api.ts` reads it -- keeping this
   screen and every write it makes attributable to the same principal the
   dev shell sends on `X-Principal-Id`.
--------------------------------------------------------------------------- */

// Server default is 14 (`AIDA_OWNERSHIP_EXPIRY_WARN_DAYS`). Kept in sync
// with `ownership_expiry_warn_days` at the config level; a per-org override
// would flow through a future settings endpoint.
const WARN_DAYS = 14;

const CURRENT_PRINCIPAL_ID: string =
  (import.meta.env.VITE_DEV_PRINCIPAL_ID as string | undefined) || "local-ui-admin";

interface OwnershipExpiryBannerScreenProps {
  /** If given, overrides `useOrgId()` -- used by tests. */
  organizationId?: string;
}

export function OwnershipExpiryBannerScreen(props: OwnershipExpiryBannerScreenProps = {}) {
  const orgFromContext = useOrgId();
  const organizationId = props.organizationId ?? orgFromContext;
  const [rows, setRows] = useState<OwnershipAssignmentRead[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!organizationId) return;
      setLoading(true);
      setError(null);
      try {
        const page = await fetchOwnershipAssignments(organizationId, { limit: 500 }, signal);
        const nowMs = Date.now();
        const windowMs = WARN_DAYS * 86_400_000;
        const expiring = page.items.filter(
          (row) =>
            row.owner_principal === CURRENT_PRINCIPAL_ID &&
            row.status === "ACTIVE" &&
            row.expires_at !== null &&
            Date.parse(row.expires_at) > nowMs &&
            Date.parse(row.expires_at) - nowMs < windowMs,
        );
        expiring.sort(
          (a, b) => Date.parse(a.expires_at!) - Date.parse(b.expires_at!),
        );
        setRows(expiring);
      } catch (err) {
        if ((err as { name?: string }).name === "AbortError") return;
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [organizationId],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void refresh(ctrl.signal);
    return () => ctrl.abort();
  }, [refresh]);

  const onReaffirm = useCallback(
    async (assignmentId: string) => {
      setBusyId(assignmentId);
      try {
        await reaffirmOwnershipAssignment(assignmentId);
        await refresh();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  const onReaffirmAll = useCallback(async () => {
    if (rows.length === 0) return;
    setBusyId("__all__");
    try {
      await bulkReaffirmOwnershipAssignments(rows.map((row) => row.id));
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }, [rows, refresh]);

  if (!organizationId) {
    return null; // banner only exists once an org is selected
  }
  if (loading && rows.length === 0) {
    return null; // silent while loading -- the banner has no "loading..." state
  }
  if (rows.length === 0 && !error) {
    return null; // nothing to warn about; render nothing
  }

  return (
    <section className="ownership-expiry-banner" role="status" aria-live="polite">
      {error ? (
        <p className="ownership-expiry-banner__error">Could not load ownerships: {error}</p>
      ) : null}
      {rows.length > 0 ? (
        <>
          <header className="ownership-expiry-banner__header">
            <strong>
              You have {rows.length} ownership{rows.length === 1 ? "" : "s"} expiring in the
              next {WARN_DAYS} days
            </strong>
            <button
              type="button"
              onClick={onReaffirmAll}
              disabled={busyId !== null}
              className="ownership-expiry-banner__reaffirm-all"
            >
              {busyId === "__all__" ? "Reaffirming..." : `Reaffirm all ${rows.length}`}
            </button>
          </header>
          <ul className="ownership-expiry-banner__list">
            {rows.map((row) => (
              <li key={row.id} className="ownership-expiry-banner__row">
                <span>
                  <code>{row.subject_type}</code> <code>{row.subject_id}</code>
                  {row.expires_at ? (
                    <> -- expires {new Date(row.expires_at).toLocaleDateString()}</>
                  ) : null}
                </span>
                <button
                  type="button"
                  onClick={() => onReaffirm(row.id)}
                  disabled={busyId !== null}
                >
                  {busyId === row.id ? "Reaffirming..." : "Reaffirm"}
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
