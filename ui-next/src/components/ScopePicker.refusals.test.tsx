import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

/* ---------------------------------------------------------------------------
   What a refused scope looks like, which R11-B11's browser journey found this
   screen was not saying.

   Two separate silences, both here because this is the component the shell
   actually mounts:

   * `OrgSelection.error` was populated and read by nobody, so a refused
     organization list rendered as an empty dropdown -- which reads as "this
     tenant has no organizations", the one thing it does not mean. `OrgPicker`
     has always shown it, but that component is not wired into the shell.
   * the scope provider carried the server's own message and the status line
     replaced it with a flat "Scope could not be loaded", so a caller refused
     for a nameable reason read the same as a dead network.

   A separate file from `ScopePicker.test.tsx` because these mocks are
   module-level: the two states cannot coexist in one module graph.
--------------------------------------------------------------------------- */

const ORG = { id: "org_1", name: "Atlas Demo Bank" };

vi.mock("../lib/org", () => ({
  useOrgSelection: () => ({
    orgId: ORG.id,
    organizations: [],
    setOrgId: () => {},
    addOrganization: () => {},
    loading: false,
    error: "not permitted to list organizations",
  }),
}));

vi.mock("../lib/scope", () => ({
  useScopeSelection: () => ({
    workspaceId: "",
    projectId: "",
    datasourceId: "",
    workspaces: [],
    projects: [],
    datasources: [],
    bindings: [],
    visibleProjects: [],
    visibleDatasources: [],
    setWorkspaceId: () => {},
    setProjectId: () => {},
    setDatasourceId: () => {},
    refresh: () => {},
    loading: false,
    error: "no access to workspaces in this organization",
    ready: true,
  }),
}));

import { ScopePicker } from "./ScopePicker";

describe("a refused scope says so", () => {
  it("reports a refused organization list instead of an empty dropdown", () => {
    render(<ScopePicker />);

    expect(screen.getByText(/not permitted to list organizations/)).toBeInTheDocument();
  });

  it("keeps the reason the provider was given rather than a flat message", () => {
    render(<ScopePicker />);

    const status = screen.getByText(/Scope could not be loaded/);
    expect(status).toHaveTextContent("no access to workspaces in this organization");
  });
});
