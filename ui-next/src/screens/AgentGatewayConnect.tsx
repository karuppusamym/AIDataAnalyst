import { useCallback, useMemo, useState } from "react";

import { identityHeaders, USE_FIXTURES } from "../lib/appConfig";
import { authorizationHeaders } from "../lib/authSession";
import { useOrgId } from "../lib/org";
import type { MeRead } from "../lib/types";
import { Button, Pill, useCopy } from "../components/primitives";

/* ---------------------------------------------------------------------------
   The Connect tab of the Agent gateway, and the one definition of the
   endpoint it advertises (review 2026-09-05, F07 · R06).

   THE DEFECT this exists to remove: the screen told an engineer to POST to
   `${location.origin}/mcp` and had no idea whether that URL reaches anything.
   Vite proxies `/mcp` in development, so it worked for whoever wrote the
   screen; the production Nginx proxies only `/v1/`, so in a real deployment
   `/mcp` falls through to the SPA's history fallback and the copied endpoint
   answers with `index.html` -- an HTML page, HTTP 200, no JSON-RPC anywhere.
   The engineer discovers this in their own terminal an hour later, with no
   reason to suspect the platform rather than their client.

   THE INVARIANT: the screen that hands out an endpoint must be able to say
   whether that endpoint currently works. `runGatewayDiagnostic` calls the
   exact URL the Copy button produces, with this browser's own credentials,
   and distinguishes "reached the MCP server" from "reached the SPA shell"
   from "did not reach anything" -- three different repairs, in three
   different places.

   The diagnostic is READ-ONLY by construction: `ping` returns `{}` and
   `tools/list` returns what this caller's roles already permit
   (`mcp_server.py`). Neither executes a tool, reads a resource, or writes.
--------------------------------------------------------------------------- */

/** The path `mcp_server.py`'s router is mounted at (`APIRouter(prefix="/mcp")`). */
export const MCP_PATH = "/mcp";

/** Mirrors `mcp_server.py`'s module constants. Pinned here rather than
 *  fetched because `initialize` is a negotiation an agent performs, not a
 *  read this screen is entitled to make on its behalf. */
export const MCP_PROTOCOL_VERSION = "2025-03-26";
export const MCP_SERVER_NAME = "atlas-governed-data-platform";

/**
 * The advertised endpoint. The ONE definition.
 *
 * Resolved against wherever this app is served so the value is correct in
 * dev, in the compose deployment and behind a reverse proxy. Everything that
 * shows, copies, or tests the endpoint reads it from here -- a second
 * derivation is how the displayed URL and the tested URL drift apart, at
 * which point a green diagnostic proves nothing.
 */
export function mcpEndpoint(): string {
  return `${window.location.origin}${MCP_PATH}`;
}

export type DiagnosticOutcome =
  /** JSON-RPC answered. The topology is correct. */
  | "reached"
  /** Routed to the API, which refused this browser's credentials. Also proof
   *  the topology is correct -- and a different repair from the one below. */
  | "unauthorized"
  /** Something answered with a web page. The proxy did not route `/mcp` to
   *  the API: this is F07 exactly. */
  | "spa-shell"
  /** Answered, but not as the MCP server would. */
  | "unexpected"
  /** Nothing answered. */
  | "unreachable";

export interface DiagnosticResult {
  readonly outcome: DiagnosticOutcome;
  readonly detail: string;
  readonly status: number | null;
  /** Tools `tools/list` reported for this caller, when the call got that far. */
  readonly toolCount: number | null;
  readonly endpoint: string;
}

function rpcBody(method: string) {
  return JSON.stringify({ jsonrpc: "2.0", id: `atlas-ui-${method}`, method, params: {} });
}

/**
 * Call the advertised endpoint and report what actually answered.
 *
 * Deliberately a bare `fetch` rather than `api.ts`'s `request`: that helper
 * parses JSON and throws, which would turn "the SPA shell answered with HTML"
 * -- the finding this exists to surface -- into an unhelpful parse error. The
 * identity and authorization headers are the same ones every other request
 * carries, so the diagnostic tests the real authenticated path.
 */
export async function runGatewayDiagnostic(orgId: string): Promise<DiagnosticResult> {
  const endpoint = mcpEndpoint();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...identityHeaders(orgId),
    ...authorizationHeaders(),
  };

  let response: Response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers,
      body: rpcBody("ping"),
      credentials: "same-origin",
    });
  } catch (error) {
    return {
      outcome: "unreachable",
      status: null,
      toolCount: null,
      endpoint,
      detail: `No response from ${endpoint}: ${(error as Error).message}. The host is unreachable, or the request was blocked before it left the browser.`,
    };
  }

  const contentType = response.headers.get("Content-Type") ?? "";
  const text = await response.text();

  if (contentType.includes("text/html") || text.trimStart().startsWith("<")) {
    return {
      outcome: "spa-shell",
      status: response.status,
      toolCount: null,
      endpoint,
      detail:
        `${endpoint} answered with a web page (HTTP ${response.status}, ${contentType || "no content type"}), not JSON-RPC. ` +
        "The reverse proxy in front of this app is not routing /mcp to the API, so it fell through to this app's own history fallback. " +
        "An MCP client pointed at this URL will fail with a parse error. Add the /mcp upstream to the proxy configuration.",
    };
  }

  let parsed: unknown = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = null;
  }
  const record = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;

  if (response.status === 401 || response.status === 403) {
    return {
      outcome: "unauthorized",
      status: response.status,
      toolCount: null,
      endpoint,
      detail:
        `${endpoint} routed to the API, which refused this browser's credentials (HTTP ${response.status}). ` +
        "The topology is correct; the agent will need its own token or principal headers, which is expected — this page's session is not an agent's.",
    };
  }

  if (!record || record["jsonrpc"] !== "2.0") {
    return {
      outcome: "unexpected",
      status: response.status,
      toolCount: null,
      endpoint,
      detail: `${endpoint} answered HTTP ${response.status} with something that is not a JSON-RPC 2.0 response: ${text.slice(0, 200)}`,
    };
  }

  if (record["error"]) {
    const error = record["error"] as Record<string, unknown>;
    return {
      outcome: "unexpected",
      status: response.status,
      toolCount: null,
      endpoint,
      detail: `The MCP server answered, and refused ping: ${String(error["message"] ?? "no message")}.`,
    };
  }

  // Reached it. Ask what this caller would actually be shown, which is the
  // half of the acceptance criterion `ping` alone does not cover.
  let toolCount: number | null = null;
  try {
    const listed = await fetch(endpoint, {
      method: "POST",
      headers,
      body: rpcBody("tools/list"),
      credentials: "same-origin",
    });
    const body = (await listed.json()) as Record<string, unknown>;
    const result = body["result"];
    const tools =
      result && typeof result === "object" ? (result as Record<string, unknown>)["tools"] : null;
    if (Array.isArray(tools)) toolCount = tools.length;
  } catch {
    // A working `ping` with a failing `tools/list` is still a reachable
    // server; the count is reported as unknown rather than as zero, which
    // would read as "you are permitted nothing".
    toolCount = null;
  }

  return {
    outcome: "reached",
    status: response.status,
    toolCount,
    endpoint,
    detail:
      `${endpoint} reached the MCP server and answered JSON-RPC 2.0. ` +
      (toolCount === null
        ? "tools/list could not be read from this session, so the visible tool count is unknown."
        : `tools/list reports ${toolCount} tool${toolCount === 1 ? "" : "s"} visible to this session's roles.`),
  };
}

const OUTCOME_TONE = {
  reached: "ok",
  unauthorized: "info",
  "spa-shell": "bad",
  unexpected: "warn",
  unreachable: "bad",
} as const;

const OUTCOME_LABEL = {
  reached: "MCP server reached",
  unauthorized: "routed to the API · credentials refused",
  "spa-shell": "reached this app, not the MCP server",
  unexpected: "unexpected response",
  unreachable: "no response",
} as const;

export function GatewayDiagnostic() {
  const orgId = useOrgId();
  const [result, setResult] = useState<DiagnosticResult | null>(null);
  const [running, setRunning] = useState(false);

  const run = useCallback(() => {
    setRunning(true);
    void runGatewayDiagnostic(orgId)
      .then(setResult)
      .finally(() => setRunning(false));
  }, [orgId]);

  return (
    <section className="agcard">
      <h2 className="agcard__h2">Check this endpoint</h2>
      <p className="agcard__lede">
        Sends one read-only <code>ping</code>, then <code>tools/list</code>, to the exact URL above
        using this browser's own credentials. It executes nothing and reads no source values — it
        answers one question: does the URL this page hands out actually reach the MCP server from
        here?
      </p>
      {USE_FIXTURES ? (
        <p className="agcard__note">
          This build is serving bundled demo data, so there is no gateway to reach. The diagnostic
          is available in a live build.
        </p>
      ) : (
        <>
          <Button variant="primary" onClick={run} disabled={running}>
            {running ? "Checking…" : "Run connection check"}
          </Button>
          {result ? (
            <div className="agdiag" role="status">
              <div className="agdiag__head">
                <Pill tone={OUTCOME_TONE[result.outcome]}>{OUTCOME_LABEL[result.outcome]}</Pill>
                {result.status !== null ? (
                  <span className="agdiag__status">HTTP {result.status}</span>
                ) : null}
              </div>
              <p className="agdiag__detail">{result.detail}</p>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

/** A copyable block that says so when the clipboard refuses. */
export function CopyBlock({ label, value }: { label: string; value: string }) {
  const { copied, failed, copy } = useCopy();
  return (
    <div className="agcopy">
      <div className="agcopy__head">
        <span className="agcopy__label">{label}</span>
        <Button onClick={() => void copy(value)}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      {failed ? (
        <p className="agcopy__fail" role="alert">
          This browser refused clipboard access — the clipboard was not changed. Select the text
          below and copy it by hand.
        </p>
      ) : null}
      <pre className="agcopy__pre">{value}</pre>
    </div>
  );
}

export function ConnectTab({ me }: { me: MeRead | null }) {
  const endpoint = mcpEndpoint();
  const oidc = me?.identity_provider === "OIDC";

  const clientConfig = useMemo(
    () =>
      JSON.stringify(
        {
          mcpServers: {
            atlas: {
              url: endpoint,
              transport: "http",
              headers: oidc
                ? { Authorization: "Bearer ${ATLAS_ACCESS_TOKEN}" }
                : {
                    "X-Principal-Id": "${ATLAS_PRINCIPAL_ID}",
                    "X-Roles": "Analyst",
                    "X-Organization-Id": me?.organization_id ?? "${ATLAS_ORGANIZATION_ID}",
                  },
            },
          },
        },
        null,
        2,
      ),
    [endpoint, oidc, me?.organization_id],
  );

  return (
    <div className="agconnect">
      <section className="agcard">
        <h2 className="agcard__h2">Endpoint</h2>
        <p className="agcard__lede">
          One stateless JSON-RPC 2.0 endpoint. Every call resolves a security context before
          dispatch and executes through the same gateway as the REST API — MCP is not a side
          door.
        </p>
        <dl className="agfacts">
          <div><dt>URL</dt><dd><code>POST {endpoint}</code></dd></div>
          <div><dt>Protocol</dt><dd><code>{MCP_PROTOCOL_VERSION}</code></dd></div>
          <div><dt>Server name</dt><dd><code>{MCP_SERVER_NAME}</code></dd></div>
          <div>
            <dt>Authentication</dt>
            <dd>
              {oidc ? (
                <><code>Authorization: Bearer &lt;OIDC token&gt;</code> — issuer, audience and JWKS verified per request.</>
              ) : (
                <>
                  This deployment runs <code>identity_provider=development</code>: identity comes
                  from <code>X-Principal-Id</code> / <code>X-Roles</code> / <code>X-Organization-Id</code>.
                  Under OIDC the server ignores those headers and requires a Bearer token instead.
                </>
              )}
            </dd>
          </div>
          {me ? (
            <div>
              <dt>You are</dt>
              <dd><code>{me.principal_id}</code> · {me.roles.length} role{me.roles.length === 1 ? "" : "s"}</dd>
            </div>
          ) : null}
        </dl>
      </section>

      <GatewayDiagnostic />

      <section className="agcard">
        <h2 className="agcard__h2">Client configuration</h2>
        <p className="agcard__lede">
          Drop this into an MCP client (Claude Desktop, Cursor, or your own). Keep the credential
          in the environment — never in the file you commit.
        </p>
        <CopyBlock label="mcp.json" value={clientConfig} />
      </section>

      <section className="agcard">
        <h2 className="agcard__h2">Methods</h2>
        <table className="agmethods">
          <thead>
            <tr><th scope="col">Method</th><th scope="col">Returns</th></tr>
          </thead>
          <tbody>
            <tr><td><code>initialize</code></td><td>Capability negotiation.</td></tr>
            <tr><td><code>tools/list</code></td><td>Published governed tools you are role-eligible for, plus native lineage, validation and marketplace tools.</td></tr>
            <tr><td><code>tools/call</code></td><td>One tool execution through the deterministic SQL gateway. Masked, cost-checked, audited.</td></tr>
            <tr><td><code>resources/list</code></td><td>Catalog assets as <code>atlas://catalog/…</code> URIs — value-free metadata only.</td></tr>
            <tr><td><code>resources/read</code></td><td>Metadata for one resource, policy-evaluated per read.</td></tr>
            <tr><td><code>prompts/list</code></td><td>Published Context Products as version-pinned governed prompts.</td></tr>
            <tr><td><code>prompts/get</code></td><td>One quality-gated context prompt at <code>atlas://context-products/&#123;key&#125;/versions/&#123;n&#125;</code>.</td></tr>
            <tr><td><code>ping</code></td><td>Liveness.</td></tr>
          </tbody>
        </table>
        <p className="agcard__note">
          Resources and prompts never carry source values. To read data, an agent calls a
          published tool — which is the only path that reaches a source at all.
        </p>
      </section>
    </div>
  );
}
