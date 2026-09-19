/* ---------------------------------------------------------------------------
   SQL review workspace (R11-SQL01) — SQL a person reads before it runs.

   Two routes (`aida/sql_workspace_api.py`), and nothing executes on the first:
   `createSqlDraft` drafts SQL from a question, or takes a pasted statement,
   and validates it without running it; a valid statement comes back with a
   receipt. `runSqlDraft` sends the exact statement back with that receipt and
   runs it once. An edited statement, another row limit or another product is
   refused as `REVALIDATION_REQUIRED` — the server checks, and the screen
   disables Run on an edit so the person is not surprised by the refusal.

   Fixture mode refuses rather than pretending: a review step whose "Run" ran
   nothing would teach the wrong thing about what Run means.
--------------------------------------------------------------------------- */

import { get, postJson } from "./transport";
import { USE_FIXTURES } from "../appConfig";
import { ApiError } from "../http";
import type {
  SqlDraftReceiptRead,
  SqlDraftRequest,
  SqlDraftResponse,
  SqlDraftRunRequest,
  SqlDraftRunResponse,
} from "../types";

const FIXTURE_REFUSAL = "The SQL workspace is unavailable in fixture mode — run against the API.";

/** `POST /v1/datasources/{id}/sql-drafts` — draft or accept SQL; validate; never execute. */
export function createSqlDraft(
  datasourceId: string,
  body: SqlDraftRequest,
  signal?: AbortSignal,
): Promise<SqlDraftResponse> {
  if (USE_FIXTURES) return Promise.reject(new Error(FIXTURE_REFUSAL));
  return postJson<SqlDraftResponse>(`/v1/datasources/${datasourceId}/sql-drafts`, body, signal);
}

/** `POST /v1/sql-drafts/{receipt_id}/run` — run the validated statement, once. */
export function runSqlDraft(
  receiptId: string,
  body: SqlDraftRunRequest,
  signal?: AbortSignal,
): Promise<SqlDraftRunResponse> {
  if (USE_FIXTURES) return Promise.reject(new Error(FIXTURE_REFUSAL));
  return postJson<SqlDraftRunResponse>(`/v1/sql-drafts/${receiptId}/run`, body, signal);
}

/** `GET /v1/datasources/{id}/sql-drafts` -- the caller's own recent receipts here, newest
 *  first. Value-free: the redacted shape, status and execution, never a literal or a row. */
export function listSqlDrafts(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<SqlDraftReceiptRead[]> {
  if (USE_FIXTURES) return Promise.resolve([]);
  return get<SqlDraftReceiptRead[]>(`/v1/datasources/${datasourceId}/sql-drafts`, signal);
}

/** Why a Run was refused, in the words a person can act on. Keyed by the server's stable
 *  `detail.code`; anything else falls back to the server's own message. */
export type SqlRunRefusalCode =
  | "REVALIDATION_REQUIRED"
  | "RECEIPT_EXPIRED"
  | "RECEIPT_ALREADY_USED"
  | "RECEIPT_NOT_YOURS"
  | "RECEIPT_NOT_FOUND";

export interface SqlWorkspaceProblem {
  title: string;
  detail: string;
  /** True when validating again is the remedy, so the screen can offer it. */
  revalidate: boolean;
}

const RUN_REFUSALS: Readonly<Record<SqlRunRefusalCode, SqlWorkspaceProblem>> = {
  REVALIDATION_REQUIRED: {
    title: "Changed since it was validated",
    detail:
      "The statement, its row limit or the context product is not the one that was validated. " +
      "Validate it again to run it.",
    revalidate: true,
  },
  RECEIPT_EXPIRED: {
    title: "The validation expired",
    detail: "Validations are short-lived so access is checked close to the run. Validate again.",
    revalidate: true,
  },
  RECEIPT_ALREADY_USED: {
    title: "Already run",
    detail: "Each validation runs once. Validate again to run the statement a second time.",
    revalidate: true,
  },
  RECEIPT_NOT_YOURS: {
    title: "Validated by someone else",
    detail: "Only the person who validated a statement can run it. Validate it yourself.",
    revalidate: true,
  },
  RECEIPT_NOT_FOUND: {
    title: "Validation not found",
    detail: "Validate the statement again.",
    revalidate: true,
  },
};

/** Map a failed workspace request onto what the person should read and do. */
export function describeSqlWorkspaceError(error: unknown): SqlWorkspaceProblem {
  if (!(error instanceof ApiError)) {
    return {
      title: "The request did not complete",
      detail: error instanceof Error ? error.message : String(error),
      revalidate: false,
    };
  }
  const code = typeof error.details?.code === "string" ? error.details.code : null;
  if (code !== null && code in RUN_REFUSALS) return RUN_REFUSALS[code as SqlRunRefusalCode];
  if (error.status === 403) {
    return {
      title: "Not permitted",
      detail: `The platform refused this for your access, not for the SQL (${error.detail}).`,
      revalidate: false,
    };
  }
  if (error.status === 404) {
    return { title: "Not found", detail: error.detail, revalidate: false };
  }
  if (error.status === 422) {
    return { title: "Refused", detail: error.detail, revalidate: false };
  }
  return { title: "The request failed", detail: error.detail, revalidate: false };
}
