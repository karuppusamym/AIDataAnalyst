import { describe, expect, it } from "vitest";

import { ApiError, decodeError } from "./http";

/* ---------------------------------------------------------------------------
   The regression these guard is F14: the per-verb decoders in `api.ts`
   understood only FastAPI's `{"detail": ...}`, so the backend's own
   `{"error": {code, message, correlation_id}}` envelope and its
   `X-Correlation-Id` header were thrown away and every failure reached the
   screen as "500 Internal Server Error" -- with nothing support could use to
   find the request in the logs.
--------------------------------------------------------------------------- */

function errorResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("decodeError", () => {
  it("keeps the structured code, message and correlation id", async () => {
    const error = await decodeError(
      errorResponse(500, {
        error: {
          code: "INTERNAL_SERVER_ERROR",
          message: "an unexpected error occurred",
          correlation_id: "corr-123",
        },
      }),
    );
    expect(error.status).toBe(500);
    expect(error.code).toBe("INTERNAL_SERVER_ERROR");
    expect(error.detail).toBe("an unexpected error occurred");
    expect(error.correlationId).toBe("corr-123");
    expect(error.supportReference).toBe("corr-123");
  });

  it("falls back to the correlation header when the body carries no id", async () => {
    const error = await decodeError(
      errorResponse(403, { detail: "policy_denied" }, { "X-Correlation-Id": "corr-hdr" }),
    );
    expect(error.detail).toBe("policy_denied");
    expect(error.correlationId).toBe("corr-hdr");
  });

  it("expands 422 validation errors into per-field messages", async () => {
    const error = await decodeError(
      errorResponse(422, {
        detail: [
          { loc: ["body", "name"], msg: "field required" },
          { loc: ["body", "limit"], msg: "must be positive" },
        ],
      }),
    );
    expect(error.fieldErrors).toEqual([
      { field: "body.name", message: "field required" },
      { field: "body.limit", message: "must be positive" },
    ]);
    expect(error.detail).toContain("body.name: field required");
  });

  it("survives a non-JSON error body without losing the status", async () => {
    const res = new Response("<html>502 Bad Gateway</html>", {
      status: 502,
      headers: { "Content-Type": "text/html", "X-Correlation-Id": "corr-proxy" },
    });
    const error = await decodeError(res);
    expect(error.status).toBe(502);
    expect(error.detail).toContain("502");
    expect(error.correlationId).toBe("corr-proxy");
  });
});

describe("ApiError", () => {
  it("keeps the two-argument shape existing call sites construct", () => {
    const error = new ApiError(403, "NO_BINDING_FOR_DATASOURCE");
    expect(error).toBeInstanceOf(Error);
    expect(error.status).toBe(403);
    expect(error.detail).toBe("NO_BINDING_FOR_DATASOURCE");
    expect(error.message).toBe("NO_BINDING_FOR_DATASOURCE");
    expect(error.isForbidden).toBe(true);
  });

  it("classifies the statuses the shell reacts to", () => {
    expect(new ApiError(401, "x").isUnauthenticated).toBe(true);
    expect(new ApiError(409, "x").isConflict).toBe(true);
    expect(new ApiError(403, "x").isForbidden).toBe(true);
  });

  it("marks transient failures retryable and caller errors not", () => {
    expect(new ApiError(503, "x").retryable).toBe(true);
    expect(new ApiError(429, "x").retryable).toBe(true);
    // A governance decision that lost a race must not be resubmitted blindly.
    expect(new ApiError(409, "x").retryable).toBe(false);
    expect(new ApiError(400, "x").retryable).toBe(false);
    expect(new ApiError(403, "x").retryable).toBe(false);
  });
});
