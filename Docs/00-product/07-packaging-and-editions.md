# Packaging and Editions

> Status: Draft for review. Owner: Product.
> Purpose: define how capability is bundled, metered, and limited. Packaging decisions constrain architecture (metering hooks, entitlement checks, tenant isolation) and must be made before, not after, the modules are built.

## 1. Why this document exists early

Three architectural decisions depend on packaging and are expensive to retrofit:

1. **Entitlement enforcement points.** If edition gates capability, the policy engine must evaluate entitlements alongside permissions, from the start.
2. **Metering granularity.** Showback and consumption pricing require per-tenant, per-LOB counters on source compute, model tokens, and object counts — instrumented at write time.
3. **Isolation tier.** Whether a tenant can be offered dedicated infrastructure determines the deployment topology in `10-architecture/09-deployment-topology.md`.

## 2. Deployment models

| Model | Description | Who it is for | Status |
|---|---|---|---|
| **Self-hosted (BYOK)** | Customer runs Atlas in their own Kubernetes/OpenShift; all data and metadata stay in their network | Regulated banks — the primary target | Target for v1 |
| Dedicated managed | Vendor-operated, single-tenant infrastructure in a customer-chosen region | Mid-size regulated firms | Later |
| Multi-tenant SaaS | Shared control plane, logically isolated tenants | Not targeted this horizon — regulated buyers reject shared metadata planes | Not planned |

**Architectural consequence.** Because self-hosted is primary, Atlas cannot assume vendor-operated telemetry, cannot phone home, and must run fully air-gapped except for approved model routes. Every capability must have an air-gapped mode or be explicitly marked as requiring egress.

## 3. Editions

| Capability group | Foundation | Enterprise | Regulated |
|---|:--:|:--:|:--:|
| Catalog, discovery, search | ● | ● | ● |
| Profiling and classification | ● | ● | ● |
| Query lineage + dbt lineage | ● | ● | ● |
| ETL / OpenLineage / BI lineage | ○ | ● | ● |
| Semantic layer + metrics | ● | ● | ● |
| Glossary + stewardship workflows | ◐ basic | ● | ● |
| Knowledge graph explorer | ◐ bounded | ● | ● |
| Data quality (thresholds, incidents) | ● | ● | ● |
| Data quality → runtime coupling | ○ | ● | ● |
| AI analyst (governed) | ● | ● | ● |
| Governed tool registry | ● | ● | ● |
| Multi-step tool plans | ○ | ● | ● |
| MCP context products | ○ | ● | ● |
| Studio (semantic + tool authoring) | ○ | ● | ● |
| RBAC | ● | ● | ● |
| ABAC + purpose-based access | ○ | ● | ● |
| Delegated source identity | ○ | ◐ | ● |
| Maker-checker governance | ● | ● | ● |
| Audit ledger | ● | ● | ● |
| WORM archive + SIEM routing | ○ | ◐ | ● |
| Compliance packs (BCBS 239, model risk) | ○ | ○ | ● |
| Source-side connector agents (restricted zones) | ○ | ◐ | ● |
| Multi-region DR with failover | ○ | ◐ | ● |
| Kill switch + model-risk evaluation harness | ◐ | ● | ● |

**Design rule.** Safety controls are never an edition upgrade. Prompt-risk screening, the execution gateway, AST validation, fail-closed behaviour, and the audit ledger are present in every edition. Editions gate *breadth and enterprise integration*, never *whether the product is safe*.

## 4. Metering dimensions

Instrumented from day one even if not billed initially.

| Dimension | Unit | Why meter it |
|---|---|---|
| Governed objects | catalogs, schemas, tables, columns | Primary size proxy; drives storage and index cost |
| Connected sources | active datasources | Drives scheduler and connector cost |
| Metadata scan volume | objects scanned / period | Drives worker capacity |
| Analyst requests | agent runs | Drives model and gateway cost |
| Model tokens | input/output per route | Direct external cost; already budgeted per route |
| Source query compute | queries, bytes scanned, seconds | Chargeback to the LOB that caused it |
| Governed tool invocations | calls | Value signal — the metric that should grow |
| MCP context consumption | context product reads | External agent usage |
| Seats by persona | named users | Traditional axis; secondary |

**Non-negotiable.** Every meter carries organization plus the tenancy scope the platform actually stores, so showback works at the boundary the bank actually budgets at. *(Corrected 2026-08-30: this line named `legal entity`, which does not exist in `src/` or `migrations/` — see `10-architecture/06-data-architecture.md` §2. `ADR-0005`'s hierarchy is superseded by ADR-0018; the scope to meter on is the one ADR-0018 defines. **Metering itself is not implemented** — there is no meter, no usage record and no showback surface in `src/`, so this whole section is a requirement, not a description.)*

## 5. Limits and quotas

| Limit | Default | Configurable | Enforced at |
|---|---|---|---|
| Synchronous ingestion envelope | 100 catalogs / 50k tables / 250k columns | Down only | Ingestion API |
| Batch ingestion | 1,000 chunks / 1M tables / 5M columns | Down only | Batch admission under lock |
| Graph traversal | 1–4 hops, node/edge caps | Down only | Graph API |
| Query rows / bytes / seconds | Per workload class | Per LOB | Query gateway |
| Model spend | Per route budget contract | Per route | Model gateway |
| Concurrent scans per source | Per source policy | Per source | Fleet scheduler |
| Requests per LOB | Rate limit | Per LOB | API gateway |

Limits are *safety mechanisms first and commercial levers second*. A limit that exists only for upsell is rejected.

## 6. Open packaging decisions

| # | Decision needed | Blocks | Owner |
|---|---|---|---|
| PK-1 | Consumption vs. seat vs. object-count pricing basis | Metering emphasis, billing integration | Product + Finance |
| PK-2 | Whether Foundation edition exists at all, or Enterprise is the floor | Edition gating in policy engine | Product |
| PK-3 | Whether connector SDK is open source | Ecosystem strategy; `W6` in whitespace map | Product + Eng |
| PK-4 | Whether MCP context products are metered separately | Metering schema | Product |
| PK-5 | Air-gapped deployment support level | Model route architecture, telemetry | Product + Eng |

## Related documents

- Vision: `00-product/01-vision-and-goals.md`
- Deployment topology: `10-architecture/09-deployment-topology.md`
- Policy and governance: `20-modules/17-policy-and-governance.md`
- Observability and audit: `20-modules/20-observability-and-audit.md`
