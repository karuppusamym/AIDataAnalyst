# Architecture Principles and Invariants

> Status: Authoritative. Owner: Architecture.
> These are binding. A design that violates an invariant is rejected in review; changing one requires a superseding ADR in `10-architecture/adr/`.

## 1. Principles vs. invariants

- A **principle** is a default that guides design. Departing from it requires justification.
- An **invariant** is a property that must hold in all states of the system. It is testable, and there is a test that fails if it is broken.

Principles shape the product. Invariants are what make it safe.

## 2. The nine invariants

Each invariant names its enforcement point and its test. An invariant without an automated test is a wish.

> **Implementation status (2026-08-30).** By this document's own standard, four of the nine
> are currently wishes. Tests exist for **INV-2, INV-3, INV-4, INV-5 and INV-8**
> (`tests/test_tier0_invariants.py` and `tests/test_inv5_tenant_isolation.py`). The tests named
> below for **INV-1, INV-6, INV-7 and INV-9** do not exist anywhere in `tests/` — verified by
> searching for each function name across the repository on 2026-08-30. Each is marked
> **Planned** in place below. The invariants themselves are still binding on design review;
> what is absent is the automated proof. Closing this is tracker `E4` /
> `Docs/review-2026-08/gap/02-gap-diff-and-plan.md` §5.
>
> *This count is volatile: INV-5's test landed during this pass. Re-verify by grepping for the
> test names rather than trusting this paragraph.*

### INV-1 — Single authoritative store

**Statement.** PostgreSQL holds authoritative state. Neo4j, vector indexes, search indexes, Redis, and object-storage indexes are rebuildable projections and are never read as truth for an authorization, approval, or correctness decision.

**Enforcement.** Projections are written only by outbox projectors, never by request-path code. No service dual-writes PostgreSQL and a projection.

**Test — Planned, not written (2026-08-30).** `test_projection_rebuild`: delete Neo4j and the search index entirely, replay from authoritative state, assert full reconstruction and identical query results. No such test exists, and the projection-rebuild drill has never been run. Note also that the "search index" in this statement is itself a target store: there is no search-index dependency or service in the repository, and lexical search runs as BM25-style scoring in PostgreSQL (`src/aida/retrieval.py`).

### INV-2 — One execution choke point

**Statement.** No **SQL statement** reaches a data source except through the Query Execution Gateway. This includes generated SQL, approved tool SQL, lineage extraction SQL, quality check SQL, and administrator SQL.

Structural discovery and bounded profiling are the two source-touching paths that do *not* carry a SQL statement: they call `Connector.discover()` and `Connector.profile_table()`, which take structured arguments (schema name, table name, column names, caps) and compose their own statements internally. They cannot be used to run caller-supplied SQL, which is why they sit outside the gateway. This is stated explicitly because the earlier wording — "no code path reaches a data source" — described something the system has never done, and an invariant that is not literally true is worth less than a narrower one that is.

**Enforcement — three independent layers, all live as of 2026-08-30.**

1. **Type system.** `ConnectorRegistry.create` returns `Connector`, which has no SQL-accepting member. The `estimate_read_query` / `execute_read_query` pair lives on `aida.connectors.sql_execution.SqlExecutor`. Calling either on a registry-produced connector is an error under `mypy --strict`, which runs in CI.
2. **Import graph.** `aida.connectors.execution_access` is the only module that returns a `SqlExecutor`, and the import-linter contract *"INV-2 connector SQL execution is reachable only from the query gateway"* (`pyproject.toml`) permits exactly one importer: `aida.query_gateway`. This is the contract ADR-0004 has always named; it did not exist until 2026-08-30.
3. **Static scan.** The Tier-0 test below catches a dynamic bypass that neither of the above can see.

**Test.** `test_no_connector_execution_outside_gateway`: walks the AST of every module under `src/aida` and asserts that no module except `query_gateway.py` calls either member of the SQL-accepting surface. Paired with `test_the_connector_handed_to_the_platform_has_no_sql_surface`, which fails if someone moves the methods back onto `Connector` — a change that would leave layers 2 and 3 passing while the type-level guarantee silently disappeared.

### INV-3 — Model output is never authority

**Statement.** LLM output is untrusted input. It is schema-validated on receipt and can never directly execute a query, call a source, mutate a policy, publish a semantic version, approve a governed object, or bind a tool.

**Enforcement.** Model gateway returns typed, validated proposal objects only. Proposal types are structurally distinct from command types; there is no conversion function.

**Test.** `test_model_output_types_are_inert`: assert no proposal type implements or can be coerced to an executable command interface.

### INV-4 — Fail closed

**Statement.** Missing identity configuration, unresolvable secrets, unapproved or unactivated model routes, unavailable policy state, or unverified connector capability produce a denial, never a degraded success.

**Enforcement.** Production configuration validation refuses to start with development identity, `env://` secret resolution, weak audit keys, or an insecure JWKS URL.

**Test.** `test_production_config_fail_closed`: parameterized over each incomplete-posture case, assert startup refusal or request denial.

### INV-5 — Tenant isolation is total

**Statement.** Every governed record carries an organization boundary and, where applicable, the workspace / business-classification scope defined by the tenancy axis. Authorization defaults to deny. Cache keys, graph nodes, vector documents, artifacts, events, logs, and metrics preserve these boundaries.

> **Implementation status (2026-08-30).** The earlier wording named `legal entity` as a
> tenancy level. **`legal_entity` does not exist in `src/` or in any migration** — searched
> for on 2026-08-30 and found nowhere. It is an ADR-only concept; `gap/02` row C2/D3 is to
> not build it. The tenancy shape itself is being changed under ADR-0018 (`Workspace`,
> `WorkspaceMembership`, `SourceBinding`, `BusinessNode` are in `src/aida/models.py` and in
> `migrations/versions/f1a2b3c4d5e6_adr_0018_three_axis_tenancy.py`); this statement is
> deliberately phrased to that axis rather than to a fixed path. The authority on the current
> shape is `20-modules/01-identity-and-tenancy.md` and ADR-0018, not this line.

**Enforcement — Planned.** The intended mechanism is a repository base class that requires a tenant scope argument, with no unscoped query helper. **No such base class exists**: there is no `Repository` class or `TenantScope` type in `src/aida/` or `src/atlas/platform/`, and scoping is applied per query by convention. Ships with the module extraction (`40-engineering/06-refactor-plan.md`). The test below is what currently substitutes for the structural guarantee.

**Test — Built (2026-08-30).** `test_cross_tenant_denial` in `tests/test_inv5_tenant_isolation.py`, which is route-table-driven rather than hand-enumerated: it also asserts that every route requires an authenticated principal, that every route reaches a tenant-boundary check, and that every background worker is tenant-scoped, each with a closed exemption list. `tests/test_tier0_invariants.py` carries a second `test_cross_tenant_denial` plus `test_authorization_defaults_to_deny_without_membership`.

### INV-6 — Value-freedom of control-plane state

**Statement.** Raw source business values do not enter platform tables, logs, traces, events, profiles, model context, or evidence records by default. Questions are stored as keyed HMAC fingerprints; persisted SQL has literals redacted; profiles contain statistics only.

**Enforcement.** Ingestion and profiling validators reject attribute keys associated with samples, row values, secrets, or credentials. Persisted SQL passes a redaction pass.

**Test — Planned, not written (2026-08-30).** `test_no_source_values_in_control_plane`: run a full end-to-end fixture with sentinel values in source data; scan every platform table, log line, event payload, and trace for the sentinels. The *design* boundary is real and was verified by reading `src/aida/semantic_inference.py`, which tags every model-bound field `"value_scope": "METADATA_ONLY"`; what is missing is the end-to-end sentinel harness that would prove it holds everywhere.

### INV-7 — Attributability of high-impact actions

**Statement.** Every mutation produces an audit record carrying actor identity, resource, action, tenant boundary, correlation ID, and timestamp, written in the same transaction as the mutation.

**Enforcement.** The unit-of-work commit path requires an audit record for any transaction touching a governed table.

**Test — Planned, not written (2026-08-30).** `test_every_mutation_audits`: reflection over governed model classes; exercise each mutation endpoint; assert a matching audit row. `record_audit` in `src/aida/events.py` writes into the caller's session (so the same-transaction property is structurally available), but nothing asserts that every mutation path calls it.

### INV-8 — Maker ≠ checker

**Statement.** The identity that proposes a governed change can never be the identity that approves it, for any object type.

**Enforcement.** A single platform-level approval service; feature modules cannot implement their own approval.

**Test.** `test_self_approval_denied`: for every governed object type, attempt self-approval and assert denial.

### INV-9 — Honest capability reporting

**Statement.** A connector, adapter, or feature advertises only behaviour that is implemented and passing its certification suite. Planned capability is displayed as planned.

**Enforcement.** Capability flags are derived from the certification result, not hand-declared.

**Test — Planned, not written (2026-08-30).** `test_capability_matrix_matches_certification`: assert every advertised capability has a passing certification check. The invariant is nonetheless honoured in the one place it is most visible: `src/aida/connectors/registry.py` distinguishes `register(...)` from `declare_planned(...)`, and Databricks, Teradata and Db2 are declared planned rather than advertised.

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
