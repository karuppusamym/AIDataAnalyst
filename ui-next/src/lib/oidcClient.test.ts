import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AppConfig } from "./appConfig";
import { getAccessToken, resetAuthForTests, signInFailure } from "./authSession";
import {
  PKCE_TRANSACTION_KEY,
  completePendingSignIn,
  createPkcePair,
  hasPendingRenewal,
  prepareAuthorizationRequest,
  readAuthorizationResponse,
  renewAccessToken,
  resetOidcClientForTests,
  signOut,
} from "./oidcClient";

/* ---------------------------------------------------------------------------
   F06/T07. What is asserted here is the part of the flow that decides whether
   a token is legitimate, not the redirect itself:

     - the challenge sent to the IdP is the SHA-256 of the verifier, never the
       verifier (that is the whole of PKCE);
     - an authorization response is exchanged only against a transaction this
       document created, and only once;
     - a mismatched `state` is refused rather than exchanged;
     - a failure is recorded so the sign-in screen can show it instead of
       leaving a dead button.
--------------------------------------------------------------------------- */

const ISSUER = "http://idp.test/atlas";

function config(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    dataMode: "live",
    authMode: "oidc",
    devPrincipalId: "local-ui-admin",
    devRoles: "Viewer",
    authModeInferred: false,
    oidc: {
      issuer: ISSUER,
      clientId: "atlas-ui-next",
      scope: "openid profile email",
      redirectPath: "/",
    },
    ...overrides,
  };
}

const DISCOVERY = {
  authorization_endpoint: `${ISSUER}/authorize`,
  token_endpoint: `${ISSUER}/token`,
  revocation_endpoint: `${ISSUER}/revoke`,
};

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

/** Answers discovery from the fixture and the token endpoint from `token`. */
function stubIdp(token: unknown, tokenOk = true): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes(".well-known/openid-configuration")) {
      return Promise.resolve(jsonResponse(DISCOVERY));
    }
    if (url.endsWith("/token")) {
      return Promise.resolve(jsonResponse(token, tokenOk, tokenOk ? 200 : 400));
    }
    return Promise.resolve(jsonResponse({}, true));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function setUrl(url: string): void {
  history.replaceState(null, "", url);
}

beforeEach(() => {
  sessionStorage.clear();
  setUrl("/");
});

afterEach(() => {
  resetOidcClientForTests();
  resetAuthForTests();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function sha256Base64Url(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  let binary = "";
  for (const byte of new Uint8Array(digest)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

describe("PKCE", () => {
  it("sends the S256 digest of the verifier, not the verifier", async () => {
    const { verifier, challenge } = await createPkcePair();
    expect(challenge).not.toBe(verifier);
    expect(challenge).toBe(await sha256Base64Url(verifier));
    // base64url: no padding and none of base64's URL-hostile characters.
    expect(challenge).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(verifier).toMatch(/^[A-Za-z0-9_-]+$/);
    // RFC 7636 requires 43..128 characters.
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
  });

  it("generates a different verifier for every request", async () => {
    const first = await createPkcePair();
    const second = await createPkcePair();
    expect(first.verifier).not.toBe(second.verifier);
  });
});

describe("the authorization request", () => {
  it("asks for a code with S256 and stores a transaction that matches it", async () => {
    stubIdp(null);
    const url = new URL(await prepareAuthorizationRequest(config()));
    expect(url.origin + url.pathname).toBe(`${ISSUER}/authorize`);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("client_id")).toBe("atlas-ui-next");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("scope")).toBe("openid profile email");

    const stored = JSON.parse(sessionStorage.getItem(PKCE_TRANSACTION_KEY) ?? "{}") as {
      state: string;
      verifier: string;
      redirectUri: string;
    };
    expect(stored.state).toBe(url.searchParams.get("state"));
    expect(url.searchParams.get("code_challenge")).toBe(await sha256Base64Url(stored.verifier));
    // The verifier itself never leaves the browser.
    expect(url.toString()).not.toContain(stored.verifier);
    expect(stored.redirectUri).toBe(`${window.location.origin}/`);
  });

  it("refuses to start when the build names no identity provider", async () => {
    stubIdp(null);
    await expect(prepareAuthorizationRequest(config({ oidc: null }))).rejects.toThrow(
      /no identity provider/,
    );
  });
});

describe("reading the authorization response", () => {
  it("recognises a code/state pair", () => {
    expect(readAuthorizationResponse("?code=abc&state=xyz")).toEqual({
      code: "abc",
      state: "xyz",
    });
  });

  it("recognises an error response and keeps its description", () => {
    expect(readAuthorizationResponse("?error=access_denied&error_description=nope")).toEqual({
      error: "access_denied: nope",
    });
  });

  it("is not fooled by a code with no state", () => {
    expect(readAuthorizationResponse("?code=abc")).toBeNull();
    expect(readAuthorizationResponse("")).toBeNull();
  });
});

describe("completing the sign-in", () => {
  it("exchanges the code with the stored verifier and adopts the token", async () => {
    stubIdp(null);
    const url = new URL(await prepareAuthorizationRequest(config()));
    const state = url.searchParams.get("state") ?? "";
    const stored = JSON.parse(sessionStorage.getItem(PKCE_TRANSACTION_KEY) ?? "{}") as {
      verifier: string;
    };

    const fetchMock = stubIdp({ access_token: "at-1", expires_in: 300, refresh_token: "rt-1" });
    setUrl(`/?code=the-code&state=${state}`);
    await expect(completePendingSignIn(config())).resolves.toEqual({ kind: "signed-in" });

    expect(getAccessToken()).toBe("at-1");
    const tokenCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/token"));
    const body = new URLSearchParams(String((tokenCall?.[1] as RequestInit).body));
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code")).toBe("the-code");
    expect(body.get("code_verifier")).toBe(stored.verifier);
    // The callback is scrubbed so the code is not left in history or copied
    // out of the address bar in a support ticket.
    expect(window.location.search).toBe("");
    // Single use: the transaction is gone whatever happened.
    expect(sessionStorage.getItem(PKCE_TRANSACTION_KEY)).toBeNull();
  });

  it("refuses a response whose state does not match the request", async () => {
    stubIdp(null);
    await prepareAuthorizationRequest(config());
    const fetchMock = stubIdp({ access_token: "at-1" });
    setUrl("/?code=the-code&state=not-the-state");

    const outcome = await completePendingSignIn(config());
    expect(outcome.kind).toBe("failed");
    expect(getAccessToken()).toBeNull();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/token"))).toBe(false);
    expect(signInFailure()).toMatch(/did not match/);
  });

  it("ignores a code this document never asked for", async () => {
    const fetchMock = stubIdp({ access_token: "at-1" });
    setUrl("/?code=someone-elses-code&state=whatever");
    await expect(completePendingSignIn(config())).resolves.toEqual({ kind: "none" });
    expect(getAccessToken()).toBeNull();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/token"))).toBe(false);
  });

  it("reports a refused exchange instead of leaving a silent dead end", async () => {
    stubIdp(null);
    const url = new URL(await prepareAuthorizationRequest(config()));
    stubIdp({ error: "invalid_grant", error_description: "code already used" }, false);
    setUrl(`/?code=the-code&state=${url.searchParams.get("state") ?? ""}`);

    const outcome = await completePendingSignIn(config());
    expect(outcome.kind).toBe("failed");
    expect(getAccessToken()).toBeNull();
    expect(signInFailure()).toMatch(/code already used/);
  });

  it("does nothing at all when the URL carries no authorization response", async () => {
    const fetchMock = stubIdp({ access_token: "at-1" });
    await expect(completePendingSignIn(config())).resolves.toEqual({ kind: "none" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does nothing in a build that is not in oidc mode", async () => {
    const fetchMock = stubIdp({ access_token: "at-1" });
    setUrl("/?code=abc&state=xyz");
    await expect(
      completePendingSignIn(config({ authMode: "development" })),
    ).resolves.toEqual({ kind: "none" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("signing out", () => {
  it("drops the token and asks the issuer to revoke the refresh grant", async () => {
    stubIdp(null);
    const url = new URL(await prepareAuthorizationRequest(config()));
    const fetchMock = stubIdp({ access_token: "at-1", expires_in: 300, refresh_token: "rt-1" });
    setUrl(`/?code=the-code&state=${url.searchParams.get("state") ?? ""}`);
    await completePendingSignIn(config());
    expect(getAccessToken()).toBe("at-1");

    signOut(config());
    expect(getAccessToken()).toBeNull();
    // Revocation is best effort and asynchronous; the local sign-out is not.
    await Promise.resolve();
    await Promise.resolve();
    const revoke = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/revoke"));
    expect(revoke).toBeDefined();
    expect(new URLSearchParams(String((revoke?.[1] as RequestInit).body)).get("token")).toBe(
      "rt-1",
    );
  });
});

describe("renewal", () => {
  /* Found by watching a real issuer: re-issuing a token with the SAME absolute
   * `exp` made the naive version request a new one every two seconds forever.
   * A renewal that does not move the deadline is not a renewal. */
  async function signIn(expiresIn: number): Promise<void> {
    stubIdp(null);
    const url = new URL(await prepareAuthorizationRequest(config()));
    stubIdp({ access_token: "at-1", expires_in: expiresIn, refresh_token: "rt-1" });
    setUrl(`/?code=the-code&state=${url.searchParams.get("state") ?? ""}`);
    await completePendingSignIn(config());
  }

  it("arms a renewal when the issuer stated a lifetime and gave a refresh token", async () => {
    await signIn(600);
    expect(hasPendingRenewal()).toBe(true);
  });

  it("stops when the issuer will not move the expiry", async () => {
    await signIn(600);
    // The same deadline, re-issued: shorter `expires_in`, identical `exp`.
    stubIdp({ access_token: "at-2", expires_in: 300, refresh_token: "rt-2" });
    await expect(renewAccessToken(config())).resolves.toBe(false);
    expect(hasPendingRenewal()).toBe(false);
    expect(signInFailure()).toMatch(/cannot be extended/);
    // The token in hand is untouched: a grant that can no longer be refreshed
    // does not invalidate what it already issued.
    expect(getAccessToken()).toBe("at-1");
  });

  it("accepts a renewal that genuinely extends the session", async () => {
    await signIn(600);
    stubIdp({ access_token: "at-2", expires_in: 1200, refresh_token: "rt-2" });
    await expect(renewAccessToken(config())).resolves.toBe(true);
    expect(getAccessToken()).toBe("at-2");
    expect(hasPendingRenewal()).toBe(true);
  });

  it("ends the renewal chain when the issuer is unreachable", async () => {
    await signIn(600);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );
    await expect(renewAccessToken(config())).resolves.toBe(false);
    expect(hasPendingRenewal()).toBe(false);
    expect(signInFailure()).toMatch(/network down/);
  });
});
