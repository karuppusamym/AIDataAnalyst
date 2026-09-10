import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

import { APP_CONFIG, USE_FIXTURES } from "./appConfig";
import { ApiError, observeRequests } from "./http";
import type { MeRead } from "./types";

/* ---------------------------------------------------------------------------
   Honest session and connection state (review 2026-09-05, F13 · T10).

   THE DEFECT this module exists to remove: the shell rendered "Platform
   connected" and "Live" as literal static text, and threw away the error from
   `/me`. A demo build, a live build talking to a healthy backend, a live build
   whose token had expired, and a live build whose API was down all rendered
   the same reassuring green. The one piece of state a user needs in order to
   know whether to trust what is on screen was the one piece the screen made
   up.

   THE INVARIANT: the shell may only claim a state it has evidence for. The
   evidence is (a) the build's configured mode, and (b) the outcome of real
   requests. `lastSuccessAt` is the timestamp of an actual 2xx, not of a render.

   WHY IT WATCHES EVERY REQUEST rather than polling: a heartbeat that succeeds
   while the screen's own reads fail is exactly the false green this replaces.
   `http.ts` reports every completed request here, so the badge reflects what
   the app is actually experiencing.

   WHAT THIS IS NOT: a health check for the platform. A green badge means "this
   browser's last request to this API succeeded". Subsystem health belongs on
   the Reliability screen, sourced from the backend's own readiness reporting.
--------------------------------------------------------------------------- */

export type SessionState =
  /** Demo data is bundled into the build; no backend is being contacted. */
  | "demo"
  /** The first identity request has not resolved yet. */
  | "connecting"
  /** A request has succeeded recently. */
  | "connected"
  /** Something succeeded before, and something has failed since. */
  | "degraded"
  /** Requests are failing at the transport level. */
  | "disconnected"
  /** The backend answered 401: sign-in is required or the token expired. */
  | "session-expired"
  /** The backend answered 403: authenticated, but not permitted here. */
  | "forbidden";

export interface Session {
  readonly state: SessionState;
  readonly me: MeRead | null;
  /** Epoch millis of the most recent successful request, or null. */
  readonly lastSuccessAt: number | null;
  /** The error that produced a non-connected state, when there was one. */
  readonly error: ApiError | Error | null;
  readonly dataMode: typeof APP_CONFIG.dataMode;
  readonly authMode: typeof APP_CONFIG.authMode;
  readonly authModeInferred: boolean;
  /** Re-run the identity request (the "Reconnect" affordance). */
  readonly reload: () => void;
}

const SessionContext = createContext<Session | null>(null);

/**
 * A session for components rendered outside the provider.
 *
 * Unit tests render screens bare. Reporting "demo" there is truthful: those
 * renders are fixture-backed by construction, and it keeps a bare render from
 * showing a scary "disconnected" badge it has no evidence for.
 */
const STANDALONE_SESSION: Session = {
  state: "demo",
  me: null,
  lastSuccessAt: null,
  error: null,
  dataMode: APP_CONFIG.dataMode,
  authMode: APP_CONFIG.authMode,
  authModeInferred: APP_CONFIG.authModeInferred,
  reload: () => undefined,
};

export function SessionProvider({
  children,
  fetchMe,
}: {
  children: ReactNode;
  /** Injected so this module does not import the API client (avoids a cycle). */
  fetchMe: (signal?: AbortSignal) => Promise<MeRead>;
}) {
  const [me, setMe] = useState<MeRead | null>(null);
  const [lastSuccessAt, setLastSuccessAt] = useState<number | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [identityResolved, setIdentityResolved] = useState(USE_FIXTURES);
  const [reloadToken, setReloadToken] = useState(0);
  const succeededOnce = useRef(false);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  // Every completed request updates the picture. A success clears a previous
  // failure; a failure does not erase the fact that something worked before,
  // which is the difference between "degraded" and "disconnected".
  useEffect(() => {
    if (USE_FIXTURES) return;
    return observeRequests((outcome) => {
      if (outcome.ok) {
        succeededOnce.current = true;
        setLastSuccessAt(outcome.at);
        setError(null);
        return;
      }
      // 404 and 422 are answers about a resource or a request, not about the
      // connection. Treating them as connection failures would paint the shell
      // red every time a screen asked for something that does not exist.
      const status = outcome.status;
      if (status === 404 || status === 422) return;
      setError(outcome.error ?? new Error(`request failed with status ${status}`));
    });
  }, []);

  /* Identity is resolved in BOTH modes.
   *
   * In fixture mode `fetchMe` returns a bundled object without touching the
   * network, and the shell still needs a persona to choose a landing work area
   * and to key onboarding progress. Skipping it here only moved the same call
   * into the shell, which is how a "demo build has no identity" special case
   * gets re-invented per consumer. The connection state stays `demo`
   * regardless -- see the state machine below; resolving identity is not a
   * claim that a backend was reached. */
  useEffect(() => {
    const controller = new AbortController();
    setIdentityResolved(false);
    fetchMe(controller.signal)
      .then((identity) => {
        if (controller.signal.aborted) return;
        setMe(identity);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setMe(null);
        setError(cause instanceof Error ? cause : new Error(String(cause)));
      })
      .finally(() => {
        if (!controller.signal.aborted) setIdentityResolved(true);
      });
    return () => controller.abort();
  }, [fetchMe, reloadToken]);

  const state = useMemo<SessionState>(() => {
    if (USE_FIXTURES) return "demo";
    if (error instanceof ApiError) {
      if (error.isUnauthenticated) return "session-expired";
      if (error.isForbidden) return "forbidden";
      if (error.status === 0) return succeededOnce.current ? "degraded" : "disconnected";
      return succeededOnce.current ? "degraded" : "disconnected";
    }
    if (error) return succeededOnce.current ? "degraded" : "disconnected";
    if (!identityResolved) return "connecting";
    return lastSuccessAt !== null || me !== null ? "connected" : "connecting";
  }, [error, identityResolved, lastSuccessAt, me]);

  const value = useMemo<Session>(
    () => ({
      state,
      me,
      lastSuccessAt,
      error,
      dataMode: APP_CONFIG.dataMode,
      authMode: APP_CONFIG.authMode,
      authModeInferred: APP_CONFIG.authModeInferred,
      reload,
    }),
    [state, me, lastSuccessAt, error, reload],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  return useContext(SessionContext) ?? STANDALONE_SESSION;
}

/** Short label and tone for the shell badge. Kept here so it is stated once. */
export function describeSession(session: Session): {
  label: string;
  tone: "demo" | "ok" | "warn" | "error" | "pending";
  hint: string;
} {
  switch (session.state) {
    case "demo":
      return {
        label: "Demo data",
        tone: "demo",
        hint: "Screens show bundled sample data. No backend is being contacted.",
      };
    case "connecting":
      return { label: "Connecting…", tone: "pending", hint: "Establishing the session." };
    case "connected":
      return {
        label: "Connected",
        tone: "ok",
        hint: session.lastSuccessAt
          ? `Last successful request ${new Date(session.lastSuccessAt).toLocaleTimeString()}.`
          : "Session established.",
      };
    case "degraded":
      return {
        label: "Degraded",
        tone: "warn",
        hint: session.lastSuccessAt
          ? `Requests are failing. Last success ${new Date(session.lastSuccessAt).toLocaleTimeString()}.`
          : "Requests are failing.",
      };
    case "disconnected":
      return {
        label: "Disconnected",
        tone: "error",
        hint: "The API could not be reached. Data on screen may be stale or missing.",
      };
    case "session-expired":
      return {
        label: "Sign-in required",
        tone: "error",
        hint:
          session.authMode === "development"
            ? "The backend rejected this build's development identity."
            : "The session has expired. Sign in again to continue.",
      };
    case "forbidden":
      return {
        label: "Not permitted",
        tone: "error",
        hint: "This account is not permitted in the selected organization.",
      };
  }
}
