/* ---------------------------------------------------------------------------
   R11-REV01 — the change-focused review queue and frozen review batches.

     GET  /v1/governance/reviews/change-queue            keyset page, filtered
     GET  /v1/governance/reviews/change-queue/details    evidence for opened rows
     POST /v1/governance/review-batches                  freeze ids + versions
     POST /v1/governance/review-batches/{id}/decision    decide: re-check, apply

   THE TYPES BELOW ARE HAND-WRITTEN, deliberately and temporarily. The routes
   are new, so `lib/types.ts` (generated from the OpenAPI baseline) does not
   know them until the baseline is regenerated -- which is an integration step
   this change must not take on its own (another session owns that artifact
   on this branch). They mirror `aida.review_batch_schemas` field for field;
   once `types.ts` is regenerated, replace them with the generated names.

   Demo mode (`VITE_USE_FIXTURES` unset) answers from a small deterministic
   queue in this module rather than from `lib/fixtures.ts`, so the screen is
   usable without a backend and its refusals are exercised, not faked away.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";

export interface ChangeQueueEvidence {
  category: string;
  claim: string;
  source: string;
  occurred_at?: string | null;
}

export interface ChangeQueueItem {
  review_id: string;
  object_type: string;
  object_id: string;
  review_family: string;
  change_kind: string;
  status: string;
  requested_by: string;
  created_at: string;
  risk_tier: string;
  confidence: number | null;
  diffable: boolean;
  evidence_count: number;
  evidence_preview: ChangeQueueEvidence[];
  evidence_fingerprint: string;
  /** Why this reviewer may not decide the row at all (NOT_PENDING, MAKER_CHECKER, ...). */
  decide_blocker: string | null;
  /** Why batch *approval* would be refused (EVIDENCE_NOT_SHOWN, INDIVIDUAL_DECISION_REQUIRED). */
  approve_gate: string | null;
  target_unavailable: boolean;
}

export interface ChangeQueuePage {
  organization_id: string;
  generated_at: string;
  limit: number;
  next_cursor: string | null;
  total: number | null;
  items: ChangeQueueItem[];
}

export interface ChangeQueueDetail {
  item: ChangeQueueItem;
  evidence: ChangeQueueEvidence[];
}

export interface ReviewBatchCorrection {
  kind: string;
  available: boolean;
  method: string | null;
  path: string | null;
  subject_type: string | null;
  subject_id: string | null;
  reason_code: string | null;
}

export interface ReviewBatch {
  id: string;
  status: string;
  selection_mode: string;
  selection_truncated: boolean;
  item_count: number;
  eligible_count: number;
  excluded_count: number;
  selection_fingerprint: string;
  decision: string | null;
  exclusion_counts: Record<string, number>;
  approve_gate_counts: Record<string, number>;
  outcome_counts: Record<string, number>;
}

export interface ReviewBatchMemberOutcome {
  review_id: string;
  position: number;
  object_type: string | null;
  review_family: string | null;
  eligibility: string;
  exclusion_code: string | null;
  outcome: string;
  reason_code: string | null;
  detail?: string | null;
  correction: ReviewBatchCorrection;
}

export interface ReviewBatchDecision {
  batch: ReviewBatch;
  overall: "SUCCESS" | "PARTIAL_SUCCESS" | "FAILURE";
  applied_count: number;
  refused_count: number;
  skipped_count: number;
  members: ReviewBatchMemberOutcome[];
}

export interface ChangeQueueQuery {
  family?: string | null;
  decidableOnly?: boolean;
  cursor?: string | null;
  limit?: number;
}

export interface ReviewBatchSelection {
  review_id: string;
  evidence_fingerprint: string;
}

/** `GET /v1/governance/reviews/change-queue` -- one keyset page. */
export function fetchChangeQueue(
  query: ChangeQueueQuery,
  signal?: AbortSignal,
): Promise<ChangeQueuePage> {
  return demoOr(
    async () => demoPage(query),
    async () => {
      const params = new URLSearchParams();
      if (query.family) params.append("family", query.family);
      if (query.decidableOnly) params.set("decidable_only", "true");
      if (query.cursor) params.set("cursor", query.cursor);
      params.set("limit", String(query.limit ?? 50));
      return get<ChangeQueuePage>(`/v1/governance/reviews/change-queue?${params}`, signal);
    },
  );
}

/** `GET /v1/governance/reviews/change-queue/details` -- evidence on demand. */
export function fetchChangeQueueDetails(
  reviewIds: string[],
  signal?: AbortSignal,
): Promise<{ items: ChangeQueueDetail[] }> {
  return demoOr(
    async () => ({
      items: DEMO_QUEUE.filter((item) => reviewIds.includes(item.review_id)).map((item) => ({
        item,
        evidence: item.evidence_preview,
      })),
    }),
    async () => {
      const params = new URLSearchParams();
      for (const id of reviewIds) params.append("review_id", id);
      return get<{ items: ChangeQueueDetail[] }>(
        `/v1/governance/reviews/change-queue/details?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/governance/review-batches` -- freeze explicit ids and the versions seen. */
export function freezeReviewBatch(
  items: ReviewBatchSelection[],
  signal?: AbortSignal,
): Promise<ReviewBatch> {
  return demoOr(
    async () => demoFreeze(items),
    async () => postJson<ReviewBatch>(`/v1/governance/review-batches`, { items }, signal),
  );
}

/** `POST /v1/governance/review-batches/{id}/decision` -- one decision, per-member outcomes. */
export function decideReviewBatch(
  batchId: string,
  body: { decision: "APPROVE" | "REJECT"; reason: string | null },
  signal?: AbortSignal,
): Promise<ReviewBatchDecision> {
  return demoOr(
    async () => demoDecide(batchId, body.decision, body.reason),
    async () =>
      postJson<ReviewBatchDecision>(
        `/v1/governance/review-batches/${encodeURIComponent(batchId)}/decision`,
        body,
        signal,
      ),
  );
}

/* ---------------------------------------------------------------------------
   Demo queue: 240 rows across three families, one of them the reviewer's own.
--------------------------------------------------------------------------- */

function demoItem(index: number): ChangeQueueItem {
  const conflict = index % 12 === 11;
  const column = !conflict && index % 2 === 1;
  const own = index === 3;
  const objectType = conflict
    ? "GLOSSARY_CONFLICT"
    : column
      ? "COLUMN_DESCRIPTION_DRAFT"
      : "ASSET_DESCRIPTION_DRAFT";
  const fingerprint = index.toString(16).padStart(64, "0");
  return {
    review_id: `demo-review-${index}`,
    object_type: objectType,
    object_id: `demo-object-${index}`,
    review_family: conflict ? "SEMANTIC" : "DESCRIPTION",
    change_kind: conflict ? "RESOLVE" : "PUBLISH_DESCRIPTION",
    status: "PENDING",
    requested_by: own ? "demo-reviewer" : "steward-agent",
    created_at: new Date(Date.UTC(2026, 8, 1, 0, 0, index)).toISOString(),
    risk_tier: conflict ? "T2" : "T0",
    confidence: conflict ? null : 0.8,
    diffable: false,
    evidence_count: conflict ? 0 : 3,
    evidence_preview: conflict
      ? []
      : [
          {
            category: "DESCRIPTION_DRAFT",
            claim: `proposed_description: ${column ? "col" : "table"}_${index} holds one attribute per event.`,
            source: `demo:${index}`,
          },
        ],
    evidence_fingerprint: fingerprint,
    decide_blocker: own ? "MAKER_CHECKER" : null,
    approve_gate: conflict ? "EVIDENCE_NOT_SHOWN" : null,
    target_unavailable: false,
  };
}

const DEMO_QUEUE: ChangeQueueItem[] = Array.from({ length: 240 }, (_, index) => demoItem(index));

function demoPage(query: ChangeQueueQuery): ChangeQueuePage {
  const filtered = DEMO_QUEUE.filter(
    (item) =>
      (!query.family || item.review_family === query.family) &&
      (!query.decidableOnly || item.decide_blocker === null),
  );
  const start = query.cursor ? Number(query.cursor) : 0;
  const limit = query.limit ?? 50;
  const items = filtered.slice(start, start + limit);
  const next = start + limit < filtered.length ? String(start + limit) : null;
  return {
    organization_id: "demo-org",
    generated_at: new Date(0).toISOString(),
    limit,
    next_cursor: next,
    total: filtered.length,
    items,
  };
}

const demoBatches = new Map<string, ReviewBatchSelection[]>();

function demoFreeze(items: ReviewBatchSelection[]): ReviewBatch {
  const id = `demo-batch-${demoBatches.size + 1}`;
  demoBatches.set(id, items);
  return demoSummary(id, items);
}

function demoSummary(id: string, items: ReviewBatchSelection[]): ReviewBatch {
  const gates: Record<string, number> = {};
  for (const selection of items) {
    const gate = DEMO_QUEUE.find((item) => item.review_id === selection.review_id)?.approve_gate;
    if (gate) gates[gate] = (gates[gate] ?? 0) + 1;
  }
  return {
    id,
    status: "FROZEN",
    selection_mode: "EXPLICIT",
    selection_truncated: false,
    item_count: items.length,
    eligible_count: items.length,
    excluded_count: 0,
    selection_fingerprint: "0".repeat(64),
    decision: null,
    exclusion_counts: {},
    approve_gate_counts: gates,
    outcome_counts: { PENDING: items.length },
  };
}

function demoDecide(
  batchId: string,
  decision: "APPROVE" | "REJECT",
  reason: string | null,
): ReviewBatchDecision {
  const selections = demoBatches.get(batchId) ?? [];
  const members: ReviewBatchMemberOutcome[] = selections.map((selection, position) => {
    const item = DEMO_QUEUE.find((row) => row.review_id === selection.review_id);
    const refusal =
      item === undefined
        ? "NOT_FOUND"
        : decision === "APPROVE" && item.approve_gate
          ? item.approve_gate
          : decision === "REJECT" && !reason
            ? "RATIONALE_REQUIRED"
            : position % 7 === 6
              ? "STALE_EVIDENCE"
              : null;
    const applied = refusal === null;
    const table = item?.object_type === "ASSET_DESCRIPTION_DRAFT";
    return {
      review_id: selection.review_id,
      position,
      object_type: item?.object_type ?? null,
      review_family: item?.review_family ?? null,
      eligibility: "ELIGIBLE",
      exclusion_code: null,
      outcome: applied ? "APPLIED" : "REFUSED",
      reason_code: refusal,
      detail: null,
      correction:
        applied && decision === "APPROVE"
          ? {
              kind: "WITHDRAW_DESCRIPTION",
              available: true,
              method: "POST",
              path: "/v1/descriptions/withdrawals",
              subject_type: table ? "TABLE" : "COLUMN",
              subject_id: `demo-subject-${position}`,
              reason_code: null,
            }
          : {
              kind: applied ? "REPROPOSE" : "NONE",
              available: false,
              method: null,
              path: null,
              subject_type: null,
              subject_id: null,
              reason_code: applied ? "NO_REOPEN_PATH" : "NOT_APPLIED",
            },
    };
  });
  const applied = members.filter((member) => member.outcome === "APPLIED").length;
  const refused = members.length - applied;
  const frozen = demoSummary(batchId, selections);
  return {
    batch: { ...frozen, status: "DECIDED", decision },
    overall: refused === 0 ? "SUCCESS" : applied > 0 ? "PARTIAL_SUCCESS" : "FAILURE",
    applied_count: applied,
    refused_count: refused,
    skipped_count: 0,
    members,
  };
}
