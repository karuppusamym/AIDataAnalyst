import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AgentContractRequestRead, ContextProductRead, MeRead, ProjectRead } from "../lib/types";
import type { PageOf } from "../lib/ui-types";
import { ApiError } from "../lib/api";

/* ---------------------------------------------------------------------------
   Agent gateway.

   The three claims worth testing are the three the screen exists to make:

     1. It tells the truth about authentication. The backend has two identity
        branches (`security.py`), and telling an engineer to send a Bearer
        token at a `identity_provider=development` deployment — or dev headers
        at an OIDC one — sends them into a 401 they cannot debug.
     2. Exposure is computed from what is actually PUBLISHED, using the same
        naming the MCP server uses. A page that lists a draft as though an
        agent could see it is worse than no page.
     3. Refusals are shown, not filtered out. The CX-4 edges include DENY
        rows, and those are the evidence the boundary held.
--------------------------------------------------------------------------- */

const fetchMe = vi.fn<(signal?: AbortSignal) => Promise<MeRead>>();
const fetchOrgProjects = vi.fn<(orgId: string, signal?: AbortSignal) => Promise<PageOf<ProjectRead>>>();
const fetchContextProducts = vi.fn();
const fetchTools = vi.fn();
const fetchConsumptionRecords = vi.fn();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchMe: (signal?: AbortSignal) => fetchMe(signal),
    fetchOrgProjects: (orgId: string, signal?: AbortSignal) => fetchOrgProjects(orgId, signal),
    fetchContextProducts: (...args: unknown[]) => fetchContextProducts(...args),
    fetchTools: (...args: unknown[]) => fetchTools(...args),
    fetchConsumptionRecords: (...args: unknown[]) => fetchConsumptionRecords(...args),
  };
});

const PROJECT: ProjectRead = {
  id: "proj_core", organization_id: "org1", line_of_business_id: "lob1", data_domain_id: "dom1",
  name: "Core Finance", slug: "core-finance", status: "ACTIVE",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const DEV_ME: MeRead = {
  principal_id: "local-ui-admin", principal_type: "USER",
  organization_id: "org1", roles: ["Analyst"], persona: "Analyst",
  identity_provider: "development",
};

function product(status: string, version: number): ContextProductRead {
  return {
    id: `cp_${status}`, organization_id: "org1", project_id: "proj_core",
    product_key: "consumer-risk-context", lifecycle_status: "ACTIVE",
    created_by: "risk-data-stewards@tenant.example",
    latest_version: {
      id: `cpv_${status}`, organization_id: "org1", product_id: `cp_${status}`,
      product_key: "consumer-risk-context", version, status,
      name: "Consumer risk analysis", description: "Bounded context for risk analysts.",
      purpose: "Explain drivers of consumer delinquency.",
      owner_type: "GROUP", owner_principal: "risk-data-stewards",
      table_ids: [], semantic_model_version_ids: [], glossary_term_version_ids: [],
      eligible_tool_version_ids: [], allowed_consumer_roles: ["Analyst"],
      lineage_depth: 2, support_window_days: null,
      fingerprint: "f".repeat(64), created_by: "risk-data-stewards@tenant.example",
      approved_by: null, approved_at: null, published_at: null, based_on_version_id: null,
      created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
    },
    created_at: "2026-07-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
  };
}

async function loadScreen() {
  return (await import("./AgentGatewayScreen")).AgentGatewayScreen;
}

beforeEach(() => {
  for (const fn of [fetchMe, fetchOrgProjects, fetchContextProducts, fetchTools, fetchConsumptionRecords]) {
    fn.mockReset();
  }
  fetchMe.mockResolvedValue(DEV_ME);
  fetchOrgProjects.mockResolvedValue({ items: [PROJECT], limit: 500, offset: 0, total: 1 });
  fetchContextProducts.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  fetchTools.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  fetchConsumptionRecords.mockResolvedValue({ items: [], limit: 200, offset: 0, total: 0 });
  vi.resetModules();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AgentGatewayScreen", () => {
  it("gives the endpoint and the auth scheme this deployment actually uses", async () => {
    const AgentGatewayScreen = await loadScreen();
    render(<AgentGatewayScreen />);

    expect(await screen.findByText(`POST ${location.origin}/mcp`)).toBeInTheDocument();
    // Development identity: the dev headers, not a Bearer token.
    expect(screen.getByText(/identity_provider=development/)).toBeInTheDocument();
    // Named twice on purpose -- once as prose in the facts list, once inside
    // the copyable client config -- so this asserts presence, not uniqueness.
    expect(screen.getAllByText(/X-Principal-Id/).length).toBeGreaterThan(0);
    expect(screen.getByText(/"X-Organization-Id": "org1"/)).toBeInTheDocument();
  });

  it("switches the documented credential when the deployment is OIDC", async () => {
    fetchMe.mockResolvedValue({ ...DEV_ME, identity_provider: "OIDC" });
    const AgentGatewayScreen = await loadScreen();
    render(<AgentGatewayScreen />);

    expect(
      await screen.findByText(/issuer, audience and JWKS verified per request/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/identity_provider=development/)).not.toBeInTheDocument();
  });

  it("lists only published context products, under the name the MCP server uses", async () => {
    fetchContextProducts.mockResolvedValue({
      items: [product("PUBLISHED", 2), product("DRAFT", 1)],
      limit: 200, offset: 0, total: 2,
    });
    const AgentGatewayScreen = await loadScreen();
    render(<AgentGatewayScreen />);

    fireEvent.change(await screen.findByLabelText("Project"), { target: { value: "proj_core" } });
    fireEvent.click(screen.getByRole("button", { name: "What agents see" }));

    // `prompts/list` names a prompt `atlas__context__{key}__v{n}` — that exact
    // string is what an agent author will search their client's output for.
    expect(
      await screen.findByText("atlas__context__consumer-risk-context__v2"),
    ).toBeInTheDocument();
    // The draft is counted and explained, never listed as visible.
    expect(screen.queryByText("atlas__context__consumer-risk-context__v1")).not.toBeInTheDocument();
    expect(screen.getByText(/1 draft or retired version is not exposed/)).toBeInTheDocument();
  });

  it("shows refused consumption rather than filtering it out", async () => {
    fetchConsumptionRecords.mockResolvedValue({
      items: [
        {
          id: "cx_1", organization_id: "org1",
          consumer_id: "unbound-agent@agents.tenant.example", consumer_type: "AGENT",
          resource_type: "CONTEXT_PRODUCT", resource_id: "consumer-risk-context",
          channel: "MCP", correlation_id: "corr-1", policy_decision: "DENY",
          business_purpose: null, details: {}, consumed_at: "2026-09-03T11:47:03Z",
        },
      ],
      limit: 200, offset: 0, total: 1,
    });
    const AgentGatewayScreen = await loadScreen();
    render(<AgentGatewayScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Consumption" }));

    expect(await screen.findByText("deny")).toBeInTheDocument();
    expect(screen.getByText(/1 refused in this page/)).toBeInTheDocument();
  });

  it("reports a consumption read failure instead of showing an empty table", async () => {
    fetchConsumptionRecords.mockRejectedValue(new ApiError(403, "not entitled to read consumption lineage"));
    const AgentGatewayScreen = await loadScreen();
    render(<AgentGatewayScreen />);

    fireEvent.click(await screen.findByRole("button", { name: "Consumption" }));

    expect(await screen.findByText("not entitled to read consumption lineage")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("No consumption recorded")).not.toBeInTheDocument());
  });
});
