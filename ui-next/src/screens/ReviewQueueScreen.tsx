import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReviewQueueProposalRead } from "../lib/types";
import { ApiError, decideGovernanceReview, fetchReviewQueue } from "../lib/api";
import { VirtualList } from "../components/VirtualList";
import { PropagationLog } from "../components/PropagationLog";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import "../components/ProposalCard.css";
import "../components/EvidencePane.css";
import "./ReviewQueueScreen.css";

/* ---------------------------------------------------------------------------
   Review queue — UX-15, migrated onto UX-17's real read model.

   This screen previously stood on `fetchReviewBatch` (`lib/fixtures.ts`), a
   fixture standing in for a read model that had not been built. UX-17
   shipped that read model for real (`GET /v1/governance/reviews/queue`,
   `review_queue_read_model.py`) 2026-09-01, so this migration is a genuine
   rewire, not a relabeling: URL-held filter state, one abortable request per
   view, a virtualized proposal list (Catalog pattern), and decisions that
   call the real `POST /v1/governance/reviews/{id}/decision` maker-checker
   endpoint instead of only mutating local component state.

   One honest redesign this forced: the old header's "applied automatically"
   tile had nothing real behind it. Every governance-review-backed proposal
   type in this codebase routes through maker-checker with no confidence-
   gated auto-apply branch (confirmed by UX-19's own accomplishment-log
   entry: "every agent reports has_auto_apply_branch=False"), so
   `GovernanceReview.status` only ever takes PENDING/APPROVED/REJECTED — there
   is no AUTO_APPLIED to show. Keeping that tile would have meant fabricating
   a state the real endpoint cannot report. The tiles below are the three
   statuses the real model actually has.
--------------------------------------------------------------------------- */

/**
 * AT-D4: the "Why orders_raw is currently blocked" `PropagationLog` below is a
 * hard-coded, four-step lineage-propagation narrative — it is not fed by any
 * fetch, fixture generator, or backend endpoint, and no such endpoint exists.
 * `quality_coupling.check_tool_gate` (`src/aida/quality_coupling.py`, wired
 * into `tool_api.py::execute_tool`) only gates on a tool's own *declared*
 * dependency tables having an open incident directly; there is no lineage
 * walk that makes "orders_raw inherits the incident via column lineage from
 * raw_sales" a real, traversed chain, and no `classification_derived`
 * propagation mechanism exists anywhere in `src/aida` at all (AT-11, which
 * would build one, is still TODO). Rendering this unconditionally would show
 * a steward a mechanism the platform cannot back with evidence — gated
 * behind `VITE_ENABLE_PROPAGATION_LOG`, default OFF, until AT-11 (or an
 * equivalent real, lineage-resolved read model) ships something to show
 * here honestly. `PropagationLog` itself stays in place, unmodified, for
 * that day. (UX-20's narrated lineage screen is the real thing this was
 * gesturing at, once a node is selected — see `NarratedLineageScreen.tsx`.)
 */
const PROPAGATION_LOG_ENABLED = import.meta.env.VITE_ENABLE_PROPAGATION_LOG === "1";

const OBJECT_TYPES = [
  "SEMANTIC_MODEL_VERSION",
  "GLOSSARY_TERM_VERSION",
  "METADATA_ENRICHMENT_PROPOSAL",
  "GLOSSARY_LINK_PROPOSAL",
  "SEMANTIC_METRIC_PROPOSAL",
  "ASSET_DESCRIPTION_DRAFT",
  "TERM_SEMANTIC_BINDING",
  "COLUMN_CLASSIFICATION_PROMOTION",
  "CONTEXT_PRODUCT_VERSION",
] as const;

const pct = (n: number) => `${Math.round(n * 100)}%`;

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

function DiffEntries({ proposal }: { proposal: ReviewQueueProposalRead }) {
  if (!proposal.diff.diffable) {
    return <p className="prop__nodiff">{proposal.diff.message ?? "No structured diff for this object type."}</p>;
  }
  const entries = proposal.diff.entries ?? [];
  if (entries.length === 0) return <p className="prop__nodiff">No field-level changes recorded.</p>;
  return (
    <div className="prop__diff" role="group" aria-label="Proposed change">
      {entries.map((e, i) => (
        <div key={`${e.field}-${i}`} className={`dl dl--${e.change}`}>
          <span className="dl__g" aria-hidden="true">
            {e.change === "added" ? "+" : e.change === "removed" ? "−" : "~"}
          </span>
          <span className="dl__t">
            <b>{e.field}</b>
            {e.change !== "added" ? ` was ${JSON.stringify(e.before)}` : ""}
            {e.change !== "removed" ? ` → ${JSON.stringify(e.after)}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function ProposalRow({
  proposal,
  focused,
  onFocus,
  onDecide,
  deciding,
}: {
  proposal: ReviewQueueProposalRead;
  focused: boolean;
  onFocus: () => void;
  onDecide: (decision: "APPROVE" | "REJECT") => void;
  deciding: boolean;
}) {
  const decided = proposal.status !== "PENDING";
  return (
    <article
      className={`prop prop--${proposal.status.toLowerCase()}${focused ? " prop--focused" : ""}`}
      aria-label={`${proposal.object_type} ${proposal.object_id}`}
    >
      <header className="prop__head">
        <div className="prop__lead">
          <div className="prop__badges">
            {proposal.status === "PENDING" ? <Pill tone="warn">review needed</Pill> : null}
            {proposal.status === "APPROVED" ? <Pill tone="ok">approved</Pill> : null}
            {proposal.status === "REJECTED" ? <Pill tone="mute">rejected</Pill> : null}
            <Pill tone="info">{proposal.requested_action}</Pill>
            <Pill tone="mute">{proposal.object_type.toLowerCase().replace(/_/g, " ")}</Pill>
          </div>
          <button className="prop__title" onClick={onFocus}>
            {proposal.object_id}
          </button>
          <div className="prop__subject">
            {proposal.requested_by.includes("agent") ? "proposed by " : "raised by "}
            {proposal.requested_by}
          </div>
        </div>
        {proposal.confidence !== null && proposal.confidence !== undefined ? (
          <span
            className="conf"
            title={`Proposer confidence ${pct(proposal.confidence)}`}
          >
            <span className="conf__bar">
              <span
                className={`conf__fill conf__fill--${
                  proposal.confidence >= 0.9 ? "ok" : proposal.confidence >= 0.75 ? "warn" : "bad"
                }`}
                style={{ width: pct(proposal.confidence) }}
              />
            </span>
            <span className="conf__n tnum">{pct(proposal.confidence)}</span>
          </span>
        ) : null}
      </header>

      <DiffEntries proposal={proposal} />

      {proposal.evidence && proposal.evidence.length > 0 ? (
        <div className="prop__why">
          <span className="prop__whyk">Evidence</span>
          <div>
            {proposal.evidence.map((e, i) => (
              <p key={i} className="prop__ev">
                <b>{e.category.replace(/_/g, " ")}:</b> {e.claim} <span className="prop__evsrc">· {e.source}</span>
              </p>
            ))}
          </div>
        </div>
      ) : null}

      <div className="prop__act">
        {decided ? (
          <span className="prop__done">
            {proposal.status === "APPROVED" ? "Approved" : "Rejected"}
            {proposal.decided_by ? ` by ${proposal.decided_by}` : ""}
            {proposal.decision_reason ? ` — ${proposal.decision_reason}` : ""}
          </span>
        ) : (
          <>
            <Button variant="primary" disabled={deciding} onClick={() => onDecide("APPROVE")}>
              Approve
            </Button>
            <Button disabled={deciding} onClick={() => onDecide("REJECT")}>
              Reject
            </Button>
          </>
        )}
      </div>
    </article>
  );
}

export function ReviewQueueScreen() {
  const [params, setParams] = useUrlState();
  // "ALL" in the URL is this screen's own spelling for "every status" — the
  // API's spelling is an explicit empty string (see `fetchReviewQueue`'s own
  // comment on why `null` there is distinct from omitting the param), which
  // cannot itself round-trip through a URLSearchParams value.
  const statusParam = params.has("status") ? params.get("status") : "PENDING";
  const statusFilter = statusParam === "ALL" ? null : statusParam;
  const objectTypeFilter = params.get("type");
  const focusedId = params.get("review");

  const [data, setData] = useState<{ proposals: ReviewQueueProposalRead[]; byStatus: Record<string, number> } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);

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
      const page = await fetchReviewQueue(
        { status: statusFilter, objectType: objectTypeFilter, limit: 1000 },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setData({ proposals: page.proposals, byStatus: page.by_status });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [statusFilter, objectTypeFilter]);

  useEffect(() => {
    void load();
    return () => inflight.current?.abort();
  }, [load]);

  const proposals = data?.proposals ?? [];

  const decide = useCallback(
    async (reviewId: string, decision: "APPROVE" | "REJECT") => {
      let reason: string | null = null;
      if (decision === "REJECT") {
        reason = window.prompt("A reason is required to reject this proposal:");
        if (!reason) return; // the endpoint itself requires a non-empty reason on REJECT
      }
      setDeciding(reviewId);
      try {
        await decideGovernanceReview(reviewId, { decision, reason });
        await load();
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setDeciding(null);
      }
    },
    [load],
  );

  const totalPending = data?.byStatus["PENDING"] ?? 0;
  const totalApproved = data?.byStatus["APPROVED"] ?? 0;
  const totalRejected = data?.byStatus["REJECTED"] ?? 0;

  const focused = useMemo(
    () => proposals.find((p) => p.review_id === focusedId) ?? null,
    [proposals, focusedId],
  );

  return (
    <div className="rq">
      <header className="rq__head">
        <div>
          <h1 className="rq__h1">Review queue</h1>
          <p className="rq__lede">
            Governed changes awaiting a decision — maker-checker, ADR-0001: models
            propose, a person or a deterministic service decides.
          </p>
        </div>
      </header>

      <div className="rq__filters">
        <Field label="Status">
          <select
            value={statusParam ?? "ALL"}
            onChange={(e) => setParams({ status: e.target.value, review: null })}
          >
            <option value="PENDING">Pending</option>
            <option value="APPROVED">Approved</option>
            <option value="REJECTED">Rejected</option>
            <option value="ALL">All statuses</option>
          </select>
        </Field>
        <Field label="Object type">
          <select
            value={objectTypeFilter ?? "ALL"}
            onChange={(e) =>
              setParams({ type: e.target.value === "ALL" ? null : e.target.value, review: null })
            }
          >
            <option value="ALL">All</option>
            {OBJECT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.toLowerCase().replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div className="rq__tiles">
        <div className="tile tile--warn">
          <div className="tile__n tnum">{totalPending}</div>
          <div className="tile__l">pending your judgment</div>
        </div>
        <div className="tile tile--ok">
          <div className="tile__n tnum">{totalApproved}</div>
          <div className="tile__l">approved</div>
        </div>
        <div className="tile">
          <div className="tile__n tnum">{totalRejected}</div>
          <div className="tile__l">rejected</div>
        </div>
      </div>

      <div className="rq__main">
        {error ? (
          <ErrorState title="The review queue could not be loaded" detail={error} onRetry={() => void load()} />
        ) : loading ? (
          <div className="rq__load" role="status" aria-live="polite">
            Loading review queue…
          </div>
        ) : proposals.length === 0 ? (
          <Empty
            title="Nothing in this batch"
            hint="Change the status or object-type filter, or check back once the next run finishes."
          />
        ) : (
          <VirtualList
            items={proposals}
            getKey={(p) => p.review_id}
            ariaLabel="Governance review queue"
            estimateSize={190}
            renderItem={(p) => (
              <ProposalRow
                proposal={p}
                focused={p.review_id === focusedId}
                onFocus={() => setParams({ review: p.review_id })}
                deciding={deciding === p.review_id}
                onDecide={(decision) => void decide(p.review_id, decision)}
              />
            )}
          />
        )}
      </div>

      {focused ? (
        <aside className="evp rq__evidence" aria-label="Proposal detail">
          <header className="evp__head">
            <div className="evp__title">
              <div className="evp__name">{focused.object_id}</div>
              <div className="evp__path">{focused.object_type} · {focused.requested_action}</div>
            </div>
            <button className="evp__x" onClick={() => setParams({ review: null })} aria-label="Close">
              ×
            </button>
          </header>
          <div className="evp__body">
            <DiffEntries proposal={focused} />
            <ol className="evl">
              {(focused.evidence ?? []).map((e, i) => (
                <li key={i} className="evi evi--info">
                  <div className="evi__label">{e.category.replace(/_/g, " ")}</div>
                  <div className="evi__value">{e.claim}</div>
                  <div className="evi__source">{e.source}</div>
                </li>
              ))}
            </ol>
          </div>
          <footer className="evp__foot">
            <Button
              onClick={() => {
                const permalink = `${location.origin}${location.pathname}?review=${focused.review_id}`;
                void navigator.clipboard?.writeText(permalink);
              }}
            >
              Copy permalink
            </Button>
          </footer>
        </aside>
      ) : null}

      {PROPAGATION_LOG_ENABLED ? (
        <section className="rq__sec">
          <h2 className="rq__h2">Why orders_raw is currently blocked</h2>
          <PropagationLog
            title="Quality propagation · ADR-0016 fails closed"
            steps={[
              {
                kind: "origin",
                text: "raw_sales failed 12 of 15 quality rules (null counts, type mismatches)",
                mechanism: "data_quality.py · rule set dq_raw_sales@4",
              },
              {
                kind: "hop",
                text: "orders_raw inherits the incident",
                mechanism: "via column lineage — orders_raw.amount derives from raw_sales.amount",
              },
              {
                kind: "hop",
                text: "revenue_agg inherits the incident",
                mechanism: "via column lineage — reads orders_raw.amount",
              },
              {
                kind: "blocked",
                text: "tool_revenue_by_lob refused while the incident is open",
                mechanism: "recorded as a refusal edge · ai_decision_lineage.py (LN-3)",
              },
            ]}
          />
        </section>
      ) : null}
    </div>
  );
}
