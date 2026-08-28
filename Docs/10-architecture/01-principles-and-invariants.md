# Architecture Principles and Invariants

> Status: Authoritative. Owner: Architecture.
> These are binding. A design that violates an invariant is rejected in review; changing one requires a superseding ADR in `10-architecture/adr/`.

## 1. Principles vs. invariants

- A **principle** is a default that guides design. Departing from it requires justification.
- An **invariant** is a property that must hold in all states of the system. It is testable, and there is a test that fails if it is broken.

Principles shape the product. Invariants are what make it safe.

## 2. The nine invariants

Each invariant names its enforcement point and its test. An invariant without an automated test is a wish.

### INV-1 — Single authoritative store

**Statement.** PostgreSQL holds authoritative state. Neo4j, vector indexes, search indexes, Redis, and object-storage indexes are rebuildable projections and are never read as truth for an authorization, approval, or correctness decision.

**Enforcement.** Projections are written only by outbox projectors, never by request-path code. No service dual-writes PostgreSQL and a projection.

**Test.** `test_projection_rebuild`: delete Neo4j and the search index entirely, replay from authoritative state, assert full reconstruction and identical query results.

### INV-2 — One execution choke point

**Statement.** No code path reaches a data source except through the Query Execution Gateway. This includes generated SQL, approved tool SQL, profiler SQL, lineage extraction SQL, quality check SQL, and administrator SQL.

**Enforcement.** Connector `execute_*` methods are private to the gateway module. The module boundary is compile-time-checked (import linter rule).

**Test.** `test_no_connector_execution_outside_gateway`: static analysis over the import graph asserts no module outside `query_gateway` imports a connector execution symbol.

### INV-3 — Model output is never authority

**Statement.** LLM output is untrusted input. It is schema-validated on receipt and can never directly execute a query, call a source, mutate a policy, publish a semantic version, approve a governed object, or bind a tool.

**Enforcement.** Model gateway returns typed, validated proposal objects only. Proposal types are structurally distinct from command types; there is no conversion function.

**Test.** `test_model_output_types_are_inert`: assert no proposal type implements or can be coerced to an executable command interface.

### INV-4 — Fail closed

**Statement.** Missing identity configuration, unresolvable secrets, unapproved or unactivated model routes, unavailable policy state, or unverified connector capability produce a denial, never a degraded success.

**Enforcement.** Production configuration validation refuses to start with development identity, `env://` secret resolution, weak audit keys, or an insecure JWKS URL.

**Test.** `test_production_config_fail_closed`: parameterized over each incomplete-posture case, assert startup refusal or request denial.

### INV-5 — Tenant isolation is total

**Statement.** Every governed record carries an organization boundary and, where applicable, legal entity, LOB, and project. Authorization defaults to deny. Cache keys, graph nodes, vector documents, artifacts, events, logs, and metrics preserve these boundaries.

**Enforcement.** Repository base class requires a tenant scope argument; there is no unscoped query helper.

**Test.** `test_cross_tenant_denial`: every list/read/write endpoint and every background worker is exercised with a foreign tenant context and must deny.

### INV-6 — Value-freedom of control-plane state

**Statement.** Raw source business values do not enter platform tables, logs, traces, events, profiles, model context, or evidence records by default. Questions are stored as keyed HMAC fingerprints; persisted SQL has literals redacted; profiles contain statistics only.

**Enforcement.** Ingestion and profiling validators reject attribute keys associated with samples, row values, secrets, or credentials. Persisted SQL passes a redaction pass.

**Test.** `test_no_source_values_in_control_plane`: run a full end-to-end fixture with sentinel values in source data; scan every platform table, log line, event payload, and trace for the sentinels.

### INV-7 — Attributability of high-impact actions

**Statement.** Every mutation produces an audit record carrying actor identity, resource, action, tenant boundary, correlation ID, and timestamp, written in the same transaction as the mutation.

**Enforcement.** The unit-of-work commit path requires an audit record for any transaction touching a governed table.

**Test.** `test_every_mutation_audits`: reflection over governed model classes; exercise each mutation endpoint; assert a matching audit row.

### INV-8 — Maker ≠ checker

**Statement.** The identity that proposes a governed change can never be the identity that approves it, for any object type.

**Enforcement.** A single platform-level approval service; feature modules cannot implement their own approval.

**Test.** `test_self_approval_denied`: for every governed object type, attempt self-approval and assert denial.

### INV-9 — Honest capability reporting

**Statement.** A connector, adapter, or feature advertises only behaviour that is implemented and passing its certification suite. Planned capability is displayed as planned.

**Enforcement.** Capability flags are derived from the certification result, not hand-declared.

**Test.** `test_capability_matrix_matches_certification`: assert every advertised capability has a passing certification check.

## 3. Design principles

| # | Principle | Practical consequence |
|---|---|---|
| P1 | Deterministic by default, probabilistic by exception | If a rule can compute it, a model does not. Models handle ambiguity, naming, and explanation — not structure, keys, policy, or execution. |
| P2 | Evidence over assertion | Every inference carries algorithm version, inputs, and confidence. Rejections are retained as negative knowledge. |
| P3 | Bounded everything | Every traversal, scan, profile, plan, retrieval, model call, and result has an explicit configured bound with a truncation reason. Unbounded is a bug. |
| P4 | Version everything governed | Metadata, semantics, metrics, tools, policies, model routes, and classifiers are versioned. Runtime pins versions so a decision can be replayed. |
| P5 | Idempotency as the default integration contract | Every ingestion, projection, event, and workflow activity is safely repeatable. Retries are the normal case. |
| P6 | Keep the data in the source | Metadata, statistics, and bounded approved results leave the source. Business data does not. |
| P7 | The unit of work is a task in a DAG, not an agent | Metadata analysis is distributed job execution. There is no agent per table. |
| P8 | Prefer reuse over generation | An approved tool beats a fresh generation. The system's cost and risk should fall as usage rises. |
| P9 | Module boundaries are contracts, not conventions | Cross-module access goes through a published interface, enforced mechanically. |
| P10 | Operability is a feature | Rebuild, replay, cancel, resume, drain, and kill-switch paths are designed with the capability, not after it. |

## 4. Quality attribute priority

When attributes conflict, resolve in this order. This ordering is itself an architectural decision.

1. **Correctness** — a wrong answer is worse than no answer.
2. **Security and isolation** — a leak is worse than an outage.
3. **Explainability** — an unexplainable correct answer cannot be acted on in a regulated context.
4. **Reproducibility** — a decision that cannot be replayed cannot be audited.
5. **Metadata freshness** — stale context produces confidently wrong answers.
6. **Query latency**
7. **Cost efficiency**
8. **Scalability**
9. **Extensibility**

Note that latency ranks sixth. Atlas deliberately trades interactive speed for correctness and explainability. A 2-second answer with full evidence beats a 200-millisecond answer that an analyst cannot defend.

## 5. Explicitly rejected architectures

| Rejected | Why | ADR |
|---|---|---|
| Vector store as system of record | Cannot represent authoritative PK/FK semantics, versioning, approval, or transactional relationships | ADR-0003 |
| LLM as profiler | Profiling is cheaper, faster, reproducible, and more accurate deterministically | ADR-0001 |
| One agent per table | Combinatorial cost, no dependency ordering, unbounded permission surface | ADR-0002 |
| Agent framework (LangGraph/ADK) in the core | State, permissions, and evidence must stay portable and deterministic; adapters only | ADR-0002 |
| Kafka as workflow state | Event replay and durable process state have different failure semantics | ADR-0007 |
| Microservices from day one | Distributed-system cost before the boundaries are proven; see `05-service-extraction-plan.md` | ADR-0011 |
| Dual-write to PostgreSQL and Neo4j | Unreconcilable divergence; outbox projection instead | ADR-0003 |
| Model-generated SQL executing directly | Violates INV-3; no model-risk review survives it | ADR-0001 |

## Related documents

- ADR register: `10-architecture/adr/README.md`
- Module decomposition: `10-architecture/04-module-decomposition.md`
- Testing strategy (where invariant tests live): `40-engineering/04-testing-strategy.md`
- Threat model: `50-security/02-threat-model.md`
