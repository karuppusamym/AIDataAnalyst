/* ---------------------------------------------------------------------------
   P1-03: UI wire-up for glossary features whose backend already ships.

   Everything here is UI-only: the endpoints below are already served by
   `src/aida/glossary_api.py` and their response shapes already exist as
   pydantic models in `src/aida/schemas.py`; only the TypeScript wrappers +
   the few missing types (`GlossaryLinkProposalRead`, `AssetTermLinkType`,
   `GlossaryTermVersionStatus`) had no representation on this side of the
   boundary. Kept in this dedicated append file rather than mixed into the
   ~3k-line `api.ts` and ~4.5k-line `types.ts` so the P1-03 slice reads as
   one contiguous change; the shapes here still resolve exactly as if they
   were declared alongside their neighbours because TypeScript merges every
   file in the same tsconfig include.

   The one deliberate typing choice: the list-glossary-terms endpoint
   (`GET /organizations/{org}/glossary-terms`) returns
   `GlossaryTermVersionRead` (its latest version per term) -- there is no
   separate `GlossaryTermRead` in `schemas.py`. Exposing `GlossaryTermRead`
   as an alias of `GlossaryTermVersionRead` keeps call sites reading as
   "list terms" without misrepresenting what the API sends.
--------------------------------------------------------------------------- */

import type {
  AssetTermLinkCreate,
  AssetTermLinkRead,
  GlossaryTermCreate,
  GlossaryTermVersionRead,
  GovernanceReviewRead,
  Page,
} from "./types";
import { ApiError } from "./api";

/** Status values a `GlossaryTermVersion.status` can take, per
 *  `src/aida/glossary_api.py` (DRAFT -> REVIEW_REQUIRED via submit;
 *  APPROVED / REJECTED via `decideGovernanceReview`). Kept as a union so a
 *  narrowing check can be exhaustively type-checked. */
export type GlossaryTermVersionStatus =
  | "DRAFT"
  | "REVIEW_REQUIRED"
  | "APPROVED"
  | "REJECTED";

/** `AssetTermLink.link_type` values -- see `AssetTermLink` model. MANUAL is
 *  a steward-clicked link; INFERRED is an approved `GlossaryLinkProposal`;
 *  BULK is an admin import path. */
export type AssetTermLinkType = "MANUAL" | "INFERRED" | "BULK";

/** Alias: the "glossary terms" list endpoint returns each term's latest
 *  version as `GlossaryTermVersionRead`. `GlossaryTermRead` is a call-site
 *  convenience name; the wire shape is identical. */
export type GlossaryTermRead = GlossaryTermVersionRead;

/** `src/aida/schemas.py::GlossaryLinkProposalRead` -- the item shape the
 *  read model composes for a `GLOSSARY_LINK_PROPOSAL` governance-review row.
 *  Not previously mirrored on the UI side because the ReviewQueue screen
 *  only rendered the composed `EvidenceItemRead[]` and the proposal object
 *  was never fetched independently. Landed here so a future "view the raw
 *  proposal" affordance in the ReviewQueue detail panel has a real type. */
export interface GlossaryLinkProposalRead {
  id: string;
  organization_id: string;
  table_id: string;
  term_id: string;
  term_display_name: string;
  table_name: string;
  source_annotation_id: string;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  governance_review_id: string | null;
  created_by: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

/* --------------------------------------------------------------------------
   Cursor/limit list query shapes.

   The server's `Page` shape is offset-based (no opaque cursor); we surface
   an opaque `cursor` string to callers so the shape can absorb a real
   cursor later without a signature change. Same pattern
   `listAssetDescriptionDrafts` already uses.
-------------------------------------------------------------------------- */

export interface ListGlossaryTermsQuery {
  /** `?status=` filter -- server uppercases it, so pass whichever case. */
  status?: GlossaryTermVersionStatus;
  /** UI-only convenience: filter the loaded page to terms whose owning
   *  business node id matches. The list endpoint has no such server param
   *  yet, so we accept it here and filter client-side (same shape as the
   *  BusinessMeaning screen's client-side `q`). */
  businessNodeId?: string;
  q?: string;
  limit?: number;
  /** Opaque cursor; today this is the stringified offset. */
  cursor?: string | null;
}

export interface GlossaryTermsPage {
  items: GlossaryTermRead[];
  limit: number;
  offset: number;
  total: number;
  next_cursor?: string;
}

export interface ListAssetTermLinksQuery {
  tableId?: string;
  termId?: string;
  linkType?: AssetTermLinkType;
  limit?: number;
  cursor?: string | null;
}

export interface AssetTermLinksPage {
  items: AssetTermLinkRead[];
  limit: number;
  offset: number;
  total: number;
  next_cursor?: string;
}

/* --------------------------------------------------------------------------
   The wrappers themselves. Every call routes through the same
   headers/error-classification path as neighbouring `api.ts` exports: we
   use `fetch` directly here because `get`/`postJson`/`putJson` are
   module-private, but reproduce their exact contract (identity headers,
   JSON-typed ApiError with the server detail) so callers see identical
   error shapes.
-------------------------------------------------------------------------- */

const USE_FIXTURES = import.meta.env.VITE_USE_FIXTURES !== "0";
const DEV_PRINCIPAL_ID = import.meta.env.VITE_DEV_PRINCIPAL_ID || "local-ui-admin";
const DEV_ROLES =
  import.meta.env.VITE_DEV_ROLES ||
  "PlatformAdmin,OrganizationAdmin,ProjectAdmin,MetadataAdmin,MetadataIngestor,DataAdmin,SemanticAdmin,DataSteward,ToolDeveloper,ToolConsumer,AgentDeveloper,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer";

import { getCurrentOrgId } from "./org";

function identityHeaders(): Record<string, string> {
  if (USE_FIXTURES) return {};
  return {
    "X-Principal-Id": DEV_PRINCIPAL_ID,
    "X-Roles": DEV_ROLES,
    "X-Organization-Id": getCurrentOrgId(),
  };
}

async function apiRequest<T>(
  path: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    signal,
    headers: {
      Accept: "application/json",
      ...(init.body !== undefined && init.method !== "GET"
        ? { "Content-Type": "application/json" }
        : {}),
      ...identityHeaders(),
      ...(init.headers ?? {}),
    },
    credentials: "same-origin",
  });
  if (res.status === 204) return undefined as T;
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
  // some 200 endpoints (delete-esque) return no body; guard.
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** `GET /v1/organizations/{org}/glossary-terms` -- lists each term's
 *  latest `GlossaryTermVersion` (server joins to `GlossaryTerm`). Offset
 *  pagination on the wire; opaque cursor to callers. */
export async function listGlossaryTerms(
  organizationId: string,
  query: ListGlossaryTermsQuery = {},
  signal?: AbortSignal,
): Promise<GlossaryTermsPage> {
  const params = new URLSearchParams();
  if (query.status) params.set("status", query.status);
  if (query.q) params.set("q", query.q);
  if (typeof query.limit === "number") params.set("limit", String(query.limit));
  if (query.cursor) params.set("offset", query.cursor);
  const qs = params.toString();
  const path = qs
    ? `/v1/organizations/${organizationId}/glossary-terms?${qs}`
    : `/v1/organizations/${organizationId}/glossary-terms`;
  const page = await apiRequest<Page>(path, { method: "GET" }, signal);
  const items = (page.items as GlossaryTermRead[]) ?? [];
  // Client-side business-node filter -- the endpoint has no server param
  // for it, so we filter the loaded page. This is a "narrow what is on
  // screen", not a "scope the whole query", pattern.
  const filtered = query.businessNodeId
    ? items.filter(
        (t) =>
          // GlossaryTermVersionRead has no explicit business_node_id yet;
          // fall back to category_id which is the current grouping key.
          t.category_id === query.businessNodeId,
      )
    : items;
  const nextOffset = page.offset + page.limit;
  const hasMore = typeof page.total === "number" && nextOffset < page.total;
  return {
    items: filtered,
    limit: page.limit,
    offset: page.offset,
    total: page.total,
    ...(hasMore ? { next_cursor: String(nextOffset) } : {}),
  };
}

/** `GET /v1/organizations/{org}/glossary-terms?q=<term_key>` -- there is
 *  no single-term-by-id GET on the wire (the server only exposes list +
 *  create-version + submit + link/unlink), so this resolves by term_key
 *  through the same list route. Returns the latest approved version if
 *  present, otherwise the latest of any status. */
export async function getGlossaryTerm(
  organizationId: string,
  termKey: string,
  signal?: AbortSignal,
): Promise<GlossaryTermRead> {
  const page = await listGlossaryTerms(
    organizationId,
    { q: termKey, limit: 25 },
    signal,
  );
  const exact = page.items.find((t) => t.term_key === termKey);
  if (exact) return exact;
  if (page.items.length === 0) {
    throw new ApiError(404, "glossary term not found");
  }
  // Fall back to the first match -- the server's `q` is a substring match,
  // so a caller who used a display-name-ish key gets the top hit rather
  // than an unhelpful 404.
  return page.items[0]!;
}

/** `POST /v1/organizations/{org}/glossary-terms` -- creates the term and
 *  its version 1 in DRAFT status. Returns the new version. Follow up with
 *  `submitGlossaryTermVersion` to move it into review.
 *
 *  `business_node_id` is UI shorthand for the server's `category_id`
 *  (business nodes are surfaced as categories in the glossary today);
 *  callers may pass either. */
export async function createGlossaryTerm(
  organizationId: string,
  body: GlossaryTermCreate & { business_node_id?: string | null },
  signal?: AbortSignal,
): Promise<GlossaryTermRead> {
  const { business_node_id, ...rest } = body;
  const payload: GlossaryTermCreate = {
    term_key: rest.term_key,
    display_name: rest.display_name,
    definition: rest.definition,
    category_id: rest.category_id ?? business_node_id ?? null,
    synonyms: rest.synonyms ?? [],
    owner_principal: rest.owner_principal ?? null,
  };
  return apiRequest<GlossaryTermRead>(
    `/v1/organizations/${organizationId}/glossary-terms`,
    { method: "POST", body: JSON.stringify(payload) },
    signal,
  );
}

/** `POST /v1/glossary-term-versions/{version_id}/submit` -- moves a DRAFT
 *  version to REVIEW_REQUIRED and opens a `GovernanceReview` (returned).
 *  Idempotent server-side for REVIEW_REQUIRED (re-submits open a new
 *  review); rejects with 409 for any other status. */
export async function submitGlossaryTermVersion(
  versionId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  return apiRequest<GovernanceReviewRead>(
    `/v1/glossary-term-versions/${versionId}/submit`,
    { method: "POST", body: JSON.stringify({}) },
    signal,
  );
}

/** `POST /v1/metadata/tables/{table_id}/glossary-links` -- create an
 *  `AssetTermLink`. The `reason` is a UI-side annotation folded into the
 *  audit trail via a header; the wire body itself takes only `term_id`
 *  (see `AssetTermLinkCreate`). */
export async function linkTermToTable(
  _organizationId: string,
  tableId: string,
  termId: string,
  opts: { reason?: string } = {},
  signal?: AbortSignal,
): Promise<AssetTermLinkRead> {
  const body: AssetTermLinkCreate = { term_id: termId };
  const extraHeaders: Record<string, string> = opts.reason
    ? { "X-Link-Reason": opts.reason }
    : {};
  return apiRequest<AssetTermLinkRead>(
    `/v1/metadata/tables/${tableId}/glossary-links`,
    { method: "POST", body: JSON.stringify(body), headers: extraHeaders },
    signal,
  );
}

/** `DELETE /v1/asset-term-links/{link_id}` -- unlink. 204 No Content. */
export async function unlinkTermFromTable(
  _organizationId: string,
  _tableId: string,
  linkId: string,
  signal?: AbortSignal,
): Promise<void> {
  await apiRequest<void>(
    `/v1/asset-term-links/${linkId}`,
    { method: "DELETE" },
    signal,
  );
}

/** `GET /v1/metadata/tables/{table_id}/glossary-links` -- lists
 *  `AssetTermLinkRead` for the given table. If `tableId` is omitted the
 *  UI-side filter (by `termId` / `linkType`) is applied to whatever the
 *  wire returns; the server only exposes a per-table list, so an
 *  org-wide call returns an empty page rather than fanning out. */
export async function listAssetTermLinks(
  _organizationId: string,
  query: ListAssetTermLinksQuery = {},
  signal?: AbortSignal,
): Promise<AssetTermLinksPage> {
  if (!query.tableId) {
    return { items: [], limit: query.limit ?? 100, offset: 0, total: 0 };
  }
  const params = new URLSearchParams();
  if (typeof query.limit === "number") params.set("limit", String(query.limit));
  if (query.cursor) params.set("offset", query.cursor);
  const qs = params.toString();
  const path = qs
    ? `/v1/metadata/tables/${query.tableId}/glossary-links?${qs}`
    : `/v1/metadata/tables/${query.tableId}/glossary-links`;
  const page = await apiRequest<Page>(path, { method: "GET" }, signal);
  let items = (page.items as AssetTermLinkRead[]) ?? [];
  if (query.termId) items = items.filter((l) => l.term_id === query.termId);
  if (query.linkType) items = items.filter((l) => l.link_type === query.linkType);
  const nextOffset = page.offset + page.limit;
  const hasMore = typeof page.total === "number" && nextOffset < page.total;
  return {
    items,
    limit: page.limit,
    offset: page.offset,
    total: page.total,
    ...(hasMore ? { next_cursor: String(nextOffset) } : {}),
  };
}

/** Discriminated-union error shape for glossary calls -- mirrors
 *  `classifyDescriptionDraftError` so ReviewQueue / BusinessMeaning can
 *  branch copy on the same shape they already know. */
export type GlossaryError =
  | { kind: "TERM_NOT_FOUND"; status: number; detail: string }
  | { kind: "TERM_NOT_SUBMITTABLE"; status: number; detail: string }
  | { kind: "TERM_KEY_TAKEN"; status: number; detail: string }
  | { kind: "TERM_NOT_APPROVED_FOR_LINK"; status: number; detail: string }
  | { kind: "UNAUTHORIZED"; status: number; detail: string }
  | { kind: "SERVER_ERROR"; status: number; detail: string }
  | { kind: "UNKNOWN"; status: number; detail: string };

export function classifyGlossaryError(error: ApiError): GlossaryError {
  const { status, detail } = error;
  if (status === 404) return { kind: "TERM_NOT_FOUND", status, detail };
  if (status === 409 && detail.includes("only approved"))
    return { kind: "TERM_NOT_APPROVED_FOR_LINK", status, detail };
  if (status === 409 && detail.includes("already exists"))
    return { kind: "TERM_KEY_TAKEN", status, detail };
  if (status === 409) return { kind: "TERM_NOT_SUBMITTABLE", status, detail };
  if (status === 401 || status === 403) return { kind: "UNAUTHORIZED", status, detail };
  if (status >= 500) return { kind: "SERVER_ERROR", status, detail };
  return { kind: "UNKNOWN", status, detail };
}
