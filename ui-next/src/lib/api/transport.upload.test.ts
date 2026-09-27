import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { observeRequests, request, setHeaderProvider } from "../http";
import { requestRawBody, requestHeaders } from "./transport";

const fetchMock = vi.fn();
const observe = vi.fn();
let unsubscribe: () => void;

beforeEach(() => {
  fetchMock.mockReset();
  observe.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setHeaderProvider(() => ({ Authorization: "Bearer test-token" }));
  unsubscribe = observeRequests(observe);
});

afterEach(() => {
  unsubscribe();
  setHeaderProvider(requestHeaders);
  vi.unstubAllGlobals();
});

describe("upload outcomes", () => {
  it("sends the exact workbook bytes and identity, and reports success once", async () => {
    const file = new File([new Uint8Array([0x50, 0x4b, 0, 255])], "model.xlsx");
    fetchMock.mockResolvedValue(new Response('{"batch_id":"batch-1"}', { status: 201 }));
    await expect(requestRawBody("POST", "/upload", file, "application/octet-stream"))
      .resolves.toEqual({ batch_id: "batch-1" });
    expect(fetchMock).toHaveBeenCalledWith("/upload", expect.objectContaining({
      method: "POST", body: file, credentials: "same-origin",
      headers: expect.objectContaining({
        Authorization: "Bearer test-token", "Content-Type": "application/octet-stream",
      }),
    }));
    expect(observe).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ ok: true, status: 201 }));
  });

  it("reports a refusal with the server's reason and correlation id", async () => {
    fetchMock.mockResolvedValue(new Response('{"detail":"upload refused"}', {
      status: 403, headers: { "X-Correlation-Id": "upload-reference" },
    }));
    await expect(requestRawBody("POST", "/upload", new Blob(), "application/octet-stream"))
      .rejects.toMatchObject({ status: 403, detail: "upload refused", correlationId: "upload-reference" });
    expect(observe).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      ok: false, status: 403, error: expect.objectContaining({ correlationId: "upload-reference" }),
    }));
  });

  it("reports network failure so the shell can show a lost connection", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(requestRawBody("POST", "/upload", new Blob(), "application/octet-stream"))
      .rejects.toMatchObject({ status: 0, code: "NETWORK_UNREACHABLE" });
    expect(observe).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ ok: false, status: 0 }));
  });

  it("passes cancellation through without reporting an outage", async () => {
    const controller = new AbortController();
    const abort = new DOMException("Cancelled", "AbortError");
    fetchMock.mockRejectedValue(abort);
    await expect(requestRawBody("POST", "/upload", new Blob(), "application/octet-stream", controller.signal))
      .rejects.toBe(abort);
    expect(fetchMock.mock.calls[0]![1].signal).toBe(controller.signal);
    expect(observe).not.toHaveBeenCalled();
  });

  it("rejects conflicting body encodings before sending anything", async () => {
    await expect(request("POST", "/upload", { body: {}, rawBody: new Blob() }))
      .rejects.toThrow("both JSON and raw bodies");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(observe).not.toHaveBeenCalled();
  });
});
