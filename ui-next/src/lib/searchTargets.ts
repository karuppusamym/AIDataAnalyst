/* ---------------------------------------------------------------------------
   Where a search hit opens (R11-AUD08) -- one answer for the Search screen and
   the Ctrl+K palette, so the two cannot disagree about what a result is.

   A hit names an object by TYPE and ID. The screen that owns a TABLE is the
   Catalog: it selects a row by `?asset=<table id>`, which is also how Quality
   and Overview already open one. Two things make that less direct than it
   looks, and both are handled here rather than at each call site:

   1. THE CATALOG SELECTS AMONG THE ROWS IT HAS LOADED. It loads the first 100
      rows the filters allow (more only as the list is scrolled) and finds
      `asset` among them, so an `asset` that is not in that page selects
      nothing. A link with only `asset` works when the table happens to sort
      early. So the link also carries the table's own name as the
      Catalog's `q` (which its name matches by construction) and, when the hit
      says which datasource it is in, `ds`. Both are the Catalog's own filters,
      declared for it in `SCREEN_QUERY_FIELDS`, and both are visible on arrival
      -- the search box shows the name, and a banner says the list is one
      source -- so nothing is narrowed silently.

   2. A COLUMN OPENS ITS TABLE ONLY WHEN THE HIT SAYS WHICH ONE. Since
      2026-09-21 `GET /v1/search` puts `table_id` in `evidence.metadata` and the
      table's datasource in `datasource_id` (see `lib/api/search.ts`); before
      that it built the table id and dropped it, and no route resolves a column
      id to its table. A hit without `table_id` (an older server) still gets "no
      destination", not a guess at one.
--------------------------------------------------------------------------- */

/**
 * The roles `GET /v1/organizations/{organization_id}/catalog/rows` admits.
 *
 * Copied from the surface-control matrix row for
 * `atlas.modules.catalog.router.list_catalog_rows`
 * (`Docs/50-security/surface-control-matrix.md`): Analyst, MetadataAdmin,
 * PlatformAdmin, Viewer. Search admits two roles this list does not (DataAdmin,
 * DataSteward), so a session holding only those can search and cannot open the
 * Catalog; offering it a link that ends in the Catalog's refusal would be a
 * button that cannot work.
 */
export const CATALOG_ROWS_ROLES: readonly string[] = [
  "Analyst",
  "MetadataAdmin",
  "PlatformAdmin",
  "Viewer",
];

/** What a hit or a suggestion has to say for a destination to be chosen. */
export interface SearchHit {
  object_type: string;
  object_id: string;
  display_name: string;
  datasource_id?: string | null;
  evidence?: { metadata?: Record<string, unknown> | null } | null;
}

export interface SearchTarget {
  screen: string;
  params: Record<string, string>;
}

/** The screen and selection a hit opens, or null when the API does not say enough to open one. */
export function searchTargetFor(hit: SearchHit): SearchTarget | null {
  if (hit.object_type === "TABLE") {
    const params: Record<string, string> = { asset: hit.object_id, q: hit.display_name };
    if (hit.datasource_id) params.ds = hit.datasource_id;
    return { screen: "catalog", params };
  }
  if (hit.object_type === "COLUMN") {
    const tableId = hit.evidence?.metadata?.["table_id"];
    if (typeof tableId === "string" && tableId) {
      const params: Record<string, string> = { asset: tableId };
      if (hit.datasource_id) params.ds = hit.datasource_id;
      return { screen: "catalog", params };
    }
  }
  return null;
}

const KNOWN_TYPES: Record<string, readonly [string, string]> = {
  TABLE: ["table", "tables"],
  COLUMN: ["column", "columns"],
};

/** A type code as a person reads it: `TABLE` -> "table" / "tables". An unknown code is shown as it came, lower-cased. */
export function objectTypeLabel(objectType: string, plural = false): string {
  const known = KNOWN_TYPES[objectType];
  if (known) return known[plural ? 1 : 0];
  const word = objectType.toLowerCase().replace(/_/g, " ");
  return plural ? `${word}s` : word;
}
