import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { TaskAgentsScreen } from "./TaskAgentsScreen";
import { ApiError } from "../lib/api";
import { makeFixtureTaskAgentRun, makeFixtureTaskAgentState } from "../lib/fixtures";

/* ---------------------------------------------------------------------------
   R11-S10 — the three task-agent consoles, merged into one screen.

   This file is `StewardAgentScreen.test.tsx`, `LineageAgentScreen.test.tsx` and
   `QualityAgentScreen.test.tsx` consolidated. Every assertion those three made
   is kept verbatim; the only change is HOW the console under test is opened --
   by the `agent` query field rather than by rendering one of three components.
   That is the merge, so that is exactly the part worth re-proving.

   The cases at the bottom are new, and are about the merge itself: that the
   selector actually switches which agent is asked for, and that a bookmark to
   one of the three retired routes still lands on the right console.
--------------------------------------------------------------------------- */

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

/** Open the merged console on one agent, the way a URL does. */
function renderAgent(kind: "steward" | "lineage" | "quality") {
  history.replaceState(null, "", `/?agent=${kind}#/steward/task-agents`);
  return render(<TaskAgentsScreen />);
}

beforeEach(() => {
  vi.clearAllMocks();
  history.replaceState(null, "", "/");
  runTaskAgent.mockImplementation(async (org: string, kind: string, body: unknown) =>
    makeFixtureTaskAgentRun(org, kind as never, body as never),
  );
});

describe("the steward agent's console (ADR-0029)", () => {
  beforeEach(() => {
    fetchTaskAgentState.mockResolvedValue(makeFixtureTaskAgentState(ORG, "steward"));
  });

  it("asks for the steward agent's own state", async () => {
    renderAgent("steward");

    await waitFor(() => expect(fetchTaskAgentState).toHaveBeenCalled());
    expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "steward"]);
  });

  it("shows a registered agent's tier, mode, identity and that it calls no model", async () => {
    renderAgent("steward");

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
    renderAgent("steward");

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
    renderAgent("steward");

    await waitFor(() => expect(screen.getByText("stopped by a kill switch")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Run steward agent" })).toBeDisabled();
  });

  it("runs, then lists what it proposed with a link into the review queue", async () => {
    renderAgent("steward");

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
    renderAgent("steward");

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
    renderAgent("steward");

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
    renderAgent("steward");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run steward agent" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Run steward agent" }));

    expect(
      await screen.findByText(/A kill switch is engaged .* \(agent_kill_switch_engaged\)/),
    ).toBeInTheDocument();
  });

  it("shows an em dash, not 0%, for a kind nothing has been decided on", async () => {
    renderAgent("steward");

    await waitFor(() => expect(screen.getByText("GLOSSARY_LINK_PROPOSAL")).toBeInTheDocument());
    expect(screen.getByText("75%")).toBeInTheDocument();
    // Glossary links and column descriptions: nothing decided on either yet.
    expect(screen.getAllByText("—")).toHaveLength(2);
  });
});

describe("the lineage agent's console (ADR-0029)", () => {
  beforeEach(() => {
    fetchTaskAgentState.mockResolvedValue(makeFixtureTaskAgentState(ORG, "lineage"));
  });

  it("asks for the lineage agent's state and shows that a person decides its edges", async () => {
    renderAgent("lineage");

    // Both capabilities -- views and procedures -- are decided in a queue no
    // agent reads from, so neither carries an ADR-0027 tier.
    await waitFor(() => expect(screen.getAllByText("human review")).toHaveLength(2));
    expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "lineage"]);
    expect(screen.getByText("agent:lineage")).toBeInTheDocument();
  });

  it("links a proposal to the parsed-lineage queue, not the governance one", async () => {
    renderAgent("lineage");

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
    /* R11-S10: the parsed-lineage queue is a queue OF the review surface now,
       so the link names that screen and selects the queue. The old
       `#/parsed-lineage-review` still resolves -- see `routes.test.ts` -- but
       it is no longer what the app emits. */
    expect(viewLink!.getAttribute("href")).toContain("#/reviewer/governance");
    expect(viewLink!.getAttribute("href")).toContain("queue=parsed-lineage");
    expect(viewLink!.getAttribute("href")).toContain("type=VIEW");
    expect(routineLink!.getAttribute("href")).toContain("type=ROUTINE");
    expect(within(list).getByText("the definition could not be parsed")).toBeInTheDocument();
  });
});

describe("the quality agent's console (ADR-0029)", () => {
  beforeEach(() => {
    fetchTaskAgentState.mockResolvedValue(makeFixtureTaskAgentState(ORG, "quality"));
  });

  it("shows that every rule it proposes is a T2 decision", async () => {
    renderAgent("quality");

    const proposes = await screen.findByRole("list", { name: "What it proposes" });
    expect(within(proposes).getAllByText("T2")).toHaveLength(2);
    expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "quality"]);
    expect(screen.getByText("agent:quality")).toBeInTheDocument();
  });

  it("runs both capabilities and links a proposal to the shared review queue", async () => {
    renderAgent("quality");

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
    expect(link!.getAttribute("href")).toContain("#/reviewer/governance");
    expect(within(list).getByText("a rule or a proposal already covers it")).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
   The merge itself.
--------------------------------------------------------------------------- */

describe("one console for three agents (R11-S10)", () => {
  beforeEach(() => {
    fetchTaskAgentState.mockImplementation(async (org: string, kind: string) =>
      makeFixtureTaskAgentState(org, kind as never),
    );
  });

  it("offers all three agents as tabs and marks the selected one", async () => {
    renderAgent("lineage");

    const tabs = screen.getByRole("tablist", { name: "Task agent" });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Steward",
      "Lineage",
      "Quality",
    ]);
    expect(within(tabs).getByRole("tab", { name: "Lineage" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => expect(fetchTaskAgentState).toHaveBeenCalled());
  });

  it("asks the backend for the agent the selector names", async () => {
    renderAgent("steward");
    await waitFor(() =>
      expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "steward"]),
    );

    fireEvent.click(screen.getByRole("tab", { name: "Quality" }));

    /* The selection is in the URL, which is what makes a console shareable and
       what the retired routes alias onto. */
    await waitFor(() => expect(location.search).toContain("agent=quality"));
    await waitFor(() =>
      expect(
        fetchTaskAgentState.mock.calls.some((call) => call[1] === "quality"),
      ).toBe(true),
    );
  });

  it("falls back to the steward agent when the URL names no agent", async () => {
    history.replaceState(null, "", "/#/steward/task-agents");
    render(<TaskAgentsScreen />);

    expect(screen.getByRole("tab", { name: "Steward" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() =>
      expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "steward"]),
    );
  });

  it("falls back to the steward agent when the URL names one that does not exist", async () => {
    history.replaceState(null, "", "/?agent=nonsense#/steward/task-agents");
    render(<TaskAgentsScreen />);

    expect(screen.getByRole("tab", { name: "Steward" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() =>
      expect(fetchTaskAgentState.mock.calls[0]!.slice(0, 2)).toEqual([ORG, "steward"]),
    );
  });
});
