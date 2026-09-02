import type {
  AiDecisionRead,
  AssetEvidenceRead,
  ConsumerFooterRead,
  DataSourceRead,
  GovernanceDecisionRequest,
  GovernanceReviewRead,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  MeRead,
  ProjectRead,
  ReviewQueueRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioDiffRead,
  StudioImpactPreview,
  UnifiedLineageImpactRead,
} from "./types";
import type {
  AuditEventRead,
  CatalogRowRead,
  CursorPage,
  MarketplaceProductRead,
  MetadataTableRead,
  PageOf,
} from "./ui-types";
import {
  makeFixtureAuditEvents,
  makeFixtureCatalog,
  makeFixtureDecideReview,
  makeFixtureEvidence,
  makeFixtureLineageImpact,
  makeFixtureMarketplaceAccessRequest,
  makeFixtureMarketplaceProducts,
  makeFixtureMe,
  makeFixtureOrgDatasources,
  makeFixtureOrgProjects,
  makeFixtureRefusals,
  makeFixtureReviewQueue,
  makeFixtureRunDecisions,
  makeFixtureSemanticMetricConsumers,
  makeFixtureSemanticMetricVersions,
  makeFixtureSemanticModelConsumers,
  makeFixtureSemanticModelVersions,
  makeFixtureStudioChangeSetItems,
  makeFixtureStudioChangeSets,
  makeFixtureStudioDiff,
  makeFixtureStudioImpact,
  makeFixtureSubmitStudioChangeSet,
} from "./fixtures";

/* ---------------------------------------------------------------------------
   One fetch wrapper for the whole app.

   Two things it must get right that the current portal does not:
   1. Every non-2xx becomes a typed ApiError carrying the server's own detail
      string, so a screen can show what actually went wrong instead of
      "something went wrong".
   2. Every request is abortable. A catalog where typing in the filter box
      leaves eight in-flight requests racing to write the same state is the
      single most common source of "the UI showed me the wrong rows".
--------------------------------------------------------------------------- */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/** Set VITE_USE_FIXTURES=0 to run against a live API on :8000 via the dev proxy. */
const USE_FIXTURES = import.meta.env.VITE_USE_FIXTURES !== "0";

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    signal,
    headers: { Accept: "application/json" },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Same contract as `get`, for the write endpoints UX-15's screens call
 *  (governance decisions, marketplace access requests, Studio submit). No
 *  request body is optional here on purpose: every write this app makes
 *  carries one, even if it is `{}` -- an empty POST invites a caller to
 *  forget the body a route actually requires. */
async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    signal,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const errBody = (await res.json()) as { detail?: string };
      if (errBody.detail) detail = errBody.detail;
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export interface CatalogQuery {
  organizationId: string;
  q?: string;
  objectType?: string;
  status?: string;
  certification?: string;
  cursor?: string | null;
  limit?: number;
}

/**
 * `GET /v1/organizations/{org}/catalog/rows` (UX-12) exists now, but
 * `VITE_USE_FIXTURES` stays at its default here: it is shared with
 * `fetchAssetEvidence` below, which still runs against UX-13's not-yet-built
 * evidence endpoint, so flipping the flag globally would 404 that call
 * rather than just switch this screen to real data. Flip it to `0` once
 * UX-13 lands too, or split the flag per-endpoint sooner.
 */
export async function fetchCatalogRows(
  query: CatalogQuery,
  signal?: AbortSignal,
): Promise<CursorPage<CatalogRowRead>> {
  if (USE_FIXTURES) return makeFixtureCatalog(query);

  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.objectType && query.objectType !== "ALL")
    params.set("object_type", query.objectType);
  if (query.status) params.set("status", query.status);
  if (query.certification && query.certification !== "ALL")
    params.set("certification", query.certification);
  if (query.cursor) params.set("cursor", query.cursor);
  params.set("limit", String(query.limit ?? 100));

  return get<CursorPage<CatalogRowRead>>(
    `/v1/organizations/${query.organizationId}/catalog/rows?${params}`,
    signal,
  );
}

/**
 * The endpoint that exists TODAY (api.py:1808). Kept so the strangle migration
 * has a working fallback per datasource while the read-model row is built —
 * it returns eight fields, so a screen built on it cannot show certification,
 * ownership or quality without N+1 calls.
 */
export async function fetchTablesLegacy(
  datasourceId: string,
  opts: { q?: string; cursor?: string | null; limit?: number } = {},
  signal?: AbortSignal,
): Promise<CursorPage<MetadataTableRead>> {
  const params = new URLSearchParams();
  if (opts.q) params.set("q", opts.q);
  if (opts.cursor) params.set("cursor", opts.cursor);
  params.set("limit", String(opts.limit ?? 100));
  return get<CursorPage<MetadataTableRead>>(
    `/v1/datasources/${datasourceId}/tables?${params}`,
    signal,
  );
}

export async function fetchAssetEvidence(
  tableId: string,
  signal?: AbortSignal,
): Promise<AssetEvidenceRead> {
  if (USE_FIXTURES) return makeFixtureEvidence(tableId);
  return get<AssetEvidenceRead>(`/v1/metadata/tables/${tableId}/evidence`, signal);
}

/**
 * UX-1 / module 21 §5: the one call that decides whether the shell may offer a
 * persona picker at all. `identity_provider` is the server's own prod/dev gate
 * (`Settings.identity_provider`, `aida.security.get_security_context`) — the shell
 * defers to it rather than inferring its own, and in `OIDC` mode `persona` is the
 * only persona the UI is allowed to use, never a client-selected value.
 *
 * `GET /v1/me` exists today (unlike the read-model calls above), so flip
 * `VITE_USE_FIXTURES=0` to see the real thing; fixture mode reports `DEVELOPMENT`
 * with no persona so the manual switcher below still works for pure-frontend
 * iteration with no backend running.
 */
export async function fetchMe(signal?: AbortSignal): Promise<MeRead> {
  if (USE_FIXTURES) return makeFixtureMe();
  return get<MeRead>("/v1/me", signal);
}

/* ---------------------------------------------------------------------------
   UX-15: review queue, marketplace, lineage refusals and Studio change sets.
   UX-20: narrated lineage traversal (the impact endpoint's per-hop evidence).

   Every one of these hits a real, already-merged route (see this file's
   comment above `fetchCatalogRows` for what "merged" means here) — no
   backend stub, no invented endpoint. `USE_FIXTURES` gates each the same way
   the calls above already do, so `npm run dev`/`npm run test` need no
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
export async function fetchReviewQueue(
  query: ReviewQueueQuery,
  signal?: AbortSignal,
): Promise<ReviewQueueRead> {
  if (USE_FIXTURES) return makeFixtureReviewQueue(query);
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
}

/** `POST /v1/governance/reviews/{review_id}/decision` — maker-checker
 *  approve/reject, the same endpoint SM-7's diff screen decides against. */
export async function decideGovernanceReview(
  reviewId: string,
  body: GovernanceDecisionRequest,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) return makeFixtureDecideReview(reviewId, body);
  return postJson<GovernanceReviewRead>(
    `/v1/governance/reviews/${reviewId}/decision`,
    body,
    signal,
  );
}

export interface MarketplaceQuery {
  organizationId: string;
  q?: string;
  domain?: string;
  classification?: string;
  sort?: "personalized" | "catalog";
  limit?: number;
  offset?: number;
}

/** `GET /v1/marketplace/products` (CX-9, `product_marketplace_api.py`). The
 *  organization is implicit server-side (the caller's own `SecurityContext`)
 *  — `organizationId` here is fixture-mode-only, mirroring how
 *  `fetchCatalogRows` takes one explicitly while the live call never sends
 *  it on the wire. */
export async function fetchMarketplaceProducts(
  query: MarketplaceQuery,
  signal?: AbortSignal,
): Promise<PageOf<MarketplaceProductRead>> {
  if (USE_FIXTURES) return makeFixtureMarketplaceProducts(query);
  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.domain) params.set("domain", query.domain);
  if (query.classification) params.set("classification", query.classification);
  params.set("sort", query.sort ?? "personalized");
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<MarketplaceProductRead>>(`/v1/marketplace/products?${params}`, signal);
}

/** `POST /v1/marketplace/products/{version_id}/access-requests` — a
 *  governed, maker-checker access request (the same route the native MCP
 *  tool `request_marketplace_access` calls). */
export async function requestMarketplaceAccess(
  versionId: string,
  body: MarketplaceAccessRequestCreate,
  signal?: AbortSignal,
): Promise<MarketplaceAccessRequestRead> {
  if (USE_FIXTURES) return makeFixtureMarketplaceAccessRequest(versionId, body);
  return postJson<MarketplaceAccessRequestRead>(
    `/v1/marketplace/products/${versionId}/access-requests`,
    body,
    signal,
  );
}

/** `GET /v1/ai-decisions/refusals` (LN-3, `ai_decision_lineage_api.py`) —
 *  every `REFUSAL`-kind AI decision for the organization: an agent run
 *  declining to use or act on an asset, gated behind `PlatformAdmin`/
 *  `DataAdmin` in production (the endpoint's own `require_roles`). */
export async function fetchLineageRefusals(
  organizationId: string,
  opts: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<AiDecisionRead>> {
  if (USE_FIXTURES) return makeFixtureRefusals(opts);
  const params = new URLSearchParams({ organization_id: organizationId });
  params.set("limit", String(opts.limit ?? 50));
  params.set("offset", String(opts.offset ?? 0));
  return get<PageOf<AiDecisionRead>>(`/v1/ai-decisions/refusals?${params}`, signal);
}

/** `GET /v1/ai-decisions/{run_id}` — every decision (not only refusals) the
 *  named agent run made, for the evidence pane behind one refusal: what the
 *  run considered and rejected before it refused. */
export async function fetchRunDecisions(
  runId: string,
  organizationId: string,
  signal?: AbortSignal,
): Promise<AiDecisionRead[]> {
  if (USE_FIXTURES) return makeFixtureRunDecisions(runId);
  const params = new URLSearchParams({ organization_id: organizationId });
  return get<AiDecisionRead[]>(`/v1/ai-decisions/${runId}?${params}`, signal);
}

export interface StudioChangeSetQuery {
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/studio/change-sets` (ST-A7, `studio_api.py`). */
export async function fetchStudioChangeSets(
  query: StudioChangeSetQuery,
  signal?: AbortSignal,
): Promise<StudioChangeSetRead[]> {
  if (USE_FIXTURES) return makeFixtureStudioChangeSets(query);
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<StudioChangeSetRead[]>(`/v1/studio/change-sets?${params}`, signal);
}

/** `GET /v1/studio/change-sets/{id}/items` — every `StudioChangeItem` in one
 *  change set, each carrying its own `before`/`after` snapshot and precomputed
 *  `diff` (`compute_diff`, `aida.studio`). */
export async function fetchStudioChangeSetItems(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioChangeItemRead[]> {
  if (USE_FIXTURES) return makeFixtureStudioChangeSetItems(changeSetId);
  return get<StudioChangeItemRead[]>(`/v1/studio/change-sets/${changeSetId}/items`, signal);
}

/** `GET /v1/studio/change-sets/{id}/diff` — the same items' diffs, composed
 *  as one document for a change-set-level review. */
export async function fetchStudioDiff(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioDiffRead> {
  if (USE_FIXTURES) return makeFixtureStudioDiff(changeSetId);
  return get<StudioDiffRead>(`/v1/studio/change-sets/${changeSetId}/diff`, signal);
}

/** `GET /v1/studio/change-sets/{id}/impact` (`compute_impact`, `aida.studio`)
 *  — this is the change-set author's own evidence pane: what merging this
 *  change set would touch, before it is ever submitted for review. */
export async function fetchStudioImpact(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioImpactPreview> {
  if (USE_FIXTURES) return makeFixtureStudioImpact(changeSetId);
  return get<StudioImpactPreview>(`/v1/studio/change-sets/${changeSetId}/impact`, signal);
}

/** `POST /v1/studio/change-sets/{id}/submit` — the real test-gated,
 *  eval-gated submission path (ST-A7/ST-A8), materializing any
 *  `CONTEXT_PRODUCT` item through `studio_context_product.py` into the same
 *  `GovernanceReview` queue `ReviewQueueScreen` reads. */
export async function submitStudioChangeSet(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioChangeSetRead> {
  if (USE_FIXTURES) return makeFixtureSubmitStudioChangeSet(changeSetId);
  return postJson<StudioChangeSetRead>(`/v1/studio/change-sets/${changeSetId}/submit`, {}, signal);
}

/** `GET /v1/organizations/{id}/datasources` — resolves a datasource's display
 *  name to the id UX-20's lineage-impact call needs (`CatalogRowRead` only
 *  carries `datasource_name`, per this file's own catalog-rows note; the
 *  unified-lineage routes are scoped by `datasource_id`, so this bridges the
 *  two without a backend change). */
export async function fetchOrgDatasources(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<DataSourceRead>> {
  if (USE_FIXTURES) return makeFixtureOrgDatasources();
  return get<PageOf<DataSourceRead>>(
    `/v1/organizations/${organizationId}/datasources?limit=500`,
    signal,
  );
}

export interface LineageImpactQuery {
  depth?: number;
  nodeLimit?: number;
}

/** `GET /v1/datasources/{datasourceId}/unified-lineage/impact/{nodeId}`
 *  (`unified_lineage_api.py::build_unified_lineage_impact_payload`) — the
 *  bounded multi-hop upstream/downstream traversal UX-20 narrates. Each
 *  returned node's `depth` and `contributing_edge_sources` is the evidence
 *  per hop: how far the hop is from the question's subject, and which real
 *  lineage source (foreign key, dbt, OpenLineage, a view/procedure
 *  definition, or a steward-approved suggestion) contributed it — not
 *  narration text invented client-side. */
export async function fetchLineageImpact(
  datasourceId: string,
  nodeId: string,
  query: LineageImpactQuery = {},
  signal?: AbortSignal,
): Promise<UnifiedLineageImpactRead> {
  if (USE_FIXTURES) return makeFixtureLineageImpact(datasourceId, nodeId, query);
  const params = new URLSearchParams();
  params.set("depth", String(query.depth ?? 5));
  params.set("node_limit", String(query.nodeLimit ?? 200));
  return get<UnifiedLineageImpactRead>(
    `/v1/datasources/${datasourceId}/unified-lineage/impact/${encodeURIComponent(nodeId)}?${params}`,
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Semantics (UX-15/UX-16, `semantics` nav id) — `SemanticsScreen`'s own
   endpoints. See that screen's file-top comment for the honest scope: there
   is no org-wide "browse every published semantic model" endpoint, so this
   is a project picker (`fetchOrgProjects`, the real
   `GET /v1/organizations/{id}/projects`) feeding project-scoped model/metric
   lists — the same composition shape `fetchOrgDatasources` above already
   uses to bridge a display name to an id `unified-lineage` needs.
--------------------------------------------------------------------------- */

/** `GET /v1/organizations/{id}/projects` (`operational_api.py::list_organization_projects`)
 *  — real, already-merged, and NOT the org-wide semantic-model browse this
 *  screen would ideally have; it lists projects so a project can be picked,
 *  one call away from the project-scoped semantic-model-versions list below. */
export async function fetchOrgProjects(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<ProjectRead>> {
  if (USE_FIXTURES) return makeFixtureOrgProjects();
  return get<PageOf<ProjectRead>>(
    `/v1/organizations/${organizationId}/projects?limit=500`,
    signal,
  );
}

export interface SemanticPageQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{id}/semantic-model-versions` (`semantic_api.py::list_semantic_model_versions`)
 *  — project-scoped, not org-wide (see this file's Semantics banner comment). */
export async function fetchSemanticModelVersions(
  projectId: string,
  opts: SemanticPageQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<SemanticModelVersionRead>> {
  if (USE_FIXTURES) return makeFixtureSemanticModelVersions(projectId, opts);
  const params = new URLSearchParams();
  params.set("limit", String(opts.limit ?? 100));
  params.set("offset", String(opts.offset ?? 0));
  return get<PageOf<SemanticModelVersionRead>>(
    `/v1/projects/${projectId}/semantic-model-versions?${params}`,
    signal,
  );
}

/** `GET /v1/semantic-model-versions/{id}/metrics` (`semantic_api.py::list_metric_versions`)
 *  — every metric version defined on one semantic model version. */
export async function fetchSemanticMetricVersions(
  modelVersionId: string,
  opts: SemanticPageQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<SemanticMetricVersionRead>> {
  if (USE_FIXTURES) return makeFixtureSemanticMetricVersions(modelVersionId, opts);
  const params = new URLSearchParams();
  params.set("limit", String(opts.limit ?? 100));
  params.set("offset", String(opts.offset ?? 0));
  return get<PageOf<SemanticMetricVersionRead>>(
    `/v1/semantic-model-versions/${modelVersionId}/metrics?${params}`,
    signal,
  );
}

/** `GET /v1/semantic-model-versions/{id}/consumers` (UX-18, `semantic_api.py::
 *  get_semantic_model_version_consumers`) — who/what currently consumes this
 *  exact model version, from CX-4 consumption lineage. */
export async function fetchSemanticModelConsumers(
  modelVersionId: string,
  signal?: AbortSignal,
): Promise<ConsumerFooterRead> {
  if (USE_FIXTURES) return makeFixtureSemanticModelConsumers(modelVersionId);
  return get<ConsumerFooterRead>(
    `/v1/semantic-model-versions/${modelVersionId}/consumers`,
    signal,
  );
}

/** `GET /v1/semantic-metric-versions/{id}/consumers` (UX-18, `semantic_api.py::
 *  get_semantic_metric_version_consumers`) — same composition as the model
 *  consumer footer above, scoped to one metric version. */
export async function fetchSemanticMetricConsumers(
  metricVersionId: string,
  signal?: AbortSignal,
): Promise<ConsumerFooterRead> {
  if (USE_FIXTURES) return makeFixtureSemanticMetricConsumers(metricVersionId);
  return get<ConsumerFooterRead>(
    `/v1/semantic-metric-versions/${metricVersionId}/consumers`,
    signal,
  );
}

/* ---------------------------------------------------------------------------
   UX-16: Relationships — the review queue for N4's impact-ordered,
   diff-based `RelationshipCandidate` surface (`relationship_candidate_review.py`),
   plus RL-6's single/bulk decision endpoints and RL-7's optional confidence-
   calibration summary. Added here as a clearly-delimited block rather than
   folded into the imports/exports above, so this screen's additions are easy
   to find and to lift out cleanly if this file is ever split per screen.
--------------------------------------------------------------------------- */

import type {
  RelationshipCandidateBulkDecisionRequest,
  RelationshipCandidateBulkDecisionResultRead,
  RelationshipCandidateCalibrationRead,
  RelationshipCandidateDecision,
  RelationshipCandidateRead,
  RelationshipCandidateReviewQueueRead,
} from "./types";
import {
  makeFixtureBulkDecideRelationshipCandidates,
  makeFixtureDecideRelationshipCandidate,
  makeFixtureRelationshipCandidateCalibration,
  makeFixtureRelationshipCandidateReviewQueue,
} from "./fixtures";

export interface RelationshipCandidateReviewQueueQuery {
  limit?: number;
  offset?: number;
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
export async function fetchRelationshipCandidateReviewQueue(
  datasourceId: string,
  query: RelationshipCandidateReviewQueueQuery = {},
  signal?: AbortSignal,
): Promise<RelationshipCandidateReviewQueueRead> {
  if (USE_FIXTURES) return makeFixtureRelationshipCandidateReviewQueue(datasourceId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return get<RelationshipCandidateReviewQueueRead>(
    `/v1/datasources/${datasourceId}/relationship-candidates/review-queue?${params}`,
    signal,
  );
}

/** `POST /v1/relationship-candidates/{candidateId}/decision` — maker-checker
 *  approve/reject of one PENDING candidate. A REJECT with no `reason` is
 *  rejected server-side (`RelationshipCandidateDecision.require_reason`);
 *  callers should collect a reason before calling this the same way
 *  `decideGovernanceReview` above expects one. */
export async function decideRelationshipCandidate(
  candidateId: string,
  body: RelationshipCandidateDecision,
  signal?: AbortSignal,
): Promise<RelationshipCandidateRead> {
  if (USE_FIXTURES) return makeFixtureDecideRelationshipCandidate(candidateId, body);
  return postJson<RelationshipCandidateRead>(
    `/v1/relationship-candidates/${candidateId}/decision`,
    body,
    signal,
  );
}

/** `POST /v1/relationship-candidates/bulk-decision` (RL-6) — decides up to
 *  500 PENDING candidates by explicit id list in one call; a rule violation
 *  on one candidate marks that candidate FAILED in the response and the
 *  rest still proceed (partial success), never aborting the whole batch. */
export async function bulkDecideRelationshipCandidates(
  body: RelationshipCandidateBulkDecisionRequest,
  signal?: AbortSignal,
): Promise<RelationshipCandidateBulkDecisionResultRead> {
  if (USE_FIXTURES) return makeFixtureBulkDecideRelationshipCandidates(body);
  return postJson<RelationshipCandidateBulkDecisionResultRead>(
    `/v1/relationship-candidates/bulk-decision`,
    body,
    signal,
  );
}

/** `GET /v1/relationship-candidates/confidence-calibration` (RL-7) — this
 *  organization's own observed steward-approval rate per confidence bucket,
 *  from its real decision history (never a published external calibration
 *  curve — see the endpoint's own `methodology_note`, echoed verbatim in the
 *  response). Optional secondary info for a calibration summary tile;
 *  `datasourceId: null` reports the org-wide history. */
export async function fetchRelationshipCandidateCalibration(
  datasourceId: string | null,
  signal?: AbortSignal,
): Promise<RelationshipCandidateCalibrationRead> {
  if (USE_FIXTURES) return makeFixtureRelationshipCandidateCalibration(datasourceId);
  const params = new URLSearchParams();
  if (datasourceId) params.set("datasource_id", datasourceId);
  return get<RelationshipCandidateCalibrationRead>(
    `/v1/relationship-candidates/confidence-calibration?${params}`,
    signal,
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
