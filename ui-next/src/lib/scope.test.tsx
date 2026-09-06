import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import type { DataSourceRead, ProjectRead, SourceBindingRead, WorkspaceRead } from "./types";

/* ---------------------------------------------------------------------------
   F10/T12. Every test here is one of the review's four scope defects, stated
   as behaviour: old scope must not outlive its organization, a late response
   must not populate the view it no longer belongs to, persistence must not
   write across tenants, and writes must be inert until scope resolves.
--------------------------------------------------------------------------- */

const ORG_A = "org-a";
const ORG_B = "org-b";

function workspace(id: string, organizationId: string): WorkspaceRead {
  return {
    id,
    organization_id: organizationId,
    name: id,
    slug: id,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } as unknown as WorkspaceRead;
}

function project(id: string, organizationId: string): ProjectRead {
  return {
    id,
    organization_id: organizationId,
    line_of_business_id: "lob",
    data_domain_id: "dom",
    name: id,
    slug: id,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } as unknown as ProjectRead;
}

function datasource(id: string, projectId: string): DataSourceRead {
  return {
    id,
    project_id: projectId,
    name: id,
    status: "ACTIVE",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } as unknown as DataSourceRead;
}

function binding(workspaceId: string, datasourceId: string): SourceBindingRead {
  return {
    id: `${workspaceId}-${datasourceId}`,
    workspace_id: workspaceId,
    datasource_id: datasourceId,
    status: "ACTIVE",
    masking_profile: "NONE",
  } as unknown as SourceBindingRead;
}

/* One estate per organization, plus deliberate control over WHEN each
   organization's load resolves -- which is the only way to reproduce an
   out-of-order response. */
const ESTATE: Record<string, { workspaces: WorkspaceRead[]; projects: ProjectRead[]; datasources: DataSourceRead[] }> = {
  [ORG_A]: {
    workspaces: [workspace("ws-a", ORG_A)],
    projects: [project("proj-a", ORG_A)],
    datasources: [datasource("ds-a", "proj-a")],
  },
  [ORG_B]: {
    workspaces: [workspace("ws-b", ORG_B)],
    projects: [project("proj-b", ORG_B)],
    datasources: [datasource("ds-b", "proj-b")],
  },
};

const pending: Record<string, (() => void)[]> = {};
let gateOrgs = new Set<string>();

function gate(orgId: string): Promise<void> {
  if (!gateOrgs.has(orgId)) return Promise.resolve();
  return new Promise<void>((resolve) => {
    (pending[orgId] ??= []).push(resolve);
  });
}

function releaseOrg(orgId: string): void {
  const waiters = pending[orgId] ?? [];
  pending[orgId] = [];
  for (const resolve of waiters) resolve();
}

vi.mock("./api", () => ({
  listOrgWorkspaces: async (orgId: string) => {
    await gate(orgId);
    const items = ESTATE[orgId]?.workspaces ?? [];
    return { items, total: items.length, truncated: false, pagesFetched: 1 };
  },
  listOrgProjects: async (orgId: string) => {
    await gate(orgId);
    const items = ESTATE[orgId]?.projects ?? [];
    return { items, total: items.length, truncated: false, pagesFetched: 1 };
  },
  listOrgDatasources: async (orgId: string) => {
    await gate(orgId);
    const items = ESTATE[orgId]?.datasources ?? [];
    return { items, total: items.length, truncated: false, pagesFetched: 1 };
  },
  findOrgWorkspaceById: async () => null,
  findOrgProjectById: async () => null,
  findOrgDatasourceById: async () => null,
  fetchWorkspaceSourceBindings: async (workspaceId: string) => {
    const items =
      workspaceId === "ws-a"
        ? [binding("ws-a", "ds-a")]
        : workspaceId === "ws-b"
          ? [binding("ws-b", "ds-b")]
          : [];
    return { items, limit: items.length, offset: 0, total: items.length };
  },
}));

let currentOrg = ORG_A;
vi.mock("./org", () => ({
  useOrgId: () => currentOrg,
}));

import { ScopeProvider, useScopeSelection } from "./scope";

function Probe() {
  const scope = useScopeSelection();
  if (!scope) return null;
  return (
    <div>
      <span data-testid="ready">{String(scope.ready === true)}</span>
      <span data-testid="workspace">{scope.workspaceId}</span>
      <span data-testid="project">{scope.projectId}</span>
      <span data-testid="datasource">{scope.datasourceId}</span>
      <span data-testid="sources">{scope.datasources.map((item) => item.id).join(",")}</span>
      <button onClick={() => scope.setDatasourceId("ds-a")}>pick ds-a</button>
    </div>
  );
}

function renderScope() {
  return render(
    <ScopeProvider>
      <Probe />
    </ScopeProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  currentOrg = ORG_A;
  gateOrgs = new Set();
  for (const key of Object.keys(pending)) pending[key] = [];
});

afterEach(() => {
  localStorage.clear();
});

describe("scope is tied to its organization", () => {
  it("resolves workspace, project and source together, then reports ready", async () => {
    renderScope();
    await waitFor(() => expect(screen.getByTestId("ready")).toHaveTextContent("true"));
    expect(screen.getByTestId("workspace")).toHaveTextContent("ws-a");
    expect(screen.getByTestId("project")).toHaveTextContent("proj-a");
    expect(screen.getByTestId("datasource")).toHaveTextContent("ds-a");
  });

  it("empties the previous organization's scope immediately on a tenant change", async () => {
    const { rerender } = renderScope();
    await waitFor(() => expect(screen.getByTestId("datasource")).toHaveTextContent("ds-a"));

    // B's load is held open, so this asserts what is on screen DURING the
    // switch -- which is where the old implementation showed A's ids under
    // B's name.
    gateOrgs = new Set([ORG_B]);
    currentOrg = ORG_B;
    act(() => {
      rerender(
        <ScopeProvider>
          <Probe />
        </ScopeProvider>,
      );
    });

    expect(screen.getByTestId("ready")).toHaveTextContent("false");
    expect(screen.getByTestId("datasource")).toHaveTextContent("");
    expect(screen.getByTestId("sources")).toHaveTextContent("");

    await act(async () => {
      releaseOrg(ORG_B);
    });
    await waitFor(() => expect(screen.getByTestId("datasource")).toHaveTextContent("ds-b"));
  });

  it("discards a response that arrives for an organization the user has left", async () => {
    // A is held open. Switch to B, let B finish, then release A's stale
    // response: it must be dropped, not adopted.
    gateOrgs = new Set([ORG_A]);
    const { rerender } = renderScope();
    expect(screen.getByTestId("ready")).toHaveTextContent("false");

    currentOrg = ORG_B;
    act(() => {
      rerender(
        <ScopeProvider>
          <Probe />
        </ScopeProvider>,
      );
    });
    await waitFor(() => expect(screen.getByTestId("datasource")).toHaveTextContent("ds-b"));

    await act(async () => {
      releaseOrg(ORG_A);
    });
    expect(screen.getByTestId("datasource")).toHaveTextContent("ds-b");
    expect(screen.getByTestId("sources")).not.toHaveTextContent("ds-a");
  });

  it("never writes one organization's selection under another's storage key", async () => {
    const { rerender } = renderScope();
    await waitFor(() => expect(screen.getByTestId("datasource")).toHaveTextContent("ds-a"));
    expect(localStorage.getItem(`atlas.scope.${ORG_A}.datasource`)).toBe("ds-a");

    currentOrg = ORG_B;
    act(() => {
      rerender(
        <ScopeProvider>
          <Probe />
        </ScopeProvider>,
      );
    });
    await waitFor(() => expect(screen.getByTestId("datasource")).toHaveTextContent("ds-b"));

    expect(localStorage.getItem(`atlas.scope.${ORG_B}.datasource`)).toBe("ds-b");
    // The previous tenant's key keeps the previous tenant's value.
    expect(localStorage.getItem(`atlas.scope.${ORG_A}.datasource`)).toBe("ds-a");
  });

  it("ignores a write attempted before scope resolves", async () => {
    gateOrgs = new Set([ORG_A]);
    renderScope();
    expect(screen.getByTestId("ready")).toHaveTextContent("false");

    act(() => {
      screen.getByRole("button", { name: "pick ds-a" }).click();
    });
    expect(screen.getByTestId("datasource")).toHaveTextContent("");
    expect(localStorage.getItem(`atlas.scope.${ORG_A}.datasource`)).toBeNull();

    await act(async () => {
      releaseOrg(ORG_A);
    });
    await waitFor(() => expect(screen.getByTestId("ready")).toHaveTextContent("true"));
  });
});
