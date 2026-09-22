import type { ReactNode } from "react";
import { readDecision, roleHolds } from "../lib/roles";
import type { ReadDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import { navigateTo } from "../lib/navigate";
import { Button } from "../components/primitives";
import type { Tone } from "../components/primitives";

/* ---------------------------------------------------------------------------
   What the two Glossary review tabs share (R11-AUD08): who may read, who may
   write, and the few words both say the same way.

   THE ROLE LISTS live here and not in `lib/roles.ts` for the reason that file
   gives: a list is copied from the surface-control matrix
   (`Docs/50-security/surface-control-matrix.md`, generated from the
   application's own `require_roles` dependencies) and belongs beside the
   requests it guards, where whoever reads the request sees it. Both tabs guard
   their requests with these two lists, so they are stated once, in the module
   both import.
--------------------------------------------------------------------------- */

/**
 * The roles the two list routes admit: `GET /v1/organizations/{organization_id}/glossary-conflicts`
 * and `GET /v1/organizations/{organization_id}/glossary-link-proposals`.
 *
 * Copied from the surface-control matrix rows for
 * `aida.stewardship_api.list_glossary_conflicts` and
 * `aida.stewardship_api.list_glossary_link_proposals` (the two rows are
 * identical): Analyst, Auditor, DataAdmin, DataSteward, MetadataAdmin,
 * PlatformAdmin, Reviewer, SemanticAdmin, Viewer.
 */
export const GLOSSARY_REVIEW_READ_ROLES = [
  "Analyst",
  "Auditor",
  "DataAdmin",
  "DataSteward",
  "MetadataAdmin",
  "PlatformAdmin",
  "Reviewer",
  "SemanticAdmin",
  "Viewer",
];

/**
 * The roles every WRITE on this screen admits: `POST .../glossary-conflicts/detect`,
 * `POST .../glossary-conflicts`, `POST /v1/glossary-conflicts/{conflict_id}/resolution`,
 * `POST .../glossary-link-proposals/generate` and
 * `POST /v1/glossary-link-proposals/{proposal_id}/submit`.
 *
 * Copied from the matrix rows for `aida.stewardship_api.detect_glossary_conflicts`,
 * `create_glossary_conflict`, `submit_conflict_resolution`,
 * `generate_glossary_link_proposals` and `submit_glossary_link_proposal` (all
 * five are identical): DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin.
 */
export const GLOSSARY_REVIEW_WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"];

/**
 * The roles that may READ the review queue a submission lands in, so the
 * "Open the Review queue" link is offered only to a session that would not be
 * refused there. `MetadataAdmin` may submit a proposal and may not open the
 * queue, which is why this is its own list and not the write list.
 *
 * Copied from the matrix rows for `aida.review_queue_api.get_review_queue` and
 * `get_review_queue_summary`: DataSteward, PlatformAdmin, Reviewer, SemanticAdmin.
 */
const REVIEW_QUEUE_READ_ROLES = ["DataSteward", "PlatformAdmin", "Reviewer", "SemanticAdmin"];

export const listOr = (items: readonly string[]): string =>
  items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;

export interface GlossaryReviewAccess {
  /** `wait` while `/v1/me` is in flight, `ask` when admitted or identity will not say otherwise, `skip` when known not to be. */
  readonly read: ReadDecision;
  /** Known to hold a write role (fail closed: nothing that changes the glossary is offered on a guess). */
  readonly mayWrite: boolean;
  /** Identity has answered, so a sentence about THIS session's roles is true rather than premature. */
  readonly identityKnown: boolean;
  /** Known to hold a role the review queue admits. */
  readonly mayOpenReviewQueue: boolean;
}

/**
 * What this session may do on Glossary review.
 *
 * The read is HELD while identity resolves (`readDecision`) and a session known
 * to be outside the read list is never asked -- it takes "not applicable", not
 * the 403 a doomed request would earn on every load. The controls are the other
 * way round: they need the role to be KNOWN (`roleHolds`), so a Detect or a
 * Resolve is never offered to a session that turns out not to be a steward.
 */
export function useGlossaryReviewAccess(): GlossaryReviewAccess {
  const session = useSession();
  const roles = session.me?.roles;
  return {
    read: readDecision(session, GLOSSARY_REVIEW_READ_ROLES),
    mayWrite: roleHolds(roles, GLOSSARY_REVIEW_WRITE_ROLES),
    identityKnown: roles !== undefined,
    mayOpenReviewQueue: roleHolds(roles, REVIEW_QUEUE_READ_ROLES),
  };
}

export function humanize(value: string): string {
  return value.toLowerCase().replace(/_/g, " ");
}

/** A timestamp as the same words for everyone, in every timezone and in a test. */
export function stamp(iso: string | null): string {
  return iso ? `${iso.slice(0, 16).replace("T", " ")} UTC` : "an unknown time";
}

/** A UUID as the short form a person can read out, the way an audit row does. */
export function shortId(value: string): string {
  return value.slice(0, 8);
}

/** Status pills, one vocabulary for both families. */
export function statusTone(status: string): Tone {
  switch (status) {
    case "OPEN":
    case "DRAFT":
      return "warn";
    case "REVIEW_REQUIRED":
      return "info";
    case "RESOLVED":
    case "APPROVED":
      return "ok";
    case "REJECTED":
      return "bad";
    default:
      return "mute";
  }
}

export function Pager({
  offset,
  limit,
  total,
  shown,
  noun,
  onPage,
}: {
  offset: number;
  limit: number;
  total: number;
  /** How many rows this page holds. */
  shown: number;
  noun: string;
  onPage: (offset: number) => void;
}) {
  const first = shown === 0 ? 0 : offset + 1;
  const last = offset + shown;
  const hasPrevious = offset > 0;
  const hasNext = last < total;
  return (
    <nav className="glrev__pager" aria-label={`${noun} pages`}>
      <span className="glrev__count" role="status">
        {shown === 0 ? `No ${noun}` : `${first}–${last} of ${total} ${noun}`}
      </span>
      {hasPrevious || hasNext ? (
        <span className="glrev__pagerbtns">
          <Button disabled={!hasPrevious} onClick={() => onPage(Math.max(0, offset - limit))}>
            Previous page
          </Button>
          <Button disabled={!hasNext} onClick={() => onPage(offset + limit)}>
            Next page
          </Button>
        </span>
      ) : null}
    </nav>
  );
}

/**
 * Says where a submission went, and how to follow it -- the half of "opens a
 * governance review" that a steward can act on.
 *
 * The link is offered only to a session the review queue would admit
 * (`mayOpenReviewQueue`): a MetadataAdmin may submit and is told so without
 * being sent to a screen that refuses them.
 */
export function ReviewHandoff({
  children,
  reviewId,
  mayOpenReviewQueue,
}: {
  children: ReactNode;
  /** The review the submission just opened -- the response to `resolution` / `submit`. */
  reviewId: string;
  mayOpenReviewQueue: boolean;
}) {
  return (
    <div className="glrev__notice" role="status">
      <p>{children}</p>
      {mayOpenReviewQueue ? (
        // `review` focuses the one review the submission opened.
        <Button onClick={() => navigateTo("governance", { review: reviewId })}>Open the Review queue</Button>
      ) : null}
    </div>
  );
}
