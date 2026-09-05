import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AgentEvaluationRunRead, AiRuntimeStatusRead, ModelRouteConfigurationCreate, ModelRouteConfigurationRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   AI governance, ported from the legacy `agents-view` onto the real,
   already-merged `ai_governance_api.py` model-route routes plus
   `api.py`'s `/ai/runtime-status` and `/agent-evaluations` routes. Mocks the
   API boundary the same way every other UX-15 screen test does
   (`StudioChangeSetsScreen.test.tsx`, `ContextProductsScreen.test.tsx`).
--------------------------------------------------------------------------- */

const fetchAiRuntimeStatus = vi.fn<(signal?: AbortSignal) => Promise<AiRuntimeStatusRead>>();
const fetchModelRoutes =
  vi.fn<(organizationId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<ModelRouteConfigurationRead>>>();
const createModelRoute =
  vi.fn<(organizationId: string, body: ModelRouteConfigurationCreate, signal?: AbortSignal) => Promise<ModelRouteConfigurationRead>>();
const submitModelRoute = vi.fn<(routeId: string, signal?: AbortSignal) => Promise<unknown>>();
const fetchAgentEvaluations =
  vi.fn<(organizationId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<AgentEvaluationRunRead>>>();
const runAgentEvaluation = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<AgentEvaluationRunRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchAiRuntimeStatus: (signal?: AbortSignal) => fetchAiRuntimeStatus(signal),
    fetchModelRoutes: (organizationId: string, query: unknown, signal?: AbortSignal) =>
      fetchModelRoutes(organizationId, query, signal),
    createModelRoute: (organizationId: string, body: ModelRouteConfigurationCreate, signal?: AbortSignal) =>
      createModelRoute(organizationId, body, signal),
    submitModelRoute: (routeId: string, signal?: AbortSignal) => submitModelRoute(routeId, signal),
    fetchAgentEvaluations: (organizationId: string, query: unknown, signal?: AbortSignal) =>
      fetchAgentEvaluations(organizationId, query, signal),
    runAgentEvaluation: (organizationId: string, signal?: AbortSignal) => runAgentEvaluation(organizationId, signal),
  };
});

const RUNTIME: AiRuntimeStatusRead = {
  orchestration_mode: "HYBRID", runtime: "FRAMEWORK_NEUTRAL_TYPED_STATE_MACHINE", runtime_version: "v2",
  model_route_status: "CONFIGURED", model_generation_enabled: true, available_model_providers: ["OPENAI"],
  development_sql_override_enabled: false, identity_provider: "DEVELOPMENT",
  identity_verification: "DEVELOPMENT_HEADERS_ONLY", oidc_configured: false,
  credential_provider: "ENV", credential_provider_available: true, enterprise_security_ready: false,
  deterministic_controls: ["authorization"], optional_framework_adapters: [],
  data_retention_statement: "Gateway-only.",
};

const DRAFT_ROUTE: ModelRouteConfigurationRead = {
  id: "route_1", organization_id: "org1", route_key: "bank-sql-primary", version: 1, status: "DRAFT",
  display_name: "Bank SQL generation", provider_type: "OPENAI", model_id: "approved-deployment-alias",
  endpoint_alias: "private-ai-east-01", uses_credential_reference: true, data_residency: "US",
  retention_policy: "ZERO_RETENTION", capabilities: ["SQL_GENERATION", "CLASSIFICATION"],
  max_input_tokens: 8000, max_output_tokens: 2000, timeout_seconds: 30,
  fingerprint: "a".repeat(64), created_by: "local-ui-admin", approved_by: null, approved_at: null,
  selected_by_runtime: false, adapter_available: false, activation_status: "DRAFT",
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
};

async function loadScreen() {
  const { AiGovernanceScreen } = await import("./AiGovernanceScreen");
  return AiGovernanceScreen;
}

beforeEach(() => {
  fetchAiRuntimeStatus.mockReset();
  fetchModelRoutes.mockReset();
  createModelRoute.mockReset();
  submitModelRoute.mockReset();
  fetchAgentEvaluations.mockReset();
  runAgentEvaluation.mockReset();
  fetchAiRuntimeStatus.mockResolvedValue(RUNTIME);
  fetchModelRoutes.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  fetchAgentEvaluations.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AiGovernanceScreen against the real ai_governance_api.py / api.py routes", () => {
  it("loads runtime status and the model route registry from the real endpoints", async () => {
    fetchModelRoutes.mockResolvedValue({ items: [DRAFT_ROUTE], limit: 200, offset: 0, total: 1 });
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);

    await waitFor(() => expect(screen.getByText("Bank SQL generation")).toBeInTheDocument());
    expect(fetchModelRoutes).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      { limit: 200 },
      expect.anything(),
    );
    expect(fetchAiRuntimeStatus).toHaveBeenCalledTimes(1);
    // Runtime tiles render the real, humanized status values.
    expect(await screen.findByText("hybrid")).toBeInTheDocument();
    expect(screen.getByText("development headers only")).toBeInTheDocument();
  });

  it("shows the legacy empty copy when there are no model routes yet", async () => {
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);

    expect(await screen.findByText("No governed model route definitions")).toBeInTheDocument();
  });

  it("creates a governed draft with the exact field mapping the real create endpoint expects", async () => {
    createModelRoute.mockResolvedValue(DRAFT_ROUTE);
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);
    await screen.findByText("No governed model route definitions");

    fireEvent.change(screen.getByLabelText("Route key"), { target: { value: "bank-sql-primary" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Bank SQL generation" } });
    fireEvent.change(screen.getByLabelText("Deployment alias"), { target: { value: "approved-deployment-alias" } });
    fireEvent.change(screen.getByLabelText("Endpoint alias"), { target: { value: "private-ai-east-01" } });

    fireEvent.click(screen.getByRole("button", { name: "Create governed draft" }));

    await waitFor(() => expect(createModelRoute).toHaveBeenCalledTimes(1));
    const [orgArg, bodyArg] = createModelRoute.mock.calls[0]!;
    expect(orgArg).toBe("00000000-0000-0000-0000-000000000001");
    expect(bodyArg).toEqual({
      route_key: "bank-sql-primary",
      display_name: "Bank SQL generation",
      provider_type: "OPENAI",
      model_id: "approved-deployment-alias",
      endpoint_alias: "private-ai-east-01",
      credential_reference: null,
      data_residency: "US",
      retention_policy: "ZERO_RETENTION",
      capabilities: ["SQL_GENERATION", "CLASSIFICATION"],
      max_input_tokens: 8000,
      max_output_tokens: 2000,
      timeout_seconds: 30,
    });
    // Refetches the registry after a successful create, same reload
    // convention as ContextProductsScreen/StudioChangeSetsScreen.
    await waitFor(() => expect(fetchModelRoutes).toHaveBeenCalledTimes(2));
  });

  it("submits a draft route through the real submit endpoint and refetches", async () => {
    fetchModelRoutes.mockResolvedValue({ items: [DRAFT_ROUTE], limit: 200, offset: 0, total: 1 });
    submitModelRoute.mockResolvedValue({
      id: "gr_1", organization_id: "org1", object_type: "MODEL_ROUTE_CONFIGURATION", object_id: "route_1",
      requested_action: "APPROVE_MODEL_ROUTE", status: "PENDING", requested_by: "local-ui-admin",
      decided_by: null, decision_reason: null, decided_at: null,
      created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    });
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);
    await waitFor(() => expect(screen.getByText("Bank SQL generation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(submitModelRoute).toHaveBeenCalledWith("route_1", undefined));
    await waitFor(() => expect(fetchModelRoutes).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Model route submitted for independent review.")).toBeInTheDocument();
  });

  it("shows the real submit failure detail without changing status client-side", async () => {
    fetchModelRoutes.mockResolvedValue({ items: [DRAFT_ROUTE], limit: 200, offset: 0, total: 1 });
    submitModelRoute.mockRejectedValue(new ApiError(409, "only draft model routes can be submitted"));
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);
    await waitFor(() => expect(screen.getByText("Bank SQL generation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    expect(await screen.findByText("only draft model routes can be submitted")).toBeInTheDocument();
  });

  it("runs the control evaluation suite through the real endpoint and refetches evaluation evidence", async () => {
    runAgentEvaluation.mockResolvedValue({
      id: "eval_1", organization_id: "org1", principal_id: "local-ui-admin", suite_version: "2026.09",
      status: "PASSED", scenario_count: 12, passed_count: 12, failed_count: 0, pass_rate: 1,
      findings: [], created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    });
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);
    await screen.findByText("No evaluation evidence is available");

    fireEvent.click(screen.getByRole("button", { name: "Run control evaluation" }));

    await waitFor(() => expect(runAgentEvaluation).toHaveBeenCalledWith("00000000-0000-0000-0000-000000000001", undefined));
    await waitFor(() => expect(fetchAgentEvaluations).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Agent control evaluation completed.")).toBeInTheDocument();
  });

  it("lists real evaluation runs and expands a run's findings on click", async () => {
    fetchAgentEvaluations.mockResolvedValue({
      items: [
        {
          id: "eval_9", organization_id: "org1", principal_id: "local-ui-admin", suite_version: "2026.08",
          status: "FAILED", scenario_count: 10, passed_count: 8, failed_count: 2, pass_rate: 0.8,
          findings: [{ case_id: "case_1", detail: "drifted" }],
          created_at: "2026-08-15T00:00:00Z", updated_at: "2026-08-15T00:00:00Z",
        },
      ],
      limit: 100, offset: 0, total: 1,
    });
    const AiGovernanceScreen = await loadScreen();
    render(<AiGovernanceScreen />);
    await waitFor(() => expect(screen.getByText("2026.08")).toBeInTheDocument());
    expect(screen.getByText("8 / 10")).toBeInTheDocument();
    expect(screen.getByText("80%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "2026.08" }));

    expect(await screen.findByText(/"case_id": "case_1"/)).toBeInTheDocument();
  });
});
