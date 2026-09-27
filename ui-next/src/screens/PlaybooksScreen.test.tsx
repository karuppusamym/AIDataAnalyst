import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { PlaybooksScreen, PLAYBOOK_UNSAVED_MESSAGE } from "./PlaybooksScreen";
import { pendingUnsavedWarning, resetUnsavedRegistryForTests } from "../lib/unsavedChanges";

const fetchPlaybooks = vi.fn();
const createPlaybook = vi.fn();
const updatePlaybook = vi.fn();
const deletePlaybook = vi.fn();
const runPlaybookNow = vi.fn();
const listOrgDatasources = vi.fn();
const storePlaybookDryRun = vi.fn();
const runPlaybookAsPreviewed = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchPlaybooks: (...args: unknown[]) => fetchPlaybooks(...args),
    createPlaybook: (...args: unknown[]) => createPlaybook(...args),
    updatePlaybook: (...args: unknown[]) => updatePlaybook(...args),
    deletePlaybook: (...args: unknown[]) => deletePlaybook(...args),
    runPlaybookNow: (...args: unknown[]) => runPlaybookNow(...args),
    listOrgDatasources: (...args: unknown[]) => listOrgDatasources(...args),
  };
});

vi.mock("../lib/api/playbookDryRuns", async () => {
  const actual = await vi.importActual<typeof import("../lib/api/playbookDryRuns")>(
    "../lib/api/playbookDryRuns",
  );
  return {
    ...actual,
    storePlaybookDryRun: (...args: unknown[]) => storePlaybookDryRun(...args),
    runPlaybookAsPreviewed: (...args: unknown[]) => runPlaybookAsPreviewed(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

const PLAYBOOK_TAG = {
  id: "aaaaaaaa-1111-1111-1111-111111111111",
  organization_id: ORG,
  name: "Tag staging tables",
  action: "TAG",
  datasource_id: "10000000-0000-0000-0000-000000000001",
  match_field: "TABLE_NAME",
  match_pattern: "stg_%",
  column_name_pattern: null,
  action_parameters: { tag_key: "needs-review" },
  schedule_interval_minutes: 60,
  auto_apply_max_items: 50,
  enabled: true,
  created_by: "priya.steward",
  last_run_at: "2026-09-04T00:00:00.000Z",
  created_at: "2026-08-01T00:00:00.000Z",
  updated_at: "2026-09-04T00:00:00.000Z",
};

const PLAYBOOK_OWN_DISABLED = {
  id: "bbbbbbbb-2222-2222-2222-222222222222",
  organization_id: ORG,
  name: "Assign finance ownership",
  action: "OWN",
  datasource_id: "10000000-0000-0000-0000-000000000002",
  match_field: "QUALIFIED_NAME",
  match_pattern: "finance.%",
  column_name_pattern: null,
  action_parameters: { owner_type: "GROUP", owner_principal: "finance-data-team" },
  schedule_interval_minutes: 720,
  auto_apply_max_items: 0,
  enabled: false,
  created_by: "raj.admin",
  last_run_at: null,
  created_at: "2026-07-01T00:00:00.000Z",
  updated_at: "2026-07-20T00:00:00.000Z",
};

const DATASOURCE = {
  id: "10000000-0000-0000-0000-000000000001",
  name: "Primary warehouse",
  connector_type: "SNOWFLAKE",
  dialect: "SNOWFLAKE",
  environment: "PROD",
  credential_reference: "vault://ds1",
  organization_id: ORG,
  line_of_business_id: "lob-1",
  data_domain_id: "dom-1",
  project_id: "proj-1",
  status: "ENABLED",
  capabilities: {},
  created_at: "2026-01-01T00:00:00.000Z",
  updated_at: "2026-01-01T00:00:00.000Z",
};

describe("PlaybooksScreen (AT-1)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchPlaybooks.mockResolvedValue({ items: [PLAYBOOK_TAG, PLAYBOOK_OWN_DISABLED], limit: 100, offset: 0, total: 2 });
    listOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
    resetUnsavedRegistryForTests();
  });

  afterEach(() => {
    resetUnsavedRegistryForTests();
  });

  it("lists existing playbooks with action, schedule and enabled state", async () => {
    render(<PlaybooksScreen />);

    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());
    const tagRow = screen.getByText("Tag staging tables").closest("li")!;
    expect(within(tagRow).getByText("TAG")).toBeInTheDocument();
    expect(within(tagRow).getByText("every 60m")).toBeInTheDocument();

    expect(screen.getByText("Assign finance ownership")).toBeInTheDocument();
    const ownRow = screen.getByText("Assign finance ownership").closest("li")!;
    expect(within(ownRow).getByText("disabled")).toBeInTheDocument();
  });

  it("shows an empty state when there are no playbooks", async () => {
    fetchPlaybooks.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("No playbooks yet.")).toBeInTheDocument());
  });

  it("renders an error state rather than a blank screen", async () => {
    fetchPlaybooks.mockRejectedValue(new Error("boom"));
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText(/playbooks could not be loaded/i)).toBeInTheDocument());
  });

  it("creates a playbook from the filled-in form", async () => {
    createPlaybook.mockResolvedValue({
      ...PLAYBOOK_TAG,
      id: "cccccccc-3333-3333-3333-333333333333",
      name: "New rule",
    });
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Create playbook", { selector: "summary" }));
    await waitFor(() => expect(listOrgDatasources).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New rule" } });
    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: DATASOURCE.id } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "stg_%" } });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "needs-review" } });

    fireEvent.click(screen.getByRole("button", { name: "Create playbook" }));

    await waitFor(() =>
      expect(createPlaybook).toHaveBeenCalledWith(
        ORG,
        expect.objectContaining({
          name: "New rule",
          action: "TAG",
          datasource_id: DATASOURCE.id,
          match_field: "TABLE_NAME",
          match_pattern: "stg_%",
          action_parameters: { tag_key: "needs-review" },
          schedule_interval_minutes: 60,
          auto_apply_max_items: 0,
          enabled: true,
        }),
      ),
    );
    await waitFor(() => expect(screen.getByText("New rule")).toBeInTheDocument());
  });

  /* ---------------------------------------------------------------------
     R11-S13: the create form's unsaved-work reporting.

     Automation (this screen) is a view of the Stewardship workspace now, so
     an in-progress draft has to survive the same tab switch the bulk form's
     `edited` flag already guards (`StewardshipScreen.test.tsx`'s "the bulk
     form reports unsaved work"). These cases pin the reporting into the
     same registry; the tab bar's side of the guard is
     `StewardshipWorkspace.test.tsx`'s.
  --------------------------------------------------------------------- */
  it("reports an edited, unsubmitted draft, and stops once it is created", async () => {
    createPlaybook.mockResolvedValue({ ...PLAYBOOK_TAG, id: "new-1", name: "New rule" });
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());
    expect(pendingUnsavedWarning()).toBeNull();

    fireEvent.click(screen.getByText("Create playbook", { selector: "summary" }));
    await waitFor(() => expect(listOrgDatasources).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New rule" } });
    expect(pendingUnsavedWarning()).toBe(PLAYBOOK_UNSAVED_MESSAGE);

    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: DATASOURCE.id } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "stg_%" } });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "needs-review" } });
    fireEvent.click(screen.getByRole("button", { name: "Create playbook" }));

    await waitFor(() => expect(createPlaybook).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(pendingUnsavedWarning()).toBeNull());
  });

  it("keeps reporting when the create call fails, because the draft is still unfinished work", async () => {
    createPlaybook.mockRejectedValue(new Error("422: match_pattern is required"));
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Create playbook", { selector: "summary" }));
    await waitFor(() => expect(listOrgDatasources).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New rule" } });
    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: DATASOURCE.id } });
    fireEvent.change(screen.getByLabelText("Match pattern"), { target: { value: "stg_%" } });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "needs-review" } });
    fireEvent.click(screen.getByRole("button", { name: "Create playbook" }));

    await waitFor(() => expect(screen.getByText(/match_pattern is required/)).toBeInTheDocument());
    expect(pendingUnsavedWarning()).toBe(PLAYBOOK_UNSAVED_MESSAGE);
  });

  it("runs a playbook now and reports the outcome", async () => {
    runPlaybookNow.mockResolvedValue({
      playbook_id: PLAYBOOK_TAG.id,
      matched_count: 5,
      outcome: "GOVERNANCE_REVIEW_QUEUED",
      bulk_action_run_id: null,
      bulk_stewardship_operation_id: null,
      governance_review_id: "dddddddd-4444-4444-4444-444444444444",
    });
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());

    fireEvent.click(screen.getAllByRole("button", { name: "Run now" })[0]!);

    await waitFor(() => expect(runPlaybookNow).toHaveBeenCalledWith(PLAYBOOK_TAG.id));
    await waitFor(() =>
      expect(screen.getByText(/matched 5 object\(s\) — governance review queued/i)).toBeInTheDocument(),
    );
  });

  it("does not offer Run now for a disabled playbook", async () => {
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Assign finance ownership")).toBeInTheDocument());
    const row = screen.getByText("Assign finance ownership").closest("li")!;
    expect(row.querySelector("button")).not.toBeNull();
    const runButtons = screen.getAllByRole("button", { name: "Run now" });
    // Second row's Run now button belongs to the disabled playbook and is disabled.
    expect(runButtons[1]).toBeDisabled();
  });

  it("toggles a playbook's enabled state", async () => {
    updatePlaybook.mockResolvedValue({ ...PLAYBOOK_TAG, enabled: false });
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());

    fireEvent.click(screen.getAllByRole("button", { name: "Disable" })[0]!);

    await waitFor(() => expect(updatePlaybook).toHaveBeenCalledWith(PLAYBOOK_TAG.id, { enabled: false }));
  });

  /* The confirmation is a real dialog, not `window.confirm`
     (review 2026-09-05, F21): it names the playbook, is dismissible with
     Escape, and stays open to report a delete the server refused. */
  async function openDeleteDialog() {
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]!);
    return await screen.findByRole("dialog");
  }

  it("asks for confirmation before deleting and skips the call when declined", async () => {
    const dialog = await openDeleteDialog();

    expect(within(dialog).getByText(/Tag staging tables/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(deletePlaybook).not.toHaveBeenCalled();
    expect(screen.getByText("Tag staging tables")).toBeInTheDocument();
  });

  it("deletes a playbook after confirmation and removes it from the list", async () => {
    deletePlaybook.mockResolvedValue(undefined);
    const dialog = await openDeleteDialog();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete playbook" }));

    await waitFor(() => expect(deletePlaybook).toHaveBeenCalledWith(PLAYBOOK_TAG.id));
    await waitFor(() => expect(screen.queryByText("Tag staging tables")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens a row's dry run, and a run bound to it updates the row like Run now", async () => {
    storePlaybookDryRun.mockResolvedValue({
      playbook_id: PLAYBOOK_TAG.id,
      action: "TAG",
      enabled: true,
      rule_version: "1".repeat(64),
      evaluated_at: "2026-09-19T00:00:00Z",
      matched_count: 0,
      tables_truncated: false,
      columns_truncated: false,
      auto_apply_max_items: 50,
      predicted_disposition: "NO_MATCHES",
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
      items: [],
      dry_run_id: "dry-1",
      match_digest: "c".repeat(64),
      evidence_digest: "d".repeat(64),
      change_counts: {},
    });
    runPlaybookAsPreviewed.mockResolvedValue({
      dry_run_id: "dry-1",
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
        playbook_id: PLAYBOOK_TAG.id,
        matched_count: 0,
        outcome: "NO_MATCHES",
        bulk_action_run_id: null,
        bulk_stewardship_operation_id: null,
        governance_review_id: null,
      },
    });
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());
    const tagRow = screen.getByText("Tag staging tables").closest("li")!;
    // Nothing is fetched until a steward opens a row's dry run.
    expect(storePlaybookDryRun).not.toHaveBeenCalled();
    fireEvent.click(within(tagRow).getByRole("button", { name: "Dry run…" }));
    const panel = within(tagRow).getByRole("region", { name: "Dry run of Tag staging tables" });
    fireEvent.click(within(panel).getByRole("button", { name: "Preview (dry run)" }));
    await within(panel).findByText(/Would do nothing: no object matches the rule/);
    expect(storePlaybookDryRun).toHaveBeenCalledWith(PLAYBOOK_TAG.id);

    fireEvent.click(within(panel).getByRole("button", { name: "Run as previewed" }));
    await waitFor(() =>
      expect(screen.getByText(/"Tag staging tables" matched 0 object\(s\) — no matches/)).toBeInTheDocument(),
    );
    expect(runPlaybookNow).not.toHaveBeenCalled();
    // The row's last run moves, exactly as it does after Run now.
    expect(within(tagRow).getByText("just now")).toBeInTheDocument();
  });

  it("does not dismiss on a backdrop click, because the action is destructive", async () => {
    const dialog = await openDeleteDialog();

    fireEvent.mouseDown(dialog.parentElement!);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(deletePlaybook).not.toHaveBeenCalled();
  });
});
