import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import { ReviewerAgentScreen } from "./ReviewerAgentScreen";
import { ApiError } from "../lib/api";
import {
  makeFixtureDisagreementRates,
  makeFixtureReviewerAgentSamples,
  makeFixtureReviewerAgentState,
} from "../lib/fixtures";

const fetchReviewerAgentState = vi.fn();
const runReviewerAgentPreReview = vi.fn();
const runReviewerAgent = vi.fn();
const suspendReviewerAgent = vi.fn();
const resumeReviewerAgent = vi.fn();
const fetchDisagreementRates = vi.fn();
const fetchReviewerAgentSamples = vi.fn();
const resolveAuditSample = vi.fn();

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    fetchReviewerAgentState: (...args: unknown[]) => fetchReviewerAgentState(...args),
    runReviewerAgentPreReview: (...args: unknown[]) => runReviewerAgentPreReview(...args),
    runReviewerAgent: (...args: unknown[]) => runReviewerAgent(...args),
    suspendReviewerAgent: (...args: unknown[]) => suspendReviewerAgent(...args),
    resumeReviewerAgent: (...args: unknown[]) => resumeReviewerAgent(...args),
    fetchDisagreementRates: (...args: unknown[]) => fetchDisagreementRates(...args),
    fetchReviewerAgentSamples: (...args: unknown[]) => fetchReviewerAgentSamples(...args),
    resolveAuditSample: (...args: unknown[]) => resolveAuditSample(...args),
  };
});

const ORG = "00000000-0000-0000-0000-000000000001";

describe("ReviewerAgentScreen (ADR-0027)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    history.replaceState(null, "", "/");
    fetchReviewerAgentState.mockResolvedValue(makeFixtureReviewerAgentState(ORG));
    fetchDisagreementRates.mockResolvedValue(makeFixtureDisagreementRates(30));
    fetchReviewerAgentSamples.mockResolvedValue(
      makeFixtureReviewerAgentSamples({ outcome: "PENDING" }),
    );
  });

  it("shows the agent's state — enabled, active, tier and sampling rate", async () => {
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getByText("enabled")).toBeInTheDocument());
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("max tier T1")).toBeInTheDocument();
    expect(screen.getByText("10%")).toBeInTheDocument();
    expect(screen.getByText("agent:reviewer")).toBeInTheDocument();
  });

  it("shows a suspended state with a Resume action instead of Suspend", async () => {
    fetchReviewerAgentState.mockResolvedValue({
      ...makeFixtureReviewerAgentState(ORG),
      suspended: true,
    });
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getByText("suspended")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Suspend" })).toBeNull();
  });

  it("requires a reason before suspending the agent", async () => {
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("   ");
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Suspend" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Suspend" }));

    expect(suspendReviewerAgent).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  it("suspends the agent with the reason a human gave, and shows a notice", async () => {
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("false positives rising");
    suspendReviewerAgent.mockResolvedValue({ ...makeFixtureReviewerAgentState(ORG), suspended: true });
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Suspend" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Suspend" }));

    await waitFor(() =>
      expect(suspendReviewerAgent).toHaveBeenCalledWith(ORG, "false positives rising"),
    );
    expect(await screen.findByText(/reviewer agent suspended/i)).toBeInTheDocument();
    promptSpy.mockRestore();
  });

  it("runs the reviewer agent and shows the decision counts it returned", async () => {
    runReviewerAgent.mockResolvedValue({
      pre_reviewed: 0,
      decided: 8,
      approved: 5,
      rejected: 3,
      sampled_for_audit: 2,
    });
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Run reviewer agent" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Run reviewer agent" }));

    expect(
      await screen.findByText("8 decided — 5 approved, 3 rejected, 2 sampled for audit"),
    ).toBeInTheDocument();
  });

  it("surfaces the 409 message when the agent is disabled or suspended", async () => {
    runReviewerAgent.mockRejectedValue(new ApiError(409, "reviewer agent is suspended"));
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Run reviewer agent" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Run reviewer agent" }));

    expect(await screen.findByText("reviewer agent is suspended")).toBeInTheDocument();
  });

  it("shows the disagreement-rate report with a breach pill", async () => {
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getAllByText("ASSET_DESCRIPTION_DRAFT").length).toBeGreaterThan(0));
    expect(screen.getAllByText("breaches revisit trigger").length).toBeGreaterThan(0);
    expect(screen.getByText("insufficient sample")).toBeInTheDocument();
  });

  it("shows an empty state, not a 0%, when nothing has been resolved yet", async () => {
    fetchDisagreementRates.mockResolvedValue({
      window_days: 7,
      computed_at: new Date().toISOString(),
      measured: false,
      threshold: 0.05,
      minimum_resolved_for_signal: 20,
      breaching_object_types: [],
      by_object_type: [],
    });
    render(<ReviewerAgentScreen />);

    await waitFor(() =>
      expect(screen.getByText(/nothing has been resolved in this window yet/i)).toBeInTheDocument(),
    );
  });

  it("shows the pending sampled decisions queue with Agree/Disagree actions", async () => {
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getAllByRole("button", { name: "Agree" }).length).toBeGreaterThan(0));
    expect(screen.getAllByRole("button", { name: "Disagree" }).length).toBeGreaterThan(0);
  });

  it("requires a rationale before resolving a sampled decision", async () => {
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("");
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getAllByRole("button", { name: "Agree" }).length).toBeGreaterThan(0));
    fireEvent.click(screen.getAllByRole("button", { name: "Agree" })[0]!);

    expect(resolveAuditSample).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  it("resolves a sampled decision with the human's rationale and reloads the queue", async () => {
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("Matches the source definition.");
    resolveAuditSample.mockResolvedValue({});
    render(<ReviewerAgentScreen />);

    await waitFor(() => expect(screen.getAllByRole("button", { name: "Agree" }).length).toBeGreaterThan(0));
    fireEvent.click(screen.getAllByRole("button", { name: "Agree" })[0]!);

    await waitFor(() =>
      expect(resolveAuditSample).toHaveBeenCalledWith(
        ORG,
        "ffffffff-1111-1111-1111-111111111111",
        { human_outcome: "AGREED", rationale: "Matches the source definition." },
      ),
    );
    expect(await screen.findByText(/marked the .* sample as agreed/i)).toBeInTheDocument();
    expect(fetchReviewerAgentSamples).toHaveBeenCalledTimes(2);
    promptSpy.mockRestore();
  });
});
