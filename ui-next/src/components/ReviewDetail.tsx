import { useState } from "react";
import type { ReactNode } from "react";

import { ApiError } from "../lib/http";
import { Button, ConfirmDialog, Pill } from "./primitives";
import "./EvidencePane.css";
import "./ReviewDetail.css";

/* ---------------------------------------------------------------------------
   One review detail (review 2026-09-05, T18 · F05).

   THE DEFECT this removes: `ReviewQueueScreen` and `ParsedLineageReviewScreen`
   each built their own idea of what a reviewer needs in front of them before
   deciding. The governance queue had a diff, an evidence list and a permalink
   but never said who a decision was assigned to or waiting on; the parsed
   lineage queue had neither a detail pane nor a permalink, and collected its
   rationale from a bare text input that no dialog owned. Neither showed a
   *conflict* as anything but a sentence in an error slot -- so a reviewer who
   lost a race was told "409 Conflict" while the server had already reported
   which decision won, who made it, when, and why.

   THE INVARIANT: every review type presents the same five things in the same
   order -- identity/target, the proposed change, its impact, who is assigned
   and what has already been decided, and the decision controls -- and the only
   part that varies by type is the EVIDENCE, which each screen passes in as a
   slot. Edge-type rules, per-object-type diff rendering and the specialised
   evidence a type composes stay where they are; what a reviewer must always be
   shown is decided once, here.

   This is deliberately NOT a merge of the two screens. They keep their own
   routes, their own queues, their own filters and their own bulk rules. What
   they share is the shell they decide inside.
--------------------------------------------------------------------------- */

export type ReviewVerdict = "APPROVE" | "REJECT";

/** The decision that won a contended review, as the server reported it. */
export interface WinningDecision {
  readonly reviewId: string | null;
  /** The terminal status the winner wrote: APPROVED / REJECTED. */
  readonly status: string | null;
  readonly decidedBy: string | null;
  readonly decidedAt: string | null;
  readonly decisionReason: string | null;
}

export interface ReviewConflict {
  /** The server's own sentence, never paraphrased. */
  readonly message: string;
  /** `CONFLICT`, `NOT_PERMITTED`, … when the server named one. */
  readonly outcome: string | null;
  /**
   * The decision that beat this caller, when the endpoint sent a refreshed
   * snapshot. `null` means the endpoint refused without reporting one -- which
   * is unknown, NOT "nobody else decided". The banner says so rather than
   * inventing a winner.
   */
  readonly winning: WinningDecision | null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * Read a refused decision out of a failed request.
 *
 * The governance decision service answers a lost claim with a structured 409:
 * `{"detail": {"message", "outcome", "review": {review_id, status, decided_by,
 * decided_at, decision_reason}}}` (`semantic_api._refusal_as_http_exception`,
 * F05). `ApiError.details` now preserves that object; before it did not exist
 * and the whole payload was dropped by the decoder.
 *
 * Returns `null` for anything that is not a refusal about this review's state
 * -- a network failure, a 500, a 422 -- because those belong beside the button
 * that was pressed, not in a panel claiming somebody else decided.
 */
export function conflictFromError(error: unknown): ReviewConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const details = error.details;
  const review =
    details && typeof details.review === "object" && details.review !== null
      ? (details.review as Record<string, unknown>)
      : null;
  return {
    message: error.detail,
    outcome: details ? stringOrNull(details.outcome) : null,
    winning: review
      ? {
          reviewId: stringOrNull(review.review_id),
          status: stringOrNull(review.status),
          decidedBy: stringOrNull(review.decided_by),
          decidedAt: stringOrNull(review.decided_at),
          decisionReason: stringOrNull(review.decision_reason),
        }
      : null,
  };
}

function formatMoment(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

/**
 * What the reviewer lost the race to.
 *
 * Two shapes, deliberately distinct. A refusal that carried a refreshed review
 * says which decision won and by whom; a refusal that carried nothing says only
 * what the server said. The second must never be dressed up as the first --
 * "configured is not healthy, unknown is not zero" applies to a decision as
 * much as to a scan.
 */
function ConflictBanner({
  conflict,
  onRefresh,
  onDismiss,
}: {
  conflict: ReviewConflict;
  onRefresh?: () => void;
  onDismiss?: () => void;
}) {
  const winner = conflict.winning;
  const raced = conflict.outcome === "CONFLICT" || winner !== null;
  const decidedAt = formatMoment(winner?.decidedAt);
  return (
    <section
      className={`rvdconflict rvdconflict--${raced ? "raced" : "refused"}`}
      role="alert"
      aria-label={raced ? "Another decision won this review" : "This decision was refused"}
    >
      <h3 className="rvdconflict__h">
        {raced ? "Another reviewer decided first" : "This decision was refused"}
      </h3>
      <p className="rvdconflict__msg">{conflict.message}</p>
      {winner ? (
        <dl className="rvdconflict__facts">
          <div>
            <dt>Recorded decision</dt>
            <dd>{winner.status ? winner.status.toLowerCase() : "not reported"}</dd>
          </div>
          <div>
            <dt>Decided by</dt>
            <dd>{winner.decidedBy ?? "not reported"}</dd>
          </div>
          <div>
            <dt>Decided at</dt>
            <dd>{decidedAt ?? "not reported"}</dd>
          </div>
          <div>
            <dt>Rationale</dt>
            <dd>{winner.decisionReason ?? "none recorded"}</dd>
          </div>
        </dl>
      ) : raced ? (
        <p className="rvdconflict__unknown">
          This endpoint did not report which decision won. Reload the queue to see the
          review's current state — do not assume it is still open.
        </p>
      ) : null}
      <div className="rvdconflict__act">
        {onRefresh ? <Button onClick={onRefresh}>Reload this queue</Button> : null}
        {onDismiss ? <Button onClick={onDismiss}>Dismiss</Button> : null}
      </div>
    </section>
  );
}

/** Who raised this, and what it is a change to. */
export interface ReviewIdentity {
  /** The headline a reviewer recognises the item by. */
  readonly subject: string;
  /** Type and requested action, in the vocabulary of the queue it came from. */
  readonly target: string;
  /** PENDING / APPROVED / REJECTED / PROPOSED — the queue's own word. */
  readonly status: string;
  readonly raisedBy?: string | null;
  readonly raisedAt?: string | null;
  /** Proposer confidence, 0..1. Rendered as a claim, never as authority. */
  readonly confidence?: number | null;
}

/** The maker/checker facts, including whichever of them blocks this reviewer. */
export interface ReviewAssignment {
  readonly decidedBy?: string | null;
  readonly decidedAt?: string | null;
  readonly decisionReason?: string | null;
  /**
   * Why this principal may not decide, in a sentence. `null` when they may.
   * Rendered in place of the decision controls: an action that cannot succeed
   * must explain itself rather than being silently absent or merely disabled.
   */
  readonly blockedReason?: string | null;
}

export interface ReviewDecisionControls {
  readonly busy: boolean;
  /** A failure that is not about this review's state — shown beside the buttons. */
  readonly error?: string | null;
  /**
   * The verdicts that may not be recorded without a written rationale. The
   * governance endpoint refuses a REJECT without one; the parsed-lineage
   * endpoint requires one for both. Stated per screen rather than assumed.
   */
  readonly reasonRequiredFor: readonly ReviewVerdict[];
  readonly onDecide: (verdict: ReviewVerdict, reason: string | null) => void;
  readonly approveLabel?: string;
  readonly rejectLabel?: string;
}

const statusTone = (status: string) => {
  const upper = status.toUpperCase();
  if (upper === "APPROVED" || upper === "ACTIVE") return "ok" as const;
  if (upper === "REJECTED") return "mute" as const;
  if (upper === "PENDING" || upper === "PROPOSED") return "warn" as const;
  return "info" as const;
};

/**
 * The pane every review type decides inside.
 *
 * `evidence` is the slot: whatever establishes this particular kind of claim
 * (a governance proposal's composed `EvidenceItemRead` list, a parsed edge's
 * parser reference and confidence coercion) is rendered by the screen that
 * understands it. Everything else is fixed, because it is the same question
 * on every review: what is changing, what does it affect, who owns it, and has
 * somebody already decided.
 */
export function ReviewDetailShell({
  identity,
  assignment,
  diff,
  impact,
  evidence,
  decision,
  conflict,
  onRefresh,
  onDismissConflict,
  onClose,
  footer,
  label = "Review detail",
  className,
}: {
  identity: ReviewIdentity;
  assignment?: ReviewAssignment;
  /** The proposed change. `undefined` renders the "no structured diff" line. */
  diff?: ReactNode;
  /** What approving would affect. `undefined` states that it is not computed. */
  impact?: ReactNode;
  /** Type-specific evidence. This is the whole point of the slot. */
  evidence?: ReactNode;
  decision?: ReviewDecisionControls;
  conflict?: ReviewConflict | null;
  onRefresh?: () => void;
  onDismissConflict?: () => void;
  onClose?: () => void;
  footer?: ReactNode;
  label?: string;
  /** The owning screen's own placement class (`.rq__evidence` docks the pane
   *  bottom-right). The shell owns its contents, not where a screen puts it. */
  className?: string;
}) {
  /* The rationale dialog lives here, not in each screen: it is the same
     transaction every review type performs, and collecting it with an
     unlabelled input (parsed lineage) or `window.prompt` (the governance
     queue, before F21) were two different ways of losing a reviewer's
     decision. */
  const [pendingVerdict, setPendingVerdict] = useState<ReviewVerdict | null>(null);
  const blocked = assignment?.blockedReason ?? null;
  const decided = Boolean(assignment?.decidedBy || assignment?.decidedAt);
  const requiresReason = (verdict: ReviewVerdict) =>
    decision?.reasonRequiredFor.includes(verdict) ?? false;

  const submit = (verdict: ReviewVerdict, reason: string | null) => {
    setPendingVerdict(null);
    decision?.onDecide(verdict, reason);
  };

  return (
    <aside className={`evp rvd${className ? ` ${className}` : ""}`} aria-label={label}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name">{identity.subject}</div>
          <div className="evp__path">{identity.target}</div>
        </div>
        {onClose ? (
          <button className="evp__x" onClick={onClose} aria-label="Close">
            ×
          </button>
        ) : null}
      </header>

      <div className="evp__body">
        <div className="rvd__badges">
          <Pill tone={statusTone(identity.status)}>{identity.status.toLowerCase()}</Pill>
          {identity.confidence !== null && identity.confidence !== undefined ? (
            <Pill tone="mute">{`proposer confidence ${Math.round(identity.confidence * 100)}%`}</Pill>
          ) : null}
        </div>

        {conflict ? (
          <ConflictBanner
            conflict={conflict}
            onRefresh={onRefresh}
            onDismiss={onDismissConflict}
          />
        ) : null}

        <section className="rvd__sec" aria-label="Proposed change">
          <h3 className="rvd__h">Proposed change</h3>
          {diff ?? <p className="rvd__none">No structured diff for this object type.</p>}
        </section>

        <section className="rvd__sec" aria-label="Impact">
          <h3 className="rvd__h">Impact</h3>
          {impact ?? (
            <p className="rvd__none">
              No impact set is computed for this review type. Approving changes only the
              object named above.
            </p>
          )}
        </section>

        <section className="rvd__sec" aria-label="Assignment">
          <h3 className="rvd__h">Assignment</h3>
          <dl className="rvd__facts">
            <div>
              <dt>Raised by</dt>
              <dd>{identity.raisedBy ?? "not recorded"}</dd>
            </div>
            <div>
              <dt>Raised at</dt>
              <dd>{formatMoment(identity.raisedAt) ?? "not recorded"}</dd>
            </div>
            <div>
              <dt>Decided by</dt>
              <dd>{assignment?.decidedBy ?? (decided ? "not recorded" : "awaiting a checker")}</dd>
            </div>
            <div>
              <dt>Decided at</dt>
              <dd>{formatMoment(assignment?.decidedAt) ?? (decided ? "not recorded" : "—")}</dd>
            </div>
            {assignment?.decisionReason ? (
              <div>
                <dt>Rationale</dt>
                <dd>{assignment.decisionReason}</dd>
              </div>
            ) : null}
          </dl>
        </section>

        <section className="rvd__sec" aria-label="Evidence">
          <h3 className="rvd__h">Evidence</h3>
          {evidence ?? (
            <p className="rvd__none">No evidence was composed for this item.</p>
          )}
        </section>
      </div>

      {decision ? (
        <footer className="evp__foot rvd__foot">
          {blocked ? (
            <span className="rvd__blocked">{blocked}</span>
          ) : (
            <>
              <Button
                variant="primary"
                disabled={decision.busy}
                onClick={() =>
                  requiresReason("APPROVE")
                    ? setPendingVerdict("APPROVE")
                    : submit("APPROVE", null)
                }
              >
                {decision.approveLabel ?? "Approve"}
              </Button>
              <Button
                disabled={decision.busy}
                onClick={() =>
                  requiresReason("REJECT") ? setPendingVerdict("REJECT") : submit("REJECT", null)
                }
              >
                {decision.rejectLabel ?? "Reject"}
              </Button>
            </>
          )}
          {decision.error ? (
            <span className="rvd__err" role="alert">
              {decision.error}
            </span>
          ) : null}
          {footer}
        </footer>
      ) : footer ? (
        <footer className="evp__foot rvd__foot">{footer}</footer>
      ) : null}

      {pendingVerdict ? (
        <ConfirmDialog
          title={pendingVerdict === "APPROVE" ? "Approve this review" : "Reject this review"}
          description="The rationale is recorded on the review and is visible to whoever raised it."
          reasonLabel={
            pendingVerdict === "APPROVE"
              ? "Why is this being approved?"
              : "Why is this being rejected?"
          }
          requireReason
          destructive={pendingVerdict === "REJECT"}
          confirmLabel={pendingVerdict === "APPROVE" ? "Approve" : "Reject"}
          busy={decision?.busy ?? false}
          error={decision?.error ?? null}
          onCancel={() => setPendingVerdict(null)}
          onConfirm={(reason) => submit(pendingVerdict, reason)}
        />
      ) : null}
    </aside>
  );
}
