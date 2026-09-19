import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { PlaybookBoundRun, PlaybookStoredDryRun } from "../lib/api/playbookDryRuns";
import type { PlaybookRead } from "../lib/types";
import { expectNoAxeViolations } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-REV01: a playbook's dry run and the run bound to it. The API module is
   mocked. What is pinned: the preview is stored before anything runs; the
   per-action report states what the run would do by the run's own rule --
   automatic with no human, or queued for review -- how each branch is undone
   (and that an automatic run has no governed reversal), that no model is
   consulted, and each object's before -> after; "Run as previewed" sends the
   stored preview's id with require_match; a refusal says what moved and that
   nothing was applied.
--------------------------------------------------------------------------- */

const storePlaybookDryRun = vi.fn<(id: string) => Promise<PlaybookStoredDryRun>>();
const runPlaybookAsPreviewed =
  vi.fn<(id: string, dryRunId: string, body: { require_match?: boolean }) => Promise<PlaybookBoundRun>>();

vi.mock("../lib/api/playbookDryRuns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/playbookDryRuns")>();
  return {
    ...actual,
    storePlaybookDryRun: (id: string) => storePlaybookDryRun(id),
    runPlaybookAsPreviewed: (id: string, dryRunId: string, body: { require_match?: boolean }) =>
      runPlaybookAsPreviewed(id, dryRunId, body),
  };
});

const PLAYBOOK: PlaybookRead = {
  id: "pb-1",
  organization_id: "org",
  name: "Tag staging tables",
  action: "TAG",
  datasource_id: "ds-1",
  match_field: "TABLE_NAME",
  match_pattern: "stg_%",
  column_name_pattern: null,
  action_parameters: { tag_key: "needs-review" },
  schedule_interval_minutes: 60,
  auto_apply_max_items: 50,
  enabled: true,
  created_by: "steward",
  last_run_at: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

function preview(overrides: Partial<PlaybookStoredDryRun> = {}): PlaybookStoredDryRun {
  return {
    playbook_id: "pb-1",
    action: "TAG",
    enabled: true,
    rule_version: "1".repeat(64),
    evaluated_at: "2026-09-19T00:00:00Z",
    matched_count: 2,
    tables_truncated: false,
    columns_truncated: false,
    auto_apply_max_items: 50,
    predicted_disposition: "AUTOMATIC",
    automation: {
      action: "TAG",
      subject_type: "TABLE",
      has_automatic_branch: true,
      automatic_branch_enabled: true,
      automatic_when: "0 < matched_count <= auto_apply_max_items",
      automatic_path: "aida.playbooks._auto_apply",
      automatic_principal: "fleet-scheduler",
      involves_model: false,
      reviewed_operation_type: "TAG",
      compensating_operation_when_reviewed: "RESTORE_TAG",
      compensating_operation_when_automatic: null,
      automatic_correction_reason: "NO_BEFORE_IMAGE_RECORDED",
    },
    items: [
      {
        subject_type: "TABLE",
        subject_id: "t-1",
        qualified_name: "staging.stg_orders",
        current_value: null,
        proposed_value: "needs-review=None",
        change: "CREATE",
        evidence_version: "a".repeat(64),
      },
      {
        subject_type: "TABLE",
        subject_id: "t-2",
        qualified_name: "staging.stg_refunds",
        current_value: "needs-review=None",
        proposed_value: "needs-review=None",
        change: "NO_CHANGE",
        evidence_version: "b".repeat(64),
      },
    ],
    dry_run_id: "dry-1",
    match_digest: "c".repeat(64),
    evidence_digest: "d".repeat(64),
    change_counts: { CREATE: 1, NO_CHANGE: 1 },
    ...overrides,
  };
}

const MATCHES = {
  status: "MATCHES",
  rule_version_matches: true,
  match_set_matches: true,
  evidence_matches: true,
  added_count: 0,
  removed_count: 0,
  changed_count: 0,
  moved_subject_ids: [],
  reasons: [],
};

async function renderPanel(onRan = vi.fn()) {
  const { PlaybookDryRunPanel } = await import("./PlaybookDryRunPanel");
  const view = render(<PlaybookDryRunPanel playbook={PLAYBOOK} onRan={onRan} />);
  return { container: view.container, onRan };
}

beforeEach(() => {
  storePlaybookDryRun.mockReset();
  runPlaybookAsPreviewed.mockReset();
});

describe("PlaybookDryRunPanel", () => {
  it("reports per action what a run would do, how it is undone, and each object's change", async () => {
    storePlaybookDryRun.mockResolvedValue(preview());
    const { container } = await renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Preview (dry run)" }));
    await screen.findByText(/Would apply to all 2 automatically, with no human review/);
    expect(storePlaybookDryRun).toHaveBeenCalledWith("pb-1");
    // Automation is stated for this action and bound, including what cannot be undone.
    expect(screen.getByText(/up to 50 matched object\(s\) apply with no review, as fleet-scheduler/)).toBeInTheDocument();
    expect(screen.getByText(/undone by RESTORE_TAG/)).toBeInTheDocument();
    expect(screen.getByText(/no governed reversal -- no before-image is recorded/)).toBeInTheDocument();
    expect(screen.getByText(/No model is consulted/)).toBeInTheDocument();
    const items = screen.getByRole("list", { name: "Objects this run would act on" });
    expect(within(items).getByText("staging.stg_orders")).toBeInTheDocument();
    expect(within(items).getByText("(none) → needs-review=None")).toBeInTheDocument();
    expect(within(items).getByText("NO_CHANGE")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("says when the matcher truncated and when the run goes to review instead", async () => {
    storePlaybookDryRun.mockResolvedValue(
      preview({
        predicted_disposition: "HUMAN_REVIEW",
        auto_apply_max_items: 0,
        matched_count: 500,
        tables_truncated: true,
        automation: { ...preview().automation, automatic_branch_enabled: false },
      }),
    );
    await renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Preview (dry run)" }));
    await screen.findByText(/Would queue one TAG operation over 500 object\(s\) for maker-checker review/);
    expect(screen.getByRole("note")).toHaveTextContent("The matcher stopped at 500 tables");
    expect(screen.getByText(/Automatic apply is off for this playbook/)).toBeInTheDocument();
  });

  it("runs bound to the stored preview, and only once", async () => {
    storePlaybookDryRun.mockResolvedValue(preview());
    runPlaybookAsPreviewed.mockResolvedValue({
      dry_run_id: "dry-1",
      ran: true,
      refusal_code: null,
      binding: MATCHES,
      run: {
        playbook_id: "pb-1",
        matched_count: 2,
        outcome: "AUTO_APPLIED",
        bulk_action_run_id: "run-1",
        bulk_stewardship_operation_id: null,
        governance_review_id: null,
      },
    });
    const { onRan } = await renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Preview (dry run)" }));
    const run = await screen.findByRole("button", { name: "Run as previewed" });
    fireEvent.click(run);
    await screen.findByText("Ran as previewed");
    expect(runPlaybookAsPreviewed).toHaveBeenCalledWith("pb-1", "dry-1", { require_match: true });
    expect(onRan).toHaveBeenCalledWith(expect.objectContaining({ outcome: "AUTO_APPLIED" }));
    expect(screen.getByRole("button", { name: "Run as previewed" })).toBeDisabled();
  });

  it("says what moved when the run is refused, and that nothing was applied", async () => {
    storePlaybookDryRun.mockResolvedValue(preview());
    runPlaybookAsPreviewed.mockResolvedValue({
      dry_run_id: "dry-1",
      ran: false,
      refusal_code: "PREVIEW_MISMATCH",
      binding: {
        ...MATCHES,
        status: "DIFFERS",
        match_set_matches: false,
        evidence_matches: false,
        added_count: 1,
        removed_count: 0,
        changed_count: 2,
        moved_subject_ids: ["t-3", "t-1", "t-2"],
        reasons: ["MATCH_SET_CHANGED", "EVIDENCE_CHANGED"],
      },
      run: null,
    });
    const { onRan } = await renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Preview (dry run)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run as previewed" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Not run, nothing applied: different objects match; matched objects changed.");
    expect(alert).toHaveTextContent("1 added, 0 removed, 2 changed since the preview");
    expect(onRan).not.toHaveBeenCalled();
  });

  it("explains a preview that was already used", async () => {
    const { ApiError } = await import("../lib/http");
    storePlaybookDryRun.mockResolvedValue(preview());
    runPlaybookAsPreviewed.mockRejectedValue(new ApiError(409, "PLAYBOOK_DRY_RUN_ALREADY_BOUND"));
    await renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Preview (dry run)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run as previewed" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("This preview has already been used for a run"),
    );
  });
});
