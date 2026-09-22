/* ---------------------------------------------------------------------------
   Demo data for global search (fixture mode only).

   Not a second catalog: it reads the demo catalog `fixtures.ts` already
   generates (handed in by `demoOr`, so this module imports nothing from it at
   run time) and shapes the matching rows the way `GET /v1/search` and
   `/v1/search/suggest` shape a table hit. The demo estate has no column
   inventory, so demo search finds TABLES only -- the live answer also carries
   COLUMN hits, and the screen renders both from the same shape.

   Matching is the demo catalog's own (the query as one substring of the name,
   description or schema), which is coarser than the server's tokenised name
   match; the demo answer is capped at the first 100 matching tables, and its
   `total` is the number it actually holds, not a count of the whole demo estate.
--------------------------------------------------------------------------- */

import type { DemoData } from "./api/transport";
import type { SearchPageRead, SearchQuery, SearchResultRead } from "./api/search";
import type { SearchSuggestion } from "./types";
import type { CatalogRowRead } from "./ui-types";

const DEMO_MATCH_CAP = 100;

async function demoTables(
  fixtures: DemoData,
  organizationId: string,
  q: string,
): Promise<CatalogRowRead[]> {
  const page = await fixtures.makeFixtureCatalog({
    organizationId,
    q,
    objectType: "ALL",
    certification: "ALL",
    limit: DEMO_MATCH_CAP,
  });
  return page.items;
}

function hit(row: CatalogRowRead): SearchResultRead {
  return {
    object_type: "TABLE",
    object_id: row.id,
    display_name: row.name,
    qualified_name: row.name,
    description: null,
    score: 1,
    evidence: {
      object_type: "TABLE",
      object_id: row.id,
      display_name: row.name,
      final_score: 1,
      fusion_method: "lexical",
      factors: [{ signal: "lexical", raw_score: 1, weight: 1, weighted_score: 1, rank: null }],
      graph_expansion_path: [],
      source_signals: ["lexical"],
      metadata: {},
    },
    datasource_id: row.datasource_id,
    datasource_name: null,
    domain_name: null,
  };
}

/** `GET /v1/search`, over the demo tables. */
export async function fixtureSearch(
  fixtures: DemoData,
  organizationId: string,
  query: SearchQuery & { limit: number; offset: number },
): Promise<SearchPageRead> {
  // A COLUMN filter matches nothing here: the demo estate lists no columns.
  const rows = query.objectType === "COLUMN" ? [] : await demoTables(fixtures, organizationId, query.q);
  const facets = rows.length ? [{ field: "object_type", value: "TABLE", count: rows.length }] : [];
  return {
    items: rows.slice(query.offset, query.offset + query.limit).map(hit),
    facets,
    total: rows.length,
    limit: query.limit,
    offset: query.offset,
  };
}

/** `GET /v1/search/suggest`, over the demo tables. */
export async function fixtureSuggest(
  fixtures: DemoData,
  organizationId: string,
  q: string,
  limit: number,
): Promise<SearchSuggestion[]> {
  const rows = await demoTables(fixtures, organizationId, q);
  return rows.slice(0, limit).map((row) => ({
    text: row.name,
    object_type: "TABLE",
    object_id: row.id,
    display_name: row.name,
    qualified_name: row.name,
    score: 1,
  }));
}
