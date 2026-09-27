/* ---------------------------------------------------------------------------
   Data dictionaries -- N8 document ingestion (`src/aida/document_ingestion_api.py`).

   A CSV data dictionary is uploaded to a delivery project, its rows are matched
   to that project's catalog tables and columns by exact name, and each matched
   row becomes its own `DOCUMENT_CLAIM` review. Approving one publishes the row's
   text as the column's (or table's) description. Nothing is published on upload.

   Transport, identity headers and the demo switch come from `./transport`.
   Re-exported from `lib/api.ts`.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import {
  fixtureDocumentClaims,
  fixtureDocumentMappings,
  fixtureDocumentSections,
  fixtureExtractDocumentClaims,
  fixtureMapDocument,
  fixtureProjectDocuments,
  fixtureUploadDataDictionary,
} from "../documentFixtures";
import type { DocumentCreate, DocumentMappingSummaryRead, DocumentRead } from "../types";
import type {
  DocumentClaimRead,
  DocumentMappingRead,
  DocumentSectionRead,
  PageOf,
} from "../ui-types";

/** The server's limits (`document_ingestion.py`): a larger upload is refused
 *  with 413, and rows past the cap are dropped. */
export const DATA_DICTIONARY_MAX_BYTES = 1_000_000;
export const DATA_DICTIONARY_MAX_ROWS = 5_000;
/** The list routes' own `limit` ceiling. */
export const DOCUMENT_PAGE_LIMIT = 500;

/** `GET /v1/projects/{project_id}/documents`, newest first. */
export function fetchProjectDocuments(
  projectId: string,
  signal?: AbortSignal,
): Promise<PageOf<DocumentRead>> {
  return demoOr(
    () => fixtureProjectDocuments(projectId),
    () =>
      get<PageOf<DocumentRead>>(
        `/v1/projects/${projectId}/documents?limit=${DOCUMENT_PAGE_LIMIT}`,
        signal,
      ),
  );
}

/** `POST /v1/projects/{project_id}/documents` -- upload and parse in one step
 *  (`create_document_from_csv`). The response counts the rows kept and the
 *  rows dropped for having no table or no description. */
export function uploadDataDictionary(
  projectId: string,
  body: DocumentCreate,
  signal?: AbortSignal,
): Promise<DocumentRead> {
  return demoOr(
    () => fixtureUploadDataDictionary(projectId, body),
    () => postJson<DocumentRead>(`/v1/projects/${projectId}/documents`, body, signal),
  );
}

/** `GET /v1/documents/{document_id}/sections` -- the kept rows, in file order. */
export function fetchDocumentSections(
  documentId: string,
  signal?: AbortSignal,
): Promise<PageOf<DocumentSectionRead>> {
  return demoOr(
    () => fixtureDocumentSections(documentId),
    () =>
      get<PageOf<DocumentSectionRead>>(
        `/v1/documents/${documentId}/sections?limit=${DOCUMENT_PAGE_LIMIT}`,
        signal,
      ),
  );
}

/** `POST /v1/documents/{document_id}/map` -- exact-name matching against the
 *  project's ACTIVE catalog (`resolve_structural_mappings`). A row that
 *  matches nothing, or more than one table, stays UNMATCHED. 409 unless the
 *  document is PARSED. */
export function mapDocument(
  documentId: string,
  signal?: AbortSignal,
): Promise<DocumentMappingSummaryRead> {
  return demoOr(
    () => fixtureMapDocument(documentId),
    () => postJson<DocumentMappingSummaryRead>(`/v1/documents/${documentId}/map`, {}, signal),
  );
}

/** `GET /v1/documents/{document_id}/mappings` -- one per row once matched. */
export function fetchDocumentMappings(
  documentId: string,
  signal?: AbortSignal,
): Promise<PageOf<DocumentMappingRead>> {
  return demoOr(
    () => fixtureDocumentMappings(documentId),
    () =>
      get<PageOf<DocumentMappingRead>>(
        `/v1/documents/${documentId}/mappings?limit=${DOCUMENT_PAGE_LIMIT}`,
        signal,
      ),
  );
}

/** `POST /v1/documents/{document_id}/extract-claims` -- one claim and one
 *  review per matched row. 409 unless the document is MAPPED, and 409 once its
 *  rows have been proposed, so a second press cannot raise every review twice. */
export function extractDocumentClaims(
  documentId: string,
  signal?: AbortSignal,
): Promise<PageOf<DocumentClaimRead>> {
  return demoOr(
    () => fixtureExtractDocumentClaims(documentId),
    () =>
      postJson<PageOf<DocumentClaimRead>>(
        `/v1/documents/${documentId}/extract-claims`,
        {},
        signal,
      ),
  );
}

/** `GET /v1/documents/{document_id}/claims` -- each with its review's id and
 *  its state: PENDING until a reviewer decides it in the review queue. */
export function fetchDocumentClaims(
  documentId: string,
  signal?: AbortSignal,
): Promise<PageOf<DocumentClaimRead>> {
  return demoOr(
    () => fixtureDocumentClaims(documentId),
    () =>
      get<PageOf<DocumentClaimRead>>(
        `/v1/documents/${documentId}/claims?limit=${DOCUMENT_PAGE_LIMIT}`,
        signal,
      ),
  );
}
