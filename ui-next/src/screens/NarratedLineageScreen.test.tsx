import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { DataSourceRead, UnifiedLineageImpactRead } from "../lib/types";
import type { CatalogRowRead, CursorPage, PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-20: narrated lineage traversal against the real
   `GET /v1/datasources/{id}/unified-lineage/impact/{node}`
   (`unified_lineage_api.py::build_unified_lineage_impact_payload`).
--------------------------------------------------------------------------- */

const fetchOrgDatasources = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchCatalogRows = vi.fn<(query: unknown, signal?: AbortSignal) => Promise<CursorPage<CatalogRowRead>>>();
const fetchLineageImpact = vi.fn<
  (datasourceId: string, nodeId: string, query: unknown, signal?: AbortSignal) => Promise<UnifiedLineageImpactRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) => fetchOrgDatasources(organizationId, signal),
    fetchCatalogRows: (query: unknown, signal?: AbortSignal) => fetchCatalogRows(query, signal),
    fetchLineageImpact: (datasourceId: string, nodeId: string, query: unknown, signal?: AbortSignal) =>
      fetchLineageImpact(datasourceId, nodeId, query, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const CANDIDATE: CatalogRowRead = {
  id: "t_orders_raw", name: "orders_raw", schema_name: "core",
  datasource_id: "ds_snowflake_prod", datasource_name: "snowflake_prod",
  object_type: "TABLE", status: "ACTIVE", description: null, description_is_proposed: false,
  owner: null, certification: "NONE", certification_expires_at: null, quality: "INCIDENT_OPEN",
  certification_evidence_summary: null,
  glossary_terms: [], row_count_estimate: 1000, updated_at: "2026-09-01T00:00:00Z",
};

const IMPACT: UnifiedLineageImpactRead = {
  datasource_id: "ds_1", focus_node_id: "t_orders_raw", focus_node_kind: "TABLE",
  focus_label: "analytics.core.orders_raw",
  upstream: [
    { node_id: "t_raw_sales", node_kind: "TABLE", label: "raw_sales", qualified_name: "analytics.raw.raw_sales", depth: 1, contributing_edge_sources: ["FOREIGN_KEY"] },
  ],
  downstream: [
    { node_id: "t_revenue_agg", node_kind: "TABLE", label: "revenue_agg", qualified_name: "analytics.mart.revenue_agg", depth: 1, contributing_edge_sources: ["DBT_DEPENDENCY"] },
  ],
  requested_depth: 5, node_limit: 200, upstream_truncated: false, downstream_truncated: false,
};

async function loadScreen() {
  const { NarratedLineageScreen } = await import("./NarratedLineageScreen");
  return NarratedLineageScreen;
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  fetchCatalogRows.mockReset();
  fetchLineageImpact.mockReset();
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchCatalogRows.mockResolvedValue({ items: [CANDIDATE], limit: 25, offset: 0, total: 1, next_cursor: null });
  fetchLineageImpact.mockResolvedValue(IMPACT);
  vi.resetModules();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("NarratedLineageScreen against the real unified-lineage impact endpoint", () => {
  it("walks datasource pick -> asset search -> real impact fetch -> narration", async () => {
    const NarratedLineageScreen = await loadScreen();
    render(<NarratedLineageScreen />);

    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });

    fireEvent.change(screen.getByLabelText("Trace from asset"), { target: { value: "orders" } });
    await vi.advanceTimersByTimeAsync(300);
    await waitFor(() => expect(fetchCatalogRows).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("orders_raw")).toBeInTheDocument());

    fireEvent.click(screen.getByText("orders_raw"));

    await waitFor(() =>
      expect(fetchLineageImpact).toHaveBeenCalledWith(
        "ds_1",
        "t_orders_raw",
        { depth: 5, nodeLimit: 200 },
        expect.anything(),
      ),
    );
    // Streaming reveal: advance past both hops' reveal timers.
    await vi.advanceTimersByTimeAsync(500);
    await waitFor(() => expect(screen.getByText(/raw_sales/)).toBeInTheDocument());
    expect(screen.getByText(/revenue_agg/)).toBeInTheDocument();
    expect(screen.getByText(/a foreign key/)).toBeInTheDocument();
    expect(screen.getByText(/a dbt dependency/)).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("node")).toBe("t_orders_raw");
  });

  it("the graph tab is a supporting view, not the entry point -- narrated renders first", async () => {
    history.replaceState(null, "", "/?ds=ds_1&node=t_orders_raw");
    const NarratedLineageScreen = await loadScreen();
    render(<NarratedLineageScreen />);

    await vi.advanceTimersByTimeAsync(500);
    await waitFor(() => expect(screen.getByRole("tab", { name: "Narrated" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByRole("tab", { name: "Graph (supporting view)" })).toHaveAttribute("aria-selected", "false");

    fireEvent.click(screen.getByRole("tab", { name: "Graph (supporting view)" }));

    await waitFor(() => expect(screen.getByRole("img", { name: /grouped by hop distance/ })).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("view")).toBe("graph");
  });

  it("shows the empty-before-search state without calling the impact endpoint", async () => {
    const NarratedLineageScreen = await loadScreen();
    render(<NarratedLineageScreen />);

    await waitFor(() => expect(screen.getByText("Pick a datasource to begin")).toBeInTheDocument());
    expect(fetchLineageImpact).not.toHaveBeenCalled();
  });
});
