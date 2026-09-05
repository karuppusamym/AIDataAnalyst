import { describe, expect, it, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OrgPicker } from "./OrgPicker";
import { OrgProvider, DEFAULT_ORG_ID, useOrgId } from "../lib/org";

/* ---------------------------------------------------------------------------
   The organization every screen reads is chosen in the shell, not hard-coded.

   These assert the two behaviours that keep the migration safe:
     - outside a provider (how every screen's own unit test renders it),
       `useOrgId()` still resolves to the historical dev id, so nothing that
       relied on the old hard-coded constant changes;
     - inside the provider, the picker lists the real organizations (the
       fixture estate here) and a selection is remembered.
--------------------------------------------------------------------------- */

beforeEach(() => {
  try {
    localStorage.clear();
  } catch {
    /* storage disabled — the provider tolerates this, so does the test */
  }
});

function OrgIdProbe() {
  return <span data-testid="probe-org">{useOrgId()}</span>;
}

describe("org selection", () => {
  it("resolves useOrgId() to the default dev org outside a provider", () => {
    render(<OrgIdProbe />);
    expect(screen.getByTestId("probe-org").textContent).toBe(DEFAULT_ORG_ID);
  });

  it("renders nothing when the picker is used outside a provider", () => {
    const { container } = render(<OrgPicker />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists organizations from the API and remembers a selection", async () => {
    render(
      <OrgProvider>
        <OrgPicker />
        <OrgIdProbe />
      </OrgProvider>,
    );

    // The fixture estate loads into the select.
    const select = (await screen.findByTestId("org-select")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(0));
    expect(screen.getByRole("option", { name: "Atlas Demo Bank" })).toBeTruthy();

    // Selecting an org updates the shared id and persists it.
    fireEvent.change(select, { target: { value: DEFAULT_ORG_ID } });
    expect(screen.getByTestId("probe-org").textContent).toBe(DEFAULT_ORG_ID);
    expect(localStorage.getItem("atlas.org.id")).toBe(DEFAULT_ORG_ID);
  });
});
