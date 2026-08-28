# 08 — Enterprise Assumptions and Architecture Decisions

## Status

This document records the working assumptions used to proceed without waiting for enterprise discovery. Every assumption is replaceable through configuration or a versioned architecture decision.

## Operating Assumptions

- The platform serves a large regulated banking organization.
- The organization contains multiple legal entities, regions, lines of business, data domains, projects, and thousands of data sources.
- Source systems include several SQL dialects and may exist in restricted network zones.
- Production is deployed on Kubernetes or OpenShift. Local development uses Docker Compose.
- Data access is read-only by default. Write operations are outside the initial analytical platform scope.
- Source-system authorization remains authoritative. The platform supports both delegated user identity and approved workload identities.
- Raw source data remains in the source system. Only bounded, policy-approved results and profiling artifacts may leave it.
- Metadata, semantic objects, policy decisions, prompts, tools, models, and executions are versioned and auditable.
- No source values are sent to an LLM by default. A policy-approved masked-value mode may be enabled per classification and model route.
- Cross-LOB access is denied by default and explicitly granted.

## ADR-001 — Hybrid Deterministic and LLM Architecture

**Decision:** Use deterministic services for discovery, profiling, classification rules, key and relationship evidence, authorization, SQL parsing, cost controls, execution, audit, and workflow state. Use LLMs only for bounded semantic interpretation, ambiguity handling, logical-plan suggestions, descriptions, and result explanations.

LLM output is untrusted input. It must be schema-validated and may never directly execute a query, call a source, change a policy, publish a semantic version, or approve a governed object.

For business-semantic inference, the model receives bounded identifiers, types, classifications, constraints and deterministic baselines only. It may propose domains, entities, descriptions, roles, grain, synonyms, questions and a column-only tool blueprint. It cannot author executable SQL. An independent checker must approve the proposal before it becomes authoritative; tool SQL is rendered and validated deterministically and begins as a draft.

## ADR-002 — Workflow and Agent Orchestration

**Decision:** Use Temporal for durable, long-running enterprise workflows such as source onboarding, discovery, profiling, lineage extraction, re-analysis, and graph projection.

Use an internal typed analytical state machine for the runtime query path:

```text
RECEIVED -> AUTHORIZED -> RESOLVED -> PLANNED -> GENERATED
         -> VALIDATED -> COSTED -> EXECUTED -> EXPLAINED -> COMPLETED
```

LangGraph may be added behind an adapter for conversational checkpointing and complex human-in-the-loop reasoning. Google ADK may be evaluated for Google-centric deployments. Neither is a platform security boundary or a required core dependency.

## ADR-003 — Authoritative State and Projections

**Decision:** PostgreSQL is authoritative. Neo4j, vector indexes, search indexes, caches, and object-storage indexes are rebuildable projections.

Authoritative transactions write an outbox event in the same PostgreSQL transaction. Projectors consume outbox events idempotently. Services do not independently dual-write PostgreSQL and Neo4j.

## ADR-004 — Execution Choke Point

**Decision:** Every source query—generated SQL, approved tool SQL, profiler SQL, lineage SQL, or administrator SQL—passes through the Query Execution Gateway.

The gateway requires identity context, purpose, datasource, workload class, policy version, bounded timeout, row/byte limits, SQL AST validation, and an audit correlation ID. Published tools do not bypass this gateway.

## ADR-005 — Enterprise Isolation Hierarchy

```text
organization
  -> legal_entity
    -> line_of_business
      -> data_domain
        -> project
          -> datasource
```

Every governed record carries an organization boundary and, where applicable, LOB and project boundaries. Authorization defaults to deny. Cache keys, graph nodes, vector documents, artifacts, events, logs, and metrics preserve these boundaries.

## ADR-006 — Connector Deployment

Connectors implement a capability-negotiated SDK. They may run centrally or as remote workers near restricted source systems. Credentials are references resolved at runtime from an enterprise secret manager; plaintext credentials are never persisted in platform tables.

## ADR-007 — Eventing

Temporal owns workflow command/state semantics. Kafka owns integration events, lineage events, replayable projection events, and high-volume decoupled consumers. They are complementary and must not be used as competing workflow engines.

## ADR-008 — Local Platform Baseline

Local Docker Compose provides:

- PostgreSQL with pgvector
- Redis
- Neo4j
- Temporal and Temporal UI
- Kafka-compatible Redpanda
- MinIO object storage
- FastAPI control-plane API
- Temporal worker

Production mappings remain replaceable with managed or bank-standard equivalents.

## ADR-009 — Initial Security Mode

Local development uses an explicit `development` identity provider based on trusted request headers. The application refuses this provider when configured for production. Production identity integrates with OIDC/OAuth2, workload identity, and the bank policy decision point.

## ADR-010 — Delivery Strategy

The platform is delivered as production-grade vertical releases. The first LOB is an initial production rollout, not a disposable POC. Contracts, isolation keys, audit events, workflow durability, migrations, and observability are implemented from the first release.
