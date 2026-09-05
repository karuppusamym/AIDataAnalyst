# Architecture Decision Records

> Owner: Architecture.
> An ADR records a decision that was expensive to make and would be expensive to reverse. If a decision can be changed in an afternoon, it does not need an ADR.

## How to use this register

- **Status** is one of `Proposed`, `Accepted`, `Superseded by ADR-NNNN`, or `Deprecated`.
- An accepted ADR is binding. Code review rejects a change that contradicts one.
- To reverse a decision, write a new ADR that supersedes it. **Never edit an accepted ADR's decision section** — the historical record is the point.
- Every ADR names its **revisit trigger**: the observable condition that would make reconsideration correct. An ADR with no revisit trigger is dogma, not a decision.

## Template

```markdown
# ADR-NNNN — <Title>

**Status:** Accepted | **Date:** YYYY-MM-DD | **Owner:** <role>

## Context
What forces are in play. What we knew and did not know.

## Decision
The decision, stated so a reviewer can test compliance.

## Consequences
### Positive
### Negative — the costs we are accepting
### Neutral

## Alternatives considered
| Option | Why rejected |

## Revisit trigger
The observable condition under which this should be reconsidered.
```

## Register

| ID | Title | Status | Revisit trigger |
|---|---|---|---|
| [0001](ADR-0001-hybrid-deterministic-llm.md) | Hybrid deterministic and LLM architecture | Accepted | Never for the authority boundary; only to expand approved reasoning routes |
| [0002](ADR-0002-workflow-and-agent-orchestration.md) | Temporal for workflows; typed state machine for the runtime | Accepted | Enterprise platform standardization on another workflow engine |
| [0003](ADR-0003-authoritative-state-and-projections.md) | PostgreSQL authoritative; everything else is a projection | Accepted | An approved enterprise metadata system of record replaces it |
| [0004](ADR-0004-execution-choke-point.md) | One mandatory query execution gateway | Accepted | Never |
| [0005](ADR-0005-tenancy-hierarchy.md) | Six-level enterprise isolation hierarchy | Accepted | Bank supplies a different legal-entity model |
| [0006](ADR-0006-connector-deployment.md) | Capability-negotiated connector SDK, central or source-side | Accepted | A source class cannot be served by either placement |
| [0007](ADR-0007-eventing-split.md) | Temporal owns process state; Kafka owns event distribution | Accepted | No planned change |
| [0008](ADR-0008-no-agent-framework-in-core.md) | No LangGraph or ADK in the core | Accepted | A specific approved workflow needs framework checkpointing; add behind an adapter |
| [0009](ADR-0009-route-approval-is-not-activation.md) | Model-route approval does not activate generation | Accepted | No planned change |
| [0010](ADR-0010-bounded-value-free-graph.md) | Graph exploration is lazy, bounded, and value-free | Accepted | Bank-scale performance and privacy certification permits raising caps |
| [0011](ADR-0011-modular-monolith-over-microservices.md) | Modular monolith with a planned extraction path | Accepted | An extraction trigger in the service extraction plan fires |
| [0012](ADR-0012-single-metadata-envelope.md) | Pull, push, and stream ingestion converge on one envelope | Accepted | Only through backward-compatible envelope versions |
| [0013](ADR-0013-prompt-risk-before-retrieval.md) | Prompt-risk screening precedes retrieval and planning | Accepted | Semantic classifiers added as defence in depth only |
| [0014](ADR-0014-value-free-control-plane.md) | Source values are not platform memory | Accepted | Classification-specific retention approval |
| [0015](ADR-0015-schema-per-module.md) | One PostgreSQL schema per module, no cross-schema FKs | Accepted | Never — this is the extraction insurance |
| [0016](ADR-0016-quality-freshness-fails-closed.md) | Quality baselines are value-free; source freshness fails closed | Accepted | An approved connector watermark and retention contract exists |
| [0017](ADR-0017-domain-complete-tenancy-and-cross-source-graph.md) | Domain-complete tenancy and boundary-aware cross-source graph traversal | **Superseded by 0018** | — |
| [0018](ADR-0018-three-axis-tenancy-and-classification.md) | Access, classification and technical hierarchies are modelled separately; only access grants | Accepted | The permission boundary itself must be the line of business, provable from containment without evaluating policy |
| [0019](ADR-0019-vector-index-without-pgvector.md) | Nearest-neighbour search is a port; the default adapter needs no PostgreSQL extension | Accepted | Post-filter candidate sets are routinely above a few thousand, or the estate's database standard adopts `pgvector` |
| [0020](ADR-0020-graph-store-decision.md) | The classification tree and the lineage graph both live in PostgreSQL; no separate graph store | Accepted | Measured p95 lineage traversal exceeds ~200 ms at real depth, or all-paths enumeration / graph algorithms become requirements |
| [0022](ADR-0022-open-semantic-interchange-target.md) | Open Semantic Interchange stays a thin export target, not the internal semantic model | Accepted | A named customer/partner needs OSI conformance, OSI reaches a stable public schema, or OSI becomes a real deal-level buying criterion |
| [0023](ADR-0023-deterministic-jobs-vs-generative-producers.md) | Deterministic jobs vs. confidence-gated generative producers for catalog enrichment | Proposed | A capability exists where the deterministic/generative line is genuinely ambiguous at design time |
| [0027](ADR-0027-risk-tiered-agent-checking.md) | An independent reviewer agent may check risk-tier T0/T1 items only, under three hard conditions | Proposed | The sampled disagreement rate exceeds 5% for any object type over a month, a T0/T1 misclassification causes an incident, or an auditor rejects sampled automated checking as a control |
| [0028](ADR-0028-developer-workbench-not-separate-applications.md) | One application with a Developer workbench, not several applications behind a landing page | Proposed | A developer surface must be reachable from a network zone the governed portal must not be, or served to an audience without an Atlas seat |

## Superseded decision history

| Superseded | By | Date | Why |
|---|---|---|---|
| [0017](ADR-0017-domain-complete-tenancy-and-cross-source-graph.md) — Domain-complete tenancy | [0018](ADR-0018-three-axis-tenancy-and-classification.md) | 2026-08-30 | Superseded before acceptance. Its own reversal condition (a table needing two sibling domains) is structurally met in a bank estate. ADR-0018 keeps its goals and its `cross_boundary_grant` mechanism, but separates classification from tenancy instead of deepening the tenancy path |
