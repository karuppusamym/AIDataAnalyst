import { beforeEach, expect, it, vi } from "vitest";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("./transport", () => ({
  USE_FIXTURES: false, get, postJson: vi.fn(), requestBlob: vi.fn(), requestRawBody: vi.fn(),
}));
import { fetchColumnDocumentation, fetchModelImportChanges } from "./columnDocumentation";

beforeEach(() => get.mockReset());
const row = (id: string) => ({ id, batch_id: "b1" });

it("loads all column pages instead of silently stopping at a thousand", async () => {
  get.mockResolvedValueOnce({items: Array.from({length: 1000}, (_, i) => ({column_id: String(i), table_id: "t1"})), total: 1001});
  get.mockResolvedValueOnce({items: [{column_id: "last", table_id: "t1"}], total: 1001});
  expect(await fetchColumnDocumentation("t1")).toHaveLength(1001);
  expect(get).toHaveBeenNthCalledWith(2, "/v1/tables/t1/column-documentation?limit=1000&offset=1000", undefined);
});

it("does not accept a missing page or wrong-table column as a complete list", async () => {
  get.mockResolvedValueOnce({items: [], total: 1});
  await expect(fetchColumnDocumentation("t1")).rejects.toThrow("incomplete");
  get.mockResolvedValueOnce({items: [{column_id: "c1", table_id: "other"}], total: 1});
  await expect(fetchColumnDocumentation("t1")).rejects.toThrow("inconsistent");
});

it("loads every preview page, including rows after the first thousand", async () => {
  const first = Array.from({ length: 1000 }, (_, i) => row(String(i)));
  get.mockResolvedValueOnce({ items: first, total: 1001 });
  get.mockResolvedValueOnce({ items: [row("last")], total: 1001 });
  const signal = new AbortController().signal;
  const rows = await fetchModelImportChanges("b1", signal);
  expect(rows).toHaveLength(1001);
  expect(rows[1000]!.id).toBe("last");
  expect(get).toHaveBeenNthCalledWith(2, "/v1/model-imports/b1/changes?limit=1000&offset=1000", signal);
});

it("rejects an incomplete preview instead of reporting success", async () => {
  get.mockResolvedValueOnce({ items: [row("1")], total: 2 });
  get.mockResolvedValueOnce({ items: [], total: 2 });
  await expect(fetchModelImportChanges("b1")).rejects.toThrow("incomplete");
});

it("rejects inconsistent batch totals between pages", async () => {
  get.mockResolvedValueOnce({ items: [row("1")], total: 2 });
  get.mockResolvedValueOnce({ items: [row("2")], total: 3 });
  await expect(fetchModelImportChanges("b1")).rejects.toThrow("changed");
});

it.each([
  { items: [row("1"), row("1")], total: 2 },
  { items: [{ id: "1", batch_id: "another-source-batch" }], total: 1 },
])("rejects duplicated or wrong-batch rows", async page => {
  get.mockResolvedValue(page);
  await expect(fetchModelImportChanges("b1")).rejects.toThrow("inconsistent");
});

it("bounds the preview and explains oversized imports", async () => {
  get.mockResolvedValue({ items: [], total: 50001 });
  await expect(fetchModelImportChanges("b1")).rejects.toThrow("Split large workbooks");
  expect(get).toHaveBeenCalledTimes(1);
});

it("accepts an empty complete preview", async () => {
  get.mockResolvedValue({ items: [], total: 0 });
  await expect(fetchModelImportChanges("b1")).resolves.toEqual([]);
});
