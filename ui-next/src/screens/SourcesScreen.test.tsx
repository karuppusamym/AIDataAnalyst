import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  AnalysisRunRead,
  ConnectorHealthScoreRead,
  DataSourceCreate,
  DataSourceRead,
  MeRead,
  ProjectRead,
  ScanPolicyRead,
} from "../lib/types";
import type { Session } from "../lib/session";
import type { PageOf } from "../lib/ui-types";
import { ApiError, type DatasourceContextSnapshot, type ProjectContextSnapshot } from "../lib/api";
import type { ScopeSelection } from "../lib/scope";

/* ---------------------------------------------------------------------------
   Sources — nav id `sources`, against the real
   `GET /v1/organizations/{org}/datasources` (reused from `listOrgDatasources`,
   already exercised by `NarratedLineageScreen`) and the new
   `GET /v1/datasources/{id}/health` (`operational_api.py::get_datasource_health`).
   API boundary mocked, matching `EvidencePane.test.tsx`/`MarketplaceScreen.test.tsx`'s
   established pattern -- real payload shapes, asserting the exact endpoint
   args, not superficial snapshots.
--------------------------------------------------------------------------- */

const listOrgDatasources = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>
>();
const fetchDatasourceHealth = vi.fn<
  (datasourceId: string, signal?: AbortSignal) => Promise<ConnectorHealthScoreRead>
>();
const downloadDatasourceContextSnapshot = vi.fn<
  (datasource: DataSourceRead, format: "markdown" | "json") => Promise<DatasourceContextSnapshot>
>();
const downloadProjectContextSnapshot = vi.fn<
  (project: ProjectRead, datasources: DataSourceRead[], format: "markdown" | "json") => Promise<ProjectContextSnapshot>
>();
/* The detail pane now also mounts `SourceAdministration` (R11-B7), which reads
   the scan policy and the run history for whichever source is selected. Stubbed
   here so these tests stay about the fleet/health/snapshot behaviour they were
   written for; the administration panel has its own file. */
const fetchScanPolicy = vi.fn<(id: string, signal?: AbortSignal) => Promise<ScanPolicyRead>>();
/* R11-S13 (M5): the registration form Administration already owned is mounted
   here too, so this file now reaches `POST /v1/projects/{id}/datasources`. */
const registerDatasource =
  vi.fn<(projectId: string, body: DataSourceCreate) => Promise<DataSourceRead>>();
const fetchDatasourceAnalysisRuns =
  vi.fn<
    (id: string, query: { limit?: number }, signal?: AbortSignal) => Promise<PageOf<AnalysisRunRead>>
  >();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      listOrgDatasources(organizationId, signal),
    fetchDatasourceHealth: (datasourceId: string, signal?: AbortSignal) =>
      fetchDatasourceHealth(datasourceId, signal),
    downloadDatasourceContextSnapshot: (datasource: DataSourceRead, format: "markdown" | "json") =>
      downloadDatasourceContextSnapshot(datasource, format),
    downloadProjectContextSnapshot: (
      project: ProjectRead,
      datasources: DataSourceRead[],
      format: "markdown" | "json",
    ) => downloadProjectContextSnapshot(project, datasources, format),
    fetchScanPolicy: (id: string, signal?: AbortSignal) => fetchScanPolicy(id, signal),
    fetchDatasourceAnalysisRuns: (id: string, query: { limit?: number }, signal?: AbortSignal) =>
      fetchDatasourceAnalysisRuns(id, query, signal),
    registerDatasource: (projectId: string, body: DataSourceCreate) =>
      registerDatasource(projectId, body),
  };
});

/* R11-S13 (M5): the registration form is role-gated, so these tests need to be
   able to say who is asking. `null` -- the default every case below runs with
   -- is "the session has not answered", which deliberately fails OPEN. */
let sessionMe: MeRead | null = null;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "demo",
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

let scopeSelection: ScopeSelection | null = null;
vi.mock("../lib/scope", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/scope")>();
  return {
    ...actual,
    useScopeSelection: () => scopeSelection,
  };
});

const PROJECT_ONE: ProjectRead = {
  id: "proj1", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", name: "Core Finance", slug: "core-finance",
  status: "ACTIVE", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

function scopeWithProject(projectId: string): ScopeSelection {
  return {
    workspaceId: "ws1", projectId, datasourceId: "",
    workspaces: [], projects: [PROJECT_ONE], datasources: [], bindings: [],
    visibleProjects: [PROJECT_ONE], visibleDatasources: [],
    setWorkspaceId: () => {}, setProjectId: () => {}, setDatasourceId: () => {},
    refresh: () => {}, loading: false, error: null,
  };
}

const SNOWFLAKE: DataSourceRead = {
  id: "ds_snowflake_prod", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", project_id: "proj1", name: "snowflake_prod",
  connector_type: "SNOWFLAKE", dialect: "snowflake", environment: "PRODUCTION",
  network_zone: "default", credential_reference: "vault://x", max_concurrency: 8,
  status: "ACTIVE", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
};

const ORACLE: DataSourceRead = {
  id: "ds_oracle_core", organization_id: "org1", line_of_business_id: "lob1",
  data_domain_id: "dom1", project_id: "proj1", name: "oracle_core",
  connector_type: "ORACLE", dialect: "oracle", environment: "PRODUCTION",
  network_zone: "restricted", credential_reference: "vault://y", max_concurrency: 4,
  status: "DISABLED", capabilities: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-07-15T00:00:00Z",
};

const HEALTH: ConnectorHealthScoreRead = {
  datasource_id: "ds_snowflake_prod",
  score: 91,
  status: "HEALTHY",
  factors: [
    { name: "RUN_SUCCESS_RATE", score: 35, maximum: 35, reason: "100% of recent runs succeeded.", evidence: { successful_runs: 20, terminal_runs: 20 } },
    { name: "STALENESS", score: 25, maximum: 25, reason: "Most recent run is within the scan interval.", evidence: { minutes_since_last_run: 12 } },
    { name: "FAILURE_STREAK", score: 20, maximum: 20, reason: "The most recent run succeeded.", evidence: { current_failure_streak: 0 } },
    { name: "PROFILING_COVERAGE", score: 8, maximum: 10, reason: "80% of discovered tables have a recent profile.", evidence: { profiled_ratio: 0.8 } },
    { name: "DATASOURCE_ENABLEMENT", score: 3, maximum: 10, reason: "The datasource status is ACTIVE.", evidence: { datasource_status: "ACTIVE" } },
  ],
  blockers: [],
  computed_at: "2026-09-02T00:00:00Z",
};

async function loadScreen() {
  const { SourcesScreen } = await import("./SourcesScreen");
  return SourcesScreen;
}

beforeEach(() => {
  listOrgDatasources.mockReset();
  fetchDatasourceHealth.mockReset();
  downloadDatasourceContextSnapshot.mockReset();
  downloadProjectContextSnapshot.mockReset();
  fetchScanPolicy.mockReset();
  fetchDatasourceAnalysisRuns.mockReset();
  registerDatasource.mockReset();
  sessionMe = null;
  // A source with no schedule and no scan history: the quietest honest answer
  // for tests that are not about the administration panel.
  fetchScanPolicy.mockRejectedValue(new ApiError(404, "scan policy not found"));
  fetchDatasourceAnalysisRuns.mockResolvedValue({ items: [], limit: 5, offset: 0, total: 0 });
  scopeSelection = null;
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourcesScreen against the real datasource fleet + health endpoints", () => {
  it("loads and renders the org's datasources via listOrgDatasources", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    const SourcesScreen = await loadScreen();

    render(<SourcesScreen />);

    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    expect(screen.getByText("oracle_core")).toBeInTheDocument();
    expect(listOrgDatasources).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      expect.anything(),
    );
    expect(screen.getByText("2")).toBeInTheDocument(); // total sources stat
    expect(fetchDatasourceHealth).not.toHaveBeenCalled(); // health is not fanned out eagerly
  });

  it("selecting a datasource fetches and renders its health/factor breakdown", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    fetchDatasourceHealth.mockResolvedValue(HEALTH);
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /snowflake_prod/ }));
    expect(new URLSearchParams(location.search).get("source")).toBe("ds_snowflake_prod");

    await waitFor(() =>
      expect(fetchDatasourceHealth).toHaveBeenCalledWith("ds_snowflake_prod", expect.anything()),
    );

    const panel = await screen.findByLabelText("Source details for snowflake_prod");
    expect(panel).toHaveTextContent("91");
    expect(panel).toHaveTextContent("healthy");
    expect(panel).toHaveTextContent("RUN SUCCESS RATE");
    expect(panel).toHaveTextContent("100% of recent runs succeeded.");
    expect(panel).toHaveTextContent("DATASOURCE ENABLEMENT");
    expect(panel).toHaveTextContent("Tables, columns and data types are populated by discovery scans.");
    expect(panel).toHaveTextContent("saving in Excel does not upload automatically");
    expect(screen.getByRole("button", { name: /Tables & columns/ })).toBeInTheDocument();
  });

  it("mounts source administration for the selected source only, and links its setup checklist", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    fetchDatasourceHealth.mockResolvedValue(HEALTH);
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    // Administration is per-selection, exactly like health: nothing is read for
    // a fleet nobody has drilled into.
    expect(fetchScanPolicy).not.toHaveBeenCalled();
    expect(fetchDatasourceAnalysisRuns).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /snowflake_prod/ }));
    await screen.findByLabelText("Source details for snowflake_prod");

    await waitFor(() =>
      expect(fetchScanPolicy).toHaveBeenCalledWith("ds_snowflake_prod", expect.anything()),
    );
    expect(fetchDatasourceAnalysisRuns).toHaveBeenCalledWith(
      "ds_snowflake_prod",
      { limit: 5 },
      expect.anything(),
    );
    expect(await screen.findByLabelText("Source administration")).toBeInTheDocument();
    // T15's resumable setup, reachable for THIS source (folded into R11-B7).
    expect(screen.getByRole("button", { name: /Setup checklist/ })).toBeInTheDocument();
  });

  it("shows a blocker pill when the health response reports one", async () => {
    listOrgDatasources.mockResolvedValue({ items: [ORACLE], limit: 500, offset: 0, total: 1 });
    fetchDatasourceHealth.mockResolvedValue({
      ...HEALTH,
      datasource_id: "ds_oracle_core",
      score: 40,
      status: "CRITICAL",
      blockers: ["DATASOURCE_DISABLED"],
    });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("oracle_core")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /oracle_core/ }));
    const panel = await screen.findByLabelText("Source details for oracle_core");
    await waitFor(() => expect(panel).toHaveTextContent("Administratively disabled"));
  });

  it("shows the loading state before data arrives, then the empty state for a fleet with no sources", async () => {
    let resolve!: (v: PageOf<DataSourceRead>) => void;
    listOrgDatasources.mockReturnValue(new Promise((r) => { resolve = r; }));
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);

    expect(screen.getByText("Loading sources…")).toBeInTheDocument();

    resolve({ items: [], limit: 500, offset: 0, total: 0 });
    await waitFor(() => expect(screen.getByText("No datasources registered")).toBeInTheDocument());
    expect(screen.getByText("Select a source")).toBeInTheDocument();
  });

  it("surfaces a fetch error with a retry action", async () => {
    listOrgDatasources.mockRejectedValue(new ApiError(403, "policy_denied"));
    const SourcesScreen = await loadScreen();

    render(<SourcesScreen />);

    await waitFor(() => expect(screen.getByText("policy_denied")).toBeInTheDocument());
  });

  it("filters the fleet by status, client-side, without a second fetch", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("oracle_core")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "DISABLED" } });

    await waitFor(() => expect(screen.queryByText("snowflake_prod")).not.toBeInTheDocument());
    expect(screen.getByText("oracle_core")).toBeInTheDocument();
    expect(listOrgDatasources).toHaveBeenCalledTimes(1); // filtering never re-fetches the fleet
  });

  it("generates and downloads a Markdown context snapshot for the selected source", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    fetchDatasourceHealth.mockResolvedValue(HEALTH);
    downloadDatasourceContextSnapshot.mockResolvedValue({
      generated_at: "2026-09-05T00:00:00Z",
      datasource: {
        id: "ds_snowflake_prod", name: "snowflake_prod", connector_type: "SNOWFLAKE",
        dialect: "snowflake", environment: "PRODUCTION", network_zone: "default",
        status: "ACTIVE", project_id: "proj1", organization_id: "org1",
      },
      health: { score: 91, status: "HEALTHY", computed_at: "2026-09-02T00:00:00Z" },
      quality: null,
      open_incidents: [],
      documented_tables: [],
      undocumented_tables: [],
      documented_count: 4,
      undocumented_count: 2,
      truncated: false,
      warnings: [],
    });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /snowflake_prod/ }));
    await screen.findByLabelText("Source details for snowflake_prod");

    fireEvent.click(screen.getByRole("button", { name: "Generate context (.md)" }));

    await waitFor(() =>
      expect(downloadDatasourceContextSnapshot).toHaveBeenCalledWith(SNOWFLAKE, "markdown"),
    );
    await waitFor(() =>
      expect(screen.getByText(/4 documented \/ 2 undocumented tables/)).toBeInTheDocument(),
    );
  });

  it("surfaces a warning count instead of hiding a partially-failed snapshot", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    fetchDatasourceHealth.mockResolvedValue(HEALTH);
    downloadDatasourceContextSnapshot.mockResolvedValue({
      generated_at: "2026-09-05T00:00:00Z",
      datasource: {
        id: "ds_snowflake_prod", name: "snowflake_prod", connector_type: "SNOWFLAKE",
        dialect: "snowflake", environment: "PRODUCTION", network_zone: "default",
        status: "ACTIVE", project_id: "proj1", organization_id: "org1",
      },
      health: { score: 91, status: "HEALTHY", computed_at: "2026-09-02T00:00:00Z" },
      quality: null,
      open_incidents: [],
      documented_tables: [],
      undocumented_tables: [],
      documented_count: 0,
      undocumented_count: 0,
      truncated: false,
      warnings: ["Quality summary could not be loaded: 403 policy_denied"],
    });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /snowflake_prod/ }));
    await screen.findByLabelText("Source details for snowflake_prod");

    fireEvent.click(screen.getByRole("button", { name: "Generate context (.json)" }));

    await waitFor(() =>
      expect(downloadDatasourceContextSnapshot).toHaveBeenCalledWith(SNOWFLAKE, "json"),
    );
    await waitFor(() =>
      expect(screen.getByText(/1 section\(s\) unavailable/)).toBeInTheDocument(),
    );
  });

  it("offers no project rollup when the scope has no project selected", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    expect(screen.queryByRole("group", { name: "Project context" })).not.toBeInTheDocument();
  });

  it("rolls up every datasource in the scoped project into one download", async () => {
    scopeSelection = scopeWithProject("proj1");
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    downloadProjectContextSnapshot.mockResolvedValue({
      generated_at: "2026-09-05T00:00:00Z",
      project: { id: "proj1", name: "Core Finance", slug: "core-finance" },
      datasource_count: 2,
      documented_count: 9,
      undocumented_count: 3,
      open_incident_count: 1,
      datasources: [],
      warnings: [],
    });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    const group = screen.getByRole("group", { name: "Project context" });
    expect(group).toHaveTextContent("Core Finance");
    expect(group).toHaveTextContent("2 datasource(s) in scope");

    fireEvent.click(screen.getByRole("button", { name: "Generate project context (.md)" }));

    await waitFor(() =>
      expect(downloadProjectContextSnapshot).toHaveBeenCalledWith(
        PROJECT_ONE,
        [SNOWFLAKE, ORACLE],
        "markdown",
      ),
    );
    await waitFor(() =>
      expect(screen.getByText(/2 datasource\(s\), 9 documented \/ 3 undocumented/)).toBeInTheDocument(),
    );
  });

  it("shows zero in scope, with the generate buttons disabled, for a project with no matching datasources", async () => {
    const emptyProject: ProjectRead = { ...PROJECT_ONE, id: "proj-empty", name: "Empty Project" };
    scopeSelection = { ...scopeWithProject("proj-empty"), projects: [emptyProject], visibleProjects: [emptyProject] };
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    const group = screen.getByRole("group", { name: "Project context" });
    expect(group).toHaveTextContent("0 datasource(s) in scope");
    expect(screen.getByRole("button", { name: "Generate project context (.md)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Generate project context (.json)" })).toBeDisabled();
  });
});

/* ---------------------------------------------------------------------------
   R11-S13 (M5) — registering a source and then testing it are one journey.

   The review's complaint was that they were two screens: an operator created a
   source in Administration, and the connection test, the scan policy and the
   run history that follow it live here. The fix is the SAME component mounted
   in both places, not a second form -- so the assertion that matters is that
   the request this screen sends is `registerDatasource`'s, unchanged, and that
   the created source is handed straight to the pane that owns the next step.
--------------------------------------------------------------------------- */

describe("registering a source is reachable from the fleet console", () => {
  const NEW_SOURCE: DataSourceRead = {
    ...SNOWFLAKE,
    id: "ds_new",
    name: "warehouse_dev",
    connector_type: "POSTGRES",
    dialect: "postgres",
    environment: "DEV",
    status: "REGISTERED",
  };

  it("posts through the shared RegisterDatasourceForm and selects the new source", async () => {
    scopeSelection = scopeWithProject("proj1");
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    registerDatasource.mockResolvedValue(NEW_SOURCE);
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    // Collapsed by default: the form is not in the tab order until asked for.
    const toggle = screen.getByRole("button", { name: /Register a data source/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    const form = screen.getByRole("form", { name: "Register data source" });
    // The project list is the shell's shared scope, not a second fetch.
    expect(form).toHaveTextContent("Core Finance");

    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "proj1" } });
    fireEvent.change(within(form).getByLabelText("Source name"), {
      target: { value: "warehouse_dev" },
    });
    fireEvent.change(within(form).getByLabelText("Credential reference"), {
      target: { value: "env://AIDA_SAMPLE_SOURCE_DSN" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Register source" }));

    // The one endpoint, with the dialect derived from the connector -- the
    // component's own contract, reused rather than re-implemented here.
    await waitFor(() =>
      expect(registerDatasource).toHaveBeenCalledWith("proj1", {
        name: "warehouse_dev",
        connector_type: "postgres",
        dialect: "postgres",
        environment: "DEV",
        network_zone: "default",
        credential_reference: "env://AIDA_SAMPLE_SOURCE_DSN",
        max_concurrency: 4,
      }),
    );

    // …and the journey continues here: `?source=` opens the detail pane whose
    // administration panel owns `POST /v1/datasources/{id}/test`.
    await waitFor(() =>
      expect(new URLSearchParams(location.search).get("source")).toBe("ds_new"),
    );
    expect(screen.getByText(/Registering does not test the connection/)).toBeInTheDocument();
  });

  it("keeps the fleet's own filters across a registration", async () => {
    scopeSelection = scopeWithProject("proj1");
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    registerDatasource.mockResolvedValue(NEW_SOURCE);
    history.replaceState(null, "", "/?q=snow&status=ACTIVE#/operator/sources");
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Register a data source/ }));
    const form = screen.getByRole("form", { name: "Register data source" });
    fireEvent.change(within(form).getByLabelText("Project"), { target: { value: "proj1" } });
    fireEvent.change(within(form).getByLabelText("Source name"), {
      target: { value: "warehouse_dev" },
    });
    fireEvent.change(within(form).getByLabelText("Credential reference"), {
      target: { value: "env://AIDA_SAMPLE_SOURCE_DSN" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Register source" }));

    await waitFor(() =>
      expect(new URLSearchParams(location.search).get("source")).toBe("ds_new"),
    );
    // Selecting the new source must not throw away the search the operator was
    // running: `setParams` merges, it does not replace.
    const params = new URLSearchParams(location.search);
    expect(params.get("q")).toBe("snow");
    expect(params.get("status")).toBe("ACTIVE");
  });

  it("hides the form from a session that may not register, without hiding the fleet", async () => {
    scopeSelection = scopeWithProject("proj1");
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    sessionMe = {
      principal_id: "p1", principal_type: "USER", organization_id: "org1",
      roles: ["Viewer"], persona: null, identity_provider: "development",
    };
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);

    // The read model is still everyone's; only the write surface is gated.
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Register a data source/ })).toBeNull();
  });
});

/* R11-OKF02: the Sources entry point onto a datasource's source bundle. The
   bundle is a full authorized read (and a rebuild if the source moved), so
   nothing is read until someone opens it; opening it mounts the knowledge view
   for the selected source, permalinked as `?knowledge=1`. */
const fetchSourceOkfBundle = vi.fn<(datasourceId: string, signal?: AbortSignal) => Promise<unknown>>();
const fetchSourceOkfPublications = vi.fn<(datasourceId: string, signal?: AbortSignal) => Promise<unknown>>();

vi.mock("../lib/api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/knowledge")>();
  return {
    ...actual,
    fetchSourceOkfBundle: (datasourceId: string, signal?: AbortSignal) => fetchSourceOkfBundle(datasourceId, signal),
    fetchSourceOkfPublications: (datasourceId: string, signal?: AbortSignal) =>
      fetchSourceOkfPublications(datasourceId, signal),
  };
});

describe("the source knowledge bundle opens from the source's details", () => {
  it("reads nothing until opened, then mounts the bundle for the selected source", async () => {
    listOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE], limit: 500, offset: 0, total: 1 });
    fetchDatasourceHealth.mockResolvedValue(HEALTH);
    fetchSourceOkfPublications.mockReset().mockResolvedValue({ datasource_id: SNOWFLAKE.id, items: [] });
    const SourcesScreen = await loadScreen();
    // A refusal keeps this test about the entry point, not the view's own rendering (the
    // refusal's wording is `KnowledgeView.test.tsx`'s to hold).
    fetchSourceOkfBundle.mockReset().mockRejectedValue(new Error("NO_BINDING_FOR_DATASOURCE"));
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /snowflake_prod/ }));
    const panel = await screen.findByLabelText("Source details for snowflake_prod");
    expect(fetchSourceOkfBundle).not.toHaveBeenCalled();

    fireEvent.click(within(panel).getByRole("button", { name: "Open knowledge bundle" }));
    expect(new URLSearchParams(location.search).get("knowledge")).toBe("1");
    const view = await screen.findByRole("article", { name: "Knowledge for snowflake_prod · source bundle" });
    await waitFor(() => expect(fetchSourceOkfBundle).toHaveBeenCalledWith("ds_snowflake_prod", expect.anything()));
    expect(await within(view).findByRole("alert")).toHaveTextContent("NO_BINDING_FOR_DATASOURCE");
    expect(within(view).queryByLabelText("Coverage")).toBeNull();

    fireEvent.click(within(panel).getByRole("button", { name: "Close knowledge bundle" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: /source bundle/ })).toBeNull());
    expect(new URLSearchParams(location.search).get("knowledge")).toBeNull();
  });
});
