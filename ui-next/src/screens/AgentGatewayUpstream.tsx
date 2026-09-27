import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import type { ExternalMcpServerRead, ExternalMcpToolRead } from "../lib/types";
import {
  discoverExternalMcpTools,
  fetchExternalMcpServers,
  fetchExternalMcpTools,
  registerExternalMcpServer,
} from "../lib/api/externalMcp";
import { roleAllows } from "../lib/roles";
import { useSession } from "../lib/session";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";

/* ---------------------------------------------------------------------------
   R11-MP10: upstream MCP servers, catalogued -- the gateway's other direction.

   The rest of this screen is about agents reaching Atlas. This tab is about
   Atlas reading what an upstream MCP server offers: register a server on an
   allowlisted host, read its tool list into the catalogue, and see each tool
   with its screening verdict. It is discovery only -- no tool listed here can
   be invoked from anywhere in the platform -- and a description the ingest
   screen quarantines is withheld, never shown.

   Roles are the routes' own (`aida.external_mcp_api`): registering is
   PlatformAdmin's, discovering PlatformAdmin's and AgentDeveloper's, reading
   also DataSteward's and Auditor's. A control the session is known not to hold
   is not offered; the server stays the authority.
--------------------------------------------------------------------------- */

const REGISTRARS = ["PlatformAdmin"] as const;
const DISCOVERERS = ["PlatformAdmin", "AgentDeveloper"] as const;

function errorText(reason: unknown, fallback: string): string {
  const message = (reason as Error)?.message;
  return message ? message : fallback;
}

function ToolList({ serverId }: { serverId: string }) {
  const [tools, setTools] = useState<ExternalMcpToolRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchExternalMcpTools(serverId, ac.signal)
      .then(setTools)
      .catch((reason) => {
        if ((reason as Error)?.name !== "AbortError") {
          setError(errorText(reason, "The tool list could not be read."));
        }
      });
    return () => ac.abort();
  }, [serverId]);
  if (error) return <p className="agupstream__err" role="alert">{error}</p>;
  if (tools === null) return <p className="agupstream__muted">Loading tools…</p>;
  if (tools.length === 0) {
    return <p className="agupstream__muted">No tools catalogued yet. Run discovery to read them.</p>;
  }
  return (
    <ul className="agupstream__tools" aria-label="Catalogued tools">
      {tools.map((tool) => (
        <li key={tool.id} className="agupstream__tool">
          <span className="agupstream__toolname">{tool.name}</span>
          <Pill tone={tool.screening_status === "CLEAN" ? "ok" : "bad"}>
            {tool.screening_status === "CLEAN" ? "screened clean" : "quarantined"}
          </Pill>
          {tool.status === "WITHDRAWN" ? <Pill tone="mute">withdrawn upstream</Pill> : null}
          <span className="agupstream__tooldesc">
            {tool.description ??
              (tool.screening_status === "CLEAN"
                ? "No description."
                : `Description withheld (${tool.screening_reason_codes.join(", ") || "screening"}).`)}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function UpstreamTab({ organizationId }: { organizationId: string }) {
  const roles = useSession().me?.roles;
  const mayRegister = roleAllows(roles, REGISTRARS);
  const mayDiscover = roleAllows(roles, DISCOVERERS);
  const [servers, setServers] = useState<ExternalMcpServerRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credential, setCredential] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await fetchExternalMcpServers(organizationId));
      setLoadError(null);
    } catch (reason) {
      setLoadError(errorText(reason, "Upstream servers could not be listed."));
    } finally {
      setLoading(false);
    }
  }, [organizationId]);
  useEffect(() => {
    void load();
  }, [load]);

  async function register(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    setBusy("register");
    try {
      await registerExternalMcpServer(organizationId, {
        name: name.trim(),
        base_url: baseUrl.trim(),
        credential_reference: credential.trim() || null,
      });
      setName("");
      setBaseUrl("");
      setCredential("");
      setNotice("Registered. Nothing is read from it until discovery runs.");
      await load();
    } catch (reason) {
      setFormError(errorText(reason, "The server could not be registered."));
    } finally {
      setBusy(null);
    }
  }

  async function discover(server: ExternalMcpServerRead) {
    setBusy(server.id);
    setNotice(null);
    try {
      const found = await discoverExternalMcpTools(server.id);
      setNotice(
        `${server.name}: ${found.listed} listed, ${found.new} new, ${found.changed} changed, ` +
          `${found.withdrawn} withdrawn, ${found.quarantined} quarantined.`,
      );
      setOpen(server.id);
      await load();
    } catch (reason) {
      setNotice(errorText(reason, "Discovery failed."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="agupstream">
      <section className="agcard" aria-label="Upstream MCP servers">
        <h2 className="agcard__h2">Upstream MCP servers</h2>
        <p className="agcard__lede">
          Servers Atlas reads tool lists from, into the catalogue. Discovery only: no tool listed
          here can be called from Atlas, and a description the ingest screen quarantines is
          withheld.
        </p>
        {notice ? (
          <p className="agupstream__notice" role="status">
            {notice}
          </p>
        ) : null}
        {loading ? (
          <p className="agupstream__muted">Loading servers…</p>
        ) : loadError ? (
          <ErrorState
            title="Upstream servers could not be listed"
            detail={loadError}
            onRetry={() => void load()}
          />
        ) : servers.length === 0 ? (
          <Empty
            title="No upstream servers registered"
            hint="A platform admin registers one on a host the deployment allowlists."
          />
        ) : (
          <ul className="agupstream__servers">
            {servers.map((server) => (
              <li key={server.id} className="agupstream__server">
                <div className="agupstream__serverhead">
                  <span className="agupstream__servername">{server.name}</span>
                  <Pill tone={server.status === "ACTIVE" ? "ok" : "mute"}>
                    {server.status.toLowerCase()}
                  </Pill>
                  <span className="agupstream__muted">
                    {server.discovered_tool_count} tool
                    {server.discovered_tool_count === 1 ? "" : "s"}
                    {server.last_discovered_at
                      ? ` · last read ${new Date(server.last_discovered_at).toLocaleString()}`
                      : " · never read"}
                  </span>
                </div>
                <code className="agupstream__url">{server.base_url}</code>
                {server.last_discovery_error ? (
                  <p className="agupstream__err">Last discovery failed: {server.last_discovery_error}</p>
                ) : null}
                <div className="agupstream__actions">
                  {mayDiscover ? (
                    <Button
                      disabled={busy !== null || server.status !== "ACTIVE"}
                      onClick={() => void discover(server)}
                    >
                      {busy === server.id ? "Reading…" : "Read tool list"}
                    </Button>
                  ) : null}
                  <Button onClick={() => setOpen(open === server.id ? null : server.id)}>
                    {open === server.id ? "Hide tools" : "Show tools"}
                  </Button>
                </div>
                {open === server.id ? <ToolList serverId={server.id} /> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      {mayRegister ? (
        <section className="agcard" aria-label="Register an upstream server">
          <h2 className="agcard__h2">Register a server</h2>
          <form className="agform" onSubmit={(event) => void register(event)}>
            <div className="agform__grid">
              <Field label="Name">
                <input required value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label="Base URL (https, allowlisted host)">
                <input
                  required
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://mcp.bank.internal/mcp"
                />
              </Field>
              <Field label="Credential reference (optional)">
                <input
                  value={credential}
                  onChange={(e) => setCredential(e.target.value)}
                  placeholder="vault://mcp/upstream-token"
                />
              </Field>
            </div>
            {formError ? (
              <p className="agupstream__err" role="alert">
                {formError}
              </p>
            ) : null}
            <Button type="submit" variant="primary" disabled={busy !== null}>
              {busy === "register" ? "Registering…" : "Register"}
            </Button>
          </form>
        </section>
      ) : null}
    </div>
  );
}
