/* ---------------------------------------------------------------------------
   Who may do what on the Studio screen (R11-AUD08).

   Two lists, each copied from the rows of
   `Docs/50-security/surface-control-matrix.md` it guards -- the matrix is
   generated from the application's own `require_roles` dependencies, so a list
   that drifts from it is a list that is wrong. They live in one small module
   instead of beside each request only because six components on this screen
   ask the same two questions; each caller still names, next to its own use,
   which of the two it is applying and why.

   THE SERVER IS THE AUTHORITY. These decide what is worth ASKING for (a load-time
   read, via `readDecision`) and what is worth OFFERING (a control, via
   `roleHolds`, which fails closed while `/v1/me` is in flight). A refusal that
   still gets through is shown in the server's own words.
--------------------------------------------------------------------------- */

/**
 * The roles every Studio READ admits, and the two stateless validators with them.
 *
 * Copied from the matrix rows for `aida.studio_api.list_change_sets`,
 * `list_items`, `view_diff`, `impact_preview`, `get_latest_eval_run`,
 * `list_eval_questions`, `validate_context_product_contract_endpoint` and
 * `validate_parameter_contract_endpoint` -- all eight rows carry the same list:
 * Analyst, Auditor, DataSteward, MetadataAdmin, PlatformAdmin, Reviewer,
 * SemanticAdmin, Viewer.
 *
 * The two validators are POSTs, so they read as writes to anything that goes by
 * the verb. The matrix says otherwise ("mutating verb, no write found",
 * audited: no) and the handlers agree: neither takes a database session or
 * records anything. They are offered to this list, not the write list.
 */
export const STUDIO_READ_ROLES = [
  "Analyst",
  "Auditor",
  "DataSteward",
  "MetadataAdmin",
  "PlatformAdmin",
  "Reviewer",
  "SemanticAdmin",
  "Viewer",
] as const;

/**
 * The roles every Studio WRITE admits.
 *
 * Copied from the matrix rows for `aida.studio_api.create_change_set`,
 * `add_item`, `remove_item`, `run_tests`, `detect_conflicts_endpoint`,
 * `submit_change_set` and `mine_eval_suite`: DataSteward, MetadataAdmin,
 * PlatformAdmin, SemanticAdmin.
 *
 * `run_tests` and `detect_conflicts_endpoint` are writes even though they sound
 * like checks: the first moves the change set to TESTING and records each item's
 * status and an eval run; the second records CONFLICTED or CLEAN on the change
 * set. `mine_eval_suite` writes the organization's mined question corpus.
 */
export const STUDIO_WRITE_ROLES = ["DataSteward", "MetadataAdmin", "PlatformAdmin", "SemanticAdmin"] as const;

/** "A, B or C" -- the sentence form a hint uses to say which roles a control needs. */
export const listOr = (items: readonly string[]): string =>
  items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;

/** The status a change set's ITEMS can be edited in: `add_item` and `remove_item` both
 *  answer 409 ("items can only be added to / removed from DRAFT change sets") otherwise. */
export const ITEMS_EDITABLE_STATUS = "DRAFT";

/** The statuses `run_tests` and `submit_change_set` accept; both answer 409 for any other. */
export const TESTABLE_STATUSES: readonly string[] = ["DRAFT", "TESTING"];
