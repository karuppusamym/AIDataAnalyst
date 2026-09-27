import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  DataQualityIncidentRead,
  DataQualityIncidentTriageRead,
  DataQualitySummaryRead,
  DataSourceRead,
  FreshnessConfigRead,
  FreshnessStatusRead,
} from "../lib/types";
import type { CursorPage, MetadataTableRead, PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-15/UX-16: Quality against the real `quality_api.py` endpoints
   (quality-summary / quality-incidents / transition). Mocks the API boundary,
   matching `ReviewQueueScreen.test.tsx`/`MarketplaceScreen.test.tsx`'s
   established pattern.
--------------------------------------------------------------------------- */

const listOrgDatasources = vi.fn<
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
const fetchQualityIncidentTriage = vi.fn<
  (incidentId: string, signal?: AbortSignal) => Promise<DataQualityIncidentTriageRead>
>();
const fetchFreshnessConfigs = vi.fn<
  (datasourceId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<FreshnessConfigRead>>
>();
const fetchFreshnessStatus = vi.fn<
  (datasourceId: string, tableId: string, signal?: AbortSignal) => Promise<FreshnessStatusRead>
>();
const upsertFreshnessConfig = vi.fn<
  (datasourceId: string, tableId: string, body: unknown, signal?: AbortSignal) => Promise<FreshnessConfigRead>
>();
const approveFreshnessConfig = vi.fn<
  (datasourceId: string, tableId: string, signal?: AbortSignal) => Promise<FreshnessConfigRead>
>();
const fetchTablesLegacy = vi.fn<
  (datasourceId: string, opts: unknown, signal?: AbortSignal) => Promise<CursorPage<MetadataTableRead>>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    listOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      listOrgDatasources(organizationId, signal),
    fetchQualitySummary: (datasourceId: string, signal?: AbortSignal) =>
      fetchQualitySummary(datasourceId, signal),
    fetchQualityIncidents: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchQualityIncidents(datasourceId, query, signal),
    transitionQualityIncident: (incidentId: string, body: unknown, signal?: AbortSignal) =>
      transitionQualityIncident(incidentId, body, signal),
    fetchQualityIncidentTriage: (incidentId: string, signal?: AbortSignal) =>
      fetchQualityIncidentTriage(incidentId, signal),
    fetchFreshnessConfigs: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchFreshnessConfigs(datasourceId, query, signal),
    fetchFreshnessStatus: (datasourceId: string, tableId: string, signal?: AbortSignal) =>
      fetchFreshnessStatus(datasourceId, tableId, signal),
    upsertFreshnessConfig: (datasourceId: string, tableId: string, body: unknown, signal?: AbortSignal) =>
      upsertFreshnessConfig(datasourceId, tableId, body, signal),
    approveFreshnessConfig: (datasourceId: string, tableId: string, signal?: AbortSignal) =>
      approveFreshnessConfig(datasourceId, tableId, signal),
    fetchTablesLegacy: (datasourceId: string, opts: unknown, signal?: AbortSignal) =>
      fetchTablesLegacy(datasourceId, opts, signal),
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

/* --- DQ-2 watermark contracts (R11-B8) ----------------------------------- */

const TABLES: MetadataTableRead[] = [
  { id: "t_1", datasource_id: "ds_1", schema_id: "s_1", name: "raw_sales", object_type: "BASE_TABLE", status: "ACTIVE", fingerprint: "fp1" },
  { id: "t_2", datasource_id: "ds_1", schema_id: "s_1", name: "orders_raw", object_type: "BASE_TABLE", status: "ACTIVE", fingerprint: "fp2" },
];

const PENDING_CONFIG: FreshnessConfigRead = {
  id: "fc_1", organization_id: "org1", datasource_id: "ds_1", table_id: "t_1",
  watermark_column: "updated_at", classification: "INTERNAL", threshold_minutes: 60,
  retention_days: 365, status: "PENDING_APPROVAL", approved_by: null, approved_at: null,
  created_by: "steward-maker", created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
};

const ACTIVE_CONFIG: FreshnessConfigRead = {
  ...PENDING_CONFIG, status: "ACTIVE", approved_by: "steward-checker",
  approved_at: "2026-09-02T00:00:00Z",
};

function configsPage(items: FreshnessConfigRead[]): PageOf<FreshnessConfigRead> {
  return { items, limit: 25, offset: 0, total: items.length };
}

function freshnessStatus(status: string, ageMinutes: number | null = null): FreshnessStatusRead {
  return {
    table_id: "t_1", status, last_watermark: null, age_minutes: ageMinutes,
    threshold_minutes: 60, evidence: {},
  };
}

async function loadScreen() {
  const { QualityScreen } = await import("./QualityScreen");
  return QualityScreen;
}

beforeEach(() => {
  listOrgDatasources.mockReset();
  fetchQualitySummary.mockReset();
  fetchQualityIncidents.mockReset();
  transitionQualityIncident.mockReset();
  fetchQualityIncidentTriage.mockReset();
  fetchFreshnessConfigs.mockReset();
  fetchFreshnessStatus.mockReset();
  upsertFreshnessConfig.mockReset();
  approveFreshnessConfig.mockReset();
  fetchTablesLegacy.mockReset();
  listOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchQualitySummary.mockResolvedValue(SUMMARY);
  fetchQualityIncidents.mockResolvedValue(incidentsPage([INCIDENT]));
  fetchFreshnessConfigs.mockResolvedValue(configsPage([]));
  fetchFreshnessStatus.mockResolvedValue(freshnessStatus("NOT_CONFIGURED"));
  fetchTablesLegacy.mockResolvedValue({ items: TABLES, limit: 200, offset: 0, next_cursor: null });
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
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    const dialog = await screen.findByRole("dialog", { name: "Acknowledge this incident" });
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Investigating with the data owner" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Acknowledge" }));

    await waitFor(() =>
      expect(transitionQualityIncident).toHaveBeenCalledWith(
        "inc_1",
        { status: "ACKNOWLEDGED", reason: "Investigating with the data owner" },
        undefined,
      ),
    );
    await waitFor(() => expect(fetchQualityIncidents).toHaveBeenCalledTimes(2));
  });

  /* The endpoint refuses a transition without at least three characters of
     reason. That rule is now stated on a labelled field in a real dialog
     rather than assumed behind an unlabelled `window.prompt` (F21). */
  it("will not submit a transition until a reason is typed into the dialog", async () => {
    history.replaceState(null, "", "/?ds=ds_1");

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));

    const dialog = await screen.findByRole("dialog", { name: "Resolve this incident" });
    expect(within(dialog).getByRole("button", { name: "Resolve incident" })).toBeDisabled();
    expect(transitionQualityIncident).not.toHaveBeenCalled();
  });

  it("shows the runtime-coupling note for an open CRITICAL incident, and omits it once resolved (DQ-3)", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "raw_sales" }));
    const panel = await screen.findByLabelText("Incident detail for raw_sales");
    expect(within(panel).getByText("Runtime coupling (DQ-3)")).toBeInTheDocument();
    expect(
      within(panel).getByText(/blocks governed tools that depend on this table/),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Close incident detail" }));

    fetchQualityIncidents.mockResolvedValue(
      incidentsPage([
        {
          ...INCIDENT,
          status: "RESOLVED",
          resolved_by: "me@tenant.example",
          resolved_at: "2026-09-02T06:00:00Z",
          resolution_reason: "False positive from a backfill.",
        },
      ]),
    );
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "RESOLVED" } });
    await waitFor(() => expect(fetchQualityIncidents).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "raw_sales" }));
    const resolvedPanel = await screen.findByLabelText("Incident detail for raw_sales");
    expect(within(resolvedPanel).queryByText("Runtime coupling (DQ-3)")).not.toBeInTheDocument();
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

  it("suggests a root cause on demand, and hides it again on a second click", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    fetchQualityIncidentTriage.mockResolvedValue({
      incident_id: "inc_1",
      anomaly_type: "NULL_RATE_SHIFT",
      likely_causes: ["At least one column's null rate shifted versus its baseline."],
      recommended_next_steps: ["Check whether the source added a new optional field."],
      basis: ["max_null_rate_change_percent"],
    });
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    expect(fetchQualityIncidentTriage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Suggest root cause" }));

    await waitFor(() => expect(fetchQualityIncidentTriage).toHaveBeenCalledWith("inc_1", expect.anything()));
    expect(
      await screen.findByText("At least one column's null rate shifted versus its baseline."),
    ).toBeInTheDocument();
    expect(screen.getByText(/max_null_rate_change_percent/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide suggested cause" }));
    expect(
      screen.queryByText("At least one column's null rate shifted versus its baseline."),
    ).not.toBeInTheDocument();
  });

  it("surfaces a triage fetch failure without breaking the row", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    fetchQualityIncidentTriage.mockRejectedValue(new Error("triage unavailable"));
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);
    await waitFor(() => expect(screen.getByText("raw_sales")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Suggest root cause" }));

    await waitFor(() => expect(screen.getByText(/triage unavailable/)).toBeInTheDocument());
  });
});

/* ---------------------------------------------------------------------------
   DQ-2 watermark contracts (R11-B8).

   `quality_api.py` has had the upsert/approve/list/status routes all along and
   no screen called any of them, so a contract could never be created and
   never leave PENDING_APPROVAL -- every table reported AWAITING_APPROVAL
   forever. These cover the two halves that were unreachable (configure,
   approve) and the refusal that proves maker-checker is real.
--------------------------------------------------------------------------- */

describe("QualityScreen freshness watermark contracts", () => {
  it("shows an approved contract's real freshness state instead of NOT_CONFIGURED", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    fetchFreshnessConfigs.mockResolvedValue(configsPage([ACTIVE_CONFIG]));
    fetchFreshnessStatus.mockResolvedValue(freshnessStatus("STALE", 145));

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    const panel = await screen.findByRole("region", { name: "Freshness watermarks" });
    await waitFor(() =>
      expect(fetchFreshnessStatus).toHaveBeenCalledWith("ds_1", "t_1", expect.anything()),
    );
    // The table is named, not shown as a bare id, and both states are
    // reported: the contract is ACTIVE and the data behind it is STALE.
    expect(within(panel).getByText("raw_sales")).toBeInTheDocument();
    expect(within(panel).getByText("active")).toBeInTheDocument();
    expect(within(panel).getByText("stale")).toBeInTheDocument();
    expect(within(panel).getByText(/watermark 145m old/)).toBeInTheDocument();
    expect(within(panel).getByText(/approved by steward-checker/)).toBeInTheDocument();
    // Nothing left to approve on an already-active contract.
    expect(within(panel).queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("configuring a watermark PUTs the contract and says it is not evaluated until approved", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    upsertFreshnessConfig.mockResolvedValue(PENDING_CONFIG);

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    const panel = await screen.findByRole("region", { name: "Freshness watermarks" });
    fireEvent.click(within(panel).getByRole("button", { name: "Configure a watermark" }));

    fireEvent.change(within(panel).getByLabelText("Table"), { target: { value: "t_1" } });
    fireEvent.change(within(panel).getByLabelText("Watermark column"), {
      target: { value: "ingested_at" },
    });
    fireEvent.change(within(panel).getByLabelText("Threshold (minutes)"), {
      target: { value: "30" },
    });
    fetchFreshnessConfigs.mockResolvedValue(configsPage([PENDING_CONFIG]));
    fireEvent.click(within(panel).getByRole("button", { name: "Save contract" }));

    await waitFor(() =>
      expect(upsertFreshnessConfig).toHaveBeenCalledWith(
        "ds_1",
        "t_1",
        { watermark_column: "ingested_at", threshold_minutes: 30 },
        undefined,
      ),
    );
    // The screen states the half of the contract a maker does not control.
    expect(
      await within(panel).findByText(/stays pending until a second principal approves it/),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(within(panel).getByRole("button", { name: "Approve" })).toBeInTheDocument(),
    );
  });

  it("refuses a threshold the server would reject, without calling the endpoint", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    const panel = await screen.findByRole("region", { name: "Freshness watermarks" });
    fireEvent.click(within(panel).getByRole("button", { name: "Configure a watermark" }));
    fireEvent.change(within(panel).getByLabelText("Table"), { target: { value: "t_1" } });
    fireEvent.change(within(panel).getByLabelText("Watermark column"), {
      target: { value: "updated_at" },
    });
    fireEvent.change(within(panel).getByLabelText("Threshold (minutes)"), {
      target: { value: "0" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: "Save contract" }));

    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "The threshold is a whole number of minutes, at least 1.",
    );
    expect(upsertFreshnessConfig).not.toHaveBeenCalled();
  });

  it("approving a pending contract calls the checker route and reloads its state", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    fetchFreshnessConfigs.mockResolvedValue(configsPage([PENDING_CONFIG]));
    fetchFreshnessStatus.mockResolvedValue(freshnessStatus("AWAITING_APPROVAL"));
    approveFreshnessConfig.mockResolvedValue(ACTIVE_CONFIG);

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    const panel = await screen.findByRole("region", { name: "Freshness watermarks" });
    await waitFor(() =>
      expect(within(panel).getByText("awaiting approval")).toBeInTheDocument(),
    );

    fetchFreshnessConfigs.mockResolvedValue(configsPage([ACTIVE_CONFIG]));
    fetchFreshnessStatus.mockResolvedValue(freshnessStatus("FRESH", 4));
    fireEvent.click(within(panel).getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(approveFreshnessConfig).toHaveBeenCalledWith("ds_1", "t_1", undefined),
    );
    // The point of the whole row: after approval the table reports a real
    // freshness state rather than a permanent AWAITING_APPROVAL.
    await waitFor(() => expect(within(panel).getByText("fresh")).toBeInTheDocument());
    expect(within(panel).queryByText("awaiting approval")).not.toBeInTheDocument();
  });

  it("surfaces the server's 403 when someone tries to approve their own contract", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    fetchFreshnessConfigs.mockResolvedValue(configsPage([PENDING_CONFIG]));
    fetchFreshnessStatus.mockResolvedValue(freshnessStatus("AWAITING_APPROVAL"));
    const { ApiError } = await import("../lib/api");
    approveFreshnessConfig.mockRejectedValue(
      new ApiError(403, "the configuration's own author cannot approve it"),
    );

    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    const panel = await screen.findByRole("region", { name: "Freshness watermarks" });
    await waitFor(() =>
      expect(within(panel).getByRole("button", { name: "Approve" })).toBeInTheDocument(),
    );
    fireEvent.click(within(panel).getByRole("button", { name: "Approve" }));

    // Shown, not hidden -- and in the server's own words. The action stays
    // available, because the person who may legitimately approve is a
    // different principal on the same screen.
    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "the configuration's own author cannot approve it",
    );
    expect(within(panel).getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(within(panel).getByText("awaiting approval")).toBeInTheDocument();
  });

  it("says so plainly when a datasource has no freshness contract at all", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const QualityScreen = await loadScreen();
    render(<QualityScreen />);

    const panel = await screen.findByRole("region", { name: "Freshness watermarks" });
    expect(
      await within(panel).findByText("No table here has a freshness contract"),
    ).toBeInTheDocument();
    expect(fetchFreshnessStatus).not.toHaveBeenCalled();
  });
});
