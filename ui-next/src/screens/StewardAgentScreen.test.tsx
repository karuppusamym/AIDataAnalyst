import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { StewardAgentScreen } from "./StewardAgentScreen";
import { ApiError } from "../lib/api";
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

describe("StewardAgentScreen (ADR-0029)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchTaskAgentState.mockResolvedValue(makeFixtureTaskAgentState(ORG, "steward"));
    runTaskAgent.mockImplementation(async (org: string, kind: string, body: unknown) =>
      makeFixtureTaskAgentRun(org, kind as never, body as never),
    );
  });

  it("asks for the steward agent's own state", async () => {
    render(<StewardAgentScreen />);

    await waitFor(() => expect(fetchTaskAgentState).toHaveBeenCalled());
    expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "steward"]);
  });

  it("shows a registered agent's tier, mode, identity and that it calls no model", async () => {
    render(<StewardAgentScreen />);

    await waitFor(() => expect(screen.getByText("registered")).toBeInTheDocument());
    expect(screen.getByText("proposes for review")).toBeInTheDocument();
    expect(screen.getByText("tier T1")).toBeInTheDocument();
    expect(screen.getByText("deterministic — no model")).toBeInTheDocument();
    expect(screen.getByText("agent:steward")).toBeInTheDocument();
    expect(screen.getByText("7 of 100")).toBeInTheDocument();
  });

  it("explains an unregistered agent and does not offer to run it", async () => {
    fetchTaskAgentState.mockResolvedValue({
      ...makeFixtureTaskAgentState(ORG, "steward"),
      registered: false,
      refusal_reason: "agent_contract_missing",
      ai_asset_version_id: null,
      autonomy_tier: null,
      mode: null,
      outcomes: [],
    });
    render(<StewardAgentScreen />);

    await waitFor(() => expect(screen.getByText("not registered")).toBeInTheDocument());
    expect(
      screen.getByText(/not registered in this organization\. \(agent_contract_missing\)/),
    ).toBeInTheDocument();
    expect(screen.getByText(/supervisor persona is STEWARD/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run steward agent" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
  });

  it("does not offer to run an agent a kill switch is stopping", async () => {
    fetchTaskAgentState.mockResolvedValue({
      ...makeFixtureTaskAgentState(ORG, "steward"),
      kill_engaged: true,
      blocking_reason: "agent_kill_switch_engaged",
    });
    render(<StewardAgentScreen />);

    await waitFor(() => expect(screen.getByText("stopped by a kill switch")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Run steward agent" })).toBeDisabled();
  });

  it("runs, then lists what it proposed with a link into the review queue", async () => {
    render(<StewardAgentScreen />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run steward agent" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Run steward agent" }));

    await waitFor(() =>
      expect(runTaskAgent).toHaveBeenCalledWith(ORG, "steward", {
        capabilities: ["TABLE_DESCRIPTION", "COLUMN_DESCRIPTION", "GLOSSARY_LINK"],
        limit: 10,
        datasource_id: null,
        dry_run: false,
      }),
    );
    expect(await screen.findByText(/2 proposed for review, 1 skipped/)).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "What the run looked at" });
    expect(within(list).getAllByRole("link", { name: "Open in review queue" }).length).toBe(2);
    expect(within(list).getByText("a draft is already open")).toBeInTheDocument();
    // The state is re-read after a run: its pending count just moved.
    expect(fetchTaskAgentState).toHaveBeenCalledTimes(2);
  });

  it("previews with dry_run and says that nothing was opened", async () => {
    render(<StewardAgentScreen />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Preview" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));

    await waitFor(() =>
      expect(runTaskAgent).toHaveBeenCalledWith(
        ORG,
        "steward",
        expect.objectContaining({ dry_run: true }),
      ),
    );
    expect(await screen.findByText(/would be proposed — nothing was opened/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open in review queue" })).toBeNull();
  });

  it("sends only the capabilities left ticked, and refuses to start with none", async () => {
    render(<StewardAgentScreen />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run steward agent" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByLabelText("Glossary links"));
    fireEvent.click(screen.getByRole("button", { name: "Run steward agent" }));
    await waitFor(() =>
      expect(runTaskAgent).toHaveBeenCalledWith(
        ORG,
        "steward",
        expect.objectContaining({ capabilities: ["TABLE_DESCRIPTION", "COLUMN_DESCRIPTION"] }),
      ),
    );

    fireEvent.click(screen.getByLabelText("Table descriptions"));
    fireEvent.click(screen.getByLabelText("Column descriptions"));
    expect(screen.getByRole("button", { name: "Run steward agent" })).toBeDisabled();
  });

  it("shows the refusal in words and its code when the run is refused", async () => {
    runTaskAgent.mockRejectedValue(new ApiError(409, "agent_kill_switch_engaged"));
    render(<StewardAgentScreen />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run steward agent" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Run steward agent" }));

    expect(
      await screen.findByText(/A kill switch is engaged .* \(agent_kill_switch_engaged\)/),
    ).toBeInTheDocument();
  });

  it("shows an em dash, not 0%, for a kind nothing has been decided on", async () => {
    render(<StewardAgentScreen />);

    await waitFor(() => expect(screen.getByText("GLOSSARY_LINK_PROPOSAL")).toBeInTheDocument());
    expect(screen.getByText("75%")).toBeInTheDocument();
    // Glossary links and column descriptions: nothing decided on either yet.
    expect(screen.getAllByText("—")).toHaveLength(2);
  });
});
