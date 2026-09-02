import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AiDecisionRead } from "../lib/types";
import { ApiError, fetchLineageRefusals, fetchRunDecisions } from "../lib/api";
import { VirtualList } from "../components/VirtualList";
import { Empty, ErrorState, Pill } from "../components/primitives";
import "../components/EvidencePane.css";
import "./LineageRefusalScreen.css";

/* ---------------------------------------------------------------------------
   Lineage refusal view — UX-15, the Catalog pattern applied to LN-3's real
   `GET /v1/ai-decisions/refusals` (`ai_decision_lineage_api.py`).

   The legacy shell (`ui/app.js`) has no refusal-specific view at all — there
   is no "refus" string anywhere in it, checked directly. What UX-15 migrates
   here, then, is not a re-skin of an existing screen but the first real
   surface for something the backend has recorded since LN-3 landed: every
   time an agent run refused to use or act on an asset, and why. Structurally
   this is still exactly the Catalog pattern:
     1. URL state       run (the focused refusal's run id, permalinkable)
     2. abortable fetch  one in-flight request per view
     3. virtualization   `VirtualList`
     4. evidence pane    every decision the focused run made, not only the
                         refusal itself -- what it considered before it
                         refused (`GET /v1/ai-decisions/{run_id}`)

   Honest scope note: `list_refusals` gates on `PlatformAdmin`/`DataAdmin`
   only (its own `require_roles`, unchanged here) -- narrower than most of
   this shell's other screens. A Steward or Analyst opening this screen gets
   the same 403 `ErrorState` any other gated call renders, not a silent
   empty list.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

function useUrlState() {
  const [params, setParams] = useState(() => new URLSearchParams(location.search));
  const update = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      history.replaceState(null, "", `${location.pathname}?${next}`);
      return next;
    });
  }, []);
  return [params, update] as const;
}

function RefusalRow({
  decision,
  focused,
  onFocus,
}: {
  decision: AiDecisionRead;
  focused: boolean;
  onFocus: () => void;
}) {
  return (
    <article className={`refusal${focused ? " refusal--sel" : ""}`} aria-label={decision.target_node}>
      <button className="refusal__click" onClick={onFocus}>
        <div className="refusal__badges">
          <Pill tone="bad">refusal</Pill>
          {decision.control_version ? <Pill tone="mute">{decision.control_version}</Pill> : null}
        </div>
        <div className="refusal__nodes">
          <span className="refusal__src">{decision.source_node}</span>
          <span className="refusal__arrow" aria-hidden="true">→</span>
          <span className="refusal__tgt">{decision.target_node}</span>
        </div>
        <p className="refusal__reason">{decision.reason}</p>
        <div className="refusal__meta">
          <span>run {decision.run_id}</span>
          <span>·</span>
          <time dateTime={decision.decided_at}>{decision.decided_at.slice(0, 19).replace("T", " ")}</time>
        </div>
      </button>
    </article>
  );
}

function RunEvidence({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [decisions, setDecisions] = useState<AiDecisionRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    setDecisions(null);
    setError(null);
    fetchRunDecisions(runId, ORG, ac.signal)
      .then(setDecisions)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
  }, [runId]);

  const permalink = `${location.origin}${location.pathname}?run=${runId}`;

  return (
    <aside className="evp" aria-label={`Decisions for run ${runId}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name">Run {runId}</div>
          <div className="evp__path">Every decision this run made, in order</div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close">×</button>
      </header>
      <div className="evp__body">
        {error ? (
          <div className="evp__error" role="alert">{error}</div>
        ) : decisions === null ? (
          <div className="evp__load" role="status">Loading run decisions…</div>
        ) : (
          <ol className="evl">
            {decisions.map((d) => (
              <li key={d.id} className={`evi ${d.decision_type === "REFUSAL" ? "evi--bad" : "evi--info"}`}>
                <div className="evi__label">{d.decision_type.replace(/_/g, " ")}</div>
                <div className="evi__value">
                  {d.source_node} → {d.target_node}
                </div>
                <div className="evi__source">{d.reason}</div>
              </li>
            ))}
          </ol>
        )}
      </div>
      <footer className="evp__foot">
        <button
          className="btn btn--quiet"
          onClick={() => void navigator.clipboard?.writeText(permalink)}
        >
          Copy permalink
        </button>
      </footer>
    </aside>
  );
}

export function LineageRefusalScreen() {
  const [params, setParams] = useUrlState();
  const focusedRun = params.get("run");

  const [items, setItems] = useState<AiDecisionRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchLineageRefusals(ORG, { limit: 50, offset: 0 }, ac.signal);
      if (seq !== reqSeq.current) return;
      setItems(page.items);
      setTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const loadMore = useCallback(async () => {
    if (loadingMore || loading || items.length >= (total ?? 0)) return;
    setLoadingMore(true);
    try {
      const page = await fetchLineageRefusals(ORG, { limit: 50, offset: items.length });
      setItems((prev) => [...prev, ...page.items]);
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, loading, items.length, total]);

  const focusedDecision = useMemo(
    () => items.find((d) => d.run_id === focusedRun) ?? null,
    [items, focusedRun],
  );

  return (
    <div className="refscreen">
      <header className="refscreen__head">
        <div>
          <h1 className="refscreen__h1">Lineage refusals</h1>
          <p className="refscreen__lede">
            Every time an agent run refused to use or act on an asset, and the control
            that stopped it — recorded as a refusal edge (LN-3).
          </p>
        </div>
        <div className="refscreen__stats">
          <span><b className="tnum">{total !== null ? total : "—"}</b> refusals</span>
        </div>
      </header>

      <div className="refscreen__main">
        {error ? (
          <ErrorState
            title="Refusals could not be loaded"
            detail={error}
            onRetry={() => void load()}
          />
        ) : loading ? (
          <div className="refscreen__skeleton" role="status" aria-live="polite">
            Loading refusals…
          </div>
        ) : (
          <VirtualList
            items={items}
            getKey={(d) => d.id}
            ariaLabel="AI refusal decisions"
            estimateSize={116}
            totalCount={total}
            onReachEnd={() => void loadMore()}
            loadingMore={loadingMore}
            emptyState={<Empty title="No refusals recorded" hint="Nothing has been refused for this organization yet." />}
            renderItem={(d) => (
              <RefusalRow decision={d} focused={d.run_id === focusedRun} onFocus={() => setParams({ run: d.run_id })} />
            )}
          />
        )}
        {focusedDecision ? (
          <RunEvidence runId={focusedDecision.run_id} onClose={() => setParams({ run: null })} />
        ) : null}
      </div>
    </div>
  );
}
