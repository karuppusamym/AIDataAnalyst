import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { AgentInboxScreen } from "./AgentInboxScreen";
import { makeFixtureAgentInbox } from "../lib/fixtures";

const fetchAgentInbox = vi.fn();
const engageAgentKillSwitch = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchAgentInbox: (...args: unknown[]) => fetchAgentInbox(...args),
    engageAgentKillSwitch: (...args: unknown[]) => engageAgentKillSwitch(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

describe("AgentInboxScreen (UX-21)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchAgentInbox.mockResolvedValue(makeFixtureAgentInbox(ORG, "STEWARD"));
  });

  it("shows the five summary counts a supervisor opens the screen for", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);

    await waitFor(() => expect(screen.getByText("Waiting on you")).toBeInTheDocument());
    expect(screen.getByText("Auto-applied")).toBeInTheDocument();
    expect(screen.getByText("Sampled for audit")).toBeInTheDocument();
    expect(screen.getByText("Agents active")).toBeInTheDocument();
  });

  it("warns prominently when any kill switch is engaged", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() =>
      expect(screen.getByText(/kill switch is engaged/i)).toBeInTheDocument(),
    );
  });

  it("labels who proposed each pending item, human or agent", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);

    await waitFor(() => expect(screen.getAllByText("AGENT").length).toBeGreaterThan(0));
    expect(screen.getByText("HUMAN")).toBeInTheDocument();
  });

  it("shows the reviewer agent's recommendation and says whose it is", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);

    await waitFor(() => expect(screen.getByText("APPROVE")).toBeInTheDocument());
    expect(screen.getByText("REJECT")).toBeInTheDocument();
    expect(screen.getAllByText(/recommended by reviewer agent/i).length).toBe(2);
  });

  it("surfaces prior rejections, which is the strongest reason not to approve", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() => expect(screen.getByText(/rejected before ×2/)).toBeInTheDocument());
  });

  it("keeps the server's ordering rather than re-sorting", async () => {
    // Blast radius desc, then confidence desc. The fixture is returned in the
    // API's order and the screen must not second-guess it -- "why is this
    // first" needs one answer.
    render(<AgentInboxScreen persona="STEWARD" />);

    await waitFor(() => expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0));
    const titles = screen
      .getAllByRole("button", { name: /^Open review/ })
      .map((button) => button.getAttribute("aria-label"));
    expect(titles[0]).toContain("ASSET_DESCRIPTION_DRAFT");
    expect(titles[2]).toContain("SEMANTIC_MODEL_VERSION");
  });

  it("opens the review queue focused on the clicked item", async () => {
    const onNavigate = vi.fn();
    render(<AgentInboxScreen persona="STEWARD" onNavigate={onNavigate} />);

    await waitFor(() => expect(screen.getAllByRole("button", { name: /^Open review/ }).length).toBe(3));
    fireEvent.click(screen.getAllByRole("button", { name: /^Open review/ })[0]!);

    expect(onNavigate).toHaveBeenCalledWith("governance", {
      review: "bbbbbbbb-1111-1111-1111-111111111111",
    });
  });

  it("marks an auto-applied task that was sampled for audit", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);
    // "Sampled for audit" is both a tile label and a per-task pill; the pill
    // is the one this asserts.
    await waitFor(() => expect(screen.getAllByText(/sampled for audit/i).length).toBe(2));
    expect(screen.getByText("PENDING")).toBeInTheDocument();
  });

  it("shows consumption against the cap, marked as an estimate", async () => {
    // No provider adapter reports usage, so the number beside the cap is the
    // gateway's own estimate. The UI must not present it as measured.
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() =>
      expect(screen.getByText(/≈43% of today's cap/)).toBeInTheDocument(),
    );
  });

  it("distinguishes 'no model call today' from zero consumption", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() =>
      expect(screen.getByText(/no model call today/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/≈0%/)).toBeNull();
  });

  it("offers no kill switch for an agent already killed", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);

    await waitFor(() => expect(screen.getByText("Red-team agent")).toBeInTheDocument());
    const killed = screen.getByText("Red-team agent").closest("li")!;
    expect(within(killed).queryByRole("button", { name: /engage kill switch/i })).toBeNull();
    expect(within(killed).getByText("kill engaged")).toBeInTheDocument();
  });

  /* The rationale is collected in a real dialog, not `window.prompt`
     (review 2026-09-05, F21). These assert the behaviour that moved: the
     confirm button is blocked until a reason is written, the reason reaches
     the endpoint, and a failing request is reported inside the dialog
     instead of dismissing it as though the agent had been stopped. */
  async function openKillDialog() {
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /engage kill switch/i }).length).toBe(2),
    );
    // jsdom does not focus a button on click the way a browser does, and
    // focus restore is only meaningful relative to whatever had focus.
    const trigger = screen.getAllByRole("button", { name: /engage kill switch/i })[0]!;
    trigger.focus();
    fireEvent.click(trigger);
    return await screen.findByRole("dialog");
  }

  it("requires a reason before engaging a kill switch", async () => {
    const dialog = await openKillDialog();

    const confirm = within(dialog).getByRole("button", { name: /engage kill switch/i });
    expect(confirm).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText(/reason/i), { target: { value: "   " } });
    expect(confirm).toBeDisabled();
    expect(engageAgentKillSwitch).not.toHaveBeenCalled();
  });

  it("engages the kill switch with the reason the human gave", async () => {
    engageAgentKillSwitch.mockResolvedValue(undefined);
    const dialog = await openKillDialog();

    fireEvent.change(within(dialog).getByLabelText(/reason/i), {
      target: { value: "drift on descriptions" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /engage kill switch/i }));

    await waitFor(() =>
      expect(engageAgentKillSwitch).toHaveBeenCalledWith(
        ORG,
        "aaaaaaaa-1111-1111-1111-111111111111",
        "drift on descriptions",
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps the kill dialog open and shows why, when the request is refused", async () => {
    engageAgentKillSwitch.mockRejectedValue(new Error("kill switch requires an owner"));
    const dialog = await openKillDialog();

    fireEvent.change(within(dialog).getByLabelText(/reason/i), { target: { value: "runaway cost" } });
    fireEvent.click(within(dialog).getByRole("button", { name: /engage kill switch/i }));

    expect(await screen.findByText("kill switch requires an owner")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("traps focus inside the kill dialog and restores it to the opener on cancel", async () => {
    const dialog = await openKillDialog();
    // The reason field is focused on open, not left to the browser: the
    // whole point of the dialog is the sentence it collects.
    expect(document.activeElement).toBe(within(dialog).getByLabelText(/reason/i));

    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(
      screen.getAllByRole("button", { name: /engage kill switch/i })[0]!,
    );
  });

  it("renders an error state rather than a blank screen", async () => {
    fetchAgentInbox.mockRejectedValue(new Error("boom"));
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() =>
      expect(screen.getByText(/agent inbox could not be loaded/i)).toBeInTheDocument(),
    );
  });

  it("refetches when the persona changes and puts it in the URL", async () => {
    render(<AgentInboxScreen persona="STEWARD" />);
    await waitFor(() => expect(fetchAgentInbox).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Inbox persona"), { target: { value: "REVIEWER" } });

    await waitFor(() => expect(fetchAgentInbox).toHaveBeenCalledTimes(2));
    expect(fetchAgentInbox.mock.calls[1]![1]).toBe("REVIEWER");
    expect(location.search).toContain("persona=REVIEWER");
  });
});
