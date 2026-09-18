/* ---------------------------------------------------------------------------
   Knowledge (R11-OKF02) -- the stored OKF bundle of a context product version,
   read the way every other surface reads it.

   Four reads and one download, all against `okf_export_api.py`, all served
   from the *stored* publication the caller's authority resolves to
   (`okf_store.read_published_bundle`). The client never assembles knowledge of
   its own: which documents exist, what changed and whether a bundle may be
   downloaded are the server's answers, and a document is shown exactly as the
   bytes a downloaded archive would contain.

   A document read or a download may be PINNED to a publication id. The
   knowledge view pins every read after the manifest to the publication that
   manifest described, so a rebuild that lands while someone is reading cannot
   splice a newer document into the older bundle they are looking at.

   Not re-exported from `lib/api.ts`: its consumers import it by path, as
   `./definitionHistory` is imported. Demo mode answers with nothing rather
   than a fabricated bundle -- a made-up knowledge document is the one fixture
   that would be read as governed meaning.
--------------------------------------------------------------------------- */

import { demoOr, get } from "./transport";
import { ApiError, requestBlob } from "../http";
import type {
  OkfBundleRead,
  OkfDocumentRead,
  OkfObjectKnowledgeRead,
  OkfPublicationHistoryRead,
} from "../types";

const DEMO_REFUSAL = "Knowledge bundles are read from a live Atlas deployment; demo data has none.";

function demoUnavailable<T>(): Promise<T> {
  return Promise.reject(new Error(DEMO_REFUSAL));
}

function versionPath(versionId: string): string {
  return `/v1/context-product-versions/${encodeURIComponent(versionId)}/okf-bundle`;
}

/** `GET .../okf-bundle` -- the stored manifest, file index and publication. */
export function fetchOkfBundle(versionId: string, signal?: AbortSignal): Promise<OkfBundleRead> {
  return demoOr(
    () => demoUnavailable<OkfBundleRead>(),
    () => get<OkfBundleRead>(versionPath(versionId), signal),
  );
}

/** `GET .../okf-bundle/document?path=` -- one stored document's exact bytes. */
export function fetchOkfDocument(
  versionId: string,
  path: string,
  publicationId: string | null,
  signal?: AbortSignal,
): Promise<OkfDocumentRead> {
  const params = new URLSearchParams({ path });
  if (publicationId) params.set("publication_id", publicationId);
  return demoOr(
    () => demoUnavailable<OkfDocumentRead>(),
    () => get<OkfDocumentRead>(`${versionPath(versionId)}/document?${params.toString()}`, signal),
  );
}

/** `GET .../okf-bundle/publications` -- the caller's own lineage, newest first. */
export function fetchOkfPublications(
  versionId: string,
  signal?: AbortSignal,
): Promise<OkfPublicationHistoryRead> {
  return demoOr(
    () => demoUnavailable<OkfPublicationHistoryRead>(),
    () => get<OkfPublicationHistoryRead>(`${versionPath(versionId)}/publications`, signal),
  );
}

/** `GET /v1/metadata/tables/{id}/okf-knowledge` -- this object's document from
 *  every product bundle the caller may read. Demo mode: none. */
export function fetchObjectKnowledge(
  tableId: string,
  signal?: AbortSignal,
): Promise<OkfObjectKnowledgeRead> {
  return demoOr(
    async () => ({ table_id: tableId, items: [] }),
    () =>
      get<OkfObjectKnowledgeRead>(
        `/v1/metadata/tables/${encodeURIComponent(tableId)}/okf-knowledge`,
        signal,
      ),
  );
}

/** `GET .../okf-bundle/download?publication_id=` -- the archive of exactly the
 *  publication on screen. The server refuses a bundle that fails its publish
 *  policy; that refusal surfaces as the thrown `ApiError`. */
export async function downloadOkfBundle(versionId: string, publicationId: string): Promise<void> {
  const { blob, response } = await requestBlob(
    `${versionPath(versionId)}/download?publication_id=${encodeURIComponent(publicationId)}`,
  );
  const disposition = response.headers.get("Content-Disposition") || "";
  const filename = disposition.match(/filename="([^"]+)"/)?.[1] || `okf-bundle-${publicationId}.zip`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Refusals worth a sentence of their own. A 409 on a read is a bundle that
 *  could not be published (sources changing under the capture, a scope over
 *  the limits); a 404 is "nothing you may read" and never a hint that
 *  something exists. */
export function describeKnowledgeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return "No knowledge bundle you may read exists for this version.";
    if (error.status === 409) return `The bundle could not be published: ${error.detail}`;
    if (error.status === 401 || error.status === 403) return "You are not permitted to read this bundle.";
    return error.detail || `The bundle could not be read (HTTP ${error.status}).`;
  }
  return (error as Error)?.message || "The bundle could not be read.";
}
