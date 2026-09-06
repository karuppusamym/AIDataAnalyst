/* Moved to `./api/crossSource.ts` (review 2026-09-05, R05): cross-source identity, domains and boundary grants.
 *
 * The name was always temporary -- this was an append file, not a
 * boundary. The path stays as a re-export because components and their
 * tests import it directly; delete it when those callers move to
 * `../lib/api`. */
export * from "./api/crossSource";
