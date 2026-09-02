import type {
  AgentAnalysisRequest,
  AgentAnalysisResponse,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  AiDecisionRead,
  AssetEvidenceRead,
  DataSourceRead,
  GovernanceDecisionRequest,
  GovernanceReviewRead,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  MeRead,
  ReviewQueueRead,
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioDiffRead,
  StudioImpactPreview,
  UnifiedLineageImpactRead,
} from "./types";
import type {
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
  makeFixtureCatalog,
  makeFixtureDecideReview,
  makeFixtureEvidence,
  makeFixtureLineageImpact,
  makeFixtureMarketplaceAccessRequest,
  makeFixtureMarketplaceProducts,
  makeFixtureMe,
  makeFixtureOrgDatasources,
  makeFixtureRefusals,
  makeFixtureReviewQueue,
  makeFixtureRunDecisions,
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
