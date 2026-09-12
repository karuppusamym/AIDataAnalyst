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

import { identityHeaders as configuredIdentityHeaders } from "../appConfig";
import { authorizationHeaders } from "../authSession";
import { decodeError, request, setHeaderProvider } from "../http";
import { getCurrentOrgId } from "../org-context";

export { ApiError, requestBlob } from "../http";
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
 * Everything `lib/fixtures.ts` exports, as a type only.
 *
 * `typeof import(...)` is erased at compile time, so naming the demo estate
 * here costs the bundle nothing: the demo arms below are still checked against
 * the real generators' signatures without a single module in this client
 * holding a static import of them.
 */
export type DemoData = typeof import("../fixtures");

/**
 * The demo-data adapter (R05), and the client's one door to the fixtures
 * (R11-X1).
 *
 * `if (USE_FIXTURES) return makeFixtureX(); return get(...)` was repeated in
 * every function in the client. Stated once, the rule is: in demo mode a
 * function answers from the bundled fixture and issues no request. The branch
 * is the same either way -- this just names it, so a reader sees the demo
 * answer and the live call as two halves of one decision instead of an `if`
 * they have to re-read in every function.
 *
 * WHY THE DEMO ARM IS HANDED ITS FIXTURES rather than closing over them:
 * `lib/fixtures.ts` is ~189 kB of minified demo estate -- a fifth of all the
 * JavaScript this client used to ship -- and it reached production users
 * because fifteen API modules imported the generators statically, every one of
 * them a module a live build loads. A static import is unconditional
 * -- there is no build in which the bundler may drop it. Routing every demo
 * answer through one dynamic `import()` here leaves exactly one reference to
 * the module in the whole client, in one branch, so:
 *
 *   - a production build folds the condition below to `live()` and Rollup
 *     drops `lib/fixtures.ts` from the graph entirely; and
 *   - a demo build emits it as its own chunk, fetched the first time a screen
 *     actually asks for data rather than preloaded with the shell.
 *
 * The condition reads `import.meta.env` directly instead of `appConfig`'s
 * `USE_FIXTURES`, which holds the identical comparison. That is deliberate and
 * load-bearing: `appConfig` lands in a different chunk, and Rollup cannot fold
 * a constant it has to reach across a chunk boundary to read -- an imported
 * flag would leave this `import()` live and the fixture chunk shipped. The one
 * source of truth is the literal `vite.config.ts` defines; both sites read it.
 *
 * A handful of functions keep an inline branch, and each is one where the two
 * arms are not the same shape: the demo arm refuses (a write with no fixture
 * to return), or the branch sits mid-function after work both arms share.
 * Forcing those into this adapter would restructure the function rather than
 * name its decision. Those branches answer from `USE_FIXTURES` and touch no
 * fixture, so they cost a live build nothing.
 */
export function demoOr<T>(
  demo: (fixtures: DemoData) => Promise<T>,
  live: () => Promise<T>,
): Promise<T> {
  return import.meta.env.VITE_USE_FIXTURES === "0"
    ? live()
    : import("../fixtures").then(demo);
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

/**
 * The one raw-byte upload in this client.
 *
 * `request` JSON-stringifies its body, so a `File` cannot go through it: an
 * octet-stream upload has to hand `fetch` the `File` itself. That is a real
 * gap in `http.ts`, not a licence for a second transport -- so the exception
 * lives here, once, and uses the same header provider and the same
 * `decodeError` as every other verb. `./columnDocumentation.ts` hand-rolled
 * five `fetch` calls, of which this is the only one that has to: three were
 * plain JSON POSTs and one was a download `requestBlob` already covers. Each
 * carried a private decoder that understood only `{"detail": ...}`, so the
 * error code, correlation id and 422 field errors F14 preserves were dropped
 * on all five.
 *
 * KNOWN GAP, deliberately not hidden: an outcome here does not reach
 * `observeRequests`, because `http.ts` does not export its notifier. So a
 * failed workbook upload is invisible to the shell's connection state (F13).
 * Closing it means a raw-body verb in `http.ts`; this module cannot fix it
 * from the outside.
 */
export async function requestRawBody<T>(
  method: "POST" | "PUT",
  path: string,
  body: BodyInit,
  contentType: string,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(path, {
    method,
    signal,
    body,
    headers: { Accept: "application/json", "Content-Type": contentType, ...requestHeaders() },
    credentials: "same-origin",
  });
  if (!res.ok) throw await decodeError(res);
  return (await res.json()) as T;
}
