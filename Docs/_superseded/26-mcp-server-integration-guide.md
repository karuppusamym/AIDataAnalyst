# Atlas MCP Server — Integration Guide

> **Status**: Implemented (`src/aida/mcp_server.py`)  
> **Endpoint**: `POST /mcp`  
> **Protocol**: Model Context Protocol (MCP) JSON-RPC 2.0  
> **Auth**: Same OIDC Bearer token as the REST API  

---

## 1. What is the Atlas MCP Server?

The Model Context Protocol (MCP) is a standard JSON-RPC 2.0 protocol for exposing data catalog metadata, tools, and resources to AI agents (Claude Desktop, Cursor, Agentforce, and custom LLM clients). Instead of giving AI agents raw database credentials, agents query the Atlas MCP server — and **every tool call routes through our full governed execution gateway** (prompt-risk screening → SQL AST guard → cost check → PII masking → immutable audit).

### Why this beats Atlan MCP
| Feature | Atlan MCP Server | Atlas MCP Server |
|---|---|---|
| Exposes | Metadata context (tags, lineage, descriptions) | **Metadata + executed governed SQL tools** |
| Query execution | Passes context to external LLM; LLM calls DB directly | **Every tool call routes through Atlas gateway** |
| PII Masking | No (relies on downstream DB GRANTs) | **Yes — deterministic row-level masking** |
| Audit Trail | Partial (catalog audit only) | **Immutable audit record per tool call** |
| Auth | Atlan API key | **OIDC token — same as REST API** |

---

## 2. Endpoint & Authentication

```
POST http://localhost:8000/mcp
Content-Type: application/json
Authorization: Bearer <OIDC_TOKEN>
X-Organization-Id: <ORG_UUID>
X-Principal-Id: <USER_UUID>
X-Principal-Type: SERVICE_ACCOUNT
X-Roles: analyst
```

---

## 3. MCP Methods

### `initialize` — Capability negotiation
```json
{
  "jsonrpc": "2.0", "id": 1, "method": "initialize",
  "params": { "protocolVersion": "2025-03-26", "clientInfo": {"name": "claude"} }
}
```
**Response**:
```json
{
  "result": {
    "protocolVersion": "2025-03-26",
    "capabilities": { "tools": {}, "resources": {} },
    "serverInfo": { "name": "atlas-governed-data-platform", "version": "1.0.0" }
  }
}
```

---

### `tools/list` — List all published governed tools
```json
{ "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {} }
```
**Response** (example):
```json
{
  "result": {
    "tools": [
      {
        "name": "atlas__get_quarterly_revenue_by_region",
        "description": "Returns reconciled gross revenue aggregated by risk region for a given quarter.\n\n⚠ Governed: Executes through the Atlas deterministic SQL gateway.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "year":    { "type": "integer", "description": "Fiscal year (e.g. 2026)" },
            "quarter": { "type": "integer", "description": "Quarter number 1-4" }
          },
          "required": ["year", "quarter"]
        }
      }
    ]
  }
}
```

---

### `tools/call` — Execute a governed tool
```json
{
  "jsonrpc": "2.0", "id": 3, "method": "tools/call",
  "params": {
    "name": "atlas__get_quarterly_revenue_by_region",
    "arguments": { "year": 2026, "quarter": 2 }
  }
}
```
**Response** (example):
```json
{
  "result": {
    "content": [
      {
        "type": "text",
        "text": "✅ Governed Execution Complete\n- Tool: Get Quarterly Revenue by Region v3\n- Rows returned: 4\n- Masked columns: none\n- Execution ID: `a1b2c3...`"
      },
      {
        "type": "text",
        "text": "```json\n[\n  {\"risk_region\": \"North America\", \"gross_revenue\": 14250000},\n  {\"risk_region\": \"EMEA\", \"gross_revenue\": 9810000}\n]\n```"
      }
    ]
  }
}
```

**Execution pipeline** (in order):
1. Resolve `atlas__<slug>` → `GovernedToolVersion` (must be `PUBLISHED`)
2. Resolve `DataSource` for caller's org
3. `GovernedAgentOrchestrator.run()`:
   - Prompt risk screening (blocks injection attempts)
   - SQL template rendering with typed parameter substitution
   - `QueryExecutionGateway`: AST guard → cost check → execution → PII masking
   - Immutable `QueryExecution` + `AgentRun` audit records

---

### `resources/list` — List catalog metadata assets
```json
{ "jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {} }
```
Returns up to 500 active tables as `atlas://catalog/{datasource_id}/{schema}/{table}` URIs.

---

### `resources/read` — Read value-free metadata for a resource
```json
{
  "jsonrpc": "2.0", "id": 5, "method": "resources/read",
  "params": { "uri": "atlas://catalog/d1e2f3.../public/fact_daily_transactions" }
}
```
Returns schema, column names, types, nullable flags, and PII classifications. **Never returns raw source values.**

---

## 4. Claude Desktop Configuration

Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "atlas": {
      "command": "curl",
      "args": ["-X", "POST", "http://localhost:8000/mcp"],
      "env": {
        "ATLAS_TOKEN": "<your-oidc-token>"
      }
    }
  }
}
```

Or use `npx @modelcontextprotocol/sdk` as an HTTP proxy pointing to `http://localhost:8000/mcp`.

---

## 5. Governance Guarantees

Every MCP tool call:
- Passes through `DeterministicPromptRiskClassifier` (7 regex safety signals)
- Validates SQL AST (read-only, no wildcards, LIMIT enforced)
- Checks query cost against `max_query_estimate_cost`
- Applies row-level PII/PHI/PCI masking
- Writes an immutable `QueryExecution` and `AgentRun` record
- Emits a `query.execution.completed.v1` Kafka outbox event

This means Claude or any external agent **cannot bypass Atlas governance** through MCP.
