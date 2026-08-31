import type {
  CatalogRowRead,
  CatalogAssetEvidence,
  CursorPage,
  MeRead,
  MetadataTableRead,
} from "./types";
import { makeFixtureCatalog, makeFixtureEvidence, makeFixtureMe } from "./fixtures";

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
 * PROPOSED read-model endpoint. Until the BFF row lands, this runs against
 * fixtures so the screen is reviewable now; flip VITE_USE_FIXTURES=0 once
 * `GET /v1/organizations/{org}/catalog/rows` exists.
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
): Promise<CatalogAssetEvidence> {
  if (USE_FIXTURES) return makeFixtureEvidence(tableId);
  return get<CatalogAssetEvidence>(`/v1/metadata/tables/${tableId}/evidence`, signal);
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
