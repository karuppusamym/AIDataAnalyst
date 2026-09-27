import type { StudioChangeItemCreate } from "../lib/types";

/* ---------------------------------------------------------------------------
   What a Studio form knows about the shape of a change item (R11-AUD08).

   A change item is an OBJECT (type + id), an OPERATION, and up to two free-form
   snapshots -- the API stores `before_snapshot` and `after_snapshot` as
   arbitrary JSON objects and validates nothing about them when the item is
   ADDED. What a snapshot must hold is decided later, when the item is TESTED
   (`studio_test_harness.py`, one validator per object type). So a form that only
   showed the server's 422 would let an author add a metric with no `grain` and
   find out at "Run tests", after the change set had already moved to TESTING and
   locked its items. The hints below say, per type, what the test will ask for,
   read from those validators; the two "Check" actions (TOOL and CONTEXT_PRODUCT,
   the two types with a stateless validator endpoint) run the real check before
   the item is added.

   The hints are advice, not validation: nothing here blocks an item the server
   would accept, and the server's own words are what a refusal shows.
--------------------------------------------------------------------------- */

export const OBJECT_TYPES = ["METRIC", "TOOL", "TERM", "CONTEXT_PRODUCT"] as const;
export const OPERATIONS = ["CREATE", "UPDATE", "DELETE"] as const;

export type StudioObjectType = StudioChangeItemCreate["object_type"];
export type StudioOperation = StudioChangeItemCreate["operation"];

/** What "Run tests" asks of an item's snapshot, per object type (`studio_test_harness.py`). */
export const SNAPSHOT_HINTS: Readonly<Record<StudioObjectType, string>> = {
  METRIC:
    "A test needs the after snapshot to carry name, aggregation (SUM, COUNT, AVG, MIN or MAX) and grain.",
  TOOL:
    "A test needs name, sql_template and allowed_roles in the after snapshot, and checks its parameters " +
    "(a list) against the SQL template's placeholders. Check runs that contract check now.",
  TERM: "A test needs display_name and a definition of at least 10 characters in the after snapshot.",
  CONTEXT_PRODUCT:
    "CREATE: the object id must equal after_snapshot.product_key, and the snapshot needs a project_id and " +
    "every context product field. UPDATE and DELETE: the object id is the existing product's UUID. " +
    "Check runs that shape check now.",
};

export type ParsedObject =
  | { readonly ok: true; readonly value: Record<string, unknown> | null }
  | { readonly ok: false; readonly error: string };

/**
 * A snapshot or state field's text, as the JSON object the API takes.
 *
 * Empty is `null` (the field is optional). A syntax error is the CLIENT's to
 * report -- the API never sees the text -- so it names the field and carries the
 * parser's own message. A value that parses but is not an object is refused here
 * too: every one of these fields is a `dict` on the server, and a bare array or
 * number would come back as a 422 about a type the author never meant to choose.
 */
export function parseJsonObject(text: string, what: string): ParsedObject {
  if (text.trim() === "") return { ok: true, value: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (failure) {
    return { ok: false, error: `${what} is not valid JSON: ${(failure as Error).message}` };
  }
  if (parsed === null) return { ok: true, value: null };
  if (typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: `${what} must be a JSON object, like { "field": "value" }.` };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/** A value from an API response, as one line of text: strings as they are, everything else as JSON. */
export const showValue = (value: unknown): string =>
  typeof value === "string" ? value : (JSON.stringify(value) ?? String(value));

/** An API timestamp as "2026-09-02 10:00:00 UTC". Every Studio time is a UTC instant the server
 *  stamped (`datetime.now(UTC)`), so the zone is stated instead of left for a reader to guess. */
export const showStamp = (iso: string | null): string =>
  iso ? `${iso.slice(0, 19).replace("T", " ")} UTC` : "not finished";
