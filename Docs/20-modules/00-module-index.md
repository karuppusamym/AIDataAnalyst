# Module Index

> Status: Authoritative. Owner: Architecture.
> One spec per bounded context defined in `10-architecture/04-module-decomposition.md`.
>
> **These specs describe target bounded contexts, not the current package layout** (stated
> 2026-08-30; the module-directory count corrected 2026-09-06). Six module directories now
> exist under `src/atlas/modules/` — `catalog`, `connectivity`, `identity_tenancy`,
> `ingestion` and `observability_audit`, each with real models, schemas and routes, plus
> `profiling`, relocated on 2026-09-06 under review point R04 with real models and schemas
> but no routes yet — and every other module's behaviour, where it exists at all, still
> lives in the flat `src/aida/` package. The six that exist have their own guides under
> [`domain-guides/`](domain-guides/), which say what is real in each and what is still an
> empty scaffold; the sentence this paragraph replaced ("one module directory exists…
> `identity_tenancy`, 69 lines") was true when written and had since become false.
> Wherever a spec below refers to
> `<module>/api.py`, `<module>/repository.py` and so on, it is describing the anatomy that
> module will have when it is extracted, not a file you can open today. Each spec's §11
> ("Current state → target") is the section that states what is actually built. Tracker
> items ST-05/06/07 are the extraction work; ST-02 (CI) and QG-7 (gateway exclusivity) landed
> 2026-08-30 and are the first boundaries that are now mechanically enforced rather than
> described.

## Two kinds of document live under this directory

The numbered specs below describe **target** bounded contexts — what each module
will own when it exists. The six guides under
[`domain-guides/`](domain-guides/) describe the six contexts that have a real
module directory today, and describe them as they actually are:

| Guide | Owns | Routes | Spec |
|---|---|---|---|
| [catalog](domain-guides/catalog.md) | The seven `metadata_*` tables, the catalog read model, bulk stewardship actions | 10 | [04](04-catalog.md) |
| [connectivity](domain-guides/connectivity.md) | Source registration, scan policy, certification runs | 7 | [02](02-connectivity.md) |
| [identity_tenancy](domain-guides/identity-tenancy.md) | Tenant hierarchy, workspaces, business hierarchy, delegation | 28 | [01](01-identity-and-tenancy.md) |
| [ingestion](domain-guides/ingestion.md) | Ingestion jobs, batches and chunks, and their state machine | 15 | [03](03-ingestion.md) |
| [observability_audit](domain-guides/observability-audit.md) | The audit ledger, outbox, archive, delivery intents, SLOs | 5 | [20](20-observability-and-audit.md) |
| [profiling](domain-guides/profiling.md) | Analysis runs and tasks, scan policy, value-free profiles, the value-profiling exception gate, classification evidence | 0 | [05](05-profiling-and-classification.md) |

Each guide answers four questions and stops: what the context owns, what must
stay true inside it, how you get into it, and what is deliberately somebody
else's problem. They are orientation, not specification — read the numbered spec
when you need the full contract.

The counts above and the shape of the import graph behind them are generated, not
typed, into
[`../10-architecture/14-generated-architecture-map.md`](../10-architecture/14-generated-architecture-map.md).
Where a context is still reached through a compatibility shim rather than its own
public face, the shim and the condition for removing it are recorded in
[`../40-engineering/09-compatibility-shim-register.md`](../40-engineering/09-compatibility-shim-register.md).

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
| [01](01-identity-and-tenancy.md) | identity-tenancy | L1 | Who is asking, on behalf of which part of the bank | **Yes** ([guide](domain-guides/identity-tenancy.md)) | `security.py`, `oidc.py`, `secrets.py`, `entitlements.py`, `domain_service.py`, `workspace_service.py`, `business_graph.py` · plus `src/atlas/modules/identity_tenancy/` (19 owned tables, 28 routes) |
| [02](02-connectivity.md) | connectivity | L1 | Reaching sources safely, with honest capabilities | **Yes** ([guide](domain-guides/connectivity.md)) | `connectors/` — 5 real drivers (`postgres`, `sqlserver`, `oracle`, `snowflake`, `bigquery`); `registry.py` declares Databricks/Teradata/Db2 **planned** |
| [03](03-ingestion.md) | ingestion | L1 | Getting metadata in, idempotently, at any scale | **Yes** ([guide](domain-guides/ingestion.md)) | `ingestion.py`, `ingestion_api.py`, `batch_ingestion.py`, `workflows/ingestion.py`, `fleet.py` |
| [04](04-catalog.md) | catalog | L2 | The authoritative inventory of the estate | **Yes** ([guide](domain-guides/catalog.md)) | `models.py` (`MetadataCatalog`/`Schema`/`Table`/`Column`/`Constraint`), `api.py`, `workflows/activities.py` |
| [05](05-profiling-and-classification.md) | profiling | L2 | What the data looks like, without looking at it | **Yes** ([guide](domain-guides/profiling.md)) | `workflows/activities.py` (`profile_table_task`, `classify_column_name`), `analysis_tasks.py` · plus `src/atlas/modules/profiling/` (9 owned tables, 0 routes — models and DTOs only) |
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
| [18](18-studio.md) | studio | L5 | Authoring semantics and tools with tests and version control | No | *(2026-08-30 snapshot: "Nothing. Zero matches for `studio` anywhere in `src/`.")* `studio.py`, `studio_api.py`, `studio_test_harness.py` now exist — see `60-delivery/00-status.md` §4 |
| [19](19-context-products-and-mcp.md) | context-products-mcp | L4 | Governed context for external agents | No | `mcp_server.py` (1,776 lines, real JSON-RPC 2.0), `mcp_budget.py`, `context_product_api.py`, `context_compiler.py`, `context_compiler_api.py`, `product_marketplace_api.py` |
| [20](20-observability-and-audit.md) | observability-audit | L1 | Evidence, telemetry, and the ledger | **Yes** ([guide](domain-guides/observability-audit.md)) | `events.py` (audit + outbox), `logging.py`, `operational_api.py`. **No OpenTelemetry export, no SIEM routing** despite the dependency being present |
| [21](21-experience-shell.md) | experience-shell | L5 | Persona-derived navigation and the product frame | No | `ui-next/` — React 18 + TypeScript SPA, 40 screens in `SCREEN_IDS`. **No server-side module.** *(The vanilla-JS `ui/` portal this row used to name was deleted on 2026-09-05; see D05 in `../review-2026-09-05/POINTS-TRACKER.md`.)* |

**How to read the last two columns (added 2026-08-30, sourced from the code; the "Module
dir?" column re-derived 2026-09-06).** "Module dir?" answers only *"does
`src/atlas/modules/<name>/` exist?"* — six now do, and each of those six links to its guide.
It still says nothing about whether the *capability* is built: modules 16 and 19 are among the
strongest-implemented parts of the platform and have no module directory at all, while several
of the six that do have one still hold their business rules in the router with `service.py`
and `repository.py` left as empty scaffolds — and `profiling` has no router either, only
models and DTOs. Each guide says which. Capability status per module is in that module's own
"Current state → target" section, and the two are independent axes.

**The last column is a dated snapshot, not a living status field.** It was sourced from the code on
2026-08-30 and has not been re-derived row by row since. Two of its claims were corrected on
2026-09-06 because they had become false — module 18 ("no code of any kind") and module 21 ("`ui/`
only", a portal that has since been deleted) — but the rest were not re-audited, and module 12's
"lexical half only" and module 17's "ABAC and bulk decisions not implemented" are both known to
have moved on. For what is true today, use
[`../60-delivery/20-capability-register.md`](../60-delivery/20-capability-register.md), which keeps
*implemented*, *reachable*, *configured* and *verified* as separate columns; this index answers
"what does each module own", which is a different question.

## Reading order

- **New engineer:** 01 → 04 → 16 → 13. These four explain the trust model end to end.
- **Product:** 13 → 14 → 19 → 11. These four are the differentiation.
- **Security review:** 01 → 17 → 16 → 15 → 13.
- **Operations:** 02 → 03 → 20 → 10.
