/* ---------------------------------------------------------------------------
   Column-level descriptions, and the model workbook export.

   Two endpoints that had no client here:

   * `GET /v1/tables/{table_id}/column-documentation`
     (`src/aida/column_documentation_api.py`) -- a table's columns with the
     source-system comment and the authored business description as separate
     fields. The app already called `/v1/tables/{id}/columns`, but only ever
     read `{id, name}` out of it for metric-builder dropdowns, so a column
     description had no way to reach a screen even when one existed.

   * `GET /v1/datasources/{datasource_id}/model/export.xlsx`
     (`src/aida/model_export_api.py`) -- the whole model as a workbook.

   Also the model workbook's re-import path (upload, preview, submit) and the
   withdrawal of an approved description.

   Transport, identity headers and the demo switch come from `./transport`:
   this module never calls `fetch` and never decodes an error itself.
   Re-exported from `lib/api.ts`; no screen import changed.
--------------------------------------------------------------------------- */

import { USE_FIXTURES, get, postJson, requestBlob, requestRawBody } from "./transport";

import type { PageOf } from "../ui-types";

/* R05/F06/F14: this module used to build its own `identityHeaders()` and its
 * own `fetch` wrapper. That copy sent the development principal in every live
 * mode -- so the F06 rule (dev headers only in development mode) did not apply
 * to these endpoints -- and decoded only `{"detail": ...}`, discarding the
 * correlation id F14 preserves.
 *
 * Five `fetch` calls survived that first correction: the workbook download,
 * the workbook upload, and three plain JSON POSTs (submit an import, exclude
 * rows from one, request a description withdrawal). Their headers were right
 * by then, but each still carried a `{"detail": ...}`-only decoder, so a 409
 * or a 422 on any of these five reached the screen as a bare status line with
 * no error code, no correlation id and no field errors -- and none of the
 * five ever reached `observeRequests`, so the shell's connection state (F13)
 * could not see them fail. All five now go through `./transport`. */

/** `src/aida/column_documentation_api.py::ColumnDocumentationRead`.
 *
 *  `source_description` and `business_description` are deliberately separate:
 *  the first is the source system's own comment, overwritten by the next
 *  rediscovery pass; the second is reviewed, authored content that rediscovery
 *  never touches. A UI that merged them would show a steward two kinds of
 *  claim with different durability as if they were one. */
export interface ColumnDocumentationRead {
  column_id: string;
  table_id: string;
  name: string;
  ordinal_position: number;
  physical_type: string;
  nullable: boolean;
  classification: string;
  classification_source: string;
  source_description: string | null;
  business_description: string | null;
  description_version: number | null;
  description_approved_by: string | null;
  description_approved_at: string | null;
  source_claim_id: string | null;
  /** Set when this column *had* an approved description that was retired
   *  through review. Distinct from `business_description === null` with no
   *  withdrawal, which means nobody has described it yet -- "we looked and
   *  decided to say nothing" and "nobody has looked" are different facts. */
  withdrawn_description: string | null;
}

export function saveColumnWorksheet(tableId: string, changes: {column_id: string; description: string; expected_version: number | null}[]): Promise<ModelImportBatchRead> {
  if (USE_FIXTURES) return Promise.reject(new Error("Worksheet saving requires a connected backend."));
  return postJson<ModelImportBatchRead>(`/v1/tables/${encodeURIComponent(tableId)}/column-worksheet`, { changes });
}

const readJson = get;

/** Fixture columns for a table, so the pane renders something recognisable in
 *  a demo build. These are written here rather than in `lib/fixtures.ts`, and
 *  stay here: that module is dropped from a live build (R11-X1), which this
 *  branch is never reached in anyway.
 *
 *  Deliberately mixed: some columns carry only a source comment, some carry
 *  an authored description, some carry neither -- because "most columns have
 *  no authored description yet" is the true state of a real catalog, and a
 *  fixture where every row is fully documented would hide exactly the empty
 *  state the pane most needs to render well. */
function makeFixtureColumnDocumentation(tableId: string): ColumnDocumentationRead[] {
  const base = {
    table_id: tableId,
    classification_source: "RULE",
    description_version: null,
    description_approved_by: null,
    description_approved_at: null,
    source_claim_id: null,
    withdrawn_description: null,
  };
  return [
    {
      ...base,
      column_id: `${tableId}-c1`,
      name: "customer_id",
      ordinal_position: 0,
      physical_type: "uuid",
      nullable: false,
      classification: "INTERNAL",
      source_description: "pk",
      withdrawn_description: null,
      business_description:
        "The customer's unique identifier across every retail system. Stable for the life of the relationship; not reused after closure.",
      description_version: 2,
      description_approved_by: "checker@example.com",
      description_approved_at: "2026-08-30T09:14:00Z",
    },
    {
      ...base,
      column_id: `${tableId}-c2`,
      name: "national_id",
      ordinal_position: 1,
      physical_type: "varchar(32)",
      nullable: true,
      classification: "RESTRICTED",
      source_description: "govt id number",
      business_description: null,
    },
    {
      ...base,
      column_id: `${tableId}-c3`,
      name: "opened_at",
      ordinal_position: 2,
      physical_type: "timestamptz",
      nullable: false,
      classification: "INTERNAL",
      source_description: null,
      business_description: null,
      // A column that was described and had it retired -- the state a reader
      // must be able to tell apart from "never described".
      withdrawn_description: "Superseded by the account-opening data contract.",
    },
    {
      ...base,
      column_id: `${tableId}-c4`,
      name: "segment_code",
      ordinal_position: 3,
      physical_type: "varchar(8)",
      nullable: true,
      classification: "INTERNAL",
      source_description: null,
      business_description:
        "Marketing segment assigned by the nightly segmentation job. Not authoritative for regulatory reporting.",
      description_version: 1,
      description_approved_by: "checker@example.com",
      description_approved_at: "2026-08-12T16:02:00Z",
    },
  ];
}

/** One table's columns with both descriptions resolved. */
export async function fetchColumnDocumentation(
  tableId: string,
  signal?: AbortSignal,
): Promise<ColumnDocumentationRead[]> {
  if (USE_FIXTURES) return makeFixtureColumnDocumentation(tableId);
  const columns: ColumnDocumentationRead[] = [];
  const seen = new Set<string>();
  let total: number | null = null;
  while (true) {
    const page = await readJson<PageOf<ColumnDocumentationRead>>(
      `/v1/tables/${encodeURIComponent(tableId)}/column-documentation?limit=1000${columns.length ? `&offset=${columns.length}` : ""}`,
      signal,
    );
    if (!Number.isInteger(page.total) || page.total < 0 || page.total > 10000 || (total !== null && page.total !== total)) {
      throw new Error("Column count changed or exceeds the 10,000-column browser limit. Refresh or use a source export.");
    }
    total = page.total;
    if (!page.items.length && columns.length < total) throw new Error("Column documentation is incomplete. Retry loading the table.");
    for (const column of page.items) {
      if (column.table_id !== tableId || seen.has(column.column_id)) throw new Error("Column documentation has inconsistent rows. Retry loading the table.");
      seen.add(column.column_id); columns.push(column);
    }
    if (columns.length > total) throw new Error("Column documentation has an inconsistent count.");
    if (columns.length === total) return columns;
  }
}

function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const match = /filename="?([^";]+)"?/i.exec(header);
  return match?.[1] ?? fallback;
}

/** Download the datasource's model workbook.
 *
 *  Fetched rather than linked: a bare `<a download href>` cannot carry this
 *  app's identity headers, so the same object-URL idiom `exportAssetEvidence`
 *  uses applies here. The response is binary, so the body is read as a blob --
 *  never parsed as JSON, which would corrupt it.
 *
 *  Under fixtures there is no workbook to produce (the writer is server-side),
 *  so this reports that plainly instead of downloading a fake file a steward
 *  might then try to edit and re-upload. */
export async function downloadDatasourceModelWorkbook(
  datasourceId: string,
  datasourceName: string,
  signal?: AbortSignal,
): Promise<void> {
  if (USE_FIXTURES) {
    throw new Error(
      "The workbook is composed by the server. Run against a live API (VITE_USE_FIXTURES=0) to export.",
    );
  }
  const path = `/v1/datasources/${encodeURIComponent(datasourceId)}/model/export.xlsx`;
  const { blob, response } = await requestBlob(path, { signal });
  const slug = datasourceName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const filename = filenameFromDisposition(
    response.headers.get("Content-Disposition"),
    `${slug || "datasource"}-model.xlsx`,
  );
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------------------------------------------------------------------
   Workbook re-import: upload -> preview -> submit.

   Three calls because it is three steps, and the middle one is the point: an
   upload parses and diffs but publishes nothing, so a steward who uploaded the
   wrong file sees a nonsense diff and abandons it rather than putting hundreds
   of spurious changes in front of a reviewer. See
   `src/aida/model_import_api.py`.
--------------------------------------------------------------------------- */

/** `src/aida/model_import_api.py::ModelImportBatchRead`. */
export interface ModelImportBatchRead {
  id: string;
  organization_id: string;
  datasource_id: string;
  filename: string;
  content_sha256: string;
  status: "DRAFT" | "PENDING_REVIEW" | "APPLIED" | "REJECTED";
  governance_review_id: string | null;
  change_count: number;
  applied_count: number;
  skipped_count: number;
  rejected_row_count: number;
  uploaded_by: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
}

/** `src/aida/model_import_api.py::ModelImportChangeRead`. */
export interface ModelImportChangeRead {
  id: string;
  batch_id: string;
  sheet_name: string;
  row_number: number;
  subject_type: "TABLE" | "COLUMN";
  subject_id: string;
  subject_label: string;
  field: string;
  old_value: string | null;
  new_value: string;
  expected_version: number | null;
  status:
    | "PENDING"
    | "APPLIED"
    | "SKIPPED_STALE"
    | "SKIPPED_MISSING"
    | "REJECTED"
    /** Dropped by the uploader before the batch was submitted. Never applied,
     *  and never counted as skipped -- it was withdrawn before anyone was
     *  asked to look at it. */
    | "EXCLUDED";
  skip_reason: string | null;
}

const FIXTURE_NOTICE =
  "Uploads are parsed by the server. Run against a live API (VITE_USE_FIXTURES=0) to import a workbook.";

/** Upload an edited workbook. Parses and diffs; publishes nothing.
 *
 *  The file is sent as the raw request body rather than as multipart form
 *  data: the server takes it that way (`python-multipart` is not a pinned
 *  dependency there), and it lets the browser stream the `File` straight
 *  through instead of base64-encoding it into a JSON field. */
export async function uploadModelWorkbook(
  datasourceId: string,
  file: File,
  signal?: AbortSignal,
): Promise<ModelImportBatchRead> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  const path =
    `/v1/datasources/${encodeURIComponent(datasourceId)}/model/import` +
    `?filename=${encodeURIComponent(file.name)}`;
  return requestRawBody<ModelImportBatchRead>(
    "POST",
    path,
    file,
    "application/octet-stream",
    signal,
  );
}

/** Every change a batch would make, or made -- rejected rows included.
 *
 *  Not filtered to the changes that worked: an upload that hid the rows it
 *  could not understand would look cleaner than it was, and those rows are
 *  exactly what a steward needs to see. */
export async function fetchModelImportChanges(
  batchId: string,
  signal?: AbortSignal,
): Promise<ModelImportChangeRead[]> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  const rows: ModelImportChangeRead[] = [];
  let expectedTotal: number | null = null;
  const seen = new Set<string>();
  // Never present the first page as the complete diff for a larger batch.
  // Bound the in-browser preview; an oversized workbook must be split rather
  // than silently approving rows that were not available to inspect.
  const previewLimit = 50_000;
  while (true) {
    const page = await readJson<PageOf<ModelImportChangeRead>>(
      `/v1/model-imports/${encodeURIComponent(batchId)}/changes?limit=1000&offset=${rows.length}`,
      signal,
    );
    if (!Number.isInteger(page.total) || page.total < 0 || page.total > previewLimit) {
      throw new Error("The workbook preview exceeds the supported 50,000 changes or has an invalid count. Split large workbooks into smaller imports.");
    }
    if (expectedTotal !== null && page.total !== expectedTotal) {
      throw new Error("The import changed while its preview was loading. Retry the preview.");
    }
    expectedTotal = page.total;
    const items = page.items ?? [];
    if ((items.length === 0 && rows.length < expectedTotal) || rows.length + items.length > expectedTotal) {
      throw new Error("The workbook preview is incomplete. Retry the preview before submitting.");
    }
    for (const row of items) {
      if (row.batch_id !== batchId || seen.has(row.id)) {
        throw new Error("The workbook preview contains inconsistent rows. Retry the preview.");
      }
      seen.add(row.id);
      rows.push(row);
    }
    if (rows.length === expectedTotal) return rows;
  }
}

/** Submit a parsed batch into the shared review queue. Still publishes
 *  nothing -- someone other than the submitter has to approve it. */
export async function submitModelImport(
  batchId: string,
  signal?: AbortSignal,
): Promise<ModelImportBatchRead> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  // No body, deliberately: the batch id in the path is the whole request, so
  // `postJson` is called with `undefined` and sends no `Content-Type` either.
  return postJson<ModelImportBatchRead>(
    `/v1/model-imports/${encodeURIComponent(batchId)}/submit`,
    undefined,
    signal,
  );
}

/* ---------------------------------------------------------------------------
   Retiring a description, and trimming a workbook batch before it is reviewed.
--------------------------------------------------------------------------- */

/** `src/aida/description_withdrawal_api.py::DescriptionWithdrawalRead`. */
export interface DescriptionWithdrawalRead {
  id: string;
  organization_id: string;
  request_type: "WITHDRAW" | "REINSTATE";
  subject_type: "TABLE" | "COLUMN";
  subject_id: string;
  subject_label: string;
  version_id: string;
  withdrawn_text: string;
  reason: string;
  status: string;
  governance_review_id: string | null;
  requested_by: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
}

/** Ask for an approved description to be retired, or a retired one brought back.
 *
 *  Publishes nothing either way: the description stays exactly what every
 *  reader resolves until a *different* principal approves the review this
 *  creates, on the Review queue. There is deliberately no approve call here. */
export async function requestDescriptionWithdrawal(
  subjectType: "TABLE" | "COLUMN",
  subjectId: string,
  reason: string,
  requestType: "WITHDRAW" | "REINSTATE" = "WITHDRAW",
  signal?: AbortSignal,
): Promise<DescriptionWithdrawalRead> {
  if (USE_FIXTURES) {
    throw new Error(
      "This is reviewed on the server. Run against a live API (VITE_USE_FIXTURES=0) to request it.",
    );
  }
  return postJson<DescriptionWithdrawalRead>(
    "/v1/descriptions/withdrawals",
    {
      subject_type: subjectType,
      subject_id: subjectId,
      reason,
      request_type: requestType,
    },
    signal,
  );
}

/** Drop rows from a parsed batch, or put them back.
 *
 *  Only works while the batch is DRAFT. That is the point: what a reviewer is
 *  asked to decide has to be fixed the moment it is submitted, so this is an
 *  uploader-side edit rather than a partial approval. */
export async function setModelImportExclusion(
  batchId: string,
  changeIds: string[],
  excluded: boolean,
  signal?: AbortSignal,
): Promise<ModelImportBatchRead> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  return postJson<ModelImportBatchRead>(
    `/v1/model-imports/${encodeURIComponent(batchId)}/changes/exclusion`,
    { change_ids: changeIds, excluded },
    signal,
  );
}

/** `src/aida/column_documentation_api.py::TableDescriptionRead`.
 *
 *  The table's own documentation state. The evidence pane's items are prose
 *  claims — good for reading, useless for driving an action; a withdraw or
 *  reinstate control needs to know structurally whether there is an approved
 *  description and which version it is. */
export interface TableDescriptionRead {
  table_id: string;
  name: string;
  source_description: string | null;
  readme: string | null;
  readme_version: number | null;
  approved_by: string | null;
  approved_at: string | null;
  withdrawn_readme: string | null;
}

export async function fetchTableDescription(
  tableId: string,
  signal?: AbortSignal,
): Promise<TableDescriptionRead> {
  if (USE_FIXTURES) {
    return {
      table_id: tableId,
      name: "customer_dim",
      source_description: "customer master, loaded nightly",
      readme:
        "One row per retail banking customer. Sourced from the party master and deduplicated nightly; closed relationships are retained, not deleted.",
      readme_version: 3,
      approved_by: "checker@example.com",
      approved_at: "2026-08-28T11:20:00Z",
      withdrawn_readme: null,
    };
  }
  return readJson<TableDescriptionRead>(
    `/v1/tables/${encodeURIComponent(tableId)}/description`,
    signal,
  );
}
