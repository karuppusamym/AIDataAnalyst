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
  it("reads an ambiguous-knowledge clarification as a choice between its candidates (R11-OKF02)", () => {
    const classified = classifyAgentAskError(
      new ApiError(409, "matches two tables", {
        details: { code: "AMBIGUOUS_KNOWLEDGE", candidates: ["a.orders", "b.orders", 7] },
      }),
    );
    expect(classified.kind).toBe("AMBIGUOUS_KNOWLEDGE");
    expect(classified.candidates).toEqual(["a.orders", "b.orders"]);
    // A clarification without that code is still the tool-parameter one.
    const tool = classifyAgentAskError(
      new ApiError(409, "needs input", { details: { code: "MISSING_TOOL_PARAMETERS", required_parameters: ["x"] } }),
    );
    expect(tool.kind).toBe("CLARIFICATION_NEEDED");
    expect(tool.candidates).toEqual([]);
  });

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

  /* R11-FP12 (F08): the three context-product refusals were one kind, so the
     screen had one title and one remedy for three different problems -- and the
     server's raw token as the message. Each is its own kind now; the screen's
     own test asserts the three sentences. */
  it("tells the three context-product refusals apart", () => {
    expect(classifyAgentAskError(new ApiError(422, "CONTEXT_PRODUCT_NOT_AVAILABLE")).kind).toBe(
      "CONTEXT_PRODUCT_UNAVAILABLE",
    );
    expect(
      classifyAgentAskError(new ApiError(422, "CONTEXT_PRODUCT_CONSUMER_ROLE_REQUIRED")).kind,
    ).toBe("CONTEXT_PRODUCT_ROLE_REQUIRED");
    expect(
      classifyAgentAskError(new ApiError(422, "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE")).kind,
    ).toBe("CONTEXT_PRODUCT_OUT_OF_SCOPE");
  });

  it("does not guess an unrecognised 422 into a context-product refusal", () => {
    // The mapping is keyed by the server's own stable tokens
    // (`agent_orchestrator.py:378-380`); anything else is the ordinary policy
    // rejection it has always been.
    expect(classifyAgentAskError(new ApiError(422, "CONTEXT_PRODUCT_SOMETHING_NEW")).kind).toBe(
      "POLICY_REJECTED",
    );
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
