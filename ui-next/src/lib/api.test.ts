import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   P1-04: description-draft api.ts wrappers. Mocks global `fetch` so the
   assertions cover URL, method, headers, request body and error
   classification -- what the wrappers actually promise to callers.

   The rest of api.ts is tested indirectly through the screen tests
   (QualityScreen.test.tsx etc.); this file covers the three new functions
   because they are the P1-04 audit's whole surface area.
--------------------------------------------------------------------------- */

const originalFetch = globalThis.fetch;
const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  vi.resetModules();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    ...init,
  });
}

const ORG = "00000000-0000-0000-0000-000000000001";

describe("generateAssetDescriptionDrafts", () => {
  it("POSTs to the org-scoped generate route with {table_ids} and unwraps the Page shape", async () => {
    const draft = { id: "d1", table_id: "t1", table_name: "orders_raw", drafted_text: "…" };
    fetchMock.mockResolvedValue(
      jsonResponse({ items: [draft], limit: 100, offset: 0, total: 1 }),
    );
    const { generateAssetDescriptionDrafts } = await import("./api");

    const result = await generateAssetDescriptionDrafts(ORG, ["t1"]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`/v1/organizations/${ORG}/asset-description-drafts/generate`);
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["Accept"]).toBe("application/json");
    expect(JSON.parse(init?.body as string)).toEqual({ table_ids: ["t1"] });
    expect(result.drafts).toEqual([draft]);
    expect(result.total).toBe(1);
  });

  it("rejects a >100 batch synchronously without hitting the network", async () => {
    const { generateAssetDescriptionDrafts, ApiError } = await import("./api");
    const tooMany = Array.from({ length: 101 }, (_, i) => `t${i}`);

    await expect(generateAssetDescriptionDrafts(ORG, tooMany)).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an empty batch synchronously", async () => {
    const { generateAssetDescriptionDrafts, ApiError } = await import("./api");
    await expect(generateAssetDescriptionDrafts(ORG, [])).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("listAssetDescriptionDrafts", () => {
  it("GETs the org-scoped list route with the status query param when provided", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], limit: 200, offset: 0, total: 0 }));
    const { listAssetDescriptionDrafts } = await import("./api");

    await listAssetDescriptionDrafts(ORG, { status: "DRAFT", limit: 200 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(
      `/v1/organizations/${ORG}/asset-description-drafts?status=DRAFT&limit=200`,
    );
    expect(init?.method).toBeUndefined(); // GET
  });

  it("derives next_cursor from offset + limit < total, and omits it on the last page", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], limit: 10, offset: 0, total: 25 }),
    );
    const { listAssetDescriptionDrafts } = await import("./api");
    const first = await listAssetDescriptionDrafts(ORG, { limit: 10 });
    expect(first.next_cursor).toBe("10");

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], limit: 10, offset: 20, total: 25 }),
    );
    const last = await listAssetDescriptionDrafts(ORG, { limit: 10, cursor: "20" });
    expect(last.next_cursor).toBeUndefined();
  });
});

describe("submitAssetDescriptionDraft", () => {
  it("POSTs to /v1/asset-description-drafts/{id}/submit with an empty body and returns the review", async () => {
    const review = {
      id: "gr_1",
      organization_id: ORG,
      object_type: "ASSET_DESCRIPTION_DRAFT",
      object_id: "d1",
      requested_action: "PUBLISH",
      status: "PENDING",
      requested_by: "me",
      decided_by: null,
      decision_reason: null,
      decided_at: null,
      created_at: "2026-09-02T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
    };
    fetchMock.mockResolvedValue(jsonResponse(review, { status: 202 }));
    const { submitAssetDescriptionDraft } = await import("./api");

    const result = await submitAssetDescriptionDraft("d1");

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/asset-description-drafts/d1/submit");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({});
    expect(result).toEqual(review);
  });
});

describe("classifyDescriptionDraftError", () => {
  it("maps the 422 evidence-gate detail to DRAFT_BELOW_EVIDENCE_THRESHOLD", async () => {
    const { ApiError, classifyDescriptionDraftError } = await import("./api");
    const err = new ApiError(422, "draft carries too little evidence for independent review");
    const classified = classifyDescriptionDraftError(err);
    expect(classified.kind).toBe("DRAFT_BELOW_EVIDENCE_THRESHOLD");
    expect(classified.status).toBe(422);
  });

  it("maps 404/409/401/403/5xx to their own kinds", async () => {
    const { ApiError, classifyDescriptionDraftError } = await import("./api");
    expect(classifyDescriptionDraftError(new ApiError(404, "not found")).kind).toBe("DRAFT_NOT_FOUND");
    expect(classifyDescriptionDraftError(new ApiError(409, "already submitted")).kind).toBe("DRAFT_NOT_SUBMITTABLE");
    expect(classifyDescriptionDraftError(new ApiError(401, "no")).kind).toBe("UNAUTHORIZED");
    expect(classifyDescriptionDraftError(new ApiError(403, "no")).kind).toBe("UNAUTHORIZED");
    expect(classifyDescriptionDraftError(new ApiError(500, "boom")).kind).toBe("SERVER_ERROR");
    expect(classifyDescriptionDraftError(new ApiError(418, "teapot")).kind).toBe("UNKNOWN");
  });
});
