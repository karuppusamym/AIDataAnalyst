import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   Ownership's five calls and the leaver preview (R11-AUD08, part 2): the URL and
   body each sends, and what the preview does to a listing that has no owner filter.

   The properties:
     * `apply` sends NO body of substance -- it takes none, and a body that named
       tables would suggest a preview or a selection the route does not have;
     * the leaver request names ids only when it is given ids (`assignment_ids`
       omitted means "the server discovers the whole portfolio", which is a different
       request from "an empty list", and an empty list is a 422);
     * the preview keeps exactly the ACTIVE rows of that principal AND that owner
       type, each once, and says when it stopped before the end -- including when a
       page comes back empty, which must not become an unbounded loop.
--------------------------------------------------------------------------- */

const { get, postJson, fetchOwnershipAssignments, demoData } = vi.hoisted(() => ({
  get: vi.fn(),
  postJson: vi.fn(),
  fetchOwnershipAssignments: vi.fn(),
  // What `demoOr` hands its demo arm, when a test turns demo data on.
  demoData: { on: false, fixtures: {} as unknown },
}));

vi.mock("./transport", () => ({
  get: (...args: unknown[]) => get(...args),
  postJson: (...args: unknown[]) => postJson(...args),
  putJson: vi.fn(),
  // The live arm unless a test turns demo data on: the request is the one a deployed client sends.
  demoOr: (demo: (fixtures: unknown) => Promise<unknown>, live: () => Promise<unknown>) =>
    demoData.on ? demo(demoData.fixtures) : live(),
}));
vi.mock("./catalog", () => ({
  fetchOwnershipAssignments: (...args: unknown[]) => fetchOwnershipAssignments(...args),
}));

import {
  OWNERSHIP_PORTFOLIO_MAX_PAGES,
  applyOwnershipRule,
  createOwnershipRule,
  fetchOwnershipOperations,
  fetchOwnershipPortfolio,
  fetchOwnershipRules,
  requestLeaverReassignment,
} from "./ownership";

const ORG = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87";

beforeEach(() => {
  get.mockReset();
  postJson.mockReset();
  fetchOwnershipAssignments.mockReset();
  demoData.on = false;
  demoData.fixtures = {};
});

describe("the ownership rule calls", () => {
  it("reads the organization's rules, passing the signal", async () => {
    const rules = { items: [{ id: "r1" }], limit: 500, offset: 0, total: 1 };
    get.mockResolvedValue(rules);
    const controller = new AbortController();

    await expect(fetchOwnershipRules(ORG, controller.signal)).resolves.toBe(rules);

    expect(get).toHaveBeenCalledWith(`/v1/organizations/${ORG}/ownership-rules`, controller.signal);
  });

  it("creates a rule with exactly the body it was given", async () => {
    const body = {
      rule_key: "retail-tables", display_name: "Retail tables", match_field: "SCHEMA_NAME" as const,
      match_pattern: "retail*", owner_type: "GROUP" as const, owner_principal: "retail-data-stewards",
    };
    postJson.mockResolvedValue({ id: "r1", ...body });

    await createOwnershipRule(ORG, body);

    expect(postJson).toHaveBeenCalledWith(`/v1/organizations/${ORG}/ownership-rules`, body, undefined);
  });

  it("applies a rule by id with nothing to say about which tables: the route takes no selection", async () => {
    const operation = { id: "op1", status: "REVIEW_REQUIRED", applied_count: 0 };
    postJson.mockResolvedValue(operation);

    await expect(applyOwnershipRule("rule-1")).resolves.toBe(operation);

    expect(postJson).toHaveBeenCalledWith("/v1/ownership-rules/rule-1/apply", {}, undefined);
  });

  it("lets a server refusal through untouched", async () => {
    const refusal = Object.assign(new Error("ownership rule matched no active tables"), { status: 409 });
    postJson.mockRejectedValue(refusal);

    await expect(applyOwnershipRule("rule-1")).rejects.toBe(refusal);
  });
});

describe("the leaver request", () => {
  it("omits assignment_ids when none were named, so the server discovers the portfolio", async () => {
    postJson.mockResolvedValue({ id: "op1" });
    const body = { leaving_principal: "priya", successor_principal: "morgan", owner_type: "INDIVIDUAL" as const, rationale: "Priya left." };

    await requestLeaverReassignment(ORG, body);

    expect(postJson).toHaveBeenCalledWith(`/v1/organizations/${ORG}/stewardship/leaver-reassignment`, body, undefined);
    expect(postJson.mock.calls[0]![1]).not.toHaveProperty("assignment_ids");
  });

  it("sends the ids it was given, and the signal", async () => {
    postJson.mockResolvedValue({ id: "op1" });
    const controller = new AbortController();
    const body = {
      leaving_principal: "priya", successor_principal: "morgan", owner_type: "GROUP" as const,
      rationale: "Priya left.", assignment_ids: ["a", "b"],
    };

    await requestLeaverReassignment(ORG, body, controller.signal);

    expect(postJson).toHaveBeenCalledWith(`/v1/organizations/${ORG}/stewardship/leaver-reassignment`, body, controller.signal);
  });
});

describe("the operations list", () => {
  it("reads the newest 100 by default and states the paging it asked for", async () => {
    get.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });

    await fetchOwnershipOperations(ORG);

    expect(get).toHaveBeenCalledWith(`/v1/organizations/${ORG}/stewardship/bulk-operations?limit=100&offset=0`, undefined);
  });

  it("sends the status, limit and offset it was given", async () => {
    get.mockResolvedValue({ items: [], limit: 500, offset: 20, total: 0 });
    const controller = new AbortController();

    await fetchOwnershipOperations(ORG, { status: "APPLIED", limit: 500, offset: 20 }, controller.signal);

    expect(get).toHaveBeenCalledWith(
      `/v1/organizations/${ORG}/stewardship/bulk-operations?status=APPLIED&limit=500&offset=20`,
      controller.signal,
    );
  });
});

describe("the leaver preview", () => {
  const row = (id: string, overrides: Record<string, unknown> = {}) => ({
    id, status: "ACTIVE", owner_principal: "priya", owner_type: "INDIVIDUAL", subject_type: "TABLE", subject_id: `t_${id}`,
    ...overrides,
  });

  it("keeps the ACTIVE rows of that principal and that owner type, and nothing else", async () => {
    fetchOwnershipAssignments.mockResolvedValue({
      items: [
        row("a"),
        row("b", { owner_principal: "morgan" }),
        row("c", { owner_type: "GROUP" }),
        row("d", { status: "LAPSED" }),
        row("e"),
      ],
      limit: 500, offset: 0, total: 5,
    });

    const found = await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL");

    expect(found.items.map((item) => item.id)).toEqual(["a", "e"]);
    expect(found).toMatchObject({ total: 5, scanned: 5, complete: true });
  });

  it("matches the principal exactly, capital letters included", async () => {
    fetchOwnershipAssignments.mockResolvedValue({ items: [row("a", { owner_principal: "Priya" })], limit: 500, offset: 0, total: 1 });

    expect((await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL")).items).toEqual([]);
  });

  it("pages 500 at a time until it has read what the listing says exists", async () => {
    fetchOwnershipAssignments
      .mockResolvedValueOnce({ items: Array.from({ length: 500 }, (_, i) => row(`p${i}`, { owner_principal: "other" })), limit: 500, offset: 0, total: 700 })
      .mockResolvedValueOnce({ items: Array.from({ length: 200 }, (_, i) => row(`q${i}`)), limit: 500, offset: 500, total: 700 });
    const controller = new AbortController();

    const found = await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL", controller.signal);

    expect(fetchOwnershipAssignments).toHaveBeenCalledTimes(2);
    expect(fetchOwnershipAssignments).toHaveBeenNthCalledWith(1, ORG, { limit: 500, offset: 0 }, controller.signal);
    expect(fetchOwnershipAssignments).toHaveBeenNthCalledWith(2, ORG, { limit: 500, offset: 500 }, controller.signal);
    expect(found.items).toHaveLength(200);
    expect(found).toMatchObject({ total: 700, scanned: 700, complete: true });
  });

  it("counts a row a page boundary repeated once", async () => {
    fetchOwnershipAssignments
      .mockResolvedValueOnce({ items: [row("a"), row("b")], limit: 500, offset: 0, total: 4 })
      .mockResolvedValueOnce({ items: [row("b"), row("c")], limit: 500, offset: 2, total: 4 });

    const found = await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL");

    expect(found.items.map((item) => item.id)).toEqual(["a", "b", "c"]);
  });

  it("stops at its page bound and says it did not read everything", async () => {
    fetchOwnershipAssignments.mockImplementation(async (_org: string, query: { offset: number }) => ({
      items: Array.from({ length: 500 }, (_, i) => row(`x${query.offset + i}`, { owner_principal: "other" })),
      limit: 500, offset: query.offset, total: 1_000_000,
    }));

    const found = await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL");

    expect(fetchOwnershipAssignments).toHaveBeenCalledTimes(OWNERSHIP_PORTFOLIO_MAX_PAGES);
    expect(found).toMatchObject({ scanned: 500 * OWNERSHIP_PORTFOLIO_MAX_PAGES, total: 1_000_000, complete: false });
  });

  it("does not loop on a listing that returns nothing before it reaches its own total", async () => {
    fetchOwnershipAssignments.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 900 });

    const found = await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL");

    expect(fetchOwnershipAssignments).toHaveBeenCalledTimes(1);
    expect(found).toMatchObject({ items: [], scanned: 0, total: 900, complete: false });
  });

  it("reads an empty organization as complete", async () => {
    fetchOwnershipAssignments.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });

    expect(await fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL")).toMatchObject({ items: [], complete: true });
  });

  it("lets a listing failure through, so a failed preview is never an empty portfolio", async () => {
    const failure = Object.assign(new Error("database unavailable"), { status: 503 });
    fetchOwnershipAssignments.mockRejectedValue(failure);

    await expect(fetchOwnershipPortfolio(ORG, "priya", "INDIVIDUAL")).rejects.toBe(failure);
  });
});

describe("under demo data", () => {
  it("answers the leaver request from the assignments the fixture module is handed in, and sends nothing", async () => {
    // `ownershipFixtures` must not import `../fixtures` itself (`demoDataMode.test.ts` allows one dynamic
    // reference, the transport seam's), so the demo arm passes the reader in. A private import would
    // ignore this stub and fail here.
    const readAssignments = vi.fn(async () => ({
      items: [
        { id: "z1", status: "ACTIVE", owner_principal: "priya", owner_type: "INDIVIDUAL" },
        { id: "z2", status: "ACTIVE", owner_principal: "someone-else", owner_type: "INDIVIDUAL" },
      ],
      limit: 500, offset: 0, total: 2,
    }));
    demoData.on = true;
    demoData.fixtures = { makeFixtureOwnershipAssignments: readAssignments };

    const operation = await requestLeaverReassignment(ORG, {
      leaving_principal: "priya", successor_principal: "morgan", owner_type: "INDIVIDUAL", rationale: "Priya left the bank.",
    });

    expect(readAssignments).toHaveBeenCalledWith({ limit: 500 });
    expect(operation).toMatchObject({ operation_type: "REASSIGN_LEAVER", subject_ids: ["z1"], status: "REVIEW_REQUIRED", applied_count: 0 });
    expect(postJson).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });

  it("reads the rules, applies one and lists the request from the demo store, sending nothing", async () => {
    demoData.on = true;

    const rules = await fetchOwnershipRules(ORG);
    const operation = await applyOwnershipRule("rule_retail_tables");
    const operations = await fetchOwnershipOperations(ORG);

    expect(rules.items.map((rule) => rule.id)).toContain("rule_retail_tables");
    expect(operation.status).toBe("REVIEW_REQUIRED");
    expect(operations.items.map((item) => item.id)).toContain(operation.id);
    expect(postJson).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
  });
});
