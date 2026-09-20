import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   `fetchUnownedAssetBacklog` and the candidate-owner filter (R11-S13).

   `list_unowned_asset_backlog` takes `candidate_owner` now: an exact,
   case-sensitive match on the owner as stored, applied before paging. The screen
   test mocks this function, so what the request actually carries -- and what the
   demo fixture answers for the same query -- is pinned here, on both arms.
--------------------------------------------------------------------------- */

const get = vi.fn();

vi.mock("./transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./transport")>();
  return {
    ...actual,
    get: (...args: unknown[]) => get(...args),
    // Pinned to the live arm: the request is the one a deployed client sends.
    demoOr: (_demo: unknown, live: () => Promise<unknown>) => live(),
  };
});

const ORG = "org-1";

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
  vi.resetModules();
});

async function load() {
  return import("./catalog");
}

function requested(): URL {
  const [path] = get.mock.calls.at(-1)!;
  return new URL(String(path), "http://api.test");
}

describe("fetchUnownedAssetBacklog", () => {
  it("sends the candidate owner as `candidate_owner`, beside status, limit and offset", async () => {
    const { fetchUnownedAssetBacklog } = await load();

    await fetchUnownedAssetBacklog(ORG, {
      status: "ROUTED",
      candidateOwner: "Finance Data",
      limit: 50,
      offset: 100,
    });

    const url = requested();
    expect(url.pathname).toBe(`/v1/organizations/${ORG}/stewardship/unowned-backlog`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      status: "ROUTED",
      candidate_owner: "Finance Data",
      limit: "50",
      offset: "100",
    });
  });

  it("sends the value exactly as given: encoded, never lower-cased, trimmed or split", async () => {
    const { fetchUnownedAssetBacklog } = await load();
    const owner = "Risk Data/Stewards+Ops&Co@Tenant.example";

    await fetchUnownedAssetBacklog(ORG, { candidateOwner: owner });

    expect(requested().searchParams.get("candidate_owner")).toBe(owner);
    expect([...requested().searchParams.keys()].filter((key) => key === "candidate_owner")).toHaveLength(1);
  });

  it.each([null, undefined, ""])("sends no `candidate_owner` for %j", async (none) => {
    const { fetchUnownedAssetBacklog } = await load();

    await fetchUnownedAssetBacklog(ORG, { candidateOwner: none });

    expect(requested().searchParams.has("candidate_owner")).toBe(false);
    expect(Object.fromEntries(requested().searchParams)).toEqual({ limit: "100", offset: "0" });
  });
});

describe("the demo fixture answers the same query the route does", () => {
  const FIXTURE_ORG = "00000000-0000-0000-0000-000000000001";

  it("matches the candidate owner exactly and case-sensitively", async () => {
    const { makeFixtureUnownedAssetBacklog } = await import("../fixtures");

    const exact = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, { candidateOwner: "Finance Data" });
    const otherCase = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, { candidateOwner: "finance data" });
    const partial = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, { candidateOwner: "Finance" });

    expect(exact.items.map((item) => item.candidate_owner)).toEqual(["Finance Data"]);
    expect(exact.total).toBe(1);
    expect(otherCase.total).toBe(0);
    expect(partial.total).toBe(0);
  });

  it("counts the matches in `total` and applies the filter before paging, beside status", async () => {
    const { makeFixtureUnownedAssetBacklog } = await import("../fixtures");
    const everything = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, {});

    const firstPage = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, {
      candidateOwner: "Finance Data",
      limit: 1,
      offset: 0,
    });
    const pastTheMatches = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, {
      candidateOwner: "Finance Data",
      limit: 1,
      offset: 1,
    });
    const wrongStatus = await makeFixtureUnownedAssetBacklog(FIXTURE_ORG, {
      status: "ROUTED",
      candidateOwner: "Finance Data",
    });

    expect(everything.total).toBeGreaterThan(1);
    expect(firstPage.items).toHaveLength(1);
    expect(firstPage.total).toBe(1);
    expect(pastTheMatches.items).toEqual([]);
    expect(wrongStatus.total).toBe(0);
  });
});
