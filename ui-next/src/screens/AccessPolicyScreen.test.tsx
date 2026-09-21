import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AccessPolicyCreate, AuthorizationSimulationRequest, WorkspaceRead } from "../lib/types";
import type { AccessPolicyRead, PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   ABAC access policies + authorization simulation, ported from the legacy
   portal's `renderPolicy()` / `#abac-policy-form` / `#abac-simulate-form`
   onto the real `workspace_api.py` routes. Mocks the API boundary the same
   way every other UX-15 screen test does (`ToolRegistryScreen.test.tsx`).
--------------------------------------------------------------------------- */

const fetchAccessPolicies =
  vi.fn<(organizationId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<AccessPolicyRead>>>();
const createAccessPolicy =
  vi.fn<(organizationId: string, body: AccessPolicyCreate, signal?: AbortSignal) => Promise<AccessPolicyRead>>();
const fetchOrgWorkspaces = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<WorkspaceRead>>>();
const simulateAuthorization =
  vi.fn<
    (
      workspaceId: string,
      body: AuthorizationSimulationRequest,
      signal?: AbortSignal,
    ) => Promise<{ workspace_id: string; decisions: unknown[] }>
  >();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchAccessPolicies: (organizationId: string, query: unknown, signal?: AbortSignal) =>
      fetchAccessPolicies(organizationId, query, signal),
    createAccessPolicy: (organizationId: string, body: AccessPolicyCreate, signal?: AbortSignal) =>
      createAccessPolicy(organizationId, body, signal),
    fetchOrgWorkspaces: (organizationId: string, signal?: AbortSignal) => fetchOrgWorkspaces(organizationId, signal),
    simulateAuthorization: (workspaceId: string, body: AuthorizationSimulationRequest, signal?: AbortSignal) =>
      simulateAuthorization(workspaceId, body, signal),
  };
});

const ORG_ID = "00000000-0000-0000-0000-000000000001";

const POLICY: AccessPolicyRead = {
  id: "policy_1", organization_id: ORG_ID, code: "mask-pii-columns", version: 2,
  name: "Mask PII columns for analysts", description: "Masks direct identifiers.",
  effect: "MASK", priority: 50,
  subject_match: { roles_not_in: ["DataSteward"] }, resource_match: { classifications: ["PII"] },
  action_match: ["READ_DATA", "EXPORT"], transform: { strategy: "HASH" }, condition: {},
  origin: "MANUAL", status: "ACTIVE",
  created_by: "local-ui-admin", created_at: "2026-02-01T00:00:00Z", updated_at: "2026-06-15T00:00:00Z",
};

const WORKSPACE: WorkspaceRead = {
  id: "ws_governed_analytics", organization_id: ORG_ID,
  name: "Governed analytics", slug: "governed-analytics", purpose: "Curated workspace.",
  status: "ACTIVE", monthly_cost_ceiling: null,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

async function loadScreen() {
  const { AccessPolicyScreen } = await import("./AccessPolicyScreen");
  return AccessPolicyScreen;
}

beforeEach(() => {
  fetchAccessPolicies.mockReset();
  createAccessPolicy.mockReset();
  fetchOrgWorkspaces.mockReset();
  simulateAuthorization.mockReset();
  fetchAccessPolicies.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  fetchOrgWorkspaces.mockResolvedValue({ items: [WORKSPACE], limit: 200, offset: 0, total: 1 });
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AccessPolicyScreen against the real workspace_api.py routes", () => {
  it("loads the access policy list for the current organization on mount", async () => {
    fetchAccessPolicies.mockResolvedValue({ items: [POLICY], limit: 200, offset: 0, total: 1 });
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    await waitFor(() => expect(fetchAccessPolicies).toHaveBeenCalledWith(ORG_ID, { limit: 200 }, expect.anything()));
    expect(await screen.findByText("mask-pii-columns")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("MASK", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("ACTIVE", { selector: "span" })).toBeInTheDocument();
  });

  it("shows the empty-state copy when there are no policies", async () => {
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    expect(await screen.findByText("No access policies")).toBeInTheDocument();
  });

  it("surfaces the real list failure without hiding it", async () => {
    fetchAccessPolicies.mockRejectedValue(new ApiError(403, "Reviewer may list access policies but not create them"));
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    expect(await screen.findByText("Reviewer may list access policies but not create them")).toBeInTheDocument();
  });

  it("creates a policy as a DRAFT proposal and says a second person has to approve it", async () => {
    createAccessPolicy.mockResolvedValue({ ...POLICY, id: "policy_2", code: "deny-restricted-export", status: "DRAFT" });
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "deny-restricted-export" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Deny restricted export" } });
    fireEvent.change(screen.getByLabelText("Actions (comma-separated, blank = all)"), { target: { value: "EXPORT, READ_DATA" } });
    fireEvent.click(screen.getByRole("button", { name: "Create policy" }));

    await waitFor(() =>
      expect(createAccessPolicy).toHaveBeenCalledWith(
        ORG_ID,
        expect.objectContaining({
          code: "deny-restricted-export",
          name: "Deny restricted export",
          effect: "ALLOW",
          priority: 100,
          action_match: ["EXPORT", "READ_DATA"],
          subject_match: {},
          resource_match: {},
          transform: {},
          condition: {},
          status: "DRAFT",
        }),
        undefined,
      ),
    );
    // R11-AUD02: honest about what a create is -- a proposal on the Review queue, enforcing nothing
    // until someone other than the proposer approves it.
    const saved = await screen.findByText(/Policy "deny-restricted-export" saved as a draft/);
    expect(saved).toHaveTextContent("It is on the Review queue now");
    expect(saved).toHaveTextContent("someone other than you has to approve it before it is enforced");
    // Reloads the list after a successful create.
    await waitFor(() => expect(fetchAccessPolicies).toHaveBeenCalledTimes(2));
  });

  it("offers no way to activate a policy at creation, and never sends status ACTIVE", async () => {
    // The server refuses `status: "ACTIVE"` with a 422 (R11-AUD02), so the old "Activate immediately"
    // toggle is gone: a control that can only fail is worse than no control.
    createAccessPolicy.mockResolvedValue({ ...POLICY, status: "DRAFT" });
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    expect(screen.queryByLabelText(/Activate immediately/)).not.toBeInTheDocument();
    expect(screen.getByText(/enforces nothing\. It goes to the Review queue/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "mask-pii-columns" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Mask PII columns" } });
    fireEvent.click(screen.getByRole("button", { name: "Create policy" }));

    await waitFor(() =>
      expect(createAccessPolicy).toHaveBeenCalledWith(
        ORG_ID,
        expect.objectContaining({ status: "DRAFT" }),
        undefined,
      ),
    );
    expect(createAccessPolicy.mock.calls[0]?.[1].status).not.toBe("ACTIVE");
  });

  it("shows a rejected proposal as REJECTED, not as a policy that is merely inactive", async () => {
    fetchAccessPolicies.mockResolvedValue({
      items: [{ ...POLICY, id: "policy_3", code: "deny-all", status: "REJECTED" }],
      limit: 200,
      offset: 0,
      total: 1,
    });
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    expect(await screen.findByText("REJECTED", { selector: "span" })).toBeInTheDocument();
  });

  it("surfaces the server's refusal when a create is rejected", async () => {
    createAccessPolicy.mockRejectedValue(new ApiError(403, "role not permitted"));
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "mask-pii-columns" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Mask PII columns" } });
    fireEvent.click(screen.getByRole("button", { name: "Create policy" }));

    expect(await screen.findByText("role not permitted")).toBeInTheDocument();
    expect(screen.queryByText(/saved as a draft/)).not.toBeInTheDocument();
  });

  it("rejects invalid JSON in a match field client-side without calling the API", async () => {
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "bad-json-policy" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bad JSON policy" } });
    fireEvent.change(screen.getByLabelText("Subject match (JSON object)"), { target: { value: "{not json" } });
    fireEvent.click(screen.getByRole("button", { name: "Create policy" }));

    expect(await screen.findByText("Subject match must be valid JSON.")).toBeInTheDocument();
    expect(createAccessPolicy).not.toHaveBeenCalled();
  });

  it("runs a simulation against the selected workspace and renders the returned decisions", async () => {
    simulateAuthorization.mockResolvedValue({
      workspace_id: "ws_governed_analytics",
      decisions: [
        {
          principal_kind: "HUMAN", roles: ["Analyst"], allowed: true, reason_code: "POLICY_MASK",
          matched_policy_code: "mask-pii-columns", masked_classifications: ["PII"], row_filters: [],
        },
      ],
    });
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    await waitFor(() => expect(screen.getByText("Governed analytics")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Resource type"), { target: { value: "table" } });
    fireEvent.click(screen.getByRole("button", { name: "Run simulation" }));

    await waitFor(() =>
      expect(simulateAuthorization).toHaveBeenCalledWith(
        "ws_governed_analytics",
        expect.objectContaining({ workspace_id: "ws_governed_analytics", resource_type: "table", action: "READ_DATA" }),
        undefined,
      ),
    );
    expect(await screen.findByText("POLICY_MASK")).toBeInTheDocument();
    expect(screen.getByText("ALLOWED")).toBeInTheDocument();
    expect(screen.getByText("mask-pii-columns")).toBeInTheDocument();
  });

  it("requires subjects to be a non-empty JSON array before running a simulation", async () => {
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    await waitFor(() => expect(screen.getByText("Governed analytics")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Resource type"), { target: { value: "table" } });
    fireEvent.change(screen.getByLabelText("Subjects (JSON array, 1-25 entries)"), { target: { value: "[]" } });
    fireEvent.click(screen.getByRole("button", { name: "Run simulation" }));

    expect(await screen.findByText("Subjects must be a JSON array with at least one entry.")).toBeInTheDocument();
    expect(simulateAuthorization).not.toHaveBeenCalled();
  });

  it("surfaces the real simulation failure through the same ApiError detail path", async () => {
    simulateAuthorization.mockRejectedValue(new ApiError(422, "workspace_id in body must match the path parameter"));
    const AccessPolicyScreen = await loadScreen();
    render(<AccessPolicyScreen />);

    await waitFor(() => expect(screen.getByText("Governed analytics")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Resource type"), { target: { value: "table" } });
    fireEvent.click(screen.getByRole("button", { name: "Run simulation" }));

    expect(await screen.findByText("workspace_id in body must match the path parameter")).toBeInTheDocument();
  });
});
