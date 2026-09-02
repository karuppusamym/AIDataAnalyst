import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ConnectorHealthScoreRead, DataSourceRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Sources — nav id `sources`, against the real
   `GET /v1/organizations/{org}/datasources` (reused from `fetchOrgDatasources`,
   already exercised by `NarratedLineageScreen`) and the new
   `GET /v1/datasources/{id}/health` (`operational_api.py::get_datasource_health`).
   API boundary mocked, matching `EvidencePane.test.tsx`/`MarketplaceScreen.test.tsx`'s
   established pattern -- real payload shapes, asserting the exact endpoint
   args, not superficial snapshots.
--------------------------------------------------------------------------- */

const fetchOrgDatasources = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>
>();
const fetchDatasourceHealth = vi.fn<
  (datasourceId: string, signal?: AbortSignal) => Promise<ConnectorHealthScoreRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
    fetchDatasourceHealth: (datasourceId: string, signal?: AbortSignal) =>
      fetchDatasourceHealth(datasourceId, signal),
  };
});

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
  fetchOrgDatasources.mockReset();
  fetchDatasourceHealth.mockReset();
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SourcesScreen against the real datasource fleet + health endpoints", () => {
  it("loads and renders the org's datasources via fetchOrgDatasources", async () => {
    fetchOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    const SourcesScreen = await loadScreen();

    render(<SourcesScreen />);

    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    expect(screen.getByText("oracle_core")).toBeInTheDocument();
    expect(fetchOrgDatasources).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      expect.anything(),
    );
    expect(screen.getByText("2")).toBeInTheDocument(); // total sources stat
    expect(fetchDatasourceHealth).not.toHaveBeenCalled(); // health is not fanned out eagerly
  });

  it("selecting a datasource fetches and renders its health/factor breakdown", async () => {
    fetchOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    fetchDatasourceHealth.mockResolvedValue(HEALTH);
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /snowflake_prod/ }));
    expect(new URLSearchParams(location.search).get("source")).toBe("ds_snowflake_prod");

    await waitFor(() =>
      expect(fetchDatasourceHealth).toHaveBeenCalledWith("ds_snowflake_prod", expect.anything()),
    );

    const panel = await screen.findByLabelText("Health for snowflake_prod");
    expect(panel).toHaveTextContent("91");
    expect(panel).toHaveTextContent("healthy");
    expect(panel).toHaveTextContent("RUN SUCCESS RATE");
    expect(panel).toHaveTextContent("100% of recent runs succeeded.");
    expect(panel).toHaveTextContent("DATASOURCE ENABLEMENT");
  });

  it("shows a blocker pill when the health response reports one", async () => {
    fetchOrgDatasources.mockResolvedValue({ items: [ORACLE], limit: 500, offset: 0, total: 1 });
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
    const panel = await screen.findByLabelText("Health for oracle_core");
    await waitFor(() => expect(panel).toHaveTextContent("Administratively disabled"));
  });

  it("shows the loading state before data arrives, then the empty state for a fleet with no sources", async () => {
    let resolve!: (v: PageOf<DataSourceRead>) => void;
    fetchOrgDatasources.mockReturnValue(new Promise((r) => { resolve = r; }));
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);

    expect(screen.getByText("Loading sources…")).toBeInTheDocument();

    resolve({ items: [], limit: 500, offset: 0, total: 0 });
    await waitFor(() => expect(screen.getByText("No datasources registered")).toBeInTheDocument());
    expect(screen.getByText("Select a source")).toBeInTheDocument();
  });

  it("surfaces a fetch error with a retry action", async () => {
    fetchOrgDatasources.mockRejectedValue(new ApiError(403, "policy_denied"));
    const SourcesScreen = await loadScreen();

    render(<SourcesScreen />);

    await waitFor(() => expect(screen.getByText("policy_denied")).toBeInTheDocument());
  });

  it("filters the fleet by status, client-side, without a second fetch", async () => {
    fetchOrgDatasources.mockResolvedValue({ items: [SNOWFLAKE, ORACLE], limit: 500, offset: 0, total: 2 });
    const SourcesScreen = await loadScreen();
    render(<SourcesScreen />);
    await waitFor(() => expect(screen.getByText("oracle_core")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "DISABLED" } });

    await waitFor(() => expect(screen.queryByText("snowflake_prod")).not.toBeInTheDocument());
    expect(screen.getByText("oracle_core")).toBeInTheDocument();
    expect(fetchOrgDatasources).toHaveBeenCalledTimes(1); // filtering never re-fetches the fleet
  });
});
