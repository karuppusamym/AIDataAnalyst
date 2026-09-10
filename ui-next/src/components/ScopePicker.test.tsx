import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

/* ---------------------------------------------------------------------------
   The scope picker's vertical budget, and what survives collapsing it.

   On a 1366x768 laptop at 100% zoom the four scope fields rendered 446px tall
   -- 58% of the viewport -- which pushed the first sidebar navigation link to
   y=525 and left 6 of 19 nav items on screen. The fields now start collapsed.

   What these assert is the part that makes the collapse safe rather than the
   collapse itself: a picker that hides which tenant, workspace and source are
   live would be a worse bug than the layout one. So the summary and the
   binding status must be readable while collapsed, the fields must be genuinely
   hidden (not merely visually short, which would leave them in the tab order),
   and the toggle must carry its own accessible name -- the eyebrow and the
   summary are adjacent spans, so the computed name would otherwise read
   "ACTIVE DATA SCOPEAtlas Demo Bank".
--------------------------------------------------------------------------- */

const ORG = { id: "org_1", name: "Atlas Demo Bank" };
const WORKSPACE = { id: "ws_1", name: "Governed analytics" };
const SOURCE = { id: "ds_1", name: "snowflake_prod" };

vi.mock("../lib/org", () => ({
  useOrgSelection: () => ({
    orgId: ORG.id,
    organizations: [ORG],
    setOrgId: () => {},
    addOrganization: () => {},
    loading: false,
    error: null,
  }),
}));

vi.mock("../lib/scope", () => ({
  useScopeSelection: () => ({
    workspaceId: WORKSPACE.id,
    projectId: "",
    datasourceId: SOURCE.id,
    workspaces: [WORKSPACE],
    projects: [],
    datasources: [SOURCE],
    bindings: [{ datasource_id: SOURCE.id, status: "ACTIVE", masking_profile: "DEFAULT" }],
    visibleProjects: [],
    visibleDatasources: [SOURCE],
    setWorkspaceId: () => {},
    setProjectId: () => {},
    setDatasourceId: () => {},
    refresh: () => {},
    loading: false,
    error: null,
    ready: true,
  }),
}));

import { ScopePicker } from "./ScopePicker";

const toggle = () => screen.getByRole("button", { name: /^Active data scope:/ });

describe("ScopePicker vertical budget", () => {
  it("starts collapsed, with the scope still readable", () => {
    render(<ScopePicker />);

    expect(toggle()).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.getByText("Atlas Demo Bank · Governed analytics · snowflake_prod"),
    ).toBeVisible();
    expect(screen.getByText("active binding · default masking")).toBeVisible();
  });

  it("keeps the collapsed fields out of the accessibility tree and the tab order", () => {
    render(<ScopePicker />);

    // `getByRole` ignores a `hidden` subtree; `queryByLabelText` does not, so
    // this distinguishes "genuinely hidden" from "rendered but short".
    expect(screen.queryByRole("combobox", { name: /Organization/ })).not.toBeInTheDocument();
    expect(document.getElementById("scope-fields")).toHaveAttribute("hidden");
  });

  it("expands to the four fields on click, and collapses again", () => {
    render(<ScopePicker />);

    fireEvent.click(toggle());
    expect(toggle()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText(/Organization/)).toHaveValue(ORG.id);
    expect(screen.getByLabelText(/Workspace/)).toHaveValue(WORKSPACE.id);
    expect(screen.getByLabelText(/Source/)).toHaveValue(SOURCE.id);

    fireEvent.click(toggle());
    expect(toggle()).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("scope-fields")).toHaveAttribute("hidden");
  });

  it("names the toggle without running the eyebrow into the summary", () => {
    render(<ScopePicker />);

    expect(toggle()).toHaveAccessibleName(
      "Active data scope: Atlas Demo Bank · Governed analytics · snowflake_prod",
    );
  });
});
