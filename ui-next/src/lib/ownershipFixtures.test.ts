import { beforeEach, describe, expect, it, vi } from "vitest";

import { makeFixtureOwnershipAssignments } from "./fixtures";

/* ---------------------------------------------------------------------------
   The demo ownership data (R11-AUD08, part 2) behaves the way the server does,
   because a demo that is kinder than the API teaches a steward something false.

   What is pinned here is what `aida.stewardship_api` does, not what would demo well:
   `fnmatchcase` over case-folded values, TAG matching ANY tag, a table with no domain
   never matching DOMAIN_KEY, a key that is unique, and -- above all -- an apply or a
   leaver request that opens a review at `REVIEW_REQUIRED` with `applied_count` 0 and
   assigns nobody.
--------------------------------------------------------------------------- */

/** The store is module-level, as the server's tables are: every test starts from a fresh one. */
async function load() {
  vi.resetModules();
  return import("./ownershipFixtures");
}

beforeEach(() => {
  vi.resetModules();
});

describe("globMatches: fnmatchcase over case-folded values", () => {
  it.each([
    ["retail", "retail", true],
    ["Retail", "retail", true],
    ["retail", "RETAIL", true],
    ["retail_orders", "retail*", true],
    ["retail_orders", "*orders", true],
    ["retail_orders", "*_ord*", true],
    ["retail_orders", "retail", false],
    ["retail_orders", "retail_order?", true],
    ["retail_orders", "retail_order", false],
    ["ab", "a[bc]", true],
    ["ad", "a[bc]", false],
    ["ad", "a[!bc]", true],
    ["a.b", "a.b", true],
    ["axb", "a.b", false], // "." is a literal dot, not "any character"
    ["a(b)", "a(b)", true],
    ["a+b", "a+b", true],
    ["a\nb", "a*b", true], // fnmatch's `*` crosses a newline
  ])("%j against %j is %s", async (value, pattern, expected) => {
    const { globMatches } = await load();

    expect(globMatches(value, pattern)).toBe(expected);
  });

  it("treats an unclosed [ literally rather than throwing", async () => {
    const { globMatches } = await load();

    expect(globMatches("a[b", "a[b")).toBe(true);
  });
});

describe("the demo rules", () => {
  it("lists the seeded ACTIVE rules by display name", async () => {
    const { fixtureOwnershipRules } = await load();

    const page = await fixtureOwnershipRules("org");

    expect(page.items.map((rule) => rule.display_name)).toEqual(["Anything tagged PII", "Retail tables"]);
    expect(page).toMatchObject({ limit: 500, offset: 0, total: 2 });
  });

  it("creates a rule, lists it, and refuses a second one under the same key with the server's 409", async () => {
    const { fixtureCreateOwnershipRule, fixtureOwnershipRules } = await load();
    const body = {
      rule_key: "treasury", display_name: "Treasury", match_field: "SCHEMA_NAME" as const, match_pattern: "treasury",
      owner_type: "GROUP" as const, owner_principal: "treasury-stewards",
    };

    const created = await fixtureCreateOwnershipRule("org", body);

    expect(created).toMatchObject({ ...body, status: "ACTIVE", organization_id: "org", created_by: "dev-fixture-user" });
    expect((await fixtureOwnershipRules("org")).items.map((rule) => rule.rule_key)).toContain("treasury");
    await expect(fixtureCreateOwnershipRule("org", body)).rejects.toMatchObject({
      status: 409,
      detail: "ownership rule key already exists",
    });
  });
});

describe("applying a demo rule", () => {
  it("opens a review at REVIEW_REQUIRED for the tables it matches, and assigns nobody", async () => {
    const { fixtureApplyOwnershipRule } = await load();

    const operation = await fixtureApplyOwnershipRule("rule_retail_tables");

    expect(operation).toMatchObject({
      operation_type: "ASSIGN_OWNERSHIP",
      subject_type: "TABLE",
      subject_ids: ["t_000000", "t_000001"],
      status: "REVIEW_REQUIRED",
      applied_count: 0,
      applied_subject_ids: [],
      requested_by: "dev-fixture-user",
      parameters: { owner_type: "GROUP", owner_principal: "retail-data-stewards", source_rule_id: "rule_retail_tables" },
    });
    expect(operation.governance_review_id).toBeTruthy();
  });

  it("matches a TAG rule on any one tag", async () => {
    const { fixtureApplyOwnershipRule } = await load();

    const operation = await fixtureApplyOwnershipRule("rule_pii_tagged");

    expect(operation.subject_ids).toEqual(["t_000000", "t_000001"]); // both carry "pii"
  });

  it("matches each of the fields the way the handler does", async () => {
    const { fixtureApplyOwnershipRule, fixtureCreateOwnershipRule } = await load();
    const make = async (match_field: "TABLE_NAME" | "QUALIFIED_NAME" | "DOMAIN_KEY", match_pattern: string, key: string) => {
      const rule = await fixtureCreateOwnershipRule("org", {
        rule_key: key, display_name: key, match_field, match_pattern, owner_type: "GROUP", owner_principal: "owners",
      });
      return (await fixtureApplyOwnershipRule(rule.id)).subject_ids;
    };

    expect(await make("TABLE_NAME", "raw_*", "by-name")).toEqual(["t_000005"]);
    expect(await make("QUALIFIED_NAME", "treasury.*", "by-qualified")).toEqual(["t_000002", "t_000003"]);
    expect(await make("DOMAIN_KEY", "*", "by-domain")).toEqual(["t_000000", "t_000001", "t_000002", "t_000003"]);
  });

  it("never matches a table with no domain on DOMAIN_KEY, whatever the pattern", async () => {
    const { fixtureApplyOwnershipRule, fixtureCreateOwnershipRule } = await load();
    const rule = await fixtureCreateOwnershipRule("org", {
      rule_key: "any-domain", display_name: "any", match_field: "DOMAIN_KEY", match_pattern: "*",
      owner_type: "GROUP", owner_principal: "owners",
    });

    const operation = await fixtureApplyOwnershipRule(rule.id);

    // risk.exposure_by_counterparty and staging.raw_payments have no domain.
    expect(operation.subject_ids).not.toContain("t_000004");
    expect(operation.subject_ids).not.toContain("t_000005");
  });

  it("refuses a rule that matches nothing, and a rule that does not exist, with the server's sentences", async () => {
    const { fixtureApplyOwnershipRule, fixtureCreateOwnershipRule } = await load();
    const rule = await fixtureCreateOwnershipRule("org", {
      rule_key: "nothing", display_name: "nothing", match_field: "TABLE_NAME", match_pattern: "no_such_table",
      owner_type: "GROUP", owner_principal: "owners",
    });

    await expect(fixtureApplyOwnershipRule(rule.id)).rejects.toMatchObject({ status: 409, detail: "ownership rule matched no active tables" });
    await expect(fixtureApplyOwnershipRule("rule-missing")).rejects.toMatchObject({ status: 404, detail: "active ownership rule not found" });
  });

  it("opens a second, separate review when a rule is applied twice, as the server does", async () => {
    const { fixtureApplyOwnershipRule } = await load();

    const first = await fixtureApplyOwnershipRule("rule_retail_tables");
    const second = await fixtureApplyOwnershipRule("rule_retail_tables");

    expect(second.id).not.toBe(first.id);
    expect(second.governance_review_id).not.toBe(first.governance_review_id);
  });
});

describe("a demo leaver request", () => {
  const body = (overrides = {}) => ({
    leaving_principal: "local-ui-admin", successor_principal: "morgan", owner_type: "INDIVIDUAL" as const,
    rationale: "Priya left the bank.", ...overrides,
  });

  it("takes the whole portfolio when no ids are named, and moves nothing", async () => {
    const { fixtureRequestLeaverReassignment } = await load();

    const operation = await fixtureRequestLeaverReassignment("org", body(), makeFixtureOwnershipAssignments);

    expect(operation).toMatchObject({
      operation_type: "REASSIGN_LEAVER",
      subject_type: "OWNERSHIP_ASSIGNMENT",
      subject_ids: ["own_orders_raw", "own_customer_dim"], // the leaver's two INDIVIDUAL rows, not the GROUP one
      status: "REVIEW_REQUIRED",
      applied_count: 0,
      parameters: {
        leaving_principal: "local-ui-admin", successor_principal: "morgan", owner_type: "INDIVIDUAL",
        selection_mode: "FILTER", selection_truncated: false,
      },
    });
  });

  it("takes only the named ids, in EXPLICIT mode", async () => {
    const { fixtureRequestLeaverReassignment } = await load();

    const operation = await fixtureRequestLeaverReassignment("org", body({ assignment_ids: ["own_orders_raw"] }), makeFixtureOwnershipAssignments);

    expect(operation.subject_ids).toEqual(["own_orders_raw"]);
    expect(operation.parameters).toMatchObject({ selection_mode: "EXPLICIT" });
  });

  it("refuses an id the leaver does not hold, and a leaver who holds nothing, verbatim", async () => {
    const { fixtureRequestLeaverReassignment } = await load();

    await expect(fixtureRequestLeaverReassignment("org", body({ assignment_ids: ["own_treasury_snapshot"] }), makeFixtureOwnershipAssignments)).rejects.toMatchObject({
      status: 409,
      detail: "one or more assignment_ids are not active ownership assignments currently held by leaving_principal",
    });
    await expect(fixtureRequestLeaverReassignment("org", body({ leaving_principal: "nobody" }), makeFixtureOwnershipAssignments)).rejects.toMatchObject({
      status: 409,
      detail: "leaving_principal has no active ownership assignments to reassign",
    });
    // The GROUP row is held as a GROUP, not as an INDIVIDUAL.
    await expect(
      fixtureRequestLeaverReassignment("org", body({ assignment_ids: ["own_treasury_snapshot"], owner_type: "GROUP" }), makeFixtureOwnershipAssignments),
    ).resolves.toMatchObject({ subject_ids: ["own_treasury_snapshot"] });
  });

  it("refuses a successor who is the leaver", async () => {
    const { fixtureRequestLeaverReassignment } = await load();

    await expect(fixtureRequestLeaverReassignment("org", body({ successor_principal: "local-ui-admin" }), makeFixtureOwnershipAssignments)).rejects.toMatchObject({
      status: 422,
    });
  });
});

describe("the demo operations list", () => {
  it("lists what was requested, newest first, and can be filtered by status", async () => {
    const { fixtureApplyOwnershipRule, fixtureRequestLeaverReassignment, fixtureOwnershipOperations } = await load();
    const first = await fixtureApplyOwnershipRule("rule_retail_tables");
    const second = await fixtureRequestLeaverReassignment(
      "org",
      { leaving_principal: "local-ui-admin", successor_principal: "morgan", rationale: "Priya left the bank." },
      makeFixtureOwnershipAssignments,
    );

    const all = await fixtureOwnershipOperations({});
    const applied = await fixtureOwnershipOperations({ status: "applied" });

    expect(all.items.map((operation) => operation.id)).toEqual([second.id, first.id]);
    expect(all).toMatchObject({ total: 2, limit: 100, offset: 0 });
    expect(applied.items).toEqual([]); // the demo never approves anything
    expect((await fixtureOwnershipOperations({ status: "review_required" })).items).toHaveLength(2);
    expect((await fixtureOwnershipOperations({ limit: 1, offset: 1 })).items.map((operation) => operation.id)).toEqual([first.id]);
  });

  it("is empty before anything has been requested", async () => {
    const { fixtureOwnershipOperations } = await load();

    expect((await fixtureOwnershipOperations({})).items).toEqual([]);
  });
});
