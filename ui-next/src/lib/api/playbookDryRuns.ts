/* ---------------------------------------------------------------------------
   R11-REV01 — stored playbook dry-runs, and runs bound to what was previewed.

     POST /v1/playbooks/{id}/dry-runs                   evaluate + store, apply nothing
     POST /v1/playbooks/{id}/dry-runs/{dry_run_id}/run  run exactly what was previewed

   Imported directly by `components/PlaybookDryRunPanel` rather than through the
   `lib/api` barrel, so the panel stays self-contained wherever Playbooks is
   mounted.

   Demo mode (`VITE_USE_FIXTURES` unset) answers from a small deterministic
   preview in this module, so the panel is usable without a backend: the first
   bound run of a preview matches and runs, and a second one is refused the way
   the server refuses it (a preview binds one run).
--------------------------------------------------------------------------- */

import { ApiError } from "../http";
import { demoOr, postJson } from "./transport";
import type {
  PlaybookBoundRunCreate,
  PlaybookBoundRunRead,
  PlaybookStoredDryRunRead,
} from "../types";

export type PlaybookStoredDryRun = PlaybookStoredDryRunRead;
export type PlaybookBoundRun = PlaybookBoundRunRead;

/** `POST /v1/playbooks/{id}/dry-runs` -- the dry-run, stored so a run can bind to it. */
export function storePlaybookDryRun(
  playbookId: string,
  signal?: AbortSignal,
): Promise<PlaybookStoredDryRun> {
  return demoOr(
    async () => demoPreview(playbookId),
    async () =>
      postJson<PlaybookStoredDryRun>(
        `/v1/playbooks/${encodeURIComponent(playbookId)}/dry-runs`,
        {},
        signal,
      ),
  );
}

/** `POST /v1/playbooks/{id}/dry-runs/{dry_run_id}/run` -- with `require_match` (the
 *  default) the server runs only if nothing moved since the preview; otherwise it
 *  answers `ran: false` and counts what moved. */
export function runPlaybookAsPreviewed(
  playbookId: string,
  dryRunId: string,
  body: PlaybookBoundRunCreate = { require_match: true },
  signal?: AbortSignal,
): Promise<PlaybookBoundRun> {
  return demoOr(
    async () => demoBoundRun(playbookId, dryRunId),
    async () =>
      postJson<PlaybookBoundRun>(
        `/v1/playbooks/${encodeURIComponent(playbookId)}/dry-runs/${encodeURIComponent(dryRunId)}/run`,
        body,
        signal,
      ),
  );
}

/* ---------------------------------------------------------------------------
   Demo preview: three tables, one of them already tagged.
--------------------------------------------------------------------------- */

const demoBound = new Set<string>();
let demoCounter = 0;

function demoPreview(playbookId: string): PlaybookStoredDryRun {
  demoCounter += 1;
  const items = ["orders", "order_lines", "refunds"].map((name, index) => ({
    subject_type: "TABLE",
    subject_id: `demo-table-${index}`,
    qualified_name: `sales.${name}`,
    current_value: index === 2 ? "pii-review=pending" : null,
    proposed_value: "pii-review=pending",
    change: index === 2 ? "NO_CHANGE" : "CREATE",
    evidence_version: index.toString(16).padStart(64, "0"),
  }));
  return {
    playbook_id: playbookId,
    action: "TAG",
    enabled: true,
    rule_version: "a".repeat(64),
    evaluated_at: new Date(0).toISOString(),
    matched_count: items.length,
    tables_truncated: false,
    columns_truncated: false,
    auto_apply_max_items: 0,
    predicted_disposition: "HUMAN_REVIEW",
    automation: {
      action: "TAG",
      subject_type: "TABLE",
      has_automatic_branch: true,
      automatic_branch_enabled: false,
      automatic_when: "0 < matched_count <= auto_apply_max_items",
      automatic_path: "aida.playbooks._auto_apply",
      automatic_principal: "fleet-scheduler",
      involves_model: false,
      reviewed_operation_type: "TAG",
      compensating_operation_when_reviewed: "RESTORE_TAG",
      compensating_operation_when_automatic: null,
      automatic_correction_reason: "NO_BEFORE_IMAGE_RECORDED",
    },
    items,
    dry_run_id: `demo-dry-run-${demoCounter}`,
    match_digest: "b".repeat(64),
    evidence_digest: "c".repeat(64),
    change_counts: { CREATE: 2, NO_CHANGE: 1 },
  };
}

function demoBoundRun(playbookId: string, dryRunId: string): PlaybookBoundRun {
  if (demoBound.has(dryRunId)) {
    throw new ApiError(409, "PLAYBOOK_DRY_RUN_ALREADY_BOUND");
  }
  demoBound.add(dryRunId);
  return {
    dry_run_id: dryRunId,
    ran: true,
    refusal_code: null,
    binding: {
      status: "MATCHES",
      rule_version_matches: true,
      match_set_matches: true,
      evidence_matches: true,
      added_count: 0,
      removed_count: 0,
      changed_count: 0,
      moved_subject_ids: [],
      reasons: [],
    },
    run: {
      playbook_id: playbookId,
      matched_count: 3,
      outcome: "QUEUED_FOR_REVIEW",
      bulk_action_run_id: null,
      bulk_stewardship_operation_id: "demo-operation-1",
      governance_review_id: "demo-review-1",
    },
  };
}
