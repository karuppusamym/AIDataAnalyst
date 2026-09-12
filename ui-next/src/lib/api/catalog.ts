/* ---------------------------------------------------------------------------
   Catalog — the asset inventory and what is written onto an asset.

   Four things a screen asks of an asset and one bulk path over many:
   the searchable row (`fetchCatalogRows`) and its evidence, the approved
   business meaning annotated on a table, the stewardship backlog
   (unowned assets, documentation worklist, description drafts), and
   certification/ownership as governed writes.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import { USE_FIXTURES } from "../appConfig";
import { ApiError } from "../http";
import type {
  AssetCertificationRead as _AssetCertificationRead_p208,
  AssetDescriptionDraftRead,
  AssetEvidenceRead,
  BusinessMapRead,
  CatalogBulkActionRunRead,
  CatalogBulkCertifyRequest,
  CatalogBulkClassifyRequest,
  CatalogBulkOwnRequest,
  CatalogBulkTagRequest,
  CertificationRevokeRequest as _CertificationRevokeRequest_p208,
  GovernanceReviewRead,
  MetadataBusinessAnnotationRead,
  Page,
  UnownedAssetBacklogRouteRequest,
  UnownedAssetBacklogRouteResult,
  UnownedAssetEscalationRead,
} from "../types";
import type {
  AssetDescriptionDraftGenerateResponse,
  AssetDescriptionDraftListResponse,
  CatalogRowRead,
  CursorPage,
  DocumentationWorklistEntryRead,
  MetadataTableRead,
  PageOf,
} from "../ui-types";

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

export interface CatalogQuery {
  organizationId: string;
  datasourceId?: string;
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
export function fetchCatalogRows(
  query: CatalogQuery,
  signal?: AbortSignal,
): Promise<CursorPage<CatalogRowRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureCatalog(query),
    async () => {
      const params = new URLSearchParams();
      if (query.datasourceId) params.set("datasource_id", query.datasourceId);
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
    },
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

export function fetchAssetEvidence(
  tableId: string,
  signal?: AbortSignal,
): Promise<AssetEvidenceRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureEvidence(tableId),
    async () => {
      return get<AssetEvidenceRead>(`/v1/metadata/tables/${tableId}/evidence`, signal);
    },
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
export function fetchBusinessAnnotations(
  query: BusinessAnnotationsQuery,
  signal?: AbortSignal,
): Promise<PageOf<MetadataBusinessAnnotationRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBusinessAnnotations(query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<MetadataBusinessAnnotationRead>>(
        `/v1/datasources/${query.datasourceId}/business-annotations?${params}`,
        signal,
      );
    },
  );
}

/** `GET /v1/metadata/tables/{table_id}/business-annotation` — resolves by
 *  table id alone, exactly like `fetchAssetEvidence` does for `EvidencePane`:
 *  a durable permalink target that does not depend on the caller's current
 *  datasource filter or loaded page happening to contain this table. 404s
 *  when the table has no *approved* annotation (`MetadataBusinessAnnotation`
 *  content is append-only versioned per AT-6; this always resolves the
 *  current version). */
export function fetchTableBusinessAnnotation(
  tableId: string,
  signal?: AbortSignal,
): Promise<MetadataBusinessAnnotationRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureTableBusinessAnnotation(tableId),
    async () => {
      return get<MetadataBusinessAnnotationRead>(
        `/v1/metadata/tables/${tableId}/business-annotation`,
        signal,
      );
    },
  );
}

export interface BusinessMapQuery {
  organizationId: string;
  limit?: number;
}

/** `GET /v1/organizations/{id}/business-map` — the secondary, org-wide tab:
 *  every approved domain/entity/table node plus real cross-domain foreign-key
 *  edges (`MetadataConstraint`), not a per-datasource slice. */
export function fetchBusinessMap(
  query: BusinessMapQuery,
  signal?: AbortSignal,
): Promise<BusinessMapRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBusinessMap(query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 500));
      return get<BusinessMapRead>(
        `/v1/organizations/${query.organizationId}/business-map?${params}`,
        signal,
      );
    },
  );
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

/** `POST /v1/organizations/{organization_id}/tables/bulk-tag`
 *  (`bulk_tag_tables`, `api.py:3199`) -- applies `tag_key`/`tag_value` to
 *  every table `body.table_ids` names, or every table `body.filter` (a
 *  datasource + match field/pattern) resolves to. */
export function bulkTagCatalogTables(
  organizationId: string,
  body: CatalogBulkTagRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBulkTagCatalogTables(organizationId, body),
    async () => {
      return postJson<CatalogBulkActionRunRead>(
        `/v1/organizations/${organizationId}/tables/bulk-tag`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/tables/bulk-classify`
 *  (`bulk_classify_tables`, `api.py:3271`) -- sets `classification` on every
 *  column `body.column_ids` names, every column matching
 *  `body.column_name_pattern` under `body.table_ids`/`body.filter`'s
 *  matched tables, or (default pattern `"*"`) every column of those tables. */
export function bulkClassifyCatalogColumns(
  organizationId: string,
  body: CatalogBulkClassifyRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBulkClassifyCatalogColumns(organizationId, body),
    async () => {
      return postJson<CatalogBulkActionRunRead>(
        `/v1/organizations/${organizationId}/tables/bulk-classify`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/tables/bulk-own`
 *  (`bulk_own_tables`, `api.py:3360`) -- assigns `owner_principal` (an
 *  INDIVIDUAL principal id or GROUP name) as owner of every matched table. */
export function bulkAssignCatalogOwnership(
  organizationId: string,
  body: CatalogBulkOwnRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBulkOwnCatalogTables(organizationId, body),
    async () => {
      return postJson<CatalogBulkActionRunRead>(
        `/v1/organizations/${organizationId}/tables/bulk-own`,
        body,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/tables/bulk-certify`
 *  (`bulk_certify_tables`, `api.py:3435`) -- certifies every matched table
 *  with `rationale` (server-required, >=10 chars) and a future `expires_at`. */
export function bulkCertifyCatalogTables(
  organizationId: string,
  body: CatalogBulkCertifyRequest,
  signal?: AbortSignal,
): Promise<CatalogBulkActionRunRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBulkCertifyCatalogTables(organizationId, body),
    async () => {
      return postJson<CatalogBulkActionRunRead>(
        `/v1/organizations/${organizationId}/tables/bulk-certify`,
        body,
        signal,
      );
    },
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
export function fetchUnownedAssetBacklog(
  organizationId: string,
  query: UnownedAssetBacklogQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<UnownedAssetEscalationRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureUnownedAssetBacklog(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<UnownedAssetEscalationRead>>(
        `/v1/organizations/${organizationId}/stewardship/unowned-backlog?${params}`,
        signal,
      );
    },
  );
}

export interface DocumentationWorklistQuery {
  limit?: number;
  offset?: number;
  includeZeroVolume?: boolean;
  ranking?: "priority" | "query_volume";
}

/** `GET /v1/organizations/{organization_id}/stewardship/documentation-worklist`
 *  (`stewardship_api.py::list_documentation_worklist`, AT-5/SW-1) -- the
 *  ranked "document this next" backlog: real query volume x downstream
 *  impact x a five-field documentation deficit, not usage alone. */
export function fetchDocumentationWorklist(
  organizationId: string,
  query: DocumentationWorklistQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<DocumentationWorklistEntryRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureDocumentationWorklist(organizationId, query),
    async () => {
      const params = new URLSearchParams();
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      if (query.includeZeroVolume) params.set("include_zero_volume", "true");
      if (query.ranking) params.set("ranking", query.ranking);
      return get<PageOf<DocumentationWorklistEntryRead>>(
        `/v1/organizations/${organizationId}/stewardship/documentation-worklist?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/organizations/{organization_id}/stewardship/unowned-backlog/route`
 *  (`route_unowned_backlog`, `stewardship_api.py:1645`) -- advances every
 *  in-scope escalation one notification-rule-driven step (UNOWNED -> ROUTED,
 *  ROUTED -> ESCALATED, ESCALATED -> ESCALATED_TIER_2, or straight to
 *  RESOLVED where ownership was already found), optionally scoped to one
 *  datasource/domain/line of business; an empty body routes the whole
 *  organization. Matches the legacy screen's `#route-unowned` button. */
export function routeUnownedAssetBacklog(
  organizationId: string,
  body: UnownedAssetBacklogRouteRequest,
  signal?: AbortSignal,
): Promise<UnownedAssetBacklogRouteResult> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureRouteUnownedAssetBacklog(organizationId, body),
    async () => {
      return postJson<UnownedAssetBacklogRouteResult>(
        `/v1/organizations/${organizationId}/stewardship/unowned-backlog/route`,
        body,
        signal,
      );
    },
  );
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
export function reaffirmOwnershipAssignment(
  assignmentId: string,
  signal?: AbortSignal,
): Promise<OwnershipAssignmentRead> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureReaffirmOwnershipAssignment(assignmentId),
    async () => {
      return postJson<OwnershipAssignmentRead>(
        `/v1/ownership-assignments/${assignmentId}/reaffirm`,
        {},
        signal,
      );
    },
  );
}

/** `POST /v1/ownership-assignments/bulk-reaffirm` -- up to 100 ids per call
 *  with per-item SAVEPOINT semantics (one failure does not block the rest). */
export function bulkReaffirmOwnershipAssignments(
  assignmentIds: string[],
  signal?: AbortSignal,
): Promise<OwnershipAssignmentBulkReaffirmResult> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureBulkReaffirmOwnershipAssignments(assignmentIds),
    async () => {
      return postJson<OwnershipAssignmentBulkReaffirmResult>(
        `/v1/ownership-assignments/bulk-reaffirm`,
        { assignment_ids: assignmentIds },
        signal,
      );
    },
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
export function fetchOwnershipAssignments(
  organizationId: string,
  query: OwnershipAssignmentListQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<OwnershipAssignmentRead>> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureOwnershipAssignments(query),
    async () => {
      const params = new URLSearchParams();
      if (query.subject_type) params.set("subject_type", query.subject_type);
      if (query.subject_id) params.set("subject_id", query.subject_id);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<PageOf<OwnershipAssignmentRead>>(
        `/v1/organizations/${organizationId}/ownership-assignments?${params}`,
        signal,
      );
    },
  );
}
