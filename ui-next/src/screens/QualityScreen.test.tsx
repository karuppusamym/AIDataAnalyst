import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { DataQualityIncidentRead, DataQualitySummaryRead, DataSourceRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-15/UX-16: Quality against the real `quality_api.py` endpoints
   (quality-summary / quality-incidents / transition). Mocks the API boundary,
   matching `ReviewQueueScreen.test.tsx`/`MarketplaceScreen.test.tsx`'s
   established pattern.
--------------------------------------------------------------------------- */

const fetchOrgDatasources = vi.fn<
  (organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>
>();
const fetchQualitySummary = vi.fn<
  (datasourceId: string, signal?: AbortSignal) => Promise<DataQualitySummaryRead>
>();
const fetchQualityIncidents = vi.fn<
  (datasourceId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<DataQualityIncidentRead>>
>();
const transitionQualityIncident = vi.fn<
  (incidentId: string, body: unknown, signal?: AbortSignal) => Promise<DataQualityIncidentRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
    fetchQualitySummary: (datasourceId: string, signal?: AbortSignal) =>
      fetchQualitySummary(datasourceId, signal),
    fetchQualityIncidents: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchQualityIncidents(datasourceId, query, signal),
    transitionQualityIncident: (incidentId: string, body: unknown, signal?: AbortSignal) =>
      transitionQualityIncident(incidentId, body, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const SUMMARY: DataQualitySummaryRead = {
  datasource_id: "ds_1",
  table_count: 50,
  observed_table_count: 44,
  status_counts: { HEALTHY: 40, WARNING: 3, CRITICAL: 1 },
  open_incident_count: 2,
  critical_incident_count: 1,
  average_quality_score: 91.2,
  last_observed_at: "2026-09-02T04:00:00Z",
  metadata_scan_age_minutes: 12,
  metadata_scan_status: "CURRENT",
  source_freshness_status: "NOT_CONFIGURED",
};

const INCIDENT: DataQualityIncidentRead = {
  id: "inc_1", organization_id: "org1", datasource_id: "ds_1", table_id: "t_1", table_name: "raw_sales",
  policy_id: null, latest_observation_id: "obs_1", anomaly_type: "NULL_RATE_SHIFT", severity: "CRITICAL",
  status: "OPEN", source: "INTERNAL",
  summary: "Detected null rate shift outside the governed baseline threshold.",
  evidence: { column: "amount" }, occurrence_count: 3,
  first_observed_at: "2026-08-30T00:00:00Z", last_observed_at: "2026-09-02T00:00:00Z",
  acknowledged_by: null, acknowledged_at: null, resolved_by: null, resolved_at: null, resolution_reason: null,
  created_at: "2026-08-30T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
};

function incidentsPage(items: DataQualityIncidentRead[]): PageOf<DataQualityIncidentRead> {
  return { items, limit: 200, offset: 0, total: items.length };
}

async function loadScreen() {
  const { QualityScreen } = await import("./QualityScreen");
  return QualityScreen;
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  fetchQualitySummary.mockReset();
  fetchQualityIncidents.mockReset();
  transitionQualityIncident.mockReset();
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchQualitySummary.mockResolvedValue(SUMMARY);
  fetchQualityIncidents.mockResolvedValue(incidentsPage([INCIDENT]));
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("QualityScreen against the real quality_api.py endpoints", () => {
  it("picking a datasource loads both the summary tiles and the incidents list", async () => {
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });

    await waitFor(() =>
      expect(fetchQualitySummary).toHaveBeenCalledWith("ds_1", expect.anything()),
    );
    expect(fetchQualityIncidents).toHaveBeenCalledWith(
      "ds_1",
      { status: null, severity: null, limit: 200 },
      expect.anything(),
    );
    expect(new URLSearchParams(location.search).get("ds")).toBe("ds_1");

    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());
    const tiles = document.querySelector(".qual__tiles") as HTMLElement;
    const openTile = within(tiles).getByText("open incidents").closest(".tile");
    expect(within(openTile as HTMLElement).getByText("2")).toBeInTheDocument();
    const criticalTile = within(tiles).getByText("critical").closest(".tile");
    expect(within(criticalTile as HTMLElement).getByText("1")).toBeInTheDocument();
  });

  it("re-fetches with the right query params on a filter change, and a slow stale response doesn't clobber the newer one", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    let resolveFirst!: (v: PageOf<DataQualityIncidentRead>) => void;
    const firstResponse = new Promise<PageOf<DataQualityIncidentRead>>((res) => {
      resolveFirst = res;
    });
    let call = 0;
    fetchQualityIncidents.mockImplementation(async () => {
      call += 1;
      if (call === 1) return firstResponse;
      return incidentsPage([{ ...INCIDENT, id: "inc_2", table_name: "orders_raw", status: "ACKNOWLEDGED" }]);
    });

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    await waitFor(() => expect(fetchQualityIncidents).toHaveBeenCalledTimes(1));
    const firstSignal = fetchQualityIncidents.mock.calls[0]?.[2] as AbortSignal;
    expect(firstSignal.aborted).toBe(false);

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "ACKNOWLEDGED" } });

    await waitFor(() => expect(fetchQualityIncidents).toHaveBeenCalledTimes(2));
    expect(fetchQualityIncidents).toHaveBeenLastCalledWith(
      "ds_1",
      { status: "ACKNOWLEDGED", severity: null, limit: 200 },
      expect.anything(),
    );
    // The first request's controller was aborted the moment the filter change
    // fired a second request -- the same guard `CatalogScreen.loadFirstPage`
    // relies on.
    expect(firstSignal.aborted).toBe(true);
    expect(new URLSearchParams(location.search).get("status")).toBe("ACKNOWLEDGED");

    await waitFor(() => expect(screen.getByText("orders_raw")).toBeInTheDocument());

    // The slow first response finally resolves with stale data. It must not
    // clobber what the second, newer request already rendered.
    resolveFirst(incidentsPage([INCIDENT]));
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText("raw_sales")).not.toBeInTheDocument();
    expect(screen.getByText("orders_raw")).toBeInTheDocument();
  });

  it("transitioning an incident calls the transition endpoint with the right id and action, then refetches", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    transitionQualityIncident.mockResolvedValue({
      ...INCIDENT,
      status: "ACKNOWLEDGED",
      acknowledged_by: "me@tenant.example",
      acknowledged_at: "2026-09-02T05:00:00Z",
    });
    vi.spyOn(window, "prompt").mockReturnValue("Investigating with the data owner");

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));

    await waitFor(() =>
      expect(transitionQualityIncident).toHaveBeenCalledWith(
        "inc_1",
        { status: "ACKNOWLEDGED", reason: "Investigating with the data owner" },
        undefined,
      ),
    );
    await waitFor(() => expect(fetchQualityIncidents).toHaveBeenCalledTimes(2));
  });

  it("requires a reason before calling the transition endpoint, and skips the call if none is given", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    vi.spyOn(window, "prompt").mockReturnValue(null);

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));

    expect(window.prompt).toHaveBeenCalled();
    expect(transitionQualityIncident).not.toHaveBeenCalled();
  });

  it("opens a permalinkable detail panel for a selected incident", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "raw_sales" }));

    const panel = await screen.findByLabelText("Incident detail for raw_sales");
    expect(within(panel).getByText(/Detected null rate shift/)).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("incident")).toBe("inc_1");

    fireEvent.click(within(panel).getByRole("button", { name: "Close incident detail" }));
    expect(new URLSearchParams(location.search).get("incident")).toBeNull();
  });
});
