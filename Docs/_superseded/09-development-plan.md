# 09 — Enterprise Development Plan

## Definition of Done

The platform is complete when it can onboard bank data sources at scale, maintain governed metadata and semantics, safely answer analytical requests under source and platform authorization, explain every decision, recover from failures, and operate under measurable SLOs.

## Release 0 — Engineering and Governance Foundation

- Repository standards, automated tests, linting, dependency and container scanning.
- Architecture decisions, threat model, data-flow classification, and service ownership.
- Docker development topology and Kubernetes production topology.
- Configuration, secrets references, structured logging, metrics, tracing, and health probes.
- Organization/LOB/project isolation model.
- OIDC integration boundary, authorization context, and immutable audit schema.
- PostgreSQL migrations and transactional outbox.

Exit criteria:

- All services start locally with one command.
- Readiness checks validate required dependencies.
- Every mutation produces an attributable audit event.
- Production configuration cannot enable development authentication.

## Release 1 — Source Registry and Metadata Inventory

- Connector SDK and capability negotiation.
- Source registration, connectivity tests, credential references, and network-zone metadata.
- Temporal onboarding and discovery workflows.
- Catalog, schema, table, view, column, constraint, index, and partition inventory.
- Stable object identity, change detection, soft deprecation, and fingerprints.
- Source/LOB concurrency quotas, retry policies, cancellation, and maintenance windows.

Current delivery checkpoint: envelope `1.0`, atomic synchronous full/incremental ingestion, resumable checksum-addressed Temporal manifests/chunks, deferred full reconciliation, successful payload cleanup, datasource/chunk idempotency, delivery evidence, an honest connector matrix, deterministic conformance certification, and the matching Atlas workbench are implemented. PostgreSQL and Microsoft SQL Server native pull adapters are `BETA`. Kafka/schema-registry intake, signed producers, remaining adapters, additional asset types, maximum-scale recovery tests, and vendor/version certification remain.

Exit criteria:

- Discovery is resumable and idempotent.
- A failed source does not fail unrelated sources.
- Inventory changes are versioned and auditable.
- A connector certification suite validates supported capabilities.

## Release 2 — Profiling, Classification, and Relationship Intelligence

- Policy-based adaptive profiling.
- Approximate statistics and bounded sampling.
- Sensitive-data classification with evidence.
- Primary/composite key inference and bounded relationship candidate generation.
- Temporal/table-family analysis.
- Human review with maker-checker controls and negative knowledge.
- PostgreSQL outbox projectors for Neo4j and vector/search indexes.

Exit criteria:

- Full scans cannot occur without an explicit approved policy.
- Evidence and algorithm versions reproduce every inference.
- Projection stores can be deleted and rebuilt from authoritative state.
- Quality is measured against a labeled banking metadata benchmark.

## Release 3 — Governed Semantic Layer

- Domains, entities, terms, synonyms, dimensions, measures, metrics, grains, time semantics, and join rules.
- Immutable semantic versions with draft, validation, approval, publish, supersede, and rollback states.
- LLM semantic enrichment through a policy-controlled model gateway.
- Hybrid lexical/vector/graph retrieval with permission filtering before ranking.
- Steward workbench and impact analysis.

Exit criteria:

- Runtime requests pin all semantic and policy versions.
- Unauthorized objects never enter retrieval or model context.
- Low-confidence objects cannot publish without the configured approval path.

## Release 4 — Governed Analytical Runtime

- Typed request interpretation and logical analytical plans.
- Deterministic metric compilation and join-path selection.
- Provider-neutral LLM gateway with structured output validation.
- SQLGlot AST parsing, allowlists, policy checks, EXPLAIN/cost gates, and bounded execution.
- Result schema validation, masking, retention, download, and model-context policy.
- Complete AI and technical execution lineage.

Exit criteria:

- No code path can reach a source except through the execution gateway.
- Query and policy benchmarks meet agreed correctness thresholds.
- Every response exposes interpretation, assumptions, versions, lineage, and confidence.

## Release 5 — Governed Tools and Advanced Agent Flows

- Tool draft, testing, maker-checker review, publish, deprecate, and retire lifecycle.
- Parameter schemas and deterministic invocation.
- Agent registry and constrained tool bindings.
- Optional LangGraph adapter for selected checkpointed conversational workflows.
- Query memory with semantic-version compatibility and feedback.

Exit criteria:

- Tools pass the same execution controls as generated queries.
- Tool dependencies and blast radius are queryable.
- Model loops have strict step, time, token, and monetary budgets.

## Release 6 — Bank-Wide Scale and Resilience

- Domain and region partitioning.
- Active/standby or active/active topology according to bank requirements.
- Capacity tests for millions of metadata objects and thousands of sources.
- Disaster-recovery exercises and restore verification.
- LOB chargeback, quotas, operational dashboards, and SLO error budgets.
- Progressive connector and semantic-version rollout.

## Cross-Cutting Workstreams

- Security: threat modeling, SAST/DAST, dependency/SBOM, penetration testing, key rotation.
- Model risk: prompt/model inventory, evaluation, approvals, drift, red-team tests.
- Data governance: classifications, retention, residency, legal hold, stewardship.
- Reliability: SLOs, chaos tests, backpressure, replay, reconciliation, runbooks.
- Developer experience: connector SDK, fixtures, local stack, generated API clients.
- User experience: accessible interfaces, evidence-first review, progressive disclosure, actionable failures.

## Initial Working SLO Assumptions

These are planning defaults until the bank supplies formal requirements:

- Control-plane API availability: 99.95% monthly.
- Interactive query orchestration availability: 99.9% monthly, excluding source outages.
- Metadata RPO: 15 minutes; RTO: 4 hours.
- Audit-event RPO: effectively zero through transactional persistence.
- API p95 excluding source/LLM time: 300 ms.
- Authorization decision p95: 50 ms.
- Discovery task success after retry: at least 99.5% for healthy sources.
- Unauthorized query execution tolerance: zero.
- Cross-LOB data leakage tolerance: zero.
