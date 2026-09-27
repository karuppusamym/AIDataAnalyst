import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { Session } from "../lib/session";
import type { ExternalMcpServerRead, ExternalMcpToolRead, MeRead } from "../lib/types";

/* R11-MP10: the Agent gateway's Upstream servers tab -- register, read a tool list, show each
   tool with its screening verdict, and offer each control only to the roles its route admits. */

const fetchExternalMcpServers = vi.fn();
const registerExternalMcpServer = vi.fn();
const discoverExternalMcpTools = vi.fn();
const fetchExternalMcpTools = vi.fn();
vi.mock("../lib/api/externalMcp", () => ({
  fetchExternalMcpServers: (...args: unknown[]) => fetchExternalMcpServers(...args),
  registerExternalMcpServer: (...args: unknown[]) => registerExternalMcpServer(...args),
  discoverExternalMcpTools: (...args: unknown[]) => discoverExternalMcpTools(...args),
  fetchExternalMcpTools: (...args: unknown[]) => fetchExternalMcpTools(...args),
}));

let sessionMe: MeRead | null = null;
vi.mock("../lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/session")>();
  return {
    ...actual,
    useSession: (): Session => ({
      state: "demo",
      me: sessionMe,
      lapsed: false,
      lastSuccessAt: null,
      error: null,
      dataMode: "fixtures",
      authMode: "development",
      authModeInferred: false,
      reload: () => undefined,
    }),
  };
});

const { UpstreamTab } = await import("./AgentGatewayUpstream");

const asRoles = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

const SERVER: ExternalMcpServerRead = {
  id: "srv_1", organization_id: "org_1", name: "integration", base_url: "https://mcp.bank.internal/mcp",
  uses_credential_reference: false, status: "ACTIVE", server_name: null, protocol_version: null,
  last_discovered_at: null, last_discovery_error: null, discovered_tool_count: 0,
};

const TOOLS: ExternalMcpToolRead[] = [
  {
    id: "t1", server_id: "srv_1", name: "fx_rates", description: "Daily reference FX rates.",
    input_schema: {}, screening_status: "CLEAN", screening_reason_codes: [], status: "DISCOVERED",
    first_seen_at: "2026-09-25T00:00:00Z", last_seen_at: "2026-09-25T00:00:00Z",
  },
  {
    id: "t2", server_id: "srv_1", name: "ticket_lookup", description: null,
    input_schema: {}, screening_status: "QUARANTINED", screening_reason_codes: ["INSTRUCTION_OVERRIDE"],
    status: "DISCOVERED", first_seen_at: "2026-09-25T00:00:00Z", last_seen_at: "2026-09-25T00:00:00Z",
  },
];

beforeEach(() => {
  for (const mock of [fetchExternalMcpServers, registerExternalMcpServer, discoverExternalMcpTools, fetchExternalMcpTools]) {
    mock.mockReset();
  }
  fetchExternalMcpServers.mockResolvedValue([SERVER]);
  fetchExternalMcpTools.mockResolvedValue(TOOLS);
  sessionMe = asRoles("PlatformAdmin");
});

describe("UpstreamTab", () => {
  it("reads a server's tool list and shows each tool with its screening verdict", async () => {
    discoverExternalMcpTools.mockResolvedValue({
      server_id: "srv_1", listed: 2, new: 2, changed: 0, withdrawn: 0, quarantined: 1,
    });
    render(<UpstreamTab organizationId="org_1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Read tool list" }));
    expect(await screen.findByText(/2 listed, 2 new, 0 changed, 0 withdrawn, 1 quarantined/)).toBeInTheDocument();
    const tools = await screen.findByRole("list", { name: "Catalogued tools" });
    expect(within(tools).getByText("fx_rates")).toBeInTheDocument();
    expect(within(tools).getByText("quarantined")).toBeInTheDocument();
    expect(within(tools).getByText("Description withheld (INSTRUCTION_OVERRIDE).")).toBeInTheDocument();
    expect(discoverExternalMcpTools).toHaveBeenCalledWith("srv_1");
  });

  it("registers a server and shows the server's refusal as it came", async () => {
    registerExternalMcpServer.mockRejectedValueOnce(new Error("server URL refused: HOST_NOT_ALLOWLISTED"));
    render(<UpstreamTab organizationId="org_1" />);
    const form = await screen.findByRole("region", { name: "Register an upstream server" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "other" } });
    fireEvent.change(within(form).getByLabelText("Base URL (https, allowlisted host)"), {
      target: { value: "https://evil.example/mcp" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Register" }));
    expect(await within(form).findByText("server URL refused: HOST_NOT_ALLOWLISTED")).toBeInTheDocument();
    registerExternalMcpServer.mockResolvedValueOnce(SERVER);
    fireEvent.change(within(form).getByLabelText("Base URL (https, allowlisted host)"), {
      target: { value: "https://mcp.bank.internal/mcp" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Register" }));
    await waitFor(() =>
      expect(registerExternalMcpServer).toHaveBeenLastCalledWith("org_1", {
        name: "other", base_url: "https://mcp.bank.internal/mcp", credential_reference: null,
      }),
    );
  });

  it("offers an auditor reading only: no discovery, no registration", async () => {
    sessionMe = asRoles("Auditor");
    render(<UpstreamTab organizationId="org_1" />);
    expect(await screen.findByText("integration")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Read tool list" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Register an upstream server" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show tools" }));
    expect(await screen.findByText("fx_rates")).toBeInTheDocument();
  });
});
