# Module 19 — Context Products and MCP

> Layer L4 · Schema `context_products` · Owner: AI Platform

## 1. Purpose

Packages governed context — glossary, semantics, lineage, policy, quality signals, and tool eligibility — for consumption by **external** AI clients over MCP, with the same policy enforcement the native analyst gets.

This is whitespace **W2**. Every competitor now ships an MCP server. What none of them does is **keep governing at consumption**: they authenticate, hand over context, and stop. Atlas evaluates policy on every read, records consumption as lineage, and exposes tool eligibility rather than raw context.

## 2. Jobs served

A1 (from an external surface), P5 (operator control over external consumption), S1, B3.

## 3. Responsibilities

- Context product definition, versioning, and publication.
- MCP server exposing products as resources and tools.
- Per-read policy evaluation and tenancy enforcement.
- Consumption recording as lineage and audit evidence.
- Rate limiting and budgets per consumer.
- Value-freedom enforcement at the boundary.

## 4. Not responsibilities

| Not this module | Where it lives |
|---|---|
| Owning the context | The originating modules |
| Executing queries for external agents | 16 query-gateway, via governed tools only |
| Authenticating the external client | 01 identity-tenancy |
| Being an agent | The external client is the agent |

## 5. Why "context product" and not "an API"

A raw metadata API hands over everything the caller is entitled to and forgets about it. A context product is a **curated, versioned, governed package** with an owner and a purpose.

| Property | Raw API | Context product |
|---|---|---|
| Scope | Whatever the caller requests | A defined, reviewed set |
| Versioning | Endpoint version | Product version pinned by the consumer |
| Ownership | The platform team | A named steward |
| Approval | None | Maker-checker before publication |
| Consumption record | An access log | **Lineage edges** |
| Policy | At the endpoint | **At every read** |
| Purpose | Implicit | Declared and enforced |

## 6. What a context product contains

```text
context_product
├── scope           (domains, entities, assets — bounded)
├── semantics       (approved annotations, metrics, grain, join rules)
├── glossary        (approved terms, synonyms)
├── lineage_summary (bounded upstream/downstream)
├── quality_signals (trust state per included asset)
├── policy_summary  (what the consumer may and may not do)
└── eligible_tools  (governed tools invocable in this context)
```

**`eligible_tools` is the differentiating element.** Competitors hand an external agent context and let it generate SQL in its own environment. Atlas hands it context *plus a list of approved capabilities it may invoke through the governed gateway*. The external agent gets more power and less freedom — which is exactly the trade a bank wants.

## 7. Governance at consumption

```mermaid
sequenceDiagram
    participant C as External MCP client
    participant M as Atlas MCP server
    participant P as Policy (module 17)
    participant R as Retrieval (module 12)
    participant Q as Query gateway (module 16)
    participant L as Lineage (module 09)

    C->>M: read context product v3
    M->>P: authorize(principal, read, product, purpose)
    P-->>M: allow (policy_version pinned)
    M->>R: fetch bounded, policy-filtered context
    M->>L: record consumption edge
    M-->>C: context + eligible tools + policy summary

    C->>M: invoke eligible tool
    M->>P: authorize(principal, invoke, tool)
    M->>Q: ExecutionRequest (typed params)
    Q-->>M: bounded, masked result
    M->>L: record execution + decision lineage
    M-->>C: result + evidence
```

Note that an external agent's tool invocation goes through the **same query gateway** as a native run (INV-2). There is no external execution path.

## 8. Value-freedom at the boundary

| Crosses to an external client | Never crosses |
|---|---|
| Approved annotations and descriptions | Sample values |
| Metric and dimension definitions | Credentials |
| Bounded lineage summaries | Other tenants' anything |
| Quality trust signals | Unapproved drafts |
| Policy summaries | Raw SQL of unpublished tools |
| Bounded, masked tool results | Anything outside the caller's authorization scope |

## 9. Public interface

```python
# context_products/api.py
def list_products(scope) -> list[ContextProductDTO]
def get_product(scope, product_id, version=None) -> ContextProductDTO
def publish_product(scope, draft_id) -> ProposalDTO        # via module 17
def read_product(principal, product_id, version) -> ContextPayload | Denial
def list_eligible_tools(principal, product_id) -> list[ToolDTO]
def get_consumption(scope, product_id, page) -> Page[ConsumptionDTO]
```

## 10. MCP surface

| MCP concept | Atlas mapping |
|---|---|
| Resource | A context product version |
| Resource read | Policy-evaluated, lineage-recorded context fetch |
| Tool | An eligible governed tool |
| Tool call | `ExecutionRequest` through the query gateway |
| Prompt | Curated analytical question templates from approved annotations |

## 11. Events

Emits `context.product_published|deprecated`, `context.product_consumed`, `context.consumption_denied`, `context.budget_exceeded`.

## 12. Dependencies

12 retrieval, 17 policy-governance (plus 14 tool-registry and 16 query-gateway for tool invocation).

## 13. Current state → target

**Implemented and production-hardened.** `src/aida/mcp_server.py` provides JSON-RPC 2.0 resources, prompts, governed/native tools, atomic Redis budgets, workload-identity enforcement outside development, purpose ABAC, and immutable value-free consumption evidence. Tool calls do **not** bypass the gateway: governed execution still runs through `GovernedAgentOrchestrator` and `QueryExecutionGateway`. Context Product reads remain lifecycle-, role-, purpose-, and quality-gated with anti-enumeration denials.

| Aspect | Now | Target |
|---|---|---|
| MCP server | **Implemented** — JSON-RPC 2.0 over `POST /mcp`; resources, prompts, and tools route through governed policy and evidence controls | Add workload identity, bank-scale certification, and browser-accessibility QA for the supporting portal surfaces |
| Context products | **Implemented** — immutable versions, maker-checker publication/deprecation, REST/MCP reads, quality policy, UI, compiler, and marketplace | Certify external entitlement and compiler providers |
| Per-read policy | Implemented — tenancy, role, lifecycle, exact product scope, purpose allowlists, and quality gates are enforced | Extend shared ABAC vocabulary beyond Context Products |
| Consumption lineage | Implemented for MCP — successful resources, prompts, and tools persist generic immutable evidence; Context Product reads also persist product-specific edges | Add broader retention, BI/procedure lineage projection, and enterprise-scale certification |
| Eligible tools exposure | **Implemented** — product-scoped discovery and invocation expose only approved tool-version identifiers and preserve anti-enumeration denials | Add fuzzy resolution without weakening exact authorization |
| Consumer budgets | **Implemented** -- optional Redis-backed atomic request, tool-call, and context-read budgets; production fails closed when the budget store is unavailable | Add bank plan administration, external billing/showback, and live operational certification |

## 14. Open work

| ID | Item | Priority |
|---|---|---|
| CX-1 | MCP server with resource and tool surfaces | P0 -- **native lineage tools delivered 2026-08-29** (`atlas__get_lineage_graph`, `atlas__get_lineage_impact`, `resolve_entity`, `get_transformation_detail`); governed-SQL-tool and context-product surfaces already implemented |
| CX-2 | Context product definition, versioning, maker-checker | **Implemented** -- `context_product_api.py`, `ContextProduct`/`ContextProductVersion` models, `tests/test_context_products.py` |
| CX-3 | Per-read policy evaluation | **Implemented** -- lifecycle, roles, exact scope, purpose allowlists, quality score, and critical-incident gates enforced for REST and MCP |
| CX-4 | Consumption recorded as lineage | **Implemented for MCP** -- successful resources, prompts, and tools create generic immutable evidence; Context Product reads and product-scoped tools retain richer product-specific edges |
| CX-5 | Eligible-tool exposure and governed invocation | **Implemented** -- Context Product scope and native lineage tools preserve anti-enumeration behavior |
| CX-6 | Per-consumer rate limits and budgets | **Implemented 2026-08-29** -- atomic Redis request/minute, tool/day, and context/day buckets with hashed principal keys and production fail-closed behavior |
| CX-7 | Workload identity for MCP consumers | P0 |
| CX-8 | BI-surface context injection (Tableau, Power BI, Looker) | P1 |
| MCP-2 | MCP write operations | **Partial 2026-08-29** -- agents can create a governed data-product access request but cannot grant it; catalog edits, classification changes, and glossary proposals remain |
| MCP-3 | Fuzzy entity resolution for MCP tool arguments (e.g. resolve "customers" to a table id) | **Closed 2026-08-29** for lineage tools via `resolve_entity`; governed-SQL and catalog tools still require exact UUIDs |

## 15. Enterprise AI control-plane expansion

### 15.1 Market reference and product boundary

The Collibra Platform and its August 2026 product announcements are a market reference for
expected enterprise surfaces, not a design to copy. The relevant public references are:

- `https://www.collibra.com/products/collibra-platform`
- `https://www.collibra.com/blog/data-lineage-read-apis-mcp-server-lineage-on-demand`
- `https://www.collibra.com/blog/data-products-application-centralized-oversight-for-all-your-data-products`
- `https://www.collibra.com/blog/a-single-governed-source-of-truth-for-every-ai-agent-and-platform-introducing-collibra-s`
- `https://www.collibra.com/products/ai-command-center`
- `https://www.collibra.com/products/data-quality-and-observability`

**Wired to the epic backlog 2026-08-29**: CP-2/CP-3 -> `60-delivery/02-epic-backlog.md` EE.8, CP-5 -> EE.9, CP-6 -> EE.10, CP-7/CP-8 -> EE.11. See also `competitors/08-collibra-lineage-and-platform-analysis-2026-08.md`.

The reference set was reviewed on 2026-08-29. It establishes that buyers now expect a
platform to govern data products, context, lineage, quality, policies, models, and agents as
one connected system. Atlas keeps a narrower boundary: it does not become a BI tool,
notebook, pipeline authoring environment, or general-purpose ticketing system. It becomes the
governed context and action plane that those systems consume.

### 15.2 Required platform capabilities

| ID | Required capability | Atlas requirement | Acceptance evidence |
|---|---|---|---|
| CP-1 | Governed context products | Stable product identity, immutable versions, owner, purpose, bounded asset scope, semantic versions, glossary versions, quality requirements, policy summary, and eligible tools | Maker-checker lifecycle test; published versions are immutable; cross-tenant and draft-read denial tests |
| CP-2 | Data product registry | **Implemented 2026-08-29** -- immutable versions, ports, normalized roles, ownership, publication/supersession/retirement, certification and usage terms | `tests/test_agentic_platform.py`; portfolio filters and OpenAPI route assertions |
| CP-3 | Data contract registry | **Implemented 2026-08-29** -- versioned schema/quality/freshness/SLA definitions, structural compatibility checks, product-port binding, and independently approved breaking-change exception | Compatibility and maker-checker tests; add ODCS round-trip certification fixtures |
| CP-4 | Data marketplace | **Implemented foundation 2026-08-29** -- policy-filtered published-product discovery plus request/approve/reject/expire/revoke access lifecycle | Local verifier proves request/approve/provision-pending flow; add external provider certification and SLA escalation |
| CP-5 | Context compiler | **Implemented foundation 2026-08-29** -- MCP/REST/YAML/OSI/ODCS/Snowflake Semantic View/Databricks Metric View targets, stable artifact hash, structural drift report, quality gates, and REST delivery | Determinism/drift tests; add idiomatic YAML/file download and target-specific external validators |
| CP-6 | Lineage MCP | **Implemented 2026-08-29** -- all four tools live (`atlas__get_lineage_graph`, `atlas__get_lineage_impact`, `resolve_entity`, `get_transformation_detail`), bounded depth, org-scoped | `tests/test_mcp_server.py` now covers validation, anti-enumeration, success, and not-found behavior for the two newest tools; remaining evidence is the fuzzy-resolution corpus benchmark plus dedicated-environment lineage scale/authoritativeness certification |
| CP-7 | Unified AI registry | **Implemented foundation 2026-08-29** -- tenant-scoped APIs register use cases, models, agents, versions, dependencies, policies, deployments, runtime signals, independent assessments, maker-checker retirement, provider evidence sync, and portal dependency-graph visualization | Remaining local breadth is CLI manifest registration; external registry adapters and dedicated-environment browser/accessibility certification remain |
| CP-8 | AI trust and compliance | **Implemented foundation 2026-08-29** -- deterministic 100-point trust computation exposes every documentation, accountability, lifecycle, policy, evaluation, runtime, and assessment factor plus hard blockers | Add managed assessment-template library, remediation workflow, historical score projection, and framework evidence export |
| CP-9 | Quality and observability | Reusable rules, warehouse pushdown, anomaly monitors, scores, incident routing, ownership, SLAs, and lineage-aware blast radius | Rule execution evidence; deduplicated incidents; owner notification; quality signal shown on product and agent context |
| CP-10 | Privacy operations | Purpose, legal basis, processing location, retention, sensitive-data flow, policy simulation, and external privacy-system integration | Purpose-limited access test; retention evidence; sensitive lineage impact report |
| CP-11 | Workflow automation | Versioned workflow templates for reviews, access, certification, quality remediation, and compliance; fixed safety gates remain code-owned | Workflow audit trail; timers/escalations; no workflow can bypass maker-checker or execution policy |
| CP-12 | Adoption and portfolio intelligence | **Implemented foundation 2026-08-29** -- summary and trend APIs over product views, context reads, agent/tool consumption, access requests, lifecycle queues, quality posture, and value-free usage trends | Local verifier plus route/unit tests; add tenant dashboards, bounded retention, and bank-scale certification |
| CP-13 | Integration ecosystem | Certified adapters for databases, warehouses, BI, transformation, quality, model registries, and ITSM; canonical envelope and SDK remain the scaling mechanism | Capability certification per adapter; unsupported capabilities fail closed; version compatibility report |
| CP-14 | Unstructured context | Govern metadata for documents and knowledge assets without becoming a document-chat product | Metadata-only ingestion, classification, ownership, policy, and references; content retrieval delegated to an approved provider |

### 15.3 Context product lifecycle

```text
DRAFT -> REVIEW_REQUIRED -> PUBLISHED -> SUPERSEDED
  |             |
  +-> REJECTED <-+

PUBLISHED -> pending DEPRECATE review -> DEPRECATED
```

- A stable `context_product` identity owns a sequence of immutable
  `context_product_version` records.
- Only drafts may be edited. A new change creates a new version instead of mutating a
  published definition.
- Submission creates a `CONTEXT_PRODUCT_VERSION` item in the unified governance queue.
- The requester cannot approve their own version.
- Publishing supersedes the previous published version for the same product atomically.
- MCP and REST consumers see only published versions for which their roles and purpose are
  eligible. Unauthorized and nonexistent products have indistinguishable external responses.

### 15.4 Minimum context product contract

```json
{
  "product_key": "consumer-risk-analysis",
  "version": 3,
  "purpose": "Approved context for consumer credit-risk analysis",
  "owner_principal": "consumer-risk-stewards",
  "table_ids": ["tbl_..."],
  "semantic_model_version_ids": ["sem_..."],
  "glossary_term_version_ids": ["termv_..."],
  "eligible_tool_version_ids": ["toolv_..."],
  "allowed_consumer_roles": ["RiskAnalyst"],
  "lineage_depth": 2,
  "quality_requirements": {"minimum_score": 85, "deny_on_critical_incident": true},
  "policy_summary": {"source_values": "GATEWAY_ONLY", "retention": "NO_RAW_CONTEXT"}
}
```

Every referenced object is resolved in the product's organization and project before a draft
is accepted. Semantic, glossary, and tool references must identify approved or published
versions before submission. Wildcard estate scope is not permitted.

## 16. Delivery slices

| Slice | Scope | Exit condition | Status |
|---|---|---|---|
| CP-S1 | Context product identity, immutable versions, validation, maker-checker, REST API, audit, outbox | A product can be created, submitted, independently approved, listed, and read with tenant isolation | **Implemented and hardened; live PostgreSQL integration proof pending** |
| CP-S2 | Published context products as MCP resources; role eligibility and consumption evidence | External agents consume only eligible published products and every read is audited | **Implemented** |
| CP-S3 | Lineage MCP tools and unified lineage projection | Upstream, downstream, impact, and transformation questions return bounded governed evidence | **Implemented foundation: four native tools, Redis cache, and optional generation-stamped Neo4j projection/read path; scale certification remains** |
| CP-S4 | Data products, ports, contracts, and lifecycle dashboard | Producers manage a portfolio and publish qualifying products | **Implemented foundation** |
| CP-S5 | Marketplace and access requests | Consumers discover and request governed products without draft or tenant leakage | **Implemented foundation** |
| CP-S6 | Context specification and compiler | One approved definition compiles deterministically to MCP, REST, YAML, OSI, ODCS, Snowflake, or Databricks targets | **Implemented foundation** |
| CP-S7 | AI registry, assessments, and operational trust | Models, agents, use cases, dependencies, controls, and runtime signals share one governed registry | **Implemented and hardened: managed templates, remediation, maker-checker retirement, trust history, provider evidence sync, dependency graph** |
| CP-S8 | Adoption, privacy, workflow, and ecosystem expansion | Portfolio analytics foundation is live; privacy operations, workflow templates, ecosystem/provider certification, and browser/accessibility QA remain | **Implemented foundation** |

## 17. Implemented hardening controls (2026-08-29)

- PostgreSQL guarantees at most one `PUBLISHED` version per Context Product through a partial
  unique index and validates lifecycle/version bounds with check constraints.
- Role bindings are normalized into `context_product_role_binding`, allowing tenant/role
  authorization and pagination to execute in SQL instead of scanning every version in memory.
- Governance decisions lock the review row. Conflicting publication transactions fail with a
  deterministic `409` rather than publishing two versions.
- Every REST or MCP product consumption evaluates the approved minimum quality score and
  active-critical-incident policy. Missing required quality evidence fails closed.
- Successful consumption persists an immutable `context_product_consumption_edge` in addition
  to audit and outbox records. Context-scoped MCP tool discovery and invocation expose only the
  exact governed tool-version identifiers approved by the product.
- Deprecation uses the same maker-checker queue; a product remains published while the review
  is pending and becomes unavailable only after approval.
- The UI provides Context Product authoring/lifecycle controls and a bounded unified-lineage
  explorer with transitive impact inspection.
