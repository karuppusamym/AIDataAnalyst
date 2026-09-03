import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  ExecutionRead,
  ToolPlanCreate,
  ToolPlanDetailRead,
  ToolPlanRead,
  ValidationResponse,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Tool plans, ported from the legacy portal's `#tool-plan-form` /
   `plan-validate` / `plan-execute` / `plan-cancel` / `plan-evidence`
   bindings onto the real `tool_plans_api.py` routes. Mocks the API boundary
   the same way every other screen test does
   (`ToolRegistryScreen.test.tsx`, `ContextProductsScreen.test.tsx`).
--------------------------------------------------------------------------- */

const createToolPlan = vi.fn<(body: ToolPlanCreate, signal?: AbortSignal) => Promise<ToolPlanRead>>();
const fetchToolPlan = vi.fn<(planId: string, signal?: AbortSignal) => Promise<ToolPlanDetailRead>>();
const validateToolPlan = vi.fn<(planId: string, signal?: AbortSignal) => Promise<ValidationResponse>>();
const executeToolPlan = vi.fn<(planId: string, signal?: AbortSignal) => Promise<ExecutionRead>>();
const cancelToolPlan = vi.fn<(planId: string, signal?: AbortSignal) => Promise<ToolPlanRead>>();
const fetchToolPlanEvidence =
  vi.fn<(planId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<ExecutionRead>>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    createToolPlan: (body: ToolPlanCreate, signal?: AbortSignal) => createToolPlan(body, signal),
    fetchToolPlan: (planId: string, signal?: AbortSignal) => fetchToolPlan(planId, signal),
    validateToolPlan: (planId: string, signal?: AbortSignal) => validateToolPlan(planId, signal),
    executeToolPlan: (planId: string, signal?: AbortSignal) => executeToolPlan(planId, signal),
    cancelToolPlan: (planId: string, signal?: AbortSignal) => cancelToolPlan(planId, signal),
    fetchToolPlanEvidence: (planId: string, query: unknown, signal?: AbortSignal) =>
      fetchToolPlanEvidence(planId, query, signal),
  };
});

const PLAN: ToolPlanRead = {
  id: "plan_1",
  organization_id: "org1",
  name: "Nightly delinquency remediation",
  budget: { max_steps: 20, max_time_seconds: 600, max_tokens: 100000, max_cost_units: 100 },
  status: "DRAFT",
  created_by: "local-ui-admin",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const PLAN_DETAIL: ToolPlanDetailRead = {
  ...PLAN,
  steps: [
    {
      id: "plan_1_step_1", plan_id: "plan_1", sequence: 1,
      tool_id: "t_delinquency", tool_version: "1",
      parameters: {}, dependencies: [], timeout_seconds: 300, expected_cost: 0,
      status: "PENDING", started_at: null, completed_at: null, evidence: {}, error_message: null,
    },
  ],
};

const EXECUTION: ExecutionRead = {
  id: "exec_1",
  organization_id: "org1",
  plan_id: "plan_1",
  started_at: "2026-09-01T00:00:00Z",
  completed_at: "2026-09-01T00:00:05Z",
  budget_consumed: { cost_units: 1.5 },
  status: "COMPLETED",
  executed_by: "local-ui-admin",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:05Z",
};

async function loadScreen() {
  const { ToolPlansScreen } = await import("./ToolPlansScreen");
  return ToolPlansScreen;
}

function fillMinimalCreateForm() {
  fireEvent.change(screen.getByLabelText("Plan name"), { target: { value: "Nightly delinquency remediation" } });
  fireEvent.change(screen.getByLabelText("Tool id"), { target: { value: "t_delinquency" } });
  fireEvent.change(screen.getByLabelText("Tool version"), { target: { value: "1" } });
}

beforeEach(() => {
  createToolPlan.mockReset();
  fetchToolPlan.mockReset();
  validateToolPlan.mockReset();
  executeToolPlan.mockReset();
  cancelToolPlan.mockReset();
  fetchToolPlanEvidence.mockReset();
  fetchToolPlanEvidence.mockResolvedValue({ items: [], limit: 20, offset: 0, total: 0 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ToolPlansScreen against the real tool_plans_api.py routes", () => {
  it("shows the no-plan-selected empty state and fetches nothing", async () => {
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    expect(await screen.findByText("No plan selected")).toBeInTheDocument();
    expect(fetchToolPlan).not.toHaveBeenCalled();
  });

  it("creates a single-step plan with the exact body shape create_tool_plan expects, then loads its detail", async () => {
    createToolPlan.mockResolvedValue(PLAN);
    fetchToolPlan.mockResolvedValue(PLAN_DETAIL);
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    fillMinimalCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));

    await waitFor(() => expect(createToolPlan).toHaveBeenCalledTimes(1));
    const [body] = createToolPlan.mock.calls[0]!;
    expect(body).toEqual({
      name: "Nightly delinquency remediation",
      steps: [
        {
          sequence: 1, tool_id: "t_delinquency", tool_version: "1",
          parameters: {}, dependencies: [], timeout_seconds: 300, expected_cost: 0,
        },
      ],
      budget: { max_steps: 20, max_time_seconds: 600, max_tokens: 100000, max_cost_units: 100 },
    });

    await waitFor(() => expect(fetchToolPlan).toHaveBeenCalledWith("plan_1", expect.anything()));
    expect(await screen.findByText("Nightly delinquency remediation")).toBeInTheDocument();
    expect(screen.getByText("DRAFT")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("plan")).toBe("plan_1");
  });

  it("rejects non-JSON parameters client-side before calling the API", async () => {
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    fillMinimalCreateForm();
    fireEvent.change(screen.getByLabelText("Parameters (JSON)"), { target: { value: "{not json" } });
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));

    expect(await screen.findByText("Parameters must be valid JSON.")).toBeInTheDocument();
    expect(createToolPlan).not.toHaveBeenCalled();
  });

  it("validates a plan, shows no issues, and reflects the plan moving to VALIDATED", async () => {
    history.replaceState(null, "", "/?plan=plan_1");
    fetchToolPlan
      .mockResolvedValueOnce(PLAN_DETAIL)
      .mockResolvedValueOnce({ ...PLAN_DETAIL, status: "VALIDATED" });
    validateToolPlan.mockResolvedValue({ valid: true, issues: [] });
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    await screen.findByText("Nightly delinquency remediation");
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    await waitFor(() => expect(validateToolPlan).toHaveBeenCalledWith("plan_1", undefined));
    expect(await screen.findByText("Valid — no issues found")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("VALIDATED")).toBeInTheDocument());
  });

  it("renders every validation issue with its severity when the plan is not valid", async () => {
    history.replaceState(null, "", "/?plan=plan_1");
    fetchToolPlan.mockResolvedValue(PLAN_DETAIL);
    validateToolPlan.mockResolvedValue({
      valid: false,
      issues: [
        { step_sequence: 1, issue: "depends on step 2, which is not part of this plan", severity: "ERROR" },
      ],
    });
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    await screen.findByText("Nightly delinquency remediation");
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    expect(await screen.findByText("Not valid — issues found")).toBeInTheDocument();
    expect(screen.getByText(/depends on step 2/)).toBeInTheDocument();
    expect(screen.getByText("ERROR")).toBeInTheDocument();
  });

  it("executes a plan through the real execute endpoint and shows the run in evidence", async () => {
    history.replaceState(null, "", "/?plan=plan_1");
    fetchToolPlan.mockResolvedValue(PLAN_DETAIL);
    executeToolPlan.mockResolvedValue(EXECUTION);
    fetchToolPlanEvidence
      .mockResolvedValueOnce({ items: [], limit: 20, offset: 0, total: 0 })
      .mockResolvedValueOnce({ items: [EXECUTION], limit: 20, offset: 0, total: 1 });
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    await screen.findByText("Nightly delinquency remediation");
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));

    await waitFor(() => expect(executeToolPlan).toHaveBeenCalledWith("plan_1", undefined));
    expect(await screen.findByText("exec_1")).toBeInTheDocument();
    expect(screen.getByText("Evidence (1)")).toBeInTheDocument();
  });

  it("disables Execute once the plan is already COMPLETED, matching the server's own 409 rule", async () => {
    history.replaceState(null, "", "/?plan=plan_1");
    fetchToolPlan.mockResolvedValue({ ...PLAN_DETAIL, status: "COMPLETED" });
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    await screen.findByText("Nightly delinquency remediation");
    expect(screen.getByRole("button", { name: "Execute" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  });

  it("shows a distinct message for an edition-entitlement denial, not a generic Forbidden", async () => {
    createToolPlan.mockRejectedValueOnce(new ApiError(403, "ENTITLEMENT_EDITION_INSUFFICIENT"));
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    fillMinimalCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));

    expect(await screen.findByText(/does not include multi-step tool plans/)).toBeInTheDocument();
  });

  it("shows the server's own role-denial message unchanged, distinct from the entitlement case", async () => {
    createToolPlan.mockRejectedValueOnce(
      new ApiError(403, "one of these roles is required: DataEngineer, PlatformAdmin, ToolDeveloper"),
    );
    const ToolPlansScreen = await loadScreen();
    render(<ToolPlansScreen />);

    fillMinimalCreateForm();
    fireEvent.click(screen.getByRole("button", { name: "Create plan" }));

    expect(
      await screen.findByText("one of these roles is required: DataEngineer, PlatformAdmin, ToolDeveloper"),
    ).toBeInTheDocument();
  });
});
