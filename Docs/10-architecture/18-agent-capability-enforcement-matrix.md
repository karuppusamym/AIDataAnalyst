# Agent capability enforcement matrix

> Status: Authoritative as of 2026-09-11. Produced for AR-06 in `15-agent-architecture-critical-review.md`.
> The matrix cites symbols, not line numbers, because line numbers drift with every edit. Re-derive a row whenever its surface or one of its checks changes.

An `AgentContract` declares what an agent may do:

- the governed tools it may call (`capability_envelope.tool_slugs`);
- the context products it may read (`context_product_ids`);
- the write lanes it may use (`write_lanes`);
- daily and per-run token caps, and a wall-clock cap;
- a kill switch;
- a sampling rate.

A declaration is not a control. AR-06 asked which of these are checked, and where. This page answers that for every surface a non-human principal can reach.

## How an agent is identified

- **Authentication** is `security.get_security_context` everywhere.
  - In OIDC mode, `principal_type` is one of `USER`, `SERVICE_ACCOUNT`, `AGENT` or `WORKER`.
  - In development mode, the default, it is an unvalidated `X-Principal-Type` header.
- **Role checks ignore principal type.** `require_roles` looks only at roles, so an agent holding a role is served like a person holding it.
- **Only two places branch on principal type:**
  - the MCP workload-identity gate, which admits only `AGENT` and `SERVICE_ACCOUNT` and is skipped in development;
  - policy rows that name a `principal_kind`.
- **A contract is found in one of two ways:**
  - by asset version: `load_agent_contract`, used by the orchestrator, task agents and the drafter;
  - by principal: `load_contract_for_principal`, used at a boundary. It returns `None` for a human, and refuses (`agent_contract_unresolved`) an `agent:` identity that has no contract or has more than one.

## The matrix

Key: **yes** means checked at runtime on that surface; **no** means not checked; **n/a** means the check has no meaning there. "Contract" means contract existence plus the kill switch.

| Surface | Contract | `tool_slugs` | `context_product_ids` | Token caps | Notes |
|---|---|---|---|---|---|
| MCP `tools/call`, governed `atlas__<slug>` tool | **yes, since 2026-09-11** | **yes, since 2026-09-11** | only when `contextProductUri` is sent | **yes, since 2026-09-11** | The orchestrator enforced all four, but only when told which contract, and this path never told it. It now resolves the caller's contract and passes it; an unresolved `agent:` identity is refused and audited (`mcp.tool_call.agent_contract_denied`). |
| REST `POST /datasources/{id}/agent-analyses` | **yes, since 2026-09-11** | **yes, since 2026-09-11**, on the governed-tool strategy | no | **yes, since 2026-09-11** | Same fix as the row above; an unresolved `agent:` identity gets 403. |
| MCP native tools (lineage, marketplace, `validate_sql`) | no | no | refused when a product scope is sent | no | These bypass the orchestrator, so no contract check reaches them. |
| MCP `tools/list` | only when `contextProductUri` is sent | no; the list is not filtered | only when `contextProductUri` is sent | n/a | An agent is shown tools it may not call. The call itself is refused (row 1). |
| MCP `resources/list` / `resources/read` (catalog) | no | n/a | n/a | n/a | `resources/list` returns table names to any role. |
| MCP `resources/read` (context product), `prompts/get` | no | n/a | **no** | n/a | `agent_contracts.context_product_violation`'s docstring says the MCP resource path is enforced. It is not. |
| REST tool execute, tool plans, context-product read and compile | no | no | no | no | These are guarded by roles only. |
| REST `POST /governance/reviews/{id}/decision` | no | n/a | n/a | n/a | An external agent holding the Reviewer role can decide any review. That skips the in-process agent's tier ceiling, sampling, suspension and backlog. |
| REST contract `PUT` / kill / release | n/a | n/a | n/a | n/a | Declaring `write_lanes` is refused at validation on every writer (`envelope_write_lane_unenforceable`). An `AgentDeveloper` agent can edit another agent's contract. |
| In-process reviewer agent | its own enable, suspend and backlog flags | n/a | n/a | n/a | It has a tier allowlist and maker ≠ checker. It holds no contract, by design: it is configured, not contracted. |
| In-process task agents (steward, lineage, quality) | yes | no | no | wall-clock only | `open_review` enforces object type and tier. The token caps are never read. |
| Newly-created-table drafter | yes, if a steward contract exists | no | no | no | It inserts reviews directly, bypassing the `open_review` ceiling and the backlog bound. Without a contract it runs as `WORKER`. |

## Write paths

No lane-based check bounds any write, because nothing reads `write_lanes` and declaring one is refused. What does bound each write:

| Write path | What bounds it |
|---|---|
| Marketplace access request (MCP and REST) | role, organization, product status, duplicate check |
| Governance decision | role, organization, maker ≠ checker |
| Contract edits | the `AgentDeveloper` role |
| Reviewer agent | tier allowlist, maker ≠ checker |
| Task agents | object type and tier via `open_review` |
| Drafter | none of the above |

## Where agents are treated differently from people, unintentionally

| # | Finding | Status |
|---|---|---|
| 1 | Contracted agents got human treatment on the two live orchestration paths: no `tool_slugs`, kill switch or token caps. | **Fixed 2026-09-11.** `tests/test_ar06_contract_on_live_paths.py` |
| 2 | `context_product_ids` applies only when the caller volunteers a product scope, and not on `resources/read`, `prompts/get` or the REST context-product paths. | Open |
| 3 | Outside MCP, an `agent:` principal with no contract is served like any role holder. | Fixed on the two orchestration paths (#1); open elsewhere. |
| 4 | The contract kill switch does not stop native MCP tools, REST tool execution or the reviewer agent. The reviewer agent has its own suspension. | Open |
| 5 | An external agent holding the Reviewer role decides reviews without the in-process agent's tier ceiling, sampling or suspension. | Open, and the most consequential gap left. |
| 6 | An `AgentDeveloper` agent can rewrite other agents' contracts and release their kill switches. | Open |
| 7 | No live path read the token caps. | Fixed on the two orchestration paths (#1). Task agents still enforce wall-clock only. |
| 8 | The drafter runs under different rules from the steward agent for the same object type. | Open, and documented at the drafter. |
| 9 | In development mode the workload gate is off and principal type is a free header. | By design for local development; it must not be the production configuration. |

Findings 2, 4, 5 and 6 are what keep AR-06 open.
