/* ---------------------------------------------------------------------------
   Studio — change sets as a unit of review: the set, its items, the diff a
   reviewer reads, the impact preview, and the submit that puts it in front of
   a checker.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { demoOr } from "./transport";

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
    async () => makeFixtureStudioChangeSets(query),
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
    async () => makeFixtureStudioChangeSetItems(changeSetId),
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
    async () => makeFixtureStudioDiff(changeSetId),
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
    async () => makeFixtureStudioImpact(changeSetId),
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
    async () => makeFixtureSubmitStudioChangeSet(changeSetId),
    async () => {
      return postJson<StudioChangeSetRead>(`/v1/studio/change-sets/${changeSetId}/submit`, {}, signal);
    },
  );
}
