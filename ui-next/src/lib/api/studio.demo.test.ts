import { beforeEach, describe, expect, it } from "vitest";

import { ApiError } from "../http";
import { resetStudioDemoForTests } from "../studioAuthoringDemo";
import {
  addStudioChangeItem,
  createStudioChangeSet,
  detectStudioConflicts,
  fetchStudioChangeSetItems,
  fetchStudioChangeSets,
  fetchStudioDiff,
  fetchStudioEvalQuestions,
  fetchStudioEvalRun,
  fetchStudioImpact,
  mineStudioEvalQuestions,
  removeStudioChangeItem,
  runStudioTests,
  submitStudioChangeSet,
  validateStudioContextProduct,
  validateStudioParameterContract,
} from "./studio";

/* ---------------------------------------------------------------------------
   The Studio client's DEMO arm (R11-AUD08): a store, so create -> add -> test ->
   submit behaves as the server does, answering with the handlers' own status rules
   and refusal sentences (`studio_api.py`).

   Not a copy of the platform's validators -- see `studioAuthoringDemo.ts` -- so what
   is pinned here is the workflow and the sentences, and that nothing is claimed
   beyond the one rule the demo tests items by (a snapshot, unless the operation is
   DELETE).

   These run with the real `demoOr` (vitest builds carry demo data unless
   `VITE_USE_FIXTURES=0`), so they also prove the demo arm is reachable through it.
--------------------------------------------------------------------------- */

beforeEach(() => {
  resetStudioDemoForTests();
});

const refusal = async (promise: Promise<unknown>): Promise<ApiError> => {
  try {
    await promise;
  } catch (failure) {
    expect(failure).toBeInstanceOf(ApiError);
    return failure as ApiError;
  }
  throw new Error("expected a refusal");
};

const METRIC = { name: "net_revenue", aggregation: "SUM", grain: "month" };

/** A DRAFT with one item that will test clean. */
async function draftWithItem(after: Record<string, unknown> | null = METRIC) {
  const created = await createStudioChangeSet({ name: "Fix revenue grain" });
  const added = await addStudioChangeItem(created.id, {
    object_type: "METRIC", object_id: "metric:net_revenue", operation: "UPDATE",
    ...(after ? { after_snapshot: after } : {}),
  });
  return { created, added };
}

describe("the bundled change sets", () => {
  it("are what the read-only screen always showed, until somebody edits them", async () => {
    const all = await fetchStudioChangeSets({});

    expect(all.map((set) => [set.id, set.status])).toEqual([["cs_1001", "TESTING"], ["cs_1002", "DRAFT"]]);
    expect((await fetchStudioChangeSets({ status: "DRAFT" })).map((set) => set.id)).toEqual(["cs_1002"]);
    expect(await fetchStudioChangeSetItems("cs_1001")).toHaveLength(1);
    const impact = await fetchStudioImpact("cs_1001");
    expect(impact.affected_object_count).toBe(1);
    expect(impact.affected_objects[0]).toMatchObject({ object_id: "metric:revenue", changed_fields: ["filter"] });
    const diff = await fetchStudioDiff("cs_1001");
    expect(diff.items[0]).toMatchObject({ item_id: "item_1", operation: "UPDATE" });
  });

  it("answer 404 in the handler's words for a change set that does not exist", async () => {
    expect((await refusal(fetchStudioChangeSetItems("nope"))).detail).toBe("change set not found");
    expect((await refusal(runStudioTests("nope"))).status).toBe(404);
  });
});

describe("creating a change set", () => {
  it("makes a DRAFT, CLEAN one, first in the list, that the next read sees", async () => {
    const created = await createStudioChangeSet({ name: "Fix revenue grain" });

    expect(created).toMatchObject({ name: "Fix revenue grain", status: "DRAFT", conflict_status: "CLEAN", base_version_hash: "0".repeat(64) });
    expect((await fetchStudioChangeSets({})).map((set) => set.id)).toEqual([created.id, "cs_1001", "cs_1002"]);
    expect(await fetchStudioChangeSetItems(created.id)).toEqual([]);
  });

  it("refuses a name outside 2-200 characters with the sentence the API sends", async () => {
    const short = await refusal(createStudioChangeSet({ name: "x" }));
    expect(short.status).toBe(422);
    expect(short.detail).toBe("body.name: String should have at least 2 characters");
    expect((await refusal(createStudioChangeSet({ name: "x".repeat(201) }))).detail).toBe(
      "body.name: String should have at most 200 characters",
    );
    expect(await fetchStudioChangeSets({})).toHaveLength(2);
  });
});

describe("items", () => {
  it("adds an UNTESTED item and computes the diff only when both snapshots are given", async () => {
    const { created } = await draftWithItem();
    const both = await addStudioChangeItem(created.id, {
      object_type: "METRIC", object_id: "metric:margin", operation: "UPDATE",
      before_snapshot: { grain: "day", name: "margin" }, after_snapshot: { grain: "month", name: "margin" },
    });
    const one = await addStudioChangeItem(created.id, {
      object_type: "TERM", object_id: "term:churn", operation: "CREATE", after_snapshot: { display_name: "Churn" },
    });

    expect(both.test_status).toBe("UNTESTED");
    expect(both.diff).toEqual({ grain: { before: "day", after: "month" } });
    expect(one.diff).toBeNull();
    expect((await fetchStudioChangeSetItems(created.id)).map((item) => item.object_id)).toEqual([
      "metric:net_revenue", "metric:margin", "term:churn",
    ]);
    expect((await fetchStudioImpact(created.id)).affected_objects[1]).toMatchObject({ changed_fields: ["grain"] });
  });

  it("removes an item, and answers 404 for one that is not in the change set", async () => {
    const { created, added } = await draftWithItem();

    await removeStudioChangeItem(created.id, added.id);

    expect(await fetchStudioChangeSetItems(created.id)).toEqual([]);
    expect((await refusal(removeStudioChangeItem(created.id, added.id))).detail).toBe("change item not found");
  });

  it("refuses to add or remove once a change set is not a DRAFT, in the handlers' sentences", async () => {
    const { created, added } = await draftWithItem();
    await runStudioTests(created.id); // -> TESTING

    const add = await refusal(addStudioChangeItem(created.id, { object_type: "TERM", object_id: "t", operation: "DELETE" }));
    expect(add.status).toBe(409);
    expect(add.detail).toBe("items can only be added to DRAFT change sets");
    expect((await refusal(removeStudioChangeItem(created.id, added.id))).detail).toBe(
      "items can only be removed from DRAFT change sets",
    );
    expect(await fetchStudioChangeSetItems(created.id)).toHaveLength(1);
  });

  it("refuses an object id outside 1-100 characters", async () => {
    const { created } = await draftWithItem();

    expect((await refusal(addStudioChangeItem(created.id, { object_type: "TERM", object_id: "", operation: "DELETE" }))).status).toBe(422);
    expect((await refusal(addStudioChangeItem(created.id, { object_type: "TERM", object_id: "x".repeat(101), operation: "DELETE" }))).detail).toBe(
      "body.object_id: String should have at most 100 characters",
    );
  });
});

describe("running the tests", () => {
  it("moves a DRAFT to TESTING, stores each item's status, and answers with totals", async () => {
    const { created } = await draftWithItem();
    await addStudioChangeItem(created.id, { object_type: "METRIC", object_id: "metric:no_definition", operation: "CREATE" });
    await addStudioChangeItem(created.id, { object_type: "TERM", object_id: "term:gone", operation: "DELETE" });

    const result = await runStudioTests(created.id);

    expect(result.passed).toBe(false);
    expect(result.evidence).toEqual({
      total_items: 3, passed_items: 2, failed_items: 1, eval_regression_checked: 0, eval_regression_failed: 0,
    });
    expect((await fetchStudioChangeSets({ status: "TESTING" })).map((set) => set.id)).toContain(created.id);
    expect((await fetchStudioChangeSetItems(created.id)).map((item) => item.test_status)).toEqual(["PASSED", "FAILED", "PASSED"]);
  });

  it("can be run again on a TESTING change set, and refuses one that is past testing", async () => {
    const { created } = await draftWithItem();
    await runStudioTests(created.id);
    expect((await runStudioTests(created.id)).passed).toBe(true);

    await submitStudioChangeSet(created.id);

    const refused = await refusal(runStudioTests(created.id));
    expect(refused.status).toBe(409);
    expect(refused.detail).toBe("tests can only be run on DRAFT or TESTING change sets");
  });
});

describe("the eval run and mined questions", () => {
  it("has no run until the tests have run, and then answers with one", async () => {
    const { created } = await draftWithItem();
    const none = await refusal(fetchStudioEvalRun(created.id));
    expect(none.status).toBe(404);
    expect(none.detail).toBe("no eval run recorded for this change set");

    await runStudioTests(created.id);

    const run = await fetchStudioEvalRun(created.id);
    expect(run).toMatchObject({ change_set_id: created.id, passed: true, results: [] });
    expect(run.evidence).toEqual({ checked: 0, failed: 0, failed_question_ids: [] });
  });

  it("mines the demo estate once: a second pass creates nothing and reports what was already mined", async () => {
    expect(await fetchStudioEvalQuestions({})).toEqual([]);

    const first = await mineStudioEvalQuestions();
    const second = await mineStudioEvalQuestions();

    expect(first).toEqual({ consumption_edges_scanned: 12, bi_edges_scanned: 7, questions_created: 2, questions_already_mined: 0, truncated: false });
    expect(second).toMatchObject({ questions_created: 0, questions_already_mined: 2 });
    expect(await fetchStudioEvalQuestions({})).toHaveLength(2);
    expect((await fetchStudioEvalQuestions({ objectType: "TOOL" })).map((q) => q.object_id)).toEqual(["tool:margin_by_region"]);
    expect(await fetchStudioEvalQuestions({ limit: 1 })).toHaveLength(1);
    expect(await fetchStudioEvalQuestions({ limit: 1, offset: 1 })).toHaveLength(1);
  });

  it("re-checks a mined question for an object the change set touches: it fails the run and blocks submission", async () => {
    await mineStudioEvalQuestions();
    const created = await createStudioChangeSet({ name: "Break a mined metric" });
    // The demo estate's mined metric is `metric:revenue`; this item names it and carries no definition.
    await addStudioChangeItem(created.id, { object_type: "METRIC", object_id: "metric:revenue", operation: "UPDATE" });

    const result = await runStudioTests(created.id);

    expect(result.passed).toBe(false);
    expect(result.evidence).toMatchObject({ eval_regression_checked: 1, eval_regression_failed: 1 });
    const run = await fetchStudioEvalRun(created.id);
    expect(run.passed).toBe(false);
    expect(run.results[0]).toMatchObject({ object_id: "metric:revenue", passed: false, label: "metric:net_revenue" });
    expect(run.results[0]!.evidence.failures).toEqual(["definition missing: no after_snapshot provided"]);
    const blocked = await refusal(submitStudioChangeSet(created.id));
    expect(blocked.detail).toMatch(/^1 item\(s\) have not passed testing; 1 mined eval question\(s\) regressed: \['demo-question-\d+'\]$/);
  });
});

describe("submitting", () => {
  it("refuses an empty change set, and untested items, in the handler's sentences", async () => {
    const empty = await createStudioChangeSet({ name: "Nothing yet" });
    expect((await refusal(submitStudioChangeSet(empty.id))).detail).toBe("cannot submit an empty change set");

    const { created } = await draftWithItem();
    expect((await refusal(submitStudioChangeSet(created.id))).detail).toBe("1 item(s) have not passed testing");
  });

  it("submits a tested change set, once", async () => {
    const { created } = await draftWithItem();
    await runStudioTests(created.id);

    expect((await submitStudioChangeSet(created.id)).status).toBe("SUBMITTED");

    expect((await refusal(submitStudioChangeSet(created.id))).detail).toBe("only DRAFT or TESTING change sets can be submitted");
  });

  it("submits the bundled TESTING change set, whose item is already PASSED", async () => {
    expect((await submitStudioChangeSet("cs_1001")).status).toBe("SUBMITTED");
  });
});

describe("detecting conflicts (the port of `detect_conflicts`)", () => {
  const add = (setId: string, operation: "CREATE" | "UPDATE" | "DELETE", extra: Record<string, unknown> = {}) =>
    addStudioChangeItem(setId, { object_type: "METRIC", object_id: "metric:x", operation, ...extra });

  it("reads an empty state literally: an UPDATE is NOT_FOUND, a DELETE is ALREADY_DELETED, a CREATE is fine", async () => {
    const { created } = await draftWithItem();
    const del = await createStudioChangeSet({ name: "A delete" });
    await add(del.id, "DELETE");
    const create = await createStudioChangeSet({ name: "A create" });
    await add(create.id, "CREATE");

    expect(await detectStudioConflicts(created.id, null)).toEqual([
      { object_type: "METRIC", object_id: "metric:net_revenue", field_name: "<exists>", change_set_value: "UPDATE", current_value: "NOT_FOUND" },
    ]);
    expect(await detectStudioConflicts(del.id, {})).toEqual([
      { object_type: "METRIC", object_id: "metric:x", field_name: "<exists>", change_set_value: "DELETE", current_value: "ALREADY_DELETED" },
    ]);
    expect(await detectStudioConflicts(create.id, null)).toEqual([]);
  });

  it("finds a CREATE whose object already exists", async () => {
    const create = await createStudioChangeSet({ name: "A create" });
    await add(create.id, "CREATE");

    expect(await detectStudioConflicts(create.id, { "METRIC:metric:x": { name: "x" } })).toEqual([
      { object_type: "METRIC", object_id: "metric:x", field_name: "<exists>", change_set_value: "CREATE", current_value: "ALREADY_EXISTS" },
    ]);
  });

  it("finds a field the change set changes and the published state has since changed too", async () => {
    const update = await createStudioChangeSet({ name: "An update" });
    await add(update.id, "UPDATE", { before_snapshot: { grain: "day", name: "x" }, after_snapshot: { grain: "month", name: "x" } });

    expect(await detectStudioConflicts(update.id, { "METRIC:metric:x": { grain: "week", name: "x" } })).toEqual([
      { object_type: "METRIC", object_id: "metric:x", field_name: "grain", change_set_value: "month", current_value: "week" },
    ]);
    // Published state still what the change set started from: nothing to reconcile.
    expect(await detectStudioConflicts(update.id, { "METRIC:metric:x": { grain: "day", name: "x" } })).toEqual([]);
  });

  it("records CONFLICTED or CLEAN on the change set, as the handler does", async () => {
    const { created } = await draftWithItem();

    await detectStudioConflicts(created.id, null);
    expect((await fetchStudioChangeSets({})).find((set) => set.id === created.id)!.conflict_status).toBe("CONFLICTED");

    await detectStudioConflicts(created.id, { "METRIC:metric:net_revenue": {} });
    expect((await fetchStudioChangeSets({})).find((set) => set.id === created.id)!.conflict_status).toBe("CLEAN");
  });
});

describe("the two stateless checks", () => {
  it("checks a CREATE context product the way the first lines of the server's check do", async () => {
    const project = "3f0c1c9e-0000-4000-8000-000000000001";
    expect(await validateStudioContextProduct({ operation: "CREATE", object_id: "cp_1", snapshot: { product_key: "cp_1", project_id: project, name: "C" } })).toEqual({
      valid: true, errors: [], definition: { name: "C" }, product_key: "cp_1", project_id: project,
    });
    expect((await validateStudioContextProduct({ operation: "CREATE", object_id: "cp_1", snapshot: { product_key: "cp_2", project_id: "x" } })).errors).toEqual([
      "object_id ('cp_1') must equal after_snapshot.product_key ('cp_2')",
      "project_id is not a valid UUID: 'x'",
    ]);
    expect((await validateStudioContextProduct({ operation: "CREATE", object_id: "cp_1", snapshot: {} })).errors).toEqual([
      "CREATE requires a non-empty string product_key in after_snapshot",
      "CREATE requires a non-empty string project_id in after_snapshot",
    ]);
  });

  it("needs a UUID for an UPDATE or DELETE, and a snapshot for anything but a DELETE", async () => {
    expect((await validateStudioContextProduct({ operation: "DELETE", object_id: "cp_1" })).errors).toEqual([
      "object_id must be an existing context product UUID for DELETE: 'cp_1'",
    ]);
    expect((await validateStudioContextProduct({ operation: "DELETE", object_id: "3f0c1c9e-0000-4000-8000-000000000001" })).valid).toBe(true);
    expect((await validateStudioContextProduct({ operation: "UPDATE", object_id: "3f0c1c9e-0000-4000-8000-000000000001" })).errors).toEqual([
      "context product definition missing: no after_snapshot provided",
    ]);
    expect((await validateStudioContextProduct({ operation: "UPDATE", object_id: "cp_1", snapshot: { name: "x" } })).errors).toEqual([
      "object_id must be an existing context product UUID for UPDATE: 'cp_1'",
    ]);
  });

  it("checks a parameter contract against the template's placeholders", async () => {
    const ok = await validateStudioParameterContract({ sql_template: "SELECT * FROM t WHERE a = :a AND b::text = :b", parameters: [{ name: "a" }, { name: "b" }] });
    expect(ok).toMatchObject({ valid: true, errors: [] });

    const bad = await validateStudioParameterContract({ sql_template: "SELECT :a, :b", parameters: [{ name: "a" }, { name: "c" }] });
    expect(bad.valid).toBe(false);
    expect(bad.errors).toEqual(["undeclared placeholders: b", "unused parameter definitions: c"]);
  });
});
