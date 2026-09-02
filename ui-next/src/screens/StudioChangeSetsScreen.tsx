import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { StudioChangeItemRead, StudioChangeSetRead, StudioDiffRead, StudioImpactPreview } from "../lib/types";
import {
  ApiError,
  fetchStudioChangeSetItems,
  fetchStudioChangeSets,
  fetchStudioDiff,
  fetchStudioImpact,
  submitStudioChangeSet,
} from "../lib/api";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "../components/EvidencePane.css";
import "./StudioChangeSetsScreen.css";

/* ---------------------------------------------------------------------------
   Studio change sets — UX-15, the Catalog pattern applied to the real
   authoring-environment API (`studio_api.py`, module 19 / ST-A7).

     1. URL state       status filter, `cs` (the focused change set, permalinkable)
     2. abortable fetch  one in-flight list request per view
     3. virtualization   `VirtualList`
     4. evidence pane    a change set's own items/diff/impact, fetched
                         real-time from `.../items`, `.../diff`, `.../impact`

   Submission calls the real `POST .../submit` -- the test-gated (ST-A7) and
   eval-regression-gated (ST-A8) path that materializes any CONTEXT_PRODUCT
   item into the same `GovernanceReview` queue `ReviewQueueScreen` reads, not
   a client-side status flip. A 409 from that gate (untested items, a
   regressed eval question) renders as the endpoint's own detail string,
   exactly like every other governed write this shell makes.
--------------------------------------------------------------------------- */

const statusTone = (s: string): Tone =>
  s === "MERGED" ? "ok" : s === "SUBMITTED" ? "info" : s === "REJECTED" ? "bad" : s === "TESTING" ? "warn" : "mute";

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

function ChangeSetRow({
  cs,
  selected,
  onSelect,
}: {
  cs: StudioChangeSetRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`csrow${selected ? " csrow--sel" : ""}`} aria-label={cs.name}>
      <button className="csrow__click" onClick={onSelect}>
        <div className="csrow__badges">
          <Pill tone={statusTone(cs.status)}>{cs.status.toLowerCase()}</Pill>
          {cs.conflict_status !== "CLEAN" ? <Pill tone="bad">{cs.conflict_status.toLowerCase()}</Pill> : null}
        </div>
        <h3 className="csrow__title">{cs.name}</h3>
        <div className="csrow__meta">
          <span>{cs.author}</span>
          <span>·</span>
          <time dateTime={cs.updated_at}>{cs.updated_at.slice(0, 10)}</time>
        </div>
      </button>
    </article>
  );
}

function ChangeSetDetail({ cs, onSubmitted }: { cs: StudioChangeSetRead; onSubmitted: () => void }) {
  const [items, setItems] = useState<StudioChangeItemRead[] | null>(null);
  const [diff, setDiff] = useState<StudioDiffRead | null>(null);
  const [impact, setImpact] = useState<StudioImpactPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    setItems(null);
    setDiff(null);
    setImpact(null);
    setError(null);
    Promise.all([
      fetchStudioChangeSetItems(cs.id, ac.signal),
      fetchStudioDiff(cs.id, ac.signal),
      fetchStudioImpact(cs.id, ac.signal),
    ])
      .then(([i, d, imp]) => {
        setItems(i);
        setDiff(d);
        setImpact(imp);
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      });
    return () => ac.abort();
  }, [cs.id]);

  const submit = useCallback(async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await submitStudioChangeSet(cs.id);
      onSubmitted();
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [cs.id, onSubmitted]);

  const permalink = `${location.origin}${location.pathname}?cs=${cs.id}`;
  const canSubmit = cs.status === "DRAFT" || cs.status === "TESTING";

  return (
    <aside className="evp" aria-label={`Detail for ${cs.name}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={cs.name}>{cs.name}</div>
          <div className="evp__path">{cs.author} · {cs.status.toLowerCase()}</div>
        </div>
      </header>
      <div className="evp__body">
        {error ? (
          <div className="evp__error" role="alert">{error}</div>
        ) : items === null ? (
          <div className="evp__load" role="status">Loading change set…</div>
        ) : (
          <>
            <div className="evp__sub">Items ({items.length})</div>
            <ol className="evl">
              {items.map((it) => (
                <li key={it.id} className="evi evi--info">
                  <div className="evi__label">{it.object_type} · {it.operation}</div>
                  <div className="evi__value">{it.object_id}</div>
                  <div className="evi__source">test status: {it.test_status.toLowerCase()}</div>
                </li>
              ))}
            </ol>

            <div className="evp__sub" style={{ marginTop: 14 }}>
              Impact ({impact?.affected_object_count ?? 0} affected)
            </div>
            {impact && impact.affected_objects.length > 0 ? (
              <pre className="cs__pre">{JSON.stringify(impact.affected_objects, null, 2)}</pre>
            ) : (
              <p className="cs__none">No downstream impact detected.</p>
            )}

            <div className="evp__sub" style={{ marginTop: 14 }}>Diff</div>
            {diff && diff.items.length > 0 ? (
              <pre className="cs__pre">{JSON.stringify(diff.items, null, 2)}</pre>
            ) : (
              <p className="cs__none">No diff recorded.</p>
            )}
          </>
        )}
      </div>
      <footer className="evp__foot" style={{ flexWrap: "wrap", gap: 8 }}>
        <Button onClick={() => void navigator.clipboard?.writeText(permalink)}>Copy link</Button>
        {canSubmit ? (
          <Button variant="primary" disabled={submitting} onClick={() => void submit()}>
            {submitting ? "Submitting…" : "Submit for review"}
          </Button>
        ) : (
          <span className="evp__hint">{cs.status.toLowerCase()} — nothing left to submit</span>
        )}
        {submitError ? <div className="cs__submiterr" role="alert">{submitError}</div> : null}
      </footer>
    </aside>
  );
}

export function StudioChangeSetsScreen() {
  const [params, setParams] = useUrlState();
  const statusFilter = params.get("status") ?? "ALL";
  const selectedId = params.get("cs");

  const [items, setItems] = useState<StudioChangeSetRead[]>([]);
  const [loading, setLoading] = useState(true);
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
      const rows = await fetchStudioChangeSets(
        { status: statusFilter !== "ALL" ? statusFilter : null, limit: 200 },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setItems(rows);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const selected = useMemo(() => items.find((cs) => cs.id === selectedId) ?? null, [items, selectedId]);

  return (
    <div className="csscreen">
      <header className="csscreen__head">
        <div>
          <h1 className="csscreen__h1">Studio change sets</h1>
          <p className="csscreen__lede">
            Draft, test and submit governed changes to metrics, tools, terms and context
            products — DRAFT → TESTING → SUBMITTED → MERGED/REJECTED.
          </p>
        </div>
      </header>

      <div className="csscreen__filters">
        <Field label="Status">
          <select value={statusFilter} onChange={(e) => setParams({ status: e.target.value === "ALL" ? null : e.target.value, cs: null })}>
            <option value="ALL">All</option>
            <option value="DRAFT">Draft</option>
            <option value="TESTING">Testing</option>
            <option value="SUBMITTED">Submitted</option>
            <option value="MERGED">Merged</option>
            <option value="REJECTED">Rejected</option>
          </select>
        </Field>
      </div>

      <div className="csscreen__main">
        {error ? (
          <ErrorState title="Studio change sets could not be loaded" detail={error} onRetry={() => void load()} />
        ) : loading ? (
          <div className="csscreen__skeleton" role="status" aria-live="polite">
            Loading change sets…
          </div>
        ) : (
          <VirtualList
            items={items}
            getKey={(cs) => cs.id}
            ariaLabel="Studio change sets"
            estimateSize={98}
            emptyState={<Empty title="No change sets" hint="Create one from the Studio authoring surface to see it here." />}
            renderItem={(cs) => (
              <ChangeSetRow cs={cs} selected={cs.id === selectedId} onSelect={() => setParams({ cs: cs.id })} />
            )}
          />
        )}
        {selected ? <ChangeSetDetail cs={selected} onSubmitted={() => void load()} /> : null}
      </div>
    </div>
  );
}
