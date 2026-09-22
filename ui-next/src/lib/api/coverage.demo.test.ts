import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The stewardship coverage client in DEMO mode (R11-AUD08): fixture answers,
   and no network request, for all three functions.

   `demoOr` is not mocked here, on purpose -- this is the door the fixtures come
   through. The properties: the demo store behaves like the server (a snapshot
   lands at the top of its own scope's history and no other's), the demo figures
   obey the server's arithmetic, and the demo write never touches `fetch`.
--------------------------------------------------------------------------- */

const ORG = "org-demo";

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no request in demo mode"))));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("demo coverage", () => {
  it("answers from the fixtures, and sends nothing", async () => {
    const { fetchStewardshipCoverage } = await import("./coverage");

    const coverage = await fetchStewardshipCoverage(ORG, {});

    expect(coverage.table_count).toBeGreaterThan(0);
    expect(Object.keys(coverage.dimensions)).toEqual([
      "documented", "owned", "classified", "certified", "quality_monitored", "semantically_mapped",
    ]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("uses the server's arithmetic: two-place percentages, and an overall that is their mean", async () => {
    const { fetchStewardshipCoverage } = await import("./coverage");

    const coverage = await fetchStewardshipCoverage(ORG, {});

    const percentages = Object.values(coverage.dimensions).map((dimension) => dimension.percentage);
    for (const dimension of Object.values(coverage.dimensions)) {
      expect(dimension.total).toBe(coverage.table_count);
      // `round(covered * 100 / total, 2)`, as `build_stewardship_coverage` writes it.
      expect(dimension.percentage).toBe(Math.round(((dimension.covered * 100) / dimension.total) * 100) / 100);
    }
    const mean = Math.round((percentages.reduce((a, b) => a + b, 0) / percentages.length) * 100) / 100;
    expect(coverage.overall_score).toBe(mean);
    // The API's list of unowned tables is exactly the tables the `owned` dimension does not cover.
    expect(coverage.unowned_table_ids).toHaveLength(coverage.dimensions.owned!.total - coverage.dimensions.owned!.covered);
  });

  it("scopes to a datasource: fewer tables, and its own (empty) history", async () => {
    const { fetchStewardshipCoverage, fetchCoverageSnapshots } = await import("./coverage");

    const org = await fetchStewardshipCoverage(ORG, {});
    const source = await fetchStewardshipCoverage(ORG, { datasourceId: "ds_snowflake_prod" });
    const history = await fetchCoverageSnapshots(ORG, { datasourceId: "ds_snowflake_prod" });

    expect(source.table_count).toBeLessThan(org.table_count);
    expect(source.datasource_id).toBe("ds_snowflake_prod");
    expect(history).toEqual({ items: [], limit: 50, offset: 0, total: 0 });
  });

  it("scopes to a business domain or a line of business: its own population and its own history", async () => {
    const { fetchCoverageSnapshots, fetchStewardshipCoverage } = await import("./coverage");

    const domain = await fetchStewardshipCoverage(ORG, { domainId: "dom_customer" });
    const lob = await fetchStewardshipCoverage(ORG, { lineOfBusinessId: "lob_retail" });

    expect(domain.domain_id).toBe("dom_customer");
    expect(domain.table_count).toBeLessThan(lob.table_count);
    expect(lob.line_of_business_id).toBe("lob_retail");
    expect((await fetchCoverageSnapshots(ORG, { domainId: "dom_customer" })).total).toBe(0);
  });

  it("lists the organization's history newest first", async () => {
    const { fetchCoverageSnapshots } = await import("./coverage");

    const page = await fetchCoverageSnapshots(ORG, {});

    expect(page.total).toBe(page.items.length);
    const times = page.items.map((snapshot) => Date.parse(snapshot.created_at));
    expect([...times].sort((a, b) => b - a)).toEqual(times);
    expect(page.items.every((snapshot) => snapshot.datasource_id === null)).toBe(true);
  });

  it("pages the history by limit and offset, and reports the whole total", async () => {
    const { fetchCoverageSnapshots } = await import("./coverage");

    const all = await fetchCoverageSnapshots(ORG, {});
    const second = await fetchCoverageSnapshots(ORG, {}, { limit: 1, offset: 1 });

    expect(second.items).toEqual([all.items[1]]);
    expect(second.total).toBe(all.total);
  });

  it("takes a snapshot into its own scope's history only, and sends nothing", async () => {
    const { takeCoverageSnapshot, fetchCoverageSnapshots } = await import("./coverage");
    const before = await fetchCoverageSnapshots(ORG, {});

    const stored = await takeCoverageSnapshot(ORG, {});

    const after = await fetchCoverageSnapshots(ORG, {});
    const other = await fetchCoverageSnapshots(ORG, { datasourceId: "ds_snowflake_prod" });
    expect(after.total).toBe(before.total + 1);
    // Newest first: the row just taken is on top, and it holds what the server answered with.
    expect(after.items[0]).toMatchObject({
      table_count: stored.table_count,
      overall_score: stored.overall_score,
      dimensions: stored.dimensions,
      datasource_id: null,
    });
    expect(other.total).toBe(0);
    expect(fetch).not.toHaveBeenCalled();
  });
});
