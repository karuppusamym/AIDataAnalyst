import { useEffect, useState } from "react";
import type { CatalogAssetEvidence, CatalogRowRead } from "../lib/types";
import { fetchAssetEvidence } from "../lib/api";
import { Button, Empty, Pill } from "./primitives";
import "./EvidencePane.css";

/* ---------------------------------------------------------------------------
   Evidence pane — Module 21 §7.

   Two rules this encodes, both of which the current portal breaks:
   1. Every claim shows WHERE IT CAME FROM. A description is not "the
      description"; it is either an approved fact or a model proposal, and the
      pane says which, per ADR-0001.
   2. The pane is PERMALINKABLE. A reviewer sends a colleague the evidence,
      not a screenshot — so selection lives in the URL, not in component state.
--------------------------------------------------------------------------- */

export function EvidencePane({
  row,
  onClose,
}: {
  row: CatalogRowRead | null;
  onClose: () => void;
}) {
  const [evidence, setEvidence] = useState<CatalogAssetEvidence | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!row) {
      setEvidence(null);
      return;
    }
    const ac = new AbortController();
    setEvidence(null);
    fetchAssetEvidence(row.id, ac.signal)
      .then(setEvidence)
      .catch((e: unknown) => {
        if ((e as Error)?.name !== "AbortError") setEvidence(null);
      });
    return () => ac.abort();
  }, [row]);

  useEffect(() => {
    setCopied(false);
  }, [row]);

  if (!row) {
    return (
      <aside className="evp evp--idle" aria-label="Evidence">
        <Empty
          title="Select an asset"
          hint="Its evidence — where every claim came from — appears here."
        />
      </aside>
    );
  }

  const permalink = `${location.origin}${location.pathname}?asset=${row.id}`;

  return (
    <aside className="evp" aria-label={`Evidence for ${row.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={row.name}>{row.name}</div>
          <div className="evp__path">
            {row.datasource_name} · {row.schema_name} · {row.object_type.toLowerCase()}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close evidence">×</button>
      </header>

      <div className="evp__body">
        {evidence === null ? (
          <div className="evp__load" role="status">Loading evidence…</div>
        ) : (
          <ol className="evl">
            {evidence.items.map((it) => (
              <li key={it.label} className={`evi evi--${it.kind ?? "info"}`}>
                <div className="evi__label">{it.label}</div>
                <div className="evi__value">{it.value}</div>
                <div className="evi__source">{it.source}</div>
              </li>
            ))}
          </ol>
        )}

        {row.glossary_terms.length > 0 ? (
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
        <span className="evp__hint">Permission-aware · UX-7</span>
      </footer>
    </aside>
  );
}
