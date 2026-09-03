import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  DataSourceRead,
  DbtArtifactImportRead,
  DbtArtifactImportRequest,
  DbtLineageRead,
  DbtProjectCreate,
  DbtProjectRead,
  ProjectRead,
} from "../lib/types";
import type { DbtResourceQuery, DbtResourceRead } from "../lib/api";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Transformations, against the real `src/aida/dbt_api.py` routes. API
   boundary mocked the same way every other UX-15 screen test does
   (`SourcesScreen.test.tsx`/`ContextProductsScreen.test.tsx`) -- real payload
   shapes, asserting exact endpoint args, and one test specifically for the
   403 "dbt integration is disabled for this organization" detail string
   every dbt route can return (`_require_dbt_integration`, `dbt_api.py:138`),
   since that is real, distinct backend behavior this screen was told to
   surface rather than paper over with a generic error.
--------------------------------------------------------------------------- */

const fetchOrgProjects = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
const fetchOrgDatasources = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchDbtProjects = vi.fn<(projectId: string, signal?: AbortSignal) => Promise<PageOf<DbtProjectRead>>>();
const createDbtProject =
  vi.fn<(projectId: string, body: DbtProjectCreate, signal?: AbortSignal) => Promise<DbtProjectRead>>();
const fetchDbtArtifactImports =
  vi.fn<(dbtProjectId: string, signal?: AbortSignal) => Promise<PageOf<DbtArtifactImportRead>>>();
const importDbtManifest =
  vi.fn<(dbtProjectId: string, body: DbtArtifactImportRequest, signal?: AbortSignal) => Promise<DbtArtifactImportRead>>();
const fetchDbtResources =
  vi.fn<(artifactImportId: string, query: DbtResourceQuery, signal?: AbortSignal) => Promise<PageOf<DbtResourceRead>>>();
const fetchDbtLineage = vi.fn<(artifactImportId: string, signal?: AbortSignal) => Promise<DbtLineageRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgProjects: (organizationId: string, signal?: AbortSignal) => fetchOrgProjects(organizationId, signal),
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) => fetchOrgDatasources(organizationId, signal),
    fetchDbtProjects: (projectId: string, signal?: AbortSignal) => fetchDbtProjects(projectId, signal),
    createDbtProject: (projectId: string, body: DbtProjectCreate, signal?: AbortSignal) =>
      createDbtProject(projectId, body, signal),
    fetchDbtArtifactImports: (dbtProjectId: string, signal?: AbortSignal) => fetchDbtArtifactImports(dbtProjectId, signal),
    importDbtManifest: (dbtProjectId: string, body: DbtArtifactImportRequest, signal?: AbortSignal) =>
      importDbtManifest(dbtProjectId, body, signal),
    fetchDbtResources: (artifactImportId: string, query: DbtResourceQuery, signal?: AbortSignal) =>
      fetchDbtResources(artifactImportId, query, signal),
    fetchDbtLineage: (artifactImportId: string, signal?: AbortSignal) => fetchDbtLineage(artifactImportId, signal),
  };
});

const PROJECT: ProjectRead = {
  id: "proj_core", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DATASOURCE: DataSourceRead = {
  id: "ds_snowflake_prod", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", project_id: "proj_core", name: "snowflake_prod",
  connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PRODUCTION",
  network_zone: "default", credential_reference: "vault://x", max_concurrency: 8,
  status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
};

const DBT_PROJECT: DbtProjectRead = {
  id: "dbtproj_consumer_analytics", organization_id: "org1", project_id: "proj_core",
  datasource_id: "ds_snowflake_prod", project_key: "consumer_analytics",
  display_name: "Consumer analytics transformations", repository_url: null, target_name: "prod",
  status: "ACTIVE", created_by: "data-eng@tenant.example",
  created_at: "2026-07-01T00:00:00Z", updated_at: "2026-08-20T00:00:00Z",
};

const ARTIFACT: DbtArtifactImportRead = {
  id: "dbtimport_1", organization_id: "org1", dbt_project_id: "dbtproj_consumer_analytics",
  manifest_fingerprint: "f".repeat(64), dbt_schema_version: "https://schemas.getdbt.com/dbt/manifest/v12.json",
  dbt_version: "1.8.3", invocation_id: "inv-1", generated_at: "2026-08-20T09:00:00Z", status: "IMPORTED",
  resource_count: 2, model_count: 1, source_count: 1, test_count: 0,
  lineage_edge_count: 1, matched_resource_count: 1, unmatched_resource_count: 1,
  imported_by: "local-ui-admin", created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
};

const SOURCE_RESOURCE: DbtResourceRead = {
  id: "res_source", artifact_import_id: "dbtimport_1", unique_id: "source.consumer_analytics.raw.orders_raw",
  resource_type: "SOURCE", package_name: "consumer_analytics", name: "orders_raw",
  database_name: "analytics", schema_name: "raw", relation_name: "analytics.raw.orders_raw",
  materialization: null, original_file_path: "models/staging/src_raw.yml",
  description: "Raw order events.", compiled_sql_hash: null, compiled_sql_redacted: null,
  sql_parse_status: "NOT_PRESENT", column_names: ["order_id", "amount"],
  column_descriptions: { order_id: "Primary key." }, column_types: { order_id: "NUMBER", amount: "NUMBER(18,2)" },
  tags: ["raw"], depends_on_unique_ids: [], matched_table_id: "t_orders_raw",
  test_status: null, test_failures: null, test_execution_time: null, extra_metadata: {},
  created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
};

const MODEL_RESOURCE: DbtResourceRead = {
  id: "res_model", artifact_import_id: "dbtimport_1", unique_id: "model.consumer_analytics.stg_orders",
  resource_type: "MODEL", package_name: "consumer_analytics", name: "stg_orders",
  database_name: "analytics", schema_name: "staging", relation_name: "analytics.staging.stg_orders",
  materialization: "view", original_file_path: "models/staging/stg_orders.sql",
  description: "Staged orders.", compiled_sql_hash: "a".repeat(64),
  compiled_sql_redacted: "SELECT order_id FROM analytics.raw.orders_raw",
  sql_parse_status: "PARSED", column_names: ["order_id"], column_descriptions: {}, column_types: { order_id: "NUMBER" },
  tags: ["staging"], depends_on_unique_ids: ["source.consumer_analytics.raw.orders_raw"], matched_table_id: null,
  test_status: "FAIL", test_failures: 2, test_execution_time: 0.5, extra_metadata: {},
  created_at: "2026-08-20T09:00:05Z", updated_at: "2026-08-20T09:00:05Z",
};

const LINEAGE: DbtLineageRead = {
  artifact_import_id: "dbtimport_1",
  nodes: [
    { id: "res_source", unique_id: SOURCE_RESOURCE.unique_id, label: "orders_raw", resource_type: "SOURCE", materialization: null, matched_table_id: "t_orders_raw", test_status: null },
    { id: "res_model", unique_id: MODEL_RESOURCE.unique_id, label: "stg_orders", resource_type: "MODEL", materialization: "view", matched_table_id: null, test_status: "FAIL" },
  ],
  edges: [
    { id: "res_source->res_model", source_resource_id: "res_source", target_resource_id: "res_model", edge_type: "DEPENDS_ON", source_column: "", target_column: "" },
  ],
  resource_count: 2, edge_count: 1, catalog_match_count: 1,
};

async function loadScreen() {
  const { TransformationsScreen } = await import("./TransformationsScreen");
  return TransformationsScreen;
}

beforeEach(() => {
  fetchOrgProjects.mockReset();
  fetchOrgDatasources.mockReset();
  fetchDbtProjects.mockReset();
  createDbtProject.mockReset();
  fetchDbtArtifactImports.mockReset();
  importDbtManifest.mockReset();
  fetchDbtResources.mockReset();
  fetchDbtLineage.mockReset();
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TransformationsScreen against the real dbt_api.py routes", () => {
  it("shows the empty-before-project-selection state without listing dbt projects", async () => {
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);

    await waitFor(() => expect(screen.getByText("Pick a delivery project")).toBeInTheDocument());
    expect(fetchOrgProjects).toHaveBeenCalledWith("00000000-0000-0000-0000-000000000001", expect.anything());
    expect(fetchDbtProjects).not.toHaveBeenCalled();
  });

  it("selecting a delivery project loads dbt projects and auto-selects the first one", async () => {
    fetchDbtProjects.mockResolvedValue({ items: [DBT_PROJECT], limit: 500, offset: 0, total: 1 });
    fetchDbtArtifactImports.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);

    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });

    await waitFor(() => expect(fetchDbtProjects).toHaveBeenCalledWith("proj_core", expect.anything()));
    expect(await screen.findByText("Consumer analytics transformations")).toBeInTheDocument();
    expect(screen.getByText("snowflake_prod")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchDbtArtifactImports).toHaveBeenCalledWith("dbtproj_consumer_analytics", expect.anything()),
    );
    expect(new URLSearchParams(location.search).get("dbtProject")).toBe("dbtproj_consumer_analytics");
  });

  it("renders the disabled-integration 403 with the real backend detail string, not a generic error", async () => {
    fetchDbtProjects.mockRejectedValue(new ApiError(403, "dbt integration is disabled for this organization"));
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);

    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });

    expect(await screen.findByText("dbt integration is disabled for this organization")).toBeInTheDocument();
    // The exact server detail string renders, not a generic "could not be loaded" banner.
    expect(screen.queryByText("Transformation metadata could not be loaded")).not.toBeInTheDocument();
  });

  it("selecting the auto-selected import loads metrics, resources, and lineage edges", async () => {
    fetchDbtProjects.mockResolvedValue({ items: [DBT_PROJECT], limit: 500, offset: 0, total: 1 });
    fetchDbtArtifactImports.mockResolvedValue({ items: [ARTIFACT], limit: 100, offset: 0, total: 1 });
    fetchDbtResources.mockResolvedValue({ items: [SOURCE_RESOURCE, MODEL_RESOURCE], limit: 500, offset: 0, total: 2 });
    fetchDbtLineage.mockResolvedValue(LINEAGE);
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);

    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });

    await waitFor(() =>
      expect(fetchDbtResources).toHaveBeenCalledWith(
        "dbtimport_1",
        { resourceType: null, matched: null, limit: 500 },
        expect.anything(),
      ),
    );
    await waitFor(() => expect(screen.getAllByText("orders_raw").length).toBeGreaterThan(0));
    expect(screen.getAllByText("stg_orders").length).toBeGreaterThan(0);
    // Metrics tile reads straight from the artifact's own counters.
    expect(screen.getByText("Models").nextSibling).toHaveTextContent("1");
    // Lineage edges render as a flat source -> target list (edge-list mode
    // only -- the legacy Cytoscape DAG canvas is out of scope here). Node
    // labels also appear in the resource table above, so this assertion is
    // scoped to just the edges section.
    // "Lineage edges" also labels a metrics tile above, so query the section heading specifically.
    const edgesPanel = screen.getByRole("heading", { name: "Lineage edges", level: 2 }).closest("section")!;
    expect(within(edgesPanel).getByText("orders_raw")).toBeInTheDocument();
    expect(within(edgesPanel).getByText("stg_orders")).toBeInTheDocument();
  });

  it("selecting a resource shows its detail pane with columns and redacted compiled SQL", async () => {
    fetchDbtProjects.mockResolvedValue({ items: [DBT_PROJECT], limit: 500, offset: 0, total: 1 });
    fetchDbtArtifactImports.mockResolvedValue({ items: [ARTIFACT], limit: 100, offset: 0, total: 1 });
    fetchDbtResources.mockResolvedValue({ items: [SOURCE_RESOURCE, MODEL_RESOURCE], limit: 500, offset: 0, total: 2 });
    fetchDbtLineage.mockResolvedValue(LINEAGE);
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);
    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getAllByText("stg_orders").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole("button", { name: /stg_orders/ }));

    const panel = await screen.findByLabelText("Detail for stg_orders");
    expect(panel).toHaveTextContent("analytics.staging.stg_orders");
    expect(panel).toHaveTextContent("order_id");
    expect(panel).toHaveTextContent("SELECT order_id FROM analytics.raw.orders_raw");
    expect(panel).toHaveTextContent("fail");
    expect(new URLSearchParams(location.search).get("resource")).toBe("res_model");
  });

  it("changing the resource-type filter re-fetches resources with the real query param, not client-side", async () => {
    fetchDbtProjects.mockResolvedValue({ items: [DBT_PROJECT], limit: 500, offset: 0, total: 1 });
    fetchDbtArtifactImports.mockResolvedValue({ items: [ARTIFACT], limit: 100, offset: 0, total: 1 });
    fetchDbtResources.mockResolvedValue({ items: [SOURCE_RESOURCE, MODEL_RESOURCE], limit: 500, offset: 0, total: 2 });
    fetchDbtLineage.mockResolvedValue(LINEAGE);
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);
    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });
    await waitFor(() => expect(screen.getAllByText("stg_orders").length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText("Resource type"), { target: { value: "MODEL" } });

    await waitFor(() =>
      expect(fetchDbtResources).toHaveBeenLastCalledWith(
        "dbtimport_1",
        { resourceType: "MODEL", matched: null, limit: 500 },
        expect.anything(),
      ),
    );
  });

  it("registers a dbt project through the real create endpoint and selects it", async () => {
    fetchDbtProjects.mockResolvedValueOnce({ items: [], limit: 500, offset: 0, total: 0 });
    fetchDbtProjects.mockResolvedValue({ items: [DBT_PROJECT], limit: 500, offset: 0, total: 1 });
    fetchDbtArtifactImports.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    createDbtProject.mockResolvedValue(DBT_PROJECT);
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);
    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });
    await screen.findByText("No dbt projects registered");

    fireEvent.click(screen.getByRole("button", { name: "Register dbt project" }));
    fireEvent.change(screen.getByLabelText("Project key"), { target: { value: "consumer_analytics" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Consumer analytics transformations" } });
    fireEvent.click(screen.getByRole("button", { name: "Register dbt project" }));

    await waitFor(() =>
      expect(createDbtProject).toHaveBeenCalledWith(
        "proj_core",
        {
          project_key: "consumer_analytics",
          display_name: "Consumer analytics transformations",
          datasource_id: "ds_snowflake_prod",
          repository_url: null,
          target_name: "prod",
        },
        undefined,
      ),
    );
    expect(new URLSearchParams(location.search).get("dbtProject")).toBe("dbtproj_consumer_analytics");
  });

  it("surfaces a real create conflict (409) without silently succeeding", async () => {
    fetchDbtProjects.mockResolvedValue({ items: [DBT_PROJECT], limit: 500, offset: 0, total: 1 });
    fetchDbtArtifactImports.mockResolvedValue({ items: [], limit: 100, offset: 0, total: 0 });
    createDbtProject.mockRejectedValue(new ApiError(409, "dbt project key already exists"));
    const TransformationsScreen = await loadScreen();
    render(<TransformationsScreen />);
    fireEvent.change(await screen.findByLabelText("Delivery project"), { target: { value: "proj_core" } });
    await screen.findByText("Consumer analytics transformations");

    fireEvent.click(screen.getByRole("button", { name: "Register dbt project" }));
    fireEvent.change(screen.getByLabelText("Project key"), { target: { value: "consumer_analytics" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Duplicate" } });
    fireEvent.click(screen.getByRole("button", { name: "Register dbt project" }));

    expect(await screen.findByText("dbt project key already exists")).toBeInTheDocument();
  });
});
