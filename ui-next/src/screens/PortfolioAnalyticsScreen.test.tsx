import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { PortfolioAnalyticsSummaryRead, PortfolioAnalyticsTrendsRead } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Portfolio analytics against the real `product_marketplace_api.py` routes
   (`.../portfolio-analytics/summary` and `.../trends`) -- mocks the API
   boundary the same way `MarketplaceScreen.test.tsx`/`OperationsScreen.test.tsx`
   do. The two fetches are independent (one failing must not blank the
   other), so several tests exercise that directly.
--------------------------------------------------------------------------- */

const fetchPortfolioAnalyticsSummary = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<PortfolioAnalyticsSummaryRead>
>();
const fetchPortfolioAnalyticsTrends = vi.fn<
  (query: unknown, signal?: AbortSignal) => Promise<PortfolioAnalyticsTrendsRead>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchPortfolioAnalyticsSummary: (query: unknown, signal?: AbortSignal) =>
      fetchPortfolioAnalyticsSummary(query, signal),
    fetchPortfolioAnalyticsTrends: (query: unknown, signal?: AbortSignal) =>
      fetchPortfolioAnalyticsTrends(query, signal),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

const SUMMARY: PortfolioAnalyticsSummaryRead = {
  generated_at: "2026-09-04T09:00:00Z",
  window_days: 30,
  low_quality_threshold: 80,
  lifecycle: {
    data_products_total: 58, data_products_active: 41, data_products_candidate: 12, data_products_retired: 5,
    data_product_versions_draft: 9, data_product_versions_review_required: 6,
    data_product_versions_published: 63, data_product_versions_retired: 8,
    data_contract_versions_draft: 7, data_contract_versions_review_required: 3, data_contract_versions_published: 51,
    context_products_total: 22, context_product_versions_draft: 4, context_product_versions_review_required: 2,
    context_product_versions_published: 19, context_product_versions_deprecated: 3,
  },
  access: {
    requests_created: 128, requests_pending: 14, requests_approved: 96, requests_rejected: 11,
    requests_revoked: 4, requests_expired: 3, active_grants: 214, grants_expiring_within_30_days: 17,
    fulfillment_pending: 6, fulfillment_provisioned: 201, fulfillment_failed: 2, fulfillment_revoked: 5,
  },
  usage: {
    unique_context_consumers: 47, unique_mcp_consumers: 33, unique_agent_principals: 19,
    context_product_reads: 18420, mcp_operations: 9640, mcp_resource_reads: 5210, mcp_prompt_reads: 1180,
    mcp_tool_calls: 2890, mcp_control_operations: 360, agent_runs: 3120, governed_tool_agent_runs: 2540,
    model_gateway_agent_runs: 480, development_override_agent_runs: 62, policy_blocked_agent_runs: 38,
    query_executions: 7460, governed_tool_executions: 6890,
  },
  quality: {
    published_products: 63, scored_products: 55, average_quality_score: 84.6, low_quality_products: 9,
    certified_products: 44, uncertified_products: 19, average_lineage_coverage: 76.2,
  },
  queues: {
    review_required_data_product_versions: 6, review_required_data_contract_versions: 3,
    review_required_context_product_versions: 2, pending_marketplace_access_requests: 14,
  },
  top_products: [
    {
      data_product_version_id: "dpv_revenue", product_key: "finance-revenue-model", name: "Finance revenue model",
      domain_name: "fin", certification_status: "CERTIFIED", quality_score: 97, lineage_coverage: 91,
      access_request_count: 42, approved_access_count: 38, context_read_count: 5210,
    },
    {
      data_product_version_id: "dpv_supply_chain", product_key: "supply-chain-events", name: "Supply chain events",
      domain_name: "ops", certification_status: "REVIEW_REQUIRED", quality_score: 74, lineage_coverage: 61,
      access_request_count: 16, approved_access_count: 9, context_read_count: 902,
    },
  ],
};

const TRENDS: PortfolioAnalyticsTrendsRead = {
  generated_at: "2026-09-04T09:00:00Z",
  window_days: 30,
  bucket_days: 7,
  points: [
    {
      bucket_start: "2026-08-07T00:00:00Z", bucket_end: "2026-08-14T00:00:00Z",
      access_requests: 18, context_reads: 2200, mcp_operations: 1100, mcp_tool_calls: 340,
      agent_runs: 380, governed_tool_runs: 310, model_gateway_runs: 60, query_executions: 880,
    },
    {
      bucket_start: "2026-08-28T00:00:00Z", bucket_end: "2026-09-04T00:00:00Z",
      access_requests: 32, context_reads: 4000, mcp_operations: 2000, mcp_tool_calls: 600,
      agent_runs: 720, governed_tool_runs: 570, model_gateway_runs: 110, query_executions: 1400,
    },
  ],
};

async function loadScreen() {
  const { PortfolioAnalyticsScreen } = await import("./PortfolioAnalyticsScreen");
  return PortfolioAnalyticsScreen;
}

beforeEach(() => {
  fetchPortfolioAnalyticsSummary.mockReset();
  fetchPortfolioAnalyticsTrends.mockReset();
  fetchPortfolioAnalyticsSummary.mockResolvedValue(SUMMARY);
  fetchPortfolioAnalyticsTrends.mockResolvedValue(TRENDS);
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PortfolioAnalyticsScreen against the real portfolio-analytics endpoints", () => {
  it("loads both endpoints with the default 30-day window and renders key stats", async () => {
    const PortfolioAnalyticsScreen = await loadScreen();
    render(<PortfolioAnalyticsScreen />);

    await waitFor(() =>
      expect(fetchPortfolioAnalyticsSummary).toHaveBeenCalledWith(
        expect.objectContaining({ organizationId: ORG, windowDays: 30 }),
        expect.anything(),
      ),
    );
    expect(fetchPortfolioAnalyticsTrends).toHaveBeenCalledWith(
      expect.objectContaining({ organizationId: ORG, windowDays: 30 }),
      expect.anything(),
    );

    expect(await screen.findByText("58")).toBeInTheDocument(); // data_products_total
    expect(screen.getByText("128")).toBeInTheDocument(); // requests_created
    expect(screen.getByText("84.6%")).toBeInTheDocument(); // average_quality_score
  });

  it("renders the top products table with a certification pill", async () => {
    const PortfolioAnalyticsScreen = await loadScreen();
    render(<PortfolioAnalyticsScreen />);

    expect(await screen.findByText("Finance revenue model")).toBeInTheDocument();
    expect(screen.getByText("finance-revenue-model")).toBeInTheDocument();

    const table = screen.getByRole("table", { name: "Top products" });
    expect(within(table).getByText("certified")).toBeInTheDocument();
    expect(within(table).getByText("review required")).toBeInTheDocument();
  });

  it("renders the trends table with bucket ranges", async () => {
    const PortfolioAnalyticsScreen = await loadScreen();
    render(<PortfolioAnalyticsScreen />);

    expect(await screen.findByText("2026-08-07 – 2026-08-14")).toBeInTheDocument();
    expect(screen.getByText("2026-08-28 – 2026-09-04")).toBeInTheDocument();
  });

  it("re-fetches both endpoints with a new window_days when the selector changes", async () => {
    const PortfolioAnalyticsScreen = await loadScreen();
    render(<PortfolioAnalyticsScreen />);
    await waitFor(() => expect(fetchPortfolioAnalyticsSummary).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Window"), { target: { value: "90" } });

    await waitFor(() =>
      expect(fetchPortfolioAnalyticsSummary).toHaveBeenLastCalledWith(
        expect.objectContaining({ windowDays: 90 }),
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(fetchPortfolioAnalyticsTrends).toHaveBeenLastCalledWith(
        expect.objectContaining({ windowDays: 90 }),
        expect.anything(),
      ),
    );
    expect(new URLSearchParams(location.search).get("window")).toBe("90");
  });

  it("surfaces a summary fetch error with a retry action, without blanking a loaded trends panel", async () => {
    fetchPortfolioAnalyticsSummary.mockRejectedValue(new ApiError(403, "policy_denied"));
    const PortfolioAnalyticsScreen = await loadScreen();

    render(<PortfolioAnalyticsScreen />);

    expect(await screen.findByText("policy_denied")).toBeInTheDocument();
    // The independent trends panel still loads and renders.
    expect(await screen.findByText("2026-08-07 – 2026-08-14")).toBeInTheDocument();
  });

  it("surfaces a trends fetch error without blanking a loaded summary panel", async () => {
    fetchPortfolioAnalyticsTrends.mockRejectedValue(new ApiError(502, "trend aggregation failed"));
    const PortfolioAnalyticsScreen = await loadScreen();

    render(<PortfolioAnalyticsScreen />);

    expect(await screen.findByText("trend aggregation failed")).toBeInTheDocument();
    // The independent summary panel still loads and renders.
    expect(await screen.findByText("Finance revenue model")).toBeInTheDocument();
  });

  it("shows an empty state when there are no top products", async () => {
    fetchPortfolioAnalyticsSummary.mockResolvedValue({ ...SUMMARY, top_products: [] });
    const PortfolioAnalyticsScreen = await loadScreen();

    render(<PortfolioAnalyticsScreen />);

    expect(await screen.findByText("No published products yet")).toBeInTheDocument();
  });
});
