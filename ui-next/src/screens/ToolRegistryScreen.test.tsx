import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  DataSourceRead,
  GovernanceReviewRead,
  GovernedToolVersionCreate,
  GovernedToolVersionRead,
  ProjectRead,
  ToolExecutionRequest,
  ToolExecutionResponse,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Tool registry, ported from the legacy portal's `tools` view onto the real,
   already-merged `tool_api.py` routes. Mocks the API boundary the same way
   every other UX-15 screen test does (`ContextProductsScreen.test.tsx`,
   `StudioChangeSetsScreen.test.tsx`).
--------------------------------------------------------------------------- */

const fetchOrgProjects = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
const fetchOrgDatasources = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchTools =
  vi.fn<(projectId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<GovernedToolVersionRead>>>();
const createToolVersion =
  vi.fn<(projectId: string, body: GovernedToolVersionCreate, signal?: AbortSignal) => Promise<GovernedToolVersionRead>>();
const submitToolForReview = vi.fn<(versionId: string, signal?: AbortSignal) => Promise<GovernanceReviewRead>>();
const requestToolDeprecation = vi.fn<(versionId: string, signal?: AbortSignal) => Promise<GovernanceReviewRead>>();
const executeToolVersion =
  vi.fn<(versionId: string, body: ToolExecutionRequest, signal?: AbortSignal) => Promise<ToolExecutionResponse>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgProjects: (organizationId: string, signal?: AbortSignal) => fetchOrgProjects(organizationId, signal),
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) => fetchOrgDatasources(organizationId, signal),
    fetchTools: (projectId: string, query: unknown, signal?: AbortSignal) => fetchTools(projectId, query, signal),
    createToolVersion: (projectId: string, body: GovernedToolVersionCreate, signal?: AbortSignal) =>
      createToolVersion(projectId, body, signal),
    submitToolForReview: (versionId: string, signal?: AbortSignal) => submitToolForReview(versionId, signal),
    requestToolDeprecation: (versionId: string, signal?: AbortSignal) => requestToolDeprecation(versionId, signal),
    executeToolVersion: (versionId: string, body: ToolExecutionRequest, signal?: AbortSignal) =>
      executeToolVersion(versionId, body, signal),
  };
});

const PROJECT: ProjectRead = {
  id: "proj_core", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DATASOURCE: DataSourceRead = {
  id: "ds1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1", project_id: "proj_core",
  name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PRODUCTION",
  credential_reference: "vault://ds/snowflake_prod", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DRAFT_TOOL: GovernedToolVersionRead = {
  id: "tv_1", tool_id: "t_1", organization_id: "org1", project_id: "proj_core",
  slug: "customer_lookup", version: 1, status: "DRAFT",
  name: "Customer lookup", description: "Look up a customer by state.",
  datasource_id: "ds1", semantic_model_version_id: null,
  sql_template: "SELECT customer_id FROM public.customers WHERE state = :state",
  referenced_tables: ["public.customers"],
  parameters: [{ name: "state", parameter_type: "STRING", required: true, sensitive: false }],
  allowed_roles: ["Analyst", "ToolConsumer"],
  fingerprint: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
  created_by: "local-ui-admin", approved_by: null, approved_at: null,
  created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z", usage_count: 0,
};

const PUBLISHED_TOOL: GovernedToolVersionRead = {
  ...DRAFT_TOOL, id: "tv_2", version: 2, status: "PUBLISHED",
  approved_by: "steward@tenant.example", approved_at: "2026-08-05T00:00:00Z",
};

async function loadScreen() {
  const { ToolRegistryScreen } = await import("./ToolRegistryScreen");
  return ToolRegistryScreen;
}

beforeEach(() => {
  fetchOrgProjects.mockReset();
  fetchOrgDatasources.mockReset();
  fetchTools.mockReset();
  createToolVersion.mockReset();
  submitToolForReview.mockReset();
  requestToolDeprecation.mockReset();
  executeToolVersion.mockReset();
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ToolRegistryScreen against the real tool_api.py routes", () => {
  it("shows the empty-before-selection state without listing any tools", async () => {
    const ToolRegistryScreen = await loadScreen();
    render(<ToolRegistryScreen />);

    await waitFor(() => expect(screen.getByText("Pick a project to see its tool registry")).toBeInTheDocument());
    expect(fetchOrgProjects).toHaveBeenCalledWith("00000000-0000-0000-0000-000000000001", expect.anything());
    expect(fetchTools).not.toHaveBeenCalled();
  });

  it("selecting a project loads the real registry and shows the legacy empty copy when there are none", async () => {
    fetchTools.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    const ToolRegistryScreen = await loadScreen();
    render(<ToolRegistryScreen />);

    await waitFor(() => expect(screen.getByText("Core Finance")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "proj_core" } });

    await waitFor(() =>
      expect(fetchTools).toHaveBeenCalledWith("proj_core", { status: null, limit: 200 }, expect.anything()),
    );
    await waitFor(() => expect(screen.getByText("No tool versions match")).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("project")).toBe("proj_core");
  });

  it("lists a draft tool version and submits it through the real submit endpoint", async () => {
    fetchTools.mockResolvedValue({ items: [DRAFT_TOOL], limit: 200, offset: 0, total: 1 });
    submitToolForReview.mockResolvedValue({
      id: "gr_1", organization_id: "org1", object_type: "GOVERNED_TOOL_VERSION", object_id: "tv_1",
      requested_action: "PUBLISH", status: "PENDING", requested_by: "local-ui-admin",
      decided_by: null, decision_reason: null, decided_at: null,
      created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    });
    const ToolRegistryScreen = await loadScreen();
    render(<ToolRegistryScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    fireEvent.click(await screen.findByText("Customer lookup"));

    await waitFor(() => expect(screen.getByText("customer_lookup · version 1")).toBeInTheDocument());
    expect(screen.getByText("Look up a customer by state.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Submit for review" }));

    await waitFor(() => expect(submitToolForReview).toHaveBeenCalledWith("tv_1", undefined));
    await waitFor(() =>
      expect(screen.getByText("Tool version submitted for independent review.")).toBeInTheDocument(),
    );
    // Reloads the registry after a successful submit, same as legacy's own
    // `refreshAfter` -> `loadOrganizationData()` sequence.
    await waitFor(() => expect(fetchTools).toHaveBeenCalledTimes(2));
  });

  it("shows the real 409 lifecycle failure without changing status client-side", async () => {
    fetchTools.mockResolvedValue({ items: [DRAFT_TOOL], limit: 200, offset: 0, total: 1 });
    submitToolForReview.mockRejectedValue(new ApiError(409, "tool version is not in a submittable state"));
    const ToolRegistryScreen = await loadScreen();
    render(<ToolRegistryScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    fireEvent.click(await screen.findByText("Customer lookup"));

    fireEvent.click(await screen.findByRole("button", { name: "Submit for review" }));

    expect(await screen.findByText("tool version is not in a submittable state")).toBeInTheDocument();
  });

  it("executes a published tool version through the real execute endpoint with typed parameters", async () => {
    fetchTools.mockResolvedValue({ items: [PUBLISHED_TOOL], limit: 200, offset: 0, total: 1 });
    executeToolVersion.mockResolvedValue({
      tool_execution_id: "te_1", tool_version_id: "tv_2", tool_slug: "customer_lookup", tool_version: 2,
      execution: {
        execution_id: "qe_1", status: "SUCCEEDED",
        normalized_sql: "SELECT customer_id FROM public.customers WHERE state = 'NY'",
        referenced_tables: ["public.customers"], referenced_columns: ["state", "customer_id"],
        column_lineage: [], plan_cost: 4.2, warehouse_query_id: "wh_1",
        row_count: 1, elapsed_ms: 88, masked_columns: [],
        rows: [{ customer_id: "c_100" }],
      },
      quality_gate: null,
    });
    const ToolRegistryScreen = await loadScreen();
    render(<ToolRegistryScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    fireEvent.click(await screen.findByText("Customer lookup"));

    await waitFor(() => expect(screen.getByText("Customer lookup v2")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("state"), { target: { value: "NY" } });
    fireEvent.click(screen.getByRole("button", { name: "Execute tool" }));

    await waitFor(() =>
      expect(executeToolVersion).toHaveBeenCalledWith("tv_2", { parameters: { state: "NY" } }, undefined),
    );
    expect(await screen.findByText("customer_lookup version 2 completed.")).toBeInTheDocument();
    expect(screen.getByText("c_100")).toBeInTheDocument();
  });

  it("creates a governed draft tool version with the exact field mapping the real create endpoint expects", async () => {
    fetchTools.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    createToolVersion.mockResolvedValue(DRAFT_TOOL);
    const ToolRegistryScreen = await loadScreen();
    render(<ToolRegistryScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("No tool versions match")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Stable slug"), { target: { value: "customer_lookup" } });
    fireEvent.change(screen.getByLabelText("Version name"), { target: { value: "Customer lookup" } });
    fireEvent.change(screen.getByLabelText("Data source"), { target: { value: "ds1" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Look up a customer by state." } });
    fireEvent.change(screen.getByLabelText("SQL template"), {
      target: { value: "SELECT customer_id FROM public.customers WHERE state = :state" },
    });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "state" } });

    fireEvent.click(screen.getByRole("button", { name: "Create draft version" }));

    await waitFor(() => expect(createToolVersion).toHaveBeenCalledTimes(1));
    const [projectArg, bodyArg] = createToolVersion.mock.calls[0]!;
    expect(projectArg).toBe("proj_core");
    expect(bodyArg).toEqual({
      slug: "customer_lookup",
      name: "Customer lookup",
      description: "Look up a customer by state.",
      datasource_id: "ds1",
      sql_template: "SELECT customer_id FROM public.customers WHERE state = :state",
      parameters: [{ name: "state", parameter_type: "STRING", required: true, sensitive: false }],
      allowed_roles: ["Analyst", "ToolConsumer"],
    });
    // Refetches the registry after a successful create, same as legacy's
    // `openToolAuthor` submit -> `loadOrganizationData()` sequence.
    await waitFor(() => expect(fetchTools).toHaveBeenCalledTimes(2));
  });
});
