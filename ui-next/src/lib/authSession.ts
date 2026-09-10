/* ---------------------------------------------------------------------------
   The browser's access token, and the honest answer to "can this build
   authenticate here at all?" (review 2026-09-05, F06 · T07).

   THE DEFECT this module exists to remove: the client had no token store, no
   login, no refresh and no `Authorization` header, yet it happily issued
   requests against a backend whose OIDC mode reads the bearer header and
   nothing else. Every screen then rendered its own 401 as though the estate
   were empty or the account under-privileged. A build that cannot sign in
   must say so once, in the shell, before the screens start guessing.

   THE INVARIANT: identity is asserted exactly one way per configured auth
   mode, and there is never a silent fallback to the development principal.

     development -- `appConfig.identityHeaders` sends the dev principal. No
                    token is involved and signing out is meaningless.
     oidc        -- the ONLY accepted assertion is a bearer token held here.
                    No token means the app is blocked, not that it should try
                    something else.
     proxy       -- an authenticating reverse proxy in front of the app is the
                    authority. The browser asserts nothing; a token set here
                    would be a second, weaker claim, so none is sent.

   WHERE THE TOKEN COMES FROM: `lib/oidcClient.ts` runs the authorization-code
   + PKCE flow and calls `adoptAccessToken`. This module still knows nothing
   about issuers, redirects or grants -- it holds the token, decides who may
   send it, and answers "can this build authenticate here at all?". A build
   configured for OIDC with no issuer is still blocked, and still says so
   rather than impersonating a signed-in user.

   EXPIRY IS NOT SIGN-OUT, and the difference is visible on screen. A token
   that lapses leaves the shell standing so the session badge can report
   "sign-in required" against the backend's actual 401 -- F06's acceptance
   criterion names expiry behaviour, and a shell that vanished would be
   indistinguishable from a build that was never signed in. Signing out
   discards everything and returns to the sign-in screen, because that is
   what the user asked for.
--------------------------------------------------------------------------- */

import { APP_CONFIG, type AppConfig } from "./appConfig";

/** In memory only. A token in `localStorage` is readable by any script that
 *  gets injected into this origin, and it must not outlive the tab. */
let accessToken: string | null = null;
let expiresAt: number | null = null;

/** Set when a token this browser held reached its expiry without being
 *  replaced. Distinguishes "the session ended" from "there was never one",
 *  which are two different screens. Cleared by a new token and by sign-out. */
let lapsed = false;

/** The last thing that went wrong while obtaining a token, for the sign-in
 *  screen to show. A failed redirect that reports nothing is a dead button. */
let lastFailure: string | null = null;

/** Fires at the exact moment the token lapses. Without it nothing would tell
 *  the shell that the session ended until the user happened to click
 *  something, and the badge would keep claiming "Connected" over a dead
 *  token -- the same invented green F13 removed from the connection state. */
let lapseTimer: ReturnType<typeof setTimeout> | null = null;

const listeners = new Set<() => void>();

/** Bumped on every change. `useSyncExternalStore` needs a snapshot that is
 *  referentially stable between changes, and `authBlock()` builds a fresh
 *  object each call -- returning that directly would re-render forever. A
 *  revision number is the stable thing; the verdict is derived from it. */
let revision = 0;

function emit(): void {
  revision += 1;
  for (const listener of listeners) listener();
}

/** The current change count. See `revision`. */
export function authRevision(): number {
  return revision;
}

/** Subscribe to token changes (sign-in, sign-out, expiry). */
export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function cancelLapseTimer(): void {
  if (lapseTimer !== null) {
    clearTimeout(lapseTimer);
    lapseTimer = null;
  }
}

/** The current token, or null when there is none or the one held has expired. */
export function getAccessToken(): string | null {
  if (accessToken && expiresAt !== null && Date.now() >= expiresAt) {
    accessToken = null;
    expiresAt = null;
    lapsed = true;
  }
  return accessToken;
}

export function hasAccessToken(): boolean {
  return getAccessToken() !== null;
}

/** When the current token expires (epoch millis), or null when there is no
 *  token or the issuer stated no lifetime. Read by the renewal logic, which
 *  has to be able to tell an extension from a re-issue of the same deadline. */
export function accessTokenExpiresAt(): number | null {
  return getAccessToken() === null ? null : expiresAt;
}

/**
 * Accept a token obtained by an identity flow.
 *
 * The single entry point for `lib/oidcClient.ts`'s authorization-code
 * exchange and its renewals, so the rest of the client never has to learn
 * where tokens come from.
 */
export function adoptAccessToken(token: string, expiresInSeconds?: number): void {
  accessToken = token;
  expiresAt = expiresInSeconds ? Date.now() + expiresInSeconds * 1000 : null;
  lapsed = false;
  lastFailure = null;
  cancelLapseTimer();
  if (expiresAt !== null) {
    lapseTimer = setTimeout(
      () => {
        lapseTimer = null;
        // Read through `getAccessToken` so the lapse is recorded in the one
        // place that decides a token is past its expiry.
        getAccessToken();
        emit();
      },
      Math.max(expiresAt - Date.now(), 0),
    );
  }
  emit();
}

/** Drop the token. Used by sign-out and by a 401 that invalidates it. */
export function clearAccessToken(): void {
  cancelLapseTimer();
  if (accessToken === null && expiresAt === null && !lapsed) return;
  accessToken = null;
  expiresAt = null;
  lapsed = false;
  emit();
}

/**
 * Record why a sign-in or a renewal could not produce a token.
 *
 * Held here rather than in the flow module because the screen that has to
 * show it is the one this module already decides to render. A sign-in button
 * that fails silently is a dead control.
 */
export function noteSignInFailure(message: string): void {
  lastFailure = message;
  emit();
}

/** The last sign-in failure, or null. */
export function signInFailure(): string | null {
  return lastFailure;
}

/**
 * True when a token this browser held has lapsed and nothing replaced it.
 *
 * The shell stays mounted in this state on purpose: the session badge reports
 * it against the backend's real 401 rather than the app silently reverting to
 * an unauthenticated -- or, worse, a development -- identity.
 */
export function sessionLapsed(): boolean {
  getAccessToken();
  return lapsed && accessToken === null;
}

/**
 * The `Authorization` header, when this build has something to authorize with.
 *
 * Empty in development mode (the dev principal headers are the assertion) and
 * in proxy mode (the proxy is the authority). Merged into the request headers
 * by `lib/api.ts`'s header provider.
 */
export function authorizationHeaders(config: AppConfig = APP_CONFIG): Record<string, string> {
  if (config.authMode !== "oidc") return {};
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export type AuthBlockReason = "oidc-no-token" | "oidc-sign-in-required";

export interface AuthBlock {
  readonly reason: AuthBlockReason;
  readonly title: string;
  readonly detail: string;
  /** What an operator has to change. Shown verbatim; it is a build/deploy fix. */
  readonly remedy: string;
  /** True when this build can actually start a flow, so the screen may offer
   *  a button rather than only an explanation. */
  readonly canSignIn: boolean;
  /** The last failure, when a previous attempt produced one. */
  readonly failure: string | null;
}

/**
 * Why the app cannot proceed, or null when it can try.
 *
 * This is a *configuration* verdict, not a request outcome: it is knowable
 * before the first request, which is exactly why it must be rendered instead
 * of forty screens each discovering their own 401. Request outcomes
 * (expired, forbidden, unreachable) belong to `lib/session.tsx`.
 */
export function authBlock(config: AppConfig = APP_CONFIG): AuthBlock | null {
  if (config.dataMode !== "live") return null;
  if (config.authMode !== "oidc") return null;
  if (hasAccessToken()) return null;
  // A lapsed session is a request outcome, not a configuration verdict: the
  // shell stays up and `lib/session.tsx` reports "sign-in required" from the
  // backend's own 401. Replacing the whole app here would hide the very state
  // F06 asks to be able to see.
  if (sessionLapsed()) return null;
  if (config.oidc) {
    return {
      reason: "oidc-sign-in-required",
      title: "Sign in to continue",
      detail:
        "This deployment authenticates with OIDC. Every request carries a bearer token issued " +
        `by ${config.oidc.issuer}, and this browser holds none yet.`,
      remedy:
        "Signing in redirects you to the identity provider and back. The token is kept in " +
        "memory for this tab only; closing the tab signs you out.",
      canSignIn: true,
      failure: lastFailure,
    };
  }
  return {
    reason: "oidc-no-token",
    title: "This build cannot sign you in",
    detail:
      "The app is configured for OIDC authentication, which requires a bearer token on every " +
      "request. This build was given no issuer or client id, so it has no way to obtain a " +
      "token and every request would be rejected as unauthenticated.",
    remedy:
      "Rebuild with VITE_OIDC_ISSUER and VITE_OIDC_CLIENT_ID set, put an authenticating proxy " +
      "in front of the app and build with VITE_AUTH_MODE=proxy, or run against a " +
      "development-identity backend with VITE_AUTH_MODE=development.",
    canSignIn: false,
    failure: lastFailure,
  };
}

/**
 * Whether signing out means anything in this build.
 *
 * Only a token this browser holds can be discarded. In development mode the
 * identity is a build-time header and in proxy mode it belongs to the proxy;
 * offering a "Sign out" button for either would be a control that does not
 * control anything.
 */
export function canSignOut(config: AppConfig = APP_CONFIG): boolean {
  return config.authMode === "oidc" && hasAccessToken();
}

/** Reset for tests. Not part of the runtime contract. */
export function resetAuthForTests(): void {
  cancelLapseTimer();
  accessToken = null;
  expiresAt = null;
  lapsed = false;
  lastFailure = null;
  listeners.clear();
}
