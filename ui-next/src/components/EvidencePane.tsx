import { useEffect, useState } from "react";
import type { AssetEvidenceRead } from "../lib/types";
import type { CatalogRowRead } from "../lib/ui-types";
import { ApiError, fetchAssetEvidence } from "../lib/api";
import { Button, Empty, Pill } from "./primitives";
import "./EvidencePane.css";

/* ---------------------------------------------------------------------------
   Evidence pane — Module 21 §7 / UX-7.

   Three rules this encodes:
   1. Every claim shows WHERE IT CAME FROM. A description is not "the
      description"; it is either an approved fact or a model proposal, and the
      pane says which, per ADR-0001.
   2. The pane is PERMALINKABLE, and durably so: it resolves by `tableId`
      alone, straight off `GET /v1/metadata/tables/{id}/evidence` (UX-13's own
      permalink -- durable URL, no request body, no session-only state). It
      does NOT require `row` (the `CatalogRowRead` the catalog grid already
      loaded) to be present -- `row` only adds nicer header chrome
      (datasource/schema/glossary terms) when it happens to be loaded.
      Without that decoupling, a colleague opening `?asset=<id>` for a table
      outside the sender's current filter/page would see "Select an asset"
      instead of the evidence: the URL would look shareable but silently fail
      to resolve for anyone whose loaded page didn't happen to contain that
      row. `row` is progressive enhancement; `tableId` is the actual link.
   3. PERMISSION-AWARE: resolution always goes through the same
      `fetchAssetEvidence` call against the same gated endpoint
      (`asset_evidence_api.py`'s `_authorize_table_read`) -- there is no
      locally-cached or embedded bypass. An unauthorized viewer gets the same
      403 the endpoint gives directly, surfaced here rather than silently
      swallowed.
--------------------------------------------------------------------------- */

export function EvidencePane({
  tableId,
  row,
  onClose,
}: {
  /** The one thing a permalink actually needs to resolve evidence. */
  tableId: string | null;
  /** Optional: the matching `CatalogRowRead`, when the grid already has it
   *  loaded. Purely cosmetic (name/path/glossary terms) -- evidence itself
   *  never depends on this being present. */
  row?: CatalogRowRead | null;
  onClose: () => void;
}) {
  const [evidence, setEvidence] = useState<AssetEvidenceRead | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!tableId) {
      setEvidence(null);
      setError(null);
      return;
    }
    const ac = new AbortController();
    setEvidence(null);
    setError(null);
    fetchAssetEvidence(tableId, ac.signal)
      .then(setEvidence)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setEvidence(null);
        setError(e as Error);
      });
    return () => ac.abort();
  }, [tableId]);

  useEffect(() => {
    setCopied(false);
  }, [tableId]);

  if (!tableId) {
    return (
      <aside className="evp evp--idle" aria-label="Evidence">
        <Empty
          title="Select an asset"
          hint="Its evidence — where every claim came from — appears here."
        />
      </aside>
    );
  }

  const permalink = `${location.origin}${location.pathname}?asset=${tableId}`;
  const exportHref = `/v1/metadata/tables/${tableId}/evidence/export`;
  const displayName = row?.name ?? evidence?.table_name ?? tableId;

  return (
    <aside className="evp" aria-label={`Evidence for ${displayName}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={displayName}>{displayName}</div>
          <div className="evp__path">
            {row
              ? `${row.datasource_name} · ${row.schema_name} · ${row.object_type.toLowerCase()}`
              : evidence
                ? "Opened from a permalink"
                : ""}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close evidence">×</button>
      </header>

      <div className="evp__body">
        {error ? (
          <div className="evp__error" role="alert">
            {error instanceof ApiError && error.status === 403
              ? "You are not authorized to view this evidence."
              : error instanceof ApiError && error.status === 404
                ? "This asset no longer exists."
                : `Evidence could not be loaded: ${
                    error instanceof ApiError ? error.detail : error.message
                  }`}
          </div>
        ) : evidence === null ? (
          <div className="evp__load" role="status">Loading evidence…</div>
        ) : (
          <ol className="evl">
            {evidence.items.map((it, i) => (
              <li key={`${it.category}-${i}`} className="evi evi--info">
                <div className="evi__label">{it.category.replace(/_/g, " ")}</div>
                <div className="evi__value">{it.claim}</div>
                <div className="evi__source">{it.source}</div>
              </li>
            ))}
          </ol>
        )}

        {row && row.glossary_terms.length > 0 ? (
          <div className="evp__terms">
            <div className="evp__sub">Linked glossary terms</div>
            <div className="evp__pills">
              {row.glossary_terms.map((t, i) => (
                <Pill key={`${t}-${i}`} tone="accent">{t}</Pill>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <footer className="evp__foot">
        <Button
          onClick={() => {
            void navigator.clipboard?.writeText(permalink);
            setCopied(true);
          }}
        >
          {copied ? "Link copied" : "Copy evidence link"}
        </Button>
        <a className="evp__export" href={exportHref} target="_blank" rel="noreferrer">
          Export JSON
        </a>
        <span className="evp__hint">Permission-aware · UX-7</span>
      </footer>
    </aside>
  );
}
