# 16 - Market Comparison and Product Strategy

## Purpose

This document records the competitive baseline, current Atlas position, and the product, UX, platform, and performance requirements needed to build a stronger product than the current market leaders in enterprise data intelligence.

It is intended for internal product and engineering use. It is not a marketing page.

## Baseline date and source boundary

This assessment was validated on 2026-08-27 against:

- The current implementation and status recorded in this repository, especially documents 10, 12, 14, and 15.
- Official vendor product pages for Atlan, Collibra, and Alation.

Official external source URLs:

- Atlan: https://atlan.com/
- Atlan connectors: https://atlan.com/connectors/
- Collibra platform: https://www.collibra.com/products/collibra-platform
- Collibra integrations: https://www.collibra.com/products/integrations-apis/integrations
- Alation platform: https://www.alation.com/product/agentic-data-intelligence-platform/
- Alation platform overview: https://www.alation.com/

This document intentionally compares Atlas to vendor-stated product positioning, not to custom enterprise deployments or private roadmap commitments.

## Executive position

Atlas is already stronger than a normal proof of concept. It has a real governed control plane, a live portal, hard deterministic execution boundaries, a model gateway, maker-checker controls, audit evidence, dbt artifact intelligence, business-semantic inference, a semantic layer, governed tools, and an AI analyst flow in one product.

Atlas is not yet stronger than Atlan, Collibra, or Alation overall.

The current strengths are:

- Strong governance-first architecture.
- Strong bank-safe AI execution posture.
- Strong integration of analyst, semantic, tool, governance, audit, and operations workflows in one portal.
- Clear deterministic authority boundaries around SQL, policy, lineage, and audit.

The current weaknesses are:

- Connector breadth is far behind the market.
- Retrieval breadth and scale proof are incomplete.
- Stewardship, glossary lifecycle, and enterprise collaboration are incomplete.
- Large-estate UX and graph virtualization are incomplete.
- Performance, security, recovery, and scale certification are not complete.
- Enterprise packaging and ecosystem depth are behind mature commercial platforms.

Conclusion:

To beat the market, Atlas must not try to win as a generic catalog. It must become the best governed AI data operating system for regulated enterprises, while also closing the breadth gaps that buyers expect from category leaders.

## Market baseline

### Vendor positioning snapshot

| Vendor | Current public positioning | Practical implication |
|---|---|---|
| Atlan | Context layer for AI, data graph, business context, governance, connectors | Strong AI-ready metadata and broad ecosystem story |
| Collibra | Enterprise AI control plane, governance, trusted data and AI, 100+ integrations | Strongest governance and enterprise-platform positioning |
| Alation | Agentic data intelligence platform unifying catalog, governance, lineage, quality, and AI | Strong integrated category narrative around agentic data work |

### What buyers expect from leaders

Any product that wants to beat the current leaders must be credible across all of the following:

1. Broad connector coverage and reliable ingestion.
2. Strong metadata catalog and lineage.
3. Governed business semantics, glossary, ownership, and stewardship.
4. AI-ready context with explainability and policy control.
5. Enterprise-grade review, audit, security, and compliance posture.
6. Mature UX for analysts, stewards, admins, and auditors.
7. Proven scale, reliability, and operational packaging.
8. Open integration surfaces for other enterprise tools and agents.

## Current Atlas position

### Areas where Atlas is already differentiated

| Area | Current Atlas advantage |
|---|---|
| Governed AI execution | One mandatory query gateway, deterministic validation, masking, lineage, and auditable execution |
| Business-semantic promotion | Metadata-only inference with maker-checker approval and deterministic tool drafting |
| Unified control plane | Analyst, semantics, dbt, tools, governance, audit, and operations in one product portal |
| Trust boundary clarity | LLMs can propose or assist; deterministic services remain the execution boundary |
| Regulated-enterprise posture | OIDC boundary, secret reference model, outbox/audit evidence, explicit fail-closed design |

### Areas where Atlas is behind

| Area | Current gap |
|---|---|
| Connectors | Only PostgreSQL and Microsoft SQL Server are implemented today |
| Retrieval | Hybrid retrieval is partial and still lacks vector, graph expansion, and large-catalog benchmarks |
| Stewardship | Ownership assignment, glossary lifecycle, conflict handling, and bulk review are incomplete |
| Data quality breadth | No full enterprise data quality and observability layer yet |
| Large-estate UX | Virtualization, search depth, bulk actions, and million-object ergonomics are incomplete |
| Enterprise deployment | Production topology, DR, SIEM, WORM retention, private networking, and certification remain open |
| Proven performance | Benchmarks and bank-scale validation are not yet complete |

## Strategy to beat the market

The target product should be:

> The governed AI data operating system for regulated enterprises: the system that understands enterprise data, enforces policy before action, explains every result, and turns safe repeated analysis into reusable operational intelligence.

To make that credible, Atlas must win on five fronts simultaneously:

1. Functional breadth
2. Enterprise trust
3. UX quality
4. Performance and scale
5. Ecosystem reach

## Target capability bar

### 1. Functional breadth

| Capability | Current state | Match-market requirement | Beat-market requirement |
|---|---|---|---|
| Connectors | PostgreSQL, SQL Server, Oracle | Add Snowflake, Databricks, BigQuery, Redshift, dbt Cloud/Core, BI surfaces, OpenLineage, files, and APIs | Unified connector SDK with certification harness, source-side agent option, version matrix, and per-connector health scoring |
| Catalog | Implemented for current slice | Cross-source search, indexing, faceting, ownership, usage, and asset certification | Search that is semantic, policy-aware, and action-oriented instead of browse-only |
| Lineage | Query and dbt lineage partial | ETL, view, procedure, BI, and OpenLineage coverage | Unified technical plus AI decision lineage in one explorable graph |
| Business glossary and semantics | Partial | Steward lifecycle, glossary, dimensions, join rules, conflict handling | Business meaning that can safely operationalize into tools, metrics, and agent context |
| Data quality | Limited | Rules, quality signals, issue lifecycle, freshness, SLAs | Tight coupling of quality evidence with analyst and agent runtime decisions |
| Governed tools and agents | Implemented base | Multi-tool plans, tool certification, agent registry maturity | Best-in-class governed agent execution with deterministic gates and repeatable evidence |
| Collaboration | Limited | Comments, assignment, subscriptions, change review, workflow routing | Evidence-first teamwork with ownership and decision accountability built into every object |

### 2. Enterprise trust

| Capability | Match-market requirement | Beat-market requirement |
|---|---|---|
| Identity | Production OIDC with bank claim contract and group mapping | Full ABAC plus workload identity and purpose-aware authorization |
| Secrets | Certified secret manager adapter and rotation drills | Per-connector delegated identity with zero secret exposure to users or models |
| Governance | Maker-checker, approvals, audit, retention policy | Universal policy graph across data, tools, models, agents, and outputs |
| AI controls | Route approvals, prompt-risk controls, grounded generation | Multi-stage direct and indirect prompt-risk screening, explanation policy, and model kill switch |
| Compliance evidence | Searchable audit and runtime posture | Exportable compliance packs, WORM archive, and auditor-ready evidence bundles |

### 3. UX quality

Atlas should not settle for a functional admin console. To be better than the market, the UX bar must be:

- Persona-based: analyst, steward, reviewer, admin, auditor, and platform operator each get focused workflows.
- Search-first: global search, command palette, jump-to-asset, and action shortcuts.
- Evidence-first: every answer, semantic object, quality issue, and decision shows why it exists.
- Scale-safe: virtualized lists, graph levels of detail, background loading, optimistic control actions, and bounded detail panes.
- Bulk-capable: assignment, approval, tagging, stewardship, and remediation actions in batch.
- Guided: clear empty states, setup wizards, onboarding, and progressive disclosure.
- Accessible: keyboard support, focus order, ARIA correctness, contrast, and screen-reader validation.
- Exportable: linkable views, shareable evidence, and operational reports.

### 4. Performance and scale

Without measurable performance, "better" is not credible. Atlas must define and prove explicit bars.

#### Required measurable targets

| Area | Minimum target |
|---|---|
| Control-plane API p95 excluding source and model time | 300 ms |
| Authorization decision p95 | 50 ms |
| Search result first paint for typical catalog queries | less than 1 s |
| Analyst plan preview for metadata-grounded queries | less than 2 s excluding source execution |
| Graph exploration response for bounded neighborhoods | less than 2 s at approved budgets |
| Million-object catalog browsing | Smooth pagination or virtualization without browser lockup |
| Concurrency | Thousands of sources with fair scheduling and backpressure proof |
| Recovery | Restore drills and replay verification with documented RPO/RTO |

#### Required proof

- Load tests
- Soak tests
- Failure injection
- Projection rebuild timing
- Migration rehearsal
- Connector certification
- Security and penetration testing
- Accessibility and browser regression suites

### 5. Ecosystem reach

| Capability | Match-market requirement | Beat-market requirement |
|---|---|---|
| APIs | Stable APIs and SDKs | First-class event, SDK, and automation framework for custom product extensions |
| Extensibility | Custom connectors and metadata ingestion | Certified plugin model for tools, retrieval adapters, governance checks, and UI modules |
| Downstream embedding | BI and workflow integrations | Atlas context injected directly into analyst, BI, and AI surfaces with traceability |
| Operational hooks | Alerts and ticketing integrations | Closed-loop remediation flows across ITSM, observability, and governance systems |

## Product principles that must not change

The market race does not justify weakening the architecture. These principles remain non-negotiable:

1. PostgreSQL or a future approved authoritative store remains the source of truth.
2. Projection stores remain rebuildable and non-authoritative.
3. No source access path bypasses the governed query gateway.
4. LLM output never becomes execution authority.
5. Raw sensitive data should not enter model context by default.
6. Every high-impact action must be attributable, reviewable, and auditable.
7. Production mode must remain fail closed when identity, policy, or secret posture is incomplete.

## What "better than the market" actually means

Atlas does not need to look flashier than every competitor homepage. It needs to be materially better for a regulated enterprise buyer and user.

That means:

- Faster time to trusted analytical action.
- Better explanation of why a result can be trusted.
- Better control over what AI can and cannot do.
- Better conversion of repeated analysis into governed reusable tools.
- Better visibility across semantics, lineage, tools, decisions, and runtime evidence.
- Better safety under failure, policy change, credential rotation, and audit review.

## Capability gap closure plan

### Phase A - Reach category minimum

Objective: remove the obvious reasons a buyer would reject Atlas immediately.

Required outcomes:

- Deliver the next priority connectors: BigQuery first, then Snowflake and Databricks (SQL Server and Oracle delivered).
- Add enterprise-grade global search and cross-source retrieval.
- Add glossary lifecycle, steward assignment, conflict handling, and bulk review.
- Add ETL and OpenLineage ingestion.
- Add core data quality and freshness signals.
- Add accessibility audit and persona-driven navigation.

Exit criteria:

- Atlas is no longer dismissed as "Postgres-only" or "prototype breadth."

### Phase B - Win on regulated-enterprise trust

Objective: become clearly safer and more governable than general-purpose AI data tools.

Required outcomes:

- Full bank OIDC and ABAC integration.
- Delegated source identity and certified secret adapter.
- Compliance evidence packs, WORM retention, SIEM routing, and kill-switch flows.
- Prompt-risk coverage extended to indirect and retrieved-context attacks.
- Formal tool and agent certification workflows.

Exit criteria:

- Atlas becomes the strongest trust and control story in regulated AI data operations.

### Phase C - Win on user experience

Objective: move from functional workbench to product-class experience.

Required outcomes:

- Persona homepages and navigation.
- Unified search and command actions.
- Large-list virtualization and graph level-of-detail rendering.
- Bulk stewardship and review operations.
- Rich evidence views with shareable links and exports.
- Faster setup and guided onboarding.

Exit criteria:

- Users can work faster in Atlas than in a generic catalog plus separate governance tools.

### Phase D - Win on scale and proof

Objective: replace assertions with measurable proof.

Required outcomes:

- Bank-scale benchmark corpus.
- Published performance dashboards and regression gates.
- Capacity, soak, chaos, restore, and replay evidence.
- Connector compatibility matrix and certification reports.

Exit criteria:

- Performance and resilience become demonstrated product attributes, not roadmap claims.

### Phase E - Create an advantage others do not have

Objective: deliver a category-defining feature set rather than just catching up.

Required outcomes:

- Trust-scored agent execution that can explain every step and refusal.
- Automatic promotion of successful governed analyses into reusable safe tools with approval.
- Context products that package glossary, lineage, semantics, policy, and tool eligibility for downstream AI clients.
- Continuous runtime learning from value-free evidence, approvals, and negative knowledge.

Exit criteria:

- Atlas is not just another catalog or governance suite. It is the enterprise AI context and action layer with regulated execution built in.

## Immediate product priorities

The next product increments should be prioritized in this order:

1. Connector expansion and certification. The canonical envelope, honest matrix, control-plane certification evidence, and Atlas workbench are delivered; native adapter breadth and executable vendor/version fixtures remain.
2. Cross-source retrieval, search, and indexing.
3. Stewardship, glossary lifecycle, and bulk governance workflows.
4. ETL and OpenLineage lineage ingestion.
5. Large-estate UX virtualization and accessibility.
6. Enterprise identity, delegated credentials, and compliance packaging.
7. Performance, load, recovery, and security certification.

## Decision framework for future work

When choosing roadmap work, prefer items that satisfy all three conditions:

1. They remove a category-level gap against Atlan, Collibra, or Alation.
2. They strengthen Atlas's differentiated trust and execution architecture.
3. They produce measurable proof rather than only more surface area.

Avoid:

- Decorative UI work that does not improve workflow speed or clarity.
- Autonomous AI features that weaken deterministic safety boundaries.
- Connector proliferation without certification and operational evidence.
- Metrics or semantics that are not versioned and reviewable.

## Success criteria

Atlas can credibly claim to be better than the current leaders only when all of the following are true:

1. It matches market expectations on connectors, search, lineage, glossary, governance, and scale-safe UX.
2. It exceeds market expectations on governed AI execution, trust boundaries, auditability, and safe operationalization of analysis into tools.
3. It has documented performance, recovery, and security evidence, not only local test success.
4. Real enterprise users can complete analyst, steward, reviewer, and auditor workflows faster and with fewer handoffs than in competitor products.

## Final statement

The right strategy is not "be a prettier catalog."

The right strategy is:

> Match the market on platform breadth, beat the market on governed AI execution and enterprise trust, and prove it with measurable operational evidence.
