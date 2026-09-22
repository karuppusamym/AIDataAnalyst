import { describe, expect, it } from "vitest";
import { CATALOG_ROWS_ROLES, objectTypeLabel, searchTargetFor } from "./searchTargets";

/* ---------------------------------------------------------------------------
   Where a search hit opens (R11-AUD08). The Search screen and the palette both
   call `searchTargetFor`, so this is the one place "what does clicking that do"
   is decided -- and the property that matters most is the negative one: a hit
   the API does not say enough about has NO target, not a guessed one.
--------------------------------------------------------------------------- */

describe("searchTargetFor", () => {
  it("opens a table in the Catalog, selected by id, filtered to its own name and source", () => {
    expect(
      searchTargetFor({ object_type: "TABLE", object_id: "t-1", display_name: "customer", datasource_id: "ds-1" }),
    ).toEqual({ screen: "catalog", params: { asset: "t-1", q: "customer", ds: "ds-1" } });
  });

  it("leaves the source out when the hit does not say which it is in (a suggestion never does)", () => {
    expect(searchTargetFor({ object_type: "TABLE", object_id: "t-1", display_name: "customer" })).toEqual({
      screen: "catalog",
      params: { asset: "t-1", q: "customer" },
    });
    expect(
      searchTargetFor({ object_type: "TABLE", object_id: "t-1", display_name: "customer", datasource_id: null })!
        .params,
    ).not.toHaveProperty("ds");
  });

  it("gives a column NO destination while the API does not say which table it is in", () => {
    // The live answer: `evidence.metadata` is `{}` and `datasource_id` is null for every column hit.
    expect(
      searchTargetFor({
        object_type: "COLUMN",
        object_id: "c-1",
        display_name: "customer_id",
        datasource_id: null,
        evidence: { metadata: {} },
      }),
    ).toBeNull();
    expect(searchTargetFor({ object_type: "COLUMN", object_id: "c-1", display_name: "customer_id" })).toBeNull();
  });

  it("opens a column's table the day the answer carries its id", () => {
    expect(
      searchTargetFor({
        object_type: "COLUMN",
        object_id: "c-1",
        display_name: "customer_id",
        datasource_id: "ds-1",
        evidence: { metadata: { table_id: "t-9", column_id: "c-1" } },
      }),
    ).toEqual({ screen: "catalog", params: { asset: "t-9", ds: "ds-1" } });
  });

  it("does not trust a table id that is not a non-empty string", () => {
    for (const bad of [null, 0, 12, {}, ["t-9"], ""]) {
      expect(
        searchTargetFor({
          object_type: "COLUMN",
          object_id: "c-1",
          display_name: "customer_id",
          evidence: { metadata: { table_id: bad } },
        }),
      ).toBeNull();
    }
  });

  it("has no destination for a type it does not know", () => {
    expect(searchTargetFor({ object_type: "ANNOTATION", object_id: "a-1", display_name: "x" })).toBeNull();
  });
});

describe("the Catalog role list", () => {
  it("is the surface-control matrix's row for the catalog rows read, and leaves out two roles search admits", () => {
    expect([...CATALOG_ROWS_ROLES].sort()).toEqual(["Analyst", "MetadataAdmin", "PlatformAdmin", "Viewer"]);
    // The reason the list exists: DataAdmin and DataSteward can search and cannot open the Catalog.
    expect(CATALOG_ROWS_ROLES).not.toContain("DataAdmin");
    expect(CATALOG_ROWS_ROLES).not.toContain("DataSteward");
  });
});

describe("objectTypeLabel", () => {
  it("reads the type codes the API sends", () => {
    expect(objectTypeLabel("TABLE")).toBe("table");
    expect(objectTypeLabel("TABLE", true)).toBe("tables");
    expect(objectTypeLabel("COLUMN", true)).toBe("columns");
  });

  it("shows a code it has never seen as it came, lower-cased, rather than hiding it", () => {
    expect(objectTypeLabel("GLOSSARY_TERM")).toBe("glossary term");
    expect(objectTypeLabel("GLOSSARY_TERM", true)).toBe("glossary terms");
  });
});
