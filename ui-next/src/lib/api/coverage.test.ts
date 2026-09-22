import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The stewardship coverage client (R11-AUD08): the three requests it sends and
   the demo answers it gives.

   What is worth pinning is what a caller cannot see from the screen: WHICH
   query field carries the scope, that the history is asked for exactly the scope
   the figures were, that the snapshot is a POST with no body (the server takes
   the scope from the query string and a body would be a second, ignored way to
   say it), and that the role lists are the surface-control matrix's.
--------------------------------------------------------------------------- */

const get = vi.fn();
const postJson = vi.fn();

vi.mock("./transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./transport")>();
  return {
    ...actual,
    get: (...args: unknown[]) => get(...args),
    postJson: (...args: unknown[]) => postJson(...args),
    // Pinned to the live arm: the request a deployed client sends.
    demoOr: (_demo: unknown, live: () => Promise<unknown>) => live(),
  };
});

const ORG = "org-1";

async function load() {
  return import("./coverage");
}

beforeEach(() => {
  get.mockReset();
  postJson.mockReset();
  vi.resetModules();
});

describe("the coverage read", () => {
  it("asks for the whole organization with no query at all", async () => {
    get.mockResolvedValue({});
    const { fetchStewardshipCoverage } = await load();

    await fetchStewardshipCoverage(ORG);

    expect(get.mock.calls[0]![0]).toBe("/v1/organizations/org-1/stewardship/coverage");
  });

  it("narrows to one datasource with `datasource_id`, and to nothing else", async () => {
    get.mockResolvedValue({});
    const { fetchStewardshipCoverage } = await load();

    await fetchStewardshipCoverage(ORG, { datasourceId: "ds-9" });

    expect(get.mock.calls[0]![0]).toBe("/v1/organizations/org-1/stewardship/coverage?datasource_id=ds-9");
  });

  it("treats a null or empty datasource as the whole organization", async () => {
    get.mockResolvedValue({});
    const { fetchStewardshipCoverage } = await load();

    await fetchStewardshipCoverage(ORG, { datasourceId: null });
    await fetchStewardshipCoverage(ORG, { datasourceId: "" });

    expect(get.mock.calls.map((call) => call[0])).toEqual([
      "/v1/organizations/org-1/stewardship/coverage",
      "/v1/organizations/org-1/stewardship/coverage",
    ]);
  });

  it("passes the abort signal through", async () => {
    get.mockResolvedValue({});
    const { fetchStewardshipCoverage } = await load();
    const controller = new AbortController();

    await fetchStewardshipCoverage(ORG, {}, controller.signal);

    expect(get.mock.calls[0]![1]).toBe(controller.signal);
  });
});

describe("the business-domain and line-of-business scopes (R11-VAL06)", () => {
  it("narrows to one business domain with `domain_id`", async () => {
    get.mockResolvedValue({});
    const { fetchStewardshipCoverage } = await load();

    await fetchStewardshipCoverage(ORG, { domainId: "dom-1" });

    expect(get.mock.calls[0]![0]).toBe("/v1/organizations/org-1/stewardship/coverage?domain_id=dom-1");
  });

  it("narrows to one line of business with `line_of_business_id`, for the history too", async () => {
    get.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    const { fetchCoverageSnapshots } = await load();

    await fetchCoverageSnapshots(ORG, { lineOfBusinessId: "lob-1" });

    expect(get.mock.calls[0]![0]).toBe(
      "/v1/organizations/org-1/stewardship/coverage/snapshots?line_of_business_id=lob-1&limit=50&offset=0",
    );
  });

  it("stores a snapshot under the domain it names, in the query string", async () => {
    postJson.mockResolvedValue({});
    const { takeCoverageSnapshot } = await load();

    await takeCoverageSnapshot(ORG, { domainId: "dom-1" });

    expect(postJson.mock.calls[0]![0]).toBe(
      "/v1/organizations/org-1/stewardship/coverage/snapshots?domain_id=dom-1",
    );
    expect(postJson.mock.calls[0]![1]).toBeUndefined();
  });

  it("lists the domains from the business map's DOMAIN nodes, by name, with the map's truncation", async () => {
    get.mockResolvedValue({
      organization_id: ORG,
      nodes: [
        { id: "domain:d-2", node_type: "DOMAIN", label: "Risk", parent_id: null, metadata: {} },
        { id: "entity:e-1", node_type: "ENTITY", label: "Loan", parent_id: "domain:d-2", metadata: {} },
        { id: "domain:d-1", node_type: "DOMAIN", label: "Customer", parent_id: null, metadata: {} },
        { id: "table:t-1", node_type: "TABLE", label: "loan", parent_id: "entity:e-1", metadata: {} },
      ],
      edges: [],
      domain_count: 2,
      entity_count: 1,
      table_count: 1,
      cross_domain_edge_count: 0,
      truncated: true,
    });
    const { fetchCoverageDomains } = await load();

    const answer = await fetchCoverageDomains(ORG);

    expect(get.mock.calls[0]![0]).toBe("/v1/organizations/org-1/business-map?limit=2000");
    expect(answer).toEqual({
      domains: [
        { id: "d-1", name: "Customer" },
        { id: "d-2", name: "Risk" },
      ],
      incomplete: true,
    });
  });
});

describe("the snapshot history read", () => {
  it("asks for the same scope the figures were, plus its page", async () => {
    get.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    const { fetchCoverageSnapshots } = await load();

    await fetchCoverageSnapshots(ORG, { datasourceId: "ds-9" }, { limit: 12, offset: 24 });

    const url = new URL(get.mock.calls[0]![0] as string, "http://x");
    expect(url.pathname).toBe("/v1/organizations/org-1/stewardship/coverage/snapshots");
    expect(Object.fromEntries(url.searchParams)).toEqual({ datasource_id: "ds-9", limit: "12", offset: "24" });
  });

  it("sends no scope field for the organization, which is how the server selects its history", async () => {
    get.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    const { fetchCoverageSnapshots } = await load();

    await fetchCoverageSnapshots(ORG);

    const url = new URL(get.mock.calls[0]![0] as string, "http://x");
    expect(url.searchParams.has("datasource_id")).toBe(false);
    // The server's default page is 100; this client asks for fewer unless told otherwise.
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("offset")).toBe("0");
  });
});

describe("taking a snapshot", () => {
  it("POSTs the scope in the query string and no body", async () => {
    postJson.mockResolvedValue({});
    const { takeCoverageSnapshot } = await load();

    await takeCoverageSnapshot(ORG, { datasourceId: "ds-9" });

    const [path, body] = postJson.mock.calls[0]!;
    expect(path).toBe("/v1/organizations/org-1/stewardship/coverage/snapshots?datasource_id=ds-9");
    expect(body).toBeUndefined();
  });

  it("POSTs the organization's snapshot with no query", async () => {
    postJson.mockResolvedValue({});
    const { takeCoverageSnapshot } = await load();

    await takeCoverageSnapshot(ORG);

    expect(postJson.mock.calls[0]![0]).toBe("/v1/organizations/org-1/stewardship/coverage/snapshots");
  });

  it("lets a refusal reach the caller untouched", async () => {
    const refusal = new Error("requires DataSteward");
    postJson.mockRejectedValue(refusal);
    const { takeCoverageSnapshot } = await load();

    await expect(takeCoverageSnapshot(ORG)).rejects.toBe(refusal);
  });
});

describe("the role lists", () => {
  it("are the surface-control matrix's, read and write", async () => {
    const { COVERAGE_READ_ROLES, COVERAGE_SNAPSHOT_ROLES } = await load();

    // Rows for `get_stewardship_coverage` / `list_stewardship_coverage_snapshots` and
    // `snapshot_stewardship_coverage` in Docs/50-security/surface-control-matrix.md.
    expect([...COVERAGE_READ_ROLES].sort()).toEqual(
      ["Analyst", "Auditor", "DataAdmin", "DataSteward", "MetadataAdmin", "PlatformAdmin", "Reviewer", "SemanticAdmin", "Viewer"],
    );
    expect([...COVERAGE_SNAPSHOT_ROLES].sort()).toEqual(
      ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"],
    );
  });
});
