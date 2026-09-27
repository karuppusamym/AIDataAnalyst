import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The source list, against the wire (review finding F15, tracker R11-D7).

   Both tests here are about the difference between a picker that asks the
   server and a picker that rummages through the page it already has. That
   difference is invisible on a development estate -- one page holds
   everything, so a client filter and a server search agree, and a selected id
   is always in the list. It only shows up on a fleet larger than the cap,
   where the wrong one silently reports that a source does not exist.

   So these assert the REQUESTS, not the returned array: the URL carries `q=`,
   and an id the pages never contained is fetched by id. An assertion on the
   array alone would pass against a client filter.

   `./transport` is mocked rather than `fetch`, which is this codebase's
   existing seam for endpoint-shape tests (`api/columnDocumentation.test.ts`).
--------------------------------------------------------------------------- */

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("./transport", () => ({
  get,
  // Live path only: `demoOr` would otherwise answer from fixtures and no
  // request would be made at all, which is the one thing these tests read.
  demoOr: <T,>(_demo: unknown, live: () => Promise<T>) => live(),
}));

import { listOrgDatasources } from "./identity";

const ORG = "org1";

/** A page the server would answer with: `limit` rows out of a larger `total`,
 *  which is what makes `collectPages` keep going. */
function page(offset: number, count: number, total: number) {
  return {
    items: Array.from({ length: count }, (_, index) => ({
      id: `ds_${offset + index}`,
      name: `source ${offset + index}`,
    })),
    limit: 500,
    offset,
    total,
  };
}

beforeEach(() => {
  get.mockReset();
});

describe("listOrgDatasources", () => {
  it("resolves a selected id no loaded page contains, by id, in one request", async () => {
    // A fleet deeper than the page budget: five full pages are collected and
    // 500 sources are still unread, so `ds_far` is genuinely unreachable by
    // paging any further than a picker is willing to page.
    const total = 3000;
    get.mockImplementation((path: string) => {
      if (path.startsWith("/v1/datasources/")) {
        return Promise.resolve({ id: "ds_far", name: "the linked source" });
      }
      const offset = Number(/offset=(\d+)/.exec(path)?.[1] ?? 0);
      return Promise.resolve(page(offset, 500, total));
    });

    const list = await listOrgDatasources(ORG, undefined, { selectedId: "ds_far" });

    expect(get).toHaveBeenCalledWith("/v1/datasources/ds_far", undefined);
    expect(list.items[0]).toEqual({ id: "ds_far", name: "the linked source" });
    // The splice describes what is on screen; it must not make a truncated
    // fleet look like a complete one.
    expect(list.truncated).toBe(true);
    expect(list.total).toBe(total);
  });

  it("does not spend a request resolving an id the pages already hold", async () => {
    get.mockResolvedValue(page(0, 3, 3));

    const list = await listOrgDatasources(ORG, undefined, { selectedId: "ds_1" });

    expect(get).toHaveBeenCalledTimes(1);
    expect(list.items.filter((item) => item.id === "ds_1")).toHaveLength(1);
  });

  it("sends the search to the server rather than filtering the page it holds", async () => {
    // The unsearched fleet is capped, and the source being searched for is
    // past the cap: a client-side filter over these rows could only ever
    // answer "no such source".
    const unsearched = page(0, 500, 3000);
    expect(unsearched.items.some((item) => item.name.includes("ledger"))).toBe(false);

    get.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes("q=")
          ? { items: [{ id: "ds_2900", name: "oracle ledger" }], limit: 500, offset: 0, total: 1 }
          : unsearched,
      ),
    );

    const list = await listOrgDatasources(ORG, undefined, { search: "ledger" });

    expect(get).toHaveBeenCalledWith(
      "/v1/organizations/org1/datasources?limit=500&offset=0&q=ledger",
      undefined,
    );
    expect(list.items).toEqual([{ id: "ds_2900", name: "oracle ledger" }]);
  });

  it("treats a blank search as no search, so the URL keeps its meaning", async () => {
    get.mockResolvedValue(page(0, 2, 2));

    await listOrgDatasources(ORG, undefined, { search: "   " });

    expect(get).toHaveBeenCalledWith(
      "/v1/organizations/org1/datasources?limit=500&offset=0",
      undefined,
    );
  });

  it("escapes a search term instead of letting it write query parameters", async () => {
    get.mockResolvedValue(page(0, 1, 1));

    await listOrgDatasources(ORG, undefined, { search: "a&limit=1" });

    expect(get).toHaveBeenCalledWith(
      "/v1/organizations/org1/datasources?limit=500&offset=0&q=a%26limit%3D1",
      undefined,
    );
  });
});
