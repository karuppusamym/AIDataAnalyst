import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  RelationshipCandidateCalibrationRead,
  RelationshipCandidateReviewItemRead,
  RelationshipCandidateReviewQueueRead,
} from "../lib/types";
import {
  ApiError,
  bulkDecideRelationshipCandidates,
  decideRelationshipCandidate,
  fetchRelationshipCandidateCalibration,
  fetchRelationshipCandidateReviewQueue,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import "../components/EvidencePane.css";
import "./RelationshipsScreen.css";

/* ---------------------------------------------------------------------------
   Relationships — UX-15/UX-16.

   N4's review queue (`GET .../relationship-candidates/review-queue`,
   `relationship_candidate_review.compose_relationship_candidate_review_queue`)
   is the primary read model: PENDING `RelationshipCandidate`s for one
   datasource, ordered by real computed lineage impact (EA.14's bounded
   traversal, descending) rather than confidence or insertion order — this
   screen renders that order as-is and never re-sorts client-side. Each item
   carries an SM-7 "nothing → this edge" diff (every field reports `added`,
   since a candidate has no published predecessor) and, on its
   `confidence_signals` entry, AT-15's named per-signal confidence
   breakdown. Deciding — singly (`POST .../decision`) or in bulk over a
   checked set (`POST .../bulk-decision`, RL-6) — follows the same
   maker-checker shape `ReviewQueueScreen` already uses for governance
   reviews; the datasource picker, URL-held state, abortable single-flight
   fetch and virtualized list follow `CatalogScreen`'s skeleton.

   RL-7's confidence-calibration endpoint is optional secondary info here: a
   small tile showing this org's own observed approval rate, fetched
   independently and never allowed to block or error out the main queue.
--------------------------------------------------------------------------- */

import { useOrgId } from "../lib/org";
const pct = (n: number) => `${Math.round(n * 100)}%`;

interface ConfidenceSignal {
  name: string;
  score: number;
  maximum: number;
  reason: string;
}

function asConfidenceSignals(value: unknown): ConfidenceSignal[] {
  if (!Array.isArray(value)) return [];
  return value.filter((s): s is ConfidenceSignal => {
    if (!s || typeof s !== "object") return false;
    const r = s as Record<string, unknown>;
    return typeof r.name === "string" && typeof r.score === "number" && typeof r.maximum === "number";
  });
}

function diffField(item: RelationshipCandidateReviewItemRead, field: string): unknown {
  return item.diff.find((e) => e.field === field)?.after;
}

function diffString(item: RelationshipCandidateReviewItemRead, field: string): string {
  const v = diffField(item, field);
  return typeof v === "string" ? v : "?";
}

function confidenceTone(c: number): "ok" | "warn" | "bad" {
  return c >= 0.85 ? "ok" : c >= 0.65 ? "warn" : "bad";
}

function edgeLabel(item: RelationshipCandidateReviewItemRead): string {
  return `${diffString(item, "source_table")}.${diffString(item, "source_column")} → ${diffString(
    item,
    "target_table",
  )}.${diffString(item, "target_column")}`;
}

function CandidateCard({
  item,
  checked,
  onToggleCheck,
  focused,
  onFocus,
  deciding,
  onDecide,
}: {
  item: RelationshipCandidateReviewItemRead;
  checked: boolean;
  onToggleCheck: () => void;
  focused: boolean;
  onFocus: () => void;
  deciding: boolean;
  onDecide: (decision: "APPROVE" | "REJECT") => void;
}) {
  const { candidate, impact } = item;
  const label = edgeLabel(item);
  const signals = asConfidenceSignals(diffField(item, "confidence_signals"));

  return (
    <article
      className={`rcard${focused ? " rcard--focused" : ""}`}
      aria-label={label}
    >
      <header className="rcard__head">
        <input
          type="checkbox"
          className="rcard__check"
          checked={checked}
          onChange={onToggleCheck}
          aria-label={`Select ${label}`}
        />
        <div className="rcard__lead">
          <div className="rcard__badges">
            <Pill tone="warn">pending</Pill>
            <Pill tone="info">{candidate.detection_rule.toLowerCase().replace(/_/g, " ")}</Pill>
            <Pill tone="mute">
              impact {impact.impact_score}
              {impact.truncated ? "+" : ""}
            </Pill>
          </div>
          <button className="rcard__title" onClick={onFocus}>
            {label}
          </button>
        </div>
        <span className="conf" title={`Confidence ${pct(candidate.confidence)}`}>
          <span className="conf__bar">
            <span
              className={`conf__fill conf__fill--${confidenceTone(candidate.confidence)}`}
              style={{ width: pct(candidate.confidence) }}
            />
          </span>
          <span className="conf__n tnum">{pct(candidate.confidence)}</span>
        </span>
      </header>

      {signals.length > 0 ? (
        <div className="rcard__signals" aria-label="Confidence signal breakdown">
          {signals.map((s) => (
            <div key={s.name} className="sig">
              <span className="sig__n">{s.name.replace(/_/g, " ")}</span>
              <span className="sig__bar">
                <span
                  className="sig__fill"
                  style={{ width: s.maximum > 0 ? `${(s.score / s.maximum) * 100}%` : "0%" }}
                />
              </span>
              <span className="sig__v tnum">
                {s.score.toFixed(2)}/{s.maximum.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="rcard__act">
        <Button variant="primary" disabled={deciding} onClick={() => onDecide("APPROVE")}>
          Approve
        </Button>
        <Button disabled={deciding} onClick={() => onDecide("REJECT")}>
          Reject
        </Button>
      </div>
    </article>
  );
}

export function RelationshipsScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const ds = params.get("ds");
  const focusedId = params.get("candidate");

  const { datasources, error: datasourcesError } = useDatasourcePicker(ORG);

  const [data, setData] = useState<RelationshipCandidateReviewQueueRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [bulkDeciding, setBulkDeciding] = useState(false);
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  const [calibration, setCalibration] = useState<RelationshipCandidateCalibrationRead | null>(null);

  // One in-flight review-queue request at a time — same reason CatalogScreen
  // aborts the previous one: a slow first fetch must not overwrite the
  // result of a newer datasource selection.
  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const load = useCallback(async () => {
    inflight.current?.abort();
    if (!ds) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;
    setLoading(true);
    setError(null);
    try {
      const queue = await fetchRelationshipCandidateReviewQueue(ds, { limit: 200 }, ac.signal);
      if (seq !== reqSeq.current) return;
      setData(queue);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [ds]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  useEffect(() => {
    setChecked(new Set());
  }, [ds]);

  // RL-7: optional secondary info, fetched independently — a failure here
  // (or simply no decision history yet) never blocks or errors the queue.
  useEffect(() => {
    const ac = new AbortController();
    fetchRelationshipCandidateCalibration(ds, ac.signal)
      .then((c) => setCalibration(c))
      .catch(() => setCalibration(null));
    return () => ac.abort();
  }, [ds]);

  const items = data?.items ?? [];

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
      const all = items.length > 0 && items.every((i) => prev.has(i.candidate.id));
      if (all) return new Set();
      return new Set(items.map((i) => i.candidate.id));
    });
  }, [items]);

  const decide = useCallback(
    async (candidateId: string, decision: "APPROVE" | "REJECT") => {
      let reason: string | null = null;
      if (decision === "REJECT") {
        reason = window.prompt("A reason is required to reject this relationship:");
        if (!reason) return; // the endpoint itself requires a non-empty reason on REJECT
      }
      setDeciding(candidateId);
      try {
        await decideRelationshipCandidate(candidateId, { decision, reason });
        await load();
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setDeciding(null);
      }
    },
    [load],
  );

  const bulkDecide = useCallback(
    async (decision: "APPROVE" | "REJECT") => {
      const ids = [...checked];
      if (ids.length === 0) return;
      let reason: string | null = null;
      if (decision === "REJECT") {
        reason = window.prompt(`A reason is required to reject these ${ids.length} relationships:`);
        if (!reason) return;
      }
      setBulkDeciding(true);
      try {
        await bulkDecideRelationshipCandidates({ candidate_ids: ids, decision, reason });
        setChecked(new Set());
        await load();
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setBulkDeciding(false);
      }
    },
    [checked, load],
  );

  const focused = useMemo(
    () => items.find((i) => i.candidate.id === focusedId) ?? null,
    [items, focusedId],
  );

  const selectedDatasourceName = datasourceName(datasources, ds);
  const totalPending = data?.total_pending_count ?? 0;
  const approvalRate =
    calibration && calibration.total_decided > 0
      ? calibration.buckets.reduce((sum, b) => sum + b.approved_count, 0) / calibration.total_decided
      : null;

  return (
    <div className="rel">
      <header className="rel__head">
        <div>
          <h1 className="rel__h1">Relationships</h1>
          <p className="rel__lede">
            Candidate table relationships the platform detected, ordered by how much of the
            approved lineage graph each decision touches — approve to add the edge, reject to
            record it as known-not-true.
          </p>
        </div>
      </header>

      <div className="rel__filters">
        <Field label="Datasource">
          <select
            value={ds ?? ""}
            onChange={(e) => setParams({ ds: e.target.value || null, candidate: null })}
          >
            <option value="">Select a datasource…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <div className="rel__spacer" />
        {checked.size > 0 ? (
          <div className="rel__bulk" role="status">
            <Pill tone="accent">{checked.size} selected</Pill>
            <Button variant="primary" disabled={bulkDeciding} onClick={() => void bulkDecide("APPROVE")}>
              Approve selected
            </Button>
            <Button disabled={bulkDeciding} onClick={() => void bulkDecide("REJECT")}>
              Reject selected
            </Button>
            <Button onClick={() => setChecked(new Set())}>Clear</Button>
          </div>
        ) : null}
      </div>

      {ds && data ? (
        <div className="rel__tiles">
          <div className="tile tile--warn">
            <div className="tile__n tnum">{totalPending}</div>
            <div className="tile__l">pending{selectedDatasourceName ? ` in ${selectedDatasourceName}` : ""}</div>
          </div>
          <div className="tile">
            <div className="tile__n tnum">{data.scanned_count}</div>
            <div className="tile__l">scanned for impact{data.truncated ? " (truncated)" : ""}</div>
          </div>
          {approvalRate !== null ? (
            <div className="tile tile--info">
              <div className="tile__n tnum">{pct(approvalRate)}</div>
              <div className="tile__l">
                historically approved · {calibration?.total_decided} decided (RL-7)
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="rel__main">
        {!ds ? (
          <Empty
            title="Pick a datasource"
            hint="Relationship candidates are reviewed one datasource at a time."
          />
        ) : error ? (
          <ErrorState
            title="The relationship review queue could not be loaded"
            detail={error}
            onRetry={() => void load()}
          />
        ) : loading ? (
          <div className="rel__load" role="status" aria-live="polite">
            Loading relationship candidates…
          </div>
        ) : items.length === 0 ? (
          <Empty
            title="Nothing pending"
            hint="Every detected relationship for this datasource has already been reviewed."
          />
        ) : (
          <>
            <label className="rel__all">
              <input
                type="checkbox"
                checked={items.length > 0 && items.every((i) => checked.has(i.candidate.id))}
                onChange={toggleAllVisible}
                aria-label="Select all loaded candidates"
              />
              Select all loaded ({items.length})
            </label>
            <VirtualList
              items={items}
              getKey={(i) => i.candidate.id}
              ariaLabel="Relationship candidate review queue"
              estimateSize={220}
              renderItem={(item) => (
                <CandidateCard
                  item={item}
                  checked={checked.has(item.candidate.id)}
                  onToggleCheck={() => toggleCheck(item.candidate.id)}
                  focused={item.candidate.id === focusedId}
                  onFocus={() => setParams({ candidate: item.candidate.id })}
                  deciding={deciding === item.candidate.id}
                  onDecide={(decision) => void decide(item.candidate.id, decision)}
                />
              )}
            />
          </>
        )}
      </div>

      {focused ? (
        <aside className="evp rel__evidence" aria-label="Candidate detail">
          <header className="evp__head">
            <div className="evp__title">
              <div className="evp__name">{edgeLabel(focused)}</div>
              <div className="evp__path">
                {focused.candidate.detection_rule} · impact {focused.impact.impact_score}
              </div>
            </div>
            <button className="evp__x" onClick={() => setParams({ candidate: null })} aria-label="Close">
              ×
            </button>
          </header>
          <div className="evp__body">
            <div className="rel__diff" role="group" aria-label="Proposed edge">
              {focused.diff
                .filter((e) => e.field !== "confidence_signals")
                .map((e, i) => (
                  <div key={`${e.field}-${i}`} className="dl">
                    <span className="dl__g" aria-hidden="true">+</span>
                    <span className="dl__t">
                      <b>{e.field}</b> = {typeof e.after === "string" ? e.after : JSON.stringify(e.after)}
                    </span>
                  </div>
                ))}
            </div>
            <ol className="evl">
              {asConfidenceSignals(diffField(focused, "confidence_signals")).map((s) => (
                <li key={s.name} className="evi evi--info">
                  <div className="evi__label">{s.name.replace(/_/g, " ")}</div>
                  <div className="evi__value">{s.reason}</div>
                  <div className="evi__source">
                    {s.score.toFixed(2)} / {s.maximum.toFixed(2)}
                  </div>
                </li>
              ))}
            </ol>
          </div>
          <footer className="evp__foot">
            <Button
              onClick={() => {
                const permalink = `${location.origin}${location.pathname}?ds=${ds}&candidate=${focused.candidate.id}`;
                void navigator.clipboard?.writeText(permalink);
              }}
            >
              Copy permalink
            </Button>
          </footer>
        </aside>
      ) : null}

      {datasourcesError ? <p className="rel__dserr" role="alert">{datasourcesError}</p> : null}
    </div>
  );
}
