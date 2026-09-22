/* ---------------------------------------------------------------------------
   Stewardship coverage (R11-AUD08) -- the scorecard the API has computed since
   GL-4 and no screen had ever asked for.

   THREE ROUTES, ONE ORGANIZATION (`src/aida/stewardship_api.py`):

     GET  .../stewardship/coverage             the figures NOW. Computed on
                                               request from the catalog, never
                                               stored.
     GET  .../stewardship/coverage/snapshots   the stored history, newest first.
     POST .../stewardship/coverage/snapshots   compute the figures again and
                                               STORE that result as a row.

   WHAT A FIGURE IS. Six dimensions -- documented, owned, classified,
   certified, quality_monitored, semantically_mapped -- each `{covered, total,
   percentage}` over the ACTIVE tables in scope, plus `overall_score`, which is
   the mean of the six percentages (`build_stewardship_coverage`). Every number
   is the API's: this module derives nothing and a screen must not either.

   THE SNAPSHOT IS NOT WHAT WAS ON SCREEN. `snapshot_stewardship_coverage`
   re-runs `_coverage` on the server and stores that, so the row it writes can
   differ from the figures the caller was looking at when they pressed the
   button, if the estate moved in between. The response is the freshly computed
   `StewardshipCoverageRead`, not the stored row; the caller re-reads the
   history to see the row.

   SCOPE. The organization, one datasource, one business domain (the tables
   annotated with it, `MetadataBusinessAnnotation.domain_id`) or one line of
   business (the sources of its projects) -- the four the API accepts, sent as
   `datasource_id`, `domain_id` and `line_of_business_id`. The API ANDs any it is
   given; the screen sends one. The history is filtered to EXACTLY the scope asked
   for (`column IS NULL` for every scope field not given), so the organization's
   history does not contain a datasource's snapshots.

   Transport, identity headers and the demo switch come from `./transport`.
   Re-exported from `lib/api.ts`.
--------------------------------------------------------------------------- */

import { fetchBusinessMap } from "./catalog";
import { demoOr, get, noDemoData, postJson } from "./transport";
import type { CoverageDimensionRead, StewardshipCoverageRead } from "../types";
import type { PageOf } from "../ui-types";

/** The demo store, loaded only where a demo arm runs, and absent from a live build (`noDemoData`). */
const coverageDemo = (): Promise<typeof import("../coverageFixtures")> =>
  import.meta.env.VITE_USE_FIXTURES === "0" ? noDemoData() : import("../coverageFixtures");

/**
 * The roles `GET .../stewardship/coverage` and `GET .../coverage/snapshots` admit.
 *
 * Copied from the surface-control matrix rows for
 * `aida.stewardship_api.get_stewardship_coverage` and
 * `list_stewardship_coverage_snapshots` (`Docs/50-security/surface-control-matrix.md`),
 * which the handlers' own `READ_ROLES` produces: Analyst, Auditor, DataAdmin,
 * DataSteward, MetadataAdmin, PlatformAdmin, Reviewer, SemanticAdmin, Viewer.
 */
export const COVERAGE_READ_ROLES: readonly string[] = [
  "Analyst",
  "Auditor",
  "DataAdmin",
  "DataSteward",
  "MetadataAdmin",
  "PlatformAdmin",
  "Reviewer",
  "SemanticAdmin",
  "Viewer",
];

/**
 * The roles `POST .../stewardship/coverage/snapshots` admits.
 *
 * Copied from the matrix row for `aida.stewardship_api.snapshot_stewardship_coverage`
 * (the handlers' `WRITE_ROLES`): DataSteward, MetadataAdmin, PlatformAdmin, SemanticAdmin.
 */
export const COVERAGE_SNAPSHOT_ROLES: readonly string[] = [
  "DataSteward",
  "MetadataAdmin",
  "PlatformAdmin",
  "SemanticAdmin",
];

/**
 * One stored row of `GET .../stewardship/coverage/snapshots`
 * (`CoverageSnapshotRead`, `aida.schemas`).
 *
 * Defined here because the route is declared `response_model=Page`, whose
 * `items` are `list[Any]`, so the generated `types.ts` carries the dimension
 * and the live figure (`CoverageDimensionRead`, `StewardshipCoverageRead`) but
 * no row type for the history. The fields are `CoverageSnapshotRead`'s exactly.
 */
export interface CoverageSnapshotRead {
  id: string;
  organization_id: string;
  datasource_id: string | null;
  domain_id: string | null;
  line_of_business_id: string | null;
  table_count: number;
  dimensions: Record<string, CoverageDimensionRead>;
  overall_score: number;
  computed_by: string;
  created_at: string;
}

/** Which population a figure or a history is about. Every field absent is the whole organization. */
export interface CoverageScope {
  /** One datasource. */
  datasourceId?: string | null;
  /** One business domain (`BusinessDomain.id`): the tables annotated with it. */
  domainId?: string | null;
  /** One line of business: the tables of the sources its projects hold. */
  lineOfBusinessId?: string | null;
}

function scopeQuery(scope: CoverageScope): URLSearchParams {
  const params = new URLSearchParams();
  if (scope.datasourceId) params.set("datasource_id", scope.datasourceId);
  if (scope.domainId) params.set("domain_id", scope.domainId);
  if (scope.lineOfBusinessId) params.set("line_of_business_id", scope.lineOfBusinessId);
  return params;
}

/** A business domain a coverage figure can be scoped to. */
export interface CoverageDomainOption {
  id: string;
  name: string;
}

/**
 * The business domains `GET .../stewardship/coverage` can be scoped to, read from
 * `GET /v1/organizations/{organization_id}/business-map` -- the only route that names
 * them with their ids, and one that admits exactly `COVERAGE_READ_ROLES`. Its DOMAIN
 * nodes are `domain:<BusinessDomain.id>`; a domain with no annotated table is not on
 * the map, and its coverage would be over no tables anyway. `incomplete` is the map's
 * own `truncated`: the map stops at `limit` annotations, so a domain can be missing.
 */
export async function fetchCoverageDomains(
  organizationId: string,
  signal?: AbortSignal,
): Promise<{ domains: CoverageDomainOption[]; incomplete: boolean }> {
  const map = await fetchBusinessMap({ organizationId, limit: 2000 }, signal);
  const domains = map.nodes
    .filter((node) => node.node_type === "DOMAIN" && node.id.startsWith("domain:"))
    .map((node) => ({ id: node.id.slice("domain:".length), name: node.label }))
    .sort((a, b) => a.name.localeCompare(b.name));
  return { domains, incomplete: map.truncated };
}

const withQuery = (path: string, params: URLSearchParams): string => {
  const query = params.toString();
  return query ? `${path}?${query}` : path;
};

/**
 * `GET /v1/organizations/{organization_id}/stewardship/coverage`.
 *
 * An organization or datasource with no active tables comes back as all zeros
 * (`table_count: 0`), which is "nothing to score", not "0% covered" -- the
 * screen has to say which.
 */
export function fetchStewardshipCoverage(
  organizationId: string,
  scope: CoverageScope = {},
  signal?: AbortSignal,
): Promise<StewardshipCoverageRead> {
  return demoOr(
    async () =>
      (await coverageDemo()).fixtureCoverage(organizationId, scope),
    () =>
      get<StewardshipCoverageRead>(
        withQuery(`/v1/organizations/${organizationId}/stewardship/coverage`, scopeQuery(scope)),
        signal,
      ),
  );
}

/**
 * `GET /v1/organizations/{organization_id}/stewardship/coverage/snapshots`,
 * newest first, for exactly this scope. `total` counts every stored snapshot in
 * scope, so a caller that asked for fewer can say how many it left out.
 */
export function fetchCoverageSnapshots(
  organizationId: string,
  scope: CoverageScope = {},
  page: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PageOf<CoverageSnapshotRead>> {
  const limit = page.limit ?? 50;
  const offset = page.offset ?? 0;
  return demoOr(
    async () =>
      (await coverageDemo()).fixtureCoverageSnapshots(organizationId, scope, limit, offset),
    () => {
      const params = scopeQuery(scope);
      params.set("limit", String(limit));
      params.set("offset", String(offset));
      return get<PageOf<CoverageSnapshotRead>>(
        withQuery(`/v1/organizations/${organizationId}/stewardship/coverage/snapshots`, params),
        signal,
      );
    },
  );
}

/**
 * `POST /v1/organizations/{organization_id}/stewardship/coverage/snapshots`.
 *
 * No body: the scope rides the query string, exactly as it does on the read.
 * A write -- it stores a row, records an audit entry and queues a
 * `stewardship.coverage_computed.v1` event -- so the caller confirms first.
 * Answers with the coverage the server computed for the row it stored.
 */
export function takeCoverageSnapshot(
  organizationId: string,
  scope: CoverageScope = {},
  signal?: AbortSignal,
): Promise<StewardshipCoverageRead> {
  return demoOr(
    async () =>
      (await coverageDemo()).fixtureTakeCoverageSnapshot(organizationId, scope),
    () =>
      postJson<StewardshipCoverageRead>(
        withQuery(
          `/v1/organizations/${organizationId}/stewardship/coverage/snapshots`,
          scopeQuery(scope),
        ),
        undefined,
        signal,
      ),
  );
}
