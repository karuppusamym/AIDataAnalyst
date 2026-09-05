import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { PlaybooksScreen } from "./PlaybooksScreen";

const fetchPlaybooks = vi.fn();
const createPlaybook = vi.fn();
const updatePlaybook = vi.fn();
const deletePlaybook = vi.fn();
const runPlaybookNow = vi.fn();
const fetchOrgDatasources = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchPlaybooks: (...args: unknown[]) => fetchPlaybooks(...args),
    createPlaybook: (...args: unknown[]) => createPlaybook(...args),
    updatePlaybook: (...args: unknown[]) => updatePlaybook(...args),
    deletePlaybook: (...args: unknown[]) => deletePlaybook(...args),
    runPlaybookNow: (...args: unknown[]) => runPlaybookNow(...args),
    fetchOrgDatasources: (...args: unknown[]) => fetchOrgDatasources(...args),
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
    fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
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
    await waitFor(() => expect(fetchOrgDatasources).toHaveBeenCalled());

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

  it("asks for confirmation before deleting and skips the call when declined", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]!);

    expect(confirmSpy).toHaveBeenCalled();
    expect(deletePlaybook).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("deletes a playbook after confirmation and removes it from the list", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    deletePlaybook.mockResolvedValue(undefined);
    render(<PlaybooksScreen />);
    await waitFor(() => expect(screen.getByText("Tag staging tables")).toBeInTheDocument());

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]!);

    await waitFor(() => expect(deletePlaybook).toHaveBeenCalledWith(PLAYBOOK_TAG.id));
    await waitFor(() => expect(screen.queryByText("Tag staging tables")).not.toBeInTheDocument());
    confirmSpy.mockRestore();
  });
});
