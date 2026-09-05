import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../lib/api";
import {
  fetchCrossBoundaryGrants,
  requestCrossBoundaryGrant,
} from "../lib/_cross_source_api";
import type { CrossBoundaryGrantRead, DataDomainRead } from "../lib/types";
import { Button, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./CrossBoundaryGrants.css";

/* ---------------------------------------------------------------------------
   Cross-boundary grants.

   ADR-0017 SS4 / INV-5: seeing across a data-domain boundary is deny-by-
   default and never inherited. A relationship reaching from one domain into
   another renders only while an ACTIVE grant permits it, checked per read —
   not once at discovery time.

   Two rules this component encodes:

   1. **It can request, and it cannot approve.** A grant is created
      PENDING_APPROVAL with a `CROSS_BOUNDARY_GRANT` governance review, and
      goes ACTIVE only when a *different* principal decides that review on the
      Review queue screen. Putting an approve button here would either
      duplicate that decision surface or quietly bypass maker-checker; both are
      worse than sending the reviewer to the queue.
   2. **Direction is stated, not implied.** "Finance may see into Retail" and
      "Retail may see into Finance" are different grants with different owners,
      and a list that showed only two domain names would leave a steward
      guessing which one they are looking at.
--------------------------------------------------------------------------- */

const STATUS_TONE: Record<string, Tone> = {
  ACTIVE: "ok",
  PENDING_APPROVAL: "warn",
  REJECTED: "bad",
  REVOKED: "bad",
  EXPIRED: "mute",
};

function domainName(domains: readonly DataDomainRead[], id: string): string {
  return domains.find((d) => d.id === id)?.name ?? id;
}

function expiryNote(grant: CrossBoundaryGrantRead): string | null {
  if (!grant.expires_at) return null;
  const expires = new Date(grant.expires_at);
  const expired = expires.getTime() < Date.now();
  return `${expired ? "expired" : "expires"} ${expires.toLocaleDateString()}`;
}

function GrantRow({
  grant,
  domains,
  viewpointDomainId,
}: {
  grant: CrossBoundaryGrantRead;
  domains: readonly DataDomainRead[];
  viewpointDomainId: string;
}) {
  // Which side of the boundary the domain we are looking at sits on. The
  // "source" domain owns the data being looked into; the "target" domain is
  // the one doing the looking.
  const weOwn = grant.source_data_domain_id === viewpointDomainId;
  const expiry = expiryNote(grant);
  return (
    <li className="cbg__row">
      <div className="cbg__rowhead">
        <span className="cbg__flow">
          {domainName(domains, grant.target_data_domain_id)}
          <span className="cbg__arrow" aria-label="may see into">
            {" may see into "}
          </span>
          {domainName(domains, grant.source_data_domain_id)}
        </span>
        <span className="cbg__spacer" />
        <Pill tone={STATUS_TONE[grant.status] ?? "mute"}>{grant.status.toLowerCase()}</Pill>
      </div>
      <div className="cbg__meta">
        {weOwn ? "your domain is being looked into" : "your domain is looking in"}
        {grant.edge_kinds.length > 0 ? ` · ${grant.edge_kinds.join(", ")}` : " · all edge kinds"}
        {expiry ? ` · ${expiry}` : ""}
      </div>
      <div className="cbg__reason">{grant.reason}</div>
      <div className="cbg__who">
        requested by {grant.requested_by}
        {grant.approved_by ? ` · approved by ${grant.approved_by}` : ""}
        {grant.status === "PENDING_APPROVAL"
          ? " · waiting for a decision on the Review queue"
          : ""}
      </div>
    </li>
  );
}

export function CrossBoundaryGrants({
  domainId,
  domains,
  /** Pre-fill the request form with this domain — the caller passes the domain
   *  the graph reported as withheld, so the fix is one click from the problem. */
  suggestedSourceDomainId,
  onGranted,
}: {
  domainId: string;
  domains: readonly DataDomainRead[];
  suggestedSourceDomainId?: string | null;
  onGranted?: () => void;
}) {
  const [grants, setGrants] = useState<CrossBoundaryGrantRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const [sourceDomainId, setSourceDomainId] = useState("");
  const [reason, setReason] = useState("");

  const load = useCallback(
    (signal?: AbortSignal) => {
      setError(null);
      fetchCrossBoundaryGrants(domainId, signal)
        .then(setGrants)
        .catch((e: unknown) => {
          if ((e as Error)?.name === "AbortError") return;
          setGrants([]);
          setError(e instanceof ApiError ? e.detail : (e as Error).message);
        });
    },
    [domainId],
  );

  useEffect(() => {
    const ac = new AbortController();
    setGrants(null);
    setNotice(null);
    load(ac.signal);
    return () => ac.abort();
  }, [load]);

  useEffect(() => {
    setSourceDomainId(suggestedSourceDomainId ?? "");
    if (suggestedSourceDomainId) setOpen(true);
  }, [suggestedSourceDomainId]);

  const submit = useCallback(async () => {
    if (!sourceDomainId || reason.trim().length < 3) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      // The grant is requested ON the domain that owns the data, naming this
      // domain as the one that wants to look in.
      await requestCrossBoundaryGrant(sourceDomainId, {
        target_data_domain_id: domainId,
        reason: reason.trim(),
      });
      setNotice(
        "Requested. It is on the Review queue now — someone other than you has to approve it before any cross-domain edge renders.",
      );
      setReason("");
      setOpen(false);
      load();
      onGranted?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [sourceDomainId, reason, domainId, load, onGranted]);

  const otherDomains = domains.filter((d) => d.id !== domainId);

  return (
    <div className="cbg">
      <div className="cbg__sub">
        Cross-boundary grants
        <button className="cbg__toggle" onClick={() => setOpen((v) => !v)}>
          {open ? "Cancel" : "Request access"}
        </button>
      </div>

      {open ? (
        <div className="cbg__form">
          <Field label="See into which domain">
            <select
              value={sourceDomainId}
              onChange={(e) => setSourceDomainId(e.target.value)}
              aria-label="See into which domain"
            >
              <option value="">Choose a domain…</option>
              {otherDomains.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Why">
            <input
              type="text"
              value={reason}
              placeholder="what this is needed for…"
              onChange={(e) => setReason(e.target.value)}
              aria-label="Why"
            />
          </Field>
          <Button
            variant="primary"
            disabled={busy || !sourceDomainId || reason.trim().length < 3}
            onClick={() => void submit()}
          >
            {busy ? "Requesting…" : "Request"}
          </Button>
        </div>
      ) : null}

      {notice ? (
        <div className="cbg__notice" role="status">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="cbg__error" role="alert">
          {error}
        </div>
      ) : null}

      {grants === null ? (
        <div className="cbg__load" role="status">
          Loading grants…
        </div>
      ) : grants.length === 0 ? (
        <div className="cbg__none">
          No grants either way. Nothing outside this domain can be seen from it, and nothing
          outside can see in.
        </div>
      ) : (
        <ol className="cbg__list">
          {grants.map((grant) => (
            <GrantRow
              key={grant.id}
              grant={grant}
              domains={domains}
              viewpointDomainId={domainId}
            />
          ))}
        </ol>
      )}
    </div>
  );
}
