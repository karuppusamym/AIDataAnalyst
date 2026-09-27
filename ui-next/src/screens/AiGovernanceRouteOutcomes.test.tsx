import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

import { ApiError } from "../lib/api";
import type { ModelRouteOutcomesRead } from "../lib/types";
import { expectNoAxeViolations } from "../test/a11y";
import { RouteOutcomesPanel } from "./AiGovernanceRouteOutcomes";

/* R11-MP11: the route outcomes panel shows counts from real runs, says when the
   server's bound cut the window, and shows cost only where a provider stated it. */

const ORG = "00000000-0000-0000-0000-000000000001";

const fetchModelRouteOutcomes =
  vi.fn<(organizationId: string, days: number, signal?: AbortSignal) => Promise<ModelRouteOutcomesRead>>();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchModelRouteOutcomes: (organizationId: string, days: number, signal?: AbortSignal) =>
      fetchModelRouteOutcomes(organizationId, days, signal),
  };
});

function outcomes(overrides: Partial<ModelRouteOutcomesRead> = {}): ModelRouteOutcomesRead {
  return {
    organization_id: ORG,
    since: "2026-09-17T00:00:00Z",
    runs_considered: 12,
    truncated: false,
    routes: [
      {
        route_key: "gemini-bank-sql",
        runs: 10,
        completed: 8,
        rejected: 2,
        failed: 0,
        fallback_runs: 1,
        circuit_skips: 1,
        repairs_attempted: 3,
        repairs_valid: 2,
        candidates_compared: 4,
        candidates_identical: 1,
        candidates_same_sources: 2,
        candidates_different: 1,
        stated_cost_usd: null,
        cached_input_tokens: 0,
      },
      {
        route_key: "openrouter-eu",
        runs: 2,
        completed: 2,
        rejected: 0,
        failed: 0,
        fallback_runs: 0,
        circuit_skips: 0,
        repairs_attempted: 0,
        repairs_valid: 0,
        candidates_compared: 0,
        candidates_identical: 0,
        candidates_same_sources: 0,
        candidates_different: 0,
        stated_cost_usd: 0.0123,
        cached_input_tokens: 900,
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  fetchModelRouteOutcomes.mockReset();
});

describe("RouteOutcomesPanel", () => {
  it("shows each route's record from the runs, and cost only where a provider stated it", async () => {
    fetchModelRouteOutcomes.mockResolvedValue(outcomes());
    const { container } = render(<RouteOutcomesPanel organizationId={ORG} />);

    const gemini = (await screen.findByText("gemini-bank-sql")).closest("tr") as HTMLElement;
    expect(within(gemini).getByText("80%")).toBeTruthy();
    expect(within(gemini).getByText("2 refused")).toBeTruthy();
    expect(within(gemini).getByText("1 skipped while cooling down")).toBeTruthy();
    expect(within(gemini).getByText("75%")).toBeTruthy();
    const openrouter = screen.getByText("openrouter-eu").closest("tr") as HTMLElement;
    expect(within(openrouter).getByText("$0.0123")).toBeTruthy();
    expect(fetchModelRouteOutcomes).toHaveBeenCalledWith(ORG, 7, expect.anything());
    expect(screen.getByText(/12 runs counted\./)).toBeTruthy();
    await expectNoAxeViolations(container);
  });

  it("says when the window was cut short rather than presenting a partial count as the whole", async () => {
    fetchModelRouteOutcomes.mockResolvedValue(outcomes({ truncated: true, runs_considered: 2000 }));
    render(<RouteOutcomesPanel organizationId={ORG} />);
    expect(await screen.findByText(/the newest only/)).toBeTruthy();
  });

  it("explains an empty window instead of showing an empty table", async () => {
    fetchModelRouteOutcomes.mockResolvedValue(outcomes({ routes: [], runs_considered: 0 }));
    render(<RouteOutcomesPanel organizationId={ORG} />);
    expect(await screen.findByText("No Ask run reached a model in this window")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("shows the server's refusal in its own words", async () => {
    fetchModelRouteOutcomes.mockRejectedValue(new ApiError(403, "organization access denied"));
    render(<RouteOutcomesPanel organizationId={ORG} />);
    expect(await screen.findByText("organization access denied")).toBeTruthy();
  });
});
