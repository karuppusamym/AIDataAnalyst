import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  BiArtifactImportRead,
  BiConnectionCreate,
  BiConnectionRead,
  DataSourceRead,
  ProjectRead,
  SourceBindingDecision,
  SourceBindingRead,
  WorkspaceMembershipCreate,
  WorkspaceMembershipRead,
  WorkspaceRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   WorkspaceAccessScreen -- against the real `POST/GET /v1/workspaces/{id}/members`
   (`workspace_api.py:160`/`207`), `POST /v1/source-bindings/{id}/decision`
   (`workspace_api.py:293`), and `GET/POST /v1/projects/{id}/bi-connections` +
   `POST /v1/bi-connections/{id}/artifact-imports` (`bi_api.py`). API boundary
   mocked, matching `AdministrationScreen.test.tsx`'s established pattern --
   real payload shapes, asserting exact endpoint args, not superficial
   snapshots. `fetchOrgWorkspaces`/`fetchOrgProjects`/`fetchOrgDatasources`/
   `fetchWorkspaceSourceBindings` are reused, already-merged reads; only the
   six functions this screen adds are asserted against call args below.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchOrgWorkspaces = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<WorkspaceRead>>>();
const fetchOrgProjects = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
const fetchOrgDatasources = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchWorkspaceSourceBindings = vi.fn<(workspaceId: string, signal?: AbortSignal) => Promise<PageOf<SourceBindingRead>>>();
const fetchWorkspaceMembers = vi.fn<(workspaceId: string, signal?: AbortSignal) => Promise<PageOf<WorkspaceMembershipRead>>>();
const addWorkspaceMember =
  vi.fn<(workspaceId: string, body: WorkspaceMembershipCreate, signal?: AbortSignal) => Promise<WorkspaceMembershipRead>>();
const decideSourceBinding =
  vi.fn<(bindingId: string, body: SourceBindingDecision, signal?: AbortSignal) => Promise<SourceBindingRead>>();
const fetchProjectBiConnections =
  vi.fn<(projectId: string, opts?: { limit?: number; offset?: number }, signal?: AbortSignal) => Promise<PageOf<BiConnectionRead>>>();
const createBiConnection =
  vi.fn<(projectId: string, body: BiConnectionCreate, signal?: AbortSignal) => Promise<BiConnectionRead>>();
const importBiArtifact =
  vi.fn<(connectionId: string, body: { bi_tool: string; artifact: Record<string, unknown> }, signal?: AbortSignal) => Promise<BiArtifactImportRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgWorkspaces: (organizationId: string, signal?: AbortSignal) => fetchOrgWorkspaces(organizationId, signal),
    fetchOrgProjects: (organizationId: string, signal?: AbortSignal) => fetchOrgProjects(organizationId, signal),
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) => fetchOrgDatasources(organizationId, signal),
    fetchWorkspaceSourceBindings: (workspaceId: string, signal?: AbortSignal) =>
      fetchWorkspaceSourceBindings(workspaceId, signal),
    fetchWorkspaceMembers: (workspaceId: string, signal?: AbortSignal) => fetchWorkspaceMembers(workspaceId, signal),
    addWorkspaceMember: (workspaceId: string, body: WorkspaceMembershipCreate, signal?: AbortSignal) =>
      addWorkspaceMember(workspaceId, body, signal),
    decideSourceBinding: (bindingId: string, body: SourceBindingDecision, signal?: AbortSignal) =>
      decideSourceBinding(bindingId, body, signal),
    fetchProjectBiConnections: (projectId: string, opts?: { limit?: number; offset?: number }, signal?: AbortSignal) =>
      fetchProjectBiConnections(projectId, opts, signal),
    createBiConnection: (projectId: string, body: BiConnectionCreate, signal?: AbortSignal) =>
      createBiConnection(projectId, body, signal),
    importBiArtifact: (connectionId: string, body: { bi_tool: string; artifact: Record<string, unknown> }, signal?: AbortSignal) =>
      importBiArtifact(connectionId, body, signal),
  };
});

const WORKSPACE: WorkspaceRead = {
  id: "ws_governed_analytics", organization_id: ORG, isolation_boundary_id: null,
  name: "Governed analytics", slug: "governed-analytics", purpose: "Curated analysis",
  status: "ACTIVE", monthly_cost_ceiling: null,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const PROJECT: ProjectRead = {
  id: "proj_core", organization_id: ORG, line_of_business_id: "lob_fin", data_domain_id: "dom_fin",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DATASOURCE: DataSourceRead = {
  id: "ds_snowflake_prod", organization_id: ORG, line_of_business_id: "lob_fin", data_domain_id: "dom_fin",
  project_id: "proj_core", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", network_zone: "default", credential_reference: "vault://ds/snowflake_prod",
  max_concurrency: 8, status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
};

const MEMBER: WorkspaceMembershipRead = {
  id: "member_1", organization_id: ORG, workspace_id: "ws_governed_analytics",
  principal_id: "priya.iyer", principal_kind: "HUMAN", role: "analyst", granted_by: "local-ui-admin",
  expires_at: null, status: "ACTIVE", created_at: "2026-02-01T00:00:00Z", updated_at: "2026-02-01T00:00:00Z",
};

const PENDING_BINDING: SourceBindingRead = {
  id: "binding_pending", organization_id: ORG, workspace_id: "ws_governed_analytics",
  datasource_id: "ds_snowflake_prod", schema_scope: [], permitted_classifications: [],
  masking_profile: "DEFAULT", purpose: "Quarterly reconciliation", max_query_cost: null,
  status: "PENDING_APPROVAL", requested_by: "priya.iyer", approved_by: null, approved_at: null,
  expires_at: null, created_at: "2026-08-20T00:00:00Z", updated_at: "2026-08-20T00:00:00Z",
};

const BI_CONNECTION: BiConnectionRead = {
  id: "bi_conn_1", organization_id: ORG, project_id: "proj_core", datasource_id: "ds_snowflake_prod",
  bi_tool: "TABLEAU", connection_key: "finance-tableau-prod", display_name: "Finance Tableau (Production)",
  site_or_workspace: "finance", status: "ACTIVE", created_by: "local-ui-admin",
  created_at: "2026-03-01T00:00:00Z", updated_at: "2026-03-01T00:00:00Z",
};

function mockBaseSummary() {
  fetchOrgWorkspaces.mockResolvedValue({ items: [WORKSPACE], limit: 200, offset: 0, total: 1 });
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchWorkspaceMembers.mockResolvedValue({ items: [MEMBER], limit: 1, offset: 0, total: 1 });
  fetchWorkspaceSourceBindings.mockResolvedValue({ items: [PENDING_BINDING], limit: 1, offset: 0, total: 1 });
  fetchProjectBiConnections.mockResolvedValue({ items: [BI_CONNECTION], limit: 100, offset: 0, total: 1 });
}

async function loadScreen() {
  const { WorkspaceAccessScreen } = await import("./WorkspaceAccessScreen");
  return WorkspaceAccessScreen;
}

beforeEach(() => {
  fetchOrgWorkspaces.mockReset();
  fetchOrgProjects.mockReset();
  fetchOrgDatasources.mockReset();
  fetchWorkspaceSourceBindings.mockReset();
  fetchWorkspaceMembers.mockReset();
  addWorkspaceMember.mockReset();
  decideSourceBinding.mockReset();
  fetchProjectBiConnections.mockReset();
  createBiConnection.mockReset();
  importBiArtifact.mockReset();
  vi.resetModules();
  history.replaceState(null, "", "/");
});

describe("WorkspaceAccessScreen against the real membership/binding-decision/BI endpoints", () => {
  it("loads the first workspace's members and pending bindings, and the first project's BI connections", async () => {
    mockBaseSummary();
    const WorkspaceAccessScreen = await loadScreen();

    render(<WorkspaceAccessScreen />);

    await waitFor(() => expect(screen.getByText("priya.iyer")).toBeInTheDocument());
    expect(fetchWorkspaceMembers).toHaveBeenCalledWith("ws_governed_analytics", expect.anything());
    expect(fetchWorkspaceSourceBindings).toHaveBeenCalledWith("ws_governed_analytics", expect.anything());
    expect(fetchProjectBiConnections).toHaveBeenCalledWith("proj_core", undefined, expect.anything());
    expect(screen.getByText("Quarterly reconciliation")).toBeInTheDocument();
    expect(screen.getByText("Finance Tableau (Production)")).toBeInTheDocument();
  });

  it("submits Add member with the exact WorkspaceMembershipCreate payload", async () => {
    mockBaseSummary();
    const created: WorkspaceMembershipRead = {
      ...MEMBER, id: "member_new", principal_id: "jordan.reyes", role: "steward",
    };
    addWorkspaceMember.mockResolvedValue(created);
    const WorkspaceAccessScreen = await loadScreen();
    render(<WorkspaceAccessScreen />);
    await waitFor(() => expect(fetchWorkspaceMembers).toHaveBeenCalled());

    const form = screen.getByRole("form", { name: "Add workspace member" });
    fireEvent.change(within(form).getByPlaceholderText("jordan.reyes"), { target: { value: "jordan.reyes" } });
    fireEvent.change(within(form).getByLabelText("Role"), { target: { value: "steward" } });
    fireEvent.submit(form);

    await waitFor(() =>
      expect(addWorkspaceMember).toHaveBeenCalledWith(
        "ws_governed_analytics",
        { principal_id: "jordan.reyes", principal_kind: "HUMAN", role: "steward", expires_at: null },
        undefined,
      ),
    );
    expect(await screen.findByText(/Added "jordan.reyes"/)).toBeInTheDocument();
  });

  it("approves a pending binding with a rationale, calling the exact SourceBindingDecision payload", async () => {
    mockBaseSummary();
    const decided: SourceBindingRead = { ...PENDING_BINDING, status: "ACTIVE", approved_by: "local-ui-admin" };
    decideSourceBinding.mockResolvedValue(decided);
    const WorkspaceAccessScreen = await loadScreen();
    render(<WorkspaceAccessScreen />);
    await waitFor(() => expect(screen.getByText("Quarterly reconciliation")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Rationale for snowflake_prod binding decision"), {
      target: { value: "Reviewed against policy POL-14" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(decideSourceBinding).toHaveBeenCalledWith(
        "binding_pending",
        { decision: "APPROVE", valid_for_days: 365, rationale: "Reviewed against policy POL-14" },
        undefined,
      ),
    );
    // Decided binding drops out of the pending list.
    await waitFor(() => expect(screen.queryByText("Quarterly reconciliation")).not.toBeInTheDocument());
  });

  it("surfaces the maker-checker 409 detail verbatim instead of swallowing it", async () => {
    mockBaseSummary();
    decideSourceBinding.mockRejectedValue(new ApiError(409, "maker-checker separation is required"));
    const WorkspaceAccessScreen = await loadScreen();
    render(<WorkspaceAccessScreen />);
    await waitFor(() => expect(screen.getByText("Quarterly reconciliation")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(await screen.findByText("maker-checker separation is required")).toBeInTheDocument();
    // The binding stays in the pending list since the decision was rejected by the server.
    expect(screen.getByText("Quarterly reconciliation")).toBeInTheDocument();
  });

  it("submits Register BI connection with the exact BiConnectionCreate payload", async () => {
    mockBaseSummary();
    const created: BiConnectionRead = { ...BI_CONNECTION, id: "bi_conn_new", connection_key: "retail-tableau" };
    createBiConnection.mockResolvedValue(created);
    const WorkspaceAccessScreen = await loadScreen();
    render(<WorkspaceAccessScreen />);
    await waitFor(() => expect(fetchProjectBiConnections).toHaveBeenCalled());

    const form = screen.getByRole("form", { name: "Register BI connection" });
    fireEvent.change(within(form).getByLabelText("Project source"), { target: { value: "ds_snowflake_prod" } });
    fireEvent.change(within(form).getByPlaceholderText("finance-tableau-prod"), { target: { value: "retail-tableau" } });
    fireEvent.change(within(form).getByPlaceholderText("Finance Tableau (Production)"), {
      target: { value: "Retail Tableau" },
    });
    fireEvent.submit(form);

    await waitFor(() =>
      expect(createBiConnection).toHaveBeenCalledWith(
        "proj_core",
        {
          datasource_id: "ds_snowflake_prod",
          bi_tool: "TABLEAU",
          connection_key: "retail-tableau",
          display_name: "Retail Tableau",
          site_or_workspace: null,
        },
        undefined,
      ),
    );
  });

  it("parses the artifact textarea as JSON and posts BiArtifactImportRequest, reporting invalid JSON without calling the API", async () => {
    mockBaseSummary();
    const importResult: BiArtifactImportRead = {
      id: "bi_import_1", organization_id: ORG, connection_id: "bi_conn_1",
      artifact_fingerprint: "sha256:abc", bi_tool: "TABLEAU", generated_at: "2026-09-03T00:00:00Z",
      status: "COMPLETED", report_count: 2, metric_count: 5, report_metric_edge_count: 10,
      metric_column_edge_count: 3, matched_column_count: 3, unmatched_column_count: 2,
      imported_by: "local-ui-admin", created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    importBiArtifact.mockResolvedValue(importResult);
    const WorkspaceAccessScreen = await loadScreen();
    render(<WorkspaceAccessScreen />);
    await waitFor(() => expect(screen.getByText("Finance Tableau (Production)")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Import artifact" }));
    const importForm = screen.getByRole("form", { name: "Import BI artifact for connection bi_conn_1" });

    fireEvent.change(within(importForm).getByLabelText("Artifact JSON"), { target: { value: "{not json" } });
    fireEvent.submit(importForm);
    expect(await screen.findByText("Artifact is not valid JSON.")).toBeInTheDocument();
    expect(importBiArtifact).not.toHaveBeenCalled();

    fireEvent.change(within(importForm).getByLabelText("Artifact JSON"), {
      target: { value: '{"reports": [1, 2], "metrics": [1, 2, 3, 4, 5]}' },
    });
    fireEvent.submit(importForm);

    await waitFor(() =>
      expect(importBiArtifact).toHaveBeenCalledWith(
        "bi_conn_1",
        { bi_tool: "TABLEAU", artifact: { reports: [1, 2], metrics: [1, 2, 3, 4, 5] } },
        undefined,
      ),
    );
    expect(await screen.findByText(/Imported: 2 reports, 5 metrics/)).toBeInTheDocument();
  });

  it("surfaces the BI-integration-disabled 403 detail for the connections list", async () => {
    mockBaseSummary();
    fetchProjectBiConnections.mockReset();
    fetchProjectBiConnections.mockRejectedValue(
      new ApiError(403, "bi integration is not enabled for this organization"),
    );
    const WorkspaceAccessScreen = await loadScreen();
    render(<WorkspaceAccessScreen />);

    expect(await screen.findByText("bi integration is not enabled for this organization")).toBeInTheDocument();
  });
});
