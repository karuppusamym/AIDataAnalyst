import { describe, expect, it } from "vitest";
import {
  COVERAGE_DIMENSION_INFO,
  dimensionCounts,
  dimensionLabel,
  orderedDimensionKeys,
} from "./coverageDimensions";

/* ---------------------------------------------------------------------------
   What each coverage dimension counts (R11-AUD08). The sentences are the only
   place this client says what the API measures, so what is tested is that every
   dimension the server computes has one, in the server's order, and that a
   dimension the server adds later is shown rather than dropped.
--------------------------------------------------------------------------- */

/** `COVERAGE_DIMENSIONS` in `src/aida/stewardship_service.py`. */
const SERVER_DIMENSIONS = [
  "documented",
  "owned",
  "classified",
  "certified",
  "quality_monitored",
  "semantically_mapped",
];

describe("the dimension definitions", () => {
  it("cover every dimension the server computes, in its order, each with a sentence", () => {
    expect(COVERAGE_DIMENSION_INFO.map((info) => info.key)).toEqual(SERVER_DIMENSIONS);
    for (const info of COVERAGE_DIMENSION_INFO) {
      expect(info.counts.length).toBeGreaterThan(20);
      expect(dimensionCounts(info.key)).toBe(info.counts);
    }
  });

  it("says what is easy to get wrong about certification: expiry, and columns", () => {
    expect(dimensionCounts("certified")).toMatch(/not expired/);
    expect(dimensionCounts("certified")).toMatch(/column does not make its table certified/);
  });
});

describe("dimensionLabel", () => {
  it("names the known dimensions as a person reads them", () => {
    expect(dimensionLabel("quality_monitored")).toBe("Quality monitored");
    expect(dimensionLabel("semantically_mapped")).toBe("Semantically mapped");
  });

  it("humanizes a dimension the server added, and has no definition for it", () => {
    expect(dimensionLabel("lineage_traced")).toBe("Lineage traced");
    expect(dimensionCounts("lineage_traced")).toBeNull();
  });
});

describe("orderedDimensionKeys", () => {
  it("puts the server's six first in the server's order, whatever order they arrive in", () => {
    expect(orderedDimensionKeys(["certified", "documented", "owned"])).toEqual(["documented", "owned", "certified"]);
  });

  it("keeps a key it does not know, after the known ones, in arrival order", () => {
    expect(orderedDimensionKeys(["zeta_new", "documented", "alpha_new"])).toEqual(["documented", "zeta_new", "alpha_new"]);
  });

  it("does not invent a dimension the payload lacks", () => {
    expect(orderedDimensionKeys([])).toEqual([]);
    expect(orderedDimensionKeys(["owned"])).toEqual(["owned"]);
  });
});
