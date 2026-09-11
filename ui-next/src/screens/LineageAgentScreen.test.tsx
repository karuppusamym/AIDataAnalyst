import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { LineageAgentScreen } from "./LineageAgentScreen";
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

describe("LineageAgentScreen (ADR-0029)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchTaskAgentState.mockResolvedValue(makeFixtureTaskAgentState(ORG, "lineage"));
    runTaskAgent.mockImplementation(async (org: string, kind: string, body: unknown) =>
      makeFixtureTaskAgentRun(org, kind as never, body as never),
    );
  });

  it("asks for the lineage agent's state and shows that a person decides its edges", async () => {
    render(<LineageAgentScreen />);

    // Both capabilities -- views and procedures -- are decided in a queue no
    // agent reads from, so neither carries an ADR-0027 tier.
    await waitFor(() => expect(screen.getAllByText("human review")).toHaveLength(2));
    expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "lineage"]);
    expect(screen.getByText("agent:lineage")).toBeInTheDocument();
  });

  it("links a proposal to the parsed-lineage review queue, not the shared one", async () => {
    render(<LineageAgentScreen />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run lineage agent" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Run lineage agent" }));

    await waitFor(() =>
      expect(runTaskAgent).toHaveBeenCalledWith(
        ORG,
        "lineage",
        expect.objectContaining({ capabilities: ["VIEW_LINEAGE", "PROCEDURE_LINEAGE"] }),
      ),
    );
    const list = await screen.findByRole("list", { name: "What the run looked at" });
    // Each proposal opens the per-edge queue filtered to the table it wrote.
    const [viewLink, routineLink] = within(list).getAllByRole("link", {
      name: "Open in review queue",
    });
    expect(viewLink!.getAttribute("href")).toContain("#/parsed-lineage-review");
    expect(viewLink!.getAttribute("href")).toContain("type=VIEW");
    expect(routineLink!.getAttribute("href")).toContain("type=ROUTINE");
    expect(within(list).getByText("the definition could not be parsed")).toBeInTheDocument();
  });
});
