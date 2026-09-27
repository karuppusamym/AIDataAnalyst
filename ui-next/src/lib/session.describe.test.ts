import { describe, expect, it } from "vitest";

import { ApiError } from "./http";
import {
  TOKEN_EXPIRED_DETAIL,
  TOKEN_REJECTED_DETAIL,
  describeSession,
  type Session,
} from "./session";

/* ---------------------------------------------------------------------------
   R11-D6 — "your session expired" must mean the session expired.

   Found by signing in through a real OIDC flow, not by reading code: a token
   the deployment rejected for its audience produced the same 401 as an
   expired one, and the banner said "The session has expired. Sign in again to
   continue." Signing in again could never fix that, and nothing on screen
   said so -- an operator with a misconfigured identity provider would loop.

   `describeSession` had no tests before this file.
--------------------------------------------------------------------------- */

function expired(error: Error | null, authMode = "oidc", lapsed = false): Session {
  return {
    state: "session-expired",
    me: null,
    lapsed,
    lastSuccessAt: null,
    error,
    dataMode: "live" as Session["dataMode"],
    authMode: authMode as Session["authMode"],
    authModeInferred: false,
    reload: () => {},
  };
}

describe("the sign-in banner under a real identity provider", () => {
  it("says the session expired only when the API says it expired", () => {
    const view = describeSession(expired(new ApiError(401, TOKEN_EXPIRED_DETAIL)));

    expect(view.label).toBe("Sign-in required");
    expect(view.hint).toMatch(/has expired/);
  });

  it("does not call a rejected token expired", () => {
    const view = describeSession(expired(new ApiError(401, TOKEN_REJECTED_DETAIL)));

    expect(view.label).toBe("Sign-in rejected");
    expect(view.hint).not.toMatch(/expired/i);
  });

  it("tells the operator what a repeated rejection means", () => {
    // The API returns this same detail for a revoked token, where signing in
    // again does help -- so the copy must not promise that it will not.
    const { hint } = describeSession(expired(new ApiError(401, TOKEN_REJECTED_DETAIL)));

    expect(hint).toMatch(/If signing in again does not clear this/);
    expect(hint).toMatch(/issuer or audience/);
  });

  it("says expired when the browser's own token lapsed, whatever the API answered", () => {
    // The regression this pins was found live, after this file's first
    // version: a renewal failed, the token lapsed, and because a lapsed token
    // is never sent the API answered "a bearer token is required". The banner
    // said "Sign in to continue" and dropped the one true statement it had.
    const view = describeSession(
      expired(new ApiError(401, "a bearer token is required"), "oidc", true),
    );

    expect(view.label).toBe("Sign-in required");
    expect(view.hint).toMatch(/has expired/);
  });

  it("does not claim expiry when no token was presented at all", () => {
    const view = describeSession(expired(new ApiError(401, "a bearer token is required")));

    expect(view.label).toBe("Sign-in required");
    expect(view.hint).not.toMatch(/expired/i);
  });

  it("keeps the development-identity message in development mode", () => {
    const view = describeSession(
      expired(new ApiError(401, TOKEN_REJECTED_DETAIL), "development"),
    );

    expect(view.hint).toMatch(/development identity/);
  });

  it("matches the backend's exact strings", () => {
    // `tests/test_oidc.py` and `aida/security.py` hold the other side. A change
    // to either string must change both, or the banner silently falls back.
    expect(TOKEN_EXPIRED_DETAIL).toBe("bearer token has expired");
    expect(TOKEN_REJECTED_DETAIL).toBe("bearer token verification failed");
  });
});
