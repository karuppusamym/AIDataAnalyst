/* ---------------------------------------------------------------------------
   R11-REV01 — the change-focused review queue and frozen review batches.

     GET  /v1/governance/reviews/change-queue            keyset page, filtered
     GET  /v1/governance/reviews/change-queue/details    evidence for opened rows
     POST /v1/governance/review-batches                  freeze ids + versions
     GET  /v1/governance/review-batches/{id}             the batch, its counts, resumable
     GET  /v1/governance/review-batches/{id}/items       its members, paged
     POST /v1/governance/review-batches/{id}/decision    decide (or resume): re-check, apply

   Response and request contracts use the generated OpenAPI types. Local aliases
   preserve the screen-facing names without duplicating the server schemas.

   Demo mode (`VITE_USE_FIXTURES` unset) answers from a small deterministic
   queue in this module rather than from `lib/fixtures.ts`, so the screen is
   usable without a backend and its refusals are exercised, not faked away.
--------------------------------------------------------------------------- */

import { demoOr, get, postJson } from "./transport";

import type {
  EvidenceItemRead,
  ChangeQueueItemRead,
  ChangeQueuePageRead,
  ChangeQueueDetailRead,
  ReviewBatchCorrectionRead,
  ReviewBatchRead,
  ReviewBatchDecisionMemberRead,
  ReviewBatchDecisionRead,
  ReviewBatchSelectionWrite,
  ChangeQueueDetailsRead,
  ReviewBatchDecisionCreate,
  ReviewBatchItemPageRead,
  ReviewBatchItemRead,
} from "../types";

export type ChangeQueueEvidence = EvidenceItemRead;
export type ChangeQueueItem = ChangeQueueItemRead;
export type ChangeQueuePage = ChangeQueuePageRead;
export type ChangeQueueDetail = ChangeQueueDetailRead;
export type ReviewBatchCorrection = ReviewBatchCorrectionRead;
export type ReviewBatch = ReviewBatchRead;
export type ReviewBatchMemberOutcome = ReviewBatchDecisionMemberRead;
export type ReviewBatchDecision = ReviewBatchDecisionRead;
export type ReviewBatchSelection = ReviewBatchSelectionWrite;
export type ReviewBatchMember = ReviewBatchItemRead;
export type ReviewBatchMemberPage = ReviewBatchItemPageRead;

export interface ChangeQueueQuery {
  family?: string | null;
  decidableOnly?: boolean;
  cursor?: string | null;
  limit?: number;
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
): Promise<ChangeQueueDetailsRead> {
  return demoOr(
    async () => ({
      items: DEMO_QUEUE.filter((item) => reviewIds.includes(item.review_id)).map((item) => ({
        item,
        evidence: item.evidence_preview,
        diff: null,
      })),
    }),
    async () => {
      const params = new URLSearchParams();
      for (const id of reviewIds) params.append("review_id", id);
      return get<ChangeQueueDetailsRead>(
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

/** `GET /v1/governance/review-batches/{id}` -- after a failed decision call, this says
 *  whether it is `resumable` and how many members are still PENDING. */
export function fetchReviewBatch(batchId: string, signal?: AbortSignal): Promise<ReviewBatch> {
  return demoOr(
    async () => demoSummary(batchId, demoBatches.get(batchId) ?? []),
    async () => get<ReviewBatch>(`/v1/governance/review-batches/${encodeURIComponent(batchId)}`, signal),
  );
}

export interface ReviewBatchMemberQuery {
  cursor?: string | null;
  limit?: number;
  eligibility?: "ELIGIBLE" | "EXCLUDED" | null;
}

/** `GET /v1/governance/review-batches/{id}/items` -- the frozen members, paged in
 *  selection order, so a reviewer can inspect each one before deciding. */
export function fetchReviewBatchMembers(
  batchId: string,
  query: ReviewBatchMemberQuery = {},
  signal?: AbortSignal,
): Promise<ReviewBatchMemberPage> {
  return demoOr(
    async () => demoMembers(batchId, query),
    async () => {
      const params = new URLSearchParams();
      if (query.cursor) params.set("cursor", query.cursor);
      if (query.eligibility) params.set("eligibility", query.eligibility);
      params.set("limit", String(query.limit ?? 100));
      return get<ReviewBatchMemberPage>(
        `/v1/governance/review-batches/${encodeURIComponent(batchId)}/items?${params}`,
        signal,
      );
    },
  );
}

/** `POST /v1/governance/review-batches/{id}/decision` -- one decision, per-member outcomes.
 *  Sent again with the same decision after an interruption, it resumes. */
export function decideReviewBatch(
  batchId: string,
  body: Pick<ReviewBatchDecisionCreate, "decision" | "reason">,
  signal?: AbortSignal,
): Promise<ReviewBatchDecision> {
  return demoOr(
    async () => demoDecide(batchId, body.decision, body.reason ?? null),
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
    // A glossary conflict has no batch evidence contract: reject-only in a batch.
    approve_gate: conflict ? "NO_EVIDENCE_CONTRACT" : null,
    approve_evidence_required: conflict ? [] : ["PROPOSED_TEXT", "SOURCE_SIGNALS"],
    approve_evidence_missing: [],
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
    filters: {
      status: "PENDING", object_types: [], families: query.family ? [query.family] : [],
      change_kinds: [], object_id: null, table_id: null,
      decidable_only: query.decidableOnly ?? false,
    },
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
    organization_id: "demo-org",
    created_by: "demo-reviewer",
    created_at: new Date(0).toISOString(),
    decided_at: null,
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
    resumable: false,
  };
}

function demoMembers(batchId: string, query: ReviewBatchMemberQuery): ReviewBatchMemberPage {
  const selections = demoBatches.get(batchId) ?? [];
  const start = query.cursor ? Number(query.cursor) : 0;
  const limit = query.limit ?? 100;
  const items: ReviewBatchMember[] = selections
    .slice(start, start + limit)
    .map((selection, offset) => {
      const item = DEMO_QUEUE.find((row) => row.review_id === selection.review_id);
      return {
        review_id: selection.review_id,
        position: start + offset,
        object_type: item?.object_type ?? null,
        review_family: item?.review_family ?? null,
        frozen_status: item?.status ?? null,
        evidence_fingerprint: selection.evidence_fingerprint ?? null,
        eligibility: "ELIGIBLE",
        exclusion_code: null,
        approve_gate_code: item?.approve_gate ?? null,
        outcome: "PENDING",
        reason_code: null,
        decided_at: null,
        correction: {
          kind: "NONE",
          available: false,
          method: null,
          path: null,
          subject_type: null,
          subject_id: null,
          reason_code: "NOT_APPLIED",
        },
      };
    });
  return {
    batch_id: batchId,
    next_cursor: start + limit < selections.length ? String(start + limit) : null,
    items,
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
      frozen_status: item?.status ?? null,
      evidence_fingerprint: selection.evidence_fingerprint ?? null,
      approve_gate_code: item?.approve_gate ?? null,
      decided_at: applied ? new Date(0).toISOString() : null,
      eligibility: "ELIGIBLE",
      exclusion_code: null,
      outcome: applied ? "APPLIED" : "REFUSED",
      reason_code: refusal,
      detail: null,
      decided_in_this_call: true,
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
    batch: { ...frozen, status: "DECIDED", decision, decided_at: new Date(0).toISOString() },
    overall: refused === 0 ? "SUCCESS" : applied > 0 ? "PARTIAL_SUCCESS" : "FAILURE",
    applied_count: applied,
    refused_count: refused,
    skipped_count: 0,
    resumed: false,
    decided_in_this_call_count: members.length,
    members,
  };
}
