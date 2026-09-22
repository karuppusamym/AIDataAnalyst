import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The Studio client's LIVE arm (R11-AUD08): what each function actually sends.

   The screen tests mock these functions, so nothing else pins the request a
   deployed client makes. Each case is one route from `studio_api.py`, and the
   details that would silently break it: the detect-conflicts body IS the state map
   (the handler's one body parameter is `current_state`, not an object wrapping it),
   the item DELETE has no body and answers 204, the list filters are query
   parameters with the API's own names (`status`, `object_type`), and an id that
   reaches a path is encoded.

   `demoOr` is pinned to its live arm, as `catalog.unownedBacklog.test.ts` does;
   the demo arm is `studio.demo.test.ts`.
--------------------------------------------------------------------------- */

const get = vi.fn();
const postJson = vi.fn();
const deleteRequest = vi.fn();

vi.mock("./transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./transport")>();
  return {
    ...actual,
    get: (...args: unknown[]) => get(...args),
    postJson: (...args: unknown[]) => postJson(...args),
    deleteRequest: (...args: unknown[]) => deleteRequest(...args),
    demoOr: (_demo: unknown, live: () => Promise<unknown>) => live(),
  };
});

async function load() {
  return import("./studio");
}

const ID = "3f0c1c9e-0000-4000-8000-000000000001";
const ITEM_ID = "3f0c1c9e-0000-4000-8000-000000000002";
const signal = new AbortController().signal;

beforeEach(() => {
  get.mockReset();
  postJson.mockReset();
  deleteRequest.mockReset();
  get.mockResolvedValue([]);
  postJson.mockResolvedValue({});
  deleteRequest.mockResolvedValue(undefined);
  vi.resetModules();
});

describe("the reads that were already there", () => {
  it("lists change sets with the status filter, limit and offset as query parameters", async () => {
    const { fetchStudioChangeSets } = await load();

    await fetchStudioChangeSets({ status: "TESTING", limit: 200, offset: 20 }, signal);

    expect(get).toHaveBeenCalledWith("/v1/studio/change-sets?status=TESTING&limit=200&offset=20", signal);
  });

  it("omits the status filter when there is none, and defaults the page", async () => {
    const { fetchStudioChangeSets } = await load();

    await fetchStudioChangeSets({ status: null });

    expect(get).toHaveBeenCalledWith("/v1/studio/change-sets?limit=100&offset=0", undefined);
  });

  it("submits with an empty body", async () => {
    const { submitStudioChangeSet } = await load();

    await submitStudioChangeSet(ID, signal);

    expect(postJson).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/submit`, {}, signal);
  });
});

describe("creating and editing", () => {
  it("creates a change set with just its name", async () => {
    const { createStudioChangeSet } = await load();

    await createStudioChangeSet({ name: "Fix revenue grain" }, signal);

    expect(postJson).toHaveBeenCalledWith("/v1/studio/change-sets", { name: "Fix revenue grain" }, signal);
  });

  it("adds an item to a change set, body as given", async () => {
    const { addStudioChangeItem } = await load();
    const body = {
      object_type: "METRIC" as const, object_id: "metric:revenue", operation: "UPDATE" as const,
      before_snapshot: { grain: "day" }, after_snapshot: { grain: "month" },
    };

    await addStudioChangeItem(ID, body, signal);

    expect(postJson).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/items`, body, signal);
  });

  it("removes an item with a DELETE that carries no body", async () => {
    const { removeStudioChangeItem } = await load();

    await expect(removeStudioChangeItem(ID, ITEM_ID, signal)).resolves.toBeUndefined();

    expect(deleteRequest).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/items/${ITEM_ID}`, signal);
    expect(postJson).not.toHaveBeenCalled();
  });

  it("encodes an id that reaches a path", async () => {
    const { addStudioChangeItem, removeStudioChangeItem, runStudioTests, fetchStudioEvalRun } = await load();

    await addStudioChangeItem("a/b?c", { object_type: "TERM", object_id: "x", operation: "CREATE" });
    await removeStudioChangeItem("a/b?c", "d e");
    await runStudioTests("a/b?c");
    await fetchStudioEvalRun("a/b?c");

    expect(postJson.mock.calls[0]![0]).toBe("/v1/studio/change-sets/a%2Fb%3Fc/items");
    expect(deleteRequest.mock.calls[0]![0]).toBe("/v1/studio/change-sets/a%2Fb%3Fc/items/d%20e");
    expect(postJson.mock.calls[1]![0]).toBe("/v1/studio/change-sets/a%2Fb%3Fc/test");
    expect(get.mock.calls[0]![0]).toBe("/v1/studio/change-sets/a%2Fb%3Fc/eval");
  });
});

describe("testing and conflicts", () => {
  it("runs the tests with a POST, and returns what the API answered", async () => {
    postJson.mockResolvedValue({ change_set_id: ID, passed: true, evidence: { total_items: 1 } });
    const { runStudioTests } = await load();

    const result = await runStudioTests(ID, signal);

    expect(postJson).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/test`, {}, signal);
    expect(result).toEqual({ change_set_id: ID, passed: true, evidence: { total_items: 1 } });
  });

  it("sends the published state AS the body of detect-conflicts, not wrapped in an object", async () => {
    const { detectStudioConflicts } = await load();
    const state = { "METRIC:metric:revenue": { grain: "day" } };

    await detectStudioConflicts(ID, state, signal);

    expect(postJson).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/detect-conflicts`, state, signal);
    expect(postJson.mock.calls[0]![1]).not.toHaveProperty("current_state");
  });

  it("sends an empty object, not null and not nothing, when no state is supplied", async () => {
    const { detectStudioConflicts } = await load();

    await detectStudioConflicts(ID, null);

    expect(postJson).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/detect-conflicts`, {}, undefined);
  });

  it("reads a change set's latest eval run", async () => {
    const { fetchStudioEvalRun } = await load();

    await fetchStudioEvalRun(ID, signal);

    expect(get).toHaveBeenCalledWith(`/v1/studio/change-sets/${ID}/eval`, signal);
  });
});

describe("eval questions", () => {
  it("mines with a POST and no parameters: it is organization-wide", async () => {
    const { mineStudioEvalQuestions } = await load();

    await mineStudioEvalQuestions(signal);

    expect(postJson).toHaveBeenCalledWith("/v1/studio/eval/mine", {}, signal);
  });

  it("lists the corpus with the object type as `object_type`", async () => {
    const { fetchStudioEvalQuestions } = await load();

    await fetchStudioEvalQuestions({ objectType: "TOOL", limit: 200 }, signal);

    expect(get).toHaveBeenCalledWith("/v1/studio/eval/questions?object_type=TOOL&limit=200&offset=0", signal);
  });

  it("omits the object type for every type, and defaults the page", async () => {
    const { fetchStudioEvalQuestions } = await load();

    await fetchStudioEvalQuestions({ objectType: null });

    expect(get).toHaveBeenCalledWith("/v1/studio/eval/questions?limit=100&offset=0", undefined);
  });
});

describe("the two stateless checks", () => {
  it("checks a context product definition with its operation, id and snapshot", async () => {
    const { validateStudioContextProduct } = await load();
    const body = { operation: "CREATE" as const, object_id: "cp_1", snapshot: { product_key: "cp_1" } };

    await validateStudioContextProduct(body, signal);

    expect(postJson).toHaveBeenCalledWith("/v1/studio/context-products/validate", body, signal);
  });

  it("checks a parameter contract with its SQL template, dialect and parameters", async () => {
    const { validateStudioParameterContract } = await load();
    const body = { sql_template: "SELECT :a", dialect: "postgres", parameters: [{ name: "a" }] };

    await validateStudioParameterContract(body, signal);

    expect(postJson).toHaveBeenCalledWith("/v1/studio/parameter-contracts/validate", body, signal);
  });
});
