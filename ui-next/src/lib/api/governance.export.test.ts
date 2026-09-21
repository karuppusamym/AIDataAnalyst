import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../http";

/* ---------------------------------------------------------------------------
   `downloadAuditEventsExport` -- the audit ledger as one file (R11-AUD08).

   The screen's own test mocks this function and asserts what the reader is told.
   These pin the half the screen cannot see: the request that goes out, the
   headers that are read off the answer, and that a file reaches the disk ONLY
   when the server said yes.

   THE ONE THAT MATTERS MOST is `truncated`. `audit_export_api.py` puts the row
   cap in a header because "an export that silently stops at a row cap is worse
   than one that refuses". If this function defaulted the flag to `false` when
   the header did not arrive (a proxy that strips `X-*` headers), the screen
   would tell an auditor a cut file was complete. It is `null` -- unknown --
   and the screen has a sentence for that.
--------------------------------------------------------------------------- */

const { requestBlob, mode } = vi.hoisted(() => ({
  requestBlob: vi.fn<(path: string, options?: { signal?: AbortSignal }) => Promise<unknown>>(),
  mode: { fixtures: false },
}));

vi.mock("./transport", () => ({
  get: vi.fn(),
  postJson: vi.fn(),
  demoOr: vi.fn(),
  requestBlob: (path: string, options?: { signal?: AbortSignal }) => requestBlob(path, options),
  get USE_FIXTURES() {
    return mode.fixtures;
  },
}));

import { downloadAuditEventsExport } from "./governance";

const ORG = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87";
const SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
const answer = (headers: Record<string, string>) => ({
  blob: new Blob(['{"id":1}\n'], { type: "application/x-ndjson" }),
  response: new Response(null, { headers }),
});
const FULL_HEADERS = {
  "Content-Disposition": `attachment; filename="audit-events-${ORG}-20260920T101500Z.jsonl"`,
  "X-Artifact-SHA256": SHA,
  "X-Export-Row-Count": "1234",
  "X-Export-Truncated": "false",
  "X-Export-Row-Limit": "50000",
};

let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;
let click: ReturnType<typeof vi.spyOn>;
let saved: string[];

beforeEach(() => {
  mode.fixtures = false;
  requestBlob.mockReset();
  saved = [];
  createObjectURL = vi.fn().mockReturnValue("blob:audit-export");
  revokeObjectURL = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true });
  click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("downloadAuditEventsExport", () => {
  it("requests exactly the filters it was given, through the shared transport, and saves under the server's filename", async () => {
    requestBlob.mockResolvedValue(answer(FULL_HEADERS));
    const controller = new AbortController();

    const result = await downloadAuditEventsExport(
      {
        organizationId: ORG,
        action: "governance_review.decide",
        resourceType: "GOVERNANCE_REVIEW",
        correlationId: "corr_9f21a0",
        since: "2026-09-01T00:00:00.000Z",
        until: "2026-09-15T00:00:00+05:30",
      },
      controller.signal,
    );

    expect(requestBlob).toHaveBeenCalledTimes(1);
    const [path, options] = requestBlob.mock.calls[0]!;
    expect(path.startsWith(`/v1/organizations/${ORG}/audit-events/export.jsonl?`)).toBe(true);
    const params = new URL(path, "http://x").searchParams;
    expect(Object.fromEntries(params)).toEqual({
      action: "governance_review.decide",
      resource_type: "GOVERNANCE_REVIEW",
      correlation_id: "corr_9f21a0",
      since: "2026-09-01T00:00:00.000Z",
      until: "2026-09-15T00:00:00+05:30",
    });
    // A file, not a page: no paging parameters exist on this route.
    expect(params.has("limit")).toBe(false);
    expect(params.has("offset")).toBe(false);
    expect(options?.signal).toBe(controller.signal);

    expect(saved).toEqual([`audit-events-${ORG}-20260920T101500Z.jsonl`]);
    expect(click).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(result).toEqual({
      filename: `audit-events-${ORG}-20260920T101500Z.jsonl`,
      rowCount: 1234, truncated: false, rowLimit: 50000, sha256: SHA,
    });
  });

  it("sends no query string when nothing is filtered", async () => {
    requestBlob.mockResolvedValue(answer(FULL_HEADERS));

    await downloadAuditEventsExport({ organizationId: ORG });

    expect(requestBlob.mock.calls[0]![0]).toBe(`/v1/organizations/${ORG}/audit-events/export.jsonl`);
  });

  it("reads a truncated export as truncated", async () => {
    requestBlob.mockResolvedValue(
      answer({ ...FULL_HEADERS, "X-Export-Truncated": "true", "X-Export-Row-Count": "50000" }),
    );

    const result = await downloadAuditEventsExport({ organizationId: ORG });

    expect(result.truncated).toBe(true);
    expect(result.rowCount).toBe(50000);
    expect(result.rowLimit).toBe(50000);
  });

  it("reports what it could not read as unknown, never as a clean bill of health", async () => {
    // A proxy that strips `X-*` headers leaves only the disposition -- or nothing.
    requestBlob.mockResolvedValue(answer({}));

    const result = await downloadAuditEventsExport({ organizationId: ORG });

    expect(result).toEqual({
      filename: `audit-events-${ORG}.jsonl`,
      rowCount: null, truncated: null, rowLimit: null, sha256: null,
    });
    // It still delivered the bytes it did get: the screen decides what to say about them.
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("treats a malformed count as unknown rather than as a number", async () => {
    requestBlob.mockResolvedValue(answer({ ...FULL_HEADERS, "X-Export-Row-Count": "many", "X-Export-Row-Limit": "" }));

    const result = await downloadAuditEventsExport({ organizationId: ORG });

    expect(result.rowCount).toBeNull();
    expect(result.rowLimit).toBeNull();
  });

  it("saves nothing when the server refuses, and throws the refusal as it arrived", async () => {
    // The EXPORT policy gate answers 403 with a bare reason code.
    requestBlob.mockRejectedValue(new ApiError(403, "EXPORT_DENIED_BY_POLICY"));

    await expect(downloadAuditEventsExport({ organizationId: ORG })).rejects.toMatchObject({
      status: 403,
      detail: "EXPORT_DENIED_BY_POLICY",
    });
    expect(click).not.toHaveBeenCalled();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("refuses a bound without a timezone before any request -- the server would 422 it", async () => {
    await expect(
      downloadAuditEventsExport({ organizationId: ORG, since: "2026-09-01T00:00" }),
    ).rejects.toThrow(/since must be a timezone-aware ISO datetime/);
    await expect(
      downloadAuditEventsExport({ organizationId: ORG, until: "2026-09-01 10:00" }),
    ).rejects.toThrow(/until must be a timezone-aware ISO datetime/);
    expect(requestBlob).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
  });

  it("says so, and issues no request, under demo data -- there is no server to compose the file", async () => {
    mode.fixtures = true;

    await expect(downloadAuditEventsExport({ organizationId: ORG })).rejects.toThrow(
      "The audit export is composed by the server.",
    );
    expect(requestBlob).not.toHaveBeenCalled();
    expect(click).not.toHaveBeenCalled();
  });
});
