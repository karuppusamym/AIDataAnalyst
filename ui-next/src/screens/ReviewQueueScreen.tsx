import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
/* Filters and selection live in the URL so a filtered view is shareable and
   survives Back/Forward. This screen carried a verbatim copy of the old hook
   -- a `useState` seeded once from `location.search`, subscribed to nothing --
   so its idea of the selection and the address bar drifted apart the first
   time either the Back button or a same-screen link was used (review
   2026-09-05, F09 - R07). The shared hook reads one location store. */
import { useUrlState } from "../lib/useUrlState";
import type { ReviewQueueProposalRead } from "../lib/types";
import { ApiError, decideGovernanceReview, fetchReviewQueue } from "../lib/api";
import { useSession } from "../lib/session";
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
/* T18: the detail pane is the shared review shell. This screen keeps its own
   route, its own queue, its own filters and its own per-object-type diff and
   evidence renderers -- what it stopped owning is the *shape* of a review
   detail, and in particular what a reviewer is shown when another decision
   won the race (F05). */
import {
  ReviewDetailShell,
  conflictFromError,
  type ReviewConflict,
} from "../components/ReviewDetail";
/* The .prop/.conf/.dl classes `ProposalRow` below renders. The file kept
 * its name from a fixture-era `ProposalCard` component that no longer exists
 * (D01): the component was dead, these styles were not. */
import "../components/ProposalRow.css";
import "../components/EvidencePane.css";
import "./ReviewQueueScreen.css";
import { ReviewChangePreview } from "../components/ReviewChangePreview";

const FULL_PREVIEW_TYPES = new Set(["MODEL_IMPORT_BATCH", "CONTEXT_PRODUCT_VERSION", "ONTOLOGY_VERSION"]);

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
  "COLUMN_DESCRIPTION_DRAFT",
  "TERM_SEMANTIC_BINDING",
  "COLUMN_CLASSIFICATION_PROMOTION",
  "CONTEXT_PRODUCT_VERSION",
  "MODEL_IMPORT_BATCH",
  "ONTOLOGY_VERSION",
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
  if (
    proposal.object_type === "COLUMN_DESCRIPTION_DRAFT" ||
    proposal.object_type === "ASSET_DESCRIPTION_DRAFT"
  ) {
    // The proposed text arrives as the first evidence item: description
    // drafts have no field diff, so without it the row would ask a reviewer
    // to approve text it never showed them.
    const proposed = extractEvidenceValue(proposal, "proposed_description");
    const column = extractEvidenceValue(proposal, "column");
    const parts: string[] = [];
    // Said first, where a reviewer skimming the queue cannot miss it: a
    // model's text can be wrong in a way that reads as right.
    if (extractEvidenceValue(proposal, "origin")?.startsWith("MODEL_INFERRED")) {
      parts.push("model-inferred: check it against the data");
    }
    if (proposed) parts.push(`“${proposed.length > 180 ? `${proposed.slice(0, 180)}…` : proposed}”`);
    if (proposal.confidence !== null && proposal.confidence !== undefined) {
      parts.push(`evidence ${pct(proposal.confidence)}`);
    }
    return {
      subject:
        proposal.object_type === "COLUMN_DESCRIPTION_DRAFT"
          ? `Describe column ${column ?? proposal.object_id}`
          : "Table description draft",
      subtitle: parts.join(" — ") || undefined,
    };
  }
  return { subject: proposal.object_id };
}

function DiffEntries({ proposal }: { proposal: ReviewQueueProposalRead }) {
  if (FULL_PREVIEW_TYPES.has(proposal.object_type)) {
    return <p>Select this proposal to load its complete version or workbook change preview.</p>;
  }
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
  ownProposal,
  decisionError,
  previewBlocked,
}: {
  proposal: ReviewQueueProposalRead;
  focused: boolean;
  onFocus: () => void;
  onDecide: (decision: "APPROVE" | "REJECT") => void;
  deciding: boolean;
  ownProposal: boolean;
  decisionError?: string;
  previewBlocked?: boolean;
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
        ) : ownProposal ? (
          <span className="prop__own-review">
            You proposed this change. Another reviewer must approve or reject it.
          </span>
        ) : previewBlocked ? (
          <span>Open this proposal and load its full change preview before deciding.</span>
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
        {decisionError ? (
          <span className="prop__decision-error" role="alert">{decisionError}</span>
        ) : null}
      </div>
    </article>
  );
}

/* R11-S10: this is the governance queue itself -- one of the two queues the
   exported `ReviewQueueScreen` below federates. It is unchanged by the merge:
   its own fetch, its own filters, its own per-object-type renderers and its
   own maker-checker decision call. What changed is that it is no longer the
   only queue a reviewer has to find. */
function GovernanceReviewQueue() {
  const [params, setParams] = useUrlState();
  const principalId = useSession().me?.principal_id ?? null;
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
  const [decisionErrors, setDecisionErrors] = useState<Record<string, string>>({});
  const [detailReady, setDetailReady] = useState<string | null>(null);
  /* A 409 is not an error message. The decision service answers a lost claim
     with the review's refreshed state (F05), so the reviewer who lost is shown
     WHICH decision won rather than "409 Conflict" -- which is what the old
     error slot said, because the structured detail was discarded by the
     decoder before it ever reached here. Keyed by review so a conflict
     survives the reload that follows it. */
  const [conflicts, setConflicts] = useState<Record<string, ReviewConflict>>({});
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
      // The detail pane can remain visible during a refresh. Never act on its
      // old snapshot while the current queue is loading or unavailable.
      if (loading || error !== null) return;
      const proposal = proposals.find(item => item.review_id === reviewId);
      if (proposal && FULL_PREVIEW_TYPES.has(proposal.object_type) && (detailReady !== reviewId || focusedId !== reviewId)) return;
      setDeciding(reviewId);
      setDecideError(null);
      setDecisionErrors((current) => {
        if (!(reviewId in current)) return current;
        const next = { ...current };
        delete next[reviewId];
        return next;
      });
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
        // A failed decision belongs next to that decision, not in the screen's
        // load-error slot. A maker-checker refusal (or an already-decided race)
        // must never replace the successfully loaded queue with "could not be
        // loaded". Rejections keep the dialog and rationale; direct approvals
        // keep the row and put the server's refusal beside its controls.
        const conflict = conflictFromError(e);
        if (conflict) {
          /* A refusal about this review's own state is not a message beside a
             button: the reviewer needs to see what the review now IS. Close
             the rationale dialog, open the detail on this review, and re-read
             the queue so the row underneath stops claiming PENDING. */
          setConflicts((current) => ({ ...current, [reviewId]: conflict }));
          setRejecting(null);
          setDecideError(null);
          setParams({ review: reviewId });
          await load();
        } else if (rejectingRef.current) setDecideError(message);
        else setDecisionErrors((current) => ({ ...current, [reviewId]: message }));
      } finally {
        setDeciding(null);
      }
    },
    [load, setParams, loading, error, proposals, detailReady, focusedId],
  );

  /* A failed load must not leave three tiles asserting counts.
     `data?.byStatus[...] ?? 0` rendered "0 pending review" beside "The review
     queue could not be loaded" on a first failure, and the previous load's
     counts -- unmarked as stale -- on a later one. Both state a fact the
     screen does not have: "nothing is waiting for you" and "we could not find
     out" are different answers, and only one of them is safe to act on.
     Unknown renders as unknown, the same way the Catalog header renders its
     asset total before the count arrives. */
  const countsKnown = error === null && data !== null;
  const tileCount = (status: string): string =>
    countsKnown ? String(data?.byStatus[status] ?? 0) : "—";

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
          <div className="tile__n tnum">{tileCount("PENDING")}</div>
          <div className="tile__l">pending review</div>
        </div>
        <div className="tile tile--ok">
          <div className="tile__n tnum">{tileCount("APPROVED")}</div>
          <div className="tile__l">approved</div>
        </div>
        <div className="tile">
          <div className="tile__n tnum">{tileCount("REJECTED")}</div>
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
                ownProposal={principalId !== null && p.requested_by === principalId}
                decisionError={decisionErrors[p.review_id]}
                previewBlocked={FULL_PREVIEW_TYPES.has(p.object_type) && (detailReady !== p.review_id || focusedId !== p.review_id)}
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
        <ReviewDetailShell
          label="Proposal detail"
          className="rq__evidence"
          identity={{
            subject: renderRowExtras(focused).subject,
            target: `${focused.object_type} · ${focused.requested_action}`,
            status: focused.status,
            raisedBy: focused.requested_by,
            raisedAt: focused.created_at,
            confidence: focused.confidence ?? null,
          }}
          assignment={{
            decidedBy: focused.decided_by,
            decidedAt: focused.decided_at,
            decisionReason: focused.decision_reason,
            blockedReason:
              loading || error !== null
                ? "Refresh the review queue successfully before deciding this proposal."
                : focused.status !== "PENDING"
                ? `This review is already ${focused.status.toLowerCase()}.`
                : principalId !== null && focused.requested_by === principalId
                  ? "You proposed this change. Another reviewer must approve or reject it."
                  : FULL_PREVIEW_TYPES.has(focused.object_type) && detailReady !== focused.review_id
                    ? "Load the full change preview before deciding."
                    : null,
          }}
          diff={FULL_PREVIEW_TYPES.has(focused.object_type)
            ? <ReviewChangePreview key={focused.review_id} reviewId={focused.review_id} onReady={setDetailReady} />
            : <DiffEntries proposal={focused} />}
          /* Impact: this queue composes no consumer/impact set today. Saying so
             is the honest state -- an empty "affects nothing" would be a claim
             the read model never made. */
          evidence={
            (focused.evidence ?? []).length > 0 ? (
              <ol className="evl">
                {(focused.evidence ?? []).map((e, i) => (
                  <li key={i} className="evi evi--info">
                    <div className="evi__label">{e.category.replace(/_/g, " ")}</div>
                    <div className="evi__value">{e.claim}</div>
                    <div className="evi__source">{e.source}</div>
                  </li>
                ))}
              </ol>
            ) : undefined
          }
          decision={{
            busy: deciding === focused.review_id,
            error: decisionErrors[focused.review_id] ?? null,
            /* The endpoint refuses a REJECT without a rationale and accepts an
               APPROVE without one. Stated, not assumed. */
            reasonRequiredFor: ["REJECT"],
            onDecide: (verdict, reason) =>
              void decide(focused.review_id, verdict, reason),
          }}
          conflict={conflicts[focused.review_id] ?? null}
          onRefresh={() => void load()}
          onDismissConflict={() =>
            setConflicts((current) => {
              const next = { ...current };
              delete next[focused.review_id];
              return next;
            })
          }
          onClose={() => setParams({ review: null })}
          footer={
            /* The copied link names the screen that resolves this selection.
               Built as `origin + pathname + '?' + id` it carried no
               `#/governance`, so a fresh tab landed on the persona default and
               the id was read by nobody (review 2026-09-05, F08). */
            <CopyLinkButton
              target={{ screen: "governance", params: { review: focused.review_id } }}
              label="Copy permalink"
            />
          }
        />
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

/* ---------------------------------------------------------------------------
   THE FEDERATED REVIEW SURFACE — R11-S10.

   THE DEFECT this removes: a reviewer had to know that two separate queues
   existed and visit both. `#/governance` held everything backed by the
   `GovernanceReview` table -- twelve object types behind one type filter,
   already federated -- and `#/parsed-lineage-review` held the parser-produced
   lineage edges. Nothing on either screen mentioned the other. A steward
   agent's proposals landed in one, a lineage agent's in the other, and the
   only way to learn that was to be told.

   WHY THIS IS A TAB AND NOT ONE LIST. Checked against the backend before
   designing it: `GET /v1/governance/reviews/queue` takes an `object_type`
   filter and is genuinely cross-type, but it reads one table. The parsed
   lineage edges live in six separate parser tables, are decided through
   `POST /v1/lineage/parsed-edges/{id}/decision`, and have their own bulk
   semantics -- they never reach `GovernanceReview` and cannot be filtered
   through `object_type`. Presenting them as rows of one list would mean
   inventing a union the platform cannot decide over, and a reviewer would
   learn that only when a bulk action silently applied to half a selection.

   So the federation is honest about the seam: one destination, one place to
   look, two queues named for what they decide. T18's shared
   `ReviewDetailShell` already gives both the same detail contract, so the
   thing a reviewer actually does is identical on either tab.

   The retired `#/parsed-lineage-review` route resolves here with the parsed
   lineage tab selected -- see `RETIRED_SCREEN_ALIASES` in `lib/routes.ts`.
--------------------------------------------------------------------------- */

/** The parsed-lineage queue stays its own lazily-loaded module: it is a
 *  second tab, not a second screen, and a reviewer who never opens it should
 *  not pay for its chunk. */
const ParsedLineageReviewScreen = lazy(() =>
  import("./ParsedLineageReviewScreen").then((module) => ({
    default: module.ParsedLineageReviewScreen,
  })),
);

const QUEUES = [
  { id: "governance", label: "Governance proposals" },
  { id: "parsed-lineage", label: "Parsed lineage edges" },
] as const;

type QueueId = (typeof QUEUES)[number]["id"];

export function ReviewQueueScreen() {
  const [params, setParams] = useUrlState();
  const requested = params.get("queue");
  const queue: QueueId = requested === "parsed-lineage" ? "parsed-lineage" : "governance";

  return (
    <div className="rqf">
      {/* A tablist, not links: both queues are this one screen, and announcing
          them as navigation would tell a screen-reader user they are leaving
          it. Each queue still renders its own <h1>, so the heading always
          names what is actually in front of the reviewer. */}
      <div className="rqf__tabs" role="tablist" aria-label="Review queue">
        {QUEUES.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`review-queue-tab-${item.id}`}
            className="rqf__tab"
            aria-selected={item.id === queue}
            aria-controls={`review-queue-panel-${item.id}`}
            onClick={() =>
              /* Switching queue drops the other queue's selection and filters.
                 `review`, `status` and `type` are declared once for this screen
                 because they mean the same thing in both queues -- but a
                 focused governance review id is not an edge key, and leaving it
                 behind would open a detail pane on an item the new queue has
                 never heard of. */
              setParams({
                queue: item.id === "governance" ? null : item.id,
                review: null,
                status: null,
                type: null,
              })
            }
          >
            {item.label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`review-queue-panel-${queue}`}
        aria-labelledby={`review-queue-tab-${queue}`}
      >
        {queue === "governance" ? (
          <GovernanceReviewQueue />
        ) : (
          /* A local boundary, so downloading the second queue's chunk replaces
             the panel rather than the whole screen -- the shell's own Suspense
             sits above the tabs, and falling back to it would take the tabs off
             screen mid-switch. */
          <Suspense
            fallback={
              <div className="screenloading" role="status">
                Loading parsed lineage edges…
              </div>
            }
          >
            <ParsedLineageReviewScreen />
          </Suspense>
        )}
      </div>
    </div>
  );
}
