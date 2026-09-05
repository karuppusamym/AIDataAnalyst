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

const fetchDomainLineageGraph = vi.fn();
const fetchOrgDataDomains = vi.fn();

vi.mock("../lib/_cross_source_api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/_cross_source_api")>();
  return {
    ...actual,
    fetchDomainLineageGraph: (domainId: string, query: unknown, signal?: AbortSignal) =>
      fetchDomainLineageGraph(domainId, query, signal),
    fetchOrgDataDomains: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDataDomains(organizationId, signal),
    fetchCrossBoundaryGrants: async () => [],
  };
});

const DOMAIN = {
  id: "dom1", organization_id: "org1", line_of_business_id: "lob1", parent_domain_id: null,
  name: "Finance", code: "FIN", is_default: false, status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

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
  fetchDomainLineageGraph.mockReset();
  fetchOrgDataDomains.mockReset();
  fetchOrgDataDomains.mockResolvedValue([DOMAIN]);
  fetchDomainLineageGraph.mockResolvedValue({
    data_domain_id: "dom1",
    datasource_ids: ["ds_1", "ds_2"],
    // Prefixed per-datasource by the merge step, so two sources' same-named
    // synthetic nodes cannot false-merge.
    nodes: [
      { id: "ds_1:t_orders_raw", node_kind: "TABLE", label: "orders_raw", qualified_name: "analytics.core.orders_raw", resolved: true, inbound_edge_count: 0, outbound_edge_count: 1 },
      { id: "ds_2:t_party", node_kind: "TABLE", label: "party_master", qualified_name: "core.party_master", resolved: true, inbound_edge_count: 1, outbound_edge_count: 0 },
    ],
    edges: [
      { id: "ds_2:x1", edge_source: "SUGGESTED_RELATIONSHIP", source_node_id: "ds_2:t_party", target_node_id: "ds_1:t_orders_raw", source_label: "party_master", target_label: "orders_raw", status: "APPROVED", confidence: 0.9, source_columns: [], target_columns: [] },
    ],
    counts_by_source: { SUGGESTED_RELATIONSHIP: 1 },
    returned_node_count: 2, returned_edge_count: 1, node_limit: 300, edge_limit: 1500,
    truncated: false, truncation_reasons: [],
    withheld_cross_boundary_domain_ids: ["dom_retail"],
  });
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

  /* ---------------------------------------------------------------------
     Domain scope (2026-09-05). The one view in which a relationship spanning
     two data sources renders as an edge at all -- and the one that has to be
     honest about what it is not allowed to show.
  --------------------------------------------------------------------- */

  it("switching scope to Domain federates the graph across every source in the domain", async () => {
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByLabelText("Scope")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Scope"), { target: { value: "domain" } });
    await waitFor(() => expect(screen.getByLabelText("Data domain")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Data domain"), { target: { value: "dom1" } });

    await waitFor(() =>
      expect(fetchDomainLineageGraph).toHaveBeenCalledWith(
        "dom1",
        { nodeLimit: 300, edgeLimit: 1500, suggestionStatus: "APPROVED" },
        expect.anything(),
      ),
    );
    // The cross-source edge is the reason this scope exists.
    await waitFor(() => expect(screen.getByText("2 nodes")).toBeInTheDocument());
    expect(screen.getByText("1 edges")).toBeInTheDocument();
    expect(fetchUnifiedLineageGraph).not.toHaveBeenCalled();
  });

  it("renders the graph body in domain scope, not an empty 'pick a datasource' state", async () => {
    // Regression: the graph body was gated on `ds` alone, so domain scope --
    // which has no `ds` -- showed "Pick a datasource" underneath a header that
    // was already reporting the domain graph's node and edge counts. Caught in
    // the browser, not by the counts the other domain tests assert on, because
    // those render outside that guard.
    history.replaceState(null, "", "/?scope=domain&dom=dom1");
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByRole("tab", { name: "Nodes (2)" })).toBeInTheDocument());
    expect(screen.queryByText("Pick a datasource")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Edges (1)" })).toBeInTheDocument();
  });

  it("names the domains whose edges were withheld rather than showing a quietly short graph", async () => {
    history.replaceState(null, "", "/?scope=domain&dom=dom1");
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByText(/Some edges are withheld/)).toBeInTheDocument());
    // Names the withheld domain, so it is distinguishable from the grants
    // panel's own general "Request access" control.
    expect(screen.getByRole("button", { name: /Request access to dom_retail/ })).toBeInTheDocument();
  });

  it("resolves impact against the source that owns the node, not the domain", async () => {
    // The impact endpoint is datasource-scoped, so a domain-graph node id has
    // to be split back into its `{datasource_id}:{node_id}` parts.
    // Driven through the URL rather than by clicking the Nodes tab: the
    // behaviour under test is the id split, not the tab chrome, and selecting
    // via the permalink is itself a supported entry point.
    history.replaceState(null, "", "/?scope=domain&dom=dom1&node=ds_2%3At_party");
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() =>
      expect(fetchLineageImpact).toHaveBeenCalledWith(
        "ds_2",
        "t_party",
        { depth: 5, nodeLimit: 200 },
        expect.anything(),
      ),
    );
  });

  it("keeps single-source scope untouched", async () => {
    history.replaceState(null, "", "/?ds=ds_1");
    const UnifiedLineageScreen = await loadScreen();
    render(<UnifiedLineageScreen />);

    await waitFor(() => expect(screen.getByText("3 nodes")).toBeInTheDocument());
    expect(fetchDomainLineageGraph).not.toHaveBeenCalled();
    expect(screen.queryByText(/Some edges are withheld/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Data domain")).not.toBeInTheDocument();
  });
});
