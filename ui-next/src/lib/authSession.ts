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

   WHAT IS DELIBERATELY NOT HERE: an authorization-code/PKCE redirect flow.
   Writing one against no issuer, no client id and no redirect registration
   would produce code that cannot be executed, let alone verified, and F06's
   own acceptance criterion is a fresh-browser sign-in through the real
   topology. The seam is `adoptAccessToken`: when an IdP is chosen, the
   callback handler calls it and everything below -- the bearer header, the
   blocked state clearing, sign-out -- already works. Until then the shell
   reports the gap instead of impersonating a signed-in user.
--------------------------------------------------------------------------- */

import { APP_CONFIG, type AppConfig } from "./appConfig";

/** In memory only. A token in `localStorage` is readable by any script that
 *  gets injected into this origin, and it must not outlive the tab. */
let accessToken: string | null = null;
let expiresAt: number | null = null;

const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

/** Subscribe to token changes (sign-in, sign-out, expiry). */
export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The current token, or null when there is none or the one held has expired. */
export function getAccessToken(): string | null {
  if (accessToken && expiresAt !== null && Date.now() >= expiresAt) {
    accessToken = null;
    expiresAt = null;
  }
  return accessToken;
}

export function hasAccessToken(): boolean {
  return getAccessToken() !== null;
}

/**
 * Accept a token obtained by an identity flow.
 *
 * Nothing in the shipped app calls this yet -- see the file comment. It is
 * the single entry point a real OIDC callback handler will use, so that the
 * rest of the client never needs to learn where tokens come from.
 */
export function adoptAccessToken(token: string, expiresInSeconds?: number): void {
  accessToken = token;
  expiresAt = expiresInSeconds ? Date.now() + expiresInSeconds * 1000 : null;
  emit();
}

/** Drop the token. Used by sign-out and by a 401 that invalidates it. */
export function clearAccessToken(): void {
  if (accessToken === null && expiresAt === null) return;
  accessToken = null;
  expiresAt = null;
  emit();
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

export type AuthBlockReason = "oidc-no-token";

export interface AuthBlock {
  readonly reason: AuthBlockReason;
  readonly title: string;
  readonly detail: string;
  /** What an operator has to change. Shown verbatim; it is a build/deploy fix. */
  readonly remedy: string;
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
  return {
    reason: "oidc-no-token",
    title: "This build cannot sign you in",
    detail:
      "The app is configured for OIDC authentication, which requires a bearer token on every " +
      "request. This build ships no sign-in flow, so it has no token to send and every request " +
      "would be rejected as unauthenticated.",
    remedy:
      "Put an authenticating proxy in front of the app and build with VITE_AUTH_MODE=proxy, or " +
      "run against a development-identity backend with VITE_AUTH_MODE=development.",
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
  accessToken = null;
  expiresAt = null;
  listeners.clear();
}
