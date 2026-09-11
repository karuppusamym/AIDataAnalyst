import { describe, expect, it, vi } from "vitest";
import {
  concatBytes,
  connectWorkbookHost,
  fileNameFrom,
  toBase64,
  type ExcelLike,
  type OfficeLike,
} from "./officeHost";

/* The Office adapter against a fake runtime: the file arrives in slices, in
   order, and the handle is closed whether reading succeeds or not -- Office
   keeps two open per document, so a leaked one breaks the next save. */

const OK = "succeeded";

function fakeOffice(slices: number[][], failAt: number | null = null) {
  const closeAsync = vi.fn();
  const office: OfficeLike = {
    onReady: async () => ({ host: "Excel" }),
    AsyncResultStatus: { Succeeded: OK },
    FileType: { Compressed: "compressed" },
    EventType: { DialogMessageReceived: "msg", DialogEventReceived: "evt" },
    context: {
      document: {
        url: "C:\\Users\\steward\\warehouse%20model.xlsx",
        getFileAsync: (_type, _options, callback) =>
          callback({
            status: OK,
            value: {
              sliceCount: slices.length,
              closeAsync,
              getSliceAsync: (index, sliceCallback) =>
                sliceCallback(
                  index === failAt
                    ? { status: "failed", value: { data: [] }, error: { message: "gone" } }
                    : { status: OK, value: { data: slices[index] ?? [] } },
                ),
            },
          }),
      },
      ui: { displayDialogAsync: vi.fn() },
    },
  };
  return { office, closeAsync };
}

const excel: ExcelLike = { run: vi.fn(), createWorkbook: vi.fn() };

describe("connectWorkbookHost", () => {
  it("is not in Excel when Office never loaded", async () => {
    const host = await connectWorkbookHost({});
    expect(host.inExcel).toBe(false);
    await expect(host.readWorkbookFile()).rejects.toThrow(/runs inside Excel/);
  });

  it("is not in Excel when Office loaded in another host", async () => {
    const { office } = fakeOffice([]);
    const host = await connectWorkbookHost({
      Office: { ...office, onReady: async () => ({ host: "Word" }) },
      Excel: excel,
    });
    expect(host.inExcel).toBe(false);
  });

  it("assembles the workbook from its slices, in order, and closes the file", async () => {
    const { office, closeAsync } = fakeOffice([[1, 2], [3], [4, 5, 6]]);
    const host = await connectWorkbookHost({ Office: office, Excel: excel });
    expect(Array.from(await host.readWorkbookFile())).toEqual([1, 2, 3, 4, 5, 6]);
    expect(closeAsync).toHaveBeenCalledTimes(1);
  });

  it("closes the file when a slice cannot be read", async () => {
    const { office, closeAsync } = fakeOffice([[1], [2], [3]], 1);
    const host = await connectWorkbookHost({ Office: office, Excel: excel });
    await expect(host.readWorkbookFile()).rejects.toThrow("gone");
    expect(closeAsync).toHaveBeenCalledTimes(1);
  });

  it("names the upload after the open document", async () => {
    const { office } = fakeOffice([]);
    const host = await connectWorkbookHost({ Office: office, Excel: excel });
    expect(host.fileName()).toBe("warehouse model.xlsx");
  });
});

describe("byte helpers", () => {
  it("concatenates and base64-encodes without losing bytes", () => {
    const bytes = concatBytes([new Uint8Array([0, 255]), new Uint8Array([128])]);
    expect(Array.from(bytes)).toEqual([0, 255, 128]);
    expect(atob(toBase64(bytes)).split("").map((c) => c.charCodeAt(0))).toEqual([0, 255, 128]);
  });

  it("falls back to a usable filename for a new, unsaved workbook", () => {
    expect(fileNameFrom("")).toBe("workbook.xlsx");
    expect(fileNameFrom(null)).toBe("workbook.xlsx");
    expect(fileNameFrom("https://tenant.sharepoint.com/Model%20v2")).toBe("Model v2.xlsx");
  });
});
