/* ---------------------------------------------------------------------------
   Ownership: standing rules, the leaver flow, and the reviews they open
   (R11-AUD08, part 2 of 4).

   WHAT THE NAMES HIDE. "Apply a rule" and "reassign a leaver's ownerships" read
   like the ownership changes when the button is pressed. Neither does. Both
   handlers (`aida.stewardship_api.apply_ownership_rule` and
   `request_leaver_reassignment`) end in the same place: one
   `BulkStewardshipOperation` at status `REVIEW_REQUIRED`, `applied_count` 0, and
   one `GovernanceReview` requested by the caller. An ownership is written only
   when a DIFFERENT principal approves that review (`semantic_api` answers a
   requester who tries to decide their own with 409 "maker-checker separation is
   required"), through `stewardship_service.apply_bulk_operation`. Every screen
   that calls these functions says so, because the alternative -- a steward who
   believes a leaver's tables now belong to the successor -- is the failure this
   whole flow exists to prevent.

   THE OTHER TWO THINGS THAT SHAPE THE CLIENT:

     * `apply` has no preview. The handler looks for matching tables when it is
       called, and takes no body. So there is nothing to fetch before the
       confirmation, and the confirmation says that.
     * The assignments list has no owner filter (`subject_type` and `subject_id`
       only), so "what does this leaver hold?" cannot be asked of the server. The
       leaver preview (`fetchOwnershipPortfolio`) pages the listing and filters
       here, and reports when it stopped before the end. The request itself does
       not depend on the preview: omitting `assignment_ids` makes the server
       discover the whole portfolio on its own.

   The read/reaffirm functions for assignments already live in `./catalog`
   (`fetchOwnershipAssignments`, `reaffirmOwnershipAssignment`,
   `bulkReaffirmOwnershipAssignments`, added for P2-07). They are reused here,
   not copied.

   Transport, identity headers and the demo switch come from `./transport`.
   Demo answers come from `../ownershipFixtures`, imported inside the demo arm so
   a production build, which folds `demoOr` to its live arm, ships none of it.
   Re-exported from `lib/api.ts`.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import { fetchOwnershipAssignments } from "./catalog";
import type { OwnershipAssignmentRead } from "./catalog";
import type {
  BulkStewardshipOperationRead,
  LeaverReassignmentRequest,
  OwnershipRuleCreate,
  OwnershipRuleRead,
} from "../types";
import type { PageOf } from "../ui-types";

/** The five things a rule can match a table on -- `aida.schemas.OwnershipRuleCreate.match_field`. */
export const OWNERSHIP_MATCH_FIELDS = [
  "TABLE_NAME",
  "SCHEMA_NAME",
  "QUALIFIED_NAME",
  "DOMAIN_KEY",
  "TAG",
] as const;
export type OwnershipMatchField = (typeof OWNERSHIP_MATCH_FIELDS)[number];

/** `owner_type` on a rule, an assignment and a leaver request. */
export const OWNERSHIP_OWNER_TYPES = ["INDIVIDUAL", "GROUP"] as const;
export type OwnershipOwnerType = (typeof OWNERSHIP_OWNER_TYPES)[number];

/** `OwnershipRuleCreate.rule_key`. The pattern is the server's, verbatim. */
export const OWNERSHIP_RULE_KEY_PATTERN = /^[a-z][a-z0-9_-]{1,99}$/;

/** Most tables one `apply` puts into a review: `apply_ownership_rule` stops
 *  collecting at 500 matches and does not say whether it left any behind. */
export const OWNERSHIP_RULE_APPLY_MAX_TABLES = 500;

/** Most ownerships one leaver request can carry: `LEAVER_REASSIGNMENT_MAX_ITEMS`
 *  in `stewardship_api`, which is also `max_length` of `assignment_ids`. Past it a
 *  whole-portfolio request records `selection_truncated` and covers the first 500. */
export const LEAVER_REASSIGNMENT_MAX_ITEMS = 500;

/** Most ids one bulk reaffirm takes: `OwnershipAssignmentBulkReaffirmRequest.assignment_ids`. */
export const BULK_REAFFIRM_MAX_ITEMS = 100;

/** The list route's own `limit` ceiling (`list_ownership_assignments`, `le=500`). */
export const OWNERSHIP_LIST_LIMIT = 500;

/** How far the leaver preview reads before it stops and says so: 20 pages of 500. */
export const OWNERSHIP_PORTFOLIO_MAX_PAGES = 20;

const demo = () => import("../ownershipFixtures");

/** `GET /v1/organizations/{organization_id}/ownership-rules` -- the ACTIVE rules,
 *  by display name, capped at 500 by the handler. It takes no filter and does not
 *  page, and there is no route to edit or retire a rule. */
export function fetchOwnershipRules(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<OwnershipRuleRead>> {
  return demoOr(
    async () => (await demo()).fixtureOwnershipRules(organizationId),
    () => get<PageOf<OwnershipRuleRead>>(`/v1/organizations/${organizationId}/ownership-rules`, signal),
  );
}

/** `POST /v1/organizations/{organization_id}/ownership-rules` (`create_ownership_rule`).
 *  Records the rule and nothing else: no table is matched and no owner assigned
 *  until the rule is applied. A key already in use is a 409. */
export function createOwnershipRule(
  organizationId: string,
  body: OwnershipRuleCreate,
  signal?: AbortSignal,
): Promise<OwnershipRuleRead> {
  return demoOr(
    async () => (await demo()).fixtureCreateOwnershipRule(organizationId, body),
    () => postJson<OwnershipRuleRead>(`/v1/organizations/${organizationId}/ownership-rules`, body, signal),
  );
}

/** `POST /v1/ownership-rules/{rule_id}/apply` (`apply_ownership_rule`). No body, no
 *  preview. Finds the rule's matching active tables now (at most 500) and opens ONE
 *  `ASSIGN_OWNERSHIP` review for them; 409 when nothing matches. Returns the
 *  operation at `REVIEW_REQUIRED` -- nothing is owned differently yet. */
export function applyOwnershipRule(
  ruleId: string,
  signal?: AbortSignal,
): Promise<BulkStewardshipOperationRead> {
  return demoOr(
    async () => (await demo()).fixtureApplyOwnershipRule(ruleId),
    () => postJson<BulkStewardshipOperationRead>(`/v1/ownership-rules/${ruleId}/apply`, {}, signal),
  );
}

/** `POST /v1/organizations/{organization_id}/stewardship/leaver-reassignment`
 *  (`request_leaver_reassignment`). Opens ONE `REASSIGN_LEAVER` review. Omit
 *  `assignment_ids` and the server takes the leaver's whole active portfolio (first
 *  500, `parameters.selection_truncated` set when there was more); name them and each
 *  must be an ACTIVE assignment the leaver holds with this `owner_type` (409 if not). */
export function requestLeaverReassignment(
  organizationId: string,
  body: LeaverReassignmentRequest,
  signal?: AbortSignal,
): Promise<BulkStewardshipOperationRead> {
  return demoOr(
    // The demo arm is HANDED the fixture module (`demoOr`), and the assignments the request is checked
    // against come from it. `ownershipFixtures` does not import `../fixtures` itself: that module's one
    // dynamic reference is the transport seam's (`demoDataMode.test.ts` pins it), and a second one would
    // put the whole demo estate back into a production build.
    async (fixtures) =>
      (await demo()).fixtureRequestLeaverReassignment(organizationId, body, fixtures.makeFixtureOwnershipAssignments),
    () =>
      postJson<BulkStewardshipOperationRead>(
        `/v1/organizations/${organizationId}/stewardship/leaver-reassignment`,
        body,
        signal,
      ),
  );
}

export interface OwnershipOperationQuery {
  /** Exact status, upper-cased by the server: `REVIEW_REQUIRED`, `APPLIED`, `REJECTED`. */
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/stewardship/bulk-operations` -- every bulk
 *  stewardship operation, newest first. The screen keeps the two kinds that change
 *  ownership (`ASSIGN_OWNERSHIP`, `REASSIGN_LEAVER`); the route has no type filter.
 *  This is where "how many were actually moved" lives once a reviewer has decided:
 *  `applied_count` against `subject_ids.length`. */
export function fetchOwnershipOperations(
  organizationId: string,
  query: OwnershipOperationQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<BulkStewardshipOperationRead>> {
  return demoOr(
    async () => (await demo()).fixtureOwnershipOperations(query),
    () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<BulkStewardshipOperationRead>>(
        `/v1/organizations/${organizationId}/stewardship/bulk-operations?${params}`,
        signal,
      );
    },
  );
}

/** What the leaver preview found. */
export interface OwnershipPortfolio {
  /** The ACTIVE assignments the principal holds with the asked-for owner type,
   *  among the rows that were read. Each id once: the listing pages by creation
   *  time, and rows created together share a timestamp, so a page boundary can
   *  repeat one. */
  readonly items: OwnershipAssignmentRead[];
  /** How many ACTIVE assignments the organization has, as the listing counted them. */
  readonly total: number;
  /** How many rows were read to find `items`. */
  readonly scanned: number;
  /** False when the read stopped at `OWNERSHIP_PORTFOLIO_MAX_PAGES` before reaching
   *  `total`: the principal may hold more than `items` shows. */
  readonly complete: boolean;
}

/**
 * What one principal holds, worked out from the assignments listing.
 *
 * The server has no such question: `list_ownership_assignments` filters on
 * `subject_type` and `subject_id` only. So this reads pages of 500 and keeps the
 * rows whose `owner_principal` and `owner_type` match, reusing `fetchOwnershipAssignments`
 * so the demo estate and the live API are read exactly as every other caller reads them.
 *
 * It is a PREVIEW and is labelled as one by its callers: the leaver request does not
 * send this list unless the steward narrowed it, and the server re-validates every id it
 * is sent. The bound is deliberate -- an organization with a million assignments must not
 * make a preview button read all of them -- and `complete` is how the bound is reported.
 */
export async function fetchOwnershipPortfolio(
  organizationId: string,
  principal: string,
  ownerType: OwnershipOwnerType,
  signal?: AbortSignal,
): Promise<OwnershipPortfolio> {
  const held = new Map<string, OwnershipAssignmentRead>();
  let scanned = 0;
  let total = 0;
  for (let page = 0; page < OWNERSHIP_PORTFOLIO_MAX_PAGES; page += 1) {
    const result = await fetchOwnershipAssignments(
      organizationId,
      { limit: OWNERSHIP_LIST_LIMIT, offset: scanned },
      signal,
    );
    total = result.total;
    scanned += result.items.length;
    for (const row of result.items) {
      if (row.status === "ACTIVE" && row.owner_principal === principal && row.owner_type === ownerType) {
        held.set(row.id, row);
      }
    }
    if (result.items.length === 0 || scanned >= total) break;
  }
  return { items: [...held.values()], total, scanned, complete: scanned >= total };
}
