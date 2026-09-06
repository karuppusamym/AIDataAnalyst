/* ---------------------------------------------------------------------------
   The API client's transport seam (review 2026-09-05, R05 · F06/F14).

   `api.ts` was 4,576 lines: one file holding the transport, the identity
   headers, the fixture switch, and every endpoint in the product. Three more
   modules with temporary-looking names -- `_api_append`, `_cross_source_api`,
   `_column_documentation_api` -- had each COPIED a transport of their own
   rather than import from it, because these helpers were module-private.

   That copying was not a style problem. Each copy:

     - built its own `identityHeaders()` that sent `X-Principal-Id` and
       `X-Roles` in EVERY live mode, so the F06 fix (development headers only
       in development mode) did not reach glossary, cross-source or column
       documentation calls at all;
     - understood only `{"detail": ...}`, so the correlation id, error code
       and field errors that F14 preserved were discarded on those paths;
     - imported the org id from `./org` -- the React provider -- reinstating
       the `api -> org -> api` cycle that was removed from `api.ts`.

   THE INVARIANT: there is exactly one transport, one header provider, and one
   error decoder in this client. A domain module imports the verbs from here;
   it never calls `fetch` and never assembles an identity header.

   `api.ts` remains the public barrel: every screen still imports from
   `../lib/api`, and every name it used to export it still exports.
--------------------------------------------------------------------------- */

import { identityHeaders as configuredIdentityHeaders, USE_FIXTURES } from "../appConfig";
import { authorizationHeaders } from "../authSession";
import { request, setHeaderProvider } from "../http";
import { getCurrentOrgId } from "../org-context";

export { ApiError } from "../http";
export type { FieldError } from "../http";
export { USE_FIXTURES } from "../appConfig";

/* The one place a request learns who is asking (review 2026-09-05, F06).
 *
 * `identityHeaders` sends the development principal in development mode and
 * NOTHING identifying in the other two; `authorizationHeaders` adds the
 * bearer token in OIDC mode when one has been obtained, and nothing when it
 * has not. The two are disjoint by construction, so no mode can end up
 * asserting an identity twice, and no missing token can silently fall back to
 * the all-role development principal.
 *
 * Exported as well as installed, because a few endpoints exchange raw bytes
 * (an .xlsx export, an octet-stream upload) and cannot go through the JSON
 * verbs. They still have to identify themselves the same way, so they call
 * this rather than assembling headers of their own -- which is the mistake
 * this module exists to undo. */
export function requestHeaders(): Record<string, string> {
  return {
    ...configuredIdentityHeaders(getCurrentOrgId()),
    ...authorizationHeaders(),
  };
}

setHeaderProvider(requestHeaders);

export function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>("GET", path, { signal });
}

export function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>("POST", path, { body, signal });
}

export function putJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>("PUT", path, { body, signal });
}

export function patchJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>("PATCH", path, { body, signal });
}

/** For endpoints that delete a resource with no response body (204). */
export async function deleteRequest(path: string, signal?: AbortSignal): Promise<void> {
  await request<void>("DELETE", path, { signal });
}

/**
 * The demo-data adapter (R05).
 *
 * `if (USE_FIXTURES) return makeFixtureX(); return get(...)` is repeated in
 * several hundred functions in `api.ts`. Stated once, the rule is: in demo
 * mode a function answers from the bundled fixture and issues no request. The
 * branch is the same either way -- this just names it, so a reader sees the
 * demo answer and the live call as two halves of one decision instead of an
 * `if` they have to re-read in every function.
 *
 * Migration is deliberately incremental: the modules extracted from `api.ts`
 * use it, the ones still inside it keep their inline branch until they move.
 * Rewriting five hundred call sites in a correctness pass would be a large
 * untested diff to fix a readability problem.
 */
export function demoOr<T>(demo: () => Promise<T>, live: () => Promise<T>): Promise<T> {
  return USE_FIXTURES ? demo() : live();
}

/**
 * A request adapter for domain modules moved off a private `fetch` copy.
 *
 * Those call sites pass `RequestInit` with an already-stringified body, which
 * predates the shared transport. Parsing it back here keeps the move purely
 * mechanical and the bytes on the wire identical; converting those call sites
 * to pass objects is a separate, behaviour-neutral change.
 */
export function requestWithInit<T>(
  path: string,
  init: RequestInit = {},
  signal?: AbortSignal,
): Promise<T> {
  const method = (init.method ?? "GET") as "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  const body =
    init.body === undefined || init.body === null
      ? undefined
      : (JSON.parse(String(init.body)) as unknown);
  // Per-request headers carry meaning at these call sites -- `X-Link-Reason`
  // is the reason a glossary link was made, and it is audited. Dropping them
  // would send a request the server accepts and cannot attribute.
  const headers = init.headers as Record<string, string> | undefined;
  return request<T>(method, path, { body, signal, headers });
}
