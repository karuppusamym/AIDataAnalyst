import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  ConsumerFooterRead,
  ProjectRead,
  SemanticMetricVersionRead,
  SemanticModelVersionRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-15/UX-16: Semantics, against the real project-scoped
   `semantic_api.py` routes (see SemanticsScreen.tsx's own file-top comment
   for the honest project-picker-not-org-wide-browse gap this exercises).
--------------------------------------------------------------------------- */

const fetchOrgProjects = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
const fetchSemanticModelVersions =
  vi.fn<(projectId: string, opts: unknown, signal?: AbortSignal) => Promise<PageOf<SemanticModelVersionRead>>>();
const fetchSemanticMetricVersions =
  vi.fn<(modelId: string, opts: unknown, signal?: AbortSignal) => Promise<PageOf<SemanticMetricVersionRead>>>();
const fetchSemanticModelConsumers =
  vi.fn<(modelId: string, signal?: AbortSignal) => Promise<ConsumerFooterRead>>();
const fetchSemanticMetricConsumers =
  vi.fn<(metricId: string, signal?: AbortSignal) => Promise<ConsumerFooterRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgProjects: (organizationId: string, signal?: AbortSignal) => fetchOrgProjects(organizationId, signal),
    fetchSemanticModelVersions: (projectId: string, opts: unknown, signal?: AbortSignal) =>
      fetchSemanticModelVersions(projectId, opts, signal),
    fetchSemanticMetricVersions: (modelId: string, opts: unknown, signal?: AbortSignal) =>
      fetchSemanticMetricVersions(modelId, opts, signal),
    fetchSemanticModelConsumers: (modelId: string, signal?: AbortSignal) =>
      fetchSemanticModelConsumers(modelId, signal),
    fetchSemanticMetricConsumers: (metricId: string, signal?: AbortSignal) =>
      fetchSemanticMetricConsumers(metricId, signal),
  };
});

const PROJECT: ProjectRead = {
  id: "proj_core", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const MODEL: SemanticModelVersionRead = {
  id: "smv_1", organization_id: "org1", project_id: "proj_core", version: 3,
  name: "Core Finance Semantic Model", change_summary: "Add exposure-at-default dimension",
  status: "PUBLISHED", created_by: "priya@tenant.example", approved_by: "steward@tenant.example",
  approved_at: "2026-08-20T10:00:00Z", published_at: "2026-08-20T10:05:00Z",
  based_on_version_id: null, created_at: "2026-08-18T09:00:00Z", updated_at: "2026-08-20T10:05:00Z",
};

const METRIC: SemanticMetricVersionRead = {
  id: "smtv_1", semantic_model_version_id: "smv_1", metric_id: "sm_net_revenue",
  metric_slug: "net_revenue", metric_name: "Net Revenue", version: 1, status: "PUBLISHED",
  description: "Total revenue net of intercompany transfers.",
  aggregation: "SUM", grain: "daily", source_table_id: "t_ledger_entry",
  measure_column_id: "col_amount", default_time_column_id: "col_posted_at",
  allowed_dimension_column_ids: ["col_lob"],
  fingerprint: "fp1", created_by: "priya@tenant.example", created_at: "2026-08-18T09:10:00Z",
};

const MODEL_FOOTER: ConsumerFooterRead = {
  resource_type: "semantic_model_version", resource_id: "smv_1", version: 3,
  generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 12,
  consumers: [
    { consumer_id: "agent:revenue_analyst", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 12, last_consumed_at: "2026-09-01T00:00:00Z" },
  ],
  total_consumers: 1,
};

const METRIC_FOOTER: ConsumerFooterRead = {
  resource_type: "semantic_metric_version", resource_id: "smtv_1", version: 1,
  generated_at: "2026-09-01T00:00:00Z", total_consumption_events: 5,
  consumers: [
    { consumer_id: "agent:revenue_analyst", consumer_type: "AGENT", channel: "MCP_TOOL", consumption_count: 5, last_consumed_at: "2026-09-01T00:00:00Z" },
  ],
  total_consumers: 1,
};

async function loadScreen() {
  const { SemanticsScreen } = await import("./SemanticsScreen");
  return SemanticsScreen;
}

beforeEach(() => {
  fetchOrgProjects.mockReset();
  fetchSemanticModelVersions.mockReset();
  fetchSemanticMetricVersions.mockReset();
  fetchSemanticModelConsumers.mockReset();
  fetchSemanticMetricConsumers.mockReset();
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  fetchSemanticModelVersions.mockResolvedValue({ items: [MODEL], limit: 200, offset: 0, total: 1 });
  fetchSemanticMetricVersions.mockResolvedValue({ items: [METRIC], limit: 200, offset: 0, total: 1 });
  fetchSemanticModelConsumers.mockResolvedValue(MODEL_FOOTER);
  fetchSemanticMetricConsumers.mockResolvedValue(METRIC_FOOTER);
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SemanticsScreen against the real project-scoped semantic_api.py routes", () => {
  it("shows the empty-before-selection state without listing any semantic models", async () => {
    const SemanticsScreen = await loadScreen();
    render(<SemanticsScreen />);

    await waitFor(() => expect(screen.getByText("Pick a project to see its semantic models")).toBeInTheDocument());
    expect(fetchOrgProjects).toHaveBeenCalledWith("00000000-0000-0000-0000-000000000001", expect.anything());
    expect(fetchSemanticModelVersions).not.toHaveBeenCalled();
  });

  it("walks project pick -> model list -> expand -> metric select -> consumer footer", async () => {
    const SemanticsScreen = await loadScreen();
    render(<SemanticsScreen />);

    await waitFor(() => expect(screen.getByText("Core Finance")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "proj_core" } });

    await waitFor(() =>
      expect(fetchSemanticModelVersions).toHaveBeenCalledWith("proj_core", { limit: 200 }, expect.anything()),
    );
    await waitFor(() => expect(screen.getByText("Core Finance Semantic Model")).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("project")).toBe("proj_core");

    // Selecting the model itself opens the detail pane on the model.
    fireEvent.click(screen.getByText("Core Finance Semantic Model"));
    await waitFor(() =>
      expect(fetchSemanticModelConsumers).toHaveBeenCalledWith("smv_1", expect.anything()),
    );
    await waitFor(() => expect(screen.getByText("agent:revenue_analyst")).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("model")).toBe("smv_1");

    // Expanding the row lazily fetches its metrics.
    expect(fetchSemanticMetricVersions).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Expand Core Finance Semantic Model"));
    await waitFor(() =>
      expect(fetchSemanticMetricVersions).toHaveBeenCalledWith("smv_1", { limit: 200 }, expect.anything()),
    );
    await waitFor(() => expect(screen.getByText("Net Revenue")).toBeInTheDocument());

    // Selecting a metric switches the detail pane to the metric's own consumer footer.
    fireEvent.click(screen.getByText("Net Revenue"));
    await waitFor(() =>
      expect(fetchSemanticMetricConsumers).toHaveBeenCalledWith("smtv_1", expect.anything()),
    );
    await waitFor(() => {
      const detail = screen.getByLabelText("Detail for Net Revenue");
      expect(within(detail).getByText("Total revenue net of intercompany transfers.")).toBeInTheDocument();
    });
    expect(new URLSearchParams(location.search).get("metric")).toBe("smtv_1");

    // Closing the detail pane clears both model and metric from the URL.
    fireEvent.click(screen.getByLabelText("Close detail"));
    await waitFor(() => expect(screen.getByText("Select a model or metric")).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("model")).toBeNull();
    expect(new URLSearchParams(location.search).get("metric")).toBeNull();
  });

  it("resolves a metric permalink by loading its parent model's metrics on its own", async () => {
    history.replaceState(null, "", "/?project=proj_core&model=smv_1&metric=smtv_1");
    const SemanticsScreen = await loadScreen();
    render(<SemanticsScreen />);

    await waitFor(() =>
      expect(fetchSemanticMetricVersions).toHaveBeenCalledWith("smv_1", { limit: 200 }, expect.anything()),
    );
    await waitFor(() =>
      expect(fetchSemanticMetricConsumers).toHaveBeenCalledWith("smtv_1", expect.anything()),
    );
    await waitFor(() => expect(screen.getByLabelText("Detail for Net Revenue")).toBeInTheDocument());
  });
});
