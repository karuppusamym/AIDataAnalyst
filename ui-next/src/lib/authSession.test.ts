import { afterEach, describe, expect, it } from "vitest";

import {
  adoptAccessToken,
  authBlock,
  authorizationHeaders,
  canSignOut,
  clearAccessToken,
  getAccessToken,
  noteSignInFailure,
  resetAuthForTests,
  sessionLapsed,
} from "./authSession";
import type { AppConfig } from "./appConfig";

/* ---------------------------------------------------------------------------
   F06/T07. The point of these is that each auth mode asserts identity exactly
   one way, and that a build which cannot authenticate says so instead of
   sending something that only looks like an identity.
--------------------------------------------------------------------------- */

function config(overrides: Partial<AppConfig>): AppConfig {
  return {
    dataMode: "live",
    authMode: "oidc",
    devPrincipalId: "local-ui-admin",
    devRoles: "Viewer",
    authModeInferred: false,
    oidc: null,
    ...overrides,
  };
}

afterEach(() => {
  resetAuthForTests();
});

describe("authorization headers per mode", () => {
  it("sends no bearer in development mode even when a token somehow exists", () => {
    adoptAccessToken("token-123");
    expect(authorizationHeaders(config({ authMode: "development" }))).toEqual({});
  });

  it("sends no bearer in proxy mode — the proxy is the authority", () => {
    adoptAccessToken("token-123");
    expect(authorizationHeaders(config({ authMode: "proxy" }))).toEqual({});
  });

  it("sends the bearer in oidc mode once a token has been adopted", () => {
    expect(authorizationHeaders(config({ authMode: "oidc" }))).toEqual({});
    adoptAccessToken("token-123");
    expect(authorizationHeaders(config({ authMode: "oidc" }))).toEqual({
      Authorization: "Bearer token-123",
    });
  });

  it("treats an expired token as no token rather than sending it", () => {
    adoptAccessToken("token-123", -1);
    expect(getAccessToken()).toBeNull();
    expect(authorizationHeaders(config({ authMode: "oidc" }))).toEqual({});
  });
});

describe("the blocked state", () => {
  it("blocks a live oidc build that has no token", () => {
    const block = authBlock(config({ dataMode: "live", authMode: "oidc" }));
    expect(block?.reason).toBe("oidc-no-token");
  });

  it("does not block once a token is available", () => {
    adoptAccessToken("token-123");
    expect(authBlock(config({ dataMode: "live", authMode: "oidc" }))).toBeNull();
  });

  it("does not block a demo build — no backend is being contacted", () => {
    expect(authBlock(config({ dataMode: "fixtures", authMode: "oidc" }))).toBeNull();
  });

  it("does not block development or proxy modes, which can assert an identity", () => {
    expect(authBlock(config({ authMode: "development" }))).toBeNull();
    expect(authBlock(config({ authMode: "proxy" }))).toBeNull();
  });
});

describe("sign out", () => {
  it("is offered only when there is a token this browser can actually discard", () => {
    expect(canSignOut(config({ authMode: "oidc" }))).toBe(false);
    expect(canSignOut(config({ authMode: "development" }))).toBe(false);
    adoptAccessToken("token-123");
    expect(canSignOut(config({ authMode: "oidc" }))).toBe(true);
    expect(canSignOut(config({ authMode: "proxy" }))).toBe(false);
  });

  it("clearing the token restores the blocked state", () => {
    adoptAccessToken("token-123");
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
    expect(authBlock(config({ authMode: "oidc" }))?.reason).toBe("oidc-no-token");
  });
});

describe("a lapsed session is not the same as never having signed in", () => {
  /* F06's acceptance criterion names expiry behaviour, and the two states
   * have to be told apart on screen: an expired session leaves the shell up
   * so the badge can report "sign-in required" against the backend's real
   * 401, while a build that has never held a token is blocked outright. What
   * neither may do is fall back to the development principal. */
  const live = config({ dataMode: "live", authMode: "oidc" });

  it("keeps the shell mounted after a token expires", () => {
    adoptAccessToken("token-123", -1);
    expect(getAccessToken()).toBeNull();
    expect(sessionLapsed()).toBe(true);
    expect(authBlock(live)).toBeNull();
    // And still sends nothing that could be mistaken for an identity.
    expect(authorizationHeaders(live)).toEqual({});
  });

  it("blocks again after signing out, which is a different intent", () => {
    adoptAccessToken("token-123", -1);
    expect(authBlock(live)).toBeNull();
    clearAccessToken();
    expect(sessionLapsed()).toBe(false);
    expect(authBlock(live)?.reason).toBe("oidc-no-token");
  });

  it("offers a sign-in button only when an issuer was configured", () => {
    expect(authBlock(live)?.canSignIn).toBe(false);
    const withIssuer = config({
      dataMode: "live",
      authMode: "oidc",
      oidc: {
        issuer: "http://idp.test/atlas",
        clientId: "atlas-ui-next",
        scope: "openid",
        redirectPath: "/",
      },
    });
    const block = authBlock(withIssuer);
    expect(block?.reason).toBe("oidc-sign-in-required");
    expect(block?.canSignIn).toBe(true);
    expect(block?.detail).toContain("http://idp.test/atlas");
  });

  it("carries the last failure onto the screen rather than losing it", () => {
    noteSignInFailure("the identity provider refused the token request");
    expect(authBlock(live)?.failure).toBe("the identity provider refused the token request");
    adoptAccessToken("token-123");
    // A successful sign-in clears the stale complaint.
    clearAccessToken();
    expect(authBlock(live)?.failure).toBeNull();
  });
});
