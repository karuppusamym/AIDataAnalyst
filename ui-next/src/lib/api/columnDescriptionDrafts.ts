/* ---------------------------------------------------------------------------
   Column description drafts -- `src/aida/column_description_api.py`.

     POST /v1/organizations/{org}/column-description-drafts/generate
     GET  /v1/organizations/{org}/column-description-drafts?table_id=
     PUT  /v1/column-description-drafts/{draft_id}
     POST /v1/column-description-drafts/{draft_id}/submit
     POST /v1/tables/{table_id}/column-description-drafts/submit

   The evidence threshold a draft must clear to be submitted is deliberately
   NOT mirrored here. The table-draft client copies `0.4` from the server and
   has to keep it in step by hand; every column draft instead arrives with a
   server-computed `reviewable`, so this client cannot drift from
   `MINIMUM_EVIDENCE_FOR_REVIEW`.

   There is no approve call. A draft is published only by an independent
   decision on its governance review, through the review queue, like every
   other description on this platform.
--------------------------------------------------------------------------- */

import type {
  ColumnDescriptionDraftBulkSubmitResult,
  ColumnDescriptionDraftGenerateResult,
  ColumnDescriptionDraftRead,
  GovernanceReviewRead,
} from "../types";
import { get, postJson, putJson, USE_FIXTURES } from "./transport";

const FIXTURE_NOTICE =
  "Column description drafts are composed by the server. Run against a live API " +
  "(VITE_USE_FIXTURES=0) to draft them.";

/** Page size for listing one table's drafts. Pages are followed to the end:
 *  a table wider than one page must not come back looking narrower than it is. */
const LIST_PAGE_SIZE = 500;

const orgPath = (organizationId: string) =>
  `/v1/organizations/${encodeURIComponent(organizationId)}/column-description-drafts`;

/** Draft descriptions for a table's undescribed columns. */
export async function generateColumnDescriptionDrafts(
  organizationId: string,
  tableIds: string[],
  options: { includeDescribed?: boolean } = {},
  signal?: AbortSignal,
): Promise<ColumnDescriptionDraftGenerateResult> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  return postJson<ColumnDescriptionDraftGenerateResult>(
    `${orgPath(organizationId)}/generate`,
    { table_ids: tableIds, include_described: options.includeDescribed ?? false },
    signal,
  );
}

/** Every draft for one table, in column order.
 *
 *  Under fixtures there are no drafts to show, and an empty list is the true
 *  answer for demo data rather than a failure: generating is what reports
 *  that the server is needed. */
export async function listTableColumnDescriptionDrafts(
  organizationId: string,
  tableId: string,
  signal?: AbortSignal,
): Promise<ColumnDescriptionDraftRead[]> {
  if (USE_FIXTURES) return [];
  const drafts: ColumnDescriptionDraftRead[] = [];
  for (let offset = 0; ; offset += LIST_PAGE_SIZE) {
    const page = await get<{ items?: ColumnDescriptionDraftRead[]; total?: number }>(
      `${orgPath(organizationId)}?table_id=${encodeURIComponent(tableId)}` +
        `&limit=${LIST_PAGE_SIZE}&offset=${offset}`,
      signal,
    );
    const items = page.items ?? [];
    drafts.push(...items);
    const total = typeof page.total === "number" ? page.total : drafts.length;
    if (items.length === 0 || drafts.length >= total) break;
  }
  return drafts;
}

/** Fix a draft's text. `expectedText` is what the editor started from; the
 *  server refuses the edit (409) if someone else changed the draft meanwhile. */
export async function editColumnDescriptionDraft(
  draftId: string,
  draftedText: string,
  expectedText: string,
  signal?: AbortSignal,
): Promise<ColumnDescriptionDraftRead> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  return putJson<ColumnDescriptionDraftRead>(
    `/v1/column-description-drafts/${encodeURIComponent(draftId)}`,
    { drafted_text: draftedText, expected_text: expectedText },
    signal,
  );
}

/** Send one draft to the review queue. Publishes nothing. */
export async function submitColumnDescriptionDraft(
  draftId: string,
  signal?: AbortSignal,
): Promise<GovernanceReviewRead> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  return postJson<GovernanceReviewRead>(
    `/v1/column-description-drafts/${encodeURIComponent(draftId)}/submit`,
    undefined,
    signal,
  );
}

/** Send every reviewable draft of one table to the review queue -- still one
 *  review per draft, so a reviewer can accept one column and refuse another. */
export async function submitTableColumnDescriptionDrafts(
  tableId: string,
  signal?: AbortSignal,
): Promise<ColumnDescriptionDraftBulkSubmitResult> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  return postJson<ColumnDescriptionDraftBulkSubmitResult>(
    `/v1/tables/${encodeURIComponent(tableId)}/column-description-drafts/submit`,
    undefined,
    signal,
  );
}
