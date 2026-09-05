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

   Kept in a dedicated append file rather than mixed into the ~4.5k-line
   `api.ts`, following `_api_append.ts`'s precedent, so this slice reads as one
   contiguous change. Same reason that file gives for re-deriving the request
   helpers instead of importing them: `get` is module-private in `api.ts`.
--------------------------------------------------------------------------- */

import { ApiError } from "./api";
import { getCurrentOrgId } from "./org";
import type { PageOf } from "./ui-types";

const USE_FIXTURES = import.meta.env.VITE_USE_FIXTURES !== "0";
const DEV_PRINCIPAL_ID = import.meta.env.VITE_DEV_PRINCIPAL_ID || "local-ui-admin";
const DEV_ROLES =
  import.meta.env.VITE_DEV_ROLES ||
  "PlatformAdmin,OrganizationAdmin,ProjectAdmin,MetadataAdmin,MetadataIngestor,DataAdmin,SemanticAdmin,DataSteward,ToolDeveloper,ToolConsumer,AgentDeveloper,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer";

function identityHeaders(): Record<string, string> {
  if (USE_FIXTURES) return {};
  return {
    "X-Principal-Id": DEV_PRINCIPAL_ID,
    "X-Roles": DEV_ROLES,
    "X-Organization-Id": getCurrentOrgId(),
  };
}

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

async function readJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    signal,
    headers: { Accept: "application/json", ...identityHeaders() },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Fixture columns for a table, so the pane renders something recognisable
 *  under the default `VITE_USE_FIXTURES=1`.
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
  const page = await readJson<PageOf<ColumnDocumentationRead>>(
    `/v1/tables/${encodeURIComponent(tableId)}/column-documentation?limit=1000`,
    signal,
  );
  return page.items ?? [];
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
  const res = await fetch(path, {
    signal,
    headers: { ...identityHeaders() },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* the error body may itself be non-JSON */
    }
    throw new ApiError(res.status, detail);
  }
  const slug = datasourceName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const filename = filenameFromDisposition(
    res.headers.get("Content-Disposition"),
    `${slug || "datasource"}-model.xlsx`,
  );
  const url = URL.createObjectURL(await res.blob());
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
  const res = await fetch(path, {
    method: "POST",
    signal,
    body: file,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/octet-stream",
      ...identityHeaders(),
    },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as ModelImportBatchRead;
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
  const page = await readJson<PageOf<ModelImportChangeRead>>(
    `/v1/model-imports/${encodeURIComponent(batchId)}/changes?limit=1000`,
    signal,
  );
  return page.items ?? [];
}

/** Submit a parsed batch into the shared review queue. Still publishes
 *  nothing -- someone other than the submitter has to approve it. */
export async function submitModelImport(
  batchId: string,
  signal?: AbortSignal,
): Promise<ModelImportBatchRead> {
  if (USE_FIXTURES) throw new Error(FIXTURE_NOTICE);
  const res = await fetch(`/v1/model-imports/${encodeURIComponent(batchId)}/submit`, {
    method: "POST",
    signal,
    headers: { Accept: "application/json", ...identityHeaders() },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as ModelImportBatchRead;
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
  const res = await fetch("/v1/descriptions/withdrawals", {
    method: "POST",
    signal,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...identityHeaders(),
    },
    credentials: "same-origin",
    body: JSON.stringify({
      subject_type: subjectType,
      subject_id: subjectId,
      reason,
      request_type: requestType,
    }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as DescriptionWithdrawalRead;
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
  const res = await fetch(
    `/v1/model-imports/${encodeURIComponent(batchId)}/changes/exclusion`,
    {
      method: "POST",
      signal,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...identityHeaders(),
      },
      credentials: "same-origin",
      body: JSON.stringify({ change_ids: changeIds, excluded }),
    },
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as ModelImportBatchRead;
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
