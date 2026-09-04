import { useRef, useCallback } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { CatalogRowRead } from "../lib/ui-types";
import { Pill, StateDot } from "./primitives";
import type { Tone } from "./primitives";
import "./CatalogTable.css";

/* ---------------------------------------------------------------------------
   Virtualized catalog table — Module 21 §6: 1M rows without lockup.

   Only the visible window is in the DOM. The row height is fixed (--row-h) so
   the virtualizer never has to measure, which is what keeps scrolling smooth
   at a million rows; if a future column needs to wrap, it gets truncated with
   a title attribute rather than being allowed to change row height.

   Accessibility: this is a real grid, not divs pretending to be one. Because
   only a window of rows exists, aria-rowcount declares the true total and each
   row carries its absolute aria-rowindex — otherwise a screen reader announces
   "row 3 of 40" while the user is 200,000 rows down.
--------------------------------------------------------------------------- */

const certEvidenceTitle = (row: CatalogRowRead): string | undefined => {
  // P3-09: only surface the tooltip when a CERTIFIED row carries a
  // populated `certification_evidence_summary`. Legacy rows and non-
  // CERTIFIED states return undefined so the browser renders no title.
  if (row.certification !== "CERTIFIED") return undefined;
  const s = row.certification_evidence_summary;
  if (!s) return undefined;
  const parts: string[] = [];
  if (s.description_version_id) parts.push("description v" + s.description_version_id.slice(0, 8));
  parts.push(s.active_owner_count + " owner" + (s.active_owner_count === 1 ? "" : "s"));
  parts.push(s.open_incident_count_at_certify + " open incident" + (s.open_incident_count_at_certify === 1 ? "" : "s") + " at certify");
  parts.push(s.glossary_term_count + " glossary term" + (s.glossary_term_count === 1 ? "" : "s"));
  const base = "Based on: " + parts.join(", ");
  return s.backfilled ? base + " (backfilled)" : base;
};

const certTone = (c: CatalogRowRead["certification"]): Tone =>
  c === "CERTIFIED" ? "ok" : c === "NONE" ? "mute" : "bad";
const qualityTone = (q: CatalogRowRead["quality"]): Tone =>
  q === "PASSING" ? "ok" : q === "UNKNOWN" ? "mute" : q === "STALE" ? "warn" : "bad";

const nf = new Intl.NumberFormat("en-US");

// Same relative-time convention QualityScreen already uses (relTime) --
// copied rather than shared, matching this codebase's existing pattern of
// small per-file formatters (nf itself is already redefined per-screen).
const relTime = (iso: string): string => {
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

/** P1-03: pale glossary-term chips shown under the description snippet on a
 *  catalog row. Server exposes `row.glossary_terms: string[]` via
 *  `_glossary_terms_by_table`; up to 3 chips are shown inline and a "+N more"
 *  chip absorbs the tail so the row stays one line tall (Module 21 §6: fixed
 *  row height, never re-measured). Click on a chip navigates to the
 *  Business Meaning screen scoped to that term; click on "+N more" opens the
 *  same screen scoped to this asset instead. */
const MAX_INLINE_CHIPS = 3;
function GlossaryChipRow({ terms, tableId }: { terms: readonly string[]; tableId: string }) {
  const visible = terms.slice(0, MAX_INLINE_CHIPS);
  const overflow = terms.length - visible.length;
  const openTerm = (term: string) => (e: React.MouseEvent) => {
    e.stopPropagation();
    const q = encodeURIComponent(term);
    window.location.href = `/business-meaning?view=glossary&term=${q}`;
  };
  const openAllForTable = (e: React.MouseEvent) => {
    e.stopPropagation();
    window.location.href = `/business-meaning?view=glossary&asset=${encodeURIComponent(tableId)}`;
  };
  return (
    <span
      className="cglossary"
      role="list"
      aria-label={`${terms.length} glossary term${terms.length === 1 ? "" : "s"}`}
    >
      {visible.map((t) => (
        <button
          key={t}
          type="button"
          role="listitem"
          className="cglossary__chip"
          title={`Open glossary term: ${t}`}
          onClick={openTerm(t)}
        >
          {t}
        </button>
      ))}
      {overflow > 0 ? (
        <button
          type="button"
          role="listitem"
          className="cglossary__chip cglossary__chip--more"
          title={`${overflow} more glossary term${overflow === 1 ? "" : "s"}`}
          onClick={openAllForTable}
        >
          +{overflow} more
        </button>
      ) : null}
    </span>
  );
}

export interface CatalogTableProps {
  rows: CatalogRowRead[];
  totalCount: number | null;
  selectedId: string | null;
  checked: ReadonlySet<string>;
  onSelect: (row: CatalogRowRead) => void;
  onToggleCheck: (id: string) => void;
  onToggleAllVisible: () => void;
  onReachEnd: () => void;
  loadingMore: boolean;
}

export function CatalogTable({
  rows,
  totalCount,
  selectedId,
  checked,
  onSelect,
  onToggleCheck,
  onToggleAllVisible,
  onReachEnd,
  loadingMore,
}: CatalogTableProps) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 38,
    overscan: 12,
  });

  const items = virtualizer.getVirtualItems();
  const last = items[items.length - 1];

  // Fetch the next keyset page when the user nears the end of what's loaded.
  // Guarded on loadingMore so a fast scroll cannot queue duplicate pages.
  if (last && last.index >= rows.length - 20 && !loadingMore) {
    queueMicrotask(onReachEnd);
  }

  const allVisibleChecked =
    rows.length > 0 && rows.every((r) => checked.has(r.id));

  const onRowKey = useCallback(
    (e: React.KeyboardEvent, row: CatalogRowRead) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onSelect(row);
      }
    },
    [onSelect],
  );

  return (
    <div className="ctable">
      <div className="ctable__head" role="presentation">
        <div className="cc cc--check">
          <input
            type="checkbox"
            checked={allVisibleChecked}
            onChange={onToggleAllVisible}
            aria-label="Select all loaded rows"
          />
        </div>
        <div className="cc cc--name">Asset</div>
        <div className="cc cc--desc">Description</div>
        <div className="cc cc--owner">Owner</div>
        <div className="cc cc--state">Certification</div>
        <div className="cc cc--state">Quality</div>
        <div className="cc cc--rows">Rows</div>
        <div className="cc cc--updated">Updated</div>
      </div>

      <div
        ref={parentRef}
        className="ctable__scroll"
        role="grid"
        aria-label="Catalog assets"
        aria-rowcount={totalCount ?? -1}
        tabIndex={0}
      >
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {items.map((v) => {
            const row = rows[v.index];
            if (!row) return null;
            const isSel = row.id === selectedId;
            return (
              <div
                key={row.id}
                role="row"
                aria-rowindex={v.index + 1}
                aria-selected={isSel}
                tabIndex={0}
                className={`crow${isSel ? " crow--sel" : ""}`}
                style={{ transform: `translateY(${v.start}px)` }}
                onClick={() => onSelect(row)}
                onKeyDown={(e) => onRowKey(e, row)}
              >
                <div className="cc cc--check" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={checked.has(row.id)}
                    onChange={() => onToggleCheck(row.id)}
                    aria-label={`Select ${row.name}`}
                  />
                </div>

                <div className="cc cc--name" role="gridcell">
                  <StateDot
                    tone={qualityTone(row.quality)}
                    title={`Quality: ${row.quality.toLowerCase().replace("_", " ")}`}
                  />
                  <span className="cname" title={`${row.schema_name}.${row.name}`}>
                    {row.name}
                  </span>
                  <span className="cpath">
                    {row.datasource_name} · {row.schema_name}
                  </span>
                </div>

                <div className="cc cc--desc" role="gridcell">
                  {row.description ? (
                    <span className={row.description_is_proposed ? "cdesc cdesc--prop" : "cdesc"}>
                      {row.description_is_proposed ? (
                        <Pill tone="warn">proposed</Pill>
                      ) : null}
                      <span title={row.description}>{row.description}</span>
                    </span>
                  ) : (
                    <span className="cnone">Undocumented</span>
                  )}
                  {row.glossary_terms && row.glossary_terms.length > 0 ? (
                    <GlossaryChipRow terms={row.glossary_terms} tableId={row.id} />
                  ) : null}
                </div>

                <div className="cc cc--owner" role="gridcell">
                  {row.owner ?? <span className="cnone">Unowned</span>}
                </div>

                <div
                  className="cc cc--state"
                  role="gridcell"
                  /* P3-09: hover tooltip summarising the certification's
                     structured evidence (see `CertificationEvidenceSummary`
                     on the row). Falls back cleanly when the current cert
                     is legacy (evidence null) or the row is EXPIRED /
                     REVOKED / NONE -- `title` stays undefined so the
                     browser renders no tooltip rather than an empty one. */
                  title={certEvidenceTitle(row)}
                >
                  <Pill tone={certTone(row.certification)}>
                    {row.certification.toLowerCase()}
                  </Pill>
                </div>

                <div className="cc cc--state" role="gridcell">
                  <Pill tone={qualityTone(row.quality)}>
                    {row.quality.toLowerCase().replace("_", " ")}
                  </Pill>
                </div>

                <div className="cc cc--rows tnum" role="gridcell">
                  {row.row_count_estimate === null ? (
                    <span className="cnone">—</span>
                  ) : (
                    nf.format(row.row_count_estimate)
                  )}
                </div>

                <div className="cc cc--updated tnum" role="gridcell" title={new Date(row.updated_at).toLocaleString()}>
                  {relTime(row.updated_at)}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="ctable__foot">
        <span>
          {nf.format(rows.length)} loaded
          {totalCount !== null ? ` of ${nf.format(totalCount)}` : ""}
        </span>
        {loadingMore ? <span className="ctable__more">Loading next page…</span> : null}
      </div>
    </div>
  );
}
