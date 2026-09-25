/* ---------------------------------------------------------------------------
   R11-MP10: upstream MCP servers, catalogued (`aida.external_mcp_api`).

   Register a server on an allowlisted host (PlatformAdmin), read its tool list
   into the catalogue (PlatformAdmin, AgentDeveloper), and read what it lists
   (those two plus DataSteward and Auditor). Discovery only: nothing here -- or
   anywhere in the platform -- invokes an upstream tool.
--------------------------------------------------------------------------- */

import type {
  ExternalMcpDiscoveryRead,
  ExternalMcpServerCreate,
  ExternalMcpServerRead,
  ExternalMcpToolRead,
} from "../types";
import { demoOr, get, postJson } from "./transport";

export function fetchExternalMcpServers(
  organizationId: string,
  signal?: AbortSignal,
): Promise<ExternalMcpServerRead[]> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureExternalMcpServers(),
    async () =>
      get<ExternalMcpServerRead[]>(
        `/v1/organizations/${encodeURIComponent(organizationId)}/external-mcp-servers`,
        signal,
      ),
  );
}

export function registerExternalMcpServer(
  organizationId: string,
  body: ExternalMcpServerCreate,
): Promise<ExternalMcpServerRead> {
  return demoOr(
    async (fixtures) => fixtures.registerFixtureExternalMcpServer(body),
    async () =>
      postJson<ExternalMcpServerRead>(
        `/v1/organizations/${encodeURIComponent(organizationId)}/external-mcp-servers`,
        body,
      ),
  );
}

export function discoverExternalMcpTools(serverId: string): Promise<ExternalMcpDiscoveryRead> {
  return demoOr(
    async (fixtures) => fixtures.discoverFixtureExternalMcpTools(serverId),
    async () =>
      postJson<ExternalMcpDiscoveryRead>(
        `/v1/external-mcp-servers/${encodeURIComponent(serverId)}/discover`,
        {},
      ),
  );
}

export function fetchExternalMcpTools(
  serverId: string,
  signal?: AbortSignal,
): Promise<ExternalMcpToolRead[]> {
  return demoOr(
    async (fixtures) => fixtures.makeFixtureExternalMcpTools(serverId),
    async () =>
      get<ExternalMcpToolRead[]>(
        `/v1/external-mcp-servers/${encodeURIComponent(serverId)}/tools`,
        signal,
      ),
  );
}
