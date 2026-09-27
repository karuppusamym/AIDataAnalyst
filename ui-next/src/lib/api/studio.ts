/* ---------------------------------------------------------------------------
   Studio — change sets as a unit of review: the set, its items, the diff a
   reviewer reads, the impact preview, and the submit that puts it in front of
   a checker.

   R11-AUD08 added the AUTHORING half, which had API routes and no caller:

     POST   /v1/studio/change-sets                                  create (DRAFT)
     POST   /v1/studio/change-sets/{id}/items                       add an item (DRAFT only)
     DELETE /v1/studio/change-sets/{id}/items/{item_id}             remove one (DRAFT only)
     POST   /v1/studio/change-sets/{id}/test                        run the tests + the eval gate
     POST   /v1/studio/change-sets/{id}/detect-conflicts            compare with a published state
     GET    /v1/studio/change-sets/{id}/eval                        the latest eval-gate run
     POST   /v1/studio/eval/mine                                    mine usage into eval questions
     GET    /v1/studio/eval/questions                               the mined corpus
     POST   /v1/studio/context-products/validate                    stateless shape check
     POST   /v1/studio/parameter-contracts/validate                 stateless contract check

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.

   DEMO MODE. Every function answers from `../studioAuthoringDemo` when the
   build carries demo data -- the existing reads included, so that a change set
   created in demo mode is on the very next list read. The demo module is
   imported dynamically, through a loader that tests the build's demo literal
   itself (`noDemoData` says why `demoOr` alone did not drop it), so a live
   build does not contain it.
--------------------------------------------------------------------------- */

import { deleteRequest, demoOr, get, noDemoData, postJson } from "./transport";
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
  StudioEvalRunRead,
  StudioImpactPreview,
  StudioParameterContractValidateRequest,
  StudioParameterContractValidateResult,
  StudioTestResultRead,
} from "../types";

/** The demo store, loaded only where a demo arm runs, and absent from a live build (`noDemoData`). */
const studioDemo = (): Promise<typeof import("../studioAuthoringDemo")> =>
  import.meta.env.VITE_USE_FIXTURES === "0" ? noDemoData() : import("../studioAuthoringDemo");

export interface StudioChangeSetQuery {
  status?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/studio/change-sets` (ST-A7, `studio_api.py`). */
export function fetchStudioChangeSets(
  query: StudioChangeSetQuery,
  signal?: AbortSignal,
): Promise<StudioChangeSetRead[]> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoListChangeSets(fixtures, query),
    async () => {
      const params = new URLSearchParams();
      if (query.status) params.set("status", query.status);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<StudioChangeSetRead[]>(`/v1/studio/change-sets?${params}`, signal);
    },
  );
}

/** `GET /v1/studio/change-sets/{id}/items` — every `StudioChangeItem` in one
 *  change set, each carrying its own `before`/`after` snapshot and precomputed
 *  `diff` (`compute_diff`, `aida.studio`). */
export function fetchStudioChangeSetItems(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioChangeItemRead[]> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoChangeSetItems(fixtures, changeSetId),
    async () => {
      return get<StudioChangeItemRead[]>(`/v1/studio/change-sets/${changeSetId}/items`, signal);
    },
  );
}

/** `GET /v1/studio/change-sets/{id}/diff` — the same items' diffs, composed
 *  as one document for a change-set-level review. */
export function fetchStudioDiff(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioDiffRead> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoDiff(fixtures, changeSetId),
    async () => {
      return get<StudioDiffRead>(`/v1/studio/change-sets/${changeSetId}/diff`, signal);
    },
  );
}

/** `GET /v1/studio/change-sets/{id}/impact` (`compute_impact`, `aida.studio`)
 *  — this is the change-set author's own evidence pane: what merging this
 *  change set would touch, before it is ever submitted for review. */
export function fetchStudioImpact(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioImpactPreview> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoImpact(fixtures, changeSetId),
    async () => {
      return get<StudioImpactPreview>(`/v1/studio/change-sets/${changeSetId}/impact`, signal);
    },
  );
}

/** `POST /v1/studio/change-sets/{id}/submit` — the real test-gated,
 *  eval-gated submission path (ST-A7/ST-A8), materializing any
 *  `CONTEXT_PRODUCT` item through `studio_context_product.py` into the same
 *  `GovernanceReview` queue `ReviewQueueScreen` reads. */
export function submitStudioChangeSet(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioChangeSetRead> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoSubmit(fixtures, changeSetId),
    async () => {
      return postJson<StudioChangeSetRead>(`/v1/studio/change-sets/${changeSetId}/submit`, {}, signal);
    },
  );
}

/* ---------------------------------------------------------------------------
   Authoring (R11-AUD08). Write roles: DataSteward, MetadataAdmin, PlatformAdmin,
   SemanticAdmin -- the reads and the two validators admit those plus Analyst,
   Auditor, Reviewer and Viewer (`surface-control-matrix.md`).
--------------------------------------------------------------------------- */

/** `POST /v1/studio/change-sets` — a new change set, DRAFT and CLEAN, authored by
 *  the caller. `name` is 2-200 characters; a shorter or longer one is a 422 whose
 *  sentence the caller shows as it is. */
export function createStudioChangeSet(
  body: StudioChangeSetCreate,
  signal?: AbortSignal,
): Promise<StudioChangeSetRead> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoCreateChangeSet(fixtures, body),
    async () => postJson<StudioChangeSetRead>("/v1/studio/change-sets", body, signal),
  );
}

/** `POST /v1/studio/change-sets/{id}/items` — one proposed change. DRAFT only: any
 *  other status is a 409 ("items can only be added to DRAFT change sets"). The server
 *  computes the item's `diff` only when BOTH snapshots are non-empty. */
export function addStudioChangeItem(
  changeSetId: string,
  body: StudioChangeItemCreate,
  signal?: AbortSignal,
): Promise<StudioChangeItemRead> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoAddItem(fixtures, changeSetId, body),
    async () =>
      postJson<StudioChangeItemRead>(
        `/v1/studio/change-sets/${encodeURIComponent(changeSetId)}/items`,
        body,
        signal,
      ),
  );
}

/** `DELETE /v1/studio/change-sets/{id}/items/{item_id}` — 204, nothing back. DRAFT only
 *  (409 otherwise); an item that is not in this change set is a 404. */
export function removeStudioChangeItem(
  changeSetId: string,
  itemId: string,
  signal?: AbortSignal,
): Promise<void> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoRemoveItem(fixtures, changeSetId, itemId),
    async () =>
      deleteRequest(
        `/v1/studio/change-sets/${encodeURIComponent(changeSetId)}/items/${encodeURIComponent(itemId)}`,
        signal,
      ),
  );
}

/** `POST /v1/studio/change-sets/{id}/test` — runs the item validators and the
 *  eval-regression gate. NOT a read: it moves the change set to TESTING, records each
 *  item's test status, and writes an eval run and audit rows. DRAFT or TESTING only
 *  (409 otherwise). The response carries the overall verdict and counts in `evidence`
 *  -- not a per-item breakdown; each item's own status is read back from `.../items`. */
export function runStudioTests(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioTestResultRead> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoRunTests(fixtures, changeSetId),
    async () =>
      postJson<StudioTestResultRead>(
        `/v1/studio/change-sets/${encodeURIComponent(changeSetId)}/test`,
        {},
        signal,
      ),
  );
}

/** The published state a conflict check compares against: `"OBJECT_TYPE:object_id"` ->
 *  that object's current published snapshot. */
export type StudioPublishedState = Record<string, Record<string, unknown>>;

/** `POST /v1/studio/change-sets/{id}/detect-conflicts` — the request BODY IS the state
 *  map itself (the handler's one body parameter is `current_state`, not wrapped), and it
 *  may be empty. Not a read: it records CONFLICTED or CLEAN on the change set.
 *
 *  An empty state is not "nothing to compare": the server treats every UPDATE as
 *  NOT_FOUND and every DELETE as ALREADY_DELETED when no published snapshot is given
 *  for its key (`detect_conflicts`, `studio.py`), and marks the set CONFLICTED. */
export function detectStudioConflicts(
  changeSetId: string,
  currentState: StudioPublishedState | null,
  signal?: AbortSignal,
): Promise<StudioConflict[]> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoDetectConflicts(fixtures, changeSetId, currentState),
    async () =>
      postJson<StudioConflict[]>(
        `/v1/studio/change-sets/${encodeURIComponent(changeSetId)}/detect-conflicts`,
        currentState ?? {},
        signal,
      ),
  );
}

/** `GET /v1/studio/change-sets/{id}/eval` — the most recent eval-gate run. A change set
 *  that has never been tested has none: the server answers 404 "no eval run recorded for
 *  this change set", which is a normal state and not a failure to load. */
export function fetchStudioEvalRun(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<StudioEvalRunRead> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoEvalRun(fixtures, changeSetId),
    async () =>
      get<StudioEvalRunRead>(`/v1/studio/change-sets/${encodeURIComponent(changeSetId)}/eval`, signal),
  );
}

/** `POST /v1/studio/eval/mine` — scans recent consumption and BI-lineage edges (a bounded
 *  scan; `truncated` says it hit the bound) and creates one eval question per governed
 *  metric or tool not yet mined. Idempotent. Organization-wide, not per change set. */
export function mineStudioEvalQuestions(signal?: AbortSignal): Promise<StudioEvalMiningResult> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoMineEvalQuestions(fixtures),
    async () => postJson<StudioEvalMiningResult>("/v1/studio/eval/mine", {}, signal),
  );
}

export interface StudioEvalQuestionQuery {
  objectType?: string | null;
  limit?: number;
  offset?: number;
}

/** `GET /v1/studio/eval/questions` — the mined corpus, newest first. */
export function fetchStudioEvalQuestions(
  query: StudioEvalQuestionQuery,
  signal?: AbortSignal,
): Promise<StudioEvalQuestionRead[]> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoEvalQuestions(fixtures, query),
    async () => {
      const params = new URLSearchParams();
      if (query.objectType) params.set("object_type", query.objectType);
      params.set("limit", String(query.limit ?? 100));
      params.set("offset", String(query.offset ?? 0));
      return get<StudioEvalQuestionRead[]>(`/v1/studio/eval/questions?${params}`, signal);
    },
  );
}

/** `POST /v1/studio/context-products/validate` — the shape check a CONTEXT_PRODUCT item's
 *  own test runs, without a change set. Stateless and read-only in effect (the handler
 *  takes no session and writes nothing); it does not look references up -- that happens
 *  at submission. */
export function validateStudioContextProduct(
  body: StudioContextProductValidateRequest,
  signal?: AbortSignal,
): Promise<StudioContextProductValidateResult> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoValidateContextProduct(fixtures, body),
    async () => postJson<StudioContextProductValidateResult>("/v1/studio/context-products/validate", body, signal),
  );
}

/** `POST /v1/studio/parameter-contracts/validate` — a tool's typed parameter contract
 *  against its SQL template, with a sample render when it holds. Stateless and read-only
 *  in effect, like the context-product check. */
export function validateStudioParameterContract(
  body: StudioParameterContractValidateRequest,
  signal?: AbortSignal,
): Promise<StudioParameterContractValidateResult> {
  return demoOr(
    async (fixtures) => (await studioDemo()).demoValidateParameterContract(fixtures, body),
    async () =>
      postJson<StudioParameterContractValidateResult>("/v1/studio/parameter-contracts/validate", body, signal),
  );
}
