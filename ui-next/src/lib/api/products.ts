/* ---------------------------------------------------------------------------
   Products — the data and context assets a consumer discovers, requests and
   binds to.

   The marketplace listing and its access request, portfolio analytics over
   the published estate, and Context Products end to end: create, submit,
   compile, version, bind to a scope, download the compiled artifact and
   read back what consumed it.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { deleteRequest, demoOr, get, postJson, putJson } from "./transport";
import { USE_FIXTURES } from "../appConfig";
import {
  makeFixtureCompileContextProductVersion,
  makeFixtureConsumptionRecords,
  makeFixtureContextProductBindings,
  makeFixtureContextProductVersions,
  makeFixtureContextProducts,
  makeFixtureCreateContextProduct,
  makeFixtureDeprecateContextProductVersion,
  makeFixtureMarketplaceAccessRequest,
  makeFixtureMarketplaceProducts,
  makeFixturePortfolioAnalyticsSummary,
  makeFixturePortfolioAnalyticsTrends,
  makeFixtureRemoveContextProductBinding,
  makeFixtureSetContextProductBinding,
  makeFixtureSubmitContextProductVersion,
} from "../fixtures";
import { requestBlob } from "../http";
import type {
  ConsumptionRecordPage,
  ContextCompilationRead,
  ContextProductConsumerBindingRead,
  ContextProductCreate,
  ContextProductRead,
  ContextProductScopeRead,
  ContextProductVersionRead,
  GovernanceReviewRead,
  MarketplaceAccessRequestCreate,
  MarketplaceAccessRequestRead,
  PortfolioAnalyticsSummaryRead,
  PortfolioAnalyticsTrendsRead,
} from "../types";
import type { MarketplaceProductRead, PageOf } from "../ui-types";

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
export function fetchMarketplaceProducts(
  query: MarketplaceQuery,
  signal?: AbortSignal,
): Promise<PageOf<MarketplaceProductRead>> {
  return demoOr(
    async () => makeFixtureMarketplaceProducts(query),
    async () => {
      const params = new URLSearchParams();
      if (query.q) params.set("q", query.q);
      if (query.domain) params.set("domain", query.domain);
      if (query.classification) params.set("classification", query.classification);
      params.set("sort", query.sort ?? "personalized");
      params.set("limit", String(query.limit ?? 50));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<MarketplaceProductRead>>(`/v1/marketplace/products?${params}`, signal);
    },
  );
}

/** `POST /v1/marketplace/products/{version_id}/access-requests` — a
 *  governed, maker-checker access request (the same route the native MCP
 *  tool `request_marketplace_access` calls). */
export function requestMarketplaceAccess(
  versionId: string,
  body: MarketplaceAccessRequestCreate,
  signal?: AbortSignal,
): Promise<MarketplaceAccessRequestRead> {
  return demoOr(
    async () => makeFixtureMarketplaceAccessRequest(versionId, body),
    async () => {
      return postJson<MarketplaceAccessRequestRead>(
        `/v1/marketplace/products/${versionId}/access-requests`,
        body,
        signal,
      );
    },
  );
}

export interface PortfolioAnalyticsSummaryQuery {
  organizationId: string;
  windowDays?: number;
  lowQualityThreshold?: number;
  topProductsLimit?: number;
}

/** `GET /v1/organizations/{organization_id}/portfolio-analytics/summary`
 *  (`product_marketplace_api.py::portfolio_analytics_summary`) — the org-wide
 *  operator dashboard over the marketplace: lifecycle counts, the
 *  access-request funnel, usage, quality and review-queue depth, plus a
 *  ranked `top_products` list, all as of `generated_at`. */
export function fetchPortfolioAnalyticsSummary(
  query: PortfolioAnalyticsSummaryQuery,
  signal?: AbortSignal,
): Promise<PortfolioAnalyticsSummaryRead> {
  return demoOr(
    async () => makeFixturePortfolioAnalyticsSummary(query),
    async () => {
      const params = new URLSearchParams();
      params.set("window_days", String(query.windowDays ?? 30));
      params.set("low_quality_threshold", String(query.lowQualityThreshold ?? 80));
      params.set("top_products_limit", String(query.topProductsLimit ?? 10));
      return get<PortfolioAnalyticsSummaryRead>(
        `/v1/organizations/${query.organizationId}/portfolio-analytics/summary?${params}`,
        signal,
      );
    },
  );
}

export interface PortfolioAnalyticsTrendsQuery {
  organizationId: string;
  windowDays?: number;
  bucketDays?: number;
}

/** `GET /v1/organizations/{organization_id}/portfolio-analytics/trends`
 *  (`product_marketplace_api.py::portfolio_analytics_trends`) — the same
 *  window bucketed into `bucket_days`-wide points, oldest first, for the
 *  dashboard's trend panel. A separate call from the summary above so one
 *  failing does not blank the other. */
export function fetchPortfolioAnalyticsTrends(
  query: PortfolioAnalyticsTrendsQuery,
  signal?: AbortSignal,
): Promise<PortfolioAnalyticsTrendsRead> {
  return demoOr(
    async () => makeFixturePortfolioAnalyticsTrends(query),
    async () => {
      const params = new URLSearchParams();
      params.set("window_days", String(query.windowDays ?? 30));
      params.set("bucket_days", String(query.bucketDays ?? 7));
      return get<PortfolioAnalyticsTrendsRead>(
        `/v1/organizations/${query.organizationId}/portfolio-analytics/trends?${params}`,
        signal,
      );
    },
  );
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

export interface ContextProductQuery {
  limit?: number;
  offset?: number;
}

/** `GET /v1/projects/{project_id}/context-products` (`list_context_products`,
 *  `context_product_api.py:381`) — one row per product at its latest
 *  version, exactly what `ContextProductRead.latest_version` carries;
 *  matches `loadContextProducts()`'s call in the legacy screen. */
export function fetchContextProducts(
  projectId: string,
  query: ContextProductQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<ContextProductRead>> {
  return demoOr(
    async () => makeFixtureContextProducts(projectId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 200));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ContextProductRead>>(
        `/v1/projects/${projectId}/context-products?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/projects/{project_id}/context-products` (`create_context_product`,
 *  `context_product_api.py:308`) — creates the product and its version-1
 *  DRAFT in one call, matching `createContextProduct()` in the legacy
 *  screen. Every referenced table/semantic/glossary/tool version id is
 *  server-validated against that same project's approved, PUBLISHED
 *  versions (`validate_context_product_references`); an unresolved id comes
 *  back as this endpoint's own 4xx detail string. */
export function createContextProduct(
  projectId: string,
  body: ContextProductCreate,
  signal?: AbortSignal,
): Promise<ContextProductRead> {
  return demoOr(
    async () => makeFixtureCreateContextProduct(projectId, body),
    async () => {
      return postJson<ContextProductRead>(`/v1/projects/${projectId}/context-products`, body, signal);
    },
  );
}

/** `POST /v1/context-product-versions/{id}/submit` (`submit_context_product_version`,
 *  `context_product_api.py:826`) — moves a DRAFT version to REVIEW_REQUIRED
 *  and opens the same `GovernanceReview` `ReviewQueueScreen` reads; matches
 *  the legacy screen's `data-context-submit` action. */
export function submitContextProductVersion(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    async () => makeFixtureSubmitContextProductVersion(versionId),
    async () => {
      return postJson<GovernanceReviewRead>(`/v1/context-product-versions/${versionId}/submit`, {}, signal);
    },
  );
}

/** `POST /v1/context-product-versions/{id}/deprecate` (`request_context_product_deprecation`,
 *  `context_product_api.py:887`) — requests retirement review for a
 *  PUBLISHED (or SUPPORTED) version; matches the legacy screen's
 *  `data-context-deprecate` action. */
export function requestContextProductDeprecation(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return demoOr(
    async () => makeFixtureDeprecateContextProductVersion(versionId),
    async () => {
      return postJson<GovernanceReviewRead>(`/v1/context-product-versions/${versionId}/deprecate`, {}, signal);
    },
  );
}

/** `GET /v1/context-product-versions/{id}/compile` (`compile_context_product_version`,
 *  `context_compiler_api.py:208`) — deterministic compilation of one
 *  immutable version to a target format; matches the legacy screen's
 *  `compileVersion()` (`data-context-compile`). Repeating this call against
 *  the same version and target reproduces the same `artifact_hash`. */
export function compileContextProductVersion(
  versionId: string,
  target: string,
  signal?: AbortSignal,
): Promise<ContextCompilationRead> {
  return demoOr(
    async () => makeFixtureCompileContextProductVersion(versionId, target),
    async () => {
      const params = new URLSearchParams({ target });
      return get<ContextCompilationRead>(
        `/v1/context-product-versions/${versionId}/compile?${params}`,
        signal,
      );
    },
  );
}

/* ---------------------------------------------------------------------------
   Context product staged rollout, scope, and agent consumption (P3-01).

   These are the routes that made a Context Product publishable but not
   *operable* from the UI: a steward could create, submit and compile a
   version, then had no way to say which consumer gets which version, no way
   to see the domain scope the package actually spans, no way to hand an
   agent the compiled artifact as a file, and no way to see what any agent
   had consumed. The endpoints all shipped; only the client did not.

     - GET    /v1/context-products/{id}/versions                       list_context_product_versions
     - GET    /v1/context-products/{id}/bindings                       list_context_product_consumer_bindings
     - PUT    /v1/context-products/{id}/bindings/{consumer}            set_context_product_consumer_binding
     - DELETE /v1/context-products/{id}/bindings/{consumer}            delete_context_product_consumer_binding
     - GET    /v1/context-product-versions/{id}/scope                  get_context_product_scope
     - GET    /v1/context-product-versions/{id}/compile/download       download_compiled_context_product
     - GET    /v1/organizations/{org}/consumption-lineage/by-consumer  list_consumption_by_consumer
     - GET    /v1/organizations/{org}/consumption-lineage/graph        list_consumption_graph
     - GET    /v1/organizations/{org}/consumption-lineage/by-resource  list_consumption_for_resource

   The three consumption-lineage routes are read against `/v1`. They were
   mounted only under `/api/v1` until `consumption_lineage_api.py` made `/v1`
   canonical; the old prefix still answers as a deprecated alias, but new
   callers should not learn it.
--------------------------------------------------------------------------- */

/** `GET /v1/context-products/{product_id}/versions` — the full version
 *  history behind `ContextProductRead.latest_version`. A staged rollout needs
 *  this: you cannot pin a consumer to v1 while v2 publishes if the only
 *  version the client can name is the latest one. */
export function fetchContextProductVersions(
  productId: string,
  query: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<ContextProductVersionRead>> {
  return demoOr(
    async () => makeFixtureContextProductVersions(productId),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ContextProductVersionRead>>(
        `/v1/context-products/${productId}/versions?${params}`,
        signal,
      );
    },
  );
}

/** `GET /v1/context-products/{product_id}/bindings` — who is pinned to which
 *  version. A consumer with no binding resolves to the product's published
 *  version, so an empty list is the normal state, not an error. */
export function fetchContextProductBindings(
  productId: string,
  query: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<ContextProductConsumerBindingRead>> {
  return demoOr(
    async () => makeFixtureContextProductBindings(productId),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<ContextProductConsumerBindingRead>>(
        `/v1/context-products/${productId}/bindings?${params}`,
        signal,
      );
    },
  );
}

/** `PUT /v1/context-products/{product_id}/bindings/{consumer_principal_id}`
 *  — pin or move one named consumer onto one version. Idempotent by
 *  (product, consumer): calling it again moves the existing binding rather
 *  than creating a second one. 422 if the version belongs to another product. */
export async function setContextProductBinding(
  productId: string,
  consumerPrincipalId: string,
  boundVersionId: string,
  signal?: AbortSignal,
): Promise<ContextProductConsumerBindingRead> {
  if (USE_FIXTURES)
    return makeFixtureSetContextProductBinding(productId, consumerPrincipalId, boundVersionId);
  return putJson<ContextProductConsumerBindingRead>(
    `/v1/context-products/${productId}/bindings/${encodeURIComponent(consumerPrincipalId)}`,
    { bound_version_id: boundVersionId },
    signal,
  );
}

/** `DELETE /v1/context-products/{product_id}/bindings/{consumer_principal_id}`
 *  — 204. Unpinning returns the consumer to the published version; it does
 *  not revoke their access, which is governed by `allowed_consumer_roles`. */
export function removeContextProductBinding(
  productId: string,
  consumerPrincipalId: string,
  signal?: AbortSignal,
): Promise<void> {
  return demoOr(
    async () => makeFixtureRemoveContextProductBinding(productId, consumerPrincipalId),
    async () => {
      return deleteRequest(
        `/v1/context-products/${productId}/bindings/${encodeURIComponent(consumerPrincipalId)}`,
        signal,
      );
    },
  );
}

/** `GET /v1/context-product-versions/{version_id}/scope` — both ADR-0017 SS9
 *  tenancy axes for one version: which data domains it spans, which of those
 *  it has no cross-boundary grant for, and which table ids did not resolve.
 *  This is the honest answer to "is this package safe to publish". */
export async function fetchContextProductScope(
  versionId: string,
  signal?: AbortSignal,
): Promise<ContextProductScopeRead> {
  return get<ContextProductScopeRead>(
    `/v1/context-product-versions/${versionId}/scope`,
    signal,
  );
}

/** `GET /v1/context-product-versions/{version_id}/compile/download` — the
 *  same deterministic artifact `compileContextProductVersion` returns, served
 *  with a `Content-Disposition` filename so an agent developer can commit it
 *  next to their client config. Saved through a same-origin blob URL because
 *  the download needs this app's identity headers, which a bare
 *  `<a download href>` cannot send. */
export async function downloadCompiledContextProduct(
  versionId: string,
  target: string,
): Promise<void> {
  let blob: Blob;
  let filename = `context-product-${versionId}-${target.toLowerCase()}.json`;
  if (USE_FIXTURES) {
    const artifact = await compileContextProductVersion(versionId, target);
    blob = new Blob([artifact.content], { type: artifact.content_type });
    if (artifact.content_type.includes("yaml")) filename = filename.replace(/\.json$/, ".yaml");
  } else {
    const downloaded = await requestBlob(
      `/v1/context-product-versions/${versionId}/compile/download?target=${encodeURIComponent(target)}`,
    );
    blob = downloaded.blob;
    const disposition = downloaded.response.headers.get("Content-Disposition") || "";
    filename = disposition.match(/filename="([^"]+)"/)?.[1] || filename;
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface ConsumptionQuery {
  /** Filter to one consumer principal (`by-consumer`). */
  consumerId?: string;
  /** Filter to one resource (`by-resource`); both fields are required
   *  together by the server, so pass neither or both. */
  resourceType?: string;
  resourceId?: string;
  limit?: number;
  offset?: number;
}

/** The CX-4 consumption edges: one row per allowed *or refused* read that the
 *  MCP server or the Context Product REST API recorded. This is the only
 *  place the platform answers "what has this agent actually seen".
 *
 *  Routes to `by-consumer`, `by-resource` or `graph` depending on which
 *  filter the caller supplied — three endpoints with one shape, so screens
 *  do not have to pick. */
export async function fetchConsumptionRecords(
  organizationId: string,
  query: ConsumptionQuery = {},
  signal?: AbortSignal,
): Promise<ConsumptionRecordPage> {
  if (USE_FIXTURES)
    return makeFixtureConsumptionRecords(
      { consumerId: query.consumerId, resourceType: query.resourceType, resourceId: query.resourceId },
      query,
    );
  const params = new URLSearchParams();
  params.set("limit", String(query.limit ?? 100));
  params.set("offset", String(query.offset ?? 0));
  if (query.consumerId) {
    params.set("consumer_id", query.consumerId);
    return get<ConsumptionRecordPage>(
      `/v1/organizations/${organizationId}/consumption-lineage/by-consumer?${params}`,
      signal,
    );
  }
  if (query.resourceType && query.resourceId) {
    params.set("resource_type", query.resourceType);
    params.set("resource_id", query.resourceId);
    return get<ConsumptionRecordPage>(
      `/v1/organizations/${organizationId}/consumption-lineage/by-resource?${params}`,
      signal,
    );
  }
  return get<ConsumptionRecordPage>(
    `/v1/organizations/${organizationId}/consumption-lineage/graph?${params}`,
    signal,
  );
}
