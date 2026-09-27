/* ---------------------------------------------------------------------------
   Demo data for the stewardship coverage scorecard (fixture mode only).

   Its own module rather than a block in `fixtures.ts`, for the reason
   `documentFixtures.ts` gives: it keeps a small store, so "Take a snapshot"
   behaves the way the server does -- the history gains a row, newest first --
   and nothing else reads it. Reached only through `demoOr` in
   `lib/api/coverage.ts`, by a dynamic import behind a loader that tests the
   build's demo literal (`noDemoData` in `lib/api/transport.ts`), so a live build
   does not contain it.

   The arithmetic is the server's (`build_stewardship_coverage`): each
   dimension is `{covered, total, percentage}` with the percentage rounded to two
   places, and `overall_score` is the mean of the six percentages. The figures
   are invented, and the shell says "Demo data" while they are on screen.
--------------------------------------------------------------------------- */

import type { CoverageDimensionRead, StewardshipCoverageRead } from "./types";
import type { PageOf } from "./ui-types";
import type { CoverageScope, CoverageSnapshotRead } from "./api/coverage";

/** The order the API lists dimensions in (`COVERAGE_DIMENSIONS`). */
const DIMENSIONS = [
  "documented",
  "owned",
  "classified",
  "certified",
  "quality_monitored",
  "semantically_mapped",
] as const;

type Ratios = Readonly<Record<(typeof DIMENSIONS)[number], number>>;

/** How much of the estate each dimension covers today, in the demo. */
const CURRENT: Ratios = {
  documented: 0.58,
  owned: 0.71,
  classified: 0.44,
  certified: 0.33,
  quality_monitored: 0.52,
  semantically_mapped: 0.6,
};

/** The organization's tables in the demo; one datasource holds a quarter, one business domain
 *  an eighth, and one line of business all but a sixth. */
const ORG_TABLES = 48;
const DATASOURCE_TABLES = 12;
const DOMAIN_TABLES = 6;
const LINE_OF_BUSINESS_TABLES = 40;

const round2 = (value: number): number => Math.round(value * 100) / 100;

function figures(total: number, ratios: Ratios) {
  const dimensions: Record<string, CoverageDimensionRead> = {};
  for (const name of DIMENSIONS) {
    const covered = Math.round(total * ratios[name]);
    dimensions[name] = {
      covered,
      total,
      percentage: total ? round2((covered * 100) / total) : 0,
    };
  }
  const overall = total
    ? round2(
        Object.values(dimensions).reduce((sum, dimension) => sum + dimension.percentage, 0) /
          DIMENSIONS.length,
      )
    : 0;
  return { dimensions, overall };
}

/** The demo population of a scope: the narrowest field given decides it. */
const tablesIn = (scope: CoverageScope): number =>
  scope.domainId
    ? DOMAIN_TABLES
    : scope.datasourceId
      ? DATASOURCE_TABLES
      : scope.lineOfBusinessId
        ? LINE_OF_BUSINESS_TABLES
        : ORG_TABLES;

const isOrganization = (scope: CoverageScope): boolean =>
  !scope.datasourceId && !scope.domainId && !scope.lineOfBusinessId;

/** The figures now, as `GET .../stewardship/coverage` answers them. */
export function fixtureCoverage(organizationId: string, scope: CoverageScope): StewardshipCoverageRead {
  const total = tablesIn(scope);
  const { dimensions, overall } = figures(total, CURRENT);
  const unowned = total - dimensions.owned!.covered;
  return {
    organization_id: organizationId,
    datasource_id: scope.datasourceId ?? null,
    domain_id: scope.domainId ?? null,
    line_of_business_id: scope.lineOfBusinessId ?? null,
    table_count: total,
    overall_score: overall,
    dimensions,
    unowned_table_ids: Array.from({ length: unowned }, (_, index) => `t_demo_${String(index).padStart(4, "0")}`),
    computed_at: new Date().toISOString(),
  };
}

/* The stored history, per scope. Seeded lazily with three older rows for the
   whole organization so the table has something to show before anyone takes a
   snapshot; a datasource starts with none, which is the empty state. */
const store = new Map<string, CoverageSnapshotRead[]>();

const scopeKey = (organizationId: string, scope: CoverageScope): string =>
  `${organizationId}:${scope.datasourceId ?? ""}:${scope.domainId ?? ""}:${scope.lineOfBusinessId ?? ""}`;

const DAY = 86_400_000;

function historyFor(organizationId: string, scope: CoverageScope): CoverageSnapshotRead[] {
  const key = scopeKey(organizationId, scope);
  const existing = store.get(key);
  if (existing) return existing;
  const seeded: CoverageSnapshotRead[] = [];
  if (isOrganization(scope)) {
    // Newest first, as the server returns them: each older row a little less covered.
    [
      { daysAgo: 7, scale: 0.94 },
      { daysAgo: 30, scale: 0.85 },
      { daysAgo: 60, scale: 0.72 },
    ].forEach(({ daysAgo, scale }, index) => {
      const ratios = Object.fromEntries(
        DIMENSIONS.map((name) => [name, CURRENT[name] * scale]),
      ) as unknown as Ratios;
      const { dimensions, overall } = figures(ORG_TABLES, ratios);
      seeded.push({
        id: `snap_demo_${index}`,
        organization_id: organizationId,
        datasource_id: null,
        domain_id: null,
        line_of_business_id: null,
        table_count: ORG_TABLES,
        dimensions,
        overall_score: overall,
        computed_by: "dana.steward",
        created_at: new Date(Date.now() - daysAgo * DAY).toISOString(),
      });
    });
  }
  store.set(key, seeded);
  return seeded;
}

/** `GET .../stewardship/coverage/snapshots`, newest first. */
export function fixtureCoverageSnapshots(
  organizationId: string,
  scope: CoverageScope,
  limit: number,
  offset: number,
): PageOf<CoverageSnapshotRead> {
  const all = historyFor(organizationId, scope);
  return { items: all.slice(offset, offset + limit), limit, offset, total: all.length };
}

/** `POST .../stewardship/coverage/snapshots`: compute again, store that, answer with it. */
export function fixtureTakeCoverageSnapshot(
  organizationId: string,
  scope: CoverageScope,
): StewardshipCoverageRead {
  const coverage = fixtureCoverage(organizationId, scope);
  const history = historyFor(organizationId, scope);
  history.unshift({
    id: `snap_demo_${Date.now().toString(36)}_${history.length}`,
    organization_id: organizationId,
    datasource_id: scope.datasourceId ?? null,
    domain_id: scope.domainId ?? null,
    line_of_business_id: scope.lineOfBusinessId ?? null,
    table_count: coverage.table_count,
    dimensions: coverage.dimensions,
    overall_score: coverage.overall_score,
    computed_by: "demo.steward",
    created_at: coverage.computed_at,
  });
  return coverage;
}
