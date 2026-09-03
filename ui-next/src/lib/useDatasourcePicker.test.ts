import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import type { DataSourceRead } from "./types";
import type { PageOf } from "./ui-types";

const fetchOrgDatasources =
  vi.fn<(organizationId: string, signal?: AbortSignal) => Promise<PageOf<DataSourceRead>>>();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    fetchOrgDatasources: (organizationId: string, signal?: AbortSignal) =>
      fetchOrgDatasources(organizationId, signal),
  };
});

const DATASOURCE: DataSourceRead = {
  id: "ds_1", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  project_id: "proj1", name: "snowflake_prod", connector_type: "SNOWFLAKE", dialect: "snowflake",
  environment: "PRODUCTION", credential_reference: "vault://x", status: "ACTIVE", capabilities: {},
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  fetchOrgDatasources.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useDatasourcePicker", () => {
  it("loads the org's datasources into {id, name} pairs", async () => {
    fetchOrgDatasources.mockResolvedValue({ items: [DATASOURCE], limit: 500, offset: 0, total: 1 });
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() => useDatasourcePicker("org1"));

    await waitFor(() => expect(result.current.datasources).toEqual([{ id: "ds_1", name: "snowflake_prod" }]));
    expect(fetchOrgDatasources).toHaveBeenCalledWith("org1", undefined);
    expect(result.current.error).toBeNull();
  });

  it("degrades to an empty list with an error message on failure, never throws", async () => {
    fetchOrgDatasources.mockRejectedValue(new Error("network down"));
    const { useDatasourcePicker } = await import("./useDatasourcePicker");
    const { result } = renderHook(() => useDatasourcePicker("org1"));

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.datasources).toEqual([]);
  });

  it("datasourceName resolves an id to its name, or null when unknown/absent", async () => {
    const { datasourceName } = await import("./useDatasourcePicker");
    const list = [{ id: "ds_1", name: "snowflake_prod" }];
    expect(datasourceName(list, "ds_1")).toBe("snowflake_prod");
    expect(datasourceName(list, "ds_missing")).toBeNull();
    expect(datasourceName(list, null)).toBeNull();
  });
});
