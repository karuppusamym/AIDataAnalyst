# Decision Log

> Status: Living index. Owner: Architecture.
> A one-line-per-decision index across ADRs, product choices, and open questions. The full reasoning lives in the linked document; this exists so a reader can scan every decision in one place.

## Architecture decisions (ADRs)

| ID | Decision | Status | Document |
|---|---|---|---|
| ADR-0001 | Deterministic services hold authority; LLMs only propose | Accepted | [link](../10-architecture/adr/ADR-0001-hybrid-deterministic-llm.md) |
| ADR-0002 | Temporal for durable workflows; typed state machine for the runtime | Accepted | [link](../10-architecture/adr/ADR-0002-workflow-and-agent-orchestration.md) |
| ADR-0003 | PostgreSQL authoritative; everything else is a rebuildable projection | Accepted | [link](../10-architecture/adr/ADR-0003-authoritative-state-and-projections.md) |
| ADR-0004 | One mandatory query execution gateway | Accepted | [link](../10-architecture/adr/ADR-0004-execution-choke-point.md) |
| ADR-0005 | Six-level enterprise tenancy hierarchy | Accepted | [link](../10-architecture/adr/ADR-0005-tenancy-hierarchy.md) |
| ADR-0006 | Capability-negotiated connectors; central or source-side placement | Accepted | [link](../10-architecture/adr/ADR-0006-connector-deployment.md) |
| ADR-0007 | Temporal owns process state; Kafka owns event distribution | Accepted | [link](../10-architecture/adr/ADR-0007-eventing-split.md) |
| ADR-0008 | No agent framework in the core | Accepted | [link](../10-architecture/adr/ADR-0008-no-agent-framework-in-core.md) |
| ADR-0009 | Model-route approval does not activate generation | Accepted | [link](../10-architecture/adr/ADR-0009-route-approval-is-not-activation.md) |
| ADR-0010 | Graph exploration is lazy, bounded, and value-free | Accepted | [link](../10-architecture/adr/ADR-0010-bounded-value-free-graph.md) |
| ADR-0011 | Modular monolith with a planned extraction path | Accepted | [link](../10-architecture/adr/ADR-0011-modular-monolith-over-microservices.md) |
| ADR-0012 | One metadata envelope for all ingestion transports | Accepted | [link](../10-architecture/adr/ADR-0012-single-metadata-envelope.md) |
| ADR-0013 | Prompt-risk screening precedes retrieval | Accepted | [link](../10-architecture/adr/ADR-0013-prompt-risk-before-retrieval.md) |
| ADR-0014 | Source values are not platform memory | Accepted | [link](../10-architecture/adr/ADR-0014-value-free-control-plane.md) |
| ADR-0015 | Schema per module; no cross-schema foreign keys | Accepted | [link](../10-architecture/adr/ADR-0015-schema-per-module.md) |
| ADR-0016 | Quality baselines value-free; source freshness fails closed | Accepted | [link](../10-architecture/adr/ADR-0016-quality-freshness-fails-closed.md) |

## Product decisions

| Decision | Rationale | Document |
|---|---|---|
| Position as a governed AI data operating system, not a catalog | Catalog is a commoditized, crowded category; the execution plane is not | `00-product/01-vision-and-goals.md` |
| Target ~15 certified connectors, not 80+ | Certification depth over count; the count race is unwinnable | `00-product/03-market-landscape.md` §8 |
| Do not compete on ML anomaly detection | Monte Carlo and Anomalo have years of head start; compete on runtime coupling instead | `00-product/05-differentiation-and-whitespace.md` §4 |
| Do not build BI or dashboarding | Supply context to BI tools rather than replace them | `00-product/01-vision-and-goals.md` §3.2 |
| Read-only platform; no write-back | Write paths multiply the blast radius of a model error | `00-product/01-vision-and-goals.md` §3.2 |
| Self-hosted (BYOK) as the primary deployment model | Regulated buyers reject shared metadata planes | `00-product/07-packaging-and-editions.md` §2 |
| Safety controls are never an edition upgrade | Editions gate breadth, never whether the product is safe | `00-product/07-packaging-and-editions.md` §3 |
| Latency ranks sixth in the quality-attribute order | Correctness and explainability matter more than interactive speed in a regulated context | `10-architecture/01-principles-and-invariants.md` §4 |
| Build glossary, studio, and context products directly in the target structure | Building them into the flat package would double the refactor | `40-engineering/06-refactor-plan.md` §11 |
| Proof (benchmarks, drills) is a product feature | Competitors publish benchmarks; assertions lose bake-offs | `60-delivery/01-roadmap.md` §6 |

## Open questions

| # | Question | Blocks | Owner |
|---|---|---|---|
| PK-1 | Consumption vs. seat vs. object-count pricing basis | Metering emphasis | Product + Finance |
| PK-2 | Does a Foundation edition exist, or is Enterprise the floor? | Edition gating | Product |
| PK-3 | Is the connector SDK open source? | Ecosystem strategy (W6) | Product + Eng |
| PK-4 | Are MCP context products metered separately? | Metering schema | Product |
| PK-5 | What level of air-gapped deployment is supported? | Model route architecture | Product + Eng |
| DC-1 | Are data products and context products one concept with two surfaces? | Module 19 design | Architecture |
| DC-2 | Does a data-contract breach block consumers or only warn them? | Runtime coupling design | Product |
| DC-3 | Who arbitrates when a producer must break a contract? | Governance operating model | Data Governance |
| DC-4 | Is a data contract versioned independently of its asset? | Data model | Architecture |
| SM-6 | Should Atlas adopt Open Semantic Interchange? | Semantic interoperability; commoditization risk | Architecture |

## Decisions required from the bank

Twelve items tracked in `60-delivery/03-tracker.md` §J. These change adapters and deployment policy, not the core architecture, and none of them blocks continued development — only production release.

## Related documents

- ADR register: `10-architecture/adr/README.md`
- Principles and invariants: `10-architecture/01-principles-and-invariants.md`
- Tracker: `60-delivery/03-tracker.md`
