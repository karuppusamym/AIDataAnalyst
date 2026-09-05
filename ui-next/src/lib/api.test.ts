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

/* ---------------------------------------------------------------------------
   P1-03: glossary api wrappers from `_api_append.ts`. Same pattern as
   above -- mock global fetch, assert URL/method/headers/body/error
   classification the wrappers promise.

   These wrappers now short-circuit to fixtures under the default
   `VITE_USE_FIXTURES` (they previously ignored the flag and always hit the
   network, which 401d in fixture mode). Since the whole point of this block
   is the wire contract, it pins the flag off. `vi.resetModules()` in the
   shared `beforeEach` runs after this hook, so each dynamic `import("./…")`
   below re-evaluates the module against the stubbed value.
--------------------------------------------------------------------------- */

beforeEach(() => {
  vi.stubEnv("VITE_USE_FIXTURES", "0");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("listGlossaryTerms", () => {
  it("GETs the org-scoped list route and forwards status/limit query params", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], limit: 50, offset: 0, total: 0 }));
    const { listGlossaryTerms } = await import("./_api_append");

    await listGlossaryTerms(ORG, { status: "APPROVED", limit: 50 });

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`/v1/organizations/${ORG}/glossary-terms?status=APPROVED&limit=50`);
    expect(init?.method).toBe("GET");
    const headers = init?.headers as Record<string, string>;
    expect(headers["Accept"]).toBe("application/json");
  });

  it("derives next_cursor from offset + limit < total, and omits it on the last page", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], limit: 10, offset: 0, total: 25 }));
    const { listGlossaryTerms } = await import("./_api_append");
    const first = await listGlossaryTerms(ORG, { limit: 10 });
    expect(first.next_cursor).toBe("10");

    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], limit: 10, offset: 20, total: 25 }));
    const last = await listGlossaryTerms(ORG, { limit: 10, cursor: "20" });
    expect(last.next_cursor).toBeUndefined();
  });

  it("classifies a 403 as an ApiError with the server detail", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "policy_denied" }, { status: 403 }));
    const { listGlossaryTerms, ApiError } = await import("./_api_append").then(async (m) => ({
      listGlossaryTerms: m.listGlossaryTerms,
      ApiError: (await import("./api")).ApiError,
    }));

    await expect(listGlossaryTerms(ORG)).rejects.toBeInstanceOf(ApiError);
  });
});

describe("createGlossaryTerm", () => {
  it("POSTs to the org-scoped create route with the term payload including business_node_id mapped to category_id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "ver_new", term_id: "term_new", status: "DRAFT" }, { status: 201 }),
    );
    const { createGlossaryTerm } = await import("./_api_append");

    await createGlossaryTerm(ORG, {
      term_key: "mrr",
      display_name: "Monthly Recurring Revenue",
      definition: "Recurring revenue normalized to a month.",
      business_node_id: "node_finance",
      synonyms: ["MRR"],
    });

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(`/v1/organizations/${ORG}/glossary-terms`);
    expect(init?.method).toBe("POST");
    const body = JSON.parse(init?.body as string);
    expect(body.term_key).toBe("mrr");
    expect(body.display_name).toBe("Monthly Recurring Revenue");
    expect(body.category_id).toBe("node_finance");
    expect(body.synonyms).toEqual(["MRR"]);
    // business_node_id itself must not leak onto the wire -- server does not know it.
    expect(body.business_node_id).toBeUndefined();
  });
});

describe("submitGlossaryTermVersion", () => {
  it("POSTs to /v1/glossary-term-versions/{id}/submit with an empty body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "gr_1", status: "PENDING" }, { status: 202 }));
    const { submitGlossaryTermVersion } = await import("./_api_append");

    await submitGlossaryTermVersion("ver_1");

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/glossary-term-versions/ver_1/submit");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({});
  });
});

describe("linkTermToTable", () => {
  it("POSTs to /v1/metadata/tables/{table_id}/glossary-links with {term_id} and an X-Link-Reason header", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "link_1", term_id: "term_1", table_id: "t_1" }, { status: 201 }),
    );
    const { linkTermToTable } = await import("./_api_append");

    await linkTermToTable(ORG, "t_1", "term_1", { reason: "matches finance policy" });

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/metadata/tables/t_1/glossary-links");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ term_id: "term_1" });
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-Link-Reason"]).toBe("matches finance policy");
  });
});

describe("unlinkTermFromTable", () => {
  it("DELETEs /v1/asset-term-links/{link_id} and resolves on 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const { unlinkTermFromTable } = await import("./_api_append");

    await expect(unlinkTermFromTable(ORG, "t_1", "link_1")).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/asset-term-links/link_1");
    expect(init?.method).toBe("DELETE");
  });
});

describe("listAssetTermLinks", () => {
  it("returns an empty page when no tableId is provided (no server route for org-wide)", async () => {
    const { listAssetTermLinks } = await import("./_api_append");
    const page = await listAssetTermLinks(ORG, {});
    expect(page.items).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("GETs /v1/metadata/tables/{id}/glossary-links when a tableId is given, filtering by termId client-side", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        items: [
          { id: "l1", term_id: "term_a", table_id: "t_1", link_type: "MANUAL" },
          { id: "l2", term_id: "term_b", table_id: "t_1", link_type: "INFERRED" },
        ],
        limit: 100,
        offset: 0,
        total: 2,
      }),
    );
    const { listAssetTermLinks } = await import("./_api_append");

    const page = await listAssetTermLinks(ORG, { tableId: "t_1", termId: "term_b" });

    const [url] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/metadata/tables/t_1/glossary-links?limit=100");
    expect(page.items.map((l) => l.id)).toEqual(["l2"]);
  });
});

describe("classifyGlossaryError", () => {
  it("maps 409 with 'only approved' to TERM_NOT_APPROVED_FOR_LINK", async () => {
    const { ApiError } = await import("./api");
    const { classifyGlossaryError } = await import("./_api_append");
    const err = new ApiError(409, "only approved glossary terms can be linked");
    expect(classifyGlossaryError(err).kind).toBe("TERM_NOT_APPROVED_FOR_LINK");
  });

  it("maps 409 with 'already exists' to TERM_KEY_TAKEN", async () => {
    const { ApiError } = await import("./api");
    const { classifyGlossaryError } = await import("./_api_append");
    expect(classifyGlossaryError(new ApiError(409, "glossary term key already exists")).kind).toBe(
      "TERM_KEY_TAKEN",
    );
  });

  it("maps 404/401/403/5xx/other to their own kinds", async () => {
    const { ApiError } = await import("./api");
    const { classifyGlossaryError } = await import("./_api_append");
    expect(classifyGlossaryError(new ApiError(404, "x")).kind).toBe("TERM_NOT_FOUND");
    expect(classifyGlossaryError(new ApiError(401, "x")).kind).toBe("UNAUTHORIZED");
    expect(classifyGlossaryError(new ApiError(403, "x")).kind).toBe("UNAUTHORIZED");
    expect(classifyGlossaryError(new ApiError(500, "x")).kind).toBe("SERVER_ERROR");
    expect(classifyGlossaryError(new ApiError(418, "x")).kind).toBe("UNKNOWN");
  });
});
