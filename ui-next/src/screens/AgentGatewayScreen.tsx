import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  ConsumptionRecordRead,
  ContextProductRead,
  GovernedToolVersionRead,
  MeRead,
  ProjectRead,
} from "../lib/types";
import {
  ApiError,
  fetchConsumptionRecords,
  fetchContextProducts,
  fetchMe,
  fetchOrgProjects,
  fetchTools,
} from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { navigateTo } from "../lib/navigate";
import { useOrgId } from "../lib/org";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./AgentGatewayScreen.css";

/* ---------------------------------------------------------------------------
   Agent gateway — the front door for the *other* audience.

   Atlas ships a complete MCP server (`src/aida/mcp_server.py`: initialize,
   tools/list, tools/call, resources/list, resources/read, prompts/list,
   prompts/get, every call routed through the same QueryExecutionGateway the
   REST API uses) and nothing in either portal mentioned it. An engineer
   pointing Claude Desktop, Cursor, or a custom client at this platform had no
   way to learn the endpoint, the auth scheme, or what their agent would be
   able to see — and no way to check afterwards what it had actually read.

   This screen is that missing surface, and it is deliberately three answers
   to three questions rather than a dashboard:

     1. Connect     where the endpoint is, how to authenticate, and a client
                    config that can be copied without editing.
     2. Exposure    what THIS caller's roles would make visible through
                    tools/list and prompts/list — computed from the same
                    published-and-role-eligible rules the server applies, so
                    the page never promises an agent more than it will get.
     3. Consumption the CX-4 edges: every allowed *and refused* read an agent
                    already made. The refusals matter most — they are the
                    evidence the governance boundary is holding.

   Nothing here executes a tool or reads a resource. Everything it shows comes
   from read endpoints the caller is already entitled to.
--------------------------------------------------------------------------- */

type TabId = "connect" | "exposure" | "consumption";

const TABS: { id: TabId; label: string }[] = [
  { id: "connect", label: "Connect" },
  { id: "exposure", label: "What agents see" },
  { id: "consumption", label: "Consumption" },
];

/** Mirrors `mcp_server.py`'s module constants. Pinned here rather than
 *  fetched because `initialize` is a negotiation an agent performs, not a
 *  read this screen is entitled to make on its behalf. */
const MCP_PROTOCOL_VERSION = "2025-03-26";
const MCP_SERVER_NAME = "atlas-governed-data-platform";

/** `POST /mcp`, resolved against wherever this app is served so the value is
 *  correct in dev, in the compose deployment, and behind a reverse proxy —
 *  all three of which the legacy portal's hard-coded localhost would get
 *  wrong. */
function mcpEndpoint(): string {
  return `${location.origin}/mcp`;
}

const decisionTone = (decision: string): Tone =>
  decision === "ALLOW" ? "ok" : decision === "DENY" ? "bad" : "warn";

const channelTone = (channel: string): Tone => (channel === "MCP" ? "accent" : "info");

function CopyBlock({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard is unavailable over plain HTTP on some browsers. The value
      // is on screen and selectable, so say what happened rather than
      // pretending it worked.
      setCopied(false);
    }
  };
  return (
    <div className="agcopy">
      <div className="agcopy__head">
        <span className="agcopy__label">{label}</span>
        <Button onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      <pre className="agcopy__pre">{value}</pre>
    </div>
  );
}

function ConnectTab({ me }: { me: MeRead | null }) {
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

function ExposureTab({
  products,
  tools,
  loading,
  error,
  onRetry,
  projectId,
  hasProject,
}: {
  products: ContextProductRead[];
  tools: GovernedToolVersionRead[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  projectId: string | null;
  hasProject: boolean;
}) {
  const published = products.filter(
    (p) => p.latest_version.status === "PUBLISHED" || p.latest_version.status === "SUPPORTED",
  );
  const unpublished = products.length - published.length;

  if (!hasProject) {
    return (
      <Empty
        title="Pick a project to see what agents would get"
        hint="Context products and tools are project-scoped, so exposure is answered per project."
      />
    );
  }
  if (error) return <ErrorState title="Exposure could not be computed" detail={error} onRetry={onRetry} />;
  if (loading) return <div className="agskeleton" role="status" aria-live="polite">Resolving exposure…</div>;

  return (
    <div className="agexposure">
      <section className="agcard">
        <div className="agcard__head">
          <div>
            <h2 className="agcard__h2">prompts/list</h2>
            <p className="agcard__lede">
              Published context products, named exactly as an agent will see them.
              {unpublished > 0 ? ` ${unpublished} draft or retired version is not exposed.` : ""}
            </p>
          </div>
          <Button onClick={() => navigateTo("context", projectId ? { project: projectId } : {})}>
            Manage context products →
          </Button>
        </div>
        {published.length === 0 ? (
          <Empty
            title="No published context products"
            hint="An agent connecting today gets an empty prompts/list. Publish a version to change that."
          />
        ) : (
          <ul className="aglist">
            {published.map((p) => {
              const v = p.latest_version;
              return (
                <li key={p.id} className="aglist__row">
                  <div className="aglist__main">
                    <code className="aglist__name">atlas__context__{p.product_key}__v{v.version}</code>
                    <span className="aglist__desc">{v.name} — {v.description}</span>
                    <code className="aglist__uri">atlas://context-products/{p.product_key}/versions/{v.version}</code>
                  </div>
                  <div className="aglist__meta">
                    <Pill tone={v.status === "PUBLISHED" ? "ok" : "info"}>{v.status.toLowerCase()}</Pill>
                    <span className="aglist__roles">{v.allowed_consumer_roles.join(", ") || "no roles"}</span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="agcard">
        <div className="agcard__head">
          <div>
            <h2 className="agcard__h2">tools/list</h2>
            <p className="agcard__lede">
              Published governed tools. Each executes through the SQL gateway with masking and an
              immutable audit record; an agent never sees the template.
            </p>
          </div>
          <Button onClick={() => navigateTo("tools", projectId ? { project: projectId } : {})}>
            Manage tools →
          </Button>
        </div>
        {tools.length === 0 ? (
          <Empty
            title="No published tools"
            hint="Agents can still read metadata, but nothing in this project can return rows."
          />
        ) : (
          <ul className="aglist">
            {tools.map((tool) => (
              <li key={tool.id} className="aglist__row">
                <div className="aglist__main">
                  <code className="aglist__name">atlas__{tool.slug}</code>
                  <span className="aglist__desc">{tool.description || tool.name}</span>
                  <code className="aglist__uri">
                    {tool.parameters.length} parameter{tool.parameters.length === 1 ? "" : "s"} ·{" "}
                    {tool.referenced_tables.length} table{tool.referenced_tables.length === 1 ? "" : "s"}
                  </code>
                </div>
                <div className="aglist__meta">
                  <Pill tone="ok">v{tool.version}</Pill>
                  <span className="aglist__roles">{tool.allowed_roles.join(", ") || "no roles"}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function ConsumptionTab({
  records,
  total,
  loading,
  error,
  onRetry,
  consumerFilter,
  onConsumerFilterChange,
}: {
  records: ConsumptionRecordRead[];
  total: number | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  consumerFilter: string;
  onConsumerFilterChange: (value: string) => void;
}) {
  const denied = records.filter((r) => r.policy_decision !== "ALLOW").length;

  return (
    <div className="agconsumption">
      <section className="agcard">
        <div className="agcard__head">
          <div>
            <h2 className="agcard__h2">Consumption edges</h2>
            <p className="agcard__lede">
              Every context read the MCP server and the Context Product REST API recorded —
              allowed and refused alike. Refusals are the evidence the boundary held.
            </p>
          </div>
          <Field label="Consumer principal">
            <input
              type="search"
              placeholder="All consumers"
              value={consumerFilter}
              onChange={(e) => onConsumerFilterChange(e.target.value)}
            />
          </Field>
        </div>

        {error ? (
          <ErrorState title="Consumption could not be loaded" detail={error} onRetry={onRetry} />
        ) : loading ? (
          <div className="agskeleton" role="status" aria-live="polite">Loading consumption…</div>
        ) : records.length === 0 ? (
          <Empty
            title="No consumption recorded"
            hint="No agent has read context through this organization yet, or the filter excludes everything."
          />
        ) : (
          <>
            <p className="agcount" role="status">
              {total ?? records.length} edge{(total ?? records.length) === 1 ? "" : "s"}
              {denied > 0 ? ` · ${denied} refused in this page` : ""}
            </p>
            <div className="agtablewrap">
              <table className="agtable">
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Consumer</th>
                    <th scope="col">Channel</th>
                    <th scope="col">Resource</th>
                    <th scope="col">Decision</th>
                    <th scope="col">Purpose</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((r) => (
                    <tr key={r.id}>
                      <td className="agtable__when">{new Date(r.consumed_at).toLocaleString()}</td>
                      <td><code>{r.consumer_id}</code><span className="agtable__sub">{r.consumer_type.toLowerCase()}</span></td>
                      <td><Pill tone={channelTone(r.channel)}>{r.channel}</Pill></td>
                      <td><code>{r.resource_id}</code><span className="agtable__sub">{r.resource_type.toLowerCase().replace(/_/g, " ")}</span></td>
                      <td><Pill tone={decisionTone(r.policy_decision)}>{r.policy_decision.toLowerCase()}</Pill></td>
                      <td className="agtable__purpose">{r.business_purpose ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

export function AgentGatewayScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const projectId = params.get("project");
  const tab = (TABS.some((t) => t.id === params.get("tab")) ? params.get("tab") : "connect") as TabId;

  const [me, setMe] = useState<MeRead | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchMe(ac.signal).then(setMe).catch(() => undefined);
    return () => ac.abort();
  }, []);

  const [projects, setProjects] = useState<ProjectRead[]>([]);
  useEffect(() => {
    const ac = new AbortController();
    fetchOrgProjects(ORG, ac.signal)
      .then((page) => setProjects(page.items))
      .catch(() => undefined);
    return () => ac.abort();
  }, [ORG]);

  /* ---- exposure ------------------------------------------------------ */
  const [products, setProducts] = useState<ContextProductRead[]>([]);
  const [tools, setTools] = useState<GovernedToolVersionRead[]>([]);
  const [exposureLoading, setExposureLoading] = useState(false);
  const [exposureError, setExposureError] = useState<string | null>(null);

  const loadExposure = useCallback(
    async (signal?: AbortSignal) => {
      if (!projectId) {
        setProducts([]);
        setTools([]);
        return;
      }
      setExposureLoading(true);
      setExposureError(null);
      try {
        const [productPage, toolPage] = await Promise.all([
          fetchContextProducts(projectId, { limit: 200 }, signal),
          fetchTools(projectId, { status: "PUBLISHED", limit: 200 }, signal),
        ]);
        if (signal?.aborted) return;
        setProducts(productPage.items);
        setTools(toolPage.items);
      } catch (e) {
        if (signal?.aborted || (e as Error)?.name === "AbortError") return;
        setExposureError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        if (!signal?.aborted) setExposureLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    const ac = new AbortController();
    void loadExposure(ac.signal);
    return () => ac.abort();
  }, [loadExposure]);

  /* ---- consumption --------------------------------------------------- */
  const [consumerFilter, setConsumerFilter] = useState("");
  const [records, setRecords] = useState<ConsumptionRecordRead[]>([]);
  const [recordTotal, setRecordTotal] = useState<number | null>(null);
  const [consumptionLoading, setConsumptionLoading] = useState(false);
  const [consumptionError, setConsumptionError] = useState<string | null>(null);

  const loadConsumption = useCallback(
    async (signal?: AbortSignal) => {
      setConsumptionLoading(true);
      setConsumptionError(null);
      try {
        const page = await fetchConsumptionRecords(
          ORG,
          { consumerId: consumerFilter.trim() || undefined, limit: 200 },
          signal,
        );
        if (signal?.aborted) return;
        setRecords(page.items);
        setRecordTotal(page.total);
      } catch (e) {
        if (signal?.aborted || (e as Error)?.name === "AbortError") return;
        setConsumptionError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        if (!signal?.aborted) setConsumptionLoading(false);
      }
    },
    [ORG, consumerFilter],
  );

  useEffect(() => {
    if (tab !== "consumption") return;
    const ac = new AbortController();
    // Debounced so typing a principal into the filter does not fire a request
    // per keystroke against an audit table.
    const timer = setTimeout(() => void loadConsumption(ac.signal), 250);
    return () => {
      clearTimeout(timer);
      ac.abort();
    };
  }, [tab, loadConsumption]);

  return (
    <div className="agscreen">
      <header className="agscreen__head">
        <div>
          <p className="agscreen__eyebrow">EXTERNAL AGENT ACCESS</p>
          <h1 className="agscreen__h1">Agent gateway</h1>
          <p className="agscreen__lede">
            How an agent outside Atlas connects, what it will be able to see, and what it has
            already read. Everything here is governed by the same policy as the portal.
          </p>
        </div>
        <div className="agscreen__filters">
          <Field label="Project">
            <select
              value={projectId ?? ""}
              onChange={(e) => setParams({ project: e.target.value || null })}
            >
              <option value="">Select a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </Field>
        </div>
      </header>

      <nav className="agtabs" aria-label="Agent gateway sections">
        {TABS.map((t) => (
          <button
            key={t.id}
            className="agtabs__tab"
            aria-current={t.id === tab ? "page" : undefined}
            onClick={() => setParams({ tab: t.id === "connect" ? null : t.id })}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "connect" ? <ConnectTab me={me} /> : null}
      {tab === "exposure" ? (
        <ExposureTab
          products={products}
          tools={tools}
          loading={exposureLoading}
          error={exposureError}
          onRetry={() => void loadExposure()}
          projectId={projectId}
          hasProject={Boolean(projectId)}
        />
      ) : null}
      {tab === "consumption" ? (
        <ConsumptionTab
          records={records}
          total={recordTotal}
          loading={consumptionLoading}
          error={consumptionError}
          onRetry={() => void loadConsumption()}
          consumerFilter={consumerFilter}
          onConsumerFilterChange={setConsumerFilter}
        />
      ) : null}
    </div>
  );
}
