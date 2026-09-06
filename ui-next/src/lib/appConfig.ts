/* ---------------------------------------------------------------------------
   What this build is configured to be (review 2026-09-05, F06/F13 · T07/T10).

   TWO INDEPENDENT AXES, previously conflated into one boolean:

     DATA MODE  -- where the screens' data comes from: bundled `fixtures.ts`
                   demo data, or a live backend.
     AUTH MODE  -- how a request proves who is making it: development identity
                   headers, an OIDC bearer token, or an authenticating reverse
                   proxy in front of the app.

   THE DEFECTS this module exists to remove:

   F13. `VITE_USE_FIXTURES !== "0"` meant demo data was the default in the
        client while the container image explicitly set `0`. The shell then
        printed "Platform connected" and "Live" as static text regardless, so
        a demo build and a broken live build looked identical, and neither
        could be told apart from a working one.

   F06. Identity headers were sent on every live request with the comment that
        this is "inert" under OIDC because the backend ignores them. Inert is
        not the same as correct: the client had no login, no token, no refresh
        and no bearer header, so against an OIDC backend every request 401s --
        and the headers being sent anyway means the failure looks like a
        permissions problem rather than "this build cannot authenticate here".
        The mode is now explicit, and development headers are sent ONLY in
        development mode.

   These values are build-time (`import.meta.env`), which is a property of
   Vite, not a choice: they must therefore be *displayed* rather than assumed,
   which is what `session.tsx` does with them.
--------------------------------------------------------------------------- */

export type DataMode = "fixtures" | "live";
export type AuthMode = "development" | "oidc" | "proxy";

interface RawEnv {
  readonly VITE_USE_FIXTURES?: string;
  readonly VITE_AUTH_MODE?: string;
  readonly VITE_DEV_PRINCIPAL_ID?: string;
  readonly VITE_DEV_ROLES?: string;
}

const DEFAULT_DEV_ROLES =
  "PlatformAdmin,OrganizationAdmin,ProjectAdmin,MetadataAdmin,MetadataIngestor,DataAdmin," +
  "SemanticAdmin,DataSteward,ToolDeveloper,ToolConsumer,AgentDeveloper,Reviewer," +
  "MetadataReviewer,Auditor,Operations,Analyst,Viewer";

export interface AppConfig {
  readonly dataMode: DataMode;
  readonly authMode: AuthMode;
  readonly devPrincipalId: string;
  readonly devRoles: string;
  /** True when `VITE_AUTH_MODE` was not set and the default was inferred. */
  readonly authModeInferred: boolean;
}

function readAuthMode(raw: string | undefined): AuthMode | null {
  switch (raw) {
    case "development":
    case "oidc":
    case "proxy":
      return raw;
    default:
      return null;
  }
}

export function resolveAppConfig(env: RawEnv): AppConfig {
  const dataMode: DataMode = env.VITE_USE_FIXTURES !== "0" ? "fixtures" : "live";
  const declared = readAuthMode(env.VITE_AUTH_MODE);
  return {
    dataMode,
    // An undeclared auth mode means "development" -- the only mode that can
    // work without further configuration -- but the caller is told it was
    // inferred so the shell can say so rather than implying a deployment
    // choice nobody made.
    authMode: declared ?? "development",
    authModeInferred: declared === null,
    devPrincipalId: env.VITE_DEV_PRINCIPAL_ID || "local-ui-admin",
    devRoles: env.VITE_DEV_ROLES || DEFAULT_DEV_ROLES,
  };
}

export const APP_CONFIG: AppConfig = resolveAppConfig(
  (typeof import.meta !== "undefined" ? import.meta.env : {}) as RawEnv,
);

/** Demo data. Screens must label themselves when this is true. */
export const USE_FIXTURES = APP_CONFIG.dataMode === "fixtures";

/**
 * Headers that identify the caller.
 *
 * Development mode sends the dev principal the backend's
 * `identity_provider == "development"` branch requires. OIDC and proxy modes
 * send NEITHER -- under OIDC the backend requires `Authorization: Bearer` and
 * sending a principal header alongside it only disguises the real failure;
 * under an authenticating proxy the proxy is the authority and the browser
 * must not be able to assert an identity of its own.
 *
 * `X-Organization-Id` is not identity: a handful of routes take no
 * `{organization_id}` path segment and resolve the tenant from this header,
 * which the backend then authorizes. It is sent in every live mode.
 */
export function identityHeaders(orgId: string): Record<string, string> {
  if (APP_CONFIG.dataMode === "fixtures") return {};
  const headers: Record<string, string> = { "X-Organization-Id": orgId };
  if (APP_CONFIG.authMode === "development") {
    headers["X-Principal-Id"] = APP_CONFIG.devPrincipalId;
    headers["X-Roles"] = APP_CONFIG.devRoles;
  }
  return headers;
}
