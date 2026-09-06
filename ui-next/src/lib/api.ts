/* ---------------------------------------------------------------------------
   The API client's public barrel (review 2026-09-05, R05).

   THE INVARIANT: there is exactly one transport, one header provider and one
   error decoder in this client, in `./api/transport.ts` over `./http.ts`. A
   domain module imports the verbs from there; it never calls `fetch`, never
   assembles an identity header and never decodes an error of its own.

   That is the defect this file's shape exists to prevent, not a tidiness
   preference. When the endpoints lived in one 4,576-line module the helpers
   were module-private, so three sibling modules COPIED a transport instead of
   importing one. Each copy sent the development principal in every live mode
   (defeating F06's development-only header rule), understood only
   `{"detail": ...}` (discarding the correlation id, error code and field
   errors F14 preserves), and imported the org id from the React provider,
   reinstating the `api -> org -> api` cycle. A duplicated transport is where
   those bugs live; one import boundary per domain is what stops the next one
   being written.

   This file is the client's public surface and nothing else. Every screen and
   test imports from `../lib/api`, and every name this module exported before
   the split it still exports, with the same signature -- the split is a move,
   not a redesign. Add an endpoint to its domain module, not here.
--------------------------------------------------------------------------- */

export { ApiError } from "./http";
export type { FieldError } from "./http";
export { USE_FIXTURES } from "./appConfig";
export { deleteRequest, demoOr, get, patchJson, postJson, putJson } from "./api/transport";

export * from "./api/identity";
export * from "./api/glossary";
export * from "./api/crossSource";
export * from "./api/columnDocumentation";

export * from "./api/catalog";
export * from "./api/governance";
export * from "./api/quality";
export * from "./api/agents";
export * from "./api/products";
export * from "./api/semantics";
export * from "./api/studio";
export * from "./api/operations";
export * from "./api/lineage";
export * from "./api/sources";
export * from "./api/administration";
export * from "./api/tools";
export * from "./api/transformations";
