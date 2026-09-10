import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { NegativeAssertionRead } from "../lib/types";
import {
  ApiError,
  fetchNegativeKnowledgeForSubject,
  liftNegativeAssertionSuppression,
  searchNegativeKnowledge,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { Button, ConfirmDialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./NegativeKnowledgeScreen.css";

/* ---------------------------------------------------------------------------
   Negative knowledge (Phase E / EE.3) — the registry of assertions a human
   has already rejected ("this table is NOT the customer master"), each
   carrying a suppression flag so the platform stops re-proposing something
   already rejected, plus a manual lift path for once something material
   changes. Built on the real, already-merged `negative_knowledge_api.py`:

     GET  /v1/negative-knowledge/search               -- filterable browse
     GET  /v1/negative-knowledge/{subject_id}         -- one subject's history
     POST /v1/negative-knowledge/{id}/lift-suppression -- manual lift

   Org-wide, like `AuditLedgerScreen` -- none of these three routes take an
   organization id in the path or query; scope is implicit server-side
   (`context.require_organization()`), so there is no datasource picker here.

   Two distinct read paths share one screen, deliberately not conflated: the
   filtered `search` (assertion_type / suppression_active) is the default
   view, and "look up by subject" is a separate control that switches to the
   per-subject endpoint entirely. Picking one clears the other's URL state so
   a reader is never looking at results that silently mix both queries.

   Pagination is a plain offset "load more" (`AuditLedgerScreen`'s own idiom
   against its offset-paginated route) rather than the virtualized list
   heavier screens use -- this registry has no reason to expect catalog-scale
   volume, so a windowed-DOM list would be complexity without a payoff.
--------------------------------------------------------------------------- */

const nf = new Intl.NumberFormat("en-US");

const relTime = (iso: string | null): string => {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

type SuppressionFilter = "ALL" | "ACTIVE" | "LIFTED";

/** Renders `Object.entries(data)` as `key: value` rows when every value is a
 *  primitive; a nested object or array can't be flattened honestly, so it
 *  falls back to a pretty-printed block instead of a lossy one-line-per-key
 *  rendering of something that isn't actually flat -- the same rule
 *  `AuditLedgerScreen`'s own event-detail pane applies to `details`. */
function KeyValueBlock({ label, data }: { label: string; data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  const isFlat = entries.every(
    ([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v),
  );
  return (
    <div className="nka__kv">
      <div className="nka__kvlabel">{label}</div>
      {entries.length === 0 ? (
        <p className="nka__kvempty">None recorded.</p>
      ) : isFlat ? (
        <dl className="nka__kvlist">
          {entries.map(([k, v]) => (
            <div className="nka__kvrow" key={k}>
              <dt>{k}</dt>
              <dd>{v === null ? "—" : String(v)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <pre className="nka__kvpre">{JSON.stringify(data, null, 2)}</pre>
      )}
    </div>
  );
}

function AssertionRow({
  assertion,
  onLift,
  lifting,
}: {
  assertion: NegativeAssertionRead;
  onLift: (assertion: NegativeAssertionRead) => void;
  lifting: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const suppressionTone: Tone = assertion.suppression_active ? "warn" : "ok";

  return (
    <article className="nka" aria-label={assertion.subject_id}>
      <header className="nka__head">
        <div className="nka__badges">
          <Pill tone="info">{assertion.assertion_type}</Pill>
          <Pill tone={suppressionTone}>
            {assertion.suppression_active ? "suppression active" : "suppression lifted"}
          </Pill>
        </div>
        <button className="nka__toggle" onClick={() => setExpanded((e) => !e)}>
          {expanded ? "Hide details" : "Show details"}
        </button>
      </header>

      <div className="nka__subject">{assertion.subject_id}</div>
      <div className="nka__meta">
        <span>rejected by {assertion.rejected_by}</span>
        <span aria-hidden="true">·</span>
        <span>{relTime(assertion.rejected_at)}</span>
      </div>

      {expanded ? (
        <div className="nka__body">
          <KeyValueBlock label="Predicate" data={assertion.predicate} />
          <KeyValueBlock label="Evidence" data={assertion.evidence} />
          {assertion.material_change_hash ? (
            <div className="nka__hash">
              material change hash <code>{assertion.material_change_hash}</code>
            </div>
          ) : null}
          {!assertion.suppression_active ? (
            <div className="nka__lifted">
              Lifted {relTime(assertion.suppression_lifted_at)}
              {assertion.suppression_lifted_by ? ` by ${assertion.suppression_lifted_by}` : ""}
              {assertion.lift_reason ? ` — ${assertion.lift_reason}` : ""}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="nka__act">
        {assertion.suppression_active ? (
          <Button disabled={lifting} onClick={() => onLift(assertion)}>
            Lift suppression
          </Button>
        ) : null}
      </div>
    </article>
  );
}

export function NegativeKnowledgeScreen() {
  const [params, setParams] = useUrlState();

  const assertionType = params.get("assertion_type") ?? "";
  const suppressionFilter = (params.get("suppression") as SuppressionFilter | null) ?? "ALL";
  const subjectId = params.get("subject");

  const [draftAssertionType, setDraftAssertionType] = useState(assertionType);
  const [draftSubject, setDraftSubject] = useState(subjectId ?? "");

  const [items, setItems] = useState<NegativeAssertionRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [liftingId, setLiftingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // One in-flight request at a time -- aborting the previous one is what
  // stops a slow first page from overwriting the results of a newer, narrower
  // filter (the same reason `AuditLedgerScreen.loadFirstPage` does it).
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
      const page = subjectId
        ? await fetchNegativeKnowledgeForSubject(subjectId, { limit: 50, offset: 0 }, ac.signal)
        : await searchNegativeKnowledge(
            {
              assertionType: assertionType || undefined,
              suppressionActive:
                suppressionFilter === "ALL" ? undefined : suppressionFilter === "ACTIVE",
              limit: 50,
              offset: 0,
            },
            ac.signal,
          );
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
  }, [subjectId, assertionType, suppressionFilter]);

  useEffect(() => {
    void loadFirstPage();
    return () => inflight.current?.abort();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (loadingMore || loading || items.length >= (total ?? 0)) return;
    setLoadingMore(true);
    try {
      const page = subjectId
        ? await fetchNegativeKnowledgeForSubject(subjectId, { limit: 50, offset: items.length })
        : await searchNegativeKnowledge({
            assertionType: assertionType || undefined,
            suppressionActive:
              suppressionFilter === "ALL" ? undefined : suppressionFilter === "ACTIVE",
            limit: 50,
            offset: items.length,
          });
      setItems((prev) => [...prev, ...page.items]);
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, loading, items.length, total, subjectId, assertionType, suppressionFilter]);

  // Debounce the free-text assertion-type filter -- one keystroke shouldn't be
  // one request, the same reason `AuditLedgerScreen` debounces its own
  // free-text filters.
  useEffect(() => {
    const t = setTimeout(() => {
      if (draftAssertionType !== assertionType) {
        setParams({ assertion_type: draftAssertionType || null, subject: null });
      }
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftAssertionType]);

  const runSubjectLookup = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      const trimmed = draftSubject.trim();
      if (!trimmed) return;
      setDraftAssertionType("");
      setParams({ subject: trimmed, assertion_type: null, suppression: null });
    },
    [draftSubject, setParams],
  );

  const clearSubjectLookup = useCallback(() => {
    setDraftSubject("");
    setParams({ subject: null });
  }, [setParams]);

  /* Lifting a suppression re-admits a proposal a human already rejected, so
     the endpoint requires a written reason (`LiftSuppressionRequest.reason`,
     minimum 3 characters). `window.prompt` could not state that rule, could
     not show the 422 when it was broken, and returned `null` both when the
     steward cancelled and when the browser refused to show it at all
     (review 2026-09-05, F21). */
  const [liftTarget, setLiftTarget] = useState<NegativeAssertionRead | null>(null);
  const [liftError, setLiftError] = useState<string | null>(null);

  const confirmLift = useCallback(
    async (reason: string) => {
      const assertion = liftTarget;
      // Checked client-side as well so a too-short reason does not round-trip
      // to a 422; the dialog already blocks an empty one.
      if (!assertion || reason.trim().length < 3) {
        setLiftError("Give at least three characters of reason; it is recorded on the assertion.");
        return;
      }
      setNotice(null);
      setLiftError(null);
      setLiftingId(assertion.id);
      try {
        const updated = await liftNegativeAssertionSuppression(assertion.id, { reason: reason.trim() });
        setItems((prev) => prev.map((i) => (i.id === updated.id ? updated : i)));
        setNotice(`Suppression lifted on ${updated.subject_id}.`);
        setLiftTarget(null);
      } catch (e) {
        setLiftError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setLiftingId(null);
      }
    },
    [liftTarget],
  );

  const hasFilters = assertionType !== "" || suppressionFilter !== "ALL";

  return (
    <div className="nk">
      <header className="nk__head">
        <div>
          <h1 className="nk__h1">Negative knowledge</h1>
          <p className="nk__lede">
            Assertions a human has already rejected — a suppression stops the platform from
            re-proposing the same thing until something material changes, or a steward
            manually lifts it (Phase E · EE.3).
          </p>
        </div>
        <div className="nk__stats">
          <span>
            <b className="tnum">{total !== null ? nf.format(total) : "—"}</b> assertions
          </span>
        </div>
      </header>

      {notice ? (
        <div className="nk__notice" role="status">
          {notice}
          <button
            className="nk__noticex"
            onClick={() => setNotice(null)}
            aria-label="Dismiss notice"
          >
            ×
          </button>
        </div>
      ) : null}

      <div className="nk__filters">
        <Field label="Assertion type">
          <input
            type="text"
            value={draftAssertionType}
            placeholder="e.g. TABLE_NOT_ENTITY"
            disabled={!!subjectId}
            onChange={(e) => setDraftAssertionType(e.target.value)}
          />
        </Field>
        <Field label="Suppression">
          <select
            value={suppressionFilter}
            disabled={!!subjectId}
            onChange={(e) =>
              setParams({
                suppression: e.target.value === "ALL" ? null : e.target.value,
                subject: null,
              })
            }
          >
            <option value="ALL">All</option>
            <option value="ACTIVE">Active</option>
            <option value="LIFTED">Lifted</option>
          </select>
        </Field>
        {hasFilters ? (
          <Button
            onClick={() => {
              setDraftAssertionType("");
              setParams({ assertion_type: null, suppression: null });
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </div>

      <form className="nk__subject" onSubmit={runSubjectLookup}>
        <Field label="Look up by subject ID">
          <input
            type="text"
            value={draftSubject}
            placeholder="e.g. t_customer_master"
            onChange={(e) => setDraftSubject(e.target.value)}
          />
        </Field>
        <Button type="submit">Look up</Button>
        {subjectId ? <Button onClick={clearSubjectLookup}>Back to search</Button> : null}
      </form>

      <div className="nk__main">
        {error ? (
          <ErrorState
            title="Negative knowledge could not be loaded"
            detail={error}
            onRetry={() => void loadFirstPage()}
          />
        ) : loading ? (
          <div className="nk__skeleton" role="status" aria-live="polite">
            Loading negative knowledge…
          </div>
        ) : items.length === 0 ? (
          <Empty
            title={
              subjectId ? `No assertions recorded for ${subjectId}` : "No assertions match this filter"
            }
            hint={subjectId ? "Try a different subject id." : "Try clearing a filter."}
          />
        ) : (
          <div className="nk__list" role="list" aria-label="Negative assertions">
            {items.map((a) => (
              <AssertionRow
                key={a.id}
                assertion={a}
                onLift={(assertion) => {
                  setLiftError(null);
                  setLiftTarget(assertion);
                }}
                lifting={liftingId === a.id}
              />
            ))}
            {total !== null && items.length < total ? (
              <div className="nk__more">
                <Button disabled={loadingMore} onClick={() => void loadMore()}>
                  {loadingMore ? "Loading…" : "Load more"}
                </Button>
              </div>
            ) : null}
          </div>
        )}
      </div>

      {liftTarget ? (
        <ConfirmDialog
          title="Lift this suppression?"
          description={`${liftTarget.subject_id} becomes eligible for the platform to propose again.`}
          confirmLabel="Lift suppression"
          requireReason
          reasonLabel="Reason (recorded on the assertion)"
          busy={liftingId === liftTarget.id}
          error={liftError}
          onConfirm={(reason) => void confirmLift(reason)}
          onCancel={() => setLiftTarget(null)}
        />
      ) : null}
    </div>
  );
}
