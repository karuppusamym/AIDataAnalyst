import { describe, expect, it, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import {
  DEFAULT_ORG_ID,
  OrgProvider,
  useOrgId,
  useOrgSelection,
  type OrgSelection,
} from "./org";

/* ---------------------------------------------------------------------------
   The organization every screen reads is chosen once, in the shell.

   R11-S13 (M6): these assertions used to live in `components/OrgPicker.test.tsx`
   and were the only coverage this provider had. `OrgPicker` was a second,
   never-mounted org control -- the shell's real one is `ScopePicker` -- so
   deleting it would have deleted the provider's tests with it. They are moved
   here, onto `lib/org` itself, because the provider is what the shell and every
   migrated screen actually depend on; the picker was only ever the probe.

   The three behaviours that keep the migration safe:
     - outside a provider (how every screen's own unit test renders it),
       `useOrgId()` still resolves to the historical dev id, so nothing that
       relied on the old hard-coded constant changes;
     - outside a provider `useOrgSelection()` is `null` rather than a guessed
       default, so a consumer can choose to render nothing;
     - inside the provider the real organizations are listed (the fixture estate
       here) and a selection is remembered across reloads.
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

let captured: OrgSelection | null = null;

function SelectionProbe() {
  const selection = useOrgSelection();
  captured = selection;
  if (!selection) return <span data-testid="probe-none">no provider</span>;
  return (
    <span data-testid="probe-names">
      {selection.organizations.map((item) => item.name).join(",")}
    </span>
  );
}

describe("org selection", () => {
  it("resolves useOrgId() to the default dev org outside a provider", () => {
    render(<OrgIdProbe />);
    expect(screen.getByTestId("probe-org").textContent).toBe(DEFAULT_ORG_ID);
  });

  it("reports no selection at all outside a provider", () => {
    captured = null;
    render(<SelectionProbe />);
    expect(screen.getByTestId("probe-none")).toBeTruthy();
    expect(captured).toBeNull();
  });

  it("lists organizations from the API and remembers a selection", async () => {
    captured = null;
    render(
      <OrgProvider>
        <SelectionProbe />
        <OrgIdProbe />
      </OrgProvider>,
    );

    // The fixture estate loads into the shared selection.
    await waitFor(() => expect(captured?.organizations.length).toBeGreaterThan(0));
    expect(screen.getByTestId("probe-names").textContent).toContain("Atlas Demo Bank");

    // Selecting an org updates the shared id and persists it.
    act(() => captured!.setOrgId(DEFAULT_ORG_ID));
    expect(screen.getByTestId("probe-org").textContent).toBe(DEFAULT_ORG_ID);
    expect(localStorage.getItem("atlas.org.id")).toBe(DEFAULT_ORG_ID);
  });
});
