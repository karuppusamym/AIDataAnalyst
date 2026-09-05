import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { OnboardingWizard, PERSONA_CHECKLISTS } from "./OnboardingWizard";

/* ---------------------------------------------------------------------------
   UX-8: guided onboarding branches on the same `Persona` set the shell
   already derives from `GET /v1/me` (module 21 §5) -- these tests exercise
   that branching plus the localStorage-backed, per-persona progress state,
   without a backend (there is none for this: no onboarding-progress field
   exists in `src/aida`, by design -- see the module docstring).
--------------------------------------------------------------------------- */

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("OnboardingWizard persona branching", () => {
  it("renders a persona-agnostic welcome when persona is null (loading or unmapped)", () => {
    render(<OnboardingWizard persona={null} onNavigate={() => {}} />);

    expect(screen.getByText("Get started")).toBeInTheDocument();
    expect(screen.getByText(/No persona is mapped/)).toBeInTheDocument();
  });

  it("renders the Steward checklist for a Steward persona, and a different one for Reviewer", () => {
    const { unmount } = render(<OnboardingWizard persona="Steward" onNavigate={() => {}} />);
    for (const item of PERSONA_CHECKLISTS.Steward) {
      expect(screen.getByText(item.label)).toBeInTheDocument();
    }
    expect(screen.queryByText("Lineage refusals")).not.toBeInTheDocument();
    unmount();

    render(<OnboardingWizard persona="Reviewer" onNavigate={() => {}} />);
    for (const item of PERSONA_CHECKLISTS.Reviewer) {
      expect(screen.getByText(item.label)).toBeInTheDocument();
    }
    expect(screen.queryByText("Studio change sets")).not.toBeInTheDocument();
  });

  it("does not mark migrated operator steps as legacy", () => {
    render(<OnboardingWizard persona="Operator" onNavigate={() => {}} />);

    const sourcesItem = screen.getByText("Sources").closest("li")!;
    expect(sourcesItem.querySelector(".pill")).toBeNull();
    const catalogItem = screen.getByText("Catalog").closest("li")!;
    expect(catalogItem.querySelector(".pill")).toBeNull();
  });

  it("calls onNavigate with the item's real nav id when 'Open' is clicked", () => {
    const onNavigate = vi.fn();
    render(<OnboardingWizard persona="Auditor" onNavigate={onNavigate} />);

    screen.getAllByRole("button", { name: "Open" })[0]!.click();

    expect(onNavigate).toHaveBeenCalledWith("refusals");
  });

  it("persists progress per persona in localStorage and shows it on remount", async () => {
    const { unmount } = render(<OnboardingWizard persona="Reviewer" onNavigate={() => {}} />);
    expect(screen.getByText("0/2 done")).toBeInTheDocument();

    screen.getByRole("checkbox", { name: /Mark "Review queue" as done/ }).click();

    await waitFor(() => expect(screen.getByText("1/2 done")).toBeInTheDocument());
    unmount();

    render(<OnboardingWizard persona="Reviewer" onNavigate={() => {}} />);
    expect(screen.getByText("1/2 done")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Mark "Review queue" as done/ })).toBeChecked();
  });

  it("keeps progress isolated per persona", async () => {
    const { unmount } = render(<OnboardingWizard persona="Reviewer" onNavigate={() => {}} />);
    screen.getByRole("checkbox", { name: /Mark "Review queue" as done/ }).click();
    await waitFor(() => expect(screen.getByText("1/2 done")).toBeInTheDocument());
    unmount();

    render(<OnboardingWizard persona="Auditor" onNavigate={() => {}} />);
    expect(screen.getByText("0/3 done")).toBeInTheDocument();
  });
});
