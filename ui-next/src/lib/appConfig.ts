/* ---------------------------------------------------------------------------
   What this build is configured to be (review 2026-09-05, F06/F13 · T07/T10).

   TWO INDEPENDENT AXES, previously conflated into one boolean:

     DATA MODE  -- where the screens' data comes from: bundled `fixtures.ts`
                   demo data, or a live backend. Settled while the bundle is
                   built, not while it runs (R11-X1, `demoDataMode.ts`): a
                   production build has no fixtures in it to fall back to.
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
  readonly VITE_OIDC_ISSUER?: string;
  readonly VITE_OIDC_CLIENT_ID?: string;
  readonly VITE_OIDC_SCOPE?: string;
  readonly VITE_OIDC_REDIRECT_PATH?: string;
}

/**
 * What the browser needs to run an authorization-code flow itself.
 *
 * Only the issuer is named here. Endpoint URLs are read from the issuer's
 * discovery document at sign-in time (`lib/oidcClient.ts`) rather than being
 * configured one by one, because an IdP is entitled to move them and a
 * hand-copied `authorization_endpoint` is a configuration item that silently
 * rots. `redirectPath` is a path, not a URL: the origin is whatever origin
 * this build is being served from, so one image works on localhost and on a
 * deployed host without rebuilding.
 */
export interface OidcClientConfig {
  readonly issuer: string;
  readonly clientId: string;
  readonly scope: string;
  readonly redirectPath: string;
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
  /**
   * The issuer this build can sign in against, or null when none was
   * configured.
   *
   * THE DEFECT this removes: `authMode === "oidc"` used to mean two different
   * things at once -- "the backend requires a bearer token" and "this build
   * has no way to obtain one" -- so the only honest screen was an apology.
   * They are separate facts. `authMode` says what the backend accepts; this
   * says whether the browser can run a flow. A build with the first and not
   * the second is still blocked, and still says so.
   */
  readonly oidc: OidcClientConfig | null;
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

function readOidcConfig(env: RawEnv): OidcClientConfig | null {
  // Issuer AND client id, or nothing. A half-configured flow would redirect
  // to an endpoint that rejects it, which reads to a user as "sign-in is
  // broken" rather than "this build was never given an identity provider".
  const issuer = (env.VITE_OIDC_ISSUER ?? "").trim().replace(/\/+$/, "");
  const clientId = (env.VITE_OIDC_CLIENT_ID ?? "").trim();
  if (!issuer || !clientId) return null;
  return {
    issuer,
    clientId,
    // `openid` is mandatory for an OIDC authorization-code flow; the rest is
    // what a deployment's IdP contract asks for.
    scope: (env.VITE_OIDC_SCOPE || "openid profile email").trim(),
    redirectPath: env.VITE_OIDC_REDIRECT_PATH?.trim() || "/",
  };
}

/**
 * @param demoData Whether this BUILD carries demo data at all. It defaults to
 *   reading the flag out of `env`, which is what a caller passing a
 *   hand-written environment wants; `APP_CONFIG` below instead passes the
 *   build-time literal, because after R11-X1 the two are not the same fact --
 *   see the note on `USE_FIXTURES`.
 */
export function resolveAppConfig(
  env: RawEnv,
  demoData: boolean = env.VITE_USE_FIXTURES !== "0",
): AppConfig {
  const dataMode: DataMode = demoData ? "fixtures" : "live";
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
    oidc: readOidcConfig(env),
  };
}

/**
 * Demo data. Screens must label themselves when this is true.
 *
 * R11-X1: this is a BUILD-time constant, not a runtime lookup. `vite.config.ts`
 * defines `import.meta.env.VITE_USE_FIXTURES` to a literal `"1"` or `"0"` for
 * every build, so the comparison below folds to `true` or `false` while the
 * bundle is being made -- which is what allows a production build to shed
 * `lib/fixtures.ts` entirely instead of shipping the demo estate to users who
 * can never reach it. Flipping the flag now means rebuilding, and that is the
 * point: a bundler cannot drop what a browser might still ask for.
 *
 * `api/transport.ts` repeats this comparison rather than importing this name.
 * That is not a duplicate of the decision -- both read the same one literal --
 * but a chunking constraint, and it is explained where it is written.
 */
export const USE_FIXTURES: boolean = import.meta.env.VITE_USE_FIXTURES !== "0";

export const APP_CONFIG: AppConfig = resolveAppConfig(
  (typeof import.meta !== "undefined" ? import.meta.env : {}) as RawEnv,
  USE_FIXTURES,
);

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
