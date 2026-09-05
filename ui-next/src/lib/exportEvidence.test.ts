import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.resetModules(); });
it("exports through authenticated fetch and downloads only a successful response", async () => {
  vi.stubEnv("VITE_USE_FIXTURES", "0");
  vi.resetModules();
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ table_name: "accounts", items: [] })));
  vi.stubGlobal("fetch", fetchMock);
  const createUrl = vi.fn().mockReturnValue("blob:evidence");
  Object.defineProperty(URL, "createObjectURL", { value: createUrl, configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const { exportAssetEvidence } = await import("./api");
  await exportAssetEvidence("table-1");
  expect(fetchMock).toHaveBeenCalledWith("/v1/metadata/tables/table-1/evidence/export", expect.objectContaining({ headers: expect.objectContaining({ "X-Principal-Id": "local-ui-admin", "X-Organization-Id": expect.any(String) }) }));
  expect(click).toHaveBeenCalledOnce();
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "Forbidden" }), { status: 403 }));
  await expect(exportAssetEvidence("table-1")).rejects.toThrow("Forbidden");
  expect(click).toHaveBeenCalledOnce();
});
