import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../http";
import { resetGlossaryReviewFixtures } from "../glossaryReviewFixtures";
import {
  detectGlossaryConflicts,
  fetchGlossaryConflicts,
  fetchGlossaryLinkProposals,
  generateGlossaryLinkProposals,
  raiseGlossaryConflict,
  submitGlossaryConflictResolution,
  submitGlossaryLinkProposal,
} from "./glossaryReview";

/* ---------------------------------------------------------------------------
   The Glossary review API module (R11-AUD08).

   The screens' tests mock this module, so what a request actually carries -- the
   path, the query, the body, and that the two no-body writes send none -- is
   pinned here on the live arm. The demo arm is pinned too, against the real
   fixture store, because a demo that lets a steward do what the server refuses
   (resolve a conflict already in review, submit a proposal already submitted)
   teaches a control that does not exist.
--------------------------------------------------------------------------- */

const get = vi.fn();
const postJson = vi.fn();
let demoMode = false;

vi.mock("./transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./transport")>();
  return {
    ...actual,
    get: (...args: unknown[]) => get(...args),
    postJson: (...args: unknown[]) => postJson(...args),
    // `demoMode` chooses the arm per test: the live arm is the request a deployed
    // client sends, the demo arm is the fixture store answering with no request.
    demoOr: (demo: (fixtures: unknown) => Promise<unknown>, live: () => Promise<unknown>) =>
      demoMode ? Promise.resolve().then(() => demo({})) : live(),
  };
});

const ORG = "org-1";

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
  postJson.mockReset();
  postJson.mockResolvedValue({});
  demoMode = false;
  resetGlossaryReviewFixtures();
});

function requested(): URL {
  const [path] = get.mock.calls.at(-1)!;
  return new URL(String(path), "http://api.test");
}

describe("the list reads", () => {
  it.each([
    ["conflicts", fetchGlossaryConflicts, "glossary-conflicts"],
    ["link proposals", fetchGlossaryLinkProposals, "glossary-link-proposals"],
  ] as const)("GETs the %s route with the status, limit and offset it was given", async (_name, fetchIt, route) => {
    await fetchIt(ORG, { status: "OPEN", limit: 25, offset: 50 });

    const url = requested();
    expect(url.pathname).toBe(`/v1/organizations/${ORG}/${route}`);
    expect(Object.fromEntries(url.searchParams)).toEqual({ status: "OPEN", limit: "25", offset: "50" });
    expect(get.mock.calls.at(-1)![1]).toBeUndefined();
  });

  it.each([
    ["conflicts", fetchGlossaryConflicts],
    ["link proposals", fetchGlossaryLinkProposals],
  ] as const)("sends no status for %s when none, null or empty is given, and the server's own page defaults", async (_name, fetchIt) => {
    for (const status of [undefined, null, ""]) {
      await fetchIt(ORG, { status });
      expect(requested().searchParams.has("status")).toBe(false);
      expect(Object.fromEntries(requested().searchParams)).toEqual({ limit: "100", offset: "0" });
    }
    await fetchIt(ORG);
    expect(Object.fromEntries(requested().searchParams)).toEqual({ limit: "100", offset: "0" });
  });

  it("passes the abort signal through", async () => {
    const controller = new AbortController();

    await fetchGlossaryConflicts(ORG, {}, controller.signal);

    expect(get.mock.calls.at(-1)![1]).toBe(controller.signal);
  });
});

describe("the writes", () => {
  it("POSTs detect with no body to the organization's detect route", async () => {
    await detectGlossaryConflicts(ORG);

    expect(postJson).toHaveBeenCalledWith(`/v1/organizations/${ORG}/glossary-conflicts/detect`, undefined, undefined);
  });

  it("POSTs a raised conflict to the collection route with the body as given", async () => {
    const body = {
      conflict_type: "DEFINITION" as const,
      position_a: { display_name: "Net revenue" },
      position_b: { definition: "After rebates." },
    };

    await raiseGlossaryConflict(ORG, body);

    expect(postJson).toHaveBeenCalledWith(`/v1/organizations/${ORG}/glossary-conflicts`, body, undefined);
  });

  it("POSTs a resolution to the CONFLICT's own route, not the organization's, with the body as given", async () => {
    const body = { resolution: "MERGE" as const, resolved_definition: "A merged text.", rationale: "Because of a reason." };

    await submitGlossaryConflictResolution("conflict-9", body);

    expect(postJson).toHaveBeenCalledWith("/v1/glossary-conflicts/conflict-9/resolution", body, undefined);
  });

  it("POSTs generate with the bounds as given", async () => {
    await generateGlossaryLinkProposals(ORG, { minimum_confidence: 0.9, limit: 20 });

    expect(postJson).toHaveBeenCalledWith(
      `/v1/organizations/${ORG}/glossary-link-proposals/generate`,
      { minimum_confidence: 0.9, limit: 20 },
      undefined,
    );
  });

  it("POSTs a submit with no body to the PROPOSAL's own route", async () => {
    await submitGlossaryLinkProposal("proposal-3");

    expect(postJson).toHaveBeenCalledWith("/v1/glossary-link-proposals/proposal-3/submit", undefined, undefined);
  });

  it("lets a refusal through untouched, so a screen can show the server's own sentence", async () => {
    postJson.mockRejectedValue(new ApiError(409, "only open conflicts can be resolved"));

    await expect(
      submitGlossaryConflictResolution("conflict-9", { resolution: "RETAIN_BOTH", rationale: "A reason, long enough." }),
    ).rejects.toMatchObject({ status: 409, detail: "only open conflicts can be resolved" });
  });
});

describe("the demo arm answers from the fixture store, and sends no request", () => {
  beforeEach(() => {
    demoMode = true;
  });
  const noRequest = () => {
    expect(get).not.toHaveBeenCalled();
    expect(postJson).not.toHaveBeenCalled();
  };

  it("lists conflicts newest first, filters by status before paging, and counts the matches", async () => {
    const all = await fetchGlossaryConflicts(ORG, {});
    expect(all.total).toBe(5);
    expect(all.items.map((row) => row.created_at)).toEqual([...all.items.map((row) => row.created_at)].sort().reverse());

    const open = await fetchGlossaryConflicts(ORG, { status: "OPEN" });
    expect(open.items.every((row) => row.status === "OPEN")).toBe(true);
    expect(open.total).toBe(3);
    // The server upper-cases the filter, and so does the demo.
    expect((await fetchGlossaryConflicts(ORG, { status: "open" })).total).toBe(3);

    const secondPage = await fetchGlossaryConflicts(ORG, { limit: 2, offset: 2 });
    expect(secondPage.items).toHaveLength(2);
    expect(secondPage.total).toBe(5);
    expect(secondPage.offset).toBe(2);
    expect((await fetchGlossaryConflicts(ORG, { limit: 2, offset: 4 })).items).toHaveLength(1);
    noRequest();
  });

  it("includes a metric-formula collision among the conflicts, as the real route does", async () => {
    const all = await fetchGlossaryConflicts(ORG, {});

    expect(all.items.some((row) => row.conflict_type === "METRIC_FORMULA_COLLISION" && row.term_id === null)).toBe(true);
  });

  it("hands out copies, so a screen holding a row never sees a later write land on it", async () => {
    const before = await fetchGlossaryConflicts(ORG, { status: "OPEN" });
    const target = before.items[0]!;

    await submitGlossaryConflictResolution(target.id, { resolution: "RETAIN_BOTH", rationale: "A reason, long enough." });

    expect(target.status).toBe("OPEN");
    expect((await fetchGlossaryConflicts(ORG, { status: "OPEN" })).total).toBe(2);
  });

  it("resolves an OPEN conflict: it moves to REVIEW_REQUIRED, records the proposal, and opens a PENDING review", async () => {
    const open = (await fetchGlossaryConflicts(ORG, { status: "OPEN" })).items[0]!;

    const review = await submitGlossaryConflictResolution(open.id, {
      resolution: "MERGE",
      resolved_definition: "A merged text.",
      rationale: "A reason, long enough.",
    });

    expect(review).toMatchObject({ object_type: "GLOSSARY_CONFLICT", object_id: open.id, requested_action: "RESOLVE", status: "PENDING" });
    const after = (await fetchGlossaryConflicts(ORG, { status: "REVIEW_REQUIRED" })).items.find((row) => row.id === open.id)!;
    expect(after).toMatchObject({
      status: "REVIEW_REQUIRED",
      proposed_resolution: "MERGE",
      proposed_definition: "A merged text.",
      resolution_rationale: "A reason, long enough.",
      resolved_by: null,
    });
    noRequest();
  });

  it("refuses to resolve a conflict that is not OPEN, or does not exist, in the server's words", async () => {
    const inReview = (await fetchGlossaryConflicts(ORG, { status: "REVIEW_REQUIRED" })).items[0]!;
    const resolved = (await fetchGlossaryConflicts(ORG, { status: "RESOLVED" })).items[0]!;
    const body = { resolution: "RETAIN_BOTH" as const, rationale: "A reason, long enough." };

    await expect(submitGlossaryConflictResolution(inReview.id, body)).rejects.toMatchObject({ status: 409, detail: "only open conflicts can be resolved" });
    await expect(submitGlossaryConflictResolution(resolved.id, body)).rejects.toMatchObject({ status: 409, detail: "only open conflicts can be resolved" });
    await expect(submitGlossaryConflictResolution("nope", body)).rejects.toMatchObject({ status: 404, detail: "glossary conflict not found" });
  });

  it("resolves a conflict once: a second proposal on it is refused", async () => {
    const open = (await fetchGlossaryConflicts(ORG, { status: "OPEN" })).items[0]!;
    const body = { resolution: "RETAIN_BOTH" as const, rationale: "A reason, long enough." };
    await submitGlossaryConflictResolution(open.id, body);

    await expect(submitGlossaryConflictResolution(open.id, body)).rejects.toBeInstanceOf(ApiError);
  });

  it("detects only the pairs that are still to be raised, and answers what THIS run created", async () => {
    const first = await detectGlossaryConflicts(ORG);
    expect(first.items).toHaveLength(1);
    expect(first.total).toBe(1);
    expect(first.limit).toBe(100);
    expect(first.items[0]).toMatchObject({ conflict_type: "SYNONYM_COLLISION", status: "OPEN" });
    expect((await fetchGlossaryConflicts(ORG, {})).total).toBe(6);

    // Nothing left to find: a second run raises nothing, as the real one skips pairs already open.
    const second = await detectGlossaryConflicts(ORG);
    expect(second.items).toEqual([]);
    expect((await fetchGlossaryConflicts(ORG, {})).total).toBe(6);
    noRequest();
  });

  it("raises a conflict OPEN, with the positions and owner it was given", async () => {
    const raised = await raiseGlossaryConflict(ORG, {
      conflict_type: "SOURCE_DISAGREEMENT",
      position_a: { display_name: "X" },
      position_b: { definition: "Y" },
      assigned_owner: "owner@example",
    });

    expect(raised).toMatchObject({
      status: "OPEN", conflict_type: "SOURCE_DISAGREEMENT", term_id: null, assigned_owner: "owner@example",
      position_a: { display_name: "X" }, position_b: { definition: "Y" }, proposed_resolution: null,
    });
    expect((await fetchGlossaryConflicts(ORG, { status: "OPEN" })).items[0]!.id).toBe(raised.id);
  });

  it("lists link proposals with their evidence and confidence, and filters like the route", async () => {
    const all = await fetchGlossaryLinkProposals(ORG, {});
    expect(all.total).toBe(5);
    expect(new Set(all.items.map((row) => row.status))).toEqual(new Set(["DRAFT", "REVIEW_REQUIRED", "APPROVED", "REJECTED"]));
    expect(all.items.every((row) => typeof row.confidence === "number" && row.evidence.strategy === "APPROVED_LABEL_EXACT_MATCH")).toBe(true);
    expect((await fetchGlossaryLinkProposals(ORG, { status: "DRAFT" })).total).toBe(2);
    expect((await fetchGlossaryLinkProposals(ORG, { status: "draft", limit: 1, offset: 1 })).items).toHaveLength(1);
    noRequest();
  });

  it("generates DRAFT proposals from the candidates at or above the minimum confidence, up to the limit", async () => {
    // Two candidates: one at 1.0, one at 0.92. A minimum above 0.92 keeps only the first.
    const strict = await generateGlossaryLinkProposals(ORG, { minimum_confidence: 0.95, limit: 200 });
    expect(strict.items).toHaveLength(1);
    expect(strict.items[0]).toMatchObject({ status: "DRAFT", confidence: 1, governance_review_id: null, reviewed_by: null });

    // The one below the bar is left for a later run, and is found then.
    const rest = await generateGlossaryLinkProposals(ORG, { minimum_confidence: 0.75, limit: 200 });
    expect(rest.items).toHaveLength(1);
    expect(rest.items[0]!.confidence).toBe(0.92);
    expect((await generateGlossaryLinkProposals(ORG, { minimum_confidence: 0.5, limit: 200 })).items).toEqual([]);
    expect((await fetchGlossaryLinkProposals(ORG, { status: "DRAFT" })).total).toBe(4);
    noRequest();
  });

  it("stops a generate run at its limit", async () => {
    const run = await generateGlossaryLinkProposals(ORG, { minimum_confidence: 0.5, limit: 1 });

    expect(run.items).toHaveLength(1);
    expect(run.limit).toBe(1);
    expect(run.total).toBe(1);
  });

  it("submits a DRAFT proposal: it moves to REVIEW_REQUIRED and carries the review it opened", async () => {
    const draft = (await fetchGlossaryLinkProposals(ORG, { status: "DRAFT" })).items[0]!;

    const review = await submitGlossaryLinkProposal(draft.id);

    expect(review).toMatchObject({ object_type: "GLOSSARY_LINK_PROPOSAL", object_id: draft.id, requested_action: "APPROVE_LINK", status: "PENDING" });
    const after = (await fetchGlossaryLinkProposals(ORG, { status: "REVIEW_REQUIRED" })).items.find((row) => row.id === draft.id)!;
    expect(after.governance_review_id).toBe(review.id);
    noRequest();
  });

  it("refuses to submit a proposal that is not a DRAFT, or does not exist, in the server's words", async () => {
    const inReview = (await fetchGlossaryLinkProposals(ORG, { status: "REVIEW_REQUIRED" })).items[0]!;
    const approved = (await fetchGlossaryLinkProposals(ORG, { status: "APPROVED" })).items[0]!;

    await expect(submitGlossaryLinkProposal(inReview.id)).rejects.toMatchObject({ status: 409, detail: "only draft link proposals can be submitted" });
    await expect(submitGlossaryLinkProposal(approved.id)).rejects.toMatchObject({ status: 409, detail: "only draft link proposals can be submitted" });
    await expect(submitGlossaryLinkProposal("nope")).rejects.toMatchObject({ status: 404, detail: "glossary link proposal not found" });
  });

  it("never decides a review: a submitted proposal stays in review for the session", async () => {
    const draft = (await fetchGlossaryLinkProposals(ORG, { status: "DRAFT" })).items[0]!;
    await submitGlossaryLinkProposal(draft.id);

    expect((await fetchGlossaryLinkProposals(ORG, { status: "APPROVED" })).total).toBe(1);
    expect((await fetchGlossaryLinkProposals(ORG, { status: "REVIEW_REQUIRED" })).total).toBe(2);
  });
});
