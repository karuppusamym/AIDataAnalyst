import type {
  AgentInboxRead,
  AgentAnalysisRequest,
  AgentAnalysisResponse,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  AiDecisionRead,
  AnalysisRunRead,
  AssetDescriptionDraftGenerateResponse,
  AssetDescriptionDraftListResponse,
  AssetDescriptionDraftRead,
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
  OrganizationRead,
  OutboxEventRead,
  Page,
  ProjectRead,
  ReviewQueueRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
  StudioChangeItemRead,
  StudioChangeSetRead,
  StudioDiffRead,
  StudioImpactPreview,
  UnifiedLineageImpactRead,
  UnifiedLineageGraphRead,
  SourceBindingCreate,
  SourceBindingRead,
  WorkspaceCreate,
  WorkspaceRead,
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
  makeFixtureAgentInbox,
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
  makeFixtureLineageGraph,
  makeFixtureMarketplaceAccessRequest,
  makeFixtureMarketplaceProducts,
  makeFixtureMe,
  makeFixtureOrgDatasources,
  makeFixtureOrgWorkspaces,
  makeFixtureOrganizations,
  makeFixtureOrgProjects,
  makeFixtureWorkspaceSourceBindings,
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

/**
 * Development-mode identity headers.
 *
 * The backend's `get_security_context` (security.py) fail-closes any
 * request with no `X-Principal-Id` when `identity_provider == "development"`
 * -- the same INV-4 fail-closed default used everywhere else in this app.
 * The legacy portal (ui/scripts/api.js -> baseHeaders()) has always sent
 * these on every request; this client never did, which is why every
 * live-API screen here 401s under the default `VITE_USE_FIXTURES=0`
 * compose config even though the backend and its data are fine -- the
 * legacy portal proves both work against the identical API.
 *
 * Mirrors the legacy defaults so both UIs exercise the same dev principal.
 * Override with VITE_DEV_PRINCIPAL_ID / VITE_DEV_ROLES if needed.
 *
 * Not gated on the Vite build mode on purpose: `ui-next`'s default compose
 * service serves a *production* build (`import.meta.env.DEV` is false
 * there) against the same `identity_provider=development` backend, so a
 * dev-build-only gate would silently skip these headers on exactly the
 * deployment that needs them. The real safety net is server-side: under
 * `identity_provider=oidc` (security.py's other branch) the backend never
 * consults `X-Principal-Id` at all -- it requires `Authorization: Bearer`
 * instead -- so sending this in an OIDC environment is inert, not a leak.
 */
const DEV_PRINCIPAL_ID = import.meta.env.VITE_DEV_PRINCIPAL_ID || "local-ui-admin";
const DEV_ROLES =
  import.meta.env.VITE_DEV_ROLES ||
  "PlatformAdmin,OrganizationAdmin,ProjectAdmin,MetadataAdmin,MetadataIngestor,DataAdmin,SemanticAdmin,DataSteward,ToolDeveloper,ToolConsumer,AgentDeveloper,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer";

/* A few routes (observability/SLO, notification-rules, tool-plans) take no
 * `{organization_id}` path segment and resolve the org purely from this
 * header server-side -- see `org.tsx`'s `getCurrentOrgId()` for why this is
 * read from outside React rather than threaded through every call site. */
import { getCurrentOrgId } from "./org";

function identityHeaders(): Record<string, string> {
  if (USE_FIXTURES) return {};
  return { "X-Principal-Id": DEV_PRINCIPAL_ID, "X-Roles": DEV_ROLES, "X-Organization-Id": getCurrentOrgId() };
}

function serverErrorDetail(value: unknown, fallback: string): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(item => {
    if (item && typeof item === "object" && "msg" in item) {
      const loc = "loc" in item && Array.isArray(item.loc) ? item.loc.join(".") : "Request";
      return `${loc}: ${String(item.msg)}`;
    }
    return "Request validation failed";
  }).join("; ");
  return fallback;
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    signal,
    headers: { Accept: "application/json", ...identityHeaders() },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = serverErrorDetail(body.detail, detail);
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Downloads use the same identity and authorization boundary as screen reads. */
export async function exportAssetEvidence(tableId: string): Promise<void> {
  const data = USE_FIXTURES
    ? await fetchAssetEvidence(tableId)
    : await get<AssetEvidenceRead>(`/v1/metadata/tables/${encodeURIComponent(tableId)}/evidence/export`);
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `table-${tableId}-evidence.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Same contract as `get`, for the write endpoints UX-15's screens call
 *  (governance decisions, marketplace access requests, Studio submit). No
 *  request body is optional here on purpose: every write this app makes
 *  carries one, even if it is `{}` -- an empty POST invites a caller to
 *  forget the body a route actually requires. */
export async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    signal,
    headers: { Accept: "application/json", "Content-Type": "application/json", ...identityHeaders() },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const errBody = (await res.json()) as { detail?: string };
      if (errBody.detail) detail = serverErrorDetail(errBody.detail, detail);
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Same contract as `postJson`, for the few endpoints that mutate an existing
 *  resource with PUT (e.g. advancing an AI remediation's status). */
export async function putJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "PUT",
    signal,
    headers: { Accept: "application/json", "Content-Type": "application/json", ...identityHeaders() },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const errBody = (await res.json()) as { detail?: string };
      if (errBody.detail) detail = serverErrorDetail(errBody.detail, detail);
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

/**
 * `GET /v1/organizations` (api.py) — the tenant list the shell's organization
 * picker needs. Fixture mode returns the single development organization every
 * screen historically hard-coded, so pure-frontend iteration is unchanged;
 * live mode returns the real (e.g. seeded) organizations so one can be chosen.
 */
export async function fetchOrganizations(signal?: AbortSignal): Promise<OrganizationRead[]> {
  if (USE_FIXTURES) return makeFixtureOrganizations();
  const page = await get<PageOf<OrganizationRead>>("/v1/organizations?limit=200", signal);
  return page.items;
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

/** Access-axis workspaces for an organization (ADR-0018). A workspace does
 * not own projects; it reaches project-owned sources through bindings. */
export async function fetchOrgWorkspaces(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<WorkspaceRead>> {
  if (USE_FIXTURES) return makeFixtureOrgWorkspaces(organizationId);
  return get<PageOf<WorkspaceRead>>(
    `/v1/organizations/${organizationId}/workspaces?limit=200`,
    signal,
  );
}

/** Grants connecting the selected workspace to one or more datasources. */
export async function fetchWorkspaceSourceBindings(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PageOf<SourceBindingRead>> {
  if (USE_FIXTURES) return makeFixtureWorkspaceSourceBindings(workspaceId);
  return get<PageOf<SourceBindingRead>>(
    `/v1/workspaces/${workspaceId}/source-bindings`,
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

/** Complete, server-bounded lineage graph for a datasource. Edges retain
 * their source, confidence and approval status so inferred relationships are
 * visually distinguishable from declared foreign keys. */
export async function fetchLineageGraph(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<UnifiedLineageGraphRead> {
  if (USE_FIXTURES) return makeFixtureLineageGraph(datasourceId);
  return get<UnifiedLineageGraphRead>(
    `/v1/datasources/${datasourceId}/unified-lineage/graph?node_limit=200&edge_limit=500`,
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
  | "MODEL_THROTTLED"
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
  if (status === 429) return { kind: "MODEL_THROTTLED", status, detail, alternatives: [] };
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
  makeFixtureRelationshipCandidates,
  makeFixtureRelationshipCandidateReviewQueue,
} from "./fixtures";

export interface RelationshipCandidateReviewQueueQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/datasources/{datasourceId}/relationship-candidates` (the raw list
 *  behind the review queue, `list_relationship_candidates`). Unlike the
 *  review-queue read model this can return candidates in ANY state via
 *  `candidate_status`, so a reviewer can see what was already approved or
 *  rejected — the decision history the PENDING-only queue drops. Read-only. */
export async function fetchRelationshipCandidates(
  datasourceId: string,
  opts: { status?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<RelationshipCandidateRead>> {
  if (USE_FIXTURES) return makeFixtureRelationshipCandidates(datasourceId, opts.status);
  const params = new URLSearchParams();
  if (opts.status && opts.status !== "ALL") params.set("candidate_status", opts.status);
  params.set("limit", String(opts.limit ?? 200));
  return get<PageOf<RelationshipCandidateRead>>(
    `/v1/datasources/${datasourceId}/relationship-candidates?${params}`,
    signal,
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

/* ---------------------------------------------------------------------------
   AI governance (module 15 / CP-7,CP-8) — the AI registry, trust scoring and
   remediation loop. The backend (ai_registry_api.py) has carried these since
   the AI-trust slice landed; ui-next had the types but no screen. Same
   USE_FIXTURES gate and self-contained-block convention as the relationships
   block above.
--------------------------------------------------------------------------- */

import type {
  AiAssessmentTemplateRead,
  AiAssetVersionRead,
  AiRemediationRead,
  AiRemediationUpdate,
  AiTrustScoreRead,
} from "./types";
import {
  makeFixtureAiAssessmentTemplates,
  makeFixtureAiAssets,
  makeFixtureAiRemediations,
  makeFixtureAiTrust,
  makeFixtureUpdateAiRemediation,
} from "./fixtures";

/** `GET /v1/organizations/{org}/ai-assets` — one row per AI asset at its
 *  latest version (name, provider, risk tier, and the version id the trust and
 *  remediation calls below are scoped by). */
export async function fetchAiAssets(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<AiAssetVersionRead>> {
  if (USE_FIXTURES) return makeFixtureAiAssets(organizationId);
  return get<PageOf<AiAssetVersionRead>>(
    `/v1/organizations/${organizationId}/ai-assets?limit=200`,
    signal,
  );
}

/** `GET /v1/ai-asset-versions/{id}/trust` — the deterministic trust score,
 *  grade, per-factor breakdown and blocking findings for one asset version. */
export async function fetchAiAssetTrust(
  versionId: string,
  signal?: AbortSignal,
): Promise<AiTrustScoreRead> {
  if (USE_FIXTURES) return makeFixtureAiTrust(versionId);
  return get<AiTrustScoreRead>(`/v1/ai-asset-versions/${versionId}/trust`, signal);
}

/** `GET /v1/ai-asset-versions/{id}/remediations` — the findings-to-remediation
 *  log for one asset version. */
export async function fetchAiRemediations(
  versionId: string,
  signal?: AbortSignal,
): Promise<PageOf<AiRemediationRead>> {
  if (USE_FIXTURES) return makeFixtureAiRemediations(versionId);
  return get<PageOf<AiRemediationRead>>(
    `/v1/ai-asset-versions/${versionId}/remediations?limit=200`,
    signal,
  );
}

/** `PUT /v1/ai-remediations/{id}` — advance a remediation's status. Moving one
 *  to ACCEPTED_RISK is enforced server-side to an independent risk role. */
export async function updateAiRemediation(
  remediationId: string,
  body: AiRemediationUpdate,
  signal?: AbortSignal,
): Promise<AiRemediationRead> {
  if (USE_FIXTURES) return makeFixtureUpdateAiRemediation(remediationId, body);
  return putJson<AiRemediationRead>(`/v1/ai-remediations/${remediationId}`, body, signal);
}

/** `GET /v1/ai-assessment-templates` — the built-in control checklists
 *  (EU AI Act, NIST AI RMF, enterprise use-case) an assessment is seeded from. */
export async function fetchAiAssessmentTemplates(
  signal?: AbortSignal,
): Promise<AiAssessmentTemplateRead[]> {
  if (USE_FIXTURES) return makeFixtureAiAssessmentTemplates();
  return get<AiAssessmentTemplateRead[]>("/v1/ai-assessment-templates", signal);
}

/* ---------------------------------------------------------------------------
   Context products — the legacy portal's `context-products` view
   (`ui/scripts/features/context-lineage-control-plane.js`), ported onto the
   same real, already-merged `context_product_api.py` routes that view calls.
   Added here as a clearly-delimited block, same convention as the AI
   registry and Relationships blocks above.

   Endpoints used (all `src/aida/context_product_api.py` unless noted):
     - POST   /v1/projects/{project_id}/context-products                  create_context_product          :308
     - GET    /v1/projects/{project_id}/context-products                  list_context_products            :381
     - POST   /v1/context-product-versions/{id}/submit                    submit_context_product_version   :826
     - POST   /v1/context-product-versions/{id}/deprecate                 request_context_product_deprecation :887
     - GET    /v1/context-product-versions/{id}/compile                   compile_context_product_version
       (`src/aida/context_compiler_api.py:208`)

   Deliberately not ported: `GET /context-products/{id}/versions` (:445,
   version history — the legacy screen never showed it, only the latest
   version via `ContextProductRead.latest_version`), the AT-7(b) consumer-
   binding routes (:965/:1039/:1081 — a staged-rollout registry the legacy
   UI never exposed either), `PUT /context-product-versions/{id}` (:796,
   in-place version edit — legacy only ever created new drafts), and
   `/compile/download` (`context_compiler_api.py:258` — legacy's compiler
   panel only ever called the plain `/compile` GET, never the download
   variant).
--------------------------------------------------------------------------- */

import type { ContextCompilationRead, ContextProductCreate, ContextProductRead } from "./types";
import {
  makeFixtureCompileContextProductVersion,
  makeFixtureContextProducts,
  makeFixtureCreateContextProduct,
  makeFixtureDeprecateContextProductVersion,
  makeFixtureSubmitContextProductVersion,
} from "./fixtures";

export interface ContextProductQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{project_id}/context-products` (`list_context_products`,
 *  `context_product_api.py:381`) — one row per product at its latest
 *  version, exactly what `ContextProductRead.latest_version` carries;
 *  matches `loadContextProducts()`'s call in the legacy screen. */
export async function fetchContextProducts(
  projectId: string,
  query: ContextProductQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ContextProductRead>> {
  if (USE_FIXTURES) return makeFixtureContextProducts(projectId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 200));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<ContextProductRead>>(
    `/v1/projects/${projectId}/context-products?${params}`,
    signal,
  );
}

/** `POST /v1/projects/{project_id}/context-products` (`create_context_product`,
 *  `context_product_api.py:308`) — creates the product and its version-1
 *  DRAFT in one call, matching `createContextProduct()` in the legacy
 *  screen. Every referenced table/semantic/glossary/tool version id is
 *  server-validated against that same project's approved, PUBLISHED
 *  versions (`validate_context_product_references`); an unresolved id comes
 *  back as this endpoint's own 4xx detail string. */
export async function createContextProduct(
  projectId: string,
  body: ContextProductCreate,
  signal?: AbortSignal,
): Promise<ContextProductRead> {
  if (USE_FIXTURES) return makeFixtureCreateContextProduct(projectId, body);
  return postJson<ContextProductRead>(`/v1/projects/${projectId}/context-products`, body, signal);
}

/** `POST /v1/context-product-versions/{id}/submit` (`submit_context_product_version`,
 *  `context_product_api.py:826`) — moves a DRAFT version to REVIEW_REQUIRED
 *  and opens the same `GovernanceReview` `ReviewQueueScreen` reads; matches
 *  the legacy screen's `data-context-submit` action. */
export async function submitContextProductVersion(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) return makeFixtureSubmitContextProductVersion(versionId);
  return postJson<GovernanceReviewRead>(`/v1/context-product-versions/${versionId}/submit`, {}, signal);
}

/** `POST /v1/context-product-versions/{id}/deprecate` (`request_context_product_deprecation`,
 *  `context_product_api.py:887`) — requests retirement review for a
 *  PUBLISHED (or SUPPORTED) version; matches the legacy screen's
 *  `data-context-deprecate` action. */
export async function requestContextProductDeprecation(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) return makeFixtureDeprecateContextProductVersion(versionId);
  return postJson<GovernanceReviewRead>(`/v1/context-product-versions/${versionId}/deprecate`, {}, signal);
}

/** `GET /v1/context-product-versions/{id}/compile` (`compile_context_product_version`,
 *  `context_compiler_api.py:208`) — deterministic compilation of one
 *  immutable version to a target format; matches the legacy screen's
 *  `compileVersion()` (`data-context-compile`). Repeating this call against
 *  the same version and target reproduces the same `artifact_hash`. */
export async function compileContextProductVersion(
  versionId: string,
  target: string,
  signal?: AbortSignal,
): Promise<ContextCompilationRead> {
  if (USE_FIXTURES) return makeFixtureCompileContextProductVersion(versionId, target);
  const params = new URLSearchParams({ target });
  return get<ContextCompilationRead>(
    `/v1/context-product-versions/${versionId}/compile?${params}`,
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Administration -- nav id `administration`, the tenant/onboarding wizard
   ported from the legacy portal's `administration-view` (`ui/app.js`'s
   `#organization-form`/`#lob-form`/`#project-form`/`#datasource-form`
   handlers). Every call below hits a real, already-merged route the legacy
   portal itself posts to -- the deliberate four-step hierarchy the backend
   enforces (organization -> line of business -> project -> datasource), not
   an invented "setup" API. `fetchOrganizations`, `fetchOrgProjects` and
   `fetchOrgDatasources` above already cover this screen's organization,
   project and datasource reads; `fetchOrgLinesOfBusiness` below is the one
   read nothing existing exposed yet.
--------------------------------------------------------------------------- */

import type {
  DataSourceCreate,
  LineOfBusinessCreate,
  LineOfBusinessRead,
  OrganizationCreate,
  ProjectCreate,
} from "./types";
import {
  makeFixtureCreateLineOfBusiness,
  makeFixtureCreateOrganization,
  makeFixtureCreateProject,
  makeFixtureCreateWorkspace,
  makeFixtureOrgLinesOfBusiness,
  makeFixtureRequestSourceBinding,
  makeFixtureRegisterDatasource,
} from "./fixtures";

/** `POST /v1/organizations` (`create_organization`, `api.py:584`) -- the
 *  platform-admin-gated tenant creation the legacy portal's
 *  `#organization-form` posts to. Requires the `PlatformAdmin` role. */
export async function createOrganization(
  body: OrganizationCreate,
  signal?: AbortSignal,
): Promise<OrganizationRead> {
  if (USE_FIXTURES) return makeFixtureCreateOrganization(body);
  return postJson<OrganizationRead>("/v1/organizations", body, signal);
}

/** Create an access workspace. Projects remain a separate technical axis;
 * sources are attached to this workspace with `requestSourceBinding`. */
export async function createWorkspace(
  organizationId: string,
  body: WorkspaceCreate,
  signal?: AbortSignal,
): Promise<WorkspaceRead> {
  if (USE_FIXTURES) return makeFixtureCreateWorkspace(organizationId, body);
  return postJson<WorkspaceRead>(
    `/v1/organizations/${organizationId}/workspaces`,
    body,
    signal,
  );
}

/** Request maker-checker-governed access from a workspace to a source. */
export async function requestSourceBinding(
  workspaceId: string,
  body: SourceBindingCreate,
  signal?: AbortSignal,
): Promise<SourceBindingRead> {
  if (USE_FIXTURES) return makeFixtureRequestSourceBinding(workspaceId, body);
  return postJson<SourceBindingRead>(
    `/v1/workspaces/${workspaceId}/source-bindings`,
    body,
    signal,
  );
}

/** `GET /v1/organizations/{organization_id}/lines-of-business`
 *  (`list_lines_of_business`, `api.py:463`) -- the one hierarchy read
 *  `fetchOrgProjects`/`fetchOrgDatasources` above don't already cover; feeds
 *  both the "Add project" line-of-business picker and the scope-summary
 *  tree in `AdministrationScreen`. */
export async function fetchOrgLinesOfBusiness(
  organizationId: string,
  signal?: AbortSignal,
): Promise<PageOf<LineOfBusinessRead>> {
  if (USE_FIXTURES) return makeFixtureOrgLinesOfBusiness(organizationId);
  return get<PageOf<LineOfBusinessRead>>(
    `/v1/organizations/${organizationId}/lines-of-business?limit=500`,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/lines-of-business`
 *  (`create_line_of_business`, `api.py:677`). Requires `PlatformAdmin` or
 *  `OrganizationAdmin`. */
export async function createLineOfBusiness(
  organizationId: string,
  body: LineOfBusinessCreate,
  signal?: AbortSignal,
): Promise<LineOfBusinessRead> {
  if (USE_FIXTURES) return makeFixtureCreateLineOfBusiness(organizationId, body);
  return postJson<LineOfBusinessRead>(
    `/v1/organizations/${organizationId}/lines-of-business`,
    body,
    signal,
  );
}

/** `POST /v1/lines-of-business/{lob_id}/projects` (`create_project`,
 *  `api.py:901`). `body.data_domain_id` is left unset on purpose --
 *  `create_project`'s own `resolve_domain` falls back to the line of
 *  business's default domain when it is omitted (`api.py:922`), and this
 *  screen has no data-domain picker of its own (a stated scope cut, see
 *  `AdministrationScreen`'s file-top comment). Requires `PlatformAdmin` or
 *  `ProjectAdmin`. */
export async function createProject(
  lobId: string,
  body: ProjectCreate,
  signal?: AbortSignal,
): Promise<ProjectRead> {
  if (USE_FIXTURES) return makeFixtureCreateProject(lobId, body);
  return postJson<ProjectRead>(`/v1/lines-of-business/${lobId}/projects`, body, signal);
}

/** `POST /v1/projects/{project_id}/datasources` (`create_datasource`,
 *  `api.py:1021`) -- the same registration path `SourcesScreen`'s fleet is
 *  read back from (via `fetchOrgDatasources`), scoped to one project.
 *  `credential_reference` must reference the configured secret provider
 *  (`_validate_datasource_create`, `api.py:960`); a raw connection string
 *  comes back as a 422, same as the legacy portal. Requires `PlatformAdmin`
 *  or `DataAdmin`. No post-registration connectivity test is fired here
 *  (`POST /v1/datasources/{id}/test`, `api.py:1299`, is the legacy portal's
 *  own separate follow-up call, `ui/app.js:1683`) -- a stated scope cut, not
 *  a silently dropped step. */
export async function registerDatasource(
  projectId: string,
  body: DataSourceCreate,
  signal?: AbortSignal,
): Promise<DataSourceRead> {
  if (USE_FIXTURES) return makeFixtureRegisterDatasource(projectId, body);
  return postJson<DataSourceRead>(`/v1/projects/${projectId}/datasources`, body, signal);
}

/* ---------------------------------------------------------------------------
   Tool registry -- nav id `tools`, `ToolRegistryScreen`'s own routes. See
   that screen's file-top comment for the full endpoint list and what was
   deliberately left out of scope (the multi-table blueprint helper and the
   certification-cases/certification-runs sub-flow -- legacy's `tools-view`
   never calls either). Datasource options for the create panel reuse the
   already-existing `fetchOrgDatasources` above, filtered client-side by
   `project_id` -- exactly what the legacy screen's own
   `populateProjectSources()` (`ui/scripts/core.js`) does against its
   org-wide `state.sources`; there is no project-scoped datasource-list
   endpoint to call instead. */

import type { GovernedToolVersionCreate, GovernedToolVersionRead, ToolExecutionRequest, ToolExecutionResponse } from "./types";
import {
  makeFixtureCreateToolVersion,
  makeFixtureExecuteToolVersion,
  makeFixtureRequestToolDeprecation,
  makeFixtureSubmitToolForReview,
  makeFixtureTools,
} from "./fixtures";

export interface ToolQuery {
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{project_id}/tools` (`list_tools`, `tool_api.py:609`)
 *  -- usage-ranked, optionally filtered by `status` (`DRAFT` /
 *  `REVIEW_REQUIRED` / `PUBLISHED` / `DEPRECATED`); matches the legacy
 *  screen's own `loadTools()`. */
export async function fetchTools(
  projectId: string,
  query: ToolQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<GovernedToolVersionRead>> {
  if (USE_FIXTURES) return makeFixtureTools(projectId, query);
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  params.set("limit", String(query.limit ?? 200));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<GovernedToolVersionRead>>(`/v1/projects/${projectId}/tools?${params}`, signal);
}

/** `POST /v1/projects/{project_id}/tools` (`create_tool_version`,
 *  `tool_api.py:348`) -- validates the SQL template server-side (guarded
 *  table access, placeholders matching declared parameters exactly) and
 *  persists a new `DRAFT` version; matches the legacy screen's
 *  `#tool-author-form` submit. Reusing an existing `slug` within this
 *  project attaches the draft to that tool as its next version instead of
 *  creating a new one (`_persist_tool_version_draft`, `tool_api.py:201`) --
 *  what "New version" in this screen relies on. */
export async function createToolVersion(
  projectId: string,
  body: GovernedToolVersionCreate,
  signal?: AbortSignal,
): Promise<GovernedToolVersionRead> {
  if (USE_FIXTURES) return makeFixtureCreateToolVersion(projectId, body);
  return postJson<GovernedToolVersionRead>(`/v1/projects/${projectId}/tools`, body, signal);
}

/** `POST /v1/tool-versions/{version_id}/submit` (`submit_tool_for_review`,
 *  `tool_api.py:692`) -- moves a `DRAFT` version into the same
 *  `GovernanceReview` queue `ReviewQueueScreen` reads; matches the legacy
 *  screen's `data-submit-tool` action. */
export async function submitToolForReview(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) return makeFixtureSubmitToolForReview(versionId);
  return postJson<GovernanceReviewRead>(`/v1/tool-versions/${versionId}/submit`, {}, signal);
}

/** `POST /v1/tool-versions/{version_id}/deprecation-submit`
 *  (`submit_tool_deprecation`, `tool_api.py:756`) -- requests retirement
 *  review for a `PUBLISHED` version, recording its computed blast radius as
 *  audit evidence before the review is even decided; matches the legacy
 *  screen's `data-deprecate-tool` action. */
export async function requestToolDeprecation(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) return makeFixtureRequestToolDeprecation(versionId);
  return postJson<GovernanceReviewRead>(`/v1/tool-versions/${versionId}/deprecation-submit`, {}, signal);
}

/** `POST /v1/tool-versions/{version_id}/execute` (`execute_tool`,
 *  `tool_api.py:881`, `response_model=ToolExecutionResponse`) -- runs a
 *  `PUBLISHED` version's SQL template through the same governed query
 *  gateway `AskScreen`'s freeform path uses, bound to the caller-supplied
 *  parameters; matches the legacy screen's `executeSelectedTool()`. A
 *  non-null `quality_gate` on the response means `check_tool_gate` demoted
 *  this run to WARN over an open, non-critical upstream incident -- a BLOCK
 *  never reaches this response at all (refused with 409 before execution). */
export async function executeToolVersion(
  versionId: string,
  body: ToolExecutionRequest,
  signal?: AbortSignal,
): Promise<ToolExecutionResponse> {
  if (USE_FIXTURES) return makeFixtureExecuteToolVersion(versionId, body);
  return postJson<ToolExecutionResponse>(`/v1/tool-versions/${versionId}/execute`, body, signal);
}


/* ---------------------------------------------------------------------------
   AI governance -- the legacy portal's `agents-view` (`ui/index.html`,
   heading "Models, agents, and evaluations"), ported onto the real,
   already-merged `ai_governance_api.py` model-route routes plus `api.py`'s
   `/ai/runtime-status` and `/agent-evaluations` routes that view calls.
   Added here as a clearly-delimited block, same convention as the Context
   Products block above. See `AiGovernanceScreen.tsx`'s own header comment
   for the full endpoint list, file:line citations, and what was
   deliberately left out (the kill switch; `AgentEvalGateRead`, which is
   `AiRegistryScreen`'s per-asset-version concern, not this org-wide suite).
--------------------------------------------------------------------------- */

import type {
  AgentEvaluationRunRead,
  AiRuntimeStatusRead,
  ModelRouteConfigurationCreate,
  ModelRouteConfigurationRead,
} from "./types";
import {
  makeFixtureAgentEvaluations,
  makeFixtureAiRuntimeStatus,
  makeFixtureCreateModelRoute,
  makeFixtureModelRoutes,
  makeFixtureRunAgentEvaluation,
  makeFixtureSubmitModelRoute,
} from "./fixtures";

export interface ModelRouteQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/model-routes` (`list_model_routes`,
 *  `ai_governance_api.py:167`) -- one row per route version, newest version
 *  first per `route_key`, exactly as the query orders them server-side;
 *  matches `loadModelRoutes()`'s call in the legacy screen. */
export async function fetchModelRoutes(
  organizationId: string,
  query: ModelRouteQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ModelRouteConfigurationRead>> {
  if (USE_FIXTURES) return makeFixtureModelRoutes(organizationId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<ModelRouteConfigurationRead>>(
    `/v1/organizations/${organizationId}/model-routes?${params}`,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/model-routes` (`create_model_route`,
 *  `ai_governance_api.py:94`) -- creates a new `DRAFT` version for the given
 *  `route_key` (version auto-incremented server-side); matches the legacy
 *  screen's `#model-route-form` submit handler field-for-field. */
export async function createModelRoute(
  organizationId: string,
  body: ModelRouteConfigurationCreate,
  signal?: AbortSignal,
): Promise<ModelRouteConfigurationRead> {
  if (USE_FIXTURES) return makeFixtureCreateModelRoute(organizationId, body);
  return postJson<ModelRouteConfigurationRead>(
    `/v1/organizations/${organizationId}/model-routes`,
    body,
    signal,
  );
}

/** `POST /v1/model-routes/{route_id}/submit` (`submit_model_route`,
 *  `ai_governance_api.py:209`) -- moves a `DRAFT` route to `PENDING_REVIEW`
 *  and opens the same `GovernanceReview` `ReviewQueueScreen` reads; matches
 *  the legacy screen's `data-submit-route` action. */
export async function submitModelRoute(
  routeId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) return makeFixtureSubmitModelRoute(routeId);
  return postJson<GovernanceReviewRead>(`/v1/model-routes/${routeId}/submit`, {}, signal);
}

/** `GET /v1/ai/runtime-status` (`ai_runtime_status`, `api.py:181`) -- the
 *  orchestration/model-route/identity/secrets posture the legacy screen's
 *  `#ai-runtime` rail renders via `renderRuntime()`. Org-independent (the
 *  route takes no organization id -- it reflects process-wide `Settings`,
 *  not a tenant's data). */
export async function fetchAiRuntimeStatus(signal?: AbortSignal): Promise<AiRuntimeStatusRead> {
  if (USE_FIXTURES) return makeFixtureAiRuntimeStatus();
  return get<AiRuntimeStatusRead>("/v1/ai/runtime-status", signal);
}

export interface AgentEvaluationQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/agent-evaluations`
 *  (`list_agent_evaluations`, `api.py:349`) -- the legacy screen's
 *  `#evaluation-table` evidence, newest run first. */
export async function fetchAgentEvaluations(
  organizationId: string,
  query: AgentEvaluationQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AgentEvaluationRunRead>> {
  if (USE_FIXTURES) return makeFixtureAgentEvaluations(organizationId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<AgentEvaluationRunRead>>(
    `/v1/organizations/${organizationId}/agent-evaluations?${params}`,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/agent-evaluations`
 *  (`run_agent_evaluation`, `api.py:294`) -- executes the deterministic
 *  repeatable control suite (`run_control_evaluation`, `agent_evals.py`)
 *  and records one `AgentEvaluationRunRead`; matches the legacy screen's
 *  `#run-evaluation` button. */
export async function runAgentEvaluation(
  organizationId: string,
  signal?: AbortSignal,
): Promise<AgentEvaluationRunRead> {
  if (USE_FIXTURES) return makeFixtureRunAgentEvaluation(organizationId);
  return postJson<AgentEvaluationRunRead>(
    `/v1/organizations/${organizationId}/agent-evaluations`,
    {},
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Unified lineage -- nav id `unified-lineage`, `UnifiedLineageScreen`'s own
   routes. See that screen's file-top comment for the full endpoint list and
   what was deliberately left out of scope (domain scope / cross-boundary
   grants, the legacy force-directed graph engine).
--------------------------------------------------------------------------- */

import { makeFixtureUnifiedLineageGraph } from "./fixtures";

export interface UnifiedLineageGraphQuery {
  nodeLimit?: number;
  edgeLimit?: number;
  suggestionStatus?: "ALL" | "PENDING" | "APPROVED" | "REJECTED";
}

/** `GET /v1/datasources/{datasourceId}/unified-lineage/graph`
 *  (`unified_lineage_api.py::get_unified_lineage_graph`, ~line 1181) -- the
 *  merged FK + suggested + dbt + OpenLineage + view/procedure graph for one
 *  datasource, with the real, configurable `node_limit`/`edge_limit`/
 *  `suggestion_status` query params `UnifiedLineageScreen`'s own controls
 *  need. `fetchLineageGraph` (above) already exists but hardcodes
 *  `node_limit=200&edge_limit=500` with no `suggestion_status` param -- it
 *  was built for a different, narrower purpose and is left exactly as it
 *  landed rather than edited in place, matching this file's own established
 *  convention of adding a new function alongside an earlier one instead of
 *  changing a shipped call site's behaviour out from under it. */
export async function fetchUnifiedLineageGraph(
  datasourceId: string,
  query: UnifiedLineageGraphQuery = {},
  signal?: AbortSignal,
): Promise<UnifiedLineageGraphRead> {
  if (USE_FIXTURES) return makeFixtureUnifiedLineageGraph(datasourceId, query);
  const params = new URLSearchParams();
  params.set("node_limit", String(query.nodeLimit ?? 300));
  params.set("edge_limit", String(query.edgeLimit ?? 1500));
  params.set("suggestion_status", query.suggestionStatus ?? "APPROVED");
  return get<UnifiedLineageGraphRead>(
    `/v1/datasources/${datasourceId}/unified-lineage/graph?${params}`,
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Transformations -- nav id `transformations`, `TransformationsScreen`'s own
   routes (`src/aida/dbt_api.py`). See that screen's file-top comment for how
   this screen was actually located (the nav button is real but hidden
   behind an organization integration-policy flag, not missing) and for what
   was deliberately left out of scope (the legacy Cytoscape DAG canvas).
--------------------------------------------------------------------------- */

import type {
  DbtArtifactImportRead,
  DbtArtifactImportRequest,
  DbtLineageRead,
  DbtProjectCreate,
  DbtProjectRead,
} from "./types";
import {
  makeFixtureCreateDbtProject,
  makeFixtureDbtArtifactImports,
  makeFixtureDbtLineage,
  makeFixtureDbtProjects,
  makeFixtureDbtResources,
  makeFixtureImportDbtManifest,
} from "./fixtures";

/** `GET /v1/projects/{project_id}/dbt-projects` (`list_dbt_projects`,
 *  `dbt_api.py:200`) -- delivery-project-scoped dbt project registrations,
 *  the same scoping level `fetchOrgProjects`'s callers already use one level
 *  up. Every dbt route this file calls scopes through `_project_scope`/
 *  `_dbt_project_scope`/`_artifact_scope` (`dbt_api.py:58-92`), each of
 *  which calls `_require_dbt_integration` (`dbt_api.py:138`) before doing
 *  anything else -- so THIS call, not only the create calls below, 403s with
 *  `"dbt integration is disabled for this organization"` whenever the
 *  organization's integration policy has not enabled dbt. Callers should
 *  render that detail string, not a generic error (`TransformationsScreen`'s
 *  `DbtDisabledState` does). */
export async function fetchDbtProjects(
  projectId: string,
  signal?: AbortSignal,
): Promise<PageOf<DbtProjectRead>> {
  if (USE_FIXTURES) return makeFixtureDbtProjects(projectId);
  return get<PageOf<DbtProjectRead>>(`/v1/projects/${projectId}/dbt-projects?limit=500`, signal);
}

/** `POST /v1/projects/{project_id}/dbt-projects` (`create_dbt_project`,
 *  `dbt_api.py:149`) -- registers ownership + warehouse mapping only. No
 *  repository credentials accepted, matching the legacy dialog's own
 *  privacy note (`ui/index.html#dbt-project-dialog`). */
export async function createDbtProject(
  projectId: string,
  body: DbtProjectCreate,
  signal?: AbortSignal,
): Promise<DbtProjectRead> {
  if (USE_FIXTURES) return makeFixtureCreateDbtProject(projectId, body);
  return postJson<DbtProjectRead>(`/v1/projects/${projectId}/dbt-projects`, body, signal);
}

/** `GET /v1/dbt-projects/{dbt_project_id}/artifact-imports`
 *  (`list_dbt_artifact_imports`, `dbt_api.py:432`) -- one dbt project's
 *  immutable import history, newest first. */
export async function fetchDbtArtifactImports(
  dbtProjectId: string,
  signal?: AbortSignal,
): Promise<PageOf<DbtArtifactImportRead>> {
  if (USE_FIXTURES) return makeFixtureDbtArtifactImports(dbtProjectId);
  return get<PageOf<DbtArtifactImportRead>>(
    `/v1/dbt-projects/${dbtProjectId}/artifact-imports?limit=100`,
    signal,
  );
}

/** `POST /v1/dbt-projects/{dbt_project_id}/artifact-imports`
 *  (`import_dbt_manifest`, `dbt_api.py:232`). The body carries the already
 *  PARSED JSON of manifest.json (required) plus optional catalog.json /
 *  run_results.json -- reading those `File` objects and calling
 *  `JSON.parse` is the screen's job (matching the legacy form's own
 *  `FileReader`/`JSON.parse`, `ui/app.js`'s `#dbt-import-form` handler);
 *  this call only posts the already-parsed objects. Idempotent by manifest
 *  fingerprint server-side: re-importing an unchanged manifest returns the
 *  existing artifact instead of creating a duplicate (`dbt_api.py:265-271`). */
export async function importDbtManifest(
  dbtProjectId: string,
  body: DbtArtifactImportRequest,
  signal?: AbortSignal,
): Promise<DbtArtifactImportRead> {
  if (USE_FIXTURES) return makeFixtureImportDbtManifest(dbtProjectId, body);
  return postJson<DbtArtifactImportRead>(
    `/v1/dbt-projects/${dbtProjectId}/artifact-imports`,
    body,
    signal,
  );
}

/** Mirrors `schemas.py`'s `DbtResourceRead` (`dbt_api.py:467`'s response
 *  item shape). Not added to the shared `types.ts` -- `TransformationsScreen`
 *  is this port's only consumer, matching this file's existing precedent for
 *  a response shape with one caller (`AgentAskError`, `ReviewQueueQuery`)
 *  rather than growing the large shared file for it. */
export interface DbtResourceRead {
  id: string;
  artifact_import_id: string;
  unique_id: string;
  resource_type: string;
  package_name: string;
  name: string;
  database_name: string | null;
  schema_name: string | null;
  relation_name: string | null;
  materialization: string | null;
  original_file_path: string | null;
  description: string | null;
  compiled_sql_hash: string | null;
  compiled_sql_redacted: string | null;
  sql_parse_status: string;
  column_names: string[];
  column_descriptions: Record<string, string>;
  column_types: Record<string, string>;
  tags: string[];
  depends_on_unique_ids: string[];
  matched_table_id: string | null;
  test_status: string | null;
  test_failures: number | null;
  test_execution_time: number | null;
  extra_metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DbtResourceQuery {
  resourceType?: string | null;
  matched?: boolean | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/dbt-artifact-imports/{artifact_id}/resources`
 *  (`list_dbt_resources`, `dbt_api.py:467`) -- one immutable artifact's
 *  parsed models/sources/tests/seeds/snapshots, each carrying its catalog
 *  match, SQL-parse evidence, and (for TEST resources) the last reconciled
 *  execution outcome (`reconcile_dbt_test_quality`, `dbt_quality_bridge.py`). */
export async function fetchDbtResources(
  artifactImportId: string,
  query: DbtResourceQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<DbtResourceRead>> {
  if (USE_FIXTURES) return makeFixtureDbtResources(artifactImportId, query);
  const params = new URLSearchParams();
  if (query.resourceType) params.set("resource_type", query.resourceType);
  if (query.matched !== undefined && query.matched !== null) {
    params.set("matched", String(query.matched));
  }
  params.set("limit", String(query.limit ?? 500));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<DbtResourceRead>>(
    `/v1/dbt-artifact-imports/${artifactImportId}/resources?${params}`,
    signal,
  );
}

/** `GET /v1/dbt-artifact-imports/{artifact_id}/lineage` (`get_dbt_lineage`,
 *  `dbt_api.py:507`) -- table-level dependency edges (`edge_type
 *  DEPENDS_ON`) plus column-level edges (`COLUMN_DEPENDS_ON`,
 *  `dbt_column_lineage.py::extract_column_lineage`) across the same
 *  resource set `fetchDbtResources` returns for this artifact. */
export async function fetchDbtLineage(
  artifactImportId: string,
  signal?: AbortSignal,
): Promise<DbtLineageRead> {
  if (USE_FIXTURES) return makeFixtureDbtLineage(artifactImportId);
  return get<DbtLineageRead>(`/v1/dbt-artifact-imports/${artifactImportId}/lineage?limit=2000`, signal);
}

/* ---------------------------------------------------------------------------
   ABAC access policies + authorization simulation -- nav id `access-policies`.
   Both routes live in `workspace_api.py` despite the domain name (confirmed
   by direct source read, not `api.py`):

     - GET  /v1/organizations/{organization_id}/access-policies      list_access_policies, workspace_api.py:511
     - POST /v1/organizations/{organization_id}/access-policies       create_access_policy, workspace_api.py:527
     - POST /v1/workspaces/{workspace_id}/authorization-simulations   simulate_authorization, workspace_api.py:620

   `subject_match` / `resource_match` / `transform` / `condition` / `subjects`
   are genuinely free-form policy data (matches the legacy portal's
   `#abac-policy-form` / `#abac-simulate-form`, `control-center.js:201-202`) --
   the screen parses their raw JSON textareas client-side rather than
   building a structured editor for them.
--------------------------------------------------------------------------- */

import type {
  AccessPolicyCreate,
  AccessPolicyRead,
  AuthorizationSimulationRead,
  AuthorizationSimulationRequest,
} from "./types";
import { makeFixtureAccessPolicies, makeFixtureCreateAccessPolicy, makeFixtureSimulateAuthorization } from "./fixtures";

export interface AccessPolicyQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/access-policies` -- visible to
 *  `PlatformAdmin`/`OrganizationAdmin`/`DataAdmin`/`Reviewer`, a wider set
 *  than who may create one below. Multiple rows can share a `code`: creating
 *  again under the same code auto-increments `version` server-side rather
 *  than replacing the row, so the list carries `version` per row. */
export async function fetchAccessPolicies(
  organizationId: string,
  query: AccessPolicyQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<AccessPolicyRead>> {
  if (USE_FIXTURES) return makeFixtureAccessPolicies(organizationId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 200));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<AccessPolicyRead>>(
    `/v1/organizations/${organizationId}/access-policies?${params}`,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/access-policies` -- narrower
 *  than the list above (`PlatformAdmin`/`OrganizationAdmin` only). A new
 *  policy always starts `DRAFT` unless the caller explicitly sets
 *  `status: "ACTIVE"` in the body. */
export async function createAccessPolicy(
  organizationId: string,
  body: AccessPolicyCreate,
  signal?: AbortSignal,
): Promise<AccessPolicyRead> {
  if (USE_FIXTURES) return makeFixtureCreateAccessPolicy(organizationId, body);
  return postJson<AccessPolicyRead>(`/v1/organizations/${organizationId}/access-policies`, body, signal);
}

/** `POST /v1/workspaces/{workspace_id}/authorization-simulations` --
 *  "who could see this?" against the live policy engine, open to any
 *  workspace member. `body.workspace_id` must match the path param or the
 *  real endpoint returns 422; callers must set both from the same picked
 *  workspace id. */
export async function simulateAuthorization(
  workspaceId: string,
  body: AuthorizationSimulationRequest,
  signal?: AbortSignal,
): Promise<AuthorizationSimulationRead> {
  if (USE_FIXTURES) return makeFixtureSimulateAuthorization(workspaceId, body);
  return postJson<AuthorizationSimulationRead>(
    `/v1/workspaces/${workspaceId}/authorization-simulations`,
    body,
    signal,
  );
}


/* ---------------------------------------------------------------------------
   Workspace membership, source-binding decisions, BI/Tableau lineage
   connections -- the piece of the legacy Enterprise Control Center's
   `renderAccess`/`renderBi` this file's own `fetchOrgWorkspaces`/
   `fetchWorkspaceSourceBindings`/`createWorkspace`/`requestSourceBinding`
   (above) do not cover: workspace *members* (`workspace_api.py:160-208`),
   the *decision* half of the maker-checker source-binding flow
   (`workspace_api.py:293`, `createWorkspace`/`requestSourceBinding` only
   create/request), and BI connections (`bi_api.py`, new -- nothing in this
   file touches it yet).
--------------------------------------------------------------------------- */

import type {
  BiArtifactImportRead,
  BiArtifactImportRequest,
  BiConnectionCreate,
  BiConnectionRead,
  SourceBindingDecision,
  WorkspaceMembershipCreate,
  WorkspaceMembershipRead,
} from "./types";
import {
  makeFixtureAddWorkspaceMember,
  makeFixtureCreateBiConnection,
  makeFixtureDecideSourceBinding,
  makeFixtureImportBiArtifact,
  makeFixtureProjectBiConnections,
  makeFixtureWorkspaceMembers,
} from "./fixtures";

/** `POST /v1/workspaces/{workspace_id}/members` (`workspace_api.py:160`,
 *  `_ADMIN` only: PlatformAdmin/OrganizationAdmin/DataAdmin). 409s if the
 *  principal already has a membership in this workspace. */
export async function addWorkspaceMember(
  workspaceId: string,
  body: WorkspaceMembershipCreate,
  signal?: AbortSignal,
): Promise<WorkspaceMembershipRead> {
  if (USE_FIXTURES) return makeFixtureAddWorkspaceMember(workspaceId, body);
  return postJson<WorkspaceMembershipRead>(
    `/v1/workspaces/${workspaceId}/members`,
    body,
    signal,
  );
}

/** `GET /v1/workspaces/{workspace_id}/members` (`workspace_api.py:207`,
 *  `_ANY_MEMBER`: the `_ADMIN` roles plus Steward/Analyst/Reviewer). No
 *  limit/offset -- the route returns every membership unpaginated. */
export async function fetchWorkspaceMembers(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PageOf<WorkspaceMembershipRead>> {
  if (USE_FIXTURES) return makeFixtureWorkspaceMembers(workspaceId);
  return get<PageOf<WorkspaceMembershipRead>>(
    `/v1/workspaces/${workspaceId}/members`,
    signal,
  );
}

/** `POST /v1/source-bindings/{binding_id}/decision` (`workspace_api.py:293`,
 *  roles `_ADMIN` + Reviewer) -- the maker-checker approve/reject a pending
 *  binding from `requestSourceBinding` above needs. The endpoint 403/409s
 *  when the decider is the same principal who requested the binding; that
 *  detail string is surfaced as-is by `postJson`'s `ApiError`, not swallowed. */
export async function decideSourceBinding(
  bindingId: string,
  body: SourceBindingDecision,
  signal?: AbortSignal,
): Promise<SourceBindingRead> {
  if (USE_FIXTURES) return makeFixtureDecideSourceBinding(bindingId, body);
  return postJson<SourceBindingRead>(
    `/v1/source-bindings/${bindingId}/decision`,
    body,
    signal,
  );
}

export interface BiConnectionQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{project_id}/bi-connections` (`bi_api.py:226`) -- roles
 *  add DataSteward/Auditor/Viewer on top of the create roles below. 403s via
 *  `_require_bi_integration` when the organization's integration policy has
 *  not enabled `"bi"`; that detail is a legitimate expected state for orgs
 *  that have not opted in, not a bug, and is surfaced the same way. */
export async function fetchProjectBiConnections(
  projectId: string,
  opts: BiConnectionQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<BiConnectionRead>> {
  if (USE_FIXTURES) return makeFixtureProjectBiConnections(projectId, opts);
  const params = new URLSearchParams();
  params.set("limit", String(opts.limit ?? 100));
  params.set("offset", String(opts.offset ?? 0));
  return get<PageOf<BiConnectionRead>>(
    `/v1/projects/${projectId}/bi-connections?${params}`,
    signal,
  );
}

/** `POST /v1/projects/{project_id}/bi-connections` (`bi_api.py:171`, roles
 *  PlatformAdmin/DataAdmin/MetadataAdmin) -- registers a Tableau/Power BI/
 *  Looker connection against one of the project's own datasources. */
export async function createBiConnection(
  projectId: string,
  body: BiConnectionCreate,
  signal?: AbortSignal,
): Promise<BiConnectionRead> {
  if (USE_FIXTURES) return makeFixtureCreateBiConnection(projectId, body);
  return postJson<BiConnectionRead>(
    `/v1/projects/${projectId}/bi-connections`,
    body,
    signal,
  );
}

/** `POST /v1/bi-connections/{connection_id}/artifact-imports` (`bi_api.py:258`,
 *  same create roles) -- `body.artifact` is the raw exported BI artifact
 *  JSON, same as the legacy `#bi-import-form`'s textarea (`control-center.js`);
 *  parsing that text into JSON is the caller's job, not this function's. */
export async function importBiArtifact(
  connectionId: string,
  body: BiArtifactImportRequest,
  signal?: AbortSignal,
): Promise<BiArtifactImportRead> {
  if (USE_FIXTURES) return makeFixtureImportBiArtifact(connectionId, body);
  return postJson<BiArtifactImportRead>(
    `/v1/bi-connections/${connectionId}/artifact-imports`,
    body,
    signal,
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

import type { CompliancePackRead, GeneratePackRequest } from "./types";
import {
  makeFixtureCompliancePacks,
  makeFixtureDownloadCompliancePack,
  makeFixtureGenerateCompliancePack,
} from "./fixtures";

export interface CompliancePackQuery {
  framework?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/compliance/packs` (`list_compliance_packs`, `compliance_api.py:119`).
 *  Gated server-side behind `PlatformAdmin`/`ComplianceOfficer`/`DataSteward`/
 *  `Viewer` -- a Viewer can see the list (name/framework/status/generated_at)
 *  but not a pack's evidence body, see `downloadCompliancePack` below. */
export async function fetchCompliancePacks(
  query: CompliancePackQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<CompliancePackRead>> {
  if (USE_FIXTURES) return makeFixtureCompliancePacks(query);
  const params = new URLSearchParams();
  if (query.framework) params.set("framework", query.framework);
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<CompliancePackRead>>(`/v1/compliance/packs?${params}`, signal);
}

/** `POST /v1/compliance/packs/generate` (`generate_compliance_pack`,
 *  `compliance_api.py:62`) -- gated behind `PlatformAdmin`/`ComplianceOfficer`/
 *  `DataSteward` (no `Viewer`). The route itself 422s when `period_end` is
 *  not after `period_start`; that detail string is surfaced as-is, not
 *  re-validated client-side. */
export async function generateCompliancePack(
  body: GeneratePackRequest,
  signal?: AbortSignal,
): Promise<CompliancePackRead> {
  if (USE_FIXTURES) return makeFixtureGenerateCompliancePack(body);
  return postJson<CompliancePackRead>("/v1/compliance/packs/generate", body, signal);
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
export async function downloadCompliancePack(
  packId: string,
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  if (USE_FIXTURES) return makeFixtureDownloadCompliancePack(packId);
  return get<Record<string, unknown>>(`/v1/compliance/packs/${packId}/download`, signal);
}


/* ---------------------------------------------------------------------------
   Catalog bulk actions + unowned-asset stewardship backlog.

   Ported from the legacy portal's single `#catalog-bulk-form` (one filter,
   four actions keyed off `<select name="action">`, `ui/scripts/features/
   control-center.js`'s `renderCatalog`/submit handler) and its separate
   `#route-unowned` button. Every write here requires one of
   `CATALOG_BULK_ACTION_WRITE_ROLES` (`api.py:161`) for the four table
   actions, or `WRITE_ROLES` (`stewardship_api.py:98`) for routing the
   backlog -- both already satisfied by this app's dev-mode principal
   (`identityHeaders()` above).

     POST /v1/organizations/{organization_id}/tables/bulk-tag       (api.py:3199)
     POST /v1/organizations/{organization_id}/tables/bulk-classify  (api.py:3271)
     POST /v1/organizations/{organization_id}/tables/bulk-own       (api.py:3360)
     POST /v1/organizations/{organization_id}/tables/bulk-certify   (api.py:3435)
     GET  /v1/organizations/{organization_id}/stewardship/unowned-backlog
                                                        (stewardship_api.py:1606)
     POST /v1/organizations/{organization_id}/stewardship/unowned-backlog/route
                                                        (stewardship_api.py:1645)

   Every bulk-* body carries exactly one of `table_ids`/`column_ids` (explicit
   selection) or `filter` (datasource + match field/pattern) -- the backend
   has no third, broader "match everything" mode, so this client sends
   whichever one the caller already built rather than inventing a union type
   of its own. Each matched subject succeeds or fails independently
   server-side; that per-item detail comes back on `CatalogBulkActionRunRead.
   results`, not just an aggregate count.
--------------------------------------------------------------------------- */

import type {
  CatalogBulkCertifyRequest,
  CatalogBulkClassifyRequest,
  CatalogBulkOwnRequest,
  CatalogBulkTagRequest,
  CatalogBulkActionRunRead,
  UnownedAssetBacklogRouteRequest,
  UnownedAssetBacklogRouteResult,
  UnownedAssetEscalationRead,
} from "./types";
import {
  makeFixtureBulkCertifyCatalogTables,
  makeFixtureBulkClassifyCatalogColumns,
  makeFixtureBulkOwnCatalogTables,
  makeFixtureBulkTagCatalogTables,
  makeFixtureRouteUnownedAssetBacklog,
  makeFixtureUnownedAssetBacklog,
} from "./fixtures";

/** `POST /v1/organizations/{organization_id}/tables/bulk-tag`
 *  (`bulk_tag_tables`, `api.py:3199`) -- applies `tag_key`/`tag_value` to
 *  every table `body.table_ids` names, or every table `body.filter` (a
 *  datasource + match field/pattern) resolves to. */
export async function bulkTagCatalogTables(
  organizationId: string,
  body: CatalogBulkTagRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  if (USE_FIXTURES) return makeFixtureBulkTagCatalogTables(organizationId, body);
  return postJson<CatalogBulkActionRunRead>(
    `/v1/organizations/${organizationId}/tables/bulk-tag`,
    body,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/tables/bulk-classify`
 *  (`bulk_classify_tables`, `api.py:3271`) -- sets `classification` on every
 *  column `body.column_ids` names, every column matching
 *  `body.column_name_pattern` under `body.table_ids`/`body.filter`'s
 *  matched tables, or (default pattern `"*"`) every column of those tables. */
export async function bulkClassifyCatalogColumns(
  organizationId: string,
  body: CatalogBulkClassifyRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  if (USE_FIXTURES) return makeFixtureBulkClassifyCatalogColumns(organizationId, body);
  return postJson<CatalogBulkActionRunRead>(
    `/v1/organizations/${organizationId}/tables/bulk-classify`,
    body,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/tables/bulk-own`
 *  (`bulk_own_tables`, `api.py:3360`) -- assigns `owner_principal` (an
 *  INDIVIDUAL principal id or GROUP name) as owner of every matched table. */
export async function bulkAssignCatalogOwnership(
  organizationId: string,
  body: CatalogBulkOwnRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  if (USE_FIXTURES) return makeFixtureBulkOwnCatalogTables(organizationId, body);
  return postJson<CatalogBulkActionRunRead>(
    `/v1/organizations/${organizationId}/tables/bulk-own`,
    body,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/tables/bulk-certify`
 *  (`bulk_certify_tables`, `api.py:3435`) -- certifies every matched table
 *  with `rationale` (server-required, >=10 chars) and a future `expires_at`. */
export async function bulkCertifyCatalogTables(
  organizationId: string,
  body: CatalogBulkCertifyRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  if (USE_FIXTURES) return makeFixtureBulkCertifyCatalogTables(organizationId, body);
  return postJson<CatalogBulkActionRunRead>(
    `/v1/organizations/${organizationId}/tables/bulk-certify`,
    body,
    signal,
  );
}

export interface UnownedAssetBacklogQuery {
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/stewardship/unowned-backlog`
 *  (`list_unowned_backlog`, `stewardship_api.py:1606`) -- one row per table
 *  the platform has detected has no assigned owner, at whatever stage
 *  (UNOWNED/ROUTED/ESCALATED/ESCALATED_TIER_2/RESOLVED) its escalation has
 *  reached; matches the legacy screen's "unowned backlog" list. */
export async function fetchUnownedAssetBacklog(
  organizationId: string,
  query: UnownedAssetBacklogQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<UnownedAssetEscalationRead>> {
  if (USE_FIXTURES) return makeFixtureUnownedAssetBacklog(organizationId, query);
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<UnownedAssetEscalationRead>>(
    `/v1/organizations/${organizationId}/stewardship/unowned-backlog?${params}`,
    signal,
  );
}

/** `POST /v1/organizations/{organization_id}/stewardship/unowned-backlog/route`
 *  (`route_unowned_backlog`, `stewardship_api.py:1645`) -- advances every
 *  in-scope escalation one notification-rule-driven step (UNOWNED -> ROUTED,
 *  ROUTED -> ESCALATED, ESCALATED -> ESCALATED_TIER_2, or straight to
 *  RESOLVED where ownership was already found), optionally scoped to one
 *  datasource/domain/line of business; an empty body routes the whole
 *  organization. Matches the legacy screen's `#route-unowned` button. */
export async function routeUnownedAssetBacklog(
  organizationId: string,
  body: UnownedAssetBacklogRouteRequest,
  signal?: AbortSignal,
): Promise<UnownedAssetBacklogRouteResult> {
  if (USE_FIXTURES) return makeFixtureRouteUnownedAssetBacklog(organizationId, body);
  return postJson<UnownedAssetBacklogRouteResult>(
    `/v1/organizations/${organizationId}/stewardship/unowned-backlog/route`,
    body,
    signal,
  );
}


/* ---------------------------------------------------------------------------
   Tool plans -- multi-step tool orchestration, distinct from the single
   governed-tool-version CRUD/execute `ToolRegistryScreen` already owns. All
   in `tool_plans_api.py`, `/v1` prefix:

     - POST /v1/tool-plans                       create_tool_plan,   tool_plans_api.py:174
     - GET  /v1/tool-plans/{plan_id}              get_tool_plan,      tool_plans_api.py:233
     - POST /v1/tool-plans/{plan_id}/validate     validate_tool_plan, tool_plans_api.py:275
     - POST /v1/tool-plans/{plan_id}/execute      execute_tool_plan,  tool_plans_api.py:350
     - POST /v1/tool-plans/{plan_id}/cancel       cancel_tool_plan,   tool_plans_api.py:469
     - GET  /v1/tool-plans/{plan_id}/evidence     list_tool_plan_evidence, tool_plans_api.py:512

   None of these carry `{organization_id}` in the path, unlike every sibling
   domain above -- each handler calls `context.require_organization()` and
   scopes/verifies against the row's own `organization_id` instead
   (`enforce_organization`), so the org is derived from auth context alone.

   Every route is additionally gated by an edition entitlement check
   (`_deny_unless_entitled`, `tool_plans_api.py:138`) for capability
   `"multi_step_tool_plans"`, on top of the ordinary `require_roles` check.
   A denial from the entitlement gate is a plain 403 whose `detail` is the
   reason code itself (`ENTITLEMENT_EDITION_INSUFFICIENT` /
   `ENTITLEMENT_CAPABILITY_UNREGISTERED`, `edition_entitlements.py`), while a
   plain role denial's `detail` reads
   `"one of these roles is required: ..."` -- distinguishable string shapes,
   which is what `ToolPlansScreen` keys off of to show "this org's edition
   doesn't include multi-step tool plans" instead of a generic Forbidden.
--------------------------------------------------------------------------- */

import type {
  ExecutionRead,
  ToolPlanCreate,
  ToolPlanDetailRead,
  ToolPlanRead,
  ValidationResponse,
} from "./types";
import {
  makeFixtureCancelToolPlan,
  makeFixtureCreateToolPlan,
  makeFixtureExecuteToolPlan,
  makeFixtureToolPlan,
  makeFixtureToolPlanEvidence,
  makeFixtureValidateToolPlan,
} from "./fixtures";

/** `POST /v1/tool-plans` -- creates a `DRAFT` plan from one or more steps
 *  plus a budget (both default-filled server-side if omitted beyond
 *  `name`/`steps`). Matches the legacy `#tool-plan-form` submit, which only
 *  ever built a single-step plan -- the create form here does the same;
 *  the model supports many steps, but multi-step plan *authoring* in the UI
 *  is left as a documented future enhancement (see `ToolPlansScreen.tsx`). */
export async function createToolPlan(
  body: ToolPlanCreate,
  signal?: AbortSignal,
): Promise<ToolPlanRead> {
  if (USE_FIXTURES) return makeFixtureCreateToolPlan(body);
  return postJson<ToolPlanRead>(`/v1/tool-plans`, body, signal);
}

/** `GET /v1/tool-plans/{plan_id}` -- the plan plus its ordered steps. */
export async function fetchToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ToolPlanDetailRead> {
  if (USE_FIXTURES) return makeFixtureToolPlan(planId);
  return get<ToolPlanDetailRead>(`/v1/tool-plans/${planId}`, signal);
}

/** `POST /v1/tool-plans/{plan_id}/validate` -- no body. Checks step
 *  ordering/dependencies/budget without executing anything; matches
 *  legacy's `plan-validate` button. */
export async function validateToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ValidationResponse> {
  if (USE_FIXTURES) return makeFixtureValidateToolPlan(planId);
  return postJson<ValidationResponse>(`/v1/tool-plans/${planId}/validate`, {}, signal);
}

/** `POST /v1/tool-plans/{plan_id}/execute` -- no body. 409s when the plan's
 *  `status` is not `DRAFT`/`VALIDATED`; matches legacy's `plan-execute`
 *  button. */
export async function executeToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ExecutionRead> {
  if (USE_FIXTURES) return makeFixtureExecuteToolPlan(planId);
  return postJson<ExecutionRead>(`/v1/tool-plans/${planId}/execute`, {}, signal);
}

/** `POST /v1/tool-plans/{plan_id}/cancel` -- no body, narrower roles than
 *  the rest of this file (`PlatformAdmin`/`ToolDeveloper` only, no
 *  `DataEngineer`). 409s when the plan is already `COMPLETED`/`CANCELLED`;
 *  matches legacy's `plan-cancel` button. */
export async function cancelToolPlan(
  planId: string,
  signal?: AbortSignal,
): Promise<ToolPlanRead> {
  if (USE_FIXTURES) return makeFixtureCancelToolPlan(planId);
  return postJson<ToolPlanRead>(`/v1/tool-plans/${planId}/cancel`, {}, signal);
}

export interface ToolPlanEvidenceQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/tool-plans/{plan_id}/evidence` -- paged `ExecutionRead` history
 *  for the plan; matches legacy's `plan-evidence` button. */
export async function fetchToolPlanEvidence(
  planId: string,
  query: ToolPlanEvidenceQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ExecutionRead>> {
  if (USE_FIXTURES) return makeFixtureToolPlanEvidence(planId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<ExecutionRead>>(`/v1/tool-plans/${planId}/evidence?${params}`, signal);
}

/* ---------------------------------------------------------------------------
   Reliability -- SLOs, notification rules, archive/WORM evidence posture,
   and runtime data-contract evaluation. Ports the legacy portal's
   `renderReliability()` (`ui/scripts/features/control-center.js`) onto the
   real, already-merged `observability_api.py` / `notification_api.py` /
   `runtime_contracts_api.py` routes -- the legacy screen's own
   `loadControlCenter()` calls these exact paths.

   Honest scope note: `organizationId` is accepted below on the SLO and
   notification-rule functions for parity with every other org-scoped fetch
   in this file (and to key fixture data the same way other screens do), but
   it has nowhere to go on the wire for these particular routes.
   `observability_api.py`'s and `notification_api.py`'s routes take no
   `organization_id` path or query param at all -- unlike, say,
   `fetchModelRoutes`'s `/v1/organizations/{organization_id}/model-routes` --
   because the server instead reads `context.require_organization()`, which
   resolves from the `X-Organization-Id` header
   (`security.py::get_security_context`). `identityHeaders()` above does not
   send that header today. That gap is pre-existing, shared infrastructure
   (identityHeaders is explicitly out of this addition's scope) and the
   legacy portal has the identical gap -- its own `api()` helper never sends
   `X-Organization-Id` either, so this is not a regression, just a limit on
   what a live (`VITE_USE_FIXTURES=0`) run of this screen can do today: those
   two endpoint families will 400 with "organization context is required for
   this operation" until that header is added, exactly as they would against
   the legacy UI. Fixture mode (the default) is unaffected -- it never
   depended on the header.
--------------------------------------------------------------------------- */

import type {
  ArchiveStatusRead,
  EvaluationResponse,
  NotificationRuleCreate,
  NotificationRuleRead,
  SlaStatusResponse,
  SloBudgetRead,
  SloDefinitionCreate,
  SloDefinitionRead,
} from "./types";
import type { ViolationRead } from "./ui-types";
import {
  makeFixtureArchiveStatus,
  makeFixtureContractSlaStatus,
  makeFixtureContractViolations,
  makeFixtureCreateNotificationRule,
  makeFixtureCreateSloDefinition,
  makeFixtureEvaluateDataContract,
  makeFixtureNotificationRules,
  makeFixtureSloBudget,
  makeFixtureSloDefinitions,
} from "./fixtures";

export interface SloDefinitionQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/observability/slo` (`observability_api.py::list_slo_definitions`,
 *  roles PlatformAdmin/DataAdmin/Operations/Viewer) -- every SLO definition
 *  for the caller's organization, newest first. */
export async function fetchSloDefinitions(
  organizationId: string,
  query: SloDefinitionQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<SloDefinitionRead>> {
  if (USE_FIXTURES) return makeFixtureSloDefinitions(organizationId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<SloDefinitionRead>>(`/v1/observability/slo?${params}`, signal);
}

/** `POST /v1/observability/slo` (`observability_api.py::create_slo_definition`,
 *  roles PlatformAdmin/DataAdmin/Operations) -- 409s if `slo_key` already
 *  exists for this organization. */
export async function createSloDefinition(
  organizationId: string,
  body: SloDefinitionCreate,
  signal?: AbortSignal,
): Promise<SloDefinitionRead> {
  if (USE_FIXTURES) return makeFixtureCreateSloDefinition(organizationId, body);
  return postJson<SloDefinitionRead>("/v1/observability/slo", body, signal);
}

/** `GET /v1/observability/slo/{slo_id}/budget` (`observability_api.py::get_slo_budget`)
 *  -- computed live from the SLO's most recent `SloMeasurement`, never
 *  stored: `status` is HEALTHY/AT_RISK/BREACHED once a measurement exists
 *  (compared against `target`/`threshold`), NO_DATA when none ever landed. */
export async function fetchSloBudget(
  sloId: string,
  signal?: AbortSignal,
): Promise<SloBudgetRead> {
  if (USE_FIXTURES) return makeFixtureSloBudget(sloId);
  return get<SloBudgetRead>(`/v1/observability/slo/${sloId}/budget`, signal);
}

/** `GET /v1/observability/archive/status` (`observability_api.py::get_archive_status`)
 *  -- WORM audit-archive posture: counts, latest archive id/checksum, and
 *  legal-hold count, rolled into one of NO_ARCHIVES/LEGAL_HOLD_ACTIVE/HEALTHY.
 *  Org-scoped via the security context only, same gap noted in this block's
 *  banner comment -- no `organizationId` parameter to thread through. */
export async function fetchArchiveStatus(signal?: AbortSignal): Promise<ArchiveStatusRead> {
  if (USE_FIXTURES) return makeFixtureArchiveStatus();
  return get<ArchiveStatusRead>("/v1/observability/archive/status", signal);
}

export interface NotificationRuleQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/notification-rules` (`notification_api.py::list_notification_rules`,
 *  roles PlatformAdmin/DataAdmin/Operations/Viewer). */
export async function fetchNotificationRules(
  organizationId: string,
  query: NotificationRuleQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<NotificationRuleRead>> {
  if (USE_FIXTURES) return makeFixtureNotificationRules(organizationId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<NotificationRuleRead>>(`/v1/notification-rules?${params}`, signal);
}

/** `POST /v1/notification-rules` (`notification_api.py::create_notification_rule`,
 *  roles PlatformAdmin/DataAdmin/Operations). `conditions` is a free-form
 *  JSON matcher object -- the screen collects it from a raw JSON textarea,
 *  same as the legacy `#notification-rule-form`. */
export async function createNotificationRule(
  organizationId: string,
  body: NotificationRuleCreate,
  signal?: AbortSignal,
): Promise<NotificationRuleRead> {
  if (USE_FIXTURES) return makeFixtureCreateNotificationRule(organizationId, body);
  return postJson<NotificationRuleRead>("/v1/notification-rules", body, signal);
}

/** `POST /v1/data-contracts/{contract_id}/evaluate`
 *  (`runtime_contracts_api.py::evaluate_data_contract`, roles PlatformAdmin/
 *  DataSteward/DataEngineer/Viewer) -- no request body, just the path id.
 *  Evaluates the contract against current schema/quality/freshness state,
 *  persists any violations found, and returns the same evaluation the
 *  enforcement path itself acts on (`allowed`/`enforcement_action`). */
export async function evaluateDataContract(
  contractId: string,
  signal?: AbortSignal,
): Promise<EvaluationResponse> {
  if (USE_FIXTURES) return makeFixtureEvaluateDataContract(contractId);
  return postJson<EvaluationResponse>(`/v1/data-contracts/${contractId}/evaluate`, {}, signal);
}

export interface ContractViolationsQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/data-contracts/{contract_id}/violations`
 *  (`runtime_contracts_api.py::list_contract_violations`, same roles as
 *  evaluate above). `ViolationRead` (`./ui-types`) is hand-written -- see
 *  its own comment for why. */
export async function fetchContractViolations(
  contractId: string,
  query: ContractViolationsQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ViolationRead>> {
  if (USE_FIXTURES) return makeFixtureContractViolations(contractId, query);
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 50));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<ViolationRead>>(`/v1/data-contracts/${contractId}/violations?${params}`, signal);
}

/** `GET /v1/data-contracts/{contract_id}/sla-status`
 *  (`runtime_contracts_api.py::get_sla_status`, same roles as evaluate
 *  above) -- rolling compliance over the trailing `period_days` (server
 *  `Query` bounds: default 30, 1-365). */
export async function fetchContractSlaStatus(
  contractId: string,
  periodDays = 30,
  signal?: AbortSignal,
): Promise<SlaStatusResponse> {
  if (USE_FIXTURES) return makeFixtureContractSlaStatus(contractId, periodDays);
  const params = new URLSearchParams();
  params.set("period_days", String(periodDays));
  return get<SlaStatusResponse>(`/v1/data-contracts/${contractId}/sla-status?${params}`, signal);
}

/* ---------------------------------------------------------------------------
   P1-04: Asset description drafts.

   Backend routes live in `src/aida/asset_description_api.py`:
     * POST /v1/organizations/{org}/asset-description-drafts/generate
     * GET  /v1/organizations/{org}/asset-description-drafts
     * POST /v1/asset-description-drafts/{draft_id}/submit

   Before this file added them the UI had zero references to any of the three
   -- drafts could only be created and moved to PENDING_APPROVAL from `curl`,
   which is why the ReviewQueueScreen renders ASSET_DESCRIPTION_DRAFT items
   but nothing in the app submits them. The server-side batch cap is
   `_GENERATE_BATCH_LIMIT = 100`; the client mirrors it as a fast-fail so
   selecting 101+ rows in the Catalog does not round-trip only to be trimmed
   silently on the server (the server slices `table_ids[:100]` rather than
   422-ing). The submit endpoint's minimum-evidence gate
   (`asset_description_service.ensure_reviewable`) returns HTTP 422 with
   detail "draft carries too little evidence for independent review" when the
   deterministic `overall_score < MINIMUM_EVIDENCE_FOR_REVIEW = 0.4`; that is
   a distinct-enough failure mode that `classifyDescriptionDraftError` below
   surfaces it as a dedicated `DRAFT_BELOW_EVIDENCE_THRESHOLD` kind so the
   DescriptionDraftsScreen can render specific copy rather than a raw 422.
--------------------------------------------------------------------------- */

const ASSET_DESCRIPTION_DRAFT_BATCH_LIMIT = 100;

export interface AssetDescriptionDraftListQuery {
  status?: string;
  tableId?: string;
  limit?: number;
  cursor?: string;
}

export type DescriptionDraftErrorKind =
  | "DRAFT_BELOW_EVIDENCE_THRESHOLD"
  | "DRAFT_NOT_FOUND"
  | "DRAFT_NOT_SUBMITTABLE"
  | "UNAUTHORIZED"
  | "SERVER_ERROR"
  | "UNKNOWN";

export interface DescriptionDraftError {
  kind: DescriptionDraftErrorKind;
  status: number;
  detail: string;
}

const DRAFT_BELOW_EVIDENCE_DETAIL =
  "draft carries too little evidence for independent review";

/** Maps an `ApiError` from any of the three description-draft endpoints to a
 *  discriminated kind the UI can branch copy on. Only `submit` can raise the
 *  evidence-gate 422; the other endpoints reach it via the generic branches
 *  below. */
export function classifyDescriptionDraftError(error: ApiError): DescriptionDraftError {
  const { status, detail } = error;
  if (status === 422 && detail === DRAFT_BELOW_EVIDENCE_DETAIL) {
    return { kind: "DRAFT_BELOW_EVIDENCE_THRESHOLD", status, detail };
  }
  if (status === 404) return { kind: "DRAFT_NOT_FOUND", status, detail };
  if (status === 409) return { kind: "DRAFT_NOT_SUBMITTABLE", status, detail };
  if (status === 401 || status === 403) return { kind: "UNAUTHORIZED", status, detail };
  if (status >= 500) return { kind: "SERVER_ERROR", status, detail };
  return { kind: "UNKNOWN", status, detail };
}

/** `POST /v1/organizations/{org}/asset-description-drafts/generate` (see
 *  `generate_asset_description_drafts` in asset_description_api.py). Server
 *  silently truncates a >100 table_ids batch; enforce the same limit here
 *  and reject up-front with a synthetic 400 ApiError so the UI does not have
 *  to guess why a subset came back. */
export async function generateAssetDescriptionDrafts(
  organizationId: string,
  tableIds: string[],
  signal?: AbortSignal,
): Promise<AssetDescriptionDraftGenerateResponse> {
  if (tableIds.length === 0) {
    throw new ApiError(400, "at least one table_id is required");
  }
  if (tableIds.length > ASSET_DESCRIPTION_DRAFT_BATCH_LIMIT) {
    throw new ApiError(
      400,
      `at most ${ASSET_DESCRIPTION_DRAFT_BATCH_LIMIT} tables can be drafted in one batch`,
    );
  }
  const page = await postJson<Page>(
    `/v1/organizations/${organizationId}/asset-description-drafts/generate`,
    { table_ids: tableIds },
    signal,
  );
  return {
    drafts: (page.items as AssetDescriptionDraftRead[]) ?? [],
    limit: page.limit,
    offset: page.offset,
    total: page.total,
  };
}

/** `GET /v1/organizations/{org}/asset-description-drafts` (see
 *  `list_asset_description_drafts`). The server orders by
 *  `overall_score DESC, created_at DESC` -- that is the reviewer-priority
 *  order the DescriptionDraftsScreen defaults to, so no `order_by` param is
 *  exposed here. `tableId` is a client-side convenience filter: the server
 *  has no `table_id` query param on this route, so pass-through is a no-op
 *  and callers filter locally. `cursor` here is the string form of the
 *  next `offset` — the server's `Page` shape is offset-based and has no
 *  opaque cursor of its own; the response's `next_cursor` is derived from
 *  `offset + limit < total`. */
export async function listAssetDescriptionDrafts(
  organizationId: string,
  filters: AssetDescriptionDraftListQuery = {},
  signal?: AbortSignal,
): Promise<AssetDescriptionDraftListResponse & { next_cursor?: string }> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (typeof filters.limit === "number") params.set("limit", String(filters.limit));
  if (filters.cursor) params.set("offset", filters.cursor);
  const qs = params.toString();
  const path = qs
    ? `/v1/organizations/${organizationId}/asset-description-drafts?${qs}`
    : `/v1/organizations/${organizationId}/asset-description-drafts`;
  const page = await get<Page>(path, signal);
  const drafts = (page.items as AssetDescriptionDraftRead[]) ?? [];
  const nextOffset = page.offset + page.limit;
  const hasMore = typeof page.total === "number" && nextOffset < page.total;
  return {
    drafts,
    limit: page.limit,
    offset: page.offset,
    total: page.total,
    ...(hasMore ? { next_cursor: String(nextOffset) } : {}),
  };
}

/** `POST /v1/asset-description-drafts/{draft_id}/submit` (see
 *  `submit_asset_description_draft`). Server responds 202 Accepted with the
 *  freshly-created `GovernanceReview`; the DescriptionDraftsScreen only
 *  needs to know the draft flipped to PENDING_APPROVAL, so this refetches
 *  the draft rather than returning the review. If refetch fails, an
 *  optimistically-updated `AssetDescriptionDraftRead` is synthesised from
 *  the review response so the row still flips. */
export async function submitAssetDescriptionDraft(
  draftId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return postJson<GovernanceReviewRead>(
    `/v1/asset-description-drafts/${draftId}/submit`,
    {},
    signal,
  );
}

/* ---------------------------------------------------------------------------
   P2-08: manual revoke of an ACTIVE asset certification.

   Wired to `POST /v1/tables/{table_id}/certification/revoke` -- the ONLY
   place `AssetCertification.status = "REVOKED"` is produced (before P2-08 the
   REVOKED value existed in the state machine but no code ever wrote it, so a
   revoked-by-policy certification could only be worked around by letting it
   expire).

   Auth: same roles as the certify endpoint (PlatformAdmin, MetadataAdmin,
   DataAdmin, DataSteward). Maker-checker is enforced server-side by default
   (a principal cannot revoke a certification they themselves granted); the
   flag `certification_revoke_enforce_maker_checker` toggles that for
   single-steward deployments. Server responses this call must handle at the
   UI layer:
     - 200 AssetCertificationRead .. success (`status === "REVOKED"`)
     - 404 no active certification to revoke, or table not found
     - 409 detail === "same_principal_cannot_revoke_own_certification"

   UI follow-up (not in this pass): CatalogTable.tsx should expose a
   "Revoke" button in the certification cell that opens a small dialog for
   the reason (>=10 chars) and column_id (optional), with an explicit
   confirmation copy ("This will affect downstream policy decisions."). The
   api.ts function is landed now so the follow-up UI slice is a
   copy-and-paste against an already-typed call. --------------------------- */

import type {
  AssetCertificationRead as _AssetCertificationRead_p208,
  CertificationRevokeRequest as _CertificationRevokeRequest_p208,
} from "./types";

export async function revokeAssetCertification(
  tableId: string,
  body: _CertificationRevokeRequest_p208,
  signal?: AbortSignal,
): Promise<_AssetCertificationRead_p208> {
  return postJson<_AssetCertificationRead_p208>(
    `/v1/tables/${tableId}/certification/revoke`,
    body,
    signal,
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
  edgeType?: import("./types").ParsedLineageEdgeType | null;
  minConfidence?: number | null;
  limit?: number;
  offset?: number;
}

export async function listParsedLineageReviewQueue(
  query: ParsedLineageReviewQueueQuery,
  signal?: AbortSignal,
): Promise<import("./types").ParsedLineageEdgeReviewQueueRead> {
  const params = new URLSearchParams();
  if (query.edgeType) params.set("edge_type", query.edgeType);
  if (query.minConfidence != null)
    params.set("min_confidence", String(query.minConfidence));
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<import("./types").ParsedLineageEdgeReviewQueueRead>(
    `/v1/lineage/parsed-edges/review-queue?${params}`,
    signal,
  );
}

/** `POST /v1/lineage/parsed-edges/{edge_id}/decision` -- maker-checker
 *  approve/reject of one PROPOSED parsed lineage edge. A reason is
 *  required by the schema; callers should collect one before posting. */
export async function decideParsedLineageEdge(
  edgeId: string,
  body: import("./types").ParsedLineageEdgeDecisionRequest,
  signal?: AbortSignal,
): Promise<import("./types").ParsedLineageEdgeDecisionRead> {
  return postJson<import("./types").ParsedLineageEdgeDecisionRead>(
    `/v1/lineage/parsed-edges/${edgeId}/decision`,
    body,
    signal,
  );
}

/** `POST /v1/lineage/parsed-edges/bulk-decide` -- up to 100 edges per call.
 *  A per-item failure marks that item FAILED in the response and the rest
 *  still commit (partial-success, per-item SAVEPOINT semantics). */
export async function bulkDecideParsedLineageEdges(
  body: import("./types").ParsedLineageEdgeBulkDecisionRequest,
  signal?: AbortSignal,
): Promise<import("./types").ParsedLineageEdgeBulkDecisionResultRead> {
  return postJson<import("./types").ParsedLineageEdgeBulkDecisionResultRead>(
    `/v1/lineage/parsed-edges/bulk-decide`,
    body,
    signal,
  );
}


// -------------------------------------------------------------------------
// P2-07: OwnershipAssignment re-affirmation (`reaffirm`, `bulk-reaffirm`) +
// the "expiring soon" banner listing that the OwnershipExpiryBanner screen
// reads. The types are declared inline (rather than in `./types`) because
// they are a small P2-07-specific surface; if a second screen consumes them
// they should move to `./types` on the next codegen pass.
// -------------------------------------------------------------------------

/** Server row shape. Mirrors `aida.schemas.OwnershipAssignmentRead`. */
export interface OwnershipAssignmentRead {
  id: string;
  organization_id: string;
  subject_type: string;
  subject_id: string;
  owner_type: string;
  owner_principal: string;
  assignment_kind: string;
  source_rule_id: string | null;
  status: string;
  assigned_by: string;
  expires_at: string | null;
  expiry_warning_emitted_at: string | null;
  reaffirmed_at: string | null;
  reaffirmed_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface OwnershipAssignmentBulkReaffirmItemResult {
  assignment_id: string;
  outcome: "REAFFIRMED" | "NOT_FOUND" | "FORBIDDEN" | "ERROR";
  detail: string | null;
}

export interface OwnershipAssignmentBulkReaffirmResult {
  reaffirmed: number;
  skipped: number;
  items: OwnershipAssignmentBulkReaffirmItemResult[];
}

/** `POST /v1/ownership-assignments/{id}/reaffirm` -- the owner (or admin)
 *  reaffirms one ACTIVE assignment. Extends `expires_at` by
 *  `settings.ownership_reaffirm_days` (default 180). */
export async function reaffirmOwnershipAssignment(
  assignmentId: string,
  signal?: AbortSignal,
): Promise<OwnershipAssignmentRead> {
  return postJson<OwnershipAssignmentRead>(
    `/v1/ownership-assignments/${assignmentId}/reaffirm`,
    {},
    signal,
  );
}

/** `POST /v1/ownership-assignments/bulk-reaffirm` -- up to 100 ids per call
 *  with per-item SAVEPOINT semantics (one failure does not block the rest). */
export async function bulkReaffirmOwnershipAssignments(
  assignmentIds: string[],
  signal?: AbortSignal,
): Promise<OwnershipAssignmentBulkReaffirmResult> {
  return postJson<OwnershipAssignmentBulkReaffirmResult>(
    `/v1/ownership-assignments/bulk-reaffirm`,
    { assignment_ids: assignmentIds },
    signal,
  );
}

export interface OwnershipAssignmentListQuery {
  subject_type?: string | null;
  subject_id?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/organizations/{organization_id}/ownership-assignments` --
 *  ACTIVE-only paginated listing. Used by the P2-07 "expiring soon" banner
 *  which client-side filters to rows whose `expires_at` lies inside
 *  `warn_days` and whose `owner_principal` matches the current principal. */
export async function fetchOwnershipAssignments(
  organizationId: string,
  query: OwnershipAssignmentListQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<OwnershipAssignmentRead>> {
  const params = new URLSearchParams();
  if (query.subject_type) params.set("subject_type", query.subject_type);
  if (query.subject_id) params.set("subject_id", query.subject_id);
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  return get<PageOf<OwnershipAssignmentRead>>(
    `/v1/organizations/${organizationId}/ownership-assignments?${params}`,
    signal,
  );
}

/**
 * `GET /v1/organizations/{org}/agent-inbox` (agent_contract_api.py) — the
 * one screen a supervisor opens: what their agents did, and what is waiting
 * on a human. Composed server-side in a fixed number of queries, so this is
 * a single call rather than the five the screen would otherwise make.
 */
export async function fetchAgentInbox(
  organizationId: string,
  persona: string,
  signal?: AbortSignal,
): Promise<AgentInboxRead> {
  if (USE_FIXTURES) return makeFixtureAgentInbox(organizationId, persona);
  return get<AgentInboxRead>(
    `/v1/organizations/${organizationId}/agent-inbox?persona=${encodeURIComponent(persona)}`,
    signal,
  );
}

/**
 * `POST .../agents/{version}/contract/kill` — engage one agent's kill switch.
 * Takes effect on that agent's very next run: the orchestrator queries the
 * switch live rather than caching it. Fixture mode refuses rather than
 * pretending, because a kill switch that silently did nothing is the worst
 * possible thing to mock.
 */
export async function engageAgentKillSwitch(
  organizationId: string,
  versionId: string,
  reason: string,
): Promise<void> {
  if (USE_FIXTURES) {
    throw new Error("Kill switch is unavailable in fixture mode — run against the API.");
  }
  await postJson<unknown>(
    `/v1/organizations/${organizationId}/agents/${versionId}/contract/kill`,
    { reason },
  );
}
