import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, requestEventStream, streamedApiError } from "./http";

/* R11-MP06: reading Ask's server-sent events from a POST. */

function streamOf(chunks: string[], init: ResponseInit = { status: 200 }): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    ...init,
    headers: { "Content-Type": "text/event-stream" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("requestEventStream", () => {
  it("hands over each event in order, even when a block is split across chunks", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamOf([
        'event: stage\ndata: {"stage": "RECEIVED"}\n\nevent: sta',
        'ge\ndata: {"stage": "SCREENED"}\n\n',
        'event: result\ndata: {"agent_run_id": "r1"}\n\n',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const seen: Array<[string, unknown]> = [];

    await requestEventStream("/v1/x/stream", { question: "q" }, (event, data) =>
      seen.push([event, data]),
    );

    expect(seen).toEqual([
      ["stage", { stage: "RECEIVED" }],
      ["stage", { stage: "SCREENED" }],
      ["result", { agent_run_id: "r1" }],
    ]);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Accept).toBe("text/event-stream");
    expect(init.body).toBe(JSON.stringify({ question: "q" }));
  });

  it("decodes a refusal that arrives before the stream opens like any other", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "datasource not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const error = await requestEventStream("/v1/x/stream", {}, () => undefined).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).detail).toBe("datasource not found");
  });
});

describe("streamedApiError", () => {
  it("rebuilds the error the single-shot route would have thrown, structured detail included", async () => {
    const error = await streamedApiError(409, {
      code: "MISSING_TOOL_PARAMETERS",
      message: "approved tool requires parameters: region",
      required_parameters: ["region"],
      tool_version_id: "tv1",
    });
    expect(error.status).toBe(409);
    expect(error.detail).toContain("approved tool requires parameters");
    expect(error.details?.required_parameters).toEqual(["region"]);
  });
});
