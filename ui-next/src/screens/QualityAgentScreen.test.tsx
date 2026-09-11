import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { QualityAgentScreen } from "./QualityAgentScreen";
import { makeFixtureTaskAgentRun, makeFixtureTaskAgentState } from "../lib/fixtures";

const fetchTaskAgentState = vi.fn();
const runTaskAgent = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchTaskAgentState: (...args: unknown[]) => fetchTaskAgentState(...args),
    runTaskAgent: (...args: unknown[]) => runTaskAgent(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

describe("QualityAgentScreen (ADR-0029)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchTaskAgentState.mockResolvedValue(makeFixtureTaskAgentState(ORG, "quality"));
    runTaskAgent.mockImplementation(async (org: string, kind: string, body: unknown) =>
      makeFixtureTaskAgentRun(org, kind as never, body as never),
    );
  });

  it("shows that every rule it proposes is a T2 decision", async () => {
    render(<QualityAgentScreen />);

    const proposes = await screen.findByRole("list", { name: "What it proposes" });
    expect(within(proposes).getAllByText("T2")).toHaveLength(2);
    expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "quality"]);
    expect(screen.getByText("agent:quality")).toBeInTheDocument();
  });

  it("runs both capabilities and links a proposal to the shared review queue", async () => {
    render(<QualityAgentScreen />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run quality agent" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Run quality agent" }));

    await waitFor(() =>
      expect(runTaskAgent).toHaveBeenCalledWith(
        ORG,
        "quality",
        expect.objectContaining({ capabilities: ["ROW_COUNT_FLOOR", "NULL_RATE_CEILING"] }),
      ),
    );
    const list = await screen.findByRole("list", { name: "What the run looked at" });
    const [link] = within(list).getAllByRole("link", { name: "Open in review queue" });
    expect(link!.getAttribute("href")).toContain("#/governance");
    expect(within(list).getByText("a rule or a proposal already covers it")).toBeInTheDocument();
  });
});
