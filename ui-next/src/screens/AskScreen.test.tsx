import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type {
  AgentAnalysisResponse,
  AgentRunGroundingReceiptsRead,
  AgentRunRead,
  DataSourceRead,
} from "../lib/types";
import type { PageOf } from "../lib/ui-types";

/* ---------------------------------------------------------------------------
   UX-15/UX-16: Ask against the real, single-shot
   `POST /v1/datasources/{id}/agent-analyses` (`run_agent_analysis`,
   `api.py:2912`) and its history/evidence reads -- same pattern
   `NarratedLineageScreen.test.tsx`/`ReviewQueueScreen.test.tsx` establish:
   mock the API boundary (`../lib/api`) with real payload shapes, assert the
   exact endpoint/args called, not a superficial snapshot.
--------------------------------------------------------------------------- */

const fetchOrgDatasources =
  vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();
const runAgentAnalysis =
  vi.fn<(datasourceId: string, body: unknown, signal?: AbortSignal) => Promise<AgentAnalysisResponse>>();
const fetchAgentRuns =
  vi.fn<(datasourceId: string, query: unknown, signal?: AbortSignal) => Promise<PageOf<AgentRunRead>>>();
const fetchAgentRun = vi.fn<(agentRunId: string, signal?: AbortSignal) => Promise<AgentRunRead>>();
const fetchAgentRunGroundingReceipts =
  vi.fn<(agentRunId: string, signal?: AbortSignal) => Promise<AgentRunGroundingReceiptsRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
    runAgentAnalysis: (datasourceId: string, body: unknown, signal?: AbortSignal) =>
      runAgentAnalysis(datasourceId, body, signal),
    fetchAgentRuns: (datasourceId: string, query: unknown, signal?: AbortSignal) =>
      fetchAgentRuns(datasourceId, query, signal),
    fetchAgentRun: (agentRunId: string, signal?: AbortSignal) => fetchAgentRun(agentRunId, signal),
    fetchAgentRunGroundingReceipts: (agentRunId: string, signal?: AbortSignal) =>
      fetchAgentRunGroundingReceipts(agentRunId, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const ANALYSIS_RESPONSE: AgentAnalysisResponse = {
  agent_run_id: "run_fresh_1",
  status: "SUCCEEDED",
  generation_source: "FREEFORM_SQL",
  semantic_version: "sm_1",
  policy_version: "pol_1",
  step_trace: [{ stage: "EXECUTED" }],
  retrieval_evidence: [{ object_type: "TABLE", object_id: "t_orders_raw" }],
  plan_evidence: { strategy: "FREEFORM_SQL", confidence: 0.9 },
  execution: {
    execution_id: "qe_1",
    status: "SUCCEEDED",
    normalized_sql: "SELECT date_trunc('month', order_date), SUM(net_amount) FROM orders_raw GROUP BY 1",
    referenced_tables: ["analytics.core.orders_raw"],
    referenced_columns: ["order_date", "net_amount"],
    column_lineage: [],
    plan_cost: 1.2,
    warehouse_query_id: "wh_1",
    row_count: 3,
    elapsed_ms: 120,
    masked_columns: [],
    rows: [{ month: "2026-06-01", net_revenue: 100 }],
  },
  explanation:
    "Net revenue by month for the last quarter, computed from analytics.core.orders_raw using the governed Net Revenue definition.",
};

const AMBIGUITY_DETAIL =
  "the term 'revenue' resolves to 2 equally applicable governed definitions for this datasource's scope; specify which business area you mean:" +
  " [business_node=bn_finance_revenue] 'Net Revenue (Finance)' (owner: priya@tenant.example) -- Gross bookings less refunds and intercompany transfers." +
  " [business_node=bn_sales_revenue] 'Net Revenue (Sales)' (owner: sam@tenant.example) -- Recognized bookings net of discounts, excluding renewals.";

const PAST_RUN: AgentRunRead = {
  id: "run_past_1",
  organization_id: "org1",
  datasource_id: "ds_1",
  principal_id: "p1",
  status: "SUCCEEDED",
  generation_source: "FREEFORM_SQL",
  model_route: "default/sql-planner",
  semantic_version: "sm_1",
  policy_version: "pol_1",
  query_execution_id: "qe_past_1",
  step_trace: [{ stage: "EXECUTED", strategy: "FREEFORM_SQL" }],
  retrieval_evidence: [{ object_type: "TABLE", object_id: "t_orders_raw" }],
  grounding_fragment_digests: [],
  plan_evidence: { strategy: "FREEFORM_SQL" },
  recommended_tool_version_id: null,
  failure_reason: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:05Z",
};

const PAST_RUN_RECEIPTS: AgentRunGroundingReceiptsRead = {
  agent_run_id: "run_past_1",
  fragment_count: 1,
  fragments: [
    {
      object_type: "TABLE",
      object_id: "t_orders_raw",
      fragment_digest: "sha256:abc123",
      annotation_version_id: "av_orders_raw_1",
      annotation_version: 1,
      annotation_status: "APPROVED",
      business_name: "orders raw",
      business_description: "Raw order records, one row per order.",
      digest_verified: true,
    },
  ],
};

const EMPTY_RUNS: PageOf<AgentRunRead> = { items: [], limit: 50, offset: 0, total: 0 };

async function loadScreen() {
  const { AskScreen } = await import("./AskScreen");
  return AskScreen;
}

async function pickDatasource() {
  await waitFor(() => expect(screen.getByText("snowflake_prod")).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText("Datasource"), { target: { value: "ds_1" } });
}

beforeEach(() => {
  fetchOrgDatasources.mockReset();
  runAgentAnalysis.mockReset();
  fetchAgentRuns.mockReset();
  fetchAgentRun.mockReset();
  fetchAgentRunGroundingReceipts.mockReset();
  fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
  fetchAgentRuns.mockResolvedValue(EMPTY_RUNS);
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AskScreen against the real agent-analyses endpoint", () => {
  it("picking a datasource then asking a question calls POST .../agent-analyses with the right body and renders the real explanation", async () => {
    runAgentAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    fetchAgentRunGroundingReceipts.mockResolvedValue({
      agent_run_id: ANALYSIS_RESPONSE.agent_run_id,
      fragment_count: 0,
      fragments: [],
    });
    const AskScreen = await loadScreen();
    render(<AskScreen />);
    await pickDatasource();

    fireEvent.change(screen.getByLabelText("Question"), {
      target: { value: "What was net revenue last quarter?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(runAgentAnalysis).toHaveBeenCalledWith(
        "ds_1",
        { question: "What was net revenue last quarter?" },
        expect.anything(),
      ),
    );
    await waitFor(() => expect(screen.getByText(ANALYSIS_RESPONSE.explanation)).toBeInTheDocument());
    expect(new URLSearchParams(location.search).get("run")).toBe("run_fresh_1");
    // The evidence panel reads live rows/status from the response already
    // held in memory -- no run-detail fetch for the run this session just asked.
    expect(fetchAgentRun).not.toHaveBeenCalled();

    // Provenance the answer was pinned to is surfaced, not just the prose:
    // the semantic model and policy version that grounded it. The model route
    // is not on the fresh response, so it is honestly deferred to the saved run.
    expect(screen.getByText("Provenance")).toBeInTheDocument();
    expect(screen.getByText(ANALYSIS_RESPONSE.semantic_version!)).toBeInTheDocument();
    expect(screen.getByText(ANALYSIS_RESPONSE.policy_version)).toBeInTheDocument();
    expect(screen.getByText("shown on the saved run")).toBeInTheDocument();

    // DQ-3: no open quality incident on this answer's tables means no trust
    // warning at all -- not an empty/hidden one, absent from the DOM.
    expect(screen.queryByRole("alert", { name: "Quality trust warning" })).not.toBeInTheDocument();
  });

  it("renders a machine-readable trust warning and the retrieval demotion reason when the answer's tables have an open quality incident (DQ-3)", async () => {
    runAgentAnalysis.mockResolvedValue({
      ...ANALYSIS_RESPONSE,
      agent_run_id: "run_dq3_1",
      retrieval_evidence: [
        {
          object_type: "TABLE",
          object_id: "t_orders_raw",
          score: 0.71,
          metadata: {
            quality_trust_demotion: {
              reason: "OPEN_QUALITY_INCIDENT",
              demoted_table_ids: ["t_orders_raw"],
              worst_factor: 0.3,
            },
          },
        },
      ],
      plan_evidence: {
        strategy: "FREEFORM_SQL",
        confidence: 0.87,
        trust: {
          trust_score: 58,
          trust_grade: "C",
          warnings: [
            {
              asset_id: "t_orders_raw",
              message:
                "orders_raw has 1 active quality incident (highest severity: CRITICAL). Results may be unreliable.",
              severity: "CRITICAL",
              incident_ids: ["inc_orders_raw_volume"],
            },
          ],
        },
      },
    });
    fetchAgentRunGroundingReceipts.mockResolvedValue({
      agent_run_id: "run_dq3_1",
      fragment_count: 0,
      fragments: [],
    });
    const AskScreen = await loadScreen();
    render(<AskScreen />);
    await pickDatasource();

    fireEvent.change(screen.getByLabelText("Question"), {
      target: { value: "what was net revenue for a table with a quality incident" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    const warning = await screen.findByRole("alert", { name: "Quality trust warning" });
    expect(within(warning).getByText("quality trust warning")).toBeInTheDocument();
    expect(within(warning).getByText(/trust score 58/)).toBeInTheDocument();
    expect(
      within(warning).getByText(
        "orders_raw has 1 active quality incident (highest severity: CRITICAL). Results may be unreliable.",
      ),
    ).toBeInTheDocument();

    // Not shown at all when there is no incident -- proven by the plain
    // success-path test above, which asserts the explanation renders with
    // no assertion needed here beyond this one carrying the warning.
    fireEvent.click(screen.getByText("How this was answered"));
    expect(screen.getByText(/demoted in ranking — open quality incident \(factor 0\.30\)/)).toBeInTheDocument();
  });

  it("renders a 409 ambiguity refusal as a real, informative refusal state -- both definitions, not a generic error or a success", async () => {
    runAgentAnalysis.mockRejectedValue(
      new (await import("../lib/api")).ApiError(409, AMBIGUITY_DETAIL),
    );
    const AskScreen = await loadScreen();
    render(<AskScreen />);
    await pickDatasource();

    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "what is revenue" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    const refusal = await screen.findByRole("alert", { name: "Ambiguous term refusal" });
    expect(
      screen.getByText("This question is ambiguous — more than one governed definition applies"),
    ).toBeInTheDocument();
    expect(within(refusal).getByText("Net Revenue (Finance)")).toBeInTheDocument();
    expect(within(refusal).getByText("Net Revenue (Sales)")).toBeInTheDocument();
    expect(within(refusal).getByText("owner: priya@tenant.example")).toBeInTheDocument();
    expect(within(refusal).getByText("owner: sam@tenant.example")).toBeInTheDocument();

    // Not treated as success: no explanation rendered, no run permalink set,
    // and no generic "error"-only banner swallowing the real refusal text.
    expect(screen.queryByText(ANALYSIS_RESPONSE.explanation)).not.toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("run")).toBeNull();
    expect(screen.queryByText("The question could not be answered")).not.toBeInTheDocument();
  });

  it("distinguishes a disabled-datasource 409 from the AT-9 ambiguity 409", async () => {
    runAgentAnalysis.mockRejectedValue(
      new (await import("../lib/api")).ApiError(409, "datasource is disabled"),
    );
    const AskScreen = await loadScreen();
    render(<AskScreen />);
    await pickDatasource();

    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "anything at all" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(screen.getByText("This datasource is disabled")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert", { name: "Ambiguous term refusal" })).not.toBeInTheDocument();
  });

  it("loads history and clicking a past run loads its detail/grounding receipts without re-asking the question", async () => {
    fetchAgentRuns.mockResolvedValue({ items: [PAST_RUN], limit: 50, offset: 0, total: 1 });
    fetchAgentRun.mockResolvedValue(PAST_RUN);
    fetchAgentRunGroundingReceipts.mockResolvedValue(PAST_RUN_RECEIPTS);
    const AskScreen = await loadScreen();
    render(<AskScreen />);
    await pickDatasource();

    await waitFor(() =>
      expect(fetchAgentRuns).toHaveBeenCalledWith("ds_1", { limit: 50, offset: 0 }, expect.anything()),
    );
    const historyItem = await screen.findByRole("article", { name: "Run run_past_1" });
    fireEvent.click(within(historyItem).getByRole("button"));

    await waitFor(() => expect(fetchAgentRun).toHaveBeenCalledWith("run_past_1", expect.anything()));
    expect(fetchAgentRunGroundingReceipts).toHaveBeenCalledWith("run_past_1", expect.anything());
    expect(runAgentAnalysis).not.toHaveBeenCalled();

    const panel = await screen.findByLabelText("Answer for run run_past_1");
    expect(within(panel).getByText(/not stored on the run record/)).toBeInTheDocument();
    expect(within(panel).getByText("orders raw")).toBeInTheDocument();
    expect(within(panel).getByText("Raw order records, one row per order.")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("run")).toBe("run_past_1");
  });

  it("disables the submit button while a request is in flight", async () => {
    let resolveAnalysis: (value: AgentAnalysisResponse) => void = () => {};
    runAgentAnalysis.mockReturnValue(
      new Promise<AgentAnalysisResponse>((resolve) => {
        resolveAnalysis = resolve;
      }),
    );
    const AskScreen = await loadScreen();
    render(<AskScreen />);
    await pickDatasource();

    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "what is net revenue" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Asking…" })).toBeDisabled());

    resolveAnalysis(ANALYSIS_RESPONSE);
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).not.toBeDisabled());
  });
});
