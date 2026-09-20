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

   SOURCE BUNDLES (R11-OKF02). The same five reads against one datasource's
   bundle of discovered, authorized objects, under
   `/v1/datasources/{id}/okf-bundle`. The server takes the datasource's own
   read decision on every request; a refusal is a 403, and the view says so
   rather than showing an empty bundle.

   THE CATALOG OBJECT READ ALSO CARRIES A SOURCE ENTRY (R11-OKF02). When no
   product bundle holds a table, `GET /v1/metadata/tables/{id}/okf-knowledge`
   answers from the table's own datasource bundle, resolved on the server: a
   document's path is a digest of catalog, schema and name, which no table read
   exposes, so a client cannot form it and does not try. The answer is one of
   three states -- see `ObjectKnowledgeSource` -- and a refusal is never an
   absence.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";
import { ApiError, requestBlob } from "../http";
import type {
  OkfBundleRead,
  OkfContextRead,
  OkfContextRequest,
  OkfDocumentRead,
  OkfObjectKnowledgeRead,
  OkfPublicationHistoryRead,
  OkfPublicationRead,
} from "../types";

/** `GET /v1/datasources/{id}/okf-bundle/publications`. Declared here, beside the
 *  read that returns it, until `lib/types.ts` is next regenerated from the API. */
export type OkfSourcePublicationHistoryRead = {
  datasource_id: string;
  items: OkfPublicationRead[];
};

/** `POST /v1/datasources/{id}/okf-bundle/context`: the product selection's
 *  fields, with the datasource in place of the product. */
export type OkfSourceContextRead = Omit<
  OkfContextRead,
  "context_product_version_id" | "product_key" | "product_version"
> & {
  datasource_id: string;
  datasource_name: string;
};

/** The sentence the Sources screen says beside "Open knowledge bundle", said again wherever a
 *  source bundle's content is shown, so the two surfaces make one claim in one wording.
 *  `SourcesScreen.tsx` holds the original; a test compares the two. */
export const SOURCE_BUNDLE_READABLE_ONLY =
  "Only what you may read of this source is in it, and nothing else is counted.";

/** `OkfObjectSourceRead` on `GET /v1/metadata/tables/{id}/okf-knowledge`: what the object's own
 *  datasource bundle holds, offered only when no product bundle does. Declared here, beside the
 *  read that returns it, until `lib/types.ts` is next regenerated from the API. Each state
 *  carries only what it may -- a refusal names no bundle, an absence counts nothing -- so the
 *  three are told apart by `state` alone.
 *
 *  DOCUMENT       the object's document from the caller's own publication, with its coverage.
 *  NOT_IN_BUNDLE  the bundle the caller may read holds no document for it. An object that was
 *                 never discovered, is retired, or sits in a schema the caller's workspace
 *                 refuses all read alike.
 *  REFUSED        the datasource's read decision refused the caller; `reason` is the bare code. */
export type ObjectKnowledgeSource =
  | {
      state: "DOCUMENT";
      datasource_id: string;
      datasource_name: string;
      publication: OkfPublicationRead;
      document: OkfDocumentRead;
      coverage: Record<string, unknown>;
    }
  | { state: "NOT_IN_BUNDLE"; datasource_id: string; datasource_name: string }
  | { state: "REFUSED"; reason: string };

/** `OkfObjectKnowledgeRead` with its `source` entry: null (or absent, from a server that predates
 *  it) whenever a product bundle holds the object. */
export type ObjectKnowledgeRead = Omit<OkfObjectKnowledgeRead, "source"> & {
  source?: ObjectKnowledgeSource | null;
};

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

/** `POST .../okf-bundle/context` -- the sections of the stored bundle a question
 *  needs, with receipts: exactly what an agent asking through MCP or Ask is
 *  handed. A POST so the question travels in the body, never in a URL. */
export function selectOkfContext(
  versionId: string,
  body: OkfContextRequest,
  signal?: AbortSignal,
): Promise<OkfContextRead> {
  return demoOr(
    () => demoUnavailable<OkfContextRead>(),
    () => postJson<OkfContextRead>(`${versionPath(versionId)}/context`, body, signal),
  );
}

/** `GET /v1/metadata/tables/{id}/okf-knowledge` -- this object's document from
 *  every product bundle the caller may read and, when none holds it, the answer of
 *  its own datasource's bundle (`source`). One request: the fallback is the
 *  server's, so this never asks a source route for a path it cannot form.
 *  Demo mode: none. */
export function fetchObjectKnowledge(
  tableId: string,
  signal?: AbortSignal,
): Promise<ObjectKnowledgeRead> {
  return demoOr<ObjectKnowledgeRead>(
    async () => ({ table_id: tableId, items: [] }),
    () =>
      get<ObjectKnowledgeRead>(
        `/v1/metadata/tables/${encodeURIComponent(tableId)}/okf-knowledge`,
        signal,
      ),
  );
}

/** `GET .../okf-bundle/download?publication_id=` -- the archive of exactly the
 *  publication on screen. The server refuses a bundle that fails its publish
 *  policy; that refusal surfaces as the thrown `ApiError`. */
export async function downloadOkfBundle(versionId: string, publicationId: string): Promise<void> {
  await saveArchive(versionPath(versionId), publicationId);
}

function sourcePath(datasourceId: string): string {
  return `/v1/datasources/${encodeURIComponent(datasourceId)}/okf-bundle`;
}

/** `GET /v1/datasources/{id}/okf-bundle` -- one datasource's stored source
 *  bundle: manifest, file index and publication, the product manifest's shape. */
export function fetchSourceOkfBundle(datasourceId: string, signal?: AbortSignal): Promise<OkfBundleRead> {
  return demoOr(
    () => demoUnavailable<OkfBundleRead>(),
    () => get<OkfBundleRead>(sourcePath(datasourceId), signal),
  );
}

/** `GET /v1/datasources/{id}/okf-bundle/document?path=` -- pinned like a product's. */
export function fetchSourceOkfDocument(
  datasourceId: string,
  path: string,
  publicationId: string | null,
  signal?: AbortSignal,
): Promise<OkfDocumentRead> {
  const params = new URLSearchParams({ path });
  if (publicationId) params.set("publication_id", publicationId);
  return demoOr(
    () => demoUnavailable<OkfDocumentRead>(),
    () => get<OkfDocumentRead>(`${sourcePath(datasourceId)}/document?${params.toString()}`, signal),
  );
}

/** `GET /v1/datasources/{id}/okf-bundle/publications` -- the reader's own lineage. */
export function fetchSourceOkfPublications(
  datasourceId: string,
  signal?: AbortSignal,
): Promise<OkfSourcePublicationHistoryRead> {
  return demoOr(
    () => demoUnavailable<OkfSourcePublicationHistoryRead>(),
    () => get<OkfSourcePublicationHistoryRead>(`${sourcePath(datasourceId)}/publications`, signal),
  );
}

/** `POST /v1/datasources/{id}/okf-bundle/context` -- the question in the body. */
export function selectSourceOkfContext(
  datasourceId: string,
  body: OkfContextRequest,
  signal?: AbortSignal,
): Promise<OkfSourceContextRead> {
  return demoOr(
    () => demoUnavailable<OkfSourceContextRead>(),
    () => postJson<OkfSourceContextRead>(`${sourcePath(datasourceId)}/context`, body, signal),
  );
}

/** `GET /v1/datasources/{id}/okf-bundle/download?publication_id=` -- exactly the
 *  publication on screen, refused by the server if it fails the publish policy. */
export async function downloadSourceOkfBundle(datasourceId: string, publicationId: string): Promise<void> {
  await saveArchive(sourcePath(datasourceId), publicationId);
}

async function saveArchive(base: string, publicationId: string): Promise<void> {
  const { blob, response } = await requestBlob(
    `${base}/download?publication_id=${encodeURIComponent(publicationId)}`,
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
    if (error.status === 404) return "No knowledge bundle you may read exists here.";
    if (error.status === 409) return `The bundle could not be published: ${error.detail}`;
    if (error.status === 401 || error.status === 403) return "You are not permitted to read this bundle.";
    return error.detail || `The bundle could not be read (HTTP ${error.status}).`;
  }
  return (error as Error)?.message || "The bundle could not be read.";
}
