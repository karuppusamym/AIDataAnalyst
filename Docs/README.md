# Atlas Documentation

> **Atlas** — the governed AI data operating system for regulated enterprises.
> Restructured 2026-08-28. The previous flat `Docs/NN-*.md` set has been superseded; every durable fact was migrated into the folders below.

## What this documentation is for

Atlas understands the enterprise data estate, enforces policy *before* any action, explains every result it produces, and converts safe repeated analysis into reusable operational capability. This set of documents is the specification for building it: what it is, why it is shaped this way, what is built, and what remains.

Two properties make it usable rather than decorative:

- **Every claim about current state is honest.** `60-delivery/00-status.md` says `Pending` where a module does not exist, and `Not run` where a test has not been run.
- **Every decision names its revisit trigger.** An ADR with no revisit trigger is dogma, not a decision.

> **Documentation-truth pass, 2026-08-30.** The first property above was not holding. The
> architecture, contract and engineering documents were written around a 21-module
> decomposition under `src/atlas/modules/*` of which **1 of 21 exists**, as a 69-line scaffold;
> the working system is the flat `src/aida/` package. Structural claims across `10-architecture/`,
> `20-modules/`, `30-contracts/`, `40-engineering/` and `90-reference/` have been re-checked
> against the code and marked with a dated **Implementation status** callout wherever they
> describe a target rather than the present. The design prose is unchanged underneath — this
> separated "is" from "will be", it did not delete the plan. **Convention: a blockquote
> beginning "Implementation status (date)" states what is true of the code on that date; the
> prose around it may describe intent.** What changed and the evidence for each correction is
> in `review-2026-08/gap/04-documentation-truth-pass.md`. Start with
> `20-modules/00-module-index.md`, whose last two columns map every module to the file its
> behaviour actually lives in today.

## Start here

| If you are… | Read, in order |
|---|---|
| **New to the project** | `00-product/01-vision-and-goals.md` → `10-architecture/03-logical-architecture.md` → `20-modules/00-module-index.md` |
| **An engineer about to write code** | `40-engineering/01-development-spec.md` → `10-architecture/01-principles-and-invariants.md` → the relevant `20-modules/NN` spec |
| **Doing product or strategy work** | `00-product/03-market-landscape.md` → `00-product/04-competitive-feature-matrix.md` → `00-product/05-differentiation-and-whitespace.md` |
| **Reviewing security** | `50-security/01-security-architecture.md` → `50-security/02-threat-model.md` → `50-security/03-ai-safety-controls.md` |
| **Planning delivery** | `60-delivery/01-roadmap.md` → `60-delivery/03-tracker.md` → `60-delivery/00-status.md` |
| **Operating the platform** | `40-engineering/07-local-runbook.md` → `10-architecture/09-deployment-topology.md` |
| **Auditing or assessing risk** | `50-security/04-compliance-and-evidence.md` → `60-delivery/00-status.md` |

## Structure

```text
Docs/
├── 00-product/        What we are building and why it wins
├── 10-architecture/   How the system is shaped, and the decisions behind it
│   └── adr/           Architecture decision records
├── 20-modules/        One spec per bounded context (21 modules)
├── 30-contracts/      Interfaces we promise not to break
├── 40-engineering/    How to build, test, ship, and run it
├── 50-security/       Trust model, threats, AI safety, compliance
├── 60-delivery/       Status (00), roadmap, backlog, tracker, history
└── 90-reference/      Glossary, decision index, research sources
```

### 00-product — What we are building

| Document | Contents |
|---|---|
| [01 Vision and goals](00-product/01-vision-and-goals.md) | Product definition, goals, non-goals, principles, success criteria |
| [02 Personas and jobs](00-product/02-personas-and-jobs.md) | Six personas, their jobs, and the module coverage map |
| [03 Market landscape](00-product/03-market-landscape.md) | Five competitive segments and the convergence zone |
| [04 Competitive feature matrix](00-product/04-competitive-feature-matrix.md) | Feature-by-feature scoring; the seven capabilities nobody else has |
| [05 Differentiation and whitespace](00-product/05-differentiation-and-whitespace.md) | Five defensible differentiators, ten whitespace opportunities, and the strategic clock |
| [06 Product surface catalog](00-product/06-product-surface-catalog.md) | Every workbench, workspace, inspector, and console |
| [07 Packaging and editions](00-product/07-packaging-and-editions.md) | Deployment models, editions, metering, limits |

**Per-vendor deep dives** all live in [`review-2026-08/research/`](review-2026-08/research/) as of the 2026-08-30 consolidation — the older, shallower set under `competitors/` was retired to `_superseded/`. These complement the segment-level analysis above with primary-source module breakdowns, UI-surface detail, pricing and per-vendor weakness assessments:

| Document | Contents |
|---|---|
| [Collibra](review-2026-08/research/01-collibra.md) | Lineage source matrix by mechanism, the MCP tool split, the AI Copilot's documented limits, pricing |
| [Atlan](review-2026-08/research/02-atlan.md) | Personas and Purposes, the metadata-vs-data-policy enforcement question, the popularity formula |
| [Alation, Purview, Unity Catalog, AI-native entrants](review-2026-08/research/03-alation-purview-unity-ainative.md) | The Articles / Document Hubs object model behind our wiki design; Databricks ABAC and Genie |
| [Cross-vendor synthesis](review-2026-08/research/04-cross-vendor-synthesis.md) | Seven vendors × ~22 capabilities, and the four uncontested spaces derived from it |
| [Collibra lineage and platform](review-2026-08/research/05-collibra-lineage-and-platform.md) | Screenshot-driven review; the source of the Unified Lineage Explorer requirements |
| [Collibra marketplace, catalog, MCP, governance](review-2026-08/research/06-collibra-marketplace-and-mcp.md) | Second pass; what was genuinely new beyond the CP-1..CP-14 requirements |

### 10-architecture — How it is shaped

| Document | Contents |
|---|---|
| [01 Principles and invariants](10-architecture/01-principles-and-invariants.md) | **Nine invariants, each with an enforcement point and a test** |
| [02 System context](10-architecture/02-system-context.md) | Boundary crossings and their trust posture |
| [03 Logical architecture](10-architecture/03-logical-architecture.md) | Five layers, two primary flows, the latency budget |
| [04 Module decomposition](10-architecture/04-module-decomposition.md) | **The anti-monolith document** — the 21-module target and its boundaries. **Target, not current state:** 1 of 21 modules exists under `src/atlas/modules/` and it is a scaffold; the working code is still the flat `src/aida/` package. Read alongside the tracker's section A |
| [05 Service extraction plan](10-architecture/05-service-extraction-plan.md) | Why not microservices yet, and the triggers that change that |
| [06 Data architecture](10-architecture/06-data-architecture.md) | Stores, entities, versioning, projection, retention, partitioning |
| [07 Event and messaging model](10-architecture/07-event-and-messaging-model.md) | Temporal vs. Kafka, the outbox, envelope, topics |
| [08 Workers and workflows](10-architecture/08-workers-and-workflows.md) | How "hundreds of thousands of tables" becomes tractable |
| [09 Deployment topology](10-architecture/09-deployment-topology.md) | Local, target, network zones, HA, DR |
| [10 Performance and scale model](10-architecture/10-performance-and-scale-model.md) | Every target, its test, and its current measurement status |
| [11 Capacity and cost model](10-architecture/11-capacity-and-cost-model.md) | Workload isolation, sizing tiers, backpressure, cost governance, metrics |
| [12 Runtime sequences](10-architecture/12-runtime-sequences.md) | How the modules compose at runtime, end to end |
| [ADR register](10-architecture/adr/README.md) | Seventeen accepted decisions, one superseded (0017 → 0018) |

### 20-modules — The bounded contexts

Full index with reading orders, **and a per-module map from bounded context to the file its code actually lives in today**: [`20-modules/00-module-index.md`](20-modules/00-module-index.md). The 21 names below are bounded contexts, not directories.

| L1 Foundation | L2 Intelligence | L3 Runtime | L4/L5 |
|---|---|---|---|
| [01 Identity and tenancy](20-modules/01-identity-and-tenancy.md) | [04 Catalog](20-modules/04-catalog.md) | [12 Retrieval and search](20-modules/12-retrieval-and-search.md) | [18 Studio](20-modules/18-studio.md) |
| [02 Connectivity](20-modules/02-connectivity.md) | [05 Profiling and classification](20-modules/05-profiling-and-classification.md) | [13 Agent runtime](20-modules/13-agent-runtime.md) | [19 Context products and MCP](20-modules/19-context-products-and-mcp.md) |
| [03 Ingestion](20-modules/03-ingestion.md) | [06 Relationship intelligence](20-modules/06-relationship-intelligence.md) | [14 Tool registry](20-modules/14-tool-registry.md) | [21 Experience shell](20-modules/21-experience-shell.md) |
| [17 Policy and governance](20-modules/17-policy-and-governance.md) | [07 Semantic layer](20-modules/07-semantic-layer.md) | [15 Model gateway](20-modules/15-model-gateway.md) | |
| [20 Observability and audit](20-modules/20-observability-and-audit.md) | [08 Glossary and stewardship](20-modules/08-glossary-and-stewardship.md) | [16 Query gateway](20-modules/16-query-gateway.md) | |
| | [09 Lineage](20-modules/09-lineage.md) | | |
| | [10 Knowledge graph](20-modules/10-knowledge-graph.md) | | |
| | [11 Data quality](20-modules/11-data-quality.md) | | |

### 30-contracts — Promises we keep

| Document | Contents |
|---|---|
| [01 Contract strategy](30-contracts/01-contract-strategy.md) | Four tiers, compatibility rules, deprecation, error contract |
| [02 API conventions](30-contracts/02-api-conventions.md) | Naming, status codes, pagination, idempotency, bulk operations |
| [03 Internal module contracts](30-contracts/03-internal-module-contracts.md) | How modules talk, and why the rules are strict inside one process |
| [04 Event catalog](30-contracts/04-event-catalog.md) | Every domain event, by module |
| [05 Metadata ingestion envelope](30-contracts/05-metadata-ingestion-envelope.md) | The T1 external ingestion contract |
| [06 Lineage contract](30-contracts/06-lineage-contract.md) | Ingested and exposed lineage, including AI decision lineage |
| [07 Tool and agent contract](30-contracts/07-tool-and-agent-contract.md) | What a governed tool is; what any agent may do |
| [08 Data contracts](30-contracts/08-data-contracts.md) | Producer/consumer agreements enforced at runtime *(proposed)* |
| [09 Runtime request and audit contracts](30-contracts/09-runtime-request-and-audit-contracts.md) | Analyst request/response, refusals, audit event, approval thresholds |

### 40-engineering — How to build it

| Document | Contents |
|---|---|
| [01 Development spec](40-engineering/01-development-spec.md) | **Read before writing code.** Definition of done, where code goes, anti-patterns |
| [02 Repository layout](40-engineering/02-repository-layout.md) | Current shape, target shape, naming |
| [03 Coding standards](40-engineering/03-coding-standards.md) | Import-linter contracts, typing, bounds, errors, logging |
| [04 Testing strategy](40-engineering/04-testing-strategy.md) | Six tiers; Tier 0 is the invariant suite |
| [05 CI/CD and release](40-engineering/05-ci-cd-and-release.md) | Gates, releases, migrations, rollback |
| [06 Refactor plan](40-engineering/06-refactor-plan.md) | Flat package → modular monolith, in eight shippable phases |
| [07 Local runbook](40-engineering/07-local-runbook.md) | Start, verify, inspect, triage |

### 50-security — Trust

| Document | Contents |
|---|---|
| [01 Security architecture](50-security/01-security-architecture.md) | Assets, boundaries, eight defence layers, current posture |
| [02 Threat model](50-security/02-threat-model.md) | Twenty threats with controls; three worked attack scenarios |
| [03 AI safety controls](50-security/03-ai-safety-controls.md) | **The nine-control stack, and what Atlas does not claim** |
| [04 Compliance and evidence](50-security/04-compliance-and-evidence.md) | Audit contract, compliance packs, certification status |

### 60-delivery — Getting it done

| Document | Contents |
|---|---|
| [00 Delivery status](60-delivery/00-status.md) | **Start here.** The single answer to "where are we": capability matrix, invariant status, open gaps, and the decisions waiting on a person |
| [01 Roadmap](60-delivery/01-roadmap.md) | Phases 0 and A–E, with exit criteria |
| [02 Epic backlog](60-delivery/02-epic-backlog.md) | Epics with verifiable acceptance criteria |
| [03 Tracker](60-delivery/03-tracker.md) | Item-level open work: module IDs, the 2026-08 review's C/N/E items, drill currency, bank decisions |
| [06 Accomplishment log](60-delivery/06-accomplishment-log.md) | Append-only ledger of verified outcomes |
| [07 Connector implementation backlog](60-delivery/07-connector-implementation-backlog.md) | Code-level backlog for framework hardening, Oracle, and BigQuery |

### 90-reference

| Document | Contents |
|---|---|
| [01 Glossary](90-reference/01-glossary.md) | Terms, including where Atlas differs from common usage |
| [02 Decision log](90-reference/02-decision-log.md) | One-line index of every decision and open question |
| [03 Sources](90-reference/03-sources.md) | Competitive research sources and how to refresh them |
| [04 Analysis algorithms](90-reference/04-analysis-algorithms.md) | Scoring models, pruning strategies, and detection signals behind modules 05–07 |

## The four things to understand first

If you read nothing else:

1. **Deterministic services hold all authority; models only propose.** Every other property follows from this (ADR-0001).
2. **One execution choke point.** No code path reaches a source except through the query gateway — not tools, not profilers, not admins (ADR-0004). This is the differentiator competitors cannot retrofit.
3. **PostgreSQL is authoritative; everything else is a rebuildable projection.** Graph, vector, and search stores can be deleted at any time without data loss (ADR-0003).
4. **The control plane is value-free.** Metadata, statistics, and bounded approved results leave a source. Business data does not (ADR-0014).

## Maintaining these documents

| Document | Update when |
|---|---|
| `60-delivery/03-tracker.md` | Every increment |
| `60-delivery/00-status.md` | Every increment |
| `60-delivery/06-accomplishment-log.md` | Append on every material outcome — never edit |
| `20-modules/NN` | When that module's capability or open work changes |
| `10-architecture/adr/` | New ADR for a new decision; **never edit an accepted one** |
| `00-product/03,04,05` | Quarterly, or after a major vendor announcement |
| `30-contracts/04-event-catalog.md` | Before publishing any new event |
| `60-delivery/00-status.md` | When a gap opens, closes, or changes its safe default |

**The rule that keeps this honest.** A document that claims a capability the status matrix does not support is a defect. When they disagree, the status matrix wins and the other document gets corrected.
