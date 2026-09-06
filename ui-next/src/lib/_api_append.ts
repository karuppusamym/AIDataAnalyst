/* Moved to `./api/glossary.ts` (review 2026-09-05, R05): glossary terms and asset-term links.
 *
 * The name was always temporary -- this was an append file, not a
 * boundary. The path stays as a re-export because components and their
 * tests import it directly; delete it when those callers move to
 * `../lib/api`. */
export * from "./api/glossary";
