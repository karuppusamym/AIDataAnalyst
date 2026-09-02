// UX-4 (Bulk selection + background execution) proof.
//
// Exercises the pure chunking/progress logic and the selection-state cap,
// plus a scripted end-to-end run of `runBulkOperation` against a fake
// `api()` -- proving a 10,000-item selection is split at the server's real
// 500-item cap (CATALOG_BULK_ACTION_MAX_ITEMS), progresses chunk by chunk,
// and a mid-run cancellation stops dispatching further chunks without
// losing the count of what already completed. Mirrors the established
// ui/ convention (no test runner exists for this plain browser app): run
// directly via `node ui/scripts/features/bulk-execution.test.mjs`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "bulk-execution.js"), "utf8");

// Field-by-field comparison rather than assert.deepEqual: objects returned
// from inside the vm sandbox carry that context's own Object.prototype, a
// different realm from this test's, which trips deepStrictEqual's
// prototype check even when every field matches.
function assertFields(actual, expected, message) {
  Object.keys(expected).forEach(key => assert.equal(actual[key], expected[key], message ? `${message} (field: ${key})` : `field: ${key}`));
}

function freshSandbox(apiImpl) {
  const sandbox = { window: { AtlasUI: { api: apiImpl } }, console };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "bulk-execution.js" });
  return sandbox.window.AtlasUI;
}

const base = freshSandbox(async () => ({ succeeded_count: 0, failed_count: 0 }));
const { chunkIds, createProgress, createSelectionState, runBulkOperation, BULK_MAX_SELECTION, BULK_DEFAULT_CHUNK_SIZE } = base;

assert.equal(typeof chunkIds, "function");
assert.equal(typeof createSelectionState, "function");
assert.equal(typeof runBulkOperation, "function");
assert.equal(BULK_MAX_SELECTION, 10000, "the selection cap must match this row's stated 10,000-item target");
assert.equal(BULK_DEFAULT_CHUNK_SIZE, 500, "the chunk size must match CT-1's real CATALOG_BULK_ACTION_MAX_ITEMS cap");

// --- chunkIds: a 10,000-item selection splits into exactly 20 chunks of 500 ---
{
  const ids = Array.from({ length: 10000 }, (_, i) => `id-${i}`);
  const chunks = chunkIds(ids, 500);
  assert.equal(chunks.length, 20);
  chunks.forEach(chunk => assert.ok(chunk.length <= 500, "no chunk may exceed the server's per-request cap"));
  assert.equal(chunks.flat().length, 10000, "every id must be included exactly once across all chunks");
  assert.equal(new Set(chunks.flat()).size, 10000, "no id may be duplicated across chunks");
}

assert.deepEqual(Array.from(chunkIds([], 500)), [], "an empty selection produces no chunks");
{
  const chunks = chunkIds(["a", "b", "c"], 500);
  assert.equal(chunks.length, 1);
  assert.equal(chunks[0].length, 3);
}

// --- createProgress: initial shape ---
{
  const progress = createProgress(10000, 20);
  assert.equal(progress.total, 10000);
  assert.equal(progress.processed, 0);
  assert.equal(progress.chunksTotal, 20);
  assert.equal(progress.done, false);
  assert.equal(progress.cancelled, false);
}

// --- createSelectionState: caps at max, reports capped rather than throwing ---
{
  const selection = createSelectionState(3);
  assertFields(selection.add("a"), { added: true, capped: false });
  assertFields(selection.add("b"), { added: true, capped: false });
  assertFields(selection.add("a"), { added: false, capped: false }, "re-adding an already-selected id is a no-op, not a cap hit");
  assertFields(selection.add("c"), { added: true, capped: false });
  assertFields(selection.add("d"), { added: false, capped: true }, "adding past the cap must fail closed, not silently grow past 10,000");
  assert.equal(selection.size(), 3);
  assert.equal(selection.has("a"), true);
  selection.remove("a");
  assert.equal(selection.has("a"), false);
  assert.equal(selection.size(), 2);
}

// --- createSelectionState.toggle ---
{
  const selection = createSelectionState(10);
  selection.toggle("x");
  assert.equal(selection.has("x"), true);
  selection.toggle("x");
  assert.equal(selection.has("x"), false);
}

// --- createSelectionState.addMany: bulk "select all on page" respects the cap ---
{
  const selection = createSelectionState(5);
  const result = selection.addMany(["a", "b", "c", "d", "e", "f", "g"]);
  assert.equal(result.added, 5);
  assert.equal(result.capped, true);
  assert.equal(selection.size(), 5);
}

{
  const selection = createSelectionState(50);
  const result = selection.addMany(["a", "b", "c"]);
  assert.equal(result.added, 3);
  assert.equal(result.capped, false);
}

// --- runBulkOperation: a full run over 10,000 ids reports every chunk and totals correctly ---
{
  let calls = 0;
  const requestedBodies = [];
  const sandboxApi = async (endpoint, options) => {
    calls += 1;
    const body = JSON.parse(options.body);
    requestedBodies.push(body);
    return { succeeded_count: body.table_ids.length, failed_count: 0 };
  };
  const { runBulkOperation: run, chunkIds: chunk } = freshSandbox(sandboxApi);
  const ids = Array.from({ length: 10000 }, (_, i) => `t-${i}`);
  const progressUpdates = [];
  const final = await run({
    endpoint: "/v1/organizations/org-1/tables/bulk-tag",
    buildBody: (chunkIdsArg) => ({ table_ids: chunkIdsArg, tag_key: "reviewed", tag_value: "true" }),
    ids,
    onProgress: (p) => progressUpdates.push(p),
  });
  assert.equal(calls, 20, "must issue exactly one request per 500-item chunk for a 10,000-item selection");
  assert.equal(final.processed, 10000);
  assert.equal(final.succeeded, 10000);
  assert.equal(final.failed, 0);
  assert.equal(final.done, true);
  assert.equal(final.cancelled, false);
  assert.ok(progressUpdates.length >= 21, "must report progress at least once per chunk plus the initial state");
  assert.equal(progressUpdates.at(-1).done, true, "the final progress update must be marked done");
  requestedBodies.forEach(body => assert.ok(body.table_ids.length <= 500));
}

// --- runBulkOperation: cancellation between chunks stops dispatching further work ---
{
  let calls = 0;
  const controller = { aborted: false };
  const sandboxApi = async () => {
    calls += 1;
    if (calls === 3) controller.aborted = true; // cancel mid-run, after the 3rd chunk completes
    return { succeeded_count: 500, failed_count: 0 };
  };
  const { runBulkOperation: run } = freshSandbox(sandboxApi);
  const ids = Array.from({ length: 5000 }, (_, i) => `t-${i}`); // 10 chunks of 500
  const final = await run({
    endpoint: "/v1/organizations/org-1/tables/bulk-tag",
    buildBody: (chunk) => ({ table_ids: chunk, tag_key: "x" }),
    ids,
    signal: controller,
  });
  assert.equal(calls, 3, "must stop issuing new chunk requests once cancelled, not run to completion");
  assert.equal(final.cancelled, true);
  assert.equal(final.processed, 1500, "chunks already in flight/completed before cancellation must still be counted, never lost");
  assert.equal(final.done, true, "a cancelled run still reaches a terminal, reportable state");
}

// --- runBulkOperation: a failed chunk counts as failed, not silently dropped, and the run continues ---
{
  let calls = 0;
  const sandboxApi = async () => {
    calls += 1;
    if (calls === 2) throw new Error("database constraint violation");
    return { succeeded_count: 500, failed_count: 0 };
  };
  const { runBulkOperation: run } = freshSandbox(sandboxApi);
  const ids = Array.from({ length: 1500 }, (_, i) => `t-${i}`); // 3 chunks of 500
  const final = await run({
    endpoint: "/v1/organizations/org-1/tables/bulk-certify",
    buildBody: (chunk) => ({ table_ids: chunk, rationale: "quarterly review", expires_at: "2027-01-01T00:00:00Z" }),
    ids,
  });
  assert.equal(calls, 3, "a single failed chunk must not abort the remaining chunks");
  assert.equal(final.processed, 1500);
  assert.equal(final.succeeded, 1000, "the two successful chunks' 500 items each");
  assert.equal(final.failed, 500, "the failed chunk's 500 items are honestly counted as failed");
  assert.equal(final.errors.length, 1);
}

console.log("bulk-execution.test.mjs: all assertions passed");
