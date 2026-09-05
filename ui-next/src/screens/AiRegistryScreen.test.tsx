import { describe, expect, it, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AiRegistryScreen } from "./AiRegistryScreen";

/* ---------------------------------------------------------------------------
   The AI registry closes the governance loop the React portal was missing:
   an asset's trust grade with its blocking findings, and remediations that can
   be advanced. These run against the fixture API (the default), which mirrors
   ai_registry_api.py's wire shape.
--------------------------------------------------------------------------- */

beforeEach(() => {
  history.replaceState(null, "", "/");
});

describe("AiRegistryScreen", () => {
  it("lists AI assets, shows a selected asset's trust grade and blockers, and advances a remediation", async () => {
    render(<AiRegistryScreen />);

    // Assets load from the registry.
    await screen.findByText("Revenue Analyst");
    const fraud = await screen.findByText("Fraud Scoring Model");

    // Selecting the high-risk model shows its UNTRUSTED grade and a blocking finding.
    fireEvent.click(fraud);
    await screen.findByText("untrusted");
    expect(
      screen.getByText("High-risk asset with an open HIGH-severity remediation"),
    ).toBeInTheDocument();

    // Its remediations are listed and can be advanced.
    const bias = await screen.findByText("Provide bias and fairness evaluation evidence");
    const item = bias.closest("li") as HTMLElement;
    const statusSelect = within(item).getByRole("combobox") as HTMLSelectElement;
    expect(statusSelect.value).toBe("OPEN");

    fireEvent.change(statusSelect, { target: { value: "RESOLVED" } });
    await waitFor(() =>
      expect(within(item).getByText("resolved")).toBeInTheDocument(),
    );
  });

  it("prompts to select an asset before one is chosen", async () => {
    render(<AiRegistryScreen />);
    await screen.findByText("Revenue Analyst");
    expect(screen.getByText("Select an AI asset")).toBeInTheDocument();
  });
});
