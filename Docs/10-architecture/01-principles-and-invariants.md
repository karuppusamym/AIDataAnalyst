# Architecture Principles and Invariants

> Status: Authoritative. Owner: Architecture.
> These are binding. A design that violates an invariant is rejected in review; changing one requires a superseding ADR in `10-architecture/adr/`.

## 1. Principles vs. invariants

- A **principle** is a default that guides design. Departing from it requires justification.
- An **invariant** is a property that must hold in all states of the system. It is testable, and there is a test that fails if it is broken.

Principles shape the product. Invariants are what make it safe.

## 2. The nine invariants

Each invariant names its enforcement point and its test. An invariant without an automated test is a wish.

> **Implementation status (2026-08-30, second revision).** **All nine now have automated
> tests.** An earlier revision of this paragraph — written the same day — said four were
> still wishes; the tests for INV-1, INV-6, INV-7 and INV-9 landed after it was written, in
> `tests/test_inv1_single_authoritative_store.py`, `test_inv6_value_freedom.py`,
> `test_inv7_attributability.py` and `test_inv9_capability_honesty.py`. INV-2, INV-3, INV-4
> and INV-8 are in `tests/test_tier0_invariants.py`; INV-5 has both a dedicated file and
> entries there; INV-4 gained a second file, `test_inv4_authorization_wiring.py`, when the
> authorization decision was wired into production paths.
>
> **Having a test is not the same as the invariant holding**, and the per-invariant notes
> below say which is which — INV-1's test does not prove Neo4j ingests correctly because no
> Neo4j runs in the suite, and INV-5's structural enforcement (a repository base class) still
> does not exist. Re-verify by grepping for the test names rather than trusting this
> paragraph; it has been wrong once already on the same day.

### INV-1 — Single authoritative store

**Statement.** PostgreSQL holds authoritative state. Neo4j, vector indexes, search indexes, Redis, and object-storage indexes are rebuildable projections and are never read as truth for an authorization, approval, or correctness decision.

**Enforcement.** Projections are written only by outbox projectors, never by request-path code. No service dual-writes PostgreSQL and a projection.

**Test — Built (2026-08-30), with a named limit.** `tests/test_inv1_single_authoritative_store.py` (8 tests) asserts the structural property: projections are written only by outbox projectors, no request-path code dual-writes, and no authorization or approval decision reads a projection. What it does **not** prove is that Neo4j ingests correctly, because no Neo4j runs in the suite — the test file says so itself rather than implying coverage it does not have. `test_projection_rebuild` (delete Neo4j and the search index, replay from authoritative state, assert identical query results) remains unwritten, and **the projection-rebuild drill has never been run**. Note also that the "search index" in this statement is a target store: there is no search-index dependency in the repository, and lexical search runs as BM25-style scoring in PostgreSQL (`src/aida/retrieval.py`).

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

**Test — Built.** `test_model_output_types_are_inert` (`tests/test_tier0_invariants.py`): asserts no proposal type implements or can be coerced to an executable command interface.

### INV-4 — Fail closed

**Statement.** Missing identity configuration, unresolvable secrets, unapproved or unactivated model routes, unavailable policy state, or unverified connector capability produce a denial, never a degraded success.

**Enforcement.** Production configuration validation refuses to start with development identity, `env://` secret resolution, weak audit keys, or an insecure JWKS URL.

**Test.** `test_production_config_fail_closed` (`tests/test_tier0_invariants.py`): parameterized over each incomplete-posture case, asserting startup refusal or request denial, paired with `test_the_secure_production_baseline_itself_is_accepted` so the check cannot pass by refusing everything.

**Second test — Built (2026-08-30).** `tests/test_inv4_authorization_wiring.py` covers the other half of failing closed: that the authorization decision is actually *reached*. A static scan asserts the gate is reachable from the execution path, the validation path and the five wired read surfaces, that no module outside `workspace_service`/`workspace_api` calls `authorize` directly (which would bypass shadow mode), and — as its own meta-test — that the scan can still tell a gated handler from an ungated one. The behavioural half asserts that an unresolved workspace is a distinct state rather than a quiet allow, that a `SHADOW` workspace records what it would have denied, and that an `ENFORCE` workspace refuses. **Today nothing is denied in production**: every workspace is in `SHADOW` and the unresolved posture defaults to `SHADOW`, so the system measures rather than enforces (ADR-0018, rollout status).

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

**Test — Built (2026-08-30).** `tests/test_inv6_value_freedom.py` (13 tests) runs a sentinel fixture through the query gateway and scans control-plane state for the sentinel, and includes `test_the_control_plane_scan_would_notice_a_leak` — a meta-test that fails if the scan stops being able to find one.

**A leak this test did not cover, and now does.** The scan drove only the query gateway, so the tables metadata envelope 1.1 introduced sat outside it: `metadata_view_definition.definition_sql` and `metadata_routine.body_sql` stored source SQL **raw**, so a view defined `… WHERE ssn = '123-45-6789'` landed verbatim in the control plane. Fixed in migration `d5f8b21c4a03` (redacted column + fingerprint + redaction status, raw columns dropped), with the ingestion path added to the scan. Recorded here because it is the general lesson: *a test is only as strong as the paths its author had in mind*.

### INV-7 — Attributability of high-impact actions

**Statement.** Every mutation produces an audit record carrying actor identity, resource, action, tenant boundary, correlation ID, and timestamp, written in the same transaction as the mutation.

**Enforcement.** The unit-of-work commit path requires an audit record for any transaction touching a governed table.

**Test — Built (2026-08-30).** `tests/test_inv7_attributability.py` (11 tests). `test_every_mutation_audits` derives the set of mutating routes rather than declaring it — a route qualifies on a mutating verb *or* a call graph that reaches a session write — so a GET that writes is covered and a POST that only reads is checked against a closed read-only list instead of being dropped. Thirteen endpoints in `ai_registry_api` and `product_marketplace_api` that committed governed state with no audit record were found and fixed by it; the exemption dict is now empty and `test_no_unaudited_mutation_remains` asserts that it stays empty.

### INV-8 — Maker ≠ checker

**Statement.** The identity that proposes a governed change can never be the identity that approves it, for any object type.

**Enforcement.** A single platform-level approval service; feature modules cannot implement their own approval.

**Test — Built.** `test_self_approval_denied` (`tests/test_tier0_invariants.py`): parameterized over every governed object type `decide_governance_review` handles, attempts self-approval and asserts denial.

### INV-9 — Honest capability reporting

**Statement.** A connector, adapter, or feature advertises only behaviour that is implemented and passing its certification suite. Planned capability is displayed as planned.

**Enforcement.** Capability flags are derived from the certification result, not hand-declared.

**Test — Built (2026-08-30).** `tests/test_inv9_capability_honesty.py` (11 tests): every advertised capability must trace to a passing certification check, and a capability declared planned must not be reachable as if it were implemented. The invariant is most visible in `src/aida/connectors/registry.py`, which distinguishes `register(...)` from `declare_planned(...)` — Databricks, Teradata and Db2 are declared planned rather than advertised.

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
