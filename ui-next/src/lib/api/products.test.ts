import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../http";

/* ---------------------------------------------------------------------------
   `fetchContextProductChangesSincePublished` (R11-FP12) is the one place a GraphQL
   answer becomes "this version is stale / is not / could not be checked".

   The property under test is the third one. A GraphQL refusal does not fail the HTTP
   request: it arrives as HTTP 200 with `data.contextProductCoverage === null` beside an
   `errors` entry. A client that reads the data and ignores the errors sees an empty
   list -- "nothing changed" -- for a version it was not allowed to read, or that no
   longer exists. So every answer that is not a well-formed one rejects, and the screen
   reports "could not check" instead of a clean bill of health.
--------------------------------------------------------------------------- */

const postJson = vi.fn();

vi.mock("./transport", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./transport")>();
  return {
    ...actual,
    postJson: (...args: unknown[]) => postJson(...args),
    // Pinned to the live arm: the request is the one a deployed client sends.
    demoOr: (_demo: unknown, live: () => Promise<unknown>) => live(),
  };
});

async function load() {
  return import("./products");
}

const answer = (page: unknown) => ({ data: { contextProductCoverage: { changedSincePublished: page } } });

beforeEach(() => {
  postJson.mockReset();
  vi.resetModules();
});

describe("fetchContextProductChangesSincePublished", () => {
  it("asks one named operation for one version, one page, and maps each entry", async () => {
    postJson.mockResolvedValue(
      answer({
        totalCount: 2,
        nodes: [
          { subjectKind: "VIEW", subjectId: "v-1", change: "DEFINITION_CHANGED", changeClass: "STRUCTURAL" },
          { subjectKind: "COLUMN", subjectId: "c-1", change: "MEANING_RETIRED", changeClass: null },
        ],
      }),
    );
    const { fetchContextProductChangesSincePublished } = await load();

    const read = await fetchContextProductChangesSincePublished("ver-1");

    expect(read).toEqual({
      total: 2,
      changes: [
        { subjectKind: "VIEW", subjectId: "v-1", change: "DEFINITION_CHANGED", changeClass: "STRUCTURAL" },
        { subjectKind: "COLUMN", subjectId: "c-1", change: "MEANING_RETIRED", changeClass: null },
      ],
    });
    expect(postJson).toHaveBeenCalledTimes(1);
    const [path, body] = postJson.mock.calls[0]!;
    expect(path).toBe("/graphql");
    expect(body.operationName).toBe("ContextProductChangesSincePublished");
    expect(body.variables).toEqual({ versionId: "ver-1", first: 20 });
    // The operation name is the one the document declares (the endpoint refuses a mismatch), and the
    // document reads only this section: not the routines, views or freshness a whole coverage read holds.
    expect(body.query).toContain("query ContextProductChangesSincePublished(");
    expect(body.query).toContain("contextProductCoverage(versionId: $versionId)");
    expect(body.query).toContain("changedSincePublished(first: $first)");
    expect(body.query).not.toMatch(/\b(routines|views|meaning|sourceFreshness)\b/);
  });

  it("reads an empty first page as nothing having moved", async () => {
    postJson.mockResolvedValue(answer({ totalCount: 0, nodes: [] }));
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).resolves.toEqual({ total: 0, changes: [] });
  });

  it("reports the server's total when the first page is only part of it", async () => {
    const nodes = Array.from({ length: 20 }, (_, i) => ({
      subjectKind: "ROUTINE",
      subjectId: `r-${i}`,
      change: "DEFINITION_CHANGED",
      changeClass: "LITERAL_ONLY",
    }));
    postJson.mockResolvedValue(answer({ totalCount: 31, nodes }));
    const { fetchContextProductChangesSincePublished } = await load();

    const read = await fetchContextProductChangesSincePublished("ver-1");

    expect(read.total).toBe(31);
    expect(read.changes).toHaveLength(20);
  });

  it("rejects a refusal instead of reading it as nothing having moved", async () => {
    // Exactly what the endpoint sends for a version the caller may not read: HTTP 200, the field null.
    postJson.mockResolvedValue({
      data: { contextProductCoverage: null },
      errors: [{ message: "NOT_FOUND", path: ["contextProductCoverage"], extensions: { code: "NOT_FOUND" } }],
    });
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).rejects.toThrow(
      "The coverage read was refused: NOT_FOUND.",
    );
  });

  it("names the refusal's reason code when there is one", async () => {
    postJson.mockResolvedValue({
      data: { contextProductCoverage: null },
      errors: [{ message: "FORBIDDEN", extensions: { code: "FORBIDDEN", reason: "ROLE_REQUIRED" } }],
    });
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).rejects.toThrow(
      "The coverage read was refused: FORBIDDEN (ROLE_REQUIRED).",
    );
  });

  it("rejects when errors accompany data that looks complete", async () => {
    // A partial answer next to an error is still not an answer to trust.
    postJson.mockResolvedValue({
      ...answer({ totalCount: 0, nodes: [] }),
      errors: [{ message: "INTERNAL_ERROR", extensions: { code: "INTERNAL_ERROR" } }],
    });
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).rejects.toThrow(/INTERNAL_ERROR/);
  });

  it.each([
    ["no data at all", {}],
    ["null data", { data: null }],
    ["a null coverage object", { data: { contextProductCoverage: null } }],
    ["a null section", answer(null)],
    ["a section with no nodes", answer({ totalCount: 0 })],
    ["nodes that are not a list", answer({ totalCount: 0, nodes: "none" })],
  ])("rejects rather than reading %s as nothing having moved", async (_case, response) => {
    postJson.mockResolvedValue(response);
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).rejects.toThrow(
      "The coverage read returned no answer, so this version was not checked.",
    );
  });

  it("rejects an entry it cannot read rather than dropping it", async () => {
    // Dropping it would shorten the list and could turn one stale reason into "nothing moved".
    postJson.mockResolvedValue(
      answer({ totalCount: 1, nodes: [{ subjectKind: "VIEW", change: "DEFINITION_CHANGED", changeClass: null }] }),
    );
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).rejects.toThrow(
      "The coverage read returned an entry that could not be read, so this version was not checked.",
    );
  });

  it("lets a transport failure through untouched", async () => {
    postJson.mockRejectedValue(new ApiError(502, "Bad Gateway"));
    const { fetchContextProductChangesSincePublished } = await load();

    await expect(fetchContextProductChangesSincePublished("ver-1")).rejects.toMatchObject({
      status: 502,
      detail: "Bad Gateway",
    });
  });
});
