/* ---------------------------------------------------------------------------
   Demo data for Studio authoring (fixture mode only; R11-AUD08).

   Its own module rather than a block in `fixtures.ts`, for the reason
   `documentFixtures.ts` gives: it keeps a small STORE, so create -> add item ->
   test -> submit behaves the way the server does instead of each answer being
   an independent canned sentence, and nothing else reads it. `api/studio.ts`
   reaches it through `demoOr`, by a dynamic import behind a loader that tests the
   build's demo literal (`noDemoData` in `api/transport.ts`), so a production
   build does not contain it.

   The store is SEEDED from the two bundled change sets the read-only screen
   always showed, so the demo estate looks the same until somebody edits it;
   from then on every Studio read in demo mode -- the list, a set's items, its
   diff and impact, its eval run -- answers from the store, so an edit is
   visible on the next read exactly as it is against the API.

   WHAT THIS IS NOT: the server's validators. `studio_test_harness.py` checks a
   metric's `aggregation`, a term's definition length, a tool's SQL template
   against its declared parameters and a context product against
   `ContextProductDefinition`; none of that is re-implemented here. A demo item
   PASSES its test when it carries the snapshot the harness asks for first
   (a DELETE needs none), and FAILS otherwise -- the one rule every one of the
   server's four validators has in common. The two "Check" endpoints answer with
   the same shape and the same first-line refusals, never with a claim that a
   definition is valid beyond that. The demo badge ("Demo data") is the
   disclosure; the point of this module is a screen that can be walked through,
   not a second copy of the platform's rules.

   What IS ported, because it is a dozen lines and the screen's behaviour
   depends on it: the handlers' status rules and their exact refusal sentences
   (`studio_api.py`), `compute_diff`, and `detect_conflicts` (`studio.py`).
--------------------------------------------------------------------------- */

import { ApiError } from "./http";
import type { DemoData } from "./api/transport";
import type {
  StudioChangeItemCreate,
  StudioChangeItemRead,
  StudioChangeSetCreate,
  StudioChangeSetRead,
  StudioConflict,
  StudioContextProductValidateRequest,
  StudioContextProductValidateResult,
  StudioDiffRead,
  StudioEvalMiningResult,
  StudioEvalQuestionRead,
  StudioEvalResultRead,
  StudioEvalRunRead,
  StudioImpactPreview,
  StudioParameterContractValidateRequest,
  StudioParameterContractValidateResult,
  StudioTestResultRead,
} from "./types";

const DEMO_ORG = "00000000-0000-0000-0000-000000000001";
const DEMO_AUTHOR = "demo.steward";
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface DemoState {
  sets: StudioChangeSetRead[];
  items: Record<string, StudioChangeItemRead[]>;
  evalRuns: Record<string, StudioEvalRunRead>;
  questions: StudioEvalQuestionRead[];
  counter: number;
}

let seeding: Promise<DemoState> | null = null;

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const now = (): string => new Date().toISOString();

/** Seeded once, from the bundled change sets; the promise is cached so two first
 *  calls in the same tick cannot each seed a store and overwrite the other's edits. */
function store(fixtures: DemoData): Promise<DemoState> {
  seeding ??= (async () => {
    const sets = clone(await fixtures.makeFixtureStudioChangeSets({}));
    const items: Record<string, StudioChangeItemRead[]> = {};
    for (const set of sets) items[set.id] = clone(await fixtures.makeFixtureStudioChangeSetItems(set.id));
    return { sets, items, evalRuns: {}, questions: [], counter: 0 };
  })();
  return seeding;
}

/** Tests start from a clean store. */
export function resetStudioDemoForTests(): void {
  seeding = null;
}

function setOf(state: DemoState, changeSetId: string): StudioChangeSetRead {
  const found = state.sets.find((set) => set.id === changeSetId);
  if (!found) throw new ApiError(404, "change set not found");
  return found;
}

const itemsOf = (state: DemoState, changeSetId: string): StudioChangeItemRead[] =>
  (state.items[changeSetId] ??= []);

/* ---- the reads the screen already had ------------------------------------ */

export async function demoListChangeSets(
  fixtures: DemoData,
  query: { status?: string | null },
): Promise<StudioChangeSetRead[]> {
  const state = await store(fixtures);
  return clone(query.status ? state.sets.filter((set) => set.status === query.status) : state.sets);
}

export async function demoChangeSetItems(fixtures: DemoData, changeSetId: string): Promise<StudioChangeItemRead[]> {
  const state = await store(fixtures);
  setOf(state, changeSetId);
  return clone(itemsOf(state, changeSetId));
}

export async function demoDiff(fixtures: DemoData, changeSetId: string): Promise<StudioDiffRead> {
  const items = await demoChangeSetItems(fixtures, changeSetId);
  return {
    change_set_id: changeSetId,
    items: items.map((item) => ({
      item_id: item.id,
      object_type: item.object_type,
      object_id: item.object_id,
      operation: item.operation,
      diff: item.diff,
    })),
  };
}

/** `compute_impact` (`studio.py`): one entry per item, with the changed fields when a diff exists. */
export async function demoImpact(fixtures: DemoData, changeSetId: string): Promise<StudioImpactPreview> {
  const items = await demoChangeSetItems(fixtures, changeSetId);
  const affected = items.map((item) => ({
    object_type: item.object_type,
    object_id: item.object_id,
    operation: item.operation,
    ...(item.diff ? { changed_fields: Object.keys(item.diff) } : {}),
  }));
  return { change_set_id: changeSetId, affected_object_count: affected.length, affected_objects: affected };
}

/** `submit_change_set`'s gate, with its sentences. */
export async function demoSubmit(fixtures: DemoData, changeSetId: string): Promise<StudioChangeSetRead> {
  const state = await store(fixtures);
  const set = setOf(state, changeSetId);
  if (set.status !== "DRAFT" && set.status !== "TESTING") {
    throw new ApiError(409, "only DRAFT or TESTING change sets can be submitted");
  }
  const items = itemsOf(state, changeSetId);
  if (items.length === 0) throw new ApiError(409, "cannot submit an empty change set");
  const reasons: string[] = [];
  const untested = items.filter((item) => item.test_status !== "PASSED");
  if (untested.length > 0) reasons.push(`${untested.length} item(s) have not passed testing`);
  const latest = state.evalRuns[changeSetId];
  if (latest && !latest.passed) {
    const failed = (latest.evidence.failed_question_ids as string[] | undefined) ?? [];
    reasons.push(`${failed.length} mined eval question(s) regressed: [${failed.map((id) => `'${id}'`).join(", ")}]`);
  }
  if (reasons.length > 0) throw new ApiError(409, reasons.join("; "));
  set.status = "SUBMITTED";
  set.updated_at = now();
  return clone(set);
}

/* ---- authoring ------------------------------------------------------------ */

export async function demoCreateChangeSet(
  fixtures: DemoData,
  body: StudioChangeSetCreate,
): Promise<StudioChangeSetRead> {
  const state = await store(fixtures);
  // The wording is the one FastAPI/pydantic sends for `Field(min_length=2, max_length=200)`,
  // which `http.ts` flattens to "<loc>: <msg>".
  if (body.name.length < 2) throw new ApiError(422, "body.name: String should have at least 2 characters");
  if (body.name.length > 200) throw new ApiError(422, "body.name: String should have at most 200 characters");
  state.counter += 1;
  const stamp = now();
  const created: StudioChangeSetRead = {
    id: `demo-cs-${state.counter}`,
    organization_id: DEMO_ORG,
    name: body.name,
    author: DEMO_AUTHOR,
    status: "DRAFT",
    base_version_hash: "0".repeat(64),
    conflict_status: "CLEAN",
    created_at: stamp,
    updated_at: stamp,
  };
  state.sets.unshift(created);
  state.items[created.id] = [];
  return clone(created);
}

const isFilled = (snapshot: Record<string, unknown> | null | undefined): snapshot is Record<string, unknown> =>
  snapshot != null && Object.keys(snapshot).length > 0;

/** `compute_diff` (`studio.py`): every field whose value differs, before and after. */
function computeDiff(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
): Record<string, unknown> {
  const diff: Record<string, unknown> = {};
  for (const key of [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
      diff[key] = { before: before[key] ?? null, after: after[key] ?? null };
    }
  }
  return diff;
}

export async function demoAddItem(
  fixtures: DemoData,
  changeSetId: string,
  body: StudioChangeItemCreate,
): Promise<StudioChangeItemRead> {
  const state = await store(fixtures);
  const set = setOf(state, changeSetId);
  if (set.status !== "DRAFT") throw new ApiError(409, "items can only be added to DRAFT change sets");
  if (body.object_id.length < 1) throw new ApiError(422, "body.object_id: String should have at least 1 character");
  if (body.object_id.length > 100) throw new ApiError(422, "body.object_id: String should have at most 100 characters");
  state.counter += 1;
  const stamp = now();
  const item: StudioChangeItemRead = {
    id: `demo-item-${state.counter}`,
    organization_id: DEMO_ORG,
    change_set_id: changeSetId,
    object_type: body.object_type,
    object_id: body.object_id,
    operation: body.operation,
    before_snapshot: body.before_snapshot ?? null,
    after_snapshot: body.after_snapshot ?? null,
    diff: isFilled(body.before_snapshot) && isFilled(body.after_snapshot)
      ? computeDiff(body.before_snapshot, body.after_snapshot)
      : null,
    test_status: "UNTESTED",
    created_at: stamp,
    updated_at: stamp,
  };
  itemsOf(state, changeSetId).push(item);
  set.updated_at = stamp;
  return clone(item);
}

export async function demoRemoveItem(fixtures: DemoData, changeSetId: string, itemId: string): Promise<void> {
  const state = await store(fixtures);
  const set = setOf(state, changeSetId);
  if (set.status !== "DRAFT") throw new ApiError(409, "items can only be removed from DRAFT change sets");
  const items = itemsOf(state, changeSetId);
  const index = items.findIndex((item) => item.id === itemId);
  if (index < 0) throw new ApiError(404, "change item not found");
  items.splice(index, 1);
  set.updated_at = now();
}

/** The one rule all four of the server's item validators share, as the demo's whole test. */
const demoItemPasses = (item: StudioChangeItemRead): boolean =>
  item.operation === "DELETE" || item.after_snapshot !== null;

export async function demoRunTests(fixtures: DemoData, changeSetId: string): Promise<StudioTestResultRead> {
  const state = await store(fixtures);
  const set = setOf(state, changeSetId);
  if (set.status !== "DRAFT" && set.status !== "TESTING") {
    throw new ApiError(409, "tests can only be run on DRAFT or TESTING change sets");
  }
  const started = now();
  set.status = "TESTING";
  const items = itemsOf(state, changeSetId);
  for (const item of items) item.test_status = demoItemPasses(item) ? "PASSED" : "FAILED";

  // The regression gate: a mined question for an object this change set touches is re-checked
  // with the same rule its item's own test uses.
  const results: StudioEvalResultRead[] = [];
  for (const question of state.questions) {
    const item = items.find((i) => i.object_type === question.object_type && i.object_id === question.object_id);
    if (!item) continue;
    const passed = demoItemPasses(item);
    results.push({
      eval_question_id: question.id,
      object_type: question.object_type,
      object_id: question.object_id,
      label: question.label,
      passed,
      evidence: {
        object_type: question.object_type,
        object_id: question.object_id,
        label: question.label,
        failures: passed ? [] : ["definition missing: no after_snapshot provided"],
      },
    });
  }
  const failed = results.filter((result) => !result.passed);
  const evalPassed = failed.length === 0;
  const completed = now();
  state.counter += 1;
  state.evalRuns[changeSetId] = {
    id: `demo-eval-${state.counter}`,
    change_set_id: changeSetId,
    started_at: started,
    completed_at: completed,
    passed: evalPassed,
    evidence: {
      checked: results.length,
      failed: failed.length,
      failed_question_ids: failed.map((result) => result.eval_question_id),
    },
    results,
  };
  set.updated_at = completed;

  const passedItems = items.filter((item) => item.test_status === "PASSED").length;
  return {
    change_set_id: changeSetId,
    started_at: started,
    completed_at: completed,
    passed: passedItems === items.length && evalPassed,
    evidence: {
      total_items: items.length,
      passed_items: passedItems,
      failed_items: items.length - passedItems,
      eval_regression_checked: results.length,
      eval_regression_failed: failed.length,
    },
  };
}

/** `detect_conflicts` (`studio.py`), rule for rule. */
export async function demoDetectConflicts(
  fixtures: DemoData,
  changeSetId: string,
  currentState: Record<string, Record<string, unknown>> | null,
): Promise<StudioConflict[]> {
  const state = await store(fixtures);
  const set = setOf(state, changeSetId);
  const published = currentState ?? {};
  const conflicts: StudioConflict[] = [];
  for (const item of itemsOf(state, changeSetId)) {
    const current = published[`${item.object_type}:${item.object_id}`];
    const at = (fieldName: string, changeSetValue: unknown, currentValue: unknown) =>
      conflicts.push({
        object_type: item.object_type,
        object_id: item.object_id,
        field_name: fieldName,
        change_set_value: changeSetValue,
        current_value: currentValue,
      });
    if (item.operation === "CREATE") {
      if (current !== undefined) at("<exists>", "CREATE", "ALREADY_EXISTS");
      continue;
    }
    if (item.operation === "DELETE") {
      if (current === undefined) at("<exists>", "DELETE", "ALREADY_DELETED");
      continue;
    }
    if (current === undefined) {
      at("<exists>", "UPDATE", "NOT_FOUND");
      continue;
    }
    if (item.before_snapshot === null) continue;
    for (const [field, beforeValue] of Object.entries(item.before_snapshot)) {
      const currentValue = current[field];
      const afterValue = item.after_snapshot?.[field];
      if (JSON.stringify(beforeValue) !== JSON.stringify(afterValue) && JSON.stringify(currentValue) !== JSON.stringify(beforeValue)) {
        at(field, afterValue ?? null, currentValue ?? null);
      }
    }
  }
  set.conflict_status = conflicts.length > 0 ? "CONFLICTED" : "CLEAN";
  set.updated_at = now();
  return conflicts;
}

/* ---- eval ----------------------------------------------------------------- */

export async function demoEvalRun(fixtures: DemoData, changeSetId: string): Promise<StudioEvalRunRead> {
  const state = await store(fixtures);
  setOf(state, changeSetId);
  const run = state.evalRuns[changeSetId];
  if (!run) throw new ApiError(404, "no eval run recorded for this change set");
  return clone(run);
}

/** What one mining pass over the demo estate finds: one metric a BI dashboard is bound to, and
 *  one tool a consumer invoked. Idempotent, as the server's pass is. */
const DEMO_MINING_CANDIDATES: ReadonlyArray<Pick<StudioEvalQuestionRead, "object_type" | "object_id" | "evidence_source" | "label">> = [
  { object_type: "METRIC", object_id: "metric:revenue", evidence_source: "BI", label: "metric:net_revenue" },
  { object_type: "TOOL", object_id: "tool:margin_by_region", evidence_source: "CONSUMPTION", label: "tool:margin_by_region" },
];

export async function demoMineEvalQuestions(fixtures: DemoData): Promise<StudioEvalMiningResult> {
  const state = await store(fixtures);
  let created = 0;
  for (const candidate of DEMO_MINING_CANDIDATES) {
    if (state.questions.some((q) => q.object_type === candidate.object_type && q.object_id === candidate.object_id)) continue;
    state.counter += 1;
    const stamp = now();
    state.questions.unshift({
      id: `demo-question-${state.counter}`,
      organization_id: DEMO_ORG,
      ...candidate,
      evidence_edge_id: `demo-edge-${state.counter}`,
      mined_at: stamp,
      created_at: stamp,
      updated_at: stamp,
    });
    created += 1;
  }
  return {
    consumption_edges_scanned: 12,
    bi_edges_scanned: 7,
    questions_created: created,
    questions_already_mined: DEMO_MINING_CANDIDATES.length - created,
    truncated: false,
  };
}

export async function demoEvalQuestions(
  fixtures: DemoData,
  query: { objectType?: string | null; limit?: number; offset?: number },
): Promise<StudioEvalQuestionRead[]> {
  const state = await store(fixtures);
  const rows = query.objectType ? state.questions.filter((q) => q.object_type === query.objectType) : state.questions;
  const offset = query.offset ?? 0;
  return clone(rows.slice(offset, offset + (query.limit ?? 100)));
}

/* ---- the two stateless checks ---------------------------------------------- */

/** First-line refusals of `validate_context_product_contract` (`studio.py`); the full
 *  `ContextProductDefinition` is the server's to judge. */
export async function demoValidateContextProduct(
  _fixtures: DemoData,
  body: StudioContextProductValidateRequest,
): Promise<StudioContextProductValidateResult> {
  const errors: string[] = [];
  const snapshot = body.snapshot ?? null;
  if (body.operation === "DELETE") {
    if (!UUID_SHAPE.test(body.object_id)) {
      errors.push(`object_id must be an existing context product UUID for DELETE: '${body.object_id}'`);
    }
    return { valid: errors.length === 0, errors };
  }
  if (snapshot === null) {
    return { valid: false, errors: ["context product definition missing: no after_snapshot provided"] };
  }
  const { product_key: productKey, project_id: projectId, ...definition } = snapshot as Record<string, unknown>;
  if (body.operation === "CREATE") {
    if (typeof productKey !== "string" || !productKey) {
      errors.push("CREATE requires a non-empty string product_key in after_snapshot");
    } else if (productKey !== body.object_id) {
      errors.push(`object_id ('${body.object_id}') must equal after_snapshot.product_key ('${productKey}')`);
    }
    if (typeof projectId !== "string" || !projectId) {
      errors.push("CREATE requires a non-empty string project_id in after_snapshot");
    } else if (!UUID_SHAPE.test(projectId)) {
      errors.push(`project_id is not a valid UUID: '${projectId}'`);
    }
  } else if (!UUID_SHAPE.test(body.object_id)) {
    errors.push(`object_id must be an existing context product UUID for UPDATE: '${body.object_id}'`);
  }
  if (errors.length > 0) return { valid: false, errors };
  return {
    valid: true,
    errors: [],
    definition,
    ...(body.operation === "CREATE"
      ? { product_key: productKey as string, project_id: projectId as string }
      : {}),
  };
}

/** Placeholders are `:name` here; the server parses the template with sqlglot for its dialect. */
export async function demoValidateParameterContract(
  _fixtures: DemoData,
  body: StudioParameterContractValidateRequest,
): Promise<StudioParameterContractValidateResult> {
  const definitions = body.parameters ?? [];
  const declared = new Set(definitions.map((definition) => String(definition.name ?? "")));
  const placeholders = new Set([...body.sql_template.matchAll(/(?<![:\w]):([A-Za-z_]\w*)/g)].map((match) => match[1]!));
  const errors: string[] = [];
  const missing = [...placeholders].filter((name) => !declared.has(name)).sort();
  const unused = [...declared].filter((name) => !placeholders.has(name)).sort();
  if (missing.length > 0) errors.push(`undeclared placeholders: ${missing.join(", ")}`);
  if (unused.length > 0) errors.push(`unused parameter definitions: ${unused.join(", ")}`);
  if (errors.length > 0) return { valid: false, errors, definitions };
  return { valid: true, errors: [], definitions, sample_rendered_sql: null };
}
