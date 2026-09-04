import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CatalogRowRead } from "../lib/ui-types";
import {
  ApiError,
  classifyDescriptionDraftError,
  fetchCatalogRows,
  generateAssetDescriptionDrafts,
} from "../lib/api";
import { CatalogTable } from "../components/CatalogTable";
import { EvidencePane } from "../components/EvidencePane";
import { Button, ErrorState, Field, Pill } from "../components/primitives";
import "./CatalogScreen.css";

import { useOrgId } from "../lib/org";
const nf = new Intl.NumberFormat("en-US");

/** Filters live in the URL so a filtered catalog view is shareable — the same
 *  reason the evidence pane is permalinkable (UX-7). */
function useUrlState() {
  const [params, setParams] = useState(() => new URLSearchParams(location.search));
  const update = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      const query = next.toString();
      history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
      return next;
    });
  }, []);
  return [params, update] as const;
}

export function CatalogScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();

  const q = params.get("q") ?? "";
  const objectType = params.get("type") ?? "ALL";
  const certification = params.get("cert") ?? "ALL";
  const selectedId = params.get("asset");

  const [rows, setRows] = useState<CatalogRowRead[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  const [draftQ, setDraftQ] = useState(q);

  // P1-04: batch/single draft generation state. Kept co-located rather than
  // hoisted into `useUrlState` because it is transient by design — the
  // "success" banner should not survive a filter change or a hash navigation.
  const [draftBusy, setDraftBusy] = useState(false);
  const [draftMsg, setDraftMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // One in-flight request at a time. Aborting the previous one is what stops a
  // slow first page from overwriting the results of a newer, narrower filter.
  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const loadFirstPage = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchCatalogRows(
        { organizationId: ORG, q, objectType, certification, limit: 100 },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setRows(page.items);
      setCursor(page.next_cursor ?? null);
      setTotal(page.total ?? null);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [q, objectType, certification]);

  useEffect(() => {
    void loadFirstPage();
    return () => inflight.current?.abort();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore || loading) return;
    setLoadingMore(true);
    try {
      const page = await fetchCatalogRows({
        organizationId: ORG,
        q,
        objectType,
        certification,
        cursor,
        limit: 100,
      });
      setRows((prev) => [...prev, ...page.items]);
      setCursor(page.next_cursor ?? null);
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, loadingMore, loading, q, objectType, certification]);

  // Debounce typing so each keystroke does not become a request.
  useEffect(() => {
    const t = setTimeout(() => {
      if (draftQ !== q) setParams({ q: draftQ || null, asset: null });
    }, 250);
    return () => clearTimeout(t);
  }, [draftQ, q, setParams]);

  const selected = useMemo(
    () => rows.find((r) => r.id === selectedId) ?? null,
    [rows, selectedId],
  );

  const toggleCheck = useCallback((id: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleAllVisible = useCallback(() => {
    setChecked((prev) => {
      const all = rows.every((r) => prev.has(r.id));
      if (all) return new Set();
      return new Set(rows.map((r) => r.id));
    });
  }, [rows]);

  const undocumented = rows.filter((r) => !r.description).length;
  const uncertified = rows.filter((r) => r.certification !== "CERTIFIED").length;

  const generateDrafts = useCallback(
    async (tableIds: string[]) => {
      if (tableIds.length === 0) return;
      setDraftBusy(true);
      setDraftMsg(null);
      try {
        const page = await generateAssetDescriptionDrafts(ORG, tableIds);
        const created = page.drafts.length;
        const skipped = tableIds.length - created;
        const suffix = skipped > 0
          ? ` (${skipped} skipped — a draft is already open, or a rejected duplicate exists)`
          : "";
        setDraftMsg({
          kind: "ok",
          text: `${created} draft${created === 1 ? "" : "s"} generated${suffix}. View them in Description drafts.`,
        });
        setChecked(new Set());
      } catch (e) {
        const detail =
          e instanceof ApiError
            ? classifyDescriptionDraftError(e).detail
            : (e as Error).message;
        setDraftMsg({ kind: "err", text: `Could not generate drafts: ${detail}` });
      } finally {
        setDraftBusy(false);
      }
    },
    [ORG],
  );

  const openDrafts = useCallback(() => {
    if (location.hash !== "#/description-drafts") {
      history.pushState(null, "", "#/description-drafts");
    }
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }, []);

  return (
    <div className="cat">
      <header className="cat__head">
        <div>
          <h1 className="cat__h1">Catalog</h1>
          <p className="cat__lede">
            Every asset the platform can see, with the state that decides whether an
            agent may use it.
          </p>
        </div>
        <div className="cat__stats">
          <span><b className="tnum">{total !== null ? nf.format(total) : "—"}</b> assets</span>
          <span><b className="tnum">{nf.format(undocumented)}</b> undocumented loaded</span>
          <span><b className="tnum">{nf.format(uncertified)}</b> uncertified loaded</span>
        </div>
      </header>

      <div className="cat__filters">
        <Field label="Search">
          <input
            type="search"
            value={draftQ}
            placeholder="name or description…"
            onChange={(e) => setDraftQ(e.target.value)}
          />
        </Field>
        <Field label="Type">
          <select
            value={objectType}
            onChange={(e) => setParams({ type: e.target.value, asset: null })}
          >
            <option value="ALL">All</option>
            <option value="TABLE">Table</option>
            <option value="VIEW">View</option>
            <option value="MATERIALIZED_VIEW">Materialized view</option>
          </select>
        </Field>
        <Field label="Certification">
          <select
            value={certification}
            onChange={(e) => setParams({ cert: e.target.value, asset: null })}
          >
            <option value="ALL">All</option>
            <option value="CERTIFIED">Certified</option>
            <option value="EXPIRED">Expired</option>
            <option value="REVOKED">Revoked</option>
            <option value="NONE">Never certified</option>
          </select>
        </Field>
        <div className="cat__spacer" />
        {checked.size > 0 ? (
          <div className="cat__bulk" role="status">
            <Pill tone="accent">{nf.format(checked.size)} selected</Pill>
            <Button
              variant="primary"
              onClick={() => void generateDrafts(Array.from(checked))}
              disabled={draftBusy || checked.size > 100}
              title={
                checked.size > 100
                  ? "Select at most 100 rows to generate drafts in one batch."
                  : "Generate a model-drafted description for each selected asset."
              }
            >
              {draftBusy ? "Generating…" : "Generate description drafts"}
            </Button>
            <Button
              disabled
              title="Bulk certify is not available yet — certification is managed per asset in stewardship."
            >
              Certify…
            </Button>
            <Button onClick={() => setChecked(new Set())}>Clear</Button>
          </div>
        ) : null}
        {selected ? (
          <div className="cat__rowaction" role="status">
            <Button
              onClick={() => void generateDrafts([selected.id])}
              disabled={draftBusy}
              title="Generate a model-drafted description for this asset. Submit it for review from the Description drafts screen."
            >
              {draftBusy ? "Generating…" : "Generate description draft"}
            </Button>
          </div>
        ) : null}
      </div>

      {draftMsg ? (
        <div
          className={`cat__banner cat__banner--${draftMsg.kind}`}
          role={draftMsg.kind === "err" ? "alert" : "status"}
          aria-live="polite"
        >
          <span>{draftMsg.text}</span>
          {draftMsg.kind === "ok" ? (
            <Button onClick={openDrafts}>Open description drafts</Button>
          ) : null}
          <Button onClick={() => setDraftMsg(null)}>Dismiss</Button>
        </div>
      ) : null}

      <div className="cat__main">
        {error ? (
          <ErrorState detail={error} onRetry={() => void loadFirstPage()} />
        ) : loading ? (
          <div className="cat__skeleton" role="status" aria-live="polite">
            Loading catalog…
          </div>
        ) : (
          <CatalogTable
            rows={rows}
            totalCount={total}
            selectedId={selectedId}
            checked={checked}
            onSelect={(r) => setParams({ asset: r.id })}
            onToggleCheck={toggleCheck}
            onToggleAllVisible={toggleAllVisible}
            onReachEnd={() => void loadMore()}
            loadingMore={loadingMore}
          />
        )}
        <EvidencePane
          tableId={selectedId}
          row={selected}
          onClose={() => setParams({ asset: null })}
        />
      </div>
    </div>
  );
}
