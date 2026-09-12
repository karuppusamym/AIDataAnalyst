# Agent capability enforcement matrix

> Status: Authoritative as of 2026-09-12. Produced for AR-06 in `15-agent-architecture-critical-review.md`.
> The matrix cites symbols, not line numbers, because line numbers drift with every edit. Re-derive a row whenever its surface or one of its checks changes.
> 2026-09-12 (R11-C6) re-measured five rows: the MCP context-product resource/prompt path, the REST context-product reads, the native MCP tools, and both contract-write rows.

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
| MCP native tools (lineage, marketplace, `validate_sql`) | **yes, since 2026-09-12** | no, and deliberately | refused when a product scope is sent | no | R11-C6. All seven dispatched *above* the contract resolution the governed path does, so an engaged kill switch stopped nothing here. `mcp_server._native_tool_contract_denial` now resolves the contract and applies `agent_kill_blocking_reason` before the dispatch; an unresolved `agent:` identity is refused and audited (`mcp.native_tool.agent_contract_denied`). `tool_slugs` names `GovernedToolVersion` slugs and a native tool has none, so it is *not* read here — see finding 10. Five of the seven are read-only value-free metadata; `request_data_product_access` writes (a maker-checker access request) and `validate_sql` reaches the source for a dry-run estimate, which is why this is a check and not a documentation note. |
| MCP `tools/list` | only when `contextProductUri` is sent | no; the list is not filtered | only when `contextProductUri` is sent | n/a | An agent is shown tools it may not call. The call itself is refused (row 1). |
| MCP `resources/list` / `resources/read` (catalog) | no | n/a | n/a | n/a | `resources/list` returns table names to any role. |
| MCP `resources/read` (context product), `prompts/get` | **yes, since 2026-09-11** | n/a | **yes, since 2026-09-11** | n/a | R11-C6. Was role-only, so an agent whose envelope named product A could read product B as a resource or a prompt. `_read_context_product_resource` now resolves the contract and refuses before any lifecycle disclosure (`mcp.context_product.envelope_denied`). This row previously said the fix did not exist; it did, and the row was stale. |
| REST context-product reads (`GET /context-product-versions/{id}`, its `/scope`, `GET /context-products/{id}/versions`, `GET /context-products/{id}/bindings`) | **yes, since 2026-09-12** | n/a | **yes, since 2026-09-12** | n/a | R11-C6. The same products through a different transport, and which door an agent knocks on is not a governance boundary. `context_product_api._enforce_capability_envelope`, placed above the retirement branch so an out-of-envelope agent cannot use the 410 disclosure to confirm a version exists. The AR-10 *outbound screening* that accompanies the MCP check is deliberately **not** applied: REST serves the UI, and the screening design keeps quarantined text visible to a human looking at the object. |
| REST tool execute, tool plans, context-product *compile*, project-level context-product listing | no | no | no | no | These are guarded by roles only. The project-level listing (`GET /projects/{id}/context-products`) needs a *filter* rather than a gate, and is the remaining envelope-free context-product surface. |
| REST `POST /governance/reviews/{id}/decision` | no | n/a | n/a | n/a | An external agent holding the Reviewer role can decide any review. That skips the in-process agent's tier ceiling, sampling, suspension and backlog. |
| REST contract `PUT` / kill / release | n/a | n/a | n/a | n/a | Declaring `write_lanes` is refused at validation on every writer (`envelope_write_lane_unenforceable`). **Since 2026-09-12 (R11-C6)** a direct `PUT` is bound to the agent version's registered owner (`_require_agent_steward`, `PlatformAdmin` break-glass, audited) and any edit that *widens* authority is refused with 409 and routed to the reviewed path (`contract_widening`). Releasing a kill switch adds maker != checker against the engaging principal, read from `AuditEvent`. Engaging stays open to every `CONTRACT_AUTHORS` principal, deliberately. |
| In-process reviewer agent | its own enable, suspend and backlog flags | n/a | n/a | n/a | It has a tier allowlist and maker ≠ checker. It holds no contract, by design: it is configured, not contracted. |
| In-process task agents (steward, lineage, quality) | yes | no | no | wall-clock only | `open_review` enforces object type and tier. The token caps are never read. |
| Newly-created-table drafter | yes, if a steward contract exists | no | no | no | It inserts reviews directly, bypassing the `open_review` ceiling and the backlog bound. Without a contract it runs as `WORKER`. |

## Write paths

No lane-based check bounds any write, because nothing reads `write_lanes` and declaring one is refused. What does bound each write:

| Write path | What bounds it |
|---|---|
| Marketplace access request (MCP and REST) | role, organization, product status, duplicate check |
| Governance decision | role, organization, maker ≠ checker |
| Contract edits | the `AgentDeveloper` role, the agent version's registered owner, and — for any widening — the reviewed `AGENT_CONTRACT_REQUEST` path (maker != checker plus a live eval gate) |
| Kill-switch release | the registered owner (or `PlatformAdmin`), and maker != checker against the principal who engaged it |
| Reviewer agent | tier allowlist, maker ≠ checker |
| Task agents | object type and tier via `open_review` |
| Drafter | none of the above |

## Where agents are treated differently from people, unintentionally

| # | Finding | Status |
|---|---|---|
| 1 | Contracted agents got human treatment on the two live orchestration paths: no `tool_slugs`, kill switch or token caps. | **Fixed 2026-09-11.** `tests/test_ar06_contract_on_live_paths.py` |
| 2 | `context_product_ids` applies only when the caller volunteers a product scope, and not on `resources/read`, `prompts/get` or the REST context-product paths. | **Fixed.** MCP resource/prompt 2026-09-11; the three version-addressed REST reads 2026-09-12 (`tests/test_r11c6_rest_context_product_envelope.py`). The project-level product listing is still a role-only filter — see finding 11. |
| 3 | Outside MCP, an `agent:` principal with no contract is served like any role holder. | Fixed on the two orchestration paths (#1), on the REST context-product reads and on the native MCP tools (2026-09-12); open elsewhere. |
| 4 | The contract kill switch does not stop native MCP tools, REST tool execution or the reviewer agent. The reviewer agent has its own suspension. | **Native MCP tools fixed 2026-09-12** (`tests/test_r11c6_native_mcp_tool_contract.py`). REST tool execution still open; the reviewer agent has its own suspension, by design. |
| 5 | An external agent holding the Reviewer role decides reviews without the in-process agent's tier ceiling, sampling or suspension. | Open, and the most consequential gap left. |
| 6 | An `AgentDeveloper` agent can rewrite other agents' contracts and release their kill switches. | **Fixed 2026-09-12** (`tests/test_r11c6_agent_contract_authority.py`). Ownership binds the write; widening goes to review; releasing a kill switch is maker != checker. |
| 7 | No live path read the token caps. | Fixed on the two orchestration paths (#1). Task agents still enforce wall-clock only. |
| 8 | The drafter runs under different rules from the steward agent for the same object type. | Open, and documented at the drafter. |
| 9 | In development mode the workload gate is off and principal type is a free header. | By design for local development; it must not be the production configuration. |
| 10 | The native MCP tools hold no per-agent allowlist. `tool_slugs` names `GovernedToolVersion` slugs and a native tool has none, so reading the field there would redefine it for every stored contract. `validate_sql` (reaches the source for a dry-run estimate) and `request_data_product_access` (writes a maker-checker access request) are bounded by roles, per-object gateway authorization, maker-checker and the kill switch — but not by an allowlist naming *this* agent. | Open, and a contract-schema change rather than a check. Recorded 2026-09-12 so the omission is a decision, not an oversight. |
| 11 | `GET /projects/{id}/context-products` lists a project's products under roles alone, so an agent whose envelope names one product still sees the others exist. It needs a *filter*, not a gate — the version-addressed reads beside it now have the gate. | Open, recorded 2026-09-12. |

Findings 4 (REST tool execution) and 5 are what keep AR-06 open; 10 and 11 are the
narrower residuals R11-C6 left behind deliberately, both stated with their reasons
above rather than left to be rediscovered.
