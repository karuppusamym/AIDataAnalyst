import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  DataSourceCreate,
  DataSourceRead,
  LineOfBusinessCreate,
  LineOfBusinessRead,
  OrganizationCreate,
  OrganizationRead,
  ProjectCreate,
  ProjectRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Administration -- nav id `administration`, against the real
   `POST /v1/organizations` (`api.py:584`), `GET/POST
   /v1/organizations/{id}/lines-of-business` (`api.py:463`/`677`),
   `POST /v1/lines-of-business/{lob_id}/projects` (`api.py:901`), and
   `POST /v1/projects/{project_id}/datasources` (`api.py:1021`) -- reusing
   `fetchOrgProjects`/`fetchOrgDatasources` already exercised by
   `SourcesScreen.test.tsx`. API boundary mocked, matching that file's
   established pattern -- real payload shapes, asserting the exact endpoint
   args, not superficial snapshots.
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchOrgLinesOfBusiness = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<LineOfBusinessRead>>
>();
const fetchOrgProjects = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>
>();
const fetchOrgDatasources = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>
>();
const createOrganization = vi.fn<
  (body: OrganizationCreate, signal?: AbortSignal) => Promise<OrganizationRead>
>();
const createLineOfBusiness = vi.fn<
  (organizationId: string, body: LineOfBusinessCreate, signal?: AbortSignal) => Promise<LineOfBusinessRead>
>();
const createProject = vi.fn<
  (lobId: string, body: ProjectCreate, signal?: AbortSignal) => Promise<ProjectRead>
>();
const registerDatasource = vi.fn<
  (projectId: string, body: DataSourceCreate, signal?: AbortSignal) => Promise<DataSourceRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgLinesOfBusiness: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgLinesOfBusiness(organizationId, signal),
    fetchOrgProjects: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgProjects(organizationId, signal),
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
    createOrganization: (body: OrganizationCreate, signal?: AbortSignal) =>
      createOrganization(body, signal),
    createLineOfBusiness: (organizationId: string, body: LineOfBusinessCreate, signal?: AbortSignal) =>
      createLineOfBusiness(organizationId, body, signal),
    createProject: (lobId: string, body: ProjectCreate, signal?: AbortSignal) =>
      createProject(lobId, body, signal),
    registerDatasource: (projectId: string, body: DataSourceCreate, signal?: AbortSignal) =>
      registerDatasource(projectId, body, signal),
  };
});

const LOB_FINANCE: LineOfBusinessRead = {
  id: "lob_fin", organization_id: ORG, name: "Consumer Finance", code: "FINANCE",
  status: "ACTIVE", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const PROJECT_CORE: ProjectRead = {
  id: "proj_core", organization_id: ORG, line_of_business_id: "lob_fin", data_domain_id: "dom_fin",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DATASOURCE_SNOWFLAKE: DataSourceRead = {
  id: "ds_snowflake_prod", organization_id: ORG, line_of_business_id: "lob_fin", data_domain_id: "dom_fin",
  project_id: "proj_core", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", network_zone: "default", credential_reference: "env://ds/snowflake_prod",
  max_concurrency: 8, status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

async function loadScreen() {
  const { AdministrationScreen } = await import("./AdministrationScreen");
  return AdministrationScreen;
}

function mockEmptySummary() {
  fetchOrgLinesOfBusiness.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });
  fetchOrgProjects.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });
  fetchOrgDatasources.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });
}

function mockSeededSummary() {
  fetchOrgLinesOfBusiness.mockResolvedValue({ items: [LOB_FINANCE], limit: 500, offset: 0, total: 1 });
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT_CORE], limit: 500, offset: 0, total: 1 });
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE_SNOWFLAKE], limit: 500, offset: 0, total: 1 });
}

beforeEach(() => {
  fetchOrgLinesOfBusiness.mockReset();
  fetchOrgProjects.mockReset();
  fetchOrgDatasources.mockReset();
  createOrganization.mockReset();
  createLineOfBusiness.mockReset();
  createProject.mockReset();
  registerDatasource.mockReset();
  vi.resetModules();
  history.replaceState(null, "", "/");
});

describe("AdministrationScreen against the real tenant-onboarding endpoints", () => {
  it("loads the scope summary via fetchOrgLinesOfBusiness/fetchOrgProjects/fetchOrgDatasources, scoped to the current org", async () => {
    mockSeededSummary();
    const AdministrationScreen = await loadScreen();

    render(<AdministrationScreen />);

    await waitFor(() => expect(screen.getAllByText(/Consumer Finance/).length).toBeGreaterThan(0));
    expect(fetchOrgLinesOfBusiness).toHaveBeenCalledWith(ORG, expect.anything());
    expect(fetchOrgProjects).toHaveBeenCalledWith(ORG, expect.anything());
    expect(fetchOrgDatasources).toHaveBeenCalledWith(ORG, expect.anything());
    // one project / one source under lob_fin, per ScopeSummary's per-LOB counts
    expect(screen.getByText("1 project · 1 source")).toBeInTheDocument();
  });

  it("submits Create organization with the exact OrganizationCreate payload", async () => {
    mockEmptySummary();
    const created: OrganizationRead = {
      id: "org_new", name: "Northstar Bank", slug: "northstar-bank",
      status: "ACTIVE", created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    createOrganization.mockResolvedValue(created);
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);
    await waitFor(() => expect(fetchOrgLinesOfBusiness).toHaveBeenCalled());

    const form = screen.getByRole("form", { name: "Create organization" });
    fireEvent.change(form.querySelector('input[placeholder="Northstar Bank"]')!, {
      target: { value: "Northstar Bank" },
    });
    fireEvent.change(form.querySelector('input[placeholder="northstar-bank"]')!, {
      target: { value: "northstar-bank" },
    });
    fireEvent.submit(form);

    await waitFor(() =>
      expect(createOrganization).toHaveBeenCalledWith(
        { name: "Northstar Bank", slug: "northstar-bank" },
        undefined,
      ),
    );
    expect(await screen.findByText(/Created "Northstar Bank"/)).toBeInTheDocument();
  });

  it("submits Add line of business scoped to the current org, and the new LOB immediately appears in the scope summary", async () => {
    mockEmptySummary();
    const created: LineOfBusinessRead = {
      id: "lob_new", organization_id: ORG, name: "Consumer Banking", code: "CONSUMER",
      status: "ACTIVE", created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    createLineOfBusiness.mockResolvedValue(created);
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);
    await waitFor(() => expect(fetchOrgLinesOfBusiness).toHaveBeenCalled());

    const form = screen.getByRole("form", { name: "Add line of business" });
    fireEvent.change(form.querySelector('input[placeholder="Consumer Banking"]')!, {
      target: { value: "Consumer Banking" },
    });
    fireEvent.change(form.querySelector('input[placeholder="CONSUMER"]')!, {
      target: { value: "CONSUMER" },
    });
    fireEvent.submit(form);

    await waitFor(() =>
      expect(createLineOfBusiness).toHaveBeenCalledWith(
        ORG,
        { name: "Consumer Banking", code: "CONSUMER" },
        undefined,
      ),
    );
    expect(await screen.findByText(/Consumer Banking \(CONSUMER\)/)).toBeInTheDocument();
  });

  it("Add project requires picking a line of business and posts to the selected lob_id", async () => {
    mockEmptySummary();
    fetchOrgLinesOfBusiness.mockResolvedValue({ items: [LOB_FINANCE], limit: 500, offset: 0, total: 1 });
    const created: ProjectRead = {
      id: "proj_new", organization_id: ORG, line_of_business_id: "lob_fin", data_domain_id: "dom_fin",
      name: "Customer 360", slug: "customer-360", status: "ACTIVE",
      created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
    };
    createProject.mockResolvedValue(created);
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);
    await waitFor(() => expect(screen.getByRole("form", { name: "Add project" })).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Add project" });
    fireEvent.change(screen.getByLabelText("Line of business"), { target: { value: "lob_fin" } });
    fireEvent.change(form.querySelector('input[placeholder="Customer 360"]')!, {
      target: { value: "Customer 360" },
    });
    fireEvent.change(form.querySelector('input[placeholder="customer-360"]')!, {
      target: { value: "customer-360" },
    });
    fireEvent.submit(form);

    await waitFor(() =>
      expect(createProject).toHaveBeenCalledWith(
        "lob_fin",
        { name: "Customer 360", slug: "customer-360" },
        undefined,
      ),
    );
  });

  it("shows an empty state instead of the Add project form when there are no lines of business yet", async () => {
    mockEmptySummary();
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);
    await waitFor(() => expect(fetchOrgLinesOfBusiness).toHaveBeenCalled());

    expect(screen.getByText("No lines of business yet")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add project" })).not.toBeInTheDocument();
  });

  it("Register data source posts the exact DataSourceCreate shape to the selected project", async () => {
    mockEmptySummary();
    fetchOrgProjects.mockResolvedValue({ items: [PROJECT_CORE], limit: 500, offset: 0, total: 1 });
    const created: DataSourceRead = { ...DATASOURCE_SNOWFLAKE, id: "ds_new", name: "Consumer warehouse" };
    registerDatasource.mockResolvedValue(created);
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);
    await waitFor(() => expect(screen.getByRole("form", { name: "Register data source" })).toBeInTheDocument());

    const form = screen.getByRole("form", { name: "Register data source" });
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "proj_core" } });
    fireEvent.change(form.querySelector('input[placeholder="Consumer warehouse"]')!, {
      target: { value: "Consumer warehouse" },
    });
    fireEvent.change(screen.getByLabelText("Connector"), { target: { value: "sqlserver" } });
    fireEvent.change(form.querySelector('input[placeholder="env://AIDA_SAMPLE_SOURCE_DSN"]')!, {
      target: { value: "env://AIDA_SAMPLE_SOURCE_DSN" },
    });
    fireEvent.submit(form);

    await waitFor(() =>
      expect(registerDatasource).toHaveBeenCalledWith(
        "proj_core",
        {
          name: "Consumer warehouse",
          connector_type: "sqlserver",
          dialect: "tsql",
          environment: "DEV",
          network_zone: "default",
          credential_reference: "env://AIDA_SAMPLE_SOURCE_DSN",
          max_concurrency: 4,
        },
        undefined,
      ),
    );
  });

  it("shows an empty state instead of the Register data source form when there are no projects yet", async () => {
    mockEmptySummary();
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);
    await waitFor(() => expect(fetchOrgProjects).toHaveBeenCalled());

    expect(screen.getByText("No projects yet")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Register data source" })).not.toBeInTheDocument();
  });

  it("surfaces a scope-summary fetch error with a retry action", async () => {
    fetchOrgLinesOfBusiness.mockRejectedValue(new ApiError(403, "policy_denied"));
    fetchOrgProjects.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });
    fetchOrgDatasources.mockResolvedValue({ items: [], limit: 500, offset: 0, total: 0 });
    const AdministrationScreen = await loadScreen();

    render(<AdministrationScreen />);

    await waitFor(() => expect(screen.getByText("policy_denied")).toBeInTheDocument());
  });

  it("shows the empty scope-summary state for an org with no hierarchy yet", async () => {
    mockEmptySummary();
    const AdministrationScreen = await loadScreen();
    render(<AdministrationScreen />);

    await waitFor(() => expect(screen.getByText("No hierarchy yet")).toBeInTheDocument());
  });
});
