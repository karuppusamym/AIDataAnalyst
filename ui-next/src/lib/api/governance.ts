/* ---------------------------------------------------------------------------
   Governance — the queues a human decides in, and the ledger of what was
   decided.

   Review queue and its decision endpoint; the relationship-candidate and
   parsed-lineage-edge review queues with their single and bulk decisions;
   the negative-knowledge registry (an assertion a human already rejected);
   the audit ledger; and compliance packs, the evidence bundle those
   decisions are exported into.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import { USE_FIXTURES } from "../appConfig";
import {
  makeFixtureAuditEvents,
  makeFixtureBulkDecideRelationshipCandidates,
  makeFixtureCompliancePacks,
  makeFixtureDecideRelationshipCandidate,
  makeFixtureDecideReview,
  makeFixtureDownloadCompliancePack,
  makeFixtureGenerateCompliancePack,
  makeFixtureLiftSuppression,
  makeFixtureNegativeKnowledgeSearch,
  makeFixtureNegativeKnowledgeSubject,
  makeFixtureRelationshipCandidateCalibration,
  makeFixtureRelationshipCandidateReviewQueue,
  makeFixtureRelationshipCandidates,
  makeFixtureReviewQueue,
  makeFixtureParsedLineageReviewQueue,
} from "../fixtures";
import type {
  CompliancePackRead,
  GeneratePackRequest,
  GovernanceDecisionRequest,
  GovernanceReviewRead,
  LiftSuppressionRequest,
  NegativeAssertionRead,
  RelationshipCandidateBulkDecisionRequest,
  RelationshipCandidateBulkDecisionResultRead,
  RelationshipCandidateCalibrationRead,
  RelationshipCandidateDecision,
  RelationshipCandidateRead,
  RelationshipCandidateReviewQueueRead,
  ReviewQueueRead,
} from "../types";
import type { AuditEventRead, PageOf } from "../ui-types";

/* ---------------------------------------------------------------------------
   UX-15: review queue, marketplace, lineage refusals and Studio change sets.
   UX-20: narrated lineage traversal (the impact endpoint's per-hop evidence).

   Every one of these hits a real, already-merged route (see the comment on
   `fetchCatalogRows` in `./catalog.ts` for what "merged" means here) — no
   backend stub, no invented endpoint. `USE_FIXTURES` gates each the same way
   every other call in this client does, so `npm run dev`/`npm run test` need no
   backend, and `VITE_USE_FIXTURES=0` runs every one of these against the
   real API on :8000.
--------------------------------------------------------------------------- */

export interface ReviewQueueQuery {
  status?: string | null;
  objectType?: string | null;
  inferenceRunId?: string | null;
  limit?: number;
}

/** `GET /v1/governance/reviews/queue` (UX-17, `review_queue_api.py`). */
export function fetchReviewQueue(
  query: ReviewQueueQuery,
  signal?: AbortSignal,
): Promise<ReviewQueueRead> {
  return demoOr(
    async () => makeFixtureReviewQueue(query),
    async () => {
      const params = new URLSearchParams();
      // `status=` (empty string) is the endpoint's own "every status" escape
      // hatch — distinct from omitting the param, which falls back to its
      // server-side default of PENDING. `null` here means "the caller asked for
      // every status", so it must reach the wire as an explicit empty value.
      if (query.status !== undefined) params.set("status", query.status ?? "");
      if (query.objectType) params.set("object_type", query.objectType);
      if (query.inferenceRunId) params.set("inference_run_id", query.inferenceRunId);
      params.set("limit", String(query.limit ?? 1000));
      return get<ReviewQueueRead>(`/v1/governance/reviews/queue?${params}`, signal);
    },
  );
}

/** `POST /v1/governance/reviews/{review_id}/decision` — maker-checker
 *  approve/reject, the same endpoint SM-7's diff screen decides against. */
export function decideGovernanceReview(
  reviewId: string,
  body: GovernanceDecisionRequest,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    async () => makeFixtureDecideReview(reviewId, body),
    async () => {
      return postJson<GovernanceReviewRead>(
        `/v1/governance/reviews/${reviewId}/decision`,
        body,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   UX-16: Relationships — the review queue for N4's impact-ordered,
   diff-based `RelationshipCandidate` surface (`relationship_candidate_review.py`),
   plus RL-6's single/bulk decision endpoints and RL-7's optional confidence-
   calibration summary.
--------------------------------------------------------------------------- */

export interface RelationshipCandidateReviewQueueQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{datasourceId}/relationship-candidates` (the raw list
 *  behind the review queue, `list_relationship_candidates`). Unlike the
 *  review-queue read model this can return candidates in ANY state via
 *  `candidate_status`, so a reviewer can see what was already approved or
 *  rejected — the decision history the PENDING-only queue drops. Read-only. */
export function fetchRelationshipCandidates(
  datasourceId: string,
  opts: { status?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<RelationshipCandidateRead>> {
  return demoOr(
    async () => makeFixtureRelationshipCandidates(datasourceId, opts.status),
    async () => {
      const params = new URLSearchParams();
      if (opts.status && opts.status !== "ALL") params.set("candidate_status", opts.status);
      params.set("limit", String(opts.limit ?? 200));
      return get<PageOf<RelationshipCandidateRead>>(
        `/v1/datasources/${datasourceId}/relationship-candidates?${params}`,
        signal,
      );
    },
  );
}

/** `GET /v1/datasources/{datasourceId}/relationship-candidates/review-queue`
 *  (N4, `get_relationship_candidate_review_queue` — `intelligence_api.py`) —
 *  PENDING relationship candidates for one datasource, sorted by real
 *  computed lineage impact (EA.14's bounded traversal), each carrying an
 *  SM-7 "nothing → this edge" diff and an AT-15 per-signal confidence
 *  breakdown. Supersedes the raw, confidence-sorted
 *  `GET .../relationship-candidates` list for a reviewer triaging a
 *  backlog — read-only; deciding a candidate goes through the two functions
 *  below. */
export function fetchRelationshipCandidateReviewQueue(
  datasourceId: string,
  query: RelationshipCandidateReviewQueueQuery = {},
  signal?: AbortSignal,
): Promise<RelationshipCandidateReviewQueueRead> {
  return demoOr(
    async () => makeFixtureRelationshipCandidateReviewQueue(datasourceId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<RelationshipCandidateReviewQueueRead>(
        `/v1/datasources/${datasourceId}/relationship-candidates/review-queue?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/relationship-candidates/{candidateId}/decision` — maker-checker
 *  approve/reject of one PENDING candidate. A REJECT with no `reason` is
 *  rejected server-side (`RelationshipCandidateDecision.require_reason`);
 *  callers should collect a reason before calling this the same way
 *  `decideGovernanceReview` above expects one. */
export function decideRelationshipCandidate(
  candidateId: string,
  body: RelationshipCandidateDecision,
  signal?: AbortSignal,
): Promise<RelationshipCandidateRead> {
  return demoOr(
    async () => makeFixtureDecideRelationshipCandidate(candidateId, body),
    async () => {
      return postJson<RelationshipCandidateRead>(
        `/v1/relationship-candidates/${candidateId}/decision`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/relationship-candidates/bulk-decision` (RL-6) — decides up to
 *  500 PENDING candidates by explicit id list in one call; a rule violation
 *  on one candidate marks that candidate FAILED in the response and the
 *  rest still proceed (partial success), never aborting the whole batch. */
export function bulkDecideRelationshipCandidates(
  body: RelationshipCandidateBulkDecisionRequest,
  signal?: AbortSignal,
): Promise<RelationshipCandidateBulkDecisionResultRead> {
  return demoOr(
    async () => makeFixtureBulkDecideRelationshipCandidates(body),
    async () => {
      return postJson<RelationshipCandidateBulkDecisionResultRead>(
        `/v1/relationship-candidates/bulk-decision`,
        body,
        signal,
      );
    },
  );
}

/** `GET /v1/relationship-candidates/confidence-calibration` (RL-7) — this
 *  organization's own observed steward-approval rate per confidence bucket,
 *  from its real decision history (never a published external calibration
 *  curve — see the endpoint's own `methodology_note`, echoed verbatim in the
 *  response). Optional secondary info for a calibration summary tile;
 *  `datasourceId: null` reports the org-wide history. */
export function fetchRelationshipCandidateCalibration(
  datasourceId: string | null,
  signal?: AbortSignal,
): Promise<RelationshipCandidateCalibrationRead> {
  return demoOr(
    async () => makeFixtureRelationshipCandidateCalibration(datasourceId),
    async () => {
      const params = new URLSearchParams();
      if (datasourceId) params.set("datasource_id", datasourceId);
      return get<RelationshipCandidateCalibrationRead>(
        `/v1/relationship-candidates/confidence-calibration?${params}`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   UX-16: Audit ledger — org-wide, no datasource picker.
--------------------------------------------------------------------------- */

export interface AuditEventQuery {
  organizationId: string;
  action?: string;
  resourceType?: string;
  correlationId?: string;
  /** ISO 8601 datetime, MUST carry a timezone offset (e.g. end in `Z` or
   *  `+05:30`) — `list_audit_events` (`operational_api.py:336`) 422s a naive
   *  datetime rather than guessing what timezone it was meant in. Checked
   *  client-side below so a naive-looking value never reaches the wire. */
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

const TZ_AWARE_ISO = /(Z|[+-]\d{2}:?\d{2})$/i;

function assertTimezoneAware(label: "since" | "until", value: string): void {
  if (!TZ_AWARE_ISO.test(value)) {
    throw new Error(
      `${label} must be a timezone-aware ISO datetime (e.g. end with "Z"), got "${value}"`,
    );
  }
}

/** `GET /v1/organizations/{organization_id}/audit-events` (UX-16,
 *  `list_audit_events`, `operational_api.py:336`) — every `AuditEvent` the
 *  org has recorded, filterable by `action`/`resource_type`/`correlation_id`/
 *  `since`/`until` and paginated by `limit`/`offset` (NOT a cursor — this
 *  route's own signature, unlike `fetchCatalogRows`'s keyset one). Gated
 *  server-side behind `PlatformAdmin`/`OrganizationAdmin`/`Auditor`/
 *  `Operations` (the route's own `require_roles`); an unauthorized caller
 *  gets the same 403 any other gated call in this file surfaces. */
export async function fetchAuditEvents(
  query: AuditEventQuery,
  signal?: AbortSignal,
): Promise<PageOf<AuditEventRead>> {
  if (query.since) assertTimezoneAware("since", query.since);
  if (query.until) assertTimezoneAware("until", query.until);
  if (USE_FIXTURES) return makeFixtureAuditEvents(query);

  const params = new URLSearchParams();
  if (query.action) params.set("action", query.action);
  if (query.resourceType) params.set("resource_type", query.resourceType);
  if (query.correlationId) params.set("correlation_id", query.correlationId);
  if (query.since) params.set("since", query.since);
  if (query.until) params.set("until", query.until);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));

  return get<PageOf<AuditEventRead>>(
    `/v1/organizations/${query.organizationId}/audit-events?${params}`,
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Negative knowledge (Phase E / EE.3, `negative_knowledge_api.py`) — the
   registry of assertions a human has previously rejected, each carrying a
   suppression flag so the platform stops re-proposing something already
   rejected, plus a manual lift path for once something material changes.

   Org-wide like the audit ledger above: none of these three routes take an
   organization id in the path or query at all -- scope is implicit
   server-side (`context.require_organization()`), unlike most other calls in
   this file.
--------------------------------------------------------------------------- */

export interface NegativeKnowledgeSearchQuery {
  assertionType?: string;
  suppressionActive?: boolean;
  limit?: number;
  offset?: number;
}

/** `GET /v1/negative-knowledge/search` — filterable browse across every
 *  negative assertion recorded for the organization. Both filters are
 *  optional on the wire (`Query(default=None)`) and are omitted from the
 *  query string entirely when unset, never sent as an empty string. */
export function searchNegativeKnowledge(
  query: NegativeKnowledgeSearchQuery,
  signal?: AbortSignal,
): Promise<PageOf<NegativeAssertionRead>> {
  return demoOr(
    async () => makeFixtureNegativeKnowledgeSearch(query),
    async () => {
      const params = new URLSearchParams();
      if (query.assertionType) params.set("assertion_type", query.assertionType);
      if (query.suppressionActive !== undefined) {
        params.set("suppression_active", String(query.suppressionActive));
      }
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<NegativeAssertionRead>>(`/v1/negative-knowledge/search?${params}`, signal);
    },
  );
}

export interface NegativeKnowledgeSubjectQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/negative-knowledge/{subject_id}` — every assertion recorded
 *  against one specific subject; a distinct lookup from the filtered
 *  `search` above, not a special case of it. */
export function fetchNegativeKnowledgeForSubject(
  subjectId: string,
  query: NegativeKnowledgeSubjectQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<NegativeAssertionRead>> {
  return demoOr(
    async () => makeFixtureNegativeKnowledgeSubject(subjectId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<NegativeAssertionRead>>(
        `/v1/negative-knowledge/${encodeURIComponent(subjectId)}?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/negative-knowledge/{id}/lift-suppression` — manually lifts
 *  suppression on one assertion. The endpoint requires a >=3-char `reason`
 *  (`LiftSuppressionRequest.reason`, `Field(min_length=3)`); callers should
 *  collect one before calling this. */
export function liftNegativeAssertionSuppression(
  assertionId: string,
  body: LiftSuppressionRequest,
  signal?: AbortSignal,
): Promise<NegativeAssertionRead> {
  return demoOr(
    async () => makeFixtureLiftSuppression(assertionId, body),
    async () => {
      return postJson<NegativeAssertionRead>(
        `/v1/negative-knowledge/${assertionId}/lift-suppression`,
        body,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   Compliance packs (module EE.4/OB-5) -- audit-ready evidence bundles
   generated from runtime evidence and downloaded as structured JSON
   (`compliance_api.py`). Org is derived server-side from the auth context
   (`context.require_organization()`, `compliance_api.py:74/128/152/181`) --
   none of these three routes take an `organization_id` path segment, unlike
   most of this file's other calls, so no org id is threaded through here.
--------------------------------------------------------------------------- */

export interface CompliancePackQuery {
  framework?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/compliance/packs` (`list_compliance_packs`, `compliance_api.py:119`).
 *  Gated server-side behind `PlatformAdmin`/`ComplianceOfficer`/`DataSteward`/
 *  `Viewer` -- a Viewer can see the list (name/framework/status/generated_at)
 *  but not a pack's evidence body, see `downloadCompliancePack` below. */
export function fetchCompliancePacks(
  query: CompliancePackQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<CompliancePackRead>> {
  return demoOr(
    async () => makeFixtureCompliancePacks(query),
    async () => {
      const params = new URLSearchParams();
      if (query.framework) params.set("framework", query.framework);
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<CompliancePackRead>>(`/v1/compliance/packs?${params}`, signal);
    },
  );
}

/** `POST /v1/compliance/packs/generate` (`generate_compliance_pack`,
 *  `compliance_api.py:62`) -- gated behind `PlatformAdmin`/`ComplianceOfficer`/
 *  `DataSteward` (no `Viewer`). The route itself 422s when `period_end` is
 *  not after `period_start`; that detail string is surfaced as-is, not
 *  re-validated client-side. */
export function generateCompliancePack(
  body: GeneratePackRequest,
  signal?: AbortSignal,
): Promise<CompliancePackRead> {
  return demoOr(
    async () => makeFixtureGenerateCompliancePack(body),
    async () => {
      return postJson<CompliancePackRead>("/v1/compliance/packs/generate", body, signal);
    },
  );
}

/** `GET /v1/compliance/packs/{pack_id}/download` (`download_compliance_pack`,
 *  `compliance_api.py:174`) -- the pack's structured evidence body
 *  (`response_model=dict[str, Any]`, no dedicated Pydantic model on the
 *  wire, hence the plain `Record` return type here). Gated behind
 *  `PlatformAdmin`/`ComplianceOfficer`/`DataSteward` ONLY -- deliberately
 *  narrower than the list/get-by-id routes above, which also allow
 *  `Viewer`. A Viewer's 403 here is the route working as designed (they can
 *  see a pack exists, not its evidence body), not a bug to route around --
 *  render it as the same `ErrorState` any other 403 in this app gets. */
export function downloadCompliancePack(
  packId: string,
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  return demoOr(
    async () => makeFixtureDownloadCompliancePack(packId),
    async () => {
      return get<Record<string, unknown>>(`/v1/compliance/packs/${packId}/download`, signal);
    },
  );
}

// -------------------------------------------------------------------------
// P1-05 / ADR-0026: parsed-lineage-edge review queue.
//
// The five non-governed parser-produced lineage edge tables all share the
// same review lifecycle. `listParsedLineageReviewQueue` composes across all
// five; `decideParsedLineageEdge` / `bulkDecideParsedLineageEdges` mirror
// the shape and semantics of `decideRelationshipCandidate` /
// `bulkDecideRelationshipCandidates` above.
// -------------------------------------------------------------------------

/** `GET /v1/lineage/parsed-edges/review-queue` -- one paginated view across
 *  the five non-governed parser-produced lineage edge tables, filtered to
 *  review_status="PROPOSED". */
export interface ParsedLineageReviewQueueQuery {
  edgeType?: import("../ui-types").ParsedLineageEdgeType | null;
  minConfidence?: number | null;
  limit?: number;
  offset?: number;
}

export async function listParsedLineageReviewQueue(
  query: ParsedLineageReviewQueueQuery,
  signal?: AbortSignal,
): Promise<import("../types").ParsedLineageEdgeReviewQueueRead> {
  const params = new URLSearchParams();
  if (query.edgeType) params.set("edge_type", query.edgeType);
  if (query.minConfidence != null)
    params.set("min_confidence", String(query.minConfidence));
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  // This was the one review client with no demo branch, so the screen behind
  // it issued a live request in the default fixtures build and rendered the
  // backend's "X-Principal-Id is required" as a load failure.
  return demoOr(
    () => makeFixtureParsedLineageReviewQueue(query),
    () =>
      get<import("../types").ParsedLineageEdgeReviewQueueRead>(
        `/v1/lineage/parsed-edges/review-queue?${params}`,
        signal,
      ),
  );
}

/** `POST /v1/lineage/parsed-edges/{edge_id}/decision` -- maker-checker
 *  approve/reject of one PROPOSED parsed lineage edge. A reason is
 *  required by the schema; callers should collect one before posting. */
export async function decideParsedLineageEdge(
  edgeId: string,
  body: import("../types").ParsedLineageEdgeDecisionRequest,
  signal?: AbortSignal,
): Promise<import("../types").ParsedLineageEdgeDecisionRead> {
  return postJson<import("../types").ParsedLineageEdgeDecisionRead>(
    `/v1/lineage/parsed-edges/${edgeId}/decision`,
    body,
    signal,
  );
}

/** `POST /v1/lineage/parsed-edges/bulk-decide` -- up to 100 edges per call.
 *  A per-item failure marks that item FAILED in the response and the rest
 *  still commit (partial-success, per-item SAVEPOINT semantics). */
export async function bulkDecideParsedLineageEdges(
  body: import("../types").ParsedLineageEdgeBulkDecisionRequest,
  signal?: AbortSignal,
): Promise<import("../types").ParsedLineageEdgeBulkDecisionResultRead> {
  return postJson<import("../types").ParsedLineageEdgeBulkDecisionResultRead>(
    `/v1/lineage/parsed-edges/bulk-decide`,
    body,
    signal,
  );
}
