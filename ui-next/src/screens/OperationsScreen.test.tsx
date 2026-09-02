import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AnalysisRunRead, FleetSummaryRead, MetadataIngestionBatchRead, OutboxEventRead } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   UX-16: Operations against the real, already-merged `operational_api.py`
   (fleet-summary, analysis-runs, outbox-events + requeue) and
   `ingestion_api.py` (metadata-ingestion-batches) -- mocks the API boundary
   the same way every other UX-15/UX-16 screen test does.
   `fetchOrgDatasources` is left un-mocked (the datasource-name lookup and
   the drill-down picker's own fixture-mode datasource list), matching
   `NarratedLineageScreen`'s real datasource fixture (`ds_snowflake_prod`).
--------------------------------------------------------------------------- */

const fetchFleetSummary = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<FleetSummaryRead>>();
const fetchAnalysisRuns = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<{ items: AnalysisRunRead[]; limit: number; offset: number; total: number }>
>();
const fetchOutboxEvents = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<{ items: OutboxEventRead[]; limit: number; offset: number; total: number }>
>();
const requeueOutboxEvent = vi.fn<(eventId: string, signal?: AbortSignal) => Promise<OutboxEventRead>>();
const fetchIngestionBatches = vi.fn<
  (datasourceId: string, opts: unknown, signal?: AbortSignal) => Promise<{ items: MetadataIngestionBatchRead[]; limit: number; offset: number; total: number }>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchFleetSummary: (organizationId: string, signal?: AbortSignal) => fetchFleetSummary(organizationId, signal),
    fetchAnalysisRuns: (query: unknown, signal?: AbortSignal) => fetchAnalysisRuns(query, signal),
    fetchOutboxEvents: (query: unknown, signal?: AbortSignal) => fetchOutboxEvents(query, signal),
    requeueOutboxEvent: (eventId: string, signal?: AbortSignal) => requeueOutboxEvent(eventId, signal),
    fetchIngestionBatches: (datasourceId: string, opts: unknown, signal?: AbortSignal) =>
      fetchIngestionBatches(datasourceId, opts, signal),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

const SUMMARY: FleetSummaryRead = {
  organization_id: ORG,
  datasource_statuses: { ACTIVE: 1 },
  analysis_run_statuses: { RUNNING: 1, SUCCEEDED: 2 },
  scan_policies_enabled: 5,
  scan_policies_due: 1,
  pending_outbox_events: 2,
  dead_letter_outbox_events: 1,
  generated_at: "2026-09-02T09:00:00Z",
};

const RUN: AnalysisRunRead = {
  id: "run_1", organization_id: ORG, datasource_id: "ds_snowflake_prod", resumed_from_run_id: null,
  mode: "FULL", trigger_type: "SCHEDULED", priority: 5, status: "RUNNING",
  temporal_workflow_id: "wf-1", discovered_catalogs: 1, discovered_schemas: 3,
  discovered_tables: 120, discovered_columns: 900, discovered_constraints: 40,
  created_objects: 2, changed_objects: 5, deprecated_objects: 0,
  profiled_tables: 60, profiled_columns: 400, error_class: null, error_message: null,
  created_at: "2026-09-02T08:00:00Z", updated_at: "2026-09-02T08:41:00Z",
};

const DEAD_LETTER_EVENT: OutboxEventRead = {
  id: "obx_1", organization_id: ORG, aggregate_type: "AnalysisRun", aggregate_id: "run_0",
  event_type: "analysis_run.failed", status: "DEAD_LETTER", attempt_count: 5,
  next_attempt_at: "2026-09-02T07:00:00Z", last_error: "connection reset (5 attempts)",
  occurred_at: "2026-09-02T06:00:00Z", published_at: null,
};

async function loadScreen() {
  const { OperationsScreen } = await import("./OperationsScreen");
  return OperationsScreen;
}

beforeEach(() => {
  fetchFleetSummary.mockReset();
  fetchAnalysisRuns.mockReset();
  fetchOutboxEvents.mockReset();
  requeueOutboxEvent.mockReset();
  fetchIngestionBatches.mockReset();
  fetchFleetSummary.mockResolvedValue(SUMMARY);
  fetchAnalysisRuns.mockResolvedValue({ items: [RUN], limit: 200, offset: 0, total: 1 });
  fetchOutboxEvents.mockResolvedValue({ items: [DEAD_LETTER_EVENT], limit: 100, offset: 0, total: 1 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OperationsScreen against the real operational_api.py", () => {
  it("loads and renders fleet-summary tiles plus the analysis-runs list", async () => {
    const OperationsScreen = await loadScreen();

    render(<OperationsScreen />);

    await waitFor(() => expect(screen.getByText("analysis runs")).toBeInTheDocument());
    expect(fetchFleetSummary).toHaveBeenCalledWith(ORG, expect.anything());
    // fleet-summary tile: 3 analysis runs total (1 RUNNING + 2 SUCCEEDED)
    expect(screen.getAllByText("3").length).toBeGreaterThan(0);
    // scan-policy tile
    expect(screen.getByText("5")).toBeInTheDocument();

    await waitFor(() => expect(fetchAnalysisRuns).toHaveBeenCalledWith(
      { organizationId: ORG, runStatus: null, datasourceId: null, limit: 200 },
      expect.anything(),
    ));
    expect(await screen.findByText("120 tables discovered")).toBeInTheDocument();
  });

  it("changing the run_status filter re-fetches analysis-runs with the right query param", async () => {
    const OperationsScreen = await loadScreen();
    render(<OperationsScreen />);
    await waitFor(() => expect(fetchAnalysisRuns).toHaveBeenCalledTimes(1));

    const statusSelect = screen.getAllByLabelText("Status")[0]!;
    fireEvent.change(statusSelect, { target: { value: "FAILED" } });

    await waitFor(() =>
      expect(fetchAnalysisRuns).toHaveBeenLastCalledWith(
        { organizationId: ORG, runStatus: "FAILED", datasourceId: null, limit: 200 },
        expect.anything(),
      ),
    );
    expect(new URLSearchParams(location.search).get("run_status")).toBe("FAILED");
  });

  it("requeues a dead-letter outbox event through the real requeue endpoint", async () => {
    requeueOutboxEvent.mockResolvedValue({ ...DEAD_LETTER_EVENT, status: "PENDING", attempt_count: 0 });
    const OperationsScreen = await loadScreen();
    render(<OperationsScreen />);
    await waitFor(() => expect(screen.getByText("analysis_run.failed")).toBeInTheDocument());

    screen.getByRole("button", { name: "Requeue" }).click();

    await waitFor(() => expect(requeueOutboxEvent).toHaveBeenCalledWith("obx_1", undefined));
    await waitFor(() => expect(fetchOutboxEvents).toHaveBeenCalledTimes(2));
  });

  it("shows a real error as an explicit ErrorState rather than a generic message", async () => {
    fetchFleetSummary.mockReset();
    fetchFleetSummary.mockRejectedValue(new ApiError(403, "requires PlatformAdmin, OrganizationAdmin, Auditor or Operations"));
    const OperationsScreen = await loadScreen();

    render(<OperationsScreen />);

    await waitFor(() =>
      expect(screen.getByText("Fleet summary could not be loaded")).toBeInTheDocument(),
    );
    expect(screen.getByText(/requires PlatformAdmin/)).toBeInTheDocument();
  });
});
