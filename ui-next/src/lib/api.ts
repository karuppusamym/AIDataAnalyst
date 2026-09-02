import type {
  AgentAnalysisRequest,
  AgentAnalysisResponse,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  AiDecisionRead,
  AnalysisRunRead,
  AssetEvidenceRead,
  BusinessMapRead,
  ConsumerFooterRead,
  DataQualityIncidentRead,
  DataQualityIncidentTransition,
  DataQualitySummaryRead,
  DataSourceRead,
  FleetSummaryRead,
  GovernanceDecisionRequest,
  GovernanceReviewRead,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  MeRead,
  MetadataBusinessAnnotationRead,
  MetadataIngestionBatchRead,
  OutboxEventRead,
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
  makeFixtureAgentAnalysis,
  makeFixtureAgentRun,
  makeFixtureAgentRunGroundingReceipts,
  makeFixtureAgentRuns,
  makeFixtureAnalysisRuns,
  makeFixtureAuditEvents,
  makeFixtureBusinessAnnotations,
  makeFixtureBusinessMap,
  makeFixtureCatalog,
  makeFixtureDecideReview,
  makeFixtureEvidence,
  makeFixtureFleetSummary,
  makeFixtureIngestionBatches,
  makeFixtureLineageImpact,
  makeFixtureMarketplaceAccessRequest,
  makeFixtureMarketplaceProducts,
  makeFixtureMe,
  makeFixtureOrgDatasources,
  makeFixtureOrgProjects,
  makeFixtureOutboxEvents,
  makeFixtureQualityIncidents,
  makeFixtureQualitySummary,
  makeFixtureRefusals,
  makeFixtureRequeueOutboxEvent,
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
  makeFixtureTableBusinessAnnotation,
  makeFixtureTransitionQualityIncident,
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
   Ask (UX-15/UX-16, tracker rows UX-15/UX-16): the single-shot governed
   question-answering endpoint (`run_agent_analysis`, `api.py:2912`) and its
   history/evidence reads. Every one of these hits a real, already-merged
   route -- no backend stub, no invented endpoint, same standing as the
   UX-15/UX-20 calls above.
--------------------------------------------------------------------------- */

/** `POST /v1/datasources/{id}/agent-analyses` (`run_agent_analysis`,
 *  `api.py:2912`) -- ask a governed question against one datasource.
 *  Single-shot, not streaming: one JSON response carrying the explanation,
 *  the query that was actually run, and every piece of evidence
 *  (`step_trace`/`retrieval_evidence`/`plan_evidence`) behind it.
 *
 *  This can fail closed several distinct ways, each a *different* HTTP
 *  status the route maps deliberately rather than collapsing to one error
 *  shape (`api.py:2912`'s own except clauses):
 *    409  `AgentClarificationRequired` -- most importantly AT-9's ambiguous-
 *         governed-term refusal (`_check_definition_ambiguity` /
 *         `format_ambiguous_definition_refusal`, semantic_inference.py),
 *         but also a governed tool needing parameters this v1 form never
 *         sends. Also 409 for a disabled datasource (`ensure_datasource_enabled`,
 *         fleet.py) -- same status, different `detail`, so a caller must read
 *         `detail`, not just the status, to tell them apart. See
 *         `classifyAgentAskError` below.
 *    422  `AgentPolicyRejected` / `QueryRejected` -- the deterministic policy
 *         or query layer refused the request or the generated query.
 *    503  `ModelRouteUnavailable` -- no model route could serve the request.
 *    502  anything unhandled -- `"agent analysis execution failed"`.
 */
export async function runAgentAnalysis(
  datasourceId: string,
  body: AgentAnalysisRequest,
  signal?: AbortSignal,
): Promise<AgentAnalysisResponse> {
  if (USE_FIXTURES) return makeFixtureAgentAnalysis(datasourceId, body);
  return postJson<AgentAnalysisResponse>(
    `/v1/datasources/${datasourceId}/agent-analyses`,
    body,
    signal,
  );
}

export interface AgentRunsQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{id}/agent-runs` (`list_agent_runs`, `api.py:2965`)
 *  -- past questions asked against this datasource, newest first. Offset-
 *  paged (the route's own `limit`/`offset` query params), unlike the
 *  cursor-paged catalog/tables routes above. `AgentRunRead` (./types.ts)
 *  carries no `question` text field -- the server never persists the raw
 *  question string on the run row -- so a history row is identified by its
 *  id/status/generation_source/timestamps, not by the question that produced
 *  it; only a run whose answer is still held in this session's own state
 *  (just asked, not yet reloaded from a permalink) has its question visible
 *  client-side. */
export async function fetchAgentRuns(
  datasourceId: string,
  query: AgentRunsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AgentRunRead>> {
  if (USE_FIXTURES) return makeFixtureAgentRuns(datasourceId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<AgentRunRead>>(`/v1/datasources/${datasourceId}/agent-runs?${params}`, signal);
}

/** `GET /v1/agent-runs/{id}` (`get_agent_run`, `api.py:3001`) -- one run's
 *  full detail, for the evidence panel behind a history item or a `run`
 *  URL permalink that outlives this session's own in-memory answer. */
export async function fetchAgentRun(
  agentRunId: string,
  signal?: AbortSignal,
): Promise<AgentRunRead> {
  if (USE_FIXTURES) return makeFixtureAgentRun(agentRunId);
  return get<AgentRunRead>(`/v1/agent-runs/${agentRunId}`, signal);
}

/** `GET /v1/agent-runs/{id}/grounding-receipts` (`get_agent_run_grounding_receipts`,
 *  `api.py:3018`) -- AT-6 replay proof: resolves every grounding-fragment
 *  digest this run recorded back to the actual source content (e.g. the
 *  business-annotation text) the answer was grounded on, with
 *  `digest_verified` confirming the stored content still matches what the
 *  run saw. Powers the "how this was answered" evidence panel for both a
 *  freshly-asked question and a reopened history item. */
export async function fetchAgentRunGroundingReceipts(
  agentRunId: string,
  signal?: AbortSignal,
): Promise<AgentRunGroundingReceiptsRead> {
  if (USE_FIXTURES) return makeFixtureAgentRunGroundingReceipts(agentRunId);
  return get<AgentRunGroundingReceiptsRead>(
    `/v1/agent-runs/${agentRunId}/grounding-receipts`,
    signal,
  );
}

/** AT-9 / row UX-15's error-mapping requirement: `run_agent_analysis` maps
 *  several distinct failures onto only four HTTP statuses (see
 *  `runAgentAnalysis`'s own doc comment), so the screen must read `detail`,
 *  not just `status`, to render each as its own state rather than one
 *  generic failure banner. The ambiguity case additionally carries every
 *  competing definition inline in `detail`
 *  (`format_ambiguous_definition_refusal`, semantic_inference.py) --
 *  `alternatives` below parses those back out so the refusal can render each
 *  definition and its owner as its own item instead of one wall of text.
 *  Parsing is a front-end convenience only: if the format ever changes,
 *  `alternatives` degrades to `[]` and the raw `detail` is still shown. */
export type AgentAskErrorKind =
  | "AMBIGUOUS_DEFINITION"
  | "DATASOURCE_DISABLED"
  | "POLICY_REJECTED"
  | "MODEL_UNAVAILABLE"
  | "CLARIFICATION_NEEDED"
  | "SERVER_ERROR"
  | "UNKNOWN";

export interface AgentAskErrorAlternative {
  businessNodeId: string;
  displayName: string;
  owner: string;
  definition: string;
}

export interface AgentAskError {
  kind: AgentAskErrorKind;
  status: number;
  detail: string;
  /** Only populated for `AMBIGUOUS_DEFINITION`. */
  alternatives: AgentAskErrorAlternative[];
}

const AMBIGUOUS_DEFINITION_RE =
  /^the term '.+' resolves to \d+ equally applicable governed definitions/;
const AMBIGUOUS_ALTERNATIVE_RE = /^([^\]]+)\] '([^']+)' \(owner: ([^)]+)\) -- ([\s\S]+)$/;

function parseAmbiguousAlternatives(detail: string): AgentAskErrorAlternative[] {
  const marker = " [business_node=";
  const firstIdx = detail.indexOf(marker);
  if (firstIdx === -1) return [];
  const segments = detail
    .slice(firstIdx + marker.length)
    .split(marker)
    .filter(Boolean);
  const alternatives: AgentAskErrorAlternative[] = [];
  for (const segment of segments) {
    const m = AMBIGUOUS_ALTERNATIVE_RE.exec(segment);
    if (!m) continue;
    alternatives.push({
      businessNodeId: m[1]!,
      displayName: m[2]!,
      owner: m[3]!,
      definition: m[4]!.trim(),
    });
  }
  return alternatives;
}

export function classifyAgentAskError(error: ApiError): AgentAskError {
  const { status, detail } = error;
  if (status === 409 && detail === "datasource is disabled") {
    return { kind: "DATASOURCE_DISABLED", status, detail, alternatives: [] };
  }
  if (status === 409 && AMBIGUOUS_DEFINITION_RE.test(detail)) {
    return {
      kind: "AMBIGUOUS_DEFINITION",
      status,
      detail,
      alternatives: parseAmbiguousAlternatives(detail),
    };
  }
  if (status === 409) return { kind: "CLARIFICATION_NEEDED", status, detail, alternatives: [] };
  if (status === 422) return { kind: "POLICY_REJECTED", status, detail, alternatives: [] };
  if (status === 503) return { kind: "MODEL_UNAVAILABLE", status, detail, alternatives: [] };
  if (status === 502) return { kind: "SERVER_ERROR", status, detail, alternatives: [] };
  return { kind: "UNKNOWN", status, detail, alternatives: [] };
}
/* ---------------------------------------------------------------------------
   UX-16: Operations. Composed from four org-wide, already-merged
   `operational_api.py` routes -- fleet-summary, analysis-runs, outbox-events
   and its requeue action -- plus, as an optional per-datasource drill-down,
   `ingestion_api.py`'s metadata-ingestion-batches. There is no single
   endpoint that aggregates ingestion-batch/Temporal-workflow status across
   every datasource in an org; see `OperationsScreen.tsx`'s own module
   comment for why this screen does not fake one.
--------------------------------------------------------------------------- */

/** `GET /v1/organizations/{organization_id}/fleet-summary` (`operational_api.py::fleet_summary`)
 *  -- the dashboard tiles at the top of the Operations screen. Org-wide, no
 *  datasource picker needed. */
export async function fetchFleetSummary(
  organizationId: string,
  signal?: AbortSignal,
): Promise<FleetSummaryRead> {
  if (USE_FIXTURES) return makeFixtureFleetSummary(organizationId);
  return get<FleetSummaryRead>(`/v1/organizations/${organizationId}/fleet-summary`, signal);
}

export interface AnalysisRunsQuery {
  organizationId: string;
  runStatus?: string | null;
  datasourceId?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/analysis-runs`
 *  (`operational_api.py::list_organization_analysis_runs`) -- the screen's
 *  primary list, filterable by run status and/or datasource. */
export async function fetchAnalysisRuns(
  query: AnalysisRunsQuery,
  signal?: AbortSignal,
): Promise<PageOf<AnalysisRunRead>> {
  if (USE_FIXTURES) return makeFixtureAnalysisRuns(query);
  const params = new URLSearchParams();
  if (query.runStatus) params.set("run_status", query.runStatus);
  if (query.datasourceId) params.set("datasource_id", query.datasourceId);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<AnalysisRunRead>>(
    `/v1/organizations/${query.organizationId}/analysis-runs?${params}`,
    signal,
  );
}

export interface OutboxEventsQuery {
  organizationId: string;
  status?: string | null;
  eventType?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/outbox-events`
 *  (`operational_api.py::list_outbox_events`) -- the event-backlog / dead-
 *  letter panel beneath the analysis-runs list. */
export async function fetchOutboxEvents(
  query: OutboxEventsQuery,
  signal?: AbortSignal,
): Promise<PageOf<OutboxEventRead>> {
  if (USE_FIXTURES) return makeFixtureOutboxEvents(query);
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.eventType) params.set("event_type", query.eventType);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<OutboxEventRead>>(
    `/v1/organizations/${query.organizationId}/outbox-events?${params}`,
    signal,
  );
}

/** `POST /v1/outbox-events/{event_id}/requeue` (`operational_api.py::requeue_outbox_event`)
 *  -- moves one DEAD_LETTER event back to PENDING with a reset attempt count.
 *  The route takes no request body; `{}` matches this file's own convention
 *  (see `submitStudioChangeSet`) of never sending an optional-looking empty
 *  POST without an explicit body. */
export async function requeueOutboxEvent(
  eventId: string,
  signal?: AbortSignal,
): Promise<OutboxEventRead> {
  if (USE_FIXTURES) return makeFixtureRequeueOutboxEvent(eventId);
  return postJson<OutboxEventRead>(`/v1/outbox-events/${eventId}/requeue`, {}, signal);
}

export interface IngestionBatchesQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{datasource_id}/metadata-ingestion-batches`
 *  (`ingestion_api.py::list_metadata_ingestion_batches`) -- per-datasource
 *  only, no org-wide equivalent exists. Used by this screen's secondary
 *  drill-down panel, one datasource at a time, never fanned out across the
 *  fleet. */
export async function fetchIngestionBatches(
  datasourceId: string,
  opts: IngestionBatchesQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<MetadataIngestionBatchRead>> {
  if (USE_FIXTURES) return makeFixtureIngestionBatches(datasourceId, opts);
  const params = new URLSearchParams();
  params.set("limit", String(opts.limit ?? 100));
  params.set("offset", String(opts.offset ?? 0));
  return get<PageOf<MetadataIngestionBatchRead>>(
    `/v1/datasources/${datasourceId}/metadata-ingestion-batches?${params}`,
    signal,
  );
}
/* ---------------------------------------------------------------------------
   Quality — UX-15/UX-16, `QualityScreen`.

   Both real, already-merged routes (`quality_api.py`), gated by `USE_FIXTURES`
   the same way as every call above. `list_quality_incidents` and
   `quality_summary` are scoped per datasource, matching UX-20's
   `fetchLineageImpact` above rather than `fetchCatalogRows`'s organization
   scoping.
--------------------------------------------------------------------------- */

export interface QualityIncidentsQuery {
  /** `null`/omitted means "every status" — the endpoint's own default when
   *  `status` is left off the query string entirely (unlike
   *  `fetchReviewQueue`'s explicit-empty-string convention, this endpoint has
   *  no server-side default status to override, so simply omitting the param
   *  is the correct "all statuses" request here). */
  status?: string | null;
  severity?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{id}/quality-summary` (`quality_api.py::quality_summary`)
 *  — the dashboard tiles: observed/table counts, open/critical incident
 *  counts, the datasource's rolled-up average quality score, and the
 *  metadata-scan freshness state. */
export async function fetchQualitySummary(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<DataQualitySummaryRead> {
  if (USE_FIXTURES) return makeFixtureQualitySummary(datasourceId);
  return get<DataQualitySummaryRead>(
    `/v1/datasources/${datasourceId}/quality-summary`,
    signal,
  );
}

/** `GET /v1/datasources/{id}/quality-incidents` (`quality_api.py::list_quality_incidents`)
 *  — the primary incidents list, filterable by `status`/`severity`. */
export async function fetchQualityIncidents(
  datasourceId: string,
  query: QualityIncidentsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<DataQualityIncidentRead>> {
  if (USE_FIXTURES) return makeFixtureQualityIncidents(datasourceId, query);
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.severity) params.set("severity", query.severity);
  params.set("limit", String(query.limit ?? 200));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<DataQualityIncidentRead>>(
    `/v1/datasources/${datasourceId}/quality-incidents?${params}`,
    signal,
  );
}

/** `POST /v1/quality-incidents/{id}/transition` (`quality_api.py::transition_quality_incident`)
 *  — acknowledge or resolve an open incident. The endpoint requires a
 *  non-empty (>=3 char) `reason` on both transitions and refuses (409) to
 *  transition an incident that is already RESOLVED. */
export async function transitionQualityIncident(
  incidentId: string,
  body: DataQualityIncidentTransition,
  signal?: AbortSignal,
): Promise<DataQualityIncidentRead> {
  if (USE_FIXTURES) return makeFixtureTransitionQualityIncident(incidentId, body);
  return postJson<DataQualityIncidentRead>(
    `/v1/quality-incidents/${incidentId}/transition`,
    body,
    signal,
  );
}
/* ---------------------------------------------------------------------------
   UX-16: Business meaning — datasource-scoped browse of approved business
   annotations, plus an org-wide taxonomy view (business-map).

   Both real, already-merged routes (`semantic_intelligence_api.py`):
     - `list_business_annotations` joins `MetadataBusinessAnnotation` to its
       current (AT-6, append-only-versioned) `MetadataBusinessAnnotationVersion`
       plus table/schema/domain/entity — the per-datasource browse this
       screen's list is built on.
     - `get_table_business_annotation` resolves the same shape by `table_id`
       alone, decoupled from any particular loaded page — the evidence pane's
       actual permalink target, the same role `fetchAssetEvidence` plays for
       `EvidencePane`.
     - `get_business_map` is the org-wide domain/entity/table graph, a real
       traversal (cross-domain edges come from actual `MetadataConstraint`
       foreign keys, not invented) — the "supporting view" tab.
--------------------------------------------------------------------------- */

export interface BusinessAnnotationsQuery {
  datasourceId: string;
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{id}/business-annotations`. Declares
 *  `response_model=Page` un-parameterized (see `ui-types.ts`'s `PageOf`
 *  banner) -- offset/limit paged like the route itself (no cursor, and no
 *  server-side free-text filter: the route takes only `limit`/`offset`, so
 *  `BusinessMeaningScreen` filters its already-loaded page client-side). */
export async function fetchBusinessAnnotations(
  query: BusinessAnnotationsQuery,
  signal?: AbortSignal,
): Promise<PageOf<MetadataBusinessAnnotationRead>> {
  if (USE_FIXTURES) return makeFixtureBusinessAnnotations(query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<MetadataBusinessAnnotationRead>>(
    `/v1/datasources/${query.datasourceId}/business-annotations?${params}`,
    signal,
  );
}

/** `GET /v1/metadata/tables/{table_id}/business-annotation` — resolves by
 *  table id alone, exactly like `fetchAssetEvidence` does for `EvidencePane`:
 *  a durable permalink target that does not depend on the caller's current
 *  datasource filter or loaded page happening to contain this table. 404s
 *  when the table has no *approved* annotation (`MetadataBusinessAnnotation`
 *  content is append-only versioned per AT-6; this always resolves the
 *  current version). */
export async function fetchTableBusinessAnnotation(
  tableId: string,
  signal?: AbortSignal,
): Promise<MetadataBusinessAnnotationRead> {
  if (USE_FIXTURES) return makeFixtureTableBusinessAnnotation(tableId);
  return get<MetadataBusinessAnnotationRead>(
    `/v1/metadata/tables/${tableId}/business-annotation`,
    signal,
  );
}

export interface BusinessMapQuery {
  organizationId: string;
  limit?: number;
}

/** `GET /v1/organizations/{id}/business-map` — the secondary, org-wide tab:
 *  every approved domain/entity/table node plus real cross-domain foreign-key
 *  edges (`MetadataConstraint`), not a per-datasource slice. */
export async function fetchBusinessMap(
  query: BusinessMapQuery,
  signal?: AbortSignal,
): Promise<BusinessMapRead> {
  if (USE_FIXTURES) return makeFixtureBusinessMap(query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 500));
  return get<BusinessMapRead>(
    `/v1/organizations/${query.organizationId}/business-map?${params}`,
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
   Sources — UX-15/UX-16 follow-on (nav id `sources`). Reuses
   `fetchOrgDatasources` above for the fleet list (see that function's own
   comment for the `DataSourceRead`/`DataSourceSummaryRead` shape note this
   screen also relies on -- `credential_reference` is typed but not actually
   present on this endpoint's wire response; this screen never reads it). The
   only new call this screen needs is per-source health.
--------------------------------------------------------------------------- */

import type { ConnectorHealthScoreRead } from "./types";
import { makeFixtureDatasourceHealth } from "./fixtures";

/** `GET /v1/datasources/{datasource_id}/health` (`operational_api.py::get_datasource_health`,
 *  `:266`) — a composite, explainable 0-100 score over the connector's recent
 *  run history (`aida.connector_health.compute_connector_health`): run success
 *  rate, staleness, failure streak, profiling coverage and datasource
 *  enablement, each its own weighted factor with a human-readable `reason` and
 *  `evidence`, plus any `blockers` (e.g. `NO_RUN_HISTORY`, `DATASOURCE_DISABLED`,
 *  `REPEATED_FAILURES`) explaining why the status is what it is. */
export async function fetchDatasourceHealth(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<ConnectorHealthScoreRead> {
  if (USE_FIXTURES) return makeFixtureDatasourceHealth(datasourceId);
  return get<ConnectorHealthScoreRead>(`/v1/datasources/${datasourceId}/health`, signal);
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
