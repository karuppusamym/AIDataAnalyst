import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import type { DataSourceRead } from "./types";

/* ---------------------------------------------------------------------------
   The one source picker (F15 · R11-D7).

   `api/identity.test.ts` proves what reaches the wire. This proves the hook
   hands it the right question: the screen's search term and its selected id,
   and -- for a scope-reach picker -- that the server's answer is still cut
   down to what the active workspace reaches. A search that widened a picker
   past its workspace bindings would be a way around scope, not a search.
--------------------------------------------------------------------------- */

const listOrgDatasources = vi.fn();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    listOrgDatasources: (organizationId: string, signal?: AbortSignal, options?: unknown) =>
      listOrgDatasources(organizationId, signal, options),
  };
});

/* The scope layer is stubbed rather than provided, so a test can put the hook
 * in the one state that matters here -- a workspace whose bindings reach some
 * of what a search returns -- without also driving four estate loads. */
let scope: unknown = null;
vi.mock("./scope", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./scope")>();
  return { ...actual, useScopeSelection: () => scope };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const OTHER: DataSourceRead = { ...DATASOURCE, id: "ds_2", name: "oracle_core", project_id: "proj2" };

function answer(items: DataSourceRead[], extra: { total?: number; truncated?: boolean } = {}) {
  return {
    items,
    total: extra.total ?? items.length,
    truncated: extra.truncated ?? false,
    pagesFetched: 1,
  };
}

beforeEach(() => {
  listOrgDatasources.mockReset();
  scope = null;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useDatasourcePicker", () => {
  it("loads the org's datasources, with the server's own count", async () => {
    listOrgDatasources.mockResolvedValue(answer([DATASOURCE], { total: 4812, truncated: true }));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() => useDatasourcePicker("org1"));

    await waitFor(() => expect(result.current.datasources).toEqual([DATASOURCE]));
    expect(listOrgDatasources).toHaveBeenCalledWith("org1", expect.anything(), {
      search: "",
      selectedId: null,
    });
    expect(result.current.error).toBeNull();
    // The fleet count is the server's, not the length of what was loaded.
    expect(result.current.total).toBe(4812);
    expect(result.current.truncated).toBe(true);
  });

  it("asks the server for a selected id, so one past the first page still resolves", async () => {
    listOrgDatasources.mockResolvedValue(answer([OTHER, DATASOURCE], { total: 3000, truncated: true }));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() =>
      useDatasourcePicker("org1", { reach: "organization", selectedId: "ds_1" }),
    );

    await waitFor(() => expect(result.current.datasources).toHaveLength(2));
    expect(listOrgDatasources).toHaveBeenCalledWith("org1", expect.anything(), {
      search: "",
      selectedId: "ds_1",
    });
    // The selected row is present even though the list is a prefix of the
    // fleet -- which is the whole point: a `<select>` missing its own value
    // renders some other source and reports the wrong scope.
    expect(result.current.datasources.map((item) => item.id)).toContain("ds_1");
  });

  it("re-asks the server when the search changes, rather than filtering what it holds", async () => {
    listOrgDatasources.mockResolvedValue(answer([DATASOURCE]));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { rerender } = renderHook(
      ({ search }: { search: string }) =>
        useDatasourcePicker("org1", { reach: "organization", search }),
      { initialProps: { search: "" } },
    );

    await waitFor(() => expect(listOrgDatasources).toHaveBeenCalledTimes(1));
    rerender({ search: "oracle" });

    await waitFor(() =>
      expect(listOrgDatasources).toHaveBeenLastCalledWith("org1", expect.anything(), {
        search: "oracle",
        selectedId: null,
      }),
    );
  });

  it("degrades to an empty list and reports the server's reason, never throws", async () => {
    listOrgDatasources.mockRejectedValue(new Error("network down"));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() => useDatasourcePicker("org1"));

    await waitFor(() => expect(result.current.error).toBe("network down"));
    expect(result.current.datasources).toEqual([]);
  });

  it("does not fetch while disabled, so an on-demand picker stays on-demand", async () => {
    listOrgDatasources.mockResolvedValue(answer([DATASOURCE]));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useDatasourcePicker("org1", { reach: "organization", enabled }),
      { initialProps: { enabled: false } },
    );

    expect(listOrgDatasources).not.toHaveBeenCalled();
    expect(result.current.datasources).toEqual([]);

    rerender({ enabled: true });
    await waitFor(() => expect(listOrgDatasources).toHaveBeenCalledTimes(1));
  });

  it("reads scope without a request when there is nothing to search for", async () => {
    scope = {
      workspaceId: "ws1", projectId: "proj1", bindings: [],
      visibleDatasources: [DATASOURCE], datasourceId: "ds_1",
      error: null, loading: false,
      counts: { datasources: { loaded: 1, total: 1, truncated: false } },
    };
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() => useDatasourcePicker("org1"));

    expect(result.current.datasources).toEqual([DATASOURCE]);
    expect(result.current.preferredDatasourceId).toBe("ds_1");
    expect(listOrgDatasources).not.toHaveBeenCalled();
  });

  it("narrows a searched scope picker to what the workspace reaches", async () => {
    // The server matched both on name; only one is reachable through an
    // ACTIVE binding. Search must not be a way past the workspace.
    scope = {
      workspaceId: "ws1", projectId: "", visibleDatasources: [DATASOURCE], datasourceId: "ds_1",
      error: null, loading: false,
      bindings: [{ datasource_id: "ds_1", status: "ACTIVE" }],
    };
    listOrgDatasources.mockResolvedValue(answer([DATASOURCE, OTHER]));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() => useDatasourcePicker("org1", { search: "o" }));

    await waitFor(() => expect(result.current.datasources).toEqual([DATASOURCE]));
  });

  it("datasourceName resolves an id to its name, or null when unknown/absent", async () => {
    const { datasourceName } = await import("./useDatasourcePicker");
    const list = [{ id: "ds_1", name: "snowflake_prod" }];
    expect(datasourceName(list, "ds_1")).toBe("snowflake_prod");
    expect(datasourceName(list, "ds_missing")).toBeNull();
    expect(datasourceName(list, null)).toBeNull();
  });
});
