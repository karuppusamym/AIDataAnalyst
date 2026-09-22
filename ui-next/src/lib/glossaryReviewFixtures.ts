/* ---------------------------------------------------------------------------
   Demo data for Glossary review (fixture mode only).

   Its own module rather than a block in `fixtures.ts`, and reached only through
   a dynamic import in `./api/glossaryReview.ts`: it keeps a small in-memory
   store so detect -> resolve and generate -> submit behave the way the server
   does, and nothing else reads it.

   THE RULES IT FOLLOWS are the handlers' (`stewardship_api.py`), not a
   friendlier version of them -- a demo that let you resolve a RESOLVED conflict
   would teach a steward a control that the real API refuses:

     * only an OPEN conflict can be resolved (409 otherwise), and doing so moves
       it to REVIEW_REQUIRED and hands back a PENDING governance review;
     * only a DRAFT link proposal can be submitted (409 otherwise);
     * `detect` and `generate` answer the rows THEY created, never the set;
     * nothing here ever decides a review. Approval belongs to somebody else, so
       a REVIEW_REQUIRED row stays REVIEW_REQUIRED for the rest of the session.
--------------------------------------------------------------------------- */

import { ApiError } from "./http";
import type {
  GlossaryConflictCreate,
  GlossaryConflictRead,
  GlossaryConflictResolution,
  GlossaryLinkProposalGenerate,
  GovernanceReviewRead,
} from "./types";
import type { PageOf } from "./ui-types";
import type { GlossaryLinkProposalRead } from "./api/glossary";

const DEMO_ORG = "00000000-0000-0000-0000-000000000001";
const DEMO_PRINCIPAL = "dev-fixture-user";
const CONFLICT_RUN_LIMIT = 100;

/** A UUID-shaped id that is stable across a session and readable in a test. */
const id = (kind: string, n: number): string =>
  `00000000-0000-4000-8000-${kind}${String(n).padStart(12 - kind.length, "0")}`;

const CUSTOMER_TERM = id("a", 1);
const ACTIVE_CUSTOMER_TERM = id("a", 2);

function seedConflicts(): GlossaryConflictRead[] {
  const base = {
    organization_id: DEMO_ORG,
    proposed_resolution: null,
    proposed_definition: null,
    resolution_rationale: null,
    resolved_by: null,
    resolved_at: null,
  };
  return [
    {
      ...base,
      id: id("c", 1),
      term_id: CUSTOMER_TERM,
      conflict_type: "SYNONYM_COLLISION",
      status: "OPEN",
      position_a: {
        term_id: CUSTOMER_TERM,
        display_name: "Customer",
        definition: "A party that holds at least one open account with the bank.",
        colliding_label: "client",
      },
      position_b: {
        term_id: ACTIVE_CUSTOMER_TERM,
        display_name: "Active customer",
        definition: "A customer with at least one posted transaction in the last 90 days.",
        colliding_label: "client",
      },
      assigned_owner: "risk-data-stewards@tenant.example",
      raised_by: "priya.steward",
      created_at: "2026-09-18T09:12:00Z",
      updated_at: "2026-09-18T09:12:00Z",
    },
    {
      ...base,
      id: id("c", 2),
      term_id: null,
      conflict_type: "DEFINITION",
      status: "OPEN",
      position_a: {
        display_name: "Net revenue",
        definition: "Revenue after returns and discounts.",
        source: "Finance handbook 2026",
      },
      position_b: {
        display_name: "Net revenue",
        definition: "Revenue after returns, discounts and channel rebates.",
        source: "Sales operations wiki",
      },
      assigned_owner: "finance-data@tenant.example",
      raised_by: "morgan.covering",
      created_at: "2026-09-17T14:40:00Z",
      updated_at: "2026-09-17T14:40:00Z",
    },
    {
      ...base,
      id: id("c", 3),
      term_id: null,
      conflict_type: "METRIC_FORMULA_COLLISION",
      status: "OPEN",
      position_a: {
        metric_id: id("d", 1),
        metric_name: "Monthly active customers",
        aggregation: "COUNT",
        grain: "month",
        match_kind: "EXACT_MATCH",
        created_by: "priya.steward",
      },
      position_b: {
        metric_id: id("d", 2),
        metric_name: "MAC",
        aggregation: "COUNT",
        grain: "month",
        match_kind: "EXACT_MATCH",
        created_by: "sam.agentdev",
      },
      assigned_owner: "priya.steward",
      raised_by: "priya.steward",
      created_at: "2026-09-16T08:05:00Z",
      updated_at: "2026-09-16T08:05:00Z",
    },
    {
      ...base,
      id: id("c", 4),
      term_id: null,
      conflict_type: "SOURCE_DISAGREEMENT",
      status: "REVIEW_REQUIRED",
      position_a: {
        display_name: "Settlement date",
        definition: "The date funds are released to the beneficiary.",
        source: "Core banking",
      },
      position_b: {
        display_name: "Settlement date",
        definition: "The date the clearing house confirms the payment.",
        source: "Payments hub",
      },
      assigned_owner: null,
      raised_by: "priya.steward",
      proposed_resolution: "MERGE",
      proposed_definition:
        "Settlement date is the date the clearing house confirms the payment; the beneficiary's release date is Value date.",
      resolution_rationale: "Both sources are right about different events, so the two dates get two names.",
      resolved_by: null,
      resolved_at: null,
      created_at: "2026-09-12T11:30:00Z",
      updated_at: "2026-09-19T10:02:00Z",
    },
    {
      ...base,
      id: id("c", 5),
      term_id: id("a", 3),
      conflict_type: "SYNONYM_COLLISION",
      status: "RESOLVED",
      position_a: {
        term_id: id("a", 3),
        display_name: "Balance",
        definition: "The amount held in an account at close of business.",
        colliding_label: "ledger balance",
      },
      position_b: {
        term_id: id("a", 4),
        display_name: "Ledger balance",
        definition: "The booked amount in an account, before pending items.",
        colliding_label: "ledger balance",
      },
      assigned_owner: "priya.steward",
      raised_by: "priya.steward",
      proposed_resolution: "RETAIN_BOTH",
      proposed_definition: null,
      resolution_rationale: "The two terms answer different questions and both stay.",
      resolved_by: "riya.reviewer",
      resolved_at: "2026-09-10T15:20:00Z",
      created_at: "2026-09-08T09:00:00Z",
      updated_at: "2026-09-10T15:20:00Z",
    },
  ];
}

interface DemoLinkProposal {
  table_id: string;
  table_name: string;
  term_id: string;
  term_display_name: string;
  confidence: number;
  evidence: Record<string, unknown>;
}

function seedProposals(): GlossaryLinkProposalRead[] {
  const base = {
    organization_id: DEMO_ORG,
    reviewed_by: null,
    reviewed_at: null,
  };
  const evidence = (matchedLabel: string, kind: string, version: number) => ({
    strategy: "APPROVED_LABEL_EXACT_MATCH",
    matched_label: matchedLabel,
    term_label_kind: kind,
    annotation_version: version,
  });
  return [
    {
      ...base,
      id: id("e", 1),
      table_id: id("f", 1),
      table_name: "customers",
      term_id: CUSTOMER_TERM,
      term_display_name: "Customer",
      source_annotation_id: id("b", 1),
      confidence: 1.0,
      evidence: evidence("Customer", "DISPLAY_NAME", 2),
      status: "DRAFT",
      governance_review_id: null,
      created_by: DEMO_PRINCIPAL,
      created_at: "2026-09-19T08:00:00Z",
      updated_at: "2026-09-19T08:00:00Z",
    },
    {
      ...base,
      id: id("e", 2),
      table_id: id("f", 2),
      table_name: "acct_master",
      term_id: id("a", 5),
      term_display_name: "Account",
      source_annotation_id: id("b", 2),
      confidence: 0.92,
      evidence: evidence("account", "SYNONYM", 1),
      status: "DRAFT",
      governance_review_id: null,
      created_by: DEMO_PRINCIPAL,
      created_at: "2026-09-19T08:00:00Z",
      updated_at: "2026-09-19T08:00:00Z",
    },
    {
      ...base,
      id: id("e", 3),
      table_id: id("f", 3),
      table_name: "card_transactions",
      term_id: id("a", 6),
      term_display_name: "Card transaction",
      source_annotation_id: id("b", 3),
      confidence: 1.0,
      evidence: evidence("Card transaction", "DISPLAY_NAME", 1),
      status: "REVIEW_REQUIRED",
      governance_review_id: id("9", 1),
      created_by: "agent:steward",
      created_at: "2026-09-15T13:10:00Z",
      updated_at: "2026-09-15T13:10:00Z",
    },
    {
      ...base,
      id: id("e", 4),
      table_id: id("f", 4),
      table_name: "loan_applications",
      term_id: id("a", 7),
      term_display_name: "Loan application",
      source_annotation_id: id("b", 4),
      confidence: 1.0,
      evidence: evidence("Loan application", "DISPLAY_NAME", 3),
      status: "APPROVED",
      governance_review_id: id("9", 2),
      created_by: "priya.steward",
      reviewed_by: "riya.reviewer",
      reviewed_at: "2026-09-11T16:45:00Z",
      created_at: "2026-09-10T09:30:00Z",
      updated_at: "2026-09-11T16:45:00Z",
    },
    {
      ...base,
      id: id("e", 5),
      table_id: id("f", 5),
      table_name: "stg_ledger",
      term_id: id("a", 8),
      term_display_name: "General ledger",
      source_annotation_id: id("b", 5),
      confidence: 0.92,
      evidence: evidence("ledger", "SYNONYM", 1),
      status: "REJECTED",
      governance_review_id: id("9", 3),
      created_by: "priya.steward",
      reviewed_by: "riya.reviewer",
      reviewed_at: "2026-09-11T16:50:00Z",
      created_at: "2026-09-10T09:30:00Z",
      updated_at: "2026-09-11T16:50:00Z",
    },
  ];
}

/** Candidates `generate` can still find: pairs no proposal exists for. */
function seedCandidates(): DemoLinkProposal[] {
  return [
    {
      table_id: id("f", 6),
      table_name: "payments",
      term_id: id("a", 9),
      term_display_name: "Payment",
      confidence: 1.0,
      evidence: {
        strategy: "APPROVED_LABEL_EXACT_MATCH",
        matched_label: "Payment",
        term_label_kind: "DISPLAY_NAME",
        annotation_version: 1,
      },
    },
    {
      table_id: id("f", 7),
      table_name: "loan_book",
      term_id: id("a", 10),
      term_display_name: "Loan",
      confidence: 0.92,
      evidence: {
        strategy: "APPROVED_LABEL_EXACT_MATCH",
        matched_label: "loan",
        term_label_kind: "SYNONYM",
        annotation_version: 2,
      },
    },
  ];
}

/** Collisions `detect` can still raise: labels two approved terms share. */
function seedCollisions(): Array<Pick<GlossaryConflictRead, "position_a" | "position_b" | "assigned_owner" | "term_id">> {
  return [
    {
      term_id: id("a", 11),
      assigned_owner: "finance-data@tenant.example",
      position_a: {
        term_id: id("a", 11),
        display_name: "Exposure",
        definition: "The amount the bank could lose if a counterparty defaults.",
        colliding_label: "risk amount",
      },
      position_b: {
        term_id: id("a", 12),
        display_name: "Market risk",
        definition: "Potential loss from movements in market prices.",
        colliding_label: "risk amount",
      },
    },
  ];
}

let conflicts = seedConflicts();
let proposals = seedProposals();
let candidates = seedCandidates();
let collisions = seedCollisions();
let serial = 100;

/** Tests reset between cases; a demo session never calls this. */
export function resetGlossaryReviewFixtures(): void {
  conflicts = seedConflicts();
  proposals = seedProposals();
  candidates = seedCandidates();
  collisions = seedCollisions();
  serial = 100;
}

function pageOf<T extends { created_at: string; status: string }>(
  rows: readonly T[],
  query: { status?: string | null; limit?: number; offset?: number },
): PageOf<T> {
  const limit = query.limit ?? 100;
  const offset = query.offset ?? 0;
  const wanted = query.status ? query.status.toUpperCase() : null;
  const matching = rows
    .filter((row) => wanted === null || row.status === wanted)
    // Newest first, like `order_by(created_at.desc())`. Stable for equal stamps.
    .sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0));
  // Copies, not the store's own rows: a screen holding one must not see a later write
  // land on it without a request, which the server never does.
  return {
    items: matching.slice(offset, offset + limit).map((row) => ({ ...row })),
    limit,
    offset,
    total: matching.length,
  };
}

function review(objectType: string, objectId: string, action: string): GovernanceReviewRead {
  const now = new Date().toISOString();
  serial += 1;
  return {
    id: id("9", serial),
    organization_id: DEMO_ORG,
    object_type: objectType,
    object_id: objectId,
    requested_action: action,
    status: "PENDING",
    requested_by: DEMO_PRINCIPAL,
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    created_at: now,
    updated_at: now,
  };
}

export async function fixtureGlossaryConflicts(query: {
  status?: string | null;
  limit?: number;
  offset?: number;
}): Promise<PageOf<GlossaryConflictRead>> {
  return pageOf(conflicts, query);
}

export async function fixtureDetectGlossaryConflicts(): Promise<PageOf<GlossaryConflictRead>> {
  const now = new Date().toISOString();
  const found = collisions.splice(0, CONFLICT_RUN_LIMIT);
  const created: GlossaryConflictRead[] = found.map((collision) => {
    serial += 1;
    return {
      id: id("c", serial),
      organization_id: DEMO_ORG,
      term_id: collision.term_id,
      conflict_type: "SYNONYM_COLLISION",
      status: "OPEN",
      position_a: collision.position_a,
      position_b: collision.position_b,
      assigned_owner: collision.assigned_owner,
      raised_by: DEMO_PRINCIPAL,
      proposed_resolution: null,
      proposed_definition: null,
      resolution_rationale: null,
      resolved_by: null,
      resolved_at: null,
      created_at: now,
      updated_at: now,
    };
  });
  conflicts = [...created, ...conflicts];
  return { items: created, limit: CONFLICT_RUN_LIMIT, offset: 0, total: created.length };
}

export async function fixtureRaiseGlossaryConflict(
  body: GlossaryConflictCreate,
): Promise<GlossaryConflictRead> {
  const now = new Date().toISOString();
  serial += 1;
  const conflict: GlossaryConflictRead = {
    id: id("c", serial),
    organization_id: DEMO_ORG,
    term_id: body.term_id ?? null,
    conflict_type: body.conflict_type,
    status: "OPEN",
    position_a: body.position_a,
    position_b: body.position_b,
    assigned_owner: body.assigned_owner ?? null,
    raised_by: DEMO_PRINCIPAL,
    proposed_resolution: null,
    proposed_definition: null,
    resolution_rationale: null,
    resolved_by: null,
    resolved_at: null,
    created_at: now,
    updated_at: now,
  };
  conflicts = [conflict, ...conflicts];
  return conflict;
}

export async function fixtureSubmitGlossaryConflictResolution(
  conflictId: string,
  body: GlossaryConflictResolution,
): Promise<GovernanceReviewRead> {
  const conflict = conflicts.find((row) => row.id === conflictId);
  if (!conflict) throw new ApiError(404, "glossary conflict not found");
  if (conflict.status !== "OPEN") throw new ApiError(409, "only open conflicts can be resolved");
  conflict.status = "REVIEW_REQUIRED";
  conflict.proposed_resolution = body.resolution;
  conflict.proposed_definition = body.resolved_definition ?? null;
  conflict.resolution_rationale = body.rationale;
  conflict.updated_at = new Date().toISOString();
  return review("GLOSSARY_CONFLICT", conflict.id, "RESOLVE");
}

export async function fixtureGlossaryLinkProposals(query: {
  status?: string | null;
  limit?: number;
  offset?: number;
}): Promise<PageOf<GlossaryLinkProposalRead>> {
  return pageOf(proposals, query);
}

export async function fixtureGenerateGlossaryLinkProposals(
  body: GlossaryLinkProposalGenerate,
): Promise<PageOf<GlossaryLinkProposalRead>> {
  const minimum = body.minimum_confidence ?? 0.75;
  const limit = body.limit ?? 200;
  const now = new Date().toISOString();
  const taken = candidates.filter((candidate) => candidate.confidence >= minimum).slice(0, limit);
  // Below the bar is left for a later, lower-bar run, exactly as a real one would.
  candidates = candidates.filter((candidate) => !taken.includes(candidate));
  const created: GlossaryLinkProposalRead[] = taken.map((candidate) => {
    serial += 1;
    return {
      id: id("e", serial),
      organization_id: DEMO_ORG,
      table_id: candidate.table_id,
      term_id: candidate.term_id,
      term_display_name: candidate.term_display_name,
      table_name: candidate.table_name,
      source_annotation_id: id("b", serial),
      confidence: candidate.confidence,
      evidence: candidate.evidence,
      status: "DRAFT",
      governance_review_id: null,
      created_by: DEMO_PRINCIPAL,
      reviewed_by: null,
      reviewed_at: null,
      created_at: now,
      updated_at: now,
    };
  });
  proposals = [...created, ...proposals];
  return { items: created, limit, offset: 0, total: created.length };
}

export async function fixtureSubmitGlossaryLinkProposal(
  proposalId: string,
): Promise<GovernanceReviewRead> {
  const proposal = proposals.find((row) => row.id === proposalId);
  if (!proposal) throw new ApiError(404, "glossary link proposal not found");
  if (proposal.status !== "DRAFT") {
    throw new ApiError(409, "only draft link proposals can be submitted");
  }
  const opened = review("GLOSSARY_LINK_PROPOSAL", proposal.id, "APPROVE_LINK");
  proposal.status = "REVIEW_REQUIRED";
  proposal.governance_review_id = opened.id;
  proposal.updated_at = opened.updated_at;
  return opened;
}
