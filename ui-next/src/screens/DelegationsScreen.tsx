import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import type { DelegationCreate, DelegationRead } from "../lib/types";
import { ApiError, fetchDelegations, grantDelegation, revokeDelegation } from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/workflow-author.css";
import "./DelegationsScreen.css";

/* ---------------------------------------------------------------------------
   Delegations — PG-4.

   A steward or reviewer going on leave hands one or more of its own
   governance roles to a covering colleague for a bounded time window, so a
   governance decision does not stall on one person's absence. The backend
   (`delegation_api.py`) has existed since PG-4 landed; this is the first
   frontend for it.

   `status` on the wire is only ever `"ACTIVE"` or `"REVOKED"` — nothing flips
   the column at expiry by design (mirrors GL-5/CT-5 certification expiry:
   the row is retained, evaluation is what enforces the window). So an
   `"expired"` row is a client-side projection — `status === "ACTIVE"` and
   `expires_at` already in the past — never a value the field itself carries,
   and never a value sent as a `status` filter on the wire either: the
   "Expired" filter option asks the server for `ACTIVE` and narrows further
   here.
--------------------------------------------------------------------------- */

const DELEGATION_ROLES = [
  "PlatformAdmin",
  "DataSteward",
  "Reviewer",
  "MetadataAdmin",
  "DataAdmin",
  "SemanticAdmin",
] as const;

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "All" },
  { value: "ACTIVE", label: "Active" },
  { value: "EXPIRED", label: "Expired" },
  { value: "REVOKED", label: "Revoked" },
];

type EffectiveStatus = "active" | "expired" | "revoked";

const STATUS_TONE: Record<EffectiveStatus, Tone> = {
  active: "ok",
  expired: "warn",
  revoked: "mute",
};

function effectiveStatus(delegation: DelegationRead): EffectiveStatus {
  if (delegation.status === "REVOKED") return "revoked";
  if (delegation.status === "ACTIVE" && new Date(delegation.expires_at).getTime() < Date.now()) {
    return "expired";
  }
  return "active";
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function GrantDelegationForm({
  organizationId,
  onGranted,
}: {
  organizationId: string;
  onGranted: (delegation: DelegationRead) => void;
}) {
  const [delegatePrincipalId, setDelegatePrincipalId] = useState("");
  const [roles, setRoles] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [startsAt, setStartsAt] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const toggleRole = useCallback((role: string) => {
    setRoles((prev) => (prev.includes(role) ? prev.filter((r) => r !== role) : [...prev, role]));
  }, []);

  const canSubmit =
    delegatePrincipalId.trim().length > 0 &&
    roles.length > 0 &&
    reason.trim().length >= 10 &&
    expiresAt.length > 0;

  const onSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setBusy(true);
      setMessage(null);
      try {
        const body: DelegationCreate = {
          delegate_principal_id: delegatePrincipalId.trim(),
          delegated_roles: roles,
          reason: reason.trim(),
          starts_at: startsAt ? new Date(startsAt).toISOString() : null,
          expires_at: new Date(expiresAt).toISOString(),
        };
        const delegation = await grantDelegation(organizationId, body);
        onGranted(delegation);
        setMessage("Delegation granted.");
        setDelegatePrincipalId("");
        setRoles([]);
        setReason("");
        setStartsAt("");
        setExpiresAt("");
      } catch (err) {
        setMessage(err instanceof ApiError ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [organizationId, delegatePrincipalId, roles, reason, startsAt, expiresAt, onGranted],
  );

  return (
    <details className="workflow-author">
      <summary>Grant delegation</summary>
      <p>
        Hand one or more of your own governance roles to another principal for a bounded time
        window — the covering colleague can act with that authority only until it expires or you
        revoke it. You can only delegate a role you hold yourself.
      </p>
      <form onSubmit={(event) => void onSubmit(event)}>
        <Field label="Delegate to (principal id)">
          <input
            value={delegatePrincipalId}
            onChange={(event) => setDelegatePrincipalId(event.target.value)}
            placeholder="e.g. morgan.covering"
          />
        </Field>
        <fieldset className="deleg__rolesfield">
          <legend>Roles to delegate</legend>
          {DELEGATION_ROLES.map((role) => (
            <label key={role} className="deleg__rolecheck">
              <input type="checkbox" checked={roles.includes(role)} onChange={() => toggleRole(role)} />
              {role}
            </label>
          ))}
        </fieldset>
        <Field label="Reason">
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why this delegation is needed (at least 10 characters)"
          />
        </Field>
        <Field label="Starts at (optional — defaults to now)">
          <input
            type="datetime-local"
            value={startsAt}
            onChange={(event) => setStartsAt(event.target.value)}
          />
        </Field>
        <Field label="Expires at">
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
            required
          />
        </Field>
        <Button type="submit" variant="primary" disabled={busy || !canSubmit}>
          Grant
        </Button>
      </form>
      {message ? <p role="status">{message}</p> : null}
    </details>
  );
}

function DelegationRow({
  delegation,
  onRevoke,
}: {
  delegation: DelegationRead;
  onRevoke: (delegation: DelegationRead) => void;
}) {
  const state = effectiveStatus(delegation);
  return (
    <li className="deleg__row">
      <span className="deleg__rowhead">
        <strong>{delegation.delegator_principal_id}</strong>
        <span className="deleg__arrow" aria-hidden="true">→</span>
        <strong>{delegation.delegate_principal_id}</strong>
        <Pill tone={STATUS_TONE[state]}>{state}</Pill>
      </span>
      <span className="deleg__roles">
        {delegation.delegated_roles.map((role) => (
          <Pill key={role} tone="mute">
            {role}
          </Pill>
        ))}
      </span>
      <p className="deleg__reason">{delegation.reason}</p>
      <span className="deleg__window">
        {formatDate(delegation.starts_at)} → {formatDate(delegation.expires_at)}
      </span>
      {state === "active" ? (
        <Button onClick={() => onRevoke(delegation)}>Revoke</Button>
      ) : delegation.status === "REVOKED" && delegation.revoked_by ? (
        <span className="deleg__muted">revoked by {delegation.revoked_by}</span>
      ) : null}
    </li>
  );
}

export function DelegationsScreen() {
  const organizationId = useOrgId();
  const [params, setParams] = useUrlState();
  const delegateFilter = params.get("delegate") ?? "";
  const delegatorFilter = params.get("delegator") ?? "";
  const statusFilter = params.get("status") ?? "";

  const [draftDelegate, setDraftDelegate] = useState(delegateFilter);
  const [draftDelegator, setDraftDelegator] = useState(delegatorFilter);

  const [data, setData] = useState<{ items: DelegationRead[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Debounce the two text filters so each keystroke does not become a
  // request (the same convention `CatalogScreen` established).
  useEffect(() => {
    const t = setTimeout(() => {
      if (draftDelegate !== delegateFilter || draftDelegator !== delegatorFilter) {
        setParams({ delegate: draftDelegate || null, delegator: draftDelegator || null });
      }
    }, 250);
    return () => clearTimeout(t);
  }, [draftDelegate, draftDelegator, delegateFilter, delegatorFilter, setParams]);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      // "Expired" has no server-side status of its own -- an expired row is
      // still ACTIVE on the wire, so ask the server for ACTIVE and narrow
      // further client-side (see `visibleItems` below).
      const serverStatus = statusFilter === "EXPIRED" ? "ACTIVE" : statusFilter || undefined;
      fetchDelegations(
        organizationId,
        {
          delegatePrincipalId: delegateFilter || undefined,
          delegatorPrincipalId: delegatorFilter || undefined,
          status: serverStatus,
        },
        signal,
      )
        .then((page) => {
          setData(page);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setError(err instanceof ApiError ? err.message : String(err));
          setLoading(false);
        });
    },
    [organizationId, delegateFilter, delegatorFilter, statusFilter],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const visibleItems = useMemo(() => {
    const items = data?.items ?? [];
    if (statusFilter === "EXPIRED") return items.filter((d) => effectiveStatus(d) === "expired");
    if (statusFilter === "ACTIVE") return items.filter((d) => effectiveStatus(d) === "active");
    return items;
  }, [data, statusFilter]);

  const onRevoke = useCallback((delegation: DelegationRead) => {
    if (!window.confirm("Revoke this delegation?")) return;
    void (async () => {
      try {
        const updated = await revokeDelegation(delegation.id);
        setData((prev) =>
          prev
            ? { ...prev, items: prev.items.map((item) => (item.id === updated.id ? updated : item)) }
            : prev,
        );
        setNotice("Delegation revoked.");
      } catch (err) {
        setNotice(err instanceof ApiError ? err.message : String(err));
      }
    })();
  }, []);

  const onGranted = useCallback((delegation: DelegationRead) => {
    setData((prev) => (prev ? { ...prev, items: [delegation, ...prev.items] } : { items: [delegation] }));
  }, []);

  if (error) {
    return (
      <section className="deleg">
        <ErrorState title="Delegations could not be loaded" detail={error} onRetry={() => load()} />
      </section>
    );
  }

  return (
    <section className="deleg">
      <header className="deleg__head">
        <div>
          <h1>Delegations</h1>
          <p className="deleg__sub">
            Time-bounded, audited handoff of governance authority — hand one of your own roles to a
            covering colleague while you are away, and revoke it any time before it expires.
          </p>
        </div>
      </header>

      <GrantDelegationForm organizationId={organizationId} onGranted={onGranted} />

      {notice && (
        <p className="deleg__notice" role="status">
          {notice}
        </p>
      )}

      <form className="deleg__filters" onSubmit={(event) => event.preventDefault()}>
        <Field label="Delegate principal">
          <input
            value={draftDelegate}
            onChange={(event) => setDraftDelegate(event.target.value)}
            placeholder="Filter by delegate id"
          />
        </Field>
        <Field label="Delegator principal">
          <input
            value={draftDelegator}
            onChange={(event) => setDraftDelegator(event.target.value)}
            placeholder="Filter by delegator id"
          />
        </Field>
        <Field label="Status">
          <select
            value={statusFilter}
            onChange={(event) => setParams({ status: event.target.value || null })}
            aria-label="Delegation status filter"
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
      </form>

      {loading && !data ? (
        <p role="status" className="deleg__loading">
          Loading delegations…
        </p>
      ) : visibleItems.length > 0 ? (
        <ul className="deleg__list">
          {visibleItems.map((delegation) => (
            <DelegationRow key={delegation.id} delegation={delegation} onRevoke={onRevoke} />
          ))}
        </ul>
      ) : (
        <Empty
          title="No delegation matches these filters."
          hint="Grant one above, or clear the filters to see every delegation in this organization."
        />
      )}
    </section>
  );
}
