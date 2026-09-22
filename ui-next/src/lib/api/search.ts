/* ---------------------------------------------------------------------------
   Global search (R11-AUD08) -- `src/aida/search_api.py`.

   TWO ROUTES, BOTH SCOPED BY A REQUIRED `organization_id` QUERY FIELD (not a
   path segment, unlike the stewardship routes):

     GET /v1/search          cross-source, cross-type search of table and
                             column NAMES, ranked, paged by `limit`/`offset`,
                             answered with the total and one facet per object
                             type.
     GET /v1/search/suggest  typeahead for the command palette: TABLE names
                             only, unranked.

   WHAT THE ANSWER DOES AND DOES NOT SAY -- read before rendering a hit:

     * Candidates are chosen by a case-insensitive `contains` on the NAME of a
       table or column, then ranked. Descriptions and glossary terms are not
       searched, whatever the `object_type` filter's own description says
       ("TABLE, COLUMN, ANNOTATION"): there is no annotation branch, so filtering
       to ANNOTATION returns nothing and is not offered.
     * A TABLE hit carries the id of its datasource, and `evidence.metadata`
       carries `table_id`. A COLUMN hit carries its table's datasource,
       `qualified_name` as `table.column`, and `column_id`, `table_id` and
       `table_name` in `evidence.metadata` (since 2026-09-21, R11-AUD08; before
       that the handler dropped them, so five columns called `customer_id` in
       five tables were five identical rows). Nothing here invents the parent
       when a hit lacks it; see `lib/searchTargets.ts`.
     * `datasource_name`, `domain_name` and `description` exist on the result and
       are never filled today.
     * Words under two letters and common stop words are dropped from the query
       (`build_ts_query`); a query made only of them answers `total: 0`.

   The route is declared `response_model=dict[str, Any]`, so the generated
   `types.ts` has a suggestion type (`SearchSuggestion`) and no shape for the
   search answer. The interfaces below are `SearchResult`, `SearchFacet` and
   `RetrievalEvidence` from `aida.platform_schemas`, field for field.

   Transport, identity headers and the demo switch come from `./transport`.
   Re-exported from `lib/api.ts`.
--------------------------------------------------------------------------- */

import { demoOr, get, noDemoData } from "./transport";
import type { SearchSuggestion } from "../types";

/** The demo index, loaded only where a demo arm runs, and absent from a live build (`noDemoData`). */
const searchDemo = (): Promise<typeof import("../searchFixtures")> =>
  import.meta.env.VITE_USE_FIXTURES === "0" ? noDemoData() : import("../searchFixtures");

/**
 * The roles `GET /v1/search` and `GET /v1/search/suggest` admit.
 *
 * Copied from the surface-control matrix rows for `aida.search_api.global_search`
 * and `search_suggest` (`Docs/50-security/surface-control-matrix.md`) -- both
 * declare `require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Analyst",
 * "Viewer")`. An Auditor or Reviewer holding none of these is refused by the
 * server and must never be sent the request.
 */
export const SEARCH_ROLES: readonly string[] = [
  "Analyst",
  "DataAdmin",
  "DataSteward",
  "PlatformAdmin",
  "Viewer",
];

/** The API's default page, and what the Search screen asks for. */
export const SEARCH_PAGE_SIZE = 25;
/** `q` is refused above these (`Query(max_length=...)`). */
export const SEARCH_QUERY_MAX_LENGTH = 500;
export const SUGGEST_QUERY_MAX_LENGTH = 200;

export interface SearchFactorRead {
  signal: string;
  raw_score: number;
  weight: number;
  weighted_score: number;
  rank: number | null;
}

export interface SearchEvidenceRead {
  object_type: string;
  object_id: string;
  display_name: string;
  final_score: number;
  fusion_method: string;
  factors: SearchFactorRead[];
  graph_expansion_path: string[];
  source_signals: string[];
  /** A hit's identifiers (`table_id`; for a column also `column_id` and `table_name`) -- see the note above. */
  metadata: Record<string, unknown>;
}

export interface SearchResultRead {
  object_type: string;
  object_id: string;
  display_name: string;
  qualified_name: string | null;
  description: string | null;
  score: number;
  evidence: SearchEvidenceRead;
  datasource_id: string | null;
  datasource_name: string | null;
  domain_name: string | null;
}

export interface SearchFacetRead {
  field: string;
  value: string;
  count: number;
}

export interface SearchPageRead {
  items: SearchResultRead[];
  facets: SearchFacetRead[];
  /** Every hit that matched, across pages (the handler caps candidates at 500 tables and 500 columns). */
  total: number;
  limit: number;
  offset: number;
}

export interface SearchQuery {
  q: string;
  /** `TABLE` or `COLUMN`; absent or null searches both. */
  objectType?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/search`. */
export function fetchSearchResults(
  organizationId: string,
  query: SearchQuery,
  signal?: AbortSignal,
): Promise<SearchPageRead> {
  const limit = query.limit ?? SEARCH_PAGE_SIZE;
  const offset = query.offset ?? 0;
  return demoOr(
    async (fixtures) =>
      (await searchDemo()).fixtureSearch(fixtures, organizationId, {
        ...query,
        limit,
        offset,
      }),
    () => {
      const params = new URLSearchParams({
        q: query.q,
        organization_id: organizationId,
        limit: String(limit),
        offset: String(offset),
      });
      if (query.objectType) params.set("object_type", query.objectType);
      return get<SearchPageRead>(`/v1/search?${params}`, signal);
    },
  );
}

/** `GET /v1/search/suggest` -- table names containing `q`, at most `limit` of them. */
export function fetchSearchSuggestions(
  organizationId: string,
  q: string,
  limit = 8,
  signal?: AbortSignal,
): Promise<SearchSuggestion[]> {
  return demoOr(
    async (fixtures) =>
      (await searchDemo()).fixtureSuggest(fixtures, organizationId, q, limit),
    () => {
      const params = new URLSearchParams({
        q,
        organization_id: organizationId,
        limit: String(limit),
      });
      return get<SearchSuggestion[]>(`/v1/search/suggest?${params}`, signal);
    },
  );
}
