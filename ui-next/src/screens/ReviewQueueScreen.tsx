import { useCallback, useEffect, useMemo, useRef, useState } from "react";
/* Filters and selection live in the URL so a filtered view is shareable and
   survives Back/Forward. This screen carried a verbatim copy of the old hook
   -- a `useState` seeded once from `location.search`, subscribed to nothing --
   so its idea of the selection and the address bar drifted apart the first
   time either the Back button or a same-screen link was used (review
   2026-09-05, F09 - R07). The shared hook reads one location store. */
import { useUrlState } from "../lib/useUrlState";
import type { ReviewQueueProposalRead } from "../lib/types";
import { ApiError, decideGovernanceReview, fetchReviewQueue } from "../lib/api";
import { VirtualList } from "../components/VirtualList";
import { PropagationLog } from "../components/PropagationLog";
import {
  Button,
  ConfirmDialog,
  CopyLinkButton,
  Empty,
  ErrorState,
  Field,
  Pill,
  useToast,
} from "../components/primitives";
/* The .prop/.conf/.dl classes `ProposalRow` below renders. The file kept
 * its name from a fixture-era `ProposalCard` component that no longer exists
 * (D01): the component was dead, these styles were not. */
import "../components/ProposalRow.css";
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


/** P1-03: per-object-type row-detail renderers. The queue already ships
 *  every object_type through the same generic ProposalRow; this map only
 *  adds a human-readable header + summary for the two glossary object
 *  types the audit called out. Every other type falls through to the
 *  existing default renderer, unchanged.
 *
 *  Data source: the composed `ReviewQueueProposalRead` already carries
 *  `evidence: EvidenceItemRead[]` (see `_dict_evidence_items` in
 *  `review_queue_read_model.py`), which for a `GLOSSARY_LINK_PROPOSAL`
 *  contains one entry per key of the underlying proposal's evidence dict
 *  -- `term_display_name`, `table_name`, and any similarity/annotator
 *  claims. No extra fetch is required. */
interface RowExtras {
  /** Human-readable subject line for the row's title (falls back to
   *  `object_id` when nothing better is available). */
  subject: string;
  /** Optional one-liner rendered under the badges, above the diff. */
  subtitle?: string;
}

function extractEvidenceValue(
  proposal: ReviewQueueProposalRead,
  keyPrefix: string,
): string | null {
  for (const e of proposal.evidence ?? []) {
    // `_dict_evidence_items` claims are formatted as `"<key>: <value>"`;
    // match on prefix so `term_display_name: MRR` reveals "MRR".
    if (e.claim.startsWith(keyPrefix + ":")) {
      return e.claim.slice(keyPrefix.length + 1).trim();
    }
  }
  return null;
}

function renderRowExtras(proposal: ReviewQueueProposalRead): RowExtras {
  if (proposal.object_type === "GLOSSARY_LINK_PROPOSAL") {
    const term =
      extractEvidenceValue(proposal, "term_display_name") ??
      extractEvidenceValue(proposal, "term_key") ??
      proposal.object_id;
    const table =
      extractEvidenceValue(proposal, "table_name") ??
      extractEvidenceValue(proposal, "qualified_name") ??
      null;
    const summary = extractEvidenceValue(proposal, "summary");
    const parts: string[] = [];
    if (table) parts.push(`for ${table}`);
    if (summary) parts.push(summary);
    if (proposal.confidence !== null && proposal.confidence !== undefined) {
      parts.push(`confidence ${pct(proposal.confidence)}`);
    }
    return { subject: `Link "${term}"`, subtitle: parts.join(" — ") || undefined };
  }
  if (proposal.object_type === "GLOSSARY_TERM_VERSION") {
    const term =
      extractEvidenceValue(proposal, "display_name") ??
      extractEvidenceValue(proposal, "term_display_name") ??
      extractEvidenceValue(proposal, "term_key") ??
      proposal.object_id;
    const reason = extractEvidenceValue(proposal, "reason");
    // The definition diff is already rendered by <DiffEntries/> below (the
    // `definition` field appears in `diff.entries` as a modified field);
    // no need to duplicate it. Only surface a one-liner reason when the
    // proposal composed one.
    return {
      subject: `Term "${term}"`,
      subtitle: reason ? `Reason: ${reason}` : undefined,
    };
  }
  return { subject: proposal.object_id };
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
  const extras = renderRowExtras(proposal);
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
            {extras.subject}
          </button>
          {extras.subtitle ? (
            <div className="prop__extra">{extras.subtitle}</div>
          ) : null}
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
  const toast = useToast();
  const [deciding, setDeciding] = useState<string | null>(null);
  /* Rejection needs a written rationale -- the endpoint refuses REJECT without
     one. That was collected with `window.prompt`, which cannot be labelled or
     validated, is blocked outright by some browsers (returning `null`, which
     this screen read as "cancelled", so the reviewer's decision vanished), and
     leaves no accessible name on anything. `ConfirmDialog` is the same
     transaction with a real focus-trapped dialog, a required field, and the
     server's own error shown in place (review 2026-09-05, F21). */
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [decideError, setDecideError] = useState<string | null>(null);
  const rejectingRef = useRef<string | null>(null);
  rejectingRef.current = rejecting;

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
    async (reviewId: string, decision: "APPROVE" | "REJECT", reason: string | null) => {
      setDeciding(reviewId);
      setDecideError(null);
      try {
        await decideGovernanceReview(reviewId, { decision, reason });
        setRejecting(null);
        // The list refetches underneath, which on its own looks like nothing
        // happened. Say what was recorded.
        toast.show(
          decision === "APPROVE" ? "Approval recorded." : "Rejection recorded with your rationale.",
        );
        await load();
      } catch (e) {
        const message = e instanceof ApiError ? e.detail : (e as Error).message;
        // A failed decision belongs next to the decision, not in the screen's
        // load-error slot: the reviewer still has the dialog open and needs to
        // know whether to retry or to reload because someone else decided
        // first (409). Replacing the whole list with an error banner would
        // discard the rationale they just typed.
        if (rejectingRef.current) setDecideError(message);
        else setError(message);
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
      {toast.node}
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
                onDecide={(decision) =>
                  decision === "REJECT"
                    ? setRejecting(p.review_id)
                    : void decide(p.review_id, "APPROVE", null)
                }
              />
            )}
          />
        )}
      </div>

      {focused ? (
        <aside className="evp rq__evidence" aria-label="Proposal detail">
          <header className="evp__head">
            <div className="evp__title">
              <div className="evp__name">{renderRowExtras(focused).subject}</div>
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
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/governance`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
            <CopyLinkButton
              target={{ screen: "governance", params: { review: focused.review_id } }}
              label="Copy permalink"
            />
          </footer>
        </aside>
      ) : null}

      {rejecting ? (
        <ConfirmDialog
          title="Reject this proposal"
          description="The rationale is recorded on the review and is visible to whoever raised it."
          reasonLabel="Why is this being rejected?"
          requireReason
          destructive
          confirmLabel="Reject proposal"
          busy={deciding === rejecting}
          error={decideError}
          onCancel={() => {
            setRejecting(null);
            setDecideError(null);
          }}
          onConfirm={(reason) => void decide(rejecting, "REJECT", reason)}
        />
      ) : null}

      {PROPAGATION_LOG_ENABLED ? (
        <section className="rq__sec">
          <h2 className="rq__h2">How a quality incident propagates (worked example)</h2>
          {/* D02: kept, and labelled. The gate below being on is not evidence
              that a traversal happened -- so the section says whose data this
              is not, in the heading, in the pill and in the accessible name. */}
          <PropagationLog
            title="Quality propagation · ADR-0016 fails closed"
            illustrative
            illustrativeNote="A hard-coded four-step story about a sample table. No lineage walk produced it: `quality_coupling.check_tool_gate` gates only on a tool's own declared dependencies, and no classification-propagation mechanism exists yet (AT-11). It is here to show the shape of the explanation a real traversal will render."
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
