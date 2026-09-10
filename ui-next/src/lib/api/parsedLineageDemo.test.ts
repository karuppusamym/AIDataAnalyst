import { describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The regression this guards: `listParsedLineageReviewQueue` was the one
   review client with no demo branch, so the default fixtures build issued a
   live request and the Parsed lineage review screen rendered the backend's
   "X-Principal-Id is required in development mode" as a load failure. A demo
   build that cannot open one of its own navigation entries is not an honest
   demo state.

   The assertion that matters is the NEGATIVE one: no fetch. Checking only that
   rows come back would still pass if the fixture were reached through a live
   request that happened to succeed.
--------------------------------------------------------------------------- */

describe("listParsedLineageReviewQueue in a fixtures build", () => {
  it("answers from fixtures without touching the network", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { listParsedLineageReviewQueue } = await import("./governance");

    const page = await listParsedLineageReviewQueue({ limit: 100, offset: 0 });

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(page.total).toBeGreaterThan(0);
    expect(page.items.length).toBe(page.total);
    fetchSpy.mockRestore();
  });

  it("applies the screen's own filters to the fixture rows", async () => {
    const { listParsedLineageReviewQueue } = await import("./governance");

    const dbtOnly = await listParsedLineageReviewQueue({ edgeType: "DBT" });
    expect(dbtOnly.items.map((edge) => edge.edge_type)).toEqual(["DBT"]);

    // Confidence arrives as a string for some edge types (the backend
    // serialises Numeric that way); the filter must coerce rather than compare
    // a string to a number.
    const confident = await listParsedLineageReviewQueue({ minConfidence: 0.7 });
    expect(confident.items.length).toBeGreaterThan(0);
    for (const edge of confident.items) {
      expect(Number(edge.confidence)).toBeGreaterThanOrEqual(0.7);
    }
    expect(confident.items.some((edge) => edge.edge_id === "ple_ol_col_1")).toBe(false);
  });
});
