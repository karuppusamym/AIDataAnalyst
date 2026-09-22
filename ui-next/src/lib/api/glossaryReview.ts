/* ---------------------------------------------------------------------------
   Glossary review -- conflicts and term-link proposals (R11-AUD08).

   Seven routes in `src/aida/stewardship_api.py` that had no `ui-next` caller.
   Both families are the same shape of work -- a steward asks the platform to
   find something (`detect`, `generate`), reads what it found, and puts one
   finding in front of an independent reviewer (`resolution`, `submit`) -- and
   both end in a `GovernanceReview` that somebody else decides. That last part
   is why nothing here ever changes a term or a link directly: the only thing
   these writes do to the glossary itself is nothing, until a reviewer approves.

   WHAT EACH WRITE DOES, from the handler and not from its name (a UI that
   describes a control from its label alone is how a steward learns to click
   through confirmations):

     POST .../glossary-conflicts/detect       reads up to 5,000 approved, active
        terms and raises an OPEN SYNONYM_COLLISION for each pair sharing a label
        (display name or synonym, case-folded) but not a definition -- at most
        100 per run, skipping pairs already OPEN or in review. A pair whose
        earlier conflict was RESOLVED is raised again if the terms still
        collide: a resolution edits neither term. Answers the conflicts it
        created, not the whole set.
     POST .../glossary-conflicts              records one conflict a steward saw
        for themselves (any of three types, two free-form positions).
     POST /v1/glossary-conflicts/{id}/resolution   only an OPEN conflict. Moves it
        to REVIEW_REQUIRED, records the proposed decision, and opens a
        `GLOSSARY_CONFLICT` governance review (202). Approval marks it RESOLVED;
        rejection reopens it and clears the proposal. Neither edits a term.
     POST .../glossary-link-proposals/generate   matches approved business
        annotations' names and synonyms exactly (case-insensitive) against
        approved, active terms' display names, keys and synonyms, and creates a
        DRAFT proposal for each pair not already linked or proposed -- in ANY
        status, so a rejected link is never proposed again. Nothing is linked.
     POST /v1/glossary-link-proposals/{id}/submit   only a DRAFT. Moves it to
        REVIEW_REQUIRED and opens a `GLOSSARY_LINK_PROPOSAL` review (202).
        Approval creates an INFERRED link; rejection closes the proposal.

   Demo mode answers from `../glossaryReviewFixtures`, a small in-memory store
   that follows those same rules (an OPEN conflict only, a DRAFT proposal only),
   so the journey can be walked without a backend. The fixtures are reached
   through a dynamic import behind a loader that tests the build's demo literal
   itself (`noDemoData` says why `demoOr` alone did not drop it), so a live build
   does not contain them.

   Transport, identity headers and the demo switch come from `./transport`.
   Re-exported from `lib/api.ts`.
--------------------------------------------------------------------------- */

import { demoOr, get, noDemoData, postJson } from "./transport";
import type { GlossaryLinkProposalRead } from "./glossary";
import type {
  GlossaryConflictCreate,
  GlossaryConflictRead,
  GlossaryConflictResolution,
  GlossaryLinkProposalGenerate,
  GovernanceReviewRead,
} from "../types";
import type { PageOf } from "../ui-types";

/** The demo store, loaded only where a demo arm runs, and absent from a live build (`noDemoData`). */
const glossaryReviewDemo = (): Promise<typeof import("../glossaryReviewFixtures")> =>
  import.meta.env.VITE_USE_FIXTURES === "0" ? noDemoData() : import("../glossaryReviewFixtures");

/** What `generate` accepts, from `GlossaryLinkProposalGenerate` in `schemas.py`.
 *  Mirrored so the form can refuse a value the server would refuse anyway. */
export const LINK_PROPOSAL_CONFIDENCE_MIN = 0.5;
export const LINK_PROPOSAL_CONFIDENCE_MAX = 1.0;
export const LINK_PROPOSAL_CONFIDENCE_DEFAULT = 0.75;
export const LINK_PROPOSAL_LIMIT_MAX = 500;
export const LINK_PROPOSAL_LIMIT_DEFAULT = 200;

/** `detect` stops at this many new conflicts (`len(created) == 100`). */
export const CONFLICT_DETECT_RUN_LIMIT = 100;

export interface GlossaryReviewListQuery {
  /** Exact status, upper-cased by the server. Omit for every status. */
  status?: string | null;
  limit?: number;
  offset?: number;
}

function listParams(query: GlossaryReviewListQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return params;
}

/** `GET /v1/organizations/{organization_id}/glossary-conflicts`
 *  (`stewardship_api.list_glossary_conflicts`), newest first.
 *
 *  Every conflict type shares this table and this route, INCLUDING
 *  `METRIC_FORMULA_COLLISION` (`semantic_api.detect_metric_formula_collisions`
 *  writes there with `term_id: null` and metric-shaped positions). There is no
 *  type filter, so a caller must be ready for positions that are not glossary
 *  terms. */
export function fetchGlossaryConflicts(
  organizationId: string,
  query: GlossaryReviewListQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<GlossaryConflictRead>> {
  return demoOr(
    () => glossaryReviewDemo().then((m) => m.fixtureGlossaryConflicts(query)),
    () =>
      get<PageOf<GlossaryConflictRead>>(
        `/v1/organizations/${organizationId}/glossary-conflicts?${listParams(query)}`,
        signal,
      ),
  );
}

/** `POST /v1/organizations/{organization_id}/glossary-conflicts/detect`
 *  (`stewardship_api.detect_glossary_conflicts`). No request body. The response
 *  is a `Page` of the conflicts THIS run created -- `total` is that count, not
 *  the number of conflicts in the organization. */
export function detectGlossaryConflicts(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<GlossaryConflictRead>> {
  return demoOr(
    () => glossaryReviewDemo().then((m) => m.fixtureDetectGlossaryConflicts()),
    () =>
      postJson<PageOf<GlossaryConflictRead>>(
        `/v1/organizations/${organizationId}/glossary-conflicts/detect`,
        undefined,
        signal,
      ),
  );
}

/** `POST /v1/organizations/{organization_id}/glossary-conflicts`
 *  (`stewardship_api.create_glossary_conflict`) -- 201, the new OPEN conflict. */
export function raiseGlossaryConflict(
  organizationId: string,
  body: GlossaryConflictCreate,
  signal?: AbortSignal,
): Promise<GlossaryConflictRead> {
  return demoOr(
    () => glossaryReviewDemo().then((m) => m.fixtureRaiseGlossaryConflict(body)),
    () =>
      postJson<GlossaryConflictRead>(
        `/v1/organizations/${organizationId}/glossary-conflicts`,
        body,
        signal,
      ),
  );
}

/** `POST /v1/glossary-conflicts/{conflict_id}/resolution`
 *  (`stewardship_api.submit_conflict_resolution`) -- 202, the `GovernanceReview`
 *  an independent reviewer now has to decide. Refuses 409 "only open conflicts
 *  can be resolved" when the conflict has already moved on. */
export function submitGlossaryConflictResolution(
  conflictId: string,
  body: GlossaryConflictResolution,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    () =>
      glossaryReviewDemo().then((m) =>
        m.fixtureSubmitGlossaryConflictResolution(conflictId, body),
      ),
    () =>
      postJson<GovernanceReviewRead>(
        `/v1/glossary-conflicts/${conflictId}/resolution`,
        body,
        signal,
      ),
  );
}

/** `GET /v1/organizations/{organization_id}/glossary-link-proposals`
 *  (`stewardship_api.list_glossary_link_proposals`), newest first. */
export function fetchGlossaryLinkProposals(
  organizationId: string,
  query: GlossaryReviewListQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<GlossaryLinkProposalRead>> {
  return demoOr(
    () => glossaryReviewDemo().then((m) => m.fixtureGlossaryLinkProposals(query)),
    () =>
      get<PageOf<GlossaryLinkProposalRead>>(
        `/v1/organizations/${organizationId}/glossary-link-proposals?${listParams(query)}`,
        signal,
      ),
  );
}

/** `POST /v1/organizations/{organization_id}/glossary-link-proposals/generate`
 *  (`stewardship_api.generate_glossary_link_proposals`). The response is a
 *  `Page` of the DRAFT proposals THIS run created, attributed to the caller;
 *  `limit` is the cap the caller asked for. */
export function generateGlossaryLinkProposals(
  organizationId: string,
  body: GlossaryLinkProposalGenerate,
  signal?: AbortSignal,
): Promise<PageOf<GlossaryLinkProposalRead>> {
  return demoOr(
    () =>
      glossaryReviewDemo().then((m) => m.fixtureGenerateGlossaryLinkProposals(body)),
    () =>
      postJson<PageOf<GlossaryLinkProposalRead>>(
        `/v1/organizations/${organizationId}/glossary-link-proposals/generate`,
        body,
        signal,
      ),
  );
}

/** `POST /v1/glossary-link-proposals/{proposal_id}/submit`
 *  (`stewardship_api.submit_glossary_link_proposal`) -- 202, the
 *  `GovernanceReview` (`GLOSSARY_LINK_PROPOSAL`, `APPROVE_LINK`). Refuses 409
 *  "only draft link proposals can be submitted". No request body. */
export function submitGlossaryLinkProposal(
  proposalId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    () =>
      glossaryReviewDemo().then((m) => m.fixtureSubmitGlossaryLinkProposal(proposalId)),
    () =>
      postJson<GovernanceReviewRead>(
        `/v1/glossary-link-proposals/${proposalId}/submit`,
        undefined,
        signal,
      ),
  );
}
