import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { AiDecisionRead } from "../lib/types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   UX-15: lineage refusals against LN-3's real `GET /v1/ai-decisions/refusals`
   / `GET /v1/ai-decisions/{run_id}` (`ai_decision_lineage_api.py`).
--------------------------------------------------------------------------- */

const fetchLineageRefusals = vi.fn<
  (organizationId: string, opts: unknown, signal?: AbortSignal) => Promise<{ items: AiDecisionRead[]; limit: number; offset: number; total: number }>
>();
const fetchRunDecisions = vi.fn<
  (runId: string, organizationId: string, signal?: AbortSignal) => Promise<AiDecisionRead[]>
>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchLineageRefusals: (organizationId: string, opts: unknown, signal?: AbortSignal) =>
      fetchLineageRefusals(organizationId, opts, signal),
    fetchRunDecisions: (runId: string, organizationId: string, signal?: AbortSignal) =>
      fetchRunDecisions(runId, organizationId, signal),
  };
});

const REFUSAL: AiDecisionRead = {
  id: "dec_1", organization_id: "org1", run_id: "run_1", decision_type: "REFUSAL",
  source_node: "agent:revenue_analyst", target_node: "tool:tool_revenue_by_lob",
  reason: "tool refused while the raw_sales quality incident is open",
  evidence: {}, control_version: "ADR-0016@2", decided_at: "2026-09-01T15:22:00Z",
};

async function loadScreen() {
  const { LineageRefusalScreen } = await import("./LineageRefusalScreen");
  return LineageRefusalScreen;
}

beforeEach(() => {
  fetchLineageRefusals.mockReset();
  fetchRunDecisions.mockReset();
  fetchLineageRefusals.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("LineageRefusalScreen against the real LN-3 endpoints", () => {
  it("renders refusals from the real endpoint", async () => {
    fetchLineageRefusals.mockResolvedValue({ items: [REFUSAL], limit: 50, offset: 0, total: 1 });
    const LineageRefusalScreen = await loadScreen();

    render(<LineageRefusalScreen />);

    await waitFor(() => expect(screen.getByText(/tool_revenue_by_lob/)).toBeInTheDocument());
    expect(fetchLineageRefusals).toHaveBeenCalledWith(
      "00000000-0000-0000-0000-000000000001",
      { limit: 50, offset: 0 },
      expect.anything(),
    );
  });

  it("permalinks a focused run and loads its full decision trail", async () => {
    fetchLineageRefusals.mockResolvedValue({ items: [REFUSAL], limit: 50, offset: 0, total: 1 });
    fetchRunDecisions.mockResolvedValue([
      { ...REFUSAL, id: "dec_0", decision_type: "RETRIEVAL_SELECTED", reason: "selected as a dependency" },
      REFUSAL,
    ]);
    const LineageRefusalScreen = await loadScreen();
    render(<LineageRefusalScreen />);
    await waitFor(() => expect(screen.getByText(/tool_revenue_by_lob/)).toBeInTheDocument());

    screen.getByRole("button", { name: /tool_revenue_by_lob/ }).click();

    await waitFor(() =>
      expect(fetchRunDecisions).toHaveBeenCalledWith(
        "run_1",
        "00000000-0000-0000-0000-000000000001",
        expect.anything(),
      ),
    );
    expect(await screen.findByText("selected as a dependency")).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("run")).toBe("run_1");
  });

  it("shows a 403 as an explicit denial via the shared ErrorState", async () => {
    fetchLineageRefusals.mockRejectedValue(new ApiError(403, "requires PlatformAdmin or DataAdmin"));
    const LineageRefusalScreen = await loadScreen();

    render(<LineageRefusalScreen />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/PlatformAdmin/));
  });

  it("shows the empty state when there are no refusals", async () => {
    fetchLineageRefusals.mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 });
    const LineageRefusalScreen = await loadScreen();

    render(<LineageRefusalScreen />);

    await waitFor(() => expect(screen.getByText("No refusals recorded")).toBeInTheDocument());
  });
});
