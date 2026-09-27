import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The search client in DEMO mode (R11-AUD08): answers built from the demo
   catalog, in the shape the server answers in, with no network request.

   `demoOr` is not mocked -- it is the door the demo catalog comes through, and
   the property under test is that the demo answer is the SAME SHAPE the screen
   reads from the live one: TABLE hits carrying their datasource, facets that
   count what the page total counts, paging by limit and offset.
--------------------------------------------------------------------------- */

const ORG = "org-demo";

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no request in demo mode"))));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("demo search", () => {
  it("finds demo tables by name, as TABLE hits with their datasource, and sends nothing", async () => {
    const { fetchSearchResults } = await import("./search");

    const page = await fetchSearchResults(ORG, { q: "customer" });

    expect(page.total).toBeGreaterThan(0);
    expect(page.items.length).toBeLessThanOrEqual(25);
    for (const hit of page.items) {
      expect(hit.object_type).toBe("TABLE");
      expect(hit.display_name.toLowerCase()).toContain("customer");
      expect(hit.datasource_id).toMatch(/^ds_/);
      expect(hit.evidence.object_id).toBe(hit.object_id);
    }
    // The facet counts what the total counts.
    expect(page.facets).toEqual([{ field: "object_type", value: "TABLE", count: page.total }]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("pages by limit and offset over one answer", async () => {
    const { fetchSearchResults } = await import("./search");

    const first = await fetchSearchResults(ORG, { q: "customer", limit: 5, offset: 0 });
    const second = await fetchSearchResults(ORG, { q: "customer", limit: 5, offset: 5 });

    expect(first.items).toHaveLength(5);
    expect(second.offset).toBe(5);
    expect(second.total).toBe(first.total);
    expect(second.items.map((hit) => hit.object_id)).not.toEqual(first.items.map((hit) => hit.object_id));
  });

  it("answers a column filter with nothing: the demo estate lists no columns", async () => {
    const { fetchSearchResults } = await import("./search");

    const page = await fetchSearchResults(ORG, { q: "customer", objectType: "COLUMN" });

    expect(page).toMatchObject({ items: [], facets: [], total: 0 });
  });

  it("says nothing matched when nothing did", async () => {
    const { fetchSearchResults } = await import("./search");

    const page = await fetchSearchResults(ORG, { q: "zzzznotatable" });

    expect(page).toMatchObject({ items: [], facets: [], total: 0 });
  });
});

describe("demo suggestions", () => {
  it("are table names in the API's suggestion shape, capped at the limit", async () => {
    const { fetchSearchSuggestions } = await import("./search");

    const suggestions = await fetchSearchSuggestions(ORG, "customer", 3);

    expect(suggestions.length).toBeGreaterThan(0);
    expect(suggestions.length).toBeLessThanOrEqual(3);
    for (const suggestion of suggestions) {
      expect(suggestion.object_type).toBe("TABLE");
      expect(suggestion.text).toBe(suggestion.display_name);
      expect(suggestion.display_name.toLowerCase()).toContain("customer");
    }
    expect(fetch).not.toHaveBeenCalled();
  });
});
