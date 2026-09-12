import { describe, expect, it } from "vitest";

import { ApiError } from "../http";
import { classifyAgentAskError } from "./agents";

/* ---------------------------------------------------------------------------
   `classifyAgentAskError` exists to tell refusals apart from failures, because
   the reader's next action differs: a refusal means ask for access, a failure
   means retry or call support.

   R11-B11's browser journey found the one status it did not handle. A 403 fell
   through to UNKNOWN, and Ask rendered "The question could not be answered" --
   which reads as a fault in the platform to someone who simply may not read
   that datasource, and sends them to the wrong place.
--------------------------------------------------------------------------- */

describe("classifyAgentAskError", () => {
  it("names a 403 as a refusal rather than an unknown failure", () => {
    const classified = classifyAgentAskError(new ApiError(403, "DENIED_BY_POLICY"));

    expect(classified.kind).toBe("NOT_AUTHORIZED");
    expect(classified.detail).toBe("DENIED_BY_POLICY");
  });

  it("still maps the statuses it always did", () => {
    expect(classifyAgentAskError(new ApiError(422, "bad query")).kind).toBe("POLICY_REJECTED");
    expect(classifyAgentAskError(new ApiError(429, "slow down")).kind).toBe("MODEL_THROTTLED");
    expect(classifyAgentAskError(new ApiError(503, "no route")).kind).toBe("MODEL_UNAVAILABLE");
    expect(classifyAgentAskError(new ApiError(502, "boom")).kind).toBe("SERVER_ERROR");
  });

  it("leaves a genuinely unrecognised status as UNKNOWN", () => {
    // The fallback must stay a fallback: adding the 403 branch should not have
    // turned every unmapped status into an authorization story.
    expect(classifyAgentAskError(new ApiError(418, "teapot")).kind).toBe("UNKNOWN");
  });

  it("carries no clarification payload on a refusal", () => {
    const classified = classifyAgentAskError(new ApiError(403, "DENIED_BY_POLICY"));

    expect(classified.requiredParameters).toEqual([]);
    expect(classified.toolVersionId).toBeNull();
    expect(classified.alternatives).toEqual([]);
  });
});
