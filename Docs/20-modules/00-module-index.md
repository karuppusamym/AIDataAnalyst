# Module Index

> Status: Authoritative. Owner: Architecture.
> One spec per bounded context defined in `10-architecture/04-module-decomposition.md`.
>
> **These specs describe target bounded contexts, not the current package layout** (stated
> 2026-08-30). One module directory exists under `src/atlas/modules/` — `identity_tenancy`,
> 69 lines, labelled "scaffold only" — and every other module's behaviour, where it exists at
> all, lives in the flat `src/aida/` package. Wherever a spec below refers to
> `<module>/api.py`, `<module>/repository.py` and so on, it is describing the anatomy that
> module will have when it is extracted, not a file you can open today. Each spec's §11
> ("Current state → target") is the section that states what is actually built. Tracker
> items ST-05/06/07 are the extraction work; ST-02 (CI) and QG-7 (gateway exclusivity) landed
> 2026-08-30 and are the first boundaries that are now mechanically enforced rather than
> described.

## Spec template

Every module spec follows the same sections, so a reader can find the same fact in the same place in any module:

1. **Purpose** — one paragraph; why this module exists as a separate context.
2. **Jobs served** — persona job IDs from `00-product/02-personas-and-jobs.md`.
3. **Responsibilities / Not responsibilities** — the boundary, stated both ways.
4. **Domain model** — owned entities.
5. **Public interface** — what `<module>/api.py` exposes.
6. **HTTP surface** — routes owned by this module.
7. **Events** — emitted and consumed.
8. **Dependencies** — modules this one may call.
9. **Workers** — background work owned here.
10. **Controls and invariants** — which invariants this module enforces.
11. **Current state → target** — honest gap.
12. **Open work** — tracked items.

## Index

| # | Module | Layer | Purpose in one line | Module dir? | Lives today in (`src/aida/` unless noted) |
|---|---|---|---|---|---|
| [01](01-identity-and-tenancy.md) | identity-tenancy | L1 | Who is asking, on behalf of which part of the bank | **Scaffold** | `security.py`, `oidc.py`, `secrets.py`, `entitlements.py`, `domain_service.py`, `workspace_service.py`, `business_graph.py` · plus `src/atlas/modules/identity_tenancy/` (69 lines, no logic) |
| [02](02-connectivity.md) | connectivity | L1 | Reaching sources safely, with honest capabilities | No | `connectors/` — 5 real drivers (`postgres`, `sqlserver`, `oracle`, `snowflake`, `bigquery`); `registry.py` declares Databricks/Teradata/Db2 **planned** |
| [03](03-ingestion.md) | ingestion | L1 | Getting metadata in, idempotently, at any scale | No | `ingestion.py`, `ingestion_api.py`, `batch_ingestion.py`, `workflows/ingestion.py`, `fleet.py` |
| [04](04-catalog.md) | catalog | L2 | The authoritative inventory of the estate | No | `models.py` (`MetadataCatalog`/`Schema`/`Table`/`Column`/`Constraint`), `api.py`, `workflows/activities.py` |
| [05](05-profiling-and-classification.md) | profiling | L2 | What the data looks like, without looking at it | No | `workflows/activities.py` (`profile_table_task`, `classify_column_name`), `analysis_tasks.py` |
| [06](06-relationship-intelligence.md) | relationships | L2 | How tables connect, with evidence and negative knowledge | No | `intelligence_api.py` (`RelationshipCandidate`). Request-path, not a worker |
| [07](07-semantic-layer.md) | semantic-layer | L2 | What the data means, versioned and approved | No | `semantic_api.py`, `semantic_inference.py`, `semantic_intelligence_api.py` |
| [08](08-glossary-and-stewardship.md) | glossary-stewardship | L2 | Who owns meaning, and how disagreement is resolved | No | `glossary_api.py`, `stewardship_api.py`, `stewardship_service.py` |
| [09](09-lineage.md) | lineage | L2 | Where data came from — and why the agent chose it | No | `unified_lineage.py`, `unified_lineage_api.py`, `lineage_cache.py`, `graph_store.py` (formerly lineage_graph_store.py), `openlineage.py`, `dbt_artifacts.py`. **No view-DDL, procedure or query-log parser exists** |
| [10](10-knowledge-graph.md) | knowledge-graph | L2 | Bounded, value-free traversal of the estate | No | `knowledge_graph.py`, `projectors/graph_projector.py` (Neo4j) |
| [11](11-data-quality.md) | data-quality | L2 | Whether the data can be trusted right now | No | `data_quality.py`, `quality_api.py`, `quality_service.py`, `dbt_quality_bridge.py`. `gap/02` D4 proposes folding this into profiling + policy |
| [12](12-retrieval-and-search.md) | retrieval | L3 | Finding the right context, policy-filtered before ranking | No | `retrieval.py` — **lexical BM25 only**; no vector, no graph expansion, no fusion |
| [13](13-agent-runtime.md) | agent-runtime | L3 | The governed analytical state machine | No | `agent_orchestrator.py`, `agent_runtime.py`, `agent_intelligence.py`, `agent_evals.py`, `prompt_risk.py` |
| [14](14-tool-registry.md) | tool-registry | L3 | Turning analysis into reusable governed capability | No | `tool_api.py`, `tool_rendering.py` (AST literal binding — verified real) |
| [15](15-model-gateway.md) | model-gateway | L3 | Provider-neutral, budgeted, fail-closed model access | No | `model_gateway.py` |
| [16](16-query-gateway.md) | query-gateway | L3 | The one path to a source | No | `query_gateway.py`, `sql_guard.py`, `connectors/execution_access.py`. The strongest-built module; INV-2 enforced by import-linter since 2026-08-30 |
| [17](17-policy-and-governance.md) | policy-governance | L1 | Policy, entitlement, and maker-checker as primitives | No | `policy_engine.py`, `context_product_policy.py`, `integration_service.py`, `ai_governance_api.py`. Maker≠checker real and tested; **ABAC and bulk decisions not implemented** |
| [18](18-studio.md) | studio | L5 | Authoring semantics and tools with tests and version control | No | **Nothing.** Zero matches for `studio` anywhere in `src/` or `ui/`. Entirely greenfield |
| [19](19-context-products-and-mcp.md) | context-products-mcp | L4 | Governed context for external agents | No | `mcp_server.py` (1,776 lines, real JSON-RPC 2.0), `mcp_budget.py`, `context_product_api.py`, `context_compiler.py`, `context_compiler_api.py`, `product_marketplace_api.py` |
| [20](20-observability-and-audit.md) | observability-audit | L1 | Evidence, telemetry, and the ledger | No | `events.py` (audit + outbox), `logging.py`, `operational_api.py`. **No OpenTelemetry export, no SIEM routing** despite the dependency being present |
| [21](21-experience-shell.md) | experience-shell | L5 | Persona-derived navigation and the product frame | No | `ui/` only — vanilla JS, no framework, `app.js` plus 4 feature modules. **No server-side module** |

**How to read the last two columns (added 2026-08-30, sourced from the code).** "Module dir?"
answers only *"does `src/atlas/modules/<name>/` exist?"* — for 20 of 21 the answer is No, and
for the one exception it is a scaffold with no business rules. It says nothing about whether the
*capability* is built: modules 16 and 19 are among the strongest-implemented parts of the
platform and have no module directory at all, while module 01 has the only directory and the
least of it filled in. Capability status per module is in that module's own
"Current state → target" section, and the two are independent axes.

**Two rows are worth reading twice.** Module 18 has no code of any kind. Module 12 is
implemented but only in its lexical half, and the missing half (vector, graph expansion,
fusion) is what several other modules' target behaviour depends on.

## Reading order

- **New engineer:** 01 → 04 → 16 → 13. These four explain the trust model end to end.
- **Product:** 13 → 14 → 19 → 11. These four are the differentiation.
- **Security review:** 01 → 17 → 16 → 15 → 13.
- **Operations:** 02 → 03 → 20 → 10.
