import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { DataSourceRead, UnifiedLineageGraphRead, UnifiedLineageImpactRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   Unified lineage against the real
   `GET /v1/datasources/{id}/unified-lineage/graph` and
   `GET /v1/datasources/{id}/unified-lineage/impact/{node}`
   (`unified_lineage_api.py::get_unified_lineage_graph`/`get_unified_lineage_impact`).
--------------------------------------------------------------------------- */

const fetchOrgDatasources = vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const fetchUnifiedLineageGraph = vi.fn<
  (datasourceId: string, query: unknown, signal?: AbortSignal) => Promise<UnifiedLineageGraphRead>
>();
const fetchLineageImpact = vi.fn<
  (datasourceId: string, nodeId: string, query: unknown, signal?: AbortSignal) => Promise<UnifiedLineageImpactRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) => fetchOrgDatasources(organizationId, signal),
    fetchUnifiedLineageGraph: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchUnifiedLineageGraph(datasourceId, query, signal),
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

const GRAPH: UnifiedLineageGraphRead = {
  datasource_id: "ds_1",
  nodes: [
    { id: "t_raw_sales", node_kind: "TABLE", label: "raw_sales", qualified_name: "analytics.raw.raw_sales", resolved: true, inbound_edge_count: 0, outbound_edge_count: 1 },
    { id: "t_orders_raw", node_kind: "TABLE", label: "orders_raw", qualified_name: "analytics.core.orders_raw", resolved: true, inbound_edge_count: 1, outbound_edge_count: 1 },
    { id: "t_revenue_agg", node_kind: "DBT_MODEL", label: "revenue_agg", qualified_name: "analytics.mart.revenue_agg", resolved: true, inbound_edge_count: 1, outbound_edge_count: 0 },
  ],
  edges: [
    { id: "fk_1", edge_source: "FOREIGN_KEY", source_node_id: "t_raw_sales", target_node_id: "t_orders_raw", source_label: "raw_sales", target_label: "orders_raw", status: "ACTIVE", confidence: 1, source_columns: [], target_columns: [] },
    { id: "dbt_1", edge_source: "DBT_DEPENDENCY", source_node_id: "t_orders_raw", target_node_id: "t_revenue_agg", source_label: "orders_raw", target_label: "revenue_agg", status: "ACTIVE", confidence: 1, source_columns: [], target_columns: [] },
  ],
  counts_by_source: { FOREIGN_KEY: 1, DBT_DEPENDENCY: 1 },
  returned_node_count: 3,
  returned_edge_count: 2,
  node_limit: 300,
  edge_limit: 1500,
  truncated: false,
  truncation_reasons: [],
};

const IMPACT: UnifiedLineageImpactRead = {
  datasource_id: "ds_1", focus_node_id: "t_orders_raw", focus_node_kind: "TABLE",
  focus_label: "analytics.core.orders_raw",
  upstream: [
    { node_id: "t_raw_sales", node_kind: "TABLE", label: "raw_sales", qualified_name: "analytics.raw.raw_sales", depth: 1, contributing_edge_sources: ["FOREIGN_KEY"], quality_state: "PASSING" },
  ],
  downstream: [
    { node_id: "t_revenue_agg", node_kind: "DBT_MODEL", label: "revenue_agg", qualified_name: "analytics.mart.revenue_agg", depth: 1, contributing_edge_sources: ["DBT_DEPENDENCY"], quality_state: "STALE" },
  ],
  requested_depth: 5, node_limit: 200, upstream_truncated: false, downstream_truncated: false,
};

async function loadScreen() {
  const { UnifiedLineageScreen } = await import("./UnifiedLineageScreen");
  return UnifiedLineageScreen;
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  fetchUnifiedLineageGraph.mockReset();
  fetchLineageImpact.mockReset();
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchUnifiedLineageGraph.mockResolvedValue(GRAPH);
  fetchLineageImpact.mockResolvedValue(IMPACT);
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UnifiedLineageScreen against the real unified-lineage graph/impact endpoints", () => {
  it("picking a datasource loads the bounded graph with the default node/edge limits and APPROVED suggestions", async () => {
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Data source"), { target: { value: "ds_1" } });

    await waitFor(() =>
      expect(fetchUnifiedLineageGraph).toHaveBeenCalledWith(
        "ds_1",
        { nodeLimit: 300, edgeLimit: 1500, suggestionStatus: "APPROVED" },
        expect.anything(),
      ),
    );
    await waitFor(() => expect(screen.getByText("3 nodes")).toBeInTheDocument());
    expect(screen.getByText("2 edges")).toBeInTheDocument();
    expect(screen.getByText("Complete result")).toBeInTheDocument();
  });

  it("selecting a node (from the Nodes tab) fetches its bounded impact and renders upstream/downstream rows", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByText("3 nodes")).toBeInTheDocument());
    expect(fetchLineageImpact).not.toHaveBeenCalled();
    expect(screen.getByText("Select a graph node")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Nodes (3)" }));
    const nodeRow = screen.getByText("orders_raw").closest("button")!;
    fireEvent.click(nodeRow);

    await waitFor(() =>
      expect(fetchLineageImpact).toHaveBeenCalledWith("ds_1", "t_orders_raw", { depth: 5, nodeLimit: 200 }, expect.anything()),
    );
    await waitFor(() => expect(screen.getByRole("heading", { name: "analytics.core.orders_raw" })).toBeInTheDocument());
    const table = screen.getByRole("table");
    expect(within(table).getByText("raw_sales")).toBeInTheDocument();
    expect(within(table).getByText("revenue_agg")).toBeInTheDocument();
    expect(within(table).getByText("Upstream")).toBeInTheDocument();
    expect(within(table).getByText("Downstream")).toBeInTheDocument();
  });

  it("turning off the dbt layer chip filters the Edges tab down to the remaining layers", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByRole("tab", { name: "Edges (2)" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "dbt" }));

    await waitFor(() => expect(screen.getByRole("tab", { name: "Edges (1)" })).toBeInTheDocument());
  });

  it("shows the pick-a-datasource state without calling the graph endpoint", async () => {
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByText("Pick a datasource")).toBeInTheDocument());
    expect(fetchUnifiedLineageGraph).not.toHaveBeenCalled();
  });
});
