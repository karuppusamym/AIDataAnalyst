import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The search client (R11-AUD08): the two requests it sends, in live mode, and
   what its demo answers look like.

   Worth pinning because the shape is not the usual one: `organization_id` is a
   REQUIRED QUERY FIELD here, not a path segment (a request without it is a 422),
   the paging fields are `limit`/`offset`, the type filter is `object_type`, and
   `q` must survive being a real sentence -- spaces, ampersands, non-ASCII --
   which is what `URLSearchParams` is for and string concatenation is not.
--------------------------------------------------------------------------- */

const get = vi.fn();

vi.mock("./transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./transport")>();
  return {
    ...actual,
    get: (...args: unknown[]) => get(...args),
    demoOr: (_demo: unknown, live: () => Promise<unknown>) => live(),
  };
});

const ORG = "org-1";

async function load() {
  return import("./search");
}

const requestOf = (call: number) => new URL(get.mock.calls[call]![0] as string, "http://x");

beforeEach(() => {
  get.mockReset();
  vi.resetModules();
});

describe("the search request", () => {
  it("names the organization in the query, with the API's own paging fields", async () => {
    get.mockResolvedValue({ items: [], facets: [], total: 0, limit: 25, offset: 0 });
    const { fetchSearchResults } = await load();

    await fetchSearchResults(ORG, { q: "customer" });

    const url = requestOf(0);
    expect(url.pathname).toBe("/v1/search");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: "customer",
      organization_id: "org-1",
      limit: "25",
      offset: "0",
    });
  });

  it("sends the type filter as `object_type`, and only when there is one", async () => {
    get.mockResolvedValue({ items: [], facets: [], total: 0, limit: 25, offset: 0 });
    const { fetchSearchResults } = await load();

    await fetchSearchResults(ORG, { q: "id", objectType: "COLUMN", limit: 10, offset: 30 });
    await fetchSearchResults(ORG, { q: "id", objectType: null });

    expect(Object.fromEntries(requestOf(0).searchParams)).toMatchObject({
      object_type: "COLUMN",
      limit: "10",
      offset: "30",
    });
    expect(requestOf(1).searchParams.has("object_type")).toBe(false);
  });

  it("encodes a query that is not a plain word", async () => {
    get.mockResolvedValue({ items: [], facets: [], total: 0, limit: 25, offset: 0 });
    const { fetchSearchResults } = await load();

    await fetchSearchResults(ORG, { q: "risk & exposure=α" });

    // Read back through the URL parser: what arrives is what was typed.
    expect(requestOf(0).searchParams.get("q")).toBe("risk & exposure=α");
    expect(requestOf(0).searchParams.get("organization_id")).toBe("org-1");
  });

  it("passes the abort signal through", async () => {
    get.mockResolvedValue({ items: [], facets: [], total: 0, limit: 25, offset: 0 });
    const { fetchSearchResults } = await load();
    const controller = new AbortController();

    await fetchSearchResults(ORG, { q: "x" }, controller.signal);

    expect(get.mock.calls[0]![1]).toBe(controller.signal);
  });
});

describe("the suggest request", () => {
  it("asks for a prefix of table names, in the same organization, with a small limit", async () => {
    get.mockResolvedValue([]);
    const { fetchSearchSuggestions } = await load();

    await fetchSearchSuggestions(ORG, "cust");

    const url = requestOf(0);
    expect(url.pathname).toBe("/v1/search/suggest");
    expect(Object.fromEntries(url.searchParams)).toEqual({ q: "cust", organization_id: "org-1", limit: "8" });
  });

  it("takes the caller's limit", async () => {
    get.mockResolvedValue([]);
    const { fetchSearchSuggestions } = await load();

    await fetchSearchSuggestions(ORG, "cust", 6);

    expect(requestOf(0).searchParams.get("limit")).toBe("6");
  });
});

describe("the role list", () => {
  it("is the surface-control matrix's row for both routes", async () => {
    const { SEARCH_ROLES } = await load();

    // `global_search` and `search_suggest` both: Analyst, DataAdmin, DataSteward, PlatformAdmin, Viewer.
    expect([...SEARCH_ROLES].sort()).toEqual(["Analyst", "DataAdmin", "DataSteward", "PlatformAdmin", "Viewer"]);
    // An Auditor-only or Reviewer-only session is refused by the server, so it must not be sent the request.
    expect(SEARCH_ROLES).not.toContain("Auditor");
    expect(SEARCH_ROLES).not.toContain("Reviewer");
  });
});
