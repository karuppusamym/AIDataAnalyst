import { useCallback, useEffect, useMemo, useRef, useState } from "react";
/* Filters and selection live in the URL so a filtered view is shareable and
   survives Back/Forward. This screen carried a verbatim copy of the old hook
   -- a `useState` seeded once from `location.search`, subscribed to nothing --
   so its idea of the selection and the address bar drifted apart the first
   time either the Back button or a same-screen link was used (review
   2026-09-05, F09 - R07). The shared hook reads one location store. */
import { useUrlState } from "../lib/useUrlState";
import { DescriptionEditor } from "../components/DescriptionEditor";
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
import { pushLocation } from "../lib/location";
import "./CatalogScreen.css";

import { useOrgId } from "../lib/org";
const nf = new Intl.NumberFormat("en-US");

/* ---------------------------------------------------------------------------
   R11-S13 (17B). A checked row here is sent to Stewardship's Bulk actions as
   an EXPLICIT selection (`?ids=`), never a second bulk-action UI: the same
   `bulk-tag`/`bulk-classify`/`bulk-own`/`bulk-certify` endpoints Bulk actions
   already calls, so a steward who checks rows here and one who opens Bulk
   actions and types a pattern that happens to match the same rows run the
   identical write path -- same roles, same request shape, same audit. See
   `StewardshipScreen.tsx`'s `StewardshipBulkActions` for the receiving side.

   This also retires the "Certify…" stub that used to sit here disabled: bulk
   certify already existed by filter in Bulk actions, so it needed a way to
   reach it with these rows rather than a second, parallel implementation. */
const CATALOG_BULK_ACTIONS = [
  { value: "tag", label: "Tag" },
  { value: "classify", label: "Classify" },
  { value: "own", label: "Assign ownership" },
  { value: "certify", label: "Certify" },
] as const;
type CatalogBulkAction = (typeof CATALOG_BULK_ACTIONS)[number]["value"];

/* Mirrors the cap `generateDrafts` below already applies to a checked batch.
   Bulk actions' own backend cap is 500 (`CATALOG_BULK_ACTION_MAX_ITEMS`), but
   the ids travel as a plain `?ids=` query field (see `routes.ts`), and a
   comma-joined list of UUIDs past a few hundred risks the URL itself rather
   than anything server-side -- so this is a URL-safety cap, not a copy of the
   backend's. */
const BULK_SELECTION_CAP = 100;


export function CatalogScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();

  const q = params.get("q") ?? "";
  const datasourceId = params.get("ds") ?? "";
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
  const [bulkAction, setBulkAction] = useState<CatalogBulkAction>("certify");

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
        { organizationId: ORG, datasourceId, q, objectType, certification, limit: 100 },
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
  }, [datasourceId, q, objectType, certification]);

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
        datasourceId,
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
  }, [cursor, loadingMore, loading, datasourceId, q, objectType, certification]);

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

  /* 17B: hand the checked rows to Stewardship's Bulk actions as an explicit
     id list. Nothing runs here or there until the steward submits that
     form -- this only opens it pre-filled, same as any other cross-screen
     link (`buildLink`/`pushLocation`). */
  const sendToBulkActions = useCallback(() => {
    const ids = Array.from(checked);
    if (ids.length === 0 || ids.length > BULK_SELECTION_CAP) return;
    pushLocation({
      screen: "stewardship",
      params: { view: "bulk", action: bulkAction, ids: ids.join(",") },
    });
  }, [checked, bulkAction]);

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

      {datasourceId ? (
        <div className="cat__scope" role="status">
          <span>Showing tables from the selected source.</span>
          <Button onClick={() => setParams({ ds: null, asset: null })}>Show all sources</Button>
        </div>
      ) : null}

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
                  : "Generate a metadata-drafted description for each selected asset."
              }
            >
              {draftBusy ? "Generating…" : "Generate table description drafts"}
            </Button>
            <Field label="Bulk action">
              <select
                value={bulkAction}
                onChange={(e) => setBulkAction(e.target.value as CatalogBulkAction)}
              >
                {CATALOG_BULK_ACTIONS.map((a) => (
                  <option key={a.value} value={a.value}>{a.label}</option>
                ))}
              </select>
            </Field>
            <Button
              onClick={sendToBulkActions}
              disabled={checked.size > BULK_SELECTION_CAP}
              title={
                checked.size > BULK_SELECTION_CAP
                  ? `Select at most ${BULK_SELECTION_CAP} rows to send to Bulk actions at once.`
                  : "Open Stewardship's Bulk actions with these rows as an explicit selection — nothing runs until you submit there."
              }
            >
              Send to Bulk actions…
            </Button>
            <Button onClick={() => setChecked(new Set())}>Clear</Button>
          </div>
        ) : null}
        {selected ? (
          <div className="cat__rowaction" role="status">
            <DescriptionEditor key={selected.id} tableId={selected.id} currentText={selected.description ?? ""} />
            <Button
              onClick={() => void generateDrafts([selected.id])}
              disabled={draftBusy}
              title="Generate a metadata-drafted description for this asset. Submit it for review from the Description drafts screen."
            >
              {draftBusy ? "Generating…" : "Generate table description draft"}
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
