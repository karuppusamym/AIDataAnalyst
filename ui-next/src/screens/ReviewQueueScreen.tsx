import { useCallback, useEffect, useState } from "react";
import { ProposalCard } from "../components/ProposalCard";
import type { Proposal } from "../components/ProposalCard";
import { PropagationLog } from "../components/PropagationLog";
import { Empty, Pill } from "../components/primitives";
import { fetchReviewBatch } from "../lib/fixtures";
import type { ReviewBatch } from "../lib/fixtures";
import "./ReviewQueueScreen.css";

/* ---------------------------------------------------------------------------
   Review queue.

   The old portal renders this as a table of pending items, which asks the
   reviewer to reconstruct what happened from rows. The unit of work here is
   not a row — it is an agent run that produced some changes safe enough to
   apply and some that need a person. So the screen opens by SAYING THAT, then
   shows only what needs judgment.

   The count of auto-applied changes is deliberately as prominent as the count
   needing review. A steward who cannot see what was applied without them has
   no way to audit the threshold that let it through.
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
 * that day.
 */
const PROPAGATION_LOG_ENABLED = import.meta.env.VITE_ENABLE_PROPAGATION_LOG === "1";

export function ReviewQueueScreen() {
  const [batch, setBatch] = useState<ReviewBatch | null>(null);
  const [decisions, setDecisions] = useState<Record<string, Proposal["state"]>>({});
  const [showApplied, setShowApplied] = useState(false);

  useEffect(() => {
    let live = true;
    void fetchReviewBatch().then((b) => {
      if (live) setBatch(b);
    });
    return () => {
      live = false;
    };
  }, []);

  const decide = useCallback((id: string, state: Proposal["state"]) => {
    setDecisions((d) => ({ ...d, [id]: state }));
  }, []);

  if (!batch) {
    return (
      <div className="rq">
        <div className="rq__load" role="status">
          Loading review queue…
        </div>
      </div>
    );
  }

  const withDecisions = batch.proposals.map((p) => ({
    ...p,
    state: decisions[p.id] ?? p.state,
  }));
  const open = withDecisions.filter((p) => p.state === "needs_review");
  const applied = withDecisions.filter((p) => p.state === "auto_applied");
  const closed = withDecisions.filter(
    (p) => p.state === "approved" || p.state === "rejected",
  );

  return (
    <div className="rq">
      <header className="rq__head">
        <div>
          <h1 className="rq__h1">Review queue</h1>
          <p className="rq__lede">
            <b>{batch.runLabel}</b> finished {batch.finishedAgo}. Everything below this
            tenant&rsquo;s {Math.round(batch.threshold * 100)}% threshold is waiting for
            a person.
          </p>
        </div>
      </header>

      <div className="rq__tiles">
        <div className="tile tile--ok">
          <div className="tile__n tnum">{batch.passed}</div>
          <div className="tile__l">checks passed</div>
        </div>
        <div className="tile tile--info">
          <div className="tile__n tnum">{applied.length}</div>
          <div className="tile__l">applied automatically</div>
          <button className="tile__a" onClick={() => setShowApplied((v) => !v)}>
            {showApplied ? "Hide" : "Show me"}
          </button>
        </div>
        <div className="tile tile--warn">
          <div className="tile__n tnum">{open.length}</div>
          <div className="tile__l">need your judgment</div>
        </div>
      </div>

      {showApplied && applied.length > 0 ? (
        <section className="rq__sec">
          <h2 className="rq__h2">
            Applied without you <Pill tone="ok">above threshold</Pill>
          </h2>
          <div className="rq__list">
            {applied.map((p) => (
              <ProposalCard key={p.id} proposal={p} onReject={(id) => decide(id, "rejected")} />
            ))}
          </div>
        </section>
      ) : null}

      <section className="rq__sec">
        <h2 className="rq__h2">Waiting on you</h2>
        {open.length === 0 ? (
          <Empty
            title="Nothing left in this run"
            hint="New proposals appear here when the next validation run finishes."
          />
        ) : (
          <div className="rq__list">
            {open.map((p) => (
              <ProposalCard
                key={p.id}
                proposal={p}
                onApprove={(id) => decide(id, "approved")}
                onReject={(id) => decide(id, "rejected")}
              />
            ))}
          </div>
        )}
      </section>

      {closed.length > 0 ? (
        <section className="rq__sec">
          <h2 className="rq__h2">Decided in this session</h2>
          <div className="rq__list">
            {closed.map((p) => (
              <ProposalCard key={p.id} proposal={p} />
            ))}
          </div>
        </section>
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
