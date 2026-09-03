import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ContextCompilationRead, ContextProductCreate, ContextProductRead, GovernanceReviewRead, ProjectRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Context products, ported from the legacy portal's `context-products` view
   onto the real, already-merged `context_product_api.py` /
   `context_compiler_api.py` routes. Mocks the API boundary the same way
   every other UX-15 screen test does (`StudioChangeSetsScreen.test.tsx`,
   `SemanticsScreen.test.tsx`).
--------------------------------------------------------------------------- */

const fetchOrgProjects = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
const fetchContextProducts =
  vi.fn<(projectId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<ContextProductRead>>>();
const createContextProduct =
  vi.fn<(projectId: string, body: ContextProductCreate, signal?: AbortSignal) => Promise<ContextProductRead>>();
const submitContextProductVersion =
  vi.fn<(versionId: string, signal?: AbortSignal) => Promise<GovernanceReviewRead>>();
const requestContextProductDeprecation =
  vi.fn<(versionId: string, signal?: AbortSignal) => Promise<GovernanceReviewRead>>();
const compileContextProductVersion =
  vi.fn<(versionId: string, target: string, signal?: AbortSignal) => Promise<ContextCompilationRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgProjects: (organizationId: string, signal?: AbortSignal) => fetchOrgProjects(organizationId, signal),
    fetchContextProducts: (projectId: string, query: unknown, signal?: AbortSignal) =>
      fetchContextProducts(projectId, query, signal),
    createContextProduct: (projectId: string, body: ContextProductCreate, signal?: AbortSignal) =>
      createContextProduct(projectId, body, signal),
    submitContextProductVersion: (versionId: string, signal?: AbortSignal) =>
      submitContextProductVersion(versionId, signal),
    requestContextProductDeprecation: (versionId: string, signal?: AbortSignal) =>
      requestContextProductDeprecation(versionId, signal),
    compileContextProductVersion: (versionId: string, target: string, signal?: AbortSignal) =>
      compileContextProductVersion(versionId, target, signal),
  };
});

const PROJECT: ProjectRead = {
  id: "proj_core", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DRAFT_PRODUCT: ContextProductRead = {
  id: "cp_1", organization_id: "org1", project_id: "proj_core", product_key: "consumer-risk-context",
  lifecycle_status: "ACTIVE", created_by: "risk-data-stewards@tenant.example",
  latest_version: {
    id: "cpv_1", organization_id: "org1", product_id: "cp_1", product_key: "consumer-risk-context",
    version: 1, status: "DRAFT",
    name: "Consumer risk analysis", description: "Bounded context for risk analysts.",
    purpose: "Explain drivers of consumer delinquency for the monthly risk packet.",
    owner_type: "GROUP", owner_principal: "risk-data-stewards",
    table_ids: ["t1"], semantic_model_version_ids: [], glossary_term_version_ids: [],
    eligible_tool_version_ids: [], allowed_consumer_roles: ["Analyst"], lineage_depth: 2,
    quality_requirements: { minimum_score: 85, deny_on_critical_incident: true },
    policy_summary: { source_values: "GATEWAY_ONLY", retention: "NO_RAW_CONTEXT", permitted_actions: ["READ_CONTEXT"] },
    support_window_days: null,
    fingerprint: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    created_by: "risk-data-stewards@tenant.example", approved_by: null, approved_at: null, published_at: null,
    based_on_version_id: null, created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
    superseded_at: null, support_window_ends_at: null, superseded_by_version_id: null,
  },
  created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
};

async function loadScreen() {
  const { ContextProductsScreen } = await import("./ContextProductsScreen");
  return ContextProductsScreen;
}

beforeEach(() => {
  fetchOrgProjects.mockReset();
  fetchContextProducts.mockReset();
  createContextProduct.mockReset();
  submitContextProductVersion.mockReset();
  requestContextProductDeprecation.mockReset();
  compileContextProductVersion.mockReset();
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ContextProductsScreen against the real context_product_api.py / context_compiler_api.py routes", () => {
  it("shows the empty-before-selection state without listing any products", async () => {
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);

    await waitFor(() => expect(screen.getByText("Pick a project to see its context products")).toBeInTheDocument());
    expect(fetchOrgProjects).toHaveBeenCalledWith("00000000-0000-0000-0000-000000000001", expect.anything());
    expect(fetchContextProducts).not.toHaveBeenCalled();
  });

  it("selecting a project loads the real registry and shows the legacy empty copy when there are none", async () => {
    fetchContextProducts.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);

    await waitFor(() => expect(screen.getByText("Core Finance")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "proj_core" } });

    await waitFor(() =>
      expect(fetchContextProducts).toHaveBeenCalledWith("proj_core", { limit: 200 }, expect.anything()),
    );
    await waitFor(() => expect(screen.getByText("No Context Products")).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("project")).toBe("proj_core");
  });

  it("lists a draft product and submits it through the real submit endpoint", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    submitContextProductVersion.mockResolvedValue({
      id: "gr_1", organization_id: "org1", object_type: "CONTEXT_PRODUCT_VERSION", object_id: "cpv_1",
      requested_action: "PUBLISH", status: "PENDING", requested_by: "local-ui-admin",
      decided_by: null, decision_reason: null, decided_at: null,
      created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());
    expect(screen.getByText("consumer-risk-context · v1")).toBeInTheDocument();
    expect(screen.getByText("risk-data-stewards")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(submitContextProductVersion).toHaveBeenCalledWith("cpv_1", undefined));
    // Reloads the registry after a successful submit, same as the legacy
    // screen's own `transitionVersion` -> `loadContextProducts()` sequence —
    // the reload's own "N governed products..." status supersedes the
    // transient "Publication review requested." one, exactly as legacy's
    // single shared `#context-product-message` target does.
    await waitFor(() => expect(fetchContextProducts).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText("1 governed product in this project.")).toBeInTheDocument());
  });

  it("compiles the selected version through the real compile endpoint at the chosen target", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    compileContextProductVersion.mockResolvedValue({
      target: "YAML", content_type: "application/yaml", content: "name: Consumer risk analysis\n",
      artifact_hash: "f".repeat(64), source_fingerprint: DRAFT_PRODUCT.latest_version.fingerprint,
      generated_from: { context_product_version_id: "cpv_1" },
    });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Target"), { target: { value: "YAML" } });
    fireEvent.click(screen.getByRole("button", { name: "Compile" }));

    await waitFor(() => expect(compileContextProductVersion).toHaveBeenCalledWith("cpv_1", "YAML", undefined));
    expect(await screen.findByText("name: Consumer risk analysis")).toBeInTheDocument();
    expect(screen.getByText(/artifact f{16}/)).toBeInTheDocument();
  });

  it("shows the real 409 lifecycle failure without changing status client-side", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    submitContextProductVersion.mockRejectedValue(new ApiError(409, "only a draft context product can be submitted"));
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    expect(await screen.findByText("only a draft context product can be submitted")).toBeInTheDocument();
  });

  it("creates a governed draft with the exact field mapping the real create endpoint expects", async () => {
    fetchContextProducts.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    createContextProduct.mockResolvedValue(DRAFT_PRODUCT);
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("No Context Products")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Stable key"), { target: { value: "consumer-risk-context" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Consumer risk analysis" } });
    fireEvent.change(screen.getByLabelText("Owner principal"), { target: { value: "risk-data-stewards" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Bounded context for risk analysts." } });
    fireEvent.change(screen.getByLabelText("Approved purpose"), {
      target: { value: "Explain drivers of consumer delinquency for the monthly risk packet." },
    });
    fireEvent.change(screen.getByLabelText("Table UUIDs"), { target: { value: " t1 , t2 " } });

    fireEvent.click(screen.getByRole("button", { name: "Create governed draft" }));

    await waitFor(() => expect(createContextProduct).toHaveBeenCalledTimes(1));
    const [projectArg, bodyArg] = createContextProduct.mock.calls[0]!;
    expect(projectArg).toBe("proj_core");
    expect(bodyArg).toEqual({
      product_key: "consumer-risk-context",
      name: "Consumer risk analysis",
      description: "Bounded context for risk analysts.",
      purpose: "Explain drivers of consumer delinquency for the monthly risk packet.",
      owner_type: "GROUP",
      owner_principal: "risk-data-stewards",
      table_ids: ["t1", "t2"],
      semantic_model_version_ids: [],
      glossary_term_version_ids: [],
      eligible_tool_version_ids: [],
      allowed_consumer_roles: ["Analyst"],
      lineage_depth: 2,
      quality_requirements: { minimum_score: 85, deny_on_critical_incident: true },
      policy_summary: { source_values: "GATEWAY_ONLY", retention: "NO_RAW_CONTEXT", permitted_actions: ["READ_CONTEXT", "INVOKE_ELIGIBLE_TOOLS"] },
    });
    // Refetches the registry after a successful create, same as the legacy
    // screen's own `createContextProduct` -> `loadContextProducts()`
    // sequence — the reload's own status supersedes the transient "Draft
    // created..." one on the same shared message target (see the submit
    // test above for the identical, legacy-faithful race).
    await waitFor(() => expect(fetchContextProducts).toHaveBeenCalledTimes(2));
  });
});
