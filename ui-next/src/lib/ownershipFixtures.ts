/* ---------------------------------------------------------------------------
   Demo data for the Ownership screen's rules and leaver flow (fixture mode only).

   Its own module rather than a block in `fixtures.ts`, for the reason
   `documentFixtures.ts` gives: it keeps a small store so create -> apply -> the
   requests list behaves the way the server does, and nothing else reads it. It is
   imported from inside the demo arm of `lib/api/ownership.ts`, so a production
   build, which folds `demoOr` to its live arm, contains none of it.

   The rules follow `aida.stewardship_api`, not what would demo well:

     * A rule key is unique per organization; a second create with the same key
       is a 409 "ownership rule key already exists".
     * `apply` matches with `fnmatchcase` over CASE-FOLDED values (both sides), on
       the field the rule names; TAG matches if ANY tag does; a table with no
       domain never matches DOMAIN_KEY. A rule that matches nothing is a 409
       "ownership rule matched no active tables". At most 500 tables are taken.
     * `apply` and the leaver request both return an operation at
       `REVIEW_REQUIRED` with `applied_count` 0 and a review id. Nothing is
       "assigned" -- the demo never approves anything, exactly like a server whose
       reviewer has not yet looked.
     * The leaver request refuses an explicit id that is not an ACTIVE assignment
       the leaver holds (409), and a whole-portfolio request from someone who holds
       nothing (409).

   The assignments themselves are `fixtures.ts`'s (`makeFixtureOwnershipAssignments`,
   read rather than copied), so the leaver preview and the request agree. This module
   does NOT import `./fixtures`: `lib/api/ownership.ts` hands the demo arm the fixture
   module and passes the reader in, because `demoDataMode.test.ts` allows exactly one
   dynamic reference to `fixtures.ts` in the client -- the transport seam's -- and a
   second is how the demo estate gets back into a production bundle.
--------------------------------------------------------------------------- */

import { ApiError } from "./http";
import type {
  BulkStewardshipOperationRead,
  LeaverReassignmentRequest,
  OwnershipRuleCreate,
  OwnershipRuleRead,
} from "./types";
import type { OwnershipAssignmentRead } from "./api/catalog";
import type { PageOf } from "./ui-types";

/** How the leaver request reads the demo assignments: `fixtures.ts`'s own listing, handed in. */
export type AssignmentReader = (query: { limit?: number; offset?: number }) => Promise<PageOf<OwnershipAssignmentRead>>;

const DEMO_ORG = "00000000-0000-0000-0000-000000000001";
/** Who the demo session is (`makeFixtureMe`), so a request reads as the caller's own. */
const DEMO_PRINCIPAL = "dev-fixture-user";
const APPLY_MAX = 500;

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** The demo catalog, as `apply` sees it: schema, name, the domain key of the table's
 *  business annotation (or none) and its approved tags. Ids match `fixtures.ts`'s
 *  ownership assignments, so a rule's matches and the assignments listing agree. */
interface DemoTable {
  readonly id: string;
  readonly schema: string;
  readonly name: string;
  readonly domain: string | null;
  readonly tags: readonly string[];
}

const DEMO_TABLES: readonly DemoTable[] = [
  { id: "t_000000", schema: "retail", name: "orders_raw", domain: "retail", tags: ["pii", "raw"] },
  { id: "t_000001", schema: "retail", name: "customer_dim", domain: "retail", tags: ["pii"] },
  { id: "t_000002", schema: "treasury", name: "treasury_snapshot", domain: "treasury", tags: [] },
  { id: "t_000003", schema: "treasury", name: "fx_rates_daily", domain: "treasury", tags: ["reference"] },
  { id: "t_000004", schema: "risk", name: "exposure_by_counterparty", domain: null, tags: ["confidential"] },
  { id: "t_000005", schema: "staging", name: "raw_payments", domain: null, tags: ["raw"] },
];

/** `fnmatch.fnmatchcase` after `casefold()` on both sides, for the parts of the
 *  syntax a steward types: `*`, `?` and `[seq]` / `[!seq]`. */
export function globMatches(value: string, pattern: string): boolean {
  const target = value.toLowerCase();
  const glob = pattern.toLowerCase();
  let source = "";
  for (let index = 0; index < glob.length; index += 1) {
    const char = glob[index]!;
    if (char === "*") {
      source += ".*";
    } else if (char === "?") {
      source += ".";
    } else if (char === "[") {
      const close = glob.indexOf("]", index + 2);
      if (close === -1) {
        source += "\\[";
      } else {
        const body = glob.slice(index + 1, close);
        source += body.startsWith("!") ? `[^${body.slice(1)}]` : `[${body}]`;
        index = close;
      }
    } else {
      source += char.replace(/[.+^${}()|\\]/g, "\\$&");
    }
  }
  return new RegExp(`^${source}$`, "s").test(target);
}

function ruleMatches(rule: OwnershipRuleRead, table: DemoTable): boolean {
  switch (rule.match_field) {
    case "TAG":
      return table.tags.some((tag) => globMatches(tag, rule.match_pattern));
    case "TABLE_NAME":
      return globMatches(table.name, rule.match_pattern);
    case "SCHEMA_NAME":
      return globMatches(table.schema, rule.match_pattern);
    case "QUALIFIED_NAME":
      return globMatches(`${table.schema}.${table.name}`, rule.match_pattern);
    case "DOMAIN_KEY":
      return table.domain !== null && globMatches(table.domain, rule.match_pattern);
  }
}

const RULES: OwnershipRuleRead[] = [
  {
    // The id `fixtures.ts`'s "own_customer_dim" assignment names as its source rule.
    id: "rule_retail_tables", organization_id: DEMO_ORG, status: "ACTIVE",
    rule_key: "retail-tables", display_name: "Retail tables",
    match_field: "SCHEMA_NAME", match_pattern: "retail",
    owner_type: "GROUP", owner_principal: "retail-data-stewards",
    created_by: "fixture-admin", created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
  },
  {
    id: "rule_pii_tagged", organization_id: DEMO_ORG, status: "ACTIVE",
    rule_key: "pii-tagged", display_name: "Anything tagged PII",
    match_field: "TAG", match_pattern: "pii*",
    owner_type: "INDIVIDUAL", owner_principal: "privacy.lead@tenant.example",
    created_by: "fixture-admin", created_at: "2026-03-02T00:00:00Z", updated_at: "2026-03-02T00:00:00Z",
  },
];

const OPERATIONS: BulkStewardshipOperationRead[] = [];
let sequence = 0;

function newOperation(
  base: Pick<BulkStewardshipOperationRead, "operation_type" | "subject_type" | "subject_ids" | "parameters">,
): BulkStewardshipOperationRead {
  sequence += 1;
  const now = new Date().toISOString();
  const operation: BulkStewardshipOperationRead = {
    id: `op-demo-${sequence}`,
    organization_id: DEMO_ORG,
    ...base,
    status: "REVIEW_REQUIRED",
    governance_review_id: `review-demo-${sequence}`,
    requested_by: DEMO_PRINCIPAL,
    applied_by: null,
    applied_at: null,
    applied_count: 0,
    applied_subject_ids: [],
    reverses_operation_id: null,
    review_audit_sample_id: null,
    created_at: now,
    updated_at: now,
  };
  OPERATIONS.unshift(operation);
  return operation;
}

function page<T>(items: T[], limit = 100, offset = 0): PageOf<T> {
  return { items: items.slice(offset, offset + limit), limit, offset, total: items.length };
}

/** `GET .../ownership-rules`: the ACTIVE rules by display name, 500 at most. */
export async function fixtureOwnershipRules(_organizationId: string): Promise<PageOf<OwnershipRuleRead>> {
  await wait(60);
  const active = RULES.filter((rule) => rule.status === "ACTIVE").sort((a, b) =>
    a.display_name.localeCompare(b.display_name),
  );
  return { items: active.map((rule) => ({ ...rule })), limit: 500, offset: 0, total: active.length };
}

/** `POST .../ownership-rules`. */
export async function fixtureCreateOwnershipRule(
  organizationId: string,
  body: OwnershipRuleCreate,
): Promise<OwnershipRuleRead> {
  await wait(80);
  if (RULES.some((rule) => rule.rule_key === body.rule_key)) {
    throw new ApiError(409, "ownership rule key already exists");
  }
  sequence += 1;
  const now = new Date().toISOString();
  const rule: OwnershipRuleRead = {
    ...body,
    id: `rule-demo-${sequence}`,
    organization_id: organizationId,
    status: "ACTIVE",
    created_by: DEMO_PRINCIPAL,
    created_at: now,
    updated_at: now,
  };
  RULES.push(rule);
  return { ...rule };
}

/** `POST /v1/ownership-rules/{id}/apply`: matches now, opens a review, changes nothing. */
export async function fixtureApplyOwnershipRule(ruleId: string): Promise<BulkStewardshipOperationRead> {
  await wait(90);
  const rule = RULES.find((candidate) => candidate.id === ruleId && candidate.status === "ACTIVE");
  if (!rule) throw new ApiError(404, "active ownership rule not found");
  const matched = DEMO_TABLES.filter((table) => ruleMatches(rule, table))
    .slice(0, APPLY_MAX)
    .map((table) => table.id);
  if (matched.length === 0) throw new ApiError(409, "ownership rule matched no active tables");
  return newOperation({
    operation_type: "ASSIGN_OWNERSHIP",
    subject_type: "TABLE",
    subject_ids: matched,
    parameters: {
      owner_type: rule.owner_type,
      owner_principal: rule.owner_principal,
      source_rule_id: rule.id,
    },
  });
}

/** `POST .../stewardship/leaver-reassignment`. */
export async function fixtureRequestLeaverReassignment(
  _organizationId: string,
  body: LeaverReassignmentRequest,
  readAssignments: AssignmentReader,
): Promise<BulkStewardshipOperationRead> {
  await wait(90);
  const ownerType = body.owner_type ?? "INDIVIDUAL";
  if (body.leaving_principal === body.successor_principal) {
    throw new ApiError(422, "body: Value error, successor_principal must differ from leaving_principal");
  }
  const everything = (await readAssignments({ limit: 500 })).items;
  const held = everything.filter(
    (row) =>
      row.status === "ACTIVE" && row.owner_principal === body.leaving_principal && row.owner_type === ownerType,
  );
  let subjectIds: string[];
  let mode: "EXPLICIT" | "FILTER";
  if (body.assignment_ids) {
    const heldIds = new Set(held.map((row) => row.id));
    if (body.assignment_ids.some((id) => !heldIds.has(id))) {
      throw new ApiError(
        409,
        "one or more assignment_ids are not active ownership assignments currently held by leaving_principal",
      );
    }
    subjectIds = [...body.assignment_ids];
    mode = "EXPLICIT";
  } else {
    if (held.length === 0) {
      throw new ApiError(409, "leaving_principal has no active ownership assignments to reassign");
    }
    subjectIds = held.slice(0, 500).map((row) => row.id);
    mode = "FILTER";
  }
  return newOperation({
    operation_type: "REASSIGN_LEAVER",
    subject_type: "OWNERSHIP_ASSIGNMENT",
    subject_ids: subjectIds,
    parameters: {
      leaving_principal: body.leaving_principal,
      successor_principal: body.successor_principal,
      owner_type: ownerType,
      rationale: body.rationale,
      selection_mode: mode,
      selection_truncated: mode === "FILTER" && held.length > 500,
    },
  });
}

/** `GET .../stewardship/bulk-operations`: what this session has requested, newest first. */
export async function fixtureOwnershipOperations(query: {
  status?: string | null;
  limit?: number;
  offset?: number;
}): Promise<PageOf<BulkStewardshipOperationRead>> {
  await wait(50);
  const status = query.status?.toUpperCase();
  const rows = OPERATIONS.filter((operation) => !status || operation.status === status);
  return page(
    rows.map((operation) => ({ ...operation })),
    query.limit ?? 100,
    query.offset ?? 0,
  );
}
