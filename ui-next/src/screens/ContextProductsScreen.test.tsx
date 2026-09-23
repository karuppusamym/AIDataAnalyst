import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ContextCompilationRead, ContextProductCreate, ContextProductRead, GovernanceReviewRead, MeRead, ProjectRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import type { ContextProductChangesSincePublished } from "../lib/api";
import type { ContextProductChangesSummaryListRead } from "../lib/types";
import { ApiError } from "../lib/api";
import type { Session, SessionState } from "../lib/session";
import { expectNoAxeViolations, unnamedFocusableElements } from "../test/a11y";

/* ---------------------------------------------------------------------------
   Context products, ported from the legacy portal's `context-products` view
   onto the real, already-merged `context_product_api.py` /
   `context_compiler_api.py` routes. Mocks the API boundary the same way
   every other UX-15 screen test does (`StudioChangeSetsScreen.test.tsx`,
   `SemanticsScreen.test.tsx`).
--------------------------------------------------------------------------- */

const fetchOrgProjects = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
/* R11-FP12 (F08): the screen now offers "Ask through this product", and Ask is
   datasource-scoped -- so the row needs one of this project's own sources.
   `useDatasourcePicker` reads them through this same module boundary. */
const listOrgDatasources = vi.fn();
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

/* The four governed-reference pickers replaced four "paste a UUID" boxes, so
   the screen now reads the same lists Catalog / Semantics / Tools / Business
   meaning read. Mocked here for the same reason the write endpoints are: the
   test is about this screen's behaviour, not about those read models. */
const fetchCatalogRows = vi.fn();
const fetchSemanticModelVersions = vi.fn();
const fetchTools = vi.fn();
const listGlossaryTerms = vi.fn();
const fetchContextProductRoutineOptions = vi.fn();
const listOntologyVersions = vi.fn();
/* R11-FP12: a new version posts through the shared transport verb. `demoOr` is
   pinned to its live arm so the request is the one a deployed client sends. */
const postJson = vi.fn();

vi.mock("../lib/api/ontology", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/ontology")>();
  return { ...actual, listOntologyVersions: (...args: unknown[]) => listOntologyVersions(...args) };
});

/* R11-AUD01: which roles the session holds decides whether the ontology read is
   made at all. `null` is "`/v1/me` has not answered" -- what every other test in
   this file runs as (a build with no identity to consult, `state: "demo"`), and
   the state that must keep asking. While `/v1/me` is IN FLIGHT the session is
   "connecting": the read is held (`readDecision`, `lib/roles.ts`). */
let sessionMe: MeRead | null = null;
let sessionState: SessionState = "demo";
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: sessionState,
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "fixtures",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

/* Staged rollout (AT-7(b) consumer bindings). */
const fetchContextProductVersions = vi.fn();
const fetchContextProductBindings = vi.fn();
const setContextProductBinding = vi.fn();
const removeContextProductBinding = vi.fn();
/* R11-FP12: the on-demand "changed since publication" read. Mocked at the same
   boundary as every other call; its own mapping of the GraphQL answer is proven in
   `lib/api/products.test.ts`. */
const fetchContextProductChangesSincePublished =
  vi.fn<(versionId: string, signal?: AbortSignal) => Promise<ContextProductChangesSincePublished>>();
/* R11-FP12 (2026-09-22): the passive count, one read for the project. */
const fetchContextProductChangesSummary = vi.fn<
  (
    projectId: string,
    options?: { productId?: string | null },
    signal?: AbortSignal,
  ) => Promise<ContextProductChangesSummaryListRead>
>();

vi.mock("../lib/_api_append", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/_api_append")>();
  return { ...actual, listGlossaryTerms: (...args: unknown[]) => listGlossaryTerms(...args) };
});

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchCatalogRows: (...args: unknown[]) => fetchCatalogRows(...args),
    fetchSemanticModelVersions: (...args: unknown[]) => fetchSemanticModelVersions(...args),
    fetchTools: (...args: unknown[]) => fetchTools(...args),
    fetchContextProductRoutineOptions: (...args: unknown[]) => fetchContextProductRoutineOptions(...args),
    fetchContextProductVersions: (...args: unknown[]) => fetchContextProductVersions(...args),
    fetchContextProductBindings: (...args: unknown[]) => fetchContextProductBindings(...args),
    setContextProductBinding: (...args: unknown[]) => setContextProductBinding(...args),
    removeContextProductBinding: (...args: unknown[]) => removeContextProductBinding(...args),
    fetchContextProductChangesSincePublished: (versionId: string, signal?: AbortSignal) =>
      fetchContextProductChangesSincePublished(versionId, signal),
    fetchContextProductChangesSummary: (
      projectId: string,
      options?: { productId?: string | null },
      signal?: AbortSignal,
    ) => fetchContextProductChangesSummary(projectId, options, signal),
    listOrgDatasources: (...args: unknown[]) => listOrgDatasources(...args),
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
    postJson: (...args: unknown[]) => postJson(...args),
    demoOr: (_demo: unknown, live: () => Promise<unknown>) => live(),
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

/** The same product, published -- the only status the ask path resolves. */
const PUBLISHED_PRODUCT: ContextProductRead = {
  ...DRAFT_PRODUCT,
  latest_version: { ...DRAFT_PRODUCT.latest_version, status: "PUBLISHED", version: 2 },
};

/** One source, in this project. `project_id` is what ties it to the product. */
const PROJECT_DATASOURCE = {
  id: "ds_snowflake_prod", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", project_id: "proj_core", name: "snowflake_prod",
  connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PRODUCTION",
  credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
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
  fetchContextProductChangesSincePublished.mockReset();
  fetchContextProductChangesSummary.mockReset();
  fetchContextProductChangesSummary.mockResolvedValue(summaryOf({}));
  for (const fn of [
    fetchCatalogRows, fetchSemanticModelVersions, fetchTools, listGlossaryTerms,
    fetchContextProductRoutineOptions, listOntologyVersions,
    fetchContextProductVersions, fetchContextProductBindings,
    setContextProductBinding, removeContextProductBinding, postJson,
  ]) fn.mockReset();
  fetchContextProductRoutineOptions.mockResolvedValue([]);
  listOntologyVersions.mockResolvedValue([]);
  listOrgDatasources.mockReset();
  listOrgDatasources.mockResolvedValue({ items: [PROJECT_DATASOURCE], limit: 500, offset: 0, total: 1 });
  sessionMe = null;
  sessionState = "demo";

  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  fetchCatalogRows.mockResolvedValue({ items: CATALOG_ROWS, limit: 200, offset: 0, total: CATALOG_ROWS.length });
  fetchSemanticModelVersions.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  fetchTools.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  listGlossaryTerms.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  fetchContextProductVersions.mockResolvedValue({ items: [DRAFT_PRODUCT.latest_version], limit: 200, offset: 0, total: 1 });
  fetchContextProductBindings.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });

  vi.resetModules();
  history.replaceState(null, "", "/");
});

/* Two catalog rows, shaped exactly as `GET .../catalog/rows` returns them, so
   the picker is exercised against the real read model rather than a
   convenient stub. `datasource_id` is the field the cross-links need. */
const CATALOG_ROWS = [
  {
    id: "t1", name: "orders_raw", schema_name: "core",
    datasource_id: "ds_snowflake_prod", datasource_name: "snowflake_prod",
    object_type: "TABLE", status: "ACTIVE", description: null, description_is_proposed: false,
    owner: "Risk Analytics", certification: "CERTIFIED" as const, certification_expires_at: null,
    certification_evidence_summary: null, quality: "PASSING" as const, glossary_terms: [],
    row_count_estimate: 10, updated_at: "2026-09-01T00:00:00Z",
  },
  {
    id: "t2", name: "customer_dim", schema_name: "core",
    datasource_id: "ds_snowflake_prod", datasource_name: "snowflake_prod",
    object_type: "TABLE", status: "ACTIVE", description: null, description_is_proposed: false,
    owner: null, certification: "NONE" as const, certification_expires_at: null,
    certification_evidence_summary: null, quality: "UNKNOWN" as const, glossary_terms: [],
    row_count_estimate: 20, updated_at: "2026-09-01T00:00:00Z",
  },
];

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

  /* -------------------------------------------------------------------------
     R11-FP12 (F08): asking through the product, from the product.

     Rollout / Submit / Deprecate / Compile were the only actions here, so a
     published product could be governed from this screen and not consumed from
     it -- the one thing it exists for meant going to Ask and finding it in a
     picker.
  ------------------------------------------------------------------------- */

  it("opens Ask on this product, carrying a source of its own project (F08)", async () => {
    fetchContextProducts.mockResolvedValue({ items: [PUBLISHED_PRODUCT], limit: 200, offset: 0, total: 1 });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.click(await screen.findByRole("button", { name: "Ask through this product" }));

    // The datasource is what makes the picker land on this product rather than
    // on a blank selection: Ask resolves its product list from the selected
    // source's project.
    expect(location.hash).toBe("#/analyst/analyst");
    const params = new URLSearchParams(location.search);
    expect(params.get("ds")).toBe("ds_snowflake_prod");
    expect(params.get("product")).toBe("consumer-risk-context");
  });

  it("does not offer Ask for a version that is not published (F08)", async () => {
    // Offering it for a DRAFT would be offering CONTEXT_PRODUCT_NOT_AVAILABLE:
    // the ask path resolves a PUBLISHED version and nothing else.
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    expect(screen.queryByRole("button", { name: "Ask through this product" })).not.toBeInTheDocument();
  });

  it("does not offer Ask when the project has no source to ask against (F08)", async () => {
    listOrgDatasources.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });
    fetchContextProducts.mockResolvedValue({ items: [PUBLISHED_PRODUCT], limit: 200, offset: 0, total: 1 });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    // A button that navigated to a picker with nothing in it would be worse
    // than no button.
    expect(screen.queryByRole("button", { name: "Ask through this product" })).not.toBeInTheDocument();
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
    /* The whole point of the change: a steward picks tables by name. The ids
       that reach the request body are the ones the catalog itself returned,
       so a typo can no longer produce a 422. */
    fireEvent.click(await screen.findByRole("checkbox", { name: /core\.orders_raw/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /core\.customer_dim/ }));

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

  it("names a routine through its own picker and sends routine_ids only then (R11-FP12)", async () => {
    fetchContextProducts.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    fetchContextProductRoutineOptions.mockResolvedValue([
      {
        id: "r1", datasource_id: "ds_snowflake_prod", datasource_name: "snowflake_prod",
        schema_name: "core", name: "rebuild_totals", routine_type: "PROCEDURE", signature: "()",
      },
    ]);
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
    fireEvent.click(await screen.findByRole("checkbox", { name: /core\.rebuild_totals\(\)/ }));

    fireEvent.click(screen.getByRole("button", { name: "Create governed draft" }));

    await waitFor(() => expect(createContextProduct).toHaveBeenCalledTimes(1));
    expect(fetchContextProductRoutineOptions).toHaveBeenCalledWith("proj_core", expect.anything());
    const [, bodyArg] = createContextProduct.mock.calls[0]!;
    expect(bodyArg.routine_ids).toEqual(["r1"]);
    expect(bodyArg.table_ids).toEqual([]);
    expect(bodyArg).not.toHaveProperty("ontology_version_ids");
  });

  it("binds an approved ontology version and never offers a draft one (R11-FP09)", async () => {
    fetchContextProducts.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
    listOntologyVersions.mockResolvedValue([
      { id: "ov1", ontology_id: "o1", ontology_key: "commerce", version: 2, base_version: 1, published_version: 2,
        status: "APPROVED", definition: {}, created_by: "a", approved_by: "b", governance_review_id: null },
      { id: "ov2", ontology_id: "o1", ontology_key: "commerce", version: 3, base_version: 2, published_version: 2,
        status: "DRAFT", definition: {}, created_by: "a", approved_by: null, governance_review_id: null },
    ]);
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
    fireEvent.click(await screen.findByRole("checkbox", { name: /commerce v2/ }));
    expect(screen.queryByRole("checkbox", { name: /commerce v3/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Create governed draft" }));

    await waitFor(() => expect(createContextProduct).toHaveBeenCalledTimes(1));
    const [, bodyArg] = createContextProduct.mock.calls[0]!;
    expect(bodyArg.ontology_version_ids).toEqual(["ov1"]);
  });
  /* ---- New version (R11-FP12) ------------------------------------------
     Routines could be named only when a product was first created: nothing on
     this screen added a version at all. A new version starts from the latest,
     offers the same six pickers -- the routine one fed by the same options
     route -- and names its base. */

  it("drafts a new version naming a routine through the same picker, based on the latest (R11-FP12)", async () => {
    fetchContextProducts.mockResolvedValue({ items: [PUBLISHED_PRODUCT], limit: 200, offset: 0, total: 1 });
    fetchContextProductRoutineOptions.mockResolvedValue([
      {
        id: "r1", datasource_id: "ds_snowflake_prod", datasource_name: "snowflake_prod",
        schema_name: "core", name: "rebuild_totals", routine_type: "PROCEDURE", signature: "()",
      },
    ]);
    postJson.mockResolvedValue({ ...PUBLISHED_PRODUCT.latest_version, id: "cpv_3", version: 3, status: "DRAFT" });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "New version" }));
    const panel = await screen.findByRole("article", { name: "New version of consumer-risk-context" });
    // Pre-filled from the base: its name, and the table it already names.
    expect(within(panel).getByLabelText("Name")).toHaveValue("Consumer risk analysis");
    expect(within(panel).getByRole("checkbox", { name: /core\.orders_raw/ })).toBeChecked();
    fireEvent.click(await within(panel).findByRole("checkbox", { name: /core\.rebuild_totals\(\)/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "Create version draft" }));

    await waitFor(() => expect(postJson).toHaveBeenCalledTimes(1));
    const [path, body] = postJson.mock.calls[0]!;
    expect(path).toBe("/v1/context-products/cp_1/versions");
    expect(body).toEqual({
      name: "Consumer risk analysis",
      description: "Bounded context for risk analysts.",
      purpose: "Explain drivers of consumer delinquency for the monthly risk packet.",
      owner_type: "GROUP",
      owner_principal: "risk-data-stewards",
      table_ids: ["t1"],
      semantic_model_version_ids: [],
      glossary_term_version_ids: [],
      eligible_tool_version_ids: [],
      routine_ids: ["r1"],
      allowed_consumer_roles: ["Analyst"],
      lineage_depth: 2,
      quality_requirements: { minimum_score: 85, deny_on_critical_incident: true },
      policy_summary: PUBLISHED_PRODUCT.latest_version.policy_summary,
      support_window_days: null,
      based_on_version_id: "cpv_1",
    });
    expect(fetchContextProductRoutineOptions).toHaveBeenCalledWith("proj_core", expect.anything());
    // The registry reloads and the panel closes once the draft exists.
    await waitFor(() => expect(fetchContextProducts).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByRole("article", { name: "New version of consumer-risk-context" })).not.toBeInTheDocument(),
    );
  });

  it("does not offer a new version while a draft is the latest (R11-FP12)", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    expect(screen.queryByRole("button", { name: "New version" })).not.toBeInTheDocument();
  });

  /* ---- Staged rollout (AT-7(b) consumer bindings) --------------------- */

  it("pins a named consumer to a specific version through the real binding endpoint", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    setContextProductBinding.mockResolvedValue({
      id: "cpb_1", organization_id: "org1", product_id: "cp_1",
      consumer_principal_id: "risk-copilot@agents.tenant.example",
      bound_version_id: "cpv_1", bound_version_number: 1,
      created_by: "local-ui-admin",
      created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
    });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Rollout" }));

    // Both reads are issued for the product, not the version -- the endpoints
    // are product-scoped and a version-scoped call would 404.
    await waitFor(() => expect(fetchContextProductVersions).toHaveBeenCalledWith("cp_1", { limit: 200 }, expect.anything()));
    expect(fetchContextProductBindings).toHaveBeenCalledWith("cp_1", { limit: 200 }, expect.anything());

    fireEvent.change(await screen.findByLabelText("Consumer principal"), {
      target: { value: "  risk-copilot@agents.tenant.example  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Pin consumer" }));

    // The principal is trimmed: a trailing space would otherwise create a
    // second, permanently-unmatchable binding for the same agent.
    await waitFor(() =>
      expect(setContextProductBinding).toHaveBeenCalledWith(
        "cp_1",
        "risk-copilot@agents.tenant.example",
        "cpv_1",
      ),
    );
  });

  it("says plainly that unpinned consumers resolve to the published version", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Rollout" }));

    expect(await screen.findByText("No pinned consumers")).toBeInTheDocument();
  });

  it("surfaces the server's own 422 when a version belongs to another product", async () => {
    fetchContextProducts.mockResolvedValue({ items: [DRAFT_PRODUCT], limit: 200, offset: 0, total: 1 });
    setContextProductBinding.mockRejectedValue(
      new ApiError(422, "bound_version_id is not a version of this context product"),
    );
    const ContextProductsScreen = await loadScreen();
    render(<ContextProductsScreen />);
    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getByText("Consumer risk analysis")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Rollout" }));
    fireEvent.change(await screen.findByLabelText("Consumer principal"), {
      target: { value: "risk-copilot@agents.tenant.example" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Pin consumer" }));

    expect(
      await screen.findByText("bound_version_id is not a version of this context product"),
    ).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   R11-FP12: a stale badge on the registry row.

   A published version whose covered source moved after publication is stale, and
   the row has to say so and say why. No list or read shape carries it, so it is
   asked for on demand per row (`contextProductCoverage`'s `changedSincePublished`,
   through `fetchContextProductChangesSincePublished`) -- never on load, because
   every such read is recorded as a consumption of the version.

   Three answers, and the third is the one that is easy to get wrong: stale (with
   what moved), nothing changed (with what was looked at), and *could not check*,
   which is neither of the first two.
--------------------------------------------------------------------------- */

const VIEW_ID = "3f2a9c1e-7b64-4d0a-9e51-0c8a5b7d2e11";
const ROUTINE_ID = "9d81c4a2-15fe-4a7b-8c3d-6e2f0a9b7c44";

/** In the server's own order: (subject kind, subject id, change). */
const STALE_ANSWER: ContextProductChangesSincePublished = {
  total: 2,
  changes: [
    { subjectKind: "ROUTINE", subjectId: ROUTINE_ID, change: "MEANING_RETIRED", changeClass: "MEANING_WITHDRAWN" },
    { subjectKind: "VIEW", subjectId: VIEW_ID, change: "DEFINITION_CHANGED", changeClass: "STRUCTURAL" },
  ],
};
const NOTHING_MOVED: ContextProductChangesSincePublished = { total: 0, changes: [] };

/** A second and third published product, distinct in every id the screen keys on. */
function anotherPublished(n: number, key: string, name: string): ContextProductRead {
  return {
    ...PUBLISHED_PRODUCT,
    id: `cp_${n}`,
    product_key: key,
    latest_version: {
      ...PUBLISHED_PRODUCT.latest_version,
      id: `cpv_${n}`,
      product_id: `cp_${n}`,
      product_key: key,
      name,
    },
  };
}
const PAYMENTS_PRODUCT = anotherPublished(9, "payments-context", "Payments context");
const SETTLEMENTS_PRODUCT = anotherPublished(10, "settlements-context", "Settlements context");

async function openRegistry(items: ContextProductRead[]) {
  fetchContextProducts.mockResolvedValue({ items, limit: 200, offset: 0, total: items.length });
  const ContextProductsScreen = await loadScreen();
  const view = render(<ContextProductsScreen />);
  fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
  await waitFor(() => expect(screen.getByText(items[0]!.latest_version.name)).toBeInTheDocument());
  return view;
}

const rowOf = (name: string) => screen.getByRole("article", { name });

/** The project summary, as the server answers it: counts per version id. */
function summaryOf(
  counts: Record<string, number | null>,
  meaning: Record<string, number> = {},
): ContextProductChangesSummaryListRead {
  const versionIds = [...new Set([...Object.keys(counts), ...Object.keys(meaning)])];
  return {
    project_id: "proj_core",
    generated_at: "2026-09-22T00:00:00Z",
    truncated: false,
    items: versionIds.map((versionId) => ({
      product_id: "cp",
      version_id: versionId,
      version: 1,
      status: "PUBLISHED",
      changed_subjects: versionId in counts ? counts[versionId]! : null,
      meaning_moved: meaning[versionId] ?? 0,
    })),
  };
}

describe("ContextProductsScreen: the count that needs no click (R11-FP12, 2026-09-22)", () => {
  it("shows how many covered subjects moved on each row, from one read for the whole project", async () => {
    sessionMe = asRoles("DataSteward");
    fetchContextProductChangesSummary.mockResolvedValue(summaryOf({ cpv_1: 3, cpv_9: 0 }));
    await openRegistry([PUBLISHED_PRODUCT, PAYMENTS_PRODUCT]);

    expect(await within(rowOf("Consumer risk analysis")).findByText("3 changes since published")).toBeInTheDocument();
    // 0 is "published, and nothing it covers moved": no badge, and not the word "stale" either.
    expect(within(rowOf("Payments context")).queryByText(/since published/)).not.toBeInTheDocument();
    expect(fetchContextProductChangesSummary).toHaveBeenCalledTimes(1);
    expect(fetchContextProductChangesSummary).toHaveBeenCalledWith(
      "proj_core",
      { productId: undefined },
      expect.any(AbortSignal),
    );
    // The detail is still asked for only when a person asks: it is recorded as a consumption.
    expect(fetchContextProductChangesSincePublished).not.toHaveBeenCalled();
  });

  it("gives way to the on-demand check once a person runs it, which says which subjects moved", async () => {
    sessionMe = asRoles("DataSteward");
    fetchContextProductChangesSummary.mockResolvedValue(summaryOf({ cpv_1: 2 }));
    fetchContextProductChangesSincePublished.mockResolvedValue(STALE_ANSWER);
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");
    expect(await within(row).findByText("2 changes since published")).toBeInTheDocument();

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    expect(await within(row).findByText("stale")).toBeInTheDocument();
    expect(within(row).queryByText("2 changes since published")).not.toBeInTheDocument();
  });

  it("says when meaning the version pins has moved on, and keeps saying so after the detailed check", async () => {
    sessionMe = asRoles("DataSteward");
    fetchContextProductChangesSummary.mockResolvedValue(summaryOf({ cpv_1: 0 }, { cpv_1: 2 }));
    fetchContextProductChangesSincePublished.mockResolvedValue(NOTHING_MOVED);
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    expect(await within(row).findByText("2 pinned meanings moved on")).toBeInTheDocument();
    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    // The detailed check covers the covered subjects, not the pins, so it replaces only the count.
    await waitFor(() => expect(fetchContextProductChangesSincePublished).toHaveBeenCalled());
    expect(within(row).getByText("2 pinned meanings moved on")).toBeInTheDocument();
  });

  it("asks a session outside the coverage roles nothing, and shows it no count", async () => {
    sessionMe = asRoles("Viewer");
    sessionState = "connected";
    fetchContextProductChangesSummary.mockResolvedValue(summaryOf({ cpv_1: 3 }));
    await openRegistry([PUBLISHED_PRODUCT]);

    expect(fetchContextProductChangesSummary).not.toHaveBeenCalled();
    expect(screen.queryByText(/since published/)).not.toBeInTheDocument();
  });

  it("counts every version of a product in the rollout panel: the version list and the pinned rows", async () => {
    sessionMe = asRoles("DataSteward");
    const published = PUBLISHED_PRODUCT.latest_version;
    const draft = { ...published, id: "cpv_draft", version: published.version + 1, status: "DRAFT" };
    fetchContextProductVersions.mockResolvedValue({
      items: [draft, PUBLISHED_PRODUCT.latest_version],
      limit: 200,
      offset: 0,
      total: 2,
    });
    fetchContextProductBindings.mockResolvedValue({
      items: [
        {
          id: "b1", organization_id: "org1", product_id: "cp_1",
          consumer_principal_id: "risk-copilot@agents.tenant.example",
          bound_version_id: "cpv_1", bound_version_number: published.version, created_by: "steward-1",
          created_at: "2026-09-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z",
        },
      ],
      limit: 200,
      offset: 0,
      total: 1,
    });
    // The product-scoped read answers for the product's versions; the project read for none.
    fetchContextProductChangesSummary.mockImplementation(async (_projectId, options) =>
      options?.productId === "cp_1" ? summaryOf({ cpv_1: 4, cpv_draft: null }) : summaryOf({}),
    );
    await openRegistry([PUBLISHED_PRODUCT]);

    fireEvent.click(screen.getByRole("button", { name: "Rollout" }));

    const panel = await screen.findByRole("article", { name: "Rollout for consumer-risk-context" });
    expect(
      await within(panel).findByRole("option", {
        name: `v${published.version} · published · 4 changes since published`,
      }),
    ).toBeInTheDocument();
    // Never published: no baseline, so no count -- not "0 changes".
    expect(within(panel).getByRole("option", { name: `v${draft.version} · draft` })).toBeInTheDocument();
    expect(within(within(panel).getByRole("table")).getByText("4 changes since published")).toBeInTheDocument();
    expect(fetchContextProductChangesSummary).toHaveBeenCalledWith(
      "proj_core",
      { productId: "cp_1" },
      expect.any(AbortSignal),
    );
  });

  it("shows no count when the summary could not be read, rather than reading as current", async () => {
    sessionMe = asRoles("DataSteward");
    fetchContextProductChangesSummary.mockRejectedValue(new ApiError(503, "database unavailable"));
    await openRegistry([PUBLISHED_PRODUCT]);

    await waitFor(() => expect(fetchContextProductChangesSummary).toHaveBeenCalled());
    expect(screen.queryByText(/since published/)).not.toBeInTheDocument();
    // The on-demand check stays available, which is how a person still finds out.
    expect(within(rowOf("Consumer risk analysis")).getByRole("button", { name: "Check for changes" })).toBeInTheDocument();
  });
});

describe("ContextProductsScreen: changed since publication (R11-FP12)", () => {
  it("reads nothing on load and shows neither stale nor current until a person asks", async () => {
    await openRegistry([PUBLISHED_PRODUCT, PAYMENTS_PRODUCT]);

    // Each read is recorded as a consumption of the version; a probe per row on load would
    // put N consumptions in the ledger for a screen that was merely opened.
    expect(fetchContextProductChangesSincePublished).not.toHaveBeenCalled();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
    expect(screen.queryByText(/Stale since publication/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Checked\./)).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Check for changes" })).toHaveLength(2);
  });

  it("marks a version stale and says what moved, from the server's own entries", async () => {
    fetchContextProductChangesSincePublished.mockResolvedValue(STALE_ANSWER);
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    await waitFor(() => expect(fetchContextProductChangesSincePublished).toHaveBeenCalledWith("cpv_1", undefined));
    // A word on the badge line beside the version's own status -- text, not colour alone.
    expect(await within(row).findByText("stale")).toBeInTheDocument();
    expect(within(row).getByText("published")).toBeInTheDocument();
    // And why: which covered things moved and how, from subject kind / id / change / class.
    expect(within(row).getByText(/2 changes to what v2 covers/)).toBeInTheDocument();
    const reasons = within(within(row).getByRole("list", { name: "What changed since publication" })).getAllByRole(
      "listitem",
    );
    expect(reasons).toHaveLength(2);
    expect(reasons[0]).toHaveTextContent(
      `Routine ${ROUTINE_ID} — approved description withdrawn (no approved text stands now)`,
    );
    expect(reasons[1]).toHaveTextContent(`View ${VIEW_ID} — definition changed (structural)`);
    // The message strip carries the one announcement; the row itself holds no live region.
    expect(screen.getByRole("status")).toHaveTextContent(
      "consumer-risk-context v2 is stale: 2 changes to what it covers since it was published.",
    );
    expect(row.querySelector('[role="status"],[role="alert"],[aria-live]')).toBeNull();
    // Asking again is offered, because the reading is a moment in time.
    expect(within(row).getByRole("button", { name: "Check again" })).toBeEnabled();
  });

  it("does not mark a version stale when nothing it covers changed -- a table reshape is not a change", async () => {
    // A covered table that only gained or lost a column records STRUCTURE_CHANGED, which is not one
    // of the coverage section's definition moves (`_DEFINITION_MOVES`, context_product_coverage.py):
    // deliberately, or every product would go stale whenever a column was added. So the server
    // answers with no entries, and that empty answer is the only shape a reshape-only version can
    // take. The row must not turn it into a badge, and must say what "nothing changed" means.
    fetchContextProductChangesSincePublished.mockResolvedValue(NOTHING_MOVED);
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    expect(await within(row).findByText(/No view or routine definition that v2 covers/)).toBeInTheDocument();
    expect(within(row).getByText(/A column added or removed is not counted/)).toBeInTheDocument();
    expect(within(row).queryByText("stale")).not.toBeInTheDocument();
    expect(within(row).queryByText(/Stale since publication/)).not.toBeInTheDocument();
    expect(within(row).queryByRole("list", { name: "What changed since publication" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "consumer-risk-context v2: nothing it covers has changed since it was published.",
    );
  });

  it("says a version could not be checked when the read fails: neither stale nor current", async () => {
    fetchContextProductChangesSincePublished.mockRejectedValueOnce(
      new Error("The coverage read was refused: NOT_FOUND."),
    );
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    expect(
      await within(row).findByText(
        /The coverage read was refused: NOT_FOUND\. v2 is neither marked stale nor confirmed current\./,
      ),
    ).toBeInTheDocument();
    expect(within(row).getByText("Not checked.")).toBeInTheDocument();
    // Not a badge, not a reason list, and not the "nothing changed" claim either.
    expect(within(row).queryByText("stale")).not.toBeInTheDocument();
    expect(within(row).queryByText(/Stale since publication/)).not.toBeInTheDocument();
    expect(within(row).queryByText(/No view or routine definition/)).not.toBeInTheDocument();
    expect(within(row).queryByRole("list", { name: "What changed since publication" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Could not check consumer-risk-context v2 for changes since publication. The coverage read was refused: NOT_FOUND.",
    );

    // The failure is not sticky: asking again reads again.
    fetchContextProductChangesSincePublished.mockResolvedValueOnce(STALE_ANSWER);
    fireEvent.click(within(row).getByRole("button", { name: "Check again" }));
    expect(await within(row).findByText("stale")).toBeInTheDocument();
    expect(within(row).queryByText("Not checked.")).not.toBeInTheDocument();
    expect(fetchContextProductChangesSincePublished).toHaveBeenCalledTimes(2);
  });

  it("claims nothing while the read is in flight", async () => {
    fetchContextProductChangesSincePublished.mockReturnValue(new Promise(() => {}));
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    expect(await within(row).findByRole("button", { name: "Checking…" })).toBeDisabled();
    expect(within(row).queryByText("stale")).not.toBeInTheDocument();
    expect(within(row).queryByText(/Checked\./)).not.toBeInTheDocument();
    expect(within(row).queryByText(/Not checked\./)).not.toBeInTheDocument();
  });

  it("still marks a version stale for a kind of change it has no wording for", async () => {
    // The presence of an entry is the server saying the product is stale; a code this client does
    // not know must be shown as itself, not filtered away into a reassuring silence.
    fetchContextProductChangesSincePublished.mockResolvedValue({
      total: 1,
      changes: [{ subjectKind: "TRIGGER", subjectId: "trg-1", change: "BODY_REWRITTEN", changeClass: null }],
    });
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    expect(await within(row).findByText("stale")).toBeInTheDocument();
    expect(within(row).getByText(/1 change to what v2 covers/)).toBeInTheDocument();
    expect(within(row).getByRole("listitem")).toHaveTextContent("trigger trg-1 — body rewritten");
  });

  it("lists the first few and counts the rest, from the server's total", async () => {
    const many = Array.from({ length: 8 }, (_, i) => ({
      subjectKind: "VIEW",
      subjectId: `view-${i + 1}`,
      change: "DEFINITION_CHANGED",
      changeClass: "LITERAL_ONLY",
    }));
    fetchContextProductChangesSincePublished.mockResolvedValue({ total: 25, changes: many });
    await openRegistry([PUBLISHED_PRODUCT]);
    const row = rowOf("Consumer risk analysis");

    fireEvent.click(within(row).getByRole("button", { name: "Check for changes" }));

    expect(await within(row).findByText(/25 changes to what v2 covers/)).toBeInTheDocument();
    expect(within(row).getAllByRole("listitem")).toHaveLength(5);
    expect(within(row).getByText("and 20 more.")).toBeInTheDocument();
    expect(within(row).getAllByText(/definition changed \(literal values only\)/)).toHaveLength(5);
  });

  it("asks about the row it was asked about and no other", async () => {
    fetchContextProductChangesSincePublished.mockResolvedValue(STALE_ANSWER);
    await openRegistry([PUBLISHED_PRODUCT, PAYMENTS_PRODUCT]);

    fireEvent.click(within(rowOf("Payments context")).getByRole("button", { name: "Check for changes" }));

    expect(await within(rowOf("Payments context")).findByText("stale")).toBeInTheDocument();
    expect(fetchContextProductChangesSincePublished).toHaveBeenCalledTimes(1);
    expect(fetchContextProductChangesSincePublished).toHaveBeenCalledWith("cpv_9", undefined);
    const other = rowOf("Consumer risk analysis");
    expect(within(other).queryByText("stale")).not.toBeInTheDocument();
    expect(within(other).getByRole("button", { name: "Check for changes" })).toBeEnabled();
  });

  it.each(["DRAFT", "REVIEW_REQUIRED", "REJECTED", "DEPRECATED", "RETIRED"])(
    "offers no check for a %s version: it is not what a consumer is served",
    async (status) => {
      // A version never published has no baseline to be stale against, and a deprecated or retired
      // one is no longer what anyone is given; offering the check would be offering a read the server
      // answers empty (or refuses), which the row would then have to explain.
      await openRegistry([
        { ...DRAFT_PRODUCT, latest_version: { ...DRAFT_PRODUCT.latest_version, status } },
      ]);

      expect(screen.queryByRole("button", { name: /Check for changes|Check again/ })).not.toBeInTheDocument();
    },
  );

  it("offers the check for a SUPPORTED version, which is still served", async () => {
    await openRegistry([
      { ...PUBLISHED_PRODUCT, latest_version: { ...PUBLISHED_PRODUCT.latest_version, status: "SUPPORTED" } },
    ]);

    expect(screen.getByRole("button", { name: "Check for changes" })).toBeEnabled();
  });

  it("has no WCAG A/AA violations with a stale, a current and an unchecked row on screen", async () => {
    fetchContextProductChangesSincePublished.mockImplementation(async (versionId) => {
      if (versionId === "cpv_1") return STALE_ANSWER;
      if (versionId === "cpv_9") return NOTHING_MOVED;
      throw new Error("The coverage read was refused: FORBIDDEN (ROLE_REQUIRED).");
    });
    const { container } = await openRegistry([PUBLISHED_PRODUCT, PAYMENTS_PRODUCT, SETTLEMENTS_PRODUCT]);

    for (const name of ["Consumer risk analysis", "Payments context", "Settlements context"]) {
      fireEvent.click(within(rowOf(name)).getByRole("button", { name: "Check for changes" }));
    }
    expect(await within(rowOf("Consumer risk analysis")).findByText("stale")).toBeInTheDocument();
    expect(await within(rowOf("Payments context")).findByText(/No view or routine definition/)).toBeInTheDocument();
    expect(await within(rowOf("Settlements context")).findByText("Not checked.")).toBeInTheDocument();

    await expectNoAxeViolations(container);
    expect(
      unnamedFocusableElements(container).map(
        (element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`,
      ),
    ).toEqual([]);
    // No row speaks for itself: the message strip is the only live region a check touches.
    for (const name of ["Consumer risk analysis", "Payments context", "Settlements context"]) {
      expect(rowOf(name).querySelector('[role="status"],[role="alert"],[aria-live]')).toBeNull();
    }
  });
});

/* ---- The ontology read, by role (R11-AUD01) ----------------------------------
   `GET /v1/organizations/{id}/ontology-versions` is admitted to DataSteward,
   MetadataAdmin, PlatformAdmin and Reviewer (surface-control matrix,
   `aida.ontology_api.list_ontology_versions`). The create panel sits in this
   screen's rail, so its ontology picker asked on every load for everyone: the
   demo rehearsal saw the 403 for `sam.agentdev`, and the picker rendered the
   server's "role is required" as if the steward had done something wrong.

   A session that is not admitted must send NO request for it, and the picker
   must say why it is not offered rather than sit empty (an empty list reads as
   "nothing has been approved yet", which is a claim about the estate). */

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});
const APPROVED_ONTOLOGY = {
  id: "ov1", ontology_id: "o1", ontology_key: "commerce", version: 2, base_version: 1, published_version: 2,
  status: "APPROVED", definition: {}, created_by: "a", approved_by: "b", governance_review_id: null,
};
const ONTOLOGY_REASON = /Only sessions holding DataSteward, MetadataAdmin, PlatformAdmin or Reviewer can read ontology versions/;

describe("ContextProductsScreen: the ontology picker, by role (R11-AUD01)", () => {
  it.each(["AgentDeveloper", "ToolDeveloper", "Analyst", "Viewer"])(
    "sends no ontology request as %s, and says why the control is not offered",
    async (role) => {
      sessionMe = asRoles(role);
      listOntologyVersions.mockResolvedValue([APPROVED_ONTOLOGY]);
      await openRegistry([DRAFT_PRODUCT]);

      // The other pickers are unaffected: this session still composes a draft.
      await waitFor(() => expect(fetchCatalogRows).toHaveBeenCalled());
      expect(await screen.findByRole("checkbox", { name: /core\.orders_raw/ })).toBeInTheDocument();
      expect(listOntologyVersions).not.toHaveBeenCalled();

      // One honest sentence, no control, and no error in the server's voice.
      expect(screen.getByText(ONTOLOGY_REASON)).toBeInTheDocument();
      expect(screen.getByText("Ontology versions")).toBeInTheDocument();
      expect(screen.queryByRole("checkbox", { name: /commerce/ })).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Filter Ontology versions")).not.toBeInTheDocument();
      expect(screen.queryByText(/could not be loaded|role is required/i)).not.toBeInTheDocument();
    },
  );

  it.each(["DataSteward", "MetadataAdmin", "PlatformAdmin", "Reviewer"])(
    "reads and offers ontology versions as %s",
    async (role) => {
      sessionMe = asRoles(role);
      listOntologyVersions.mockResolvedValue([APPROVED_ONTOLOGY]);
      await openRegistry([DRAFT_PRODUCT]);

      expect(await screen.findByRole("checkbox", { name: /commerce v2/ })).toBeInTheDocument();
      expect(listOntologyVersions).toHaveBeenCalledWith(
        "00000000-0000-0000-0000-000000000001", 0, expect.anything(),
      );
      expect(screen.queryByText(ONTOLOGY_REASON)).not.toBeInTheDocument();
    },
  );

  it("holds the ontology read while identity is in flight, then never sends it for a session that may not", async () => {
    // `/v1/me` has not answered: "connecting", `me` null. Nothing is asked for, and the picker says
    // neither "nothing approved" nor "not available to you" -- it does not know yet.
    sessionState = "connecting";
    listOntologyVersions.mockResolvedValue([APPROVED_ONTOLOGY]);
    const { rerender } = await openRegistry([DRAFT_PRODUCT]);
    await waitFor(() => expect(fetchCatalogRows).toHaveBeenCalled());
    expect(listOntologyVersions).not.toHaveBeenCalled();
    expect(screen.queryByText(ONTOLOGY_REASON)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /commerce/ })).not.toBeInTheDocument();

    // Identity arrives: an AgentDeveloper. The request was never made.
    sessionState = "connected";
    sessionMe = asRoles("AgentDeveloper");
    const { ContextProductsScreen } = await import("./ContextProductsScreen");
    rerender(<ContextProductsScreen />);

    expect(await screen.findByText(ONTOLOGY_REASON)).toBeInTheDocument();
    expect(listOntologyVersions).not.toHaveBeenCalled();
  });

  it("sends the ontology read once identity says the session may", async () => {
    sessionState = "connecting";
    listOntologyVersions.mockResolvedValue([APPROVED_ONTOLOGY]);
    const { rerender } = await openRegistry([DRAFT_PRODUCT]);
    await waitFor(() => expect(fetchCatalogRows).toHaveBeenCalled());
    expect(listOntologyVersions).not.toHaveBeenCalled();

    sessionState = "connected";
    sessionMe = asRoles("DataSteward");
    const { ContextProductsScreen } = await import("./ContextProductsScreen");
    rerender(<ContextProductsScreen />);

    expect(await screen.findByRole("checkbox", { name: /commerce v2/ })).toBeInTheDocument();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);
  });

  it("still asks when identity will not answer: the server stays the authority", async () => {
    // `/v1/me` failed: `me` is null and the state is not "connecting"; a held read would never end.
    sessionState = "disconnected";
    listOntologyVersions.mockResolvedValue([APPROVED_ONTOLOGY]);
    await openRegistry([DRAFT_PRODUCT]);

    expect(await screen.findByRole("checkbox", { name: /commerce v2/ })).toBeInTheDocument();
    expect(listOntologyVersions).toHaveBeenCalledTimes(1);
  });

  it("creates a draft as an AgentDeveloper without an ontology field", async () => {
    sessionMe = asRoles("AgentDeveloper");
    createContextProduct.mockResolvedValue(DRAFT_PRODUCT);
    await openRegistry([DRAFT_PRODUCT]);
    await screen.findByText(ONTOLOGY_REASON);

    fireEvent.change(screen.getByLabelText("Stable key"), { target: { value: "consumer-risk-context" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Consumer risk analysis" } });
    fireEvent.change(screen.getByLabelText("Owner principal"), { target: { value: "risk-data-stewards" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Bounded context for risk analysts." } });
    fireEvent.change(screen.getByLabelText("Approved purpose"), {
      target: { value: "Explain drivers of consumer delinquency for the monthly risk packet." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create governed draft" }));

    await waitFor(() => expect(createContextProduct).toHaveBeenCalledTimes(1));
    expect(createContextProduct.mock.calls[0]![1]).not.toHaveProperty("ontology_version_ids");
    expect(listOntologyVersions).not.toHaveBeenCalled();
  });

  it("keeps a new version's existing ontology bindings, and says so, for a session that cannot read them", async () => {
    // The draft is pre-filled from its base, and what the draft holds is what is sent: a session
    // that cannot READ ontology versions must not silently DROP the ones already bound.
    sessionMe = asRoles("AgentDeveloper");
    const bound = {
      ...PUBLISHED_PRODUCT,
      latest_version: { ...PUBLISHED_PRODUCT.latest_version, ontology_version_ids: ["ov1", "ov9"] },
    };
    postJson.mockResolvedValue({ ...bound.latest_version, id: "cpv_3", version: 3, status: "DRAFT" });
    await openRegistry([bound]);

    fireEvent.click(screen.getByRole("button", { name: "New version" }));
    const panel = await screen.findByRole("article", { name: "New version of consumer-risk-context" });
    expect(within(panel).getByText(/The 2 already bound to the previous version stay bound\./)).toBeInTheDocument();
    expect(within(panel).queryByRole("checkbox", { name: /commerce/ })).not.toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Create version draft" }));
    await waitFor(() => expect(postJson).toHaveBeenCalledTimes(1));
    expect(postJson.mock.calls[0]![1].ontology_version_ids).toEqual(["ov1", "ov9"]);
    expect(listOntologyVersions).not.toHaveBeenCalled();
  });

  it("has no WCAG A/AA violations with the ontology control withheld", async () => {
    sessionMe = asRoles("Viewer");
    const { container } = await openRegistry([DRAFT_PRODUCT]);
    await screen.findByText(ONTOLOGY_REASON);

    await expectNoAxeViolations(container);
    expect(
      unnamedFocusableElements(container).map(
        (element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`,
      ),
    ).toEqual([]);
  });
});

/* ---- The routine picker, by role -----------------------------------------------
   Found while proving the ontology fix against the live API as `sam.agentdev`
   (AgentDeveloper, ToolDeveloper, Analyst, Viewer): `GET .../context-product-
   routine-options` is admitted only to DataSteward, PlatformAdmin and
   SemanticAdmin (matrix row `list_context_product_routine_options`) -- narrower
   than the table, semantic and tool pickers, which that bundle can read. The
   read is issued once a project is chosen, which the demo rehearsal never does,
   so it never saw this one. Same defect, same remedy, one screen. */

const ROUTINE = {
  id: "r1", datasource_id: "ds_snowflake_prod", datasource_name: "snowflake_prod",
  schema_name: "core", name: "rebuild_totals", routine_type: "PROCEDURE", signature: "()",
};
const ROUTINE_REASON = /Only sessions holding DataSteward, PlatformAdmin or SemanticAdmin can read stored procedures and functions/;

describe("ContextProductsScreen: the routine picker, by role", () => {
  it.each(["AgentDeveloper", "ToolDeveloper", "Analyst", "Viewer", "MetadataAdmin"])(
    "sends no routine-options request as %s, and says why the control is not offered",
    async (role) => {
      sessionMe = asRoles(role);
      fetchContextProductRoutineOptions.mockResolvedValue([ROUTINE]);
      await openRegistry([DRAFT_PRODUCT]);

      expect(await screen.findByText(ROUTINE_REASON)).toBeInTheDocument();
      expect(fetchContextProductRoutineOptions).not.toHaveBeenCalled();
      expect(screen.queryByRole("checkbox", { name: /rebuild_totals/ })).not.toBeInTheDocument();
      expect(screen.queryByText(/could not be loaded|role is required|roles is required/i)).not.toBeInTheDocument();
      // The pickers this bundle IS admitted to are unaffected.
      expect(await screen.findByRole("checkbox", { name: /core\.orders_raw/ })).toBeInTheDocument();
    },
  );

  it.each(["DataSteward", "PlatformAdmin", "SemanticAdmin"])(
    "reads and offers routines as %s",
    async (role) => {
      sessionMe = asRoles(role);
      fetchContextProductRoutineOptions.mockResolvedValue([ROUTINE]);
      await openRegistry([DRAFT_PRODUCT]);

      expect(await screen.findByRole("checkbox", { name: /core\.rebuild_totals\(\)/ })).toBeInTheDocument();
      expect(fetchContextProductRoutineOptions).toHaveBeenCalledWith("proj_core", expect.anything());
      expect(screen.queryByText(ROUTINE_REASON)).not.toBeInTheDocument();
    },
  );

  it("keeps a new version's routines, and says so, for a session that cannot read them", async () => {
    sessionMe = asRoles("AgentDeveloper");
    const naming = {
      ...PUBLISHED_PRODUCT,
      latest_version: { ...PUBLISHED_PRODUCT.latest_version, routine_ids: ["r1"] },
    };
    postJson.mockResolvedValue({ ...naming.latest_version, id: "cpv_3", version: 3, status: "DRAFT" });
    await openRegistry([naming]);

    fireEvent.click(screen.getByRole("button", { name: "New version" }));
    const panel = await screen.findByRole("article", { name: "New version of consumer-risk-context" });
    expect(within(panel).getByText(/The 1 already named by the previous version stay named\./)).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Create version draft" }));
    await waitFor(() => expect(postJson).toHaveBeenCalledTimes(1));
    expect(postJson.mock.calls[0]![1].routine_ids).toEqual(["r1"]);
    expect(fetchContextProductRoutineOptions).not.toHaveBeenCalled();
  });

  it("withholds both controls from a bundle that may read neither, and stays free of WCAG violations", async () => {
    sessionMe = asRoles("AgentDeveloper", "ToolDeveloper", "Analyst", "Viewer");
    const { container } = await openRegistry([DRAFT_PRODUCT]);

    expect(await screen.findByText(ROUTINE_REASON)).toBeInTheDocument();
    expect(screen.getByText(ONTOLOGY_REASON)).toBeInTheDocument();
    expect(listOntologyVersions).not.toHaveBeenCalled();
    expect(fetchContextProductRoutineOptions).not.toHaveBeenCalled();
    await expectNoAxeViolations(container);
  });
});
