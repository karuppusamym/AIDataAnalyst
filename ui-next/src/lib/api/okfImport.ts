/* ---------------------------------------------------------------------------
   R11-OKF03 — the reviewer's full preview of an OKF import review.

     GET /v1/governance/reviews/{review_id}/okf-import-preview   one page of documents

   An OKF import raises `OKF_IMPORT_BATCH` reviews (table purposes and column
   descriptions) and `OKF_IMPORT_ROUTINE_DESCRIPTION` reviews (a routine's
   purpose). The generic review diff shows a batch's recorded old and new
   values; this read adds what a reviewer needs before approving an edit made
   in a file some time ago -- the changes grouped per document, and for each
   one whether the approved description moved since the export, predicted from
   the approval's own comparisons (`src/aida/okf_import_review.py`).

   The shapes below mirror `OkfImportReviewRead` in `okf_import_api.py`. They
   are written here rather than imported from the generated `../types` because
   that file is regenerated from the live OpenAPI document; once it is, these
   names can become aliases of the generated ones, as `reviewBatches.ts` does.

   No demo branch: the fixture review queue carries no OKF import, so demo mode
   never opens this preview.
--------------------------------------------------------------------------- */

import { get } from "./transport";

/** What approving will do with one change. */
export type OkfImportReviewState = "APPLIES" | "CONFLICT" | "TARGET_UNAVAILABLE" | "DECIDED";

export interface OkfImportReviewChange {
  change_id: string;
  subject_type: string;
  subject_id: string;
  /** `purpose`, or `column:<name>`. */
  field: string;
  label: string;
  /** What the proposal replaces, as the import recorded it. Null: none, or withheld. */
  before_value: string | null;
  proposed_value: string | null;
  expected_version: number | null;
  current_version: number | null;
  /** The approved text now, only when it is not what the import replaces. */
  current_value: string | null;
  status: string;
  skip_reason: string | null;
  state: OkfImportReviewState | string;
  reason_code: string | null;
  target_active: boolean | null;
  approval_effect: string;
}

export interface OkfImportReviewDocument {
  document_id: string;
  label: string;
  object_type: string;
  conflicts: number;
  changes: OkfImportReviewChange[];
}

export interface OkfImportReview {
  review_id: string;
  object_type: string;
  object_id: string;
  review_status: string;
  requested_by: string;
  proposal_status: string;
  datasource_id: string;
  filename: string | null;
  archive_sha256: string | null;
  /** Over every document, not only this page. */
  counts: Record<string, number>;
  offset: number;
  limit: number;
  total_documents: number;
  documents: OkfImportReviewDocument[];
  authority: string;
}

/** The review object types this preview serves. */
export const OKF_IMPORT_REVIEW_TYPES: ReadonlySet<string> = new Set([
  "OKF_IMPORT_BATCH",
  "OKF_IMPORT_ROUTINE_DESCRIPTION",
]);

/** `GET /v1/governance/reviews/{review_id}/okf-import-preview` -- one page of documents. */
export function fetchOkfImportReview(
  reviewId: string,
  page: { offset: number; limit: number },
  signal?: AbortSignal,
): Promise<OkfImportReview> {
  const query = new URLSearchParams({ offset: String(page.offset), limit: String(page.limit) });
  return get(
    `/v1/governance/reviews/${encodeURIComponent(reviewId)}/okf-import-preview?${query.toString()}`,
    signal,
  );
}
