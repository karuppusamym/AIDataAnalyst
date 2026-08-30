# Target Design 5 — Target architecture

Status: Proposal, clean-room. Consolidates designs 1–4 into modules, stores and flows.

---

## 1. Invariants

The existing nine invariants are, with one exception, the best thing in the current
architecture and are carried forward unchanged. They are restated here only where the
target design changes them.

| # | Invariant | Change |
|---|---|---|
| INV-1 | PostgreSQL authoritative; everything else is a rebuildable projection | **Unchanged.** Neo4j leaves the topology, so there is one fewer projection to rebuild |
| INV-2 | One execution choke point | **Extended**: a federated tool is a plan of single-source leaf queries, each through the gateway. The join layer is not an execution path. **And actually enforced** — the import-linter contract must exist |
| INV-3 | Model output is never authority | **Unchanged.** Extended to wiki blocks and document claims: both are proposals |
| INV-4 | Fail closed | **Unchanged** |
| INV-5 | Tenant isolation is total | **Re-keyed**: the scope is `(organization_id, workspace_id)`, not a six-level path. Isolation boundaries handle hard walls |
| INV-6 | Value-freedom of control-plane state | **Clarified**: uploaded customer documentation and metadata embeddings are in scope for storage; source business values are not. Federated intermediates are ephemeral and never persisted |
| INV-7 | Attributability of high-impact actions | **Unchanged** |
| INV-8 | Maker ≠ checker | **Unchanged** |
| INV-9 | Honest capability reporting | **Extended** to lineage: a connector's advertised lineage depth is derived from its parser certification suite, not declared |
| **INV-10** | **Generated knowledge is never silently authoritative** | **New.** Every compiled statement carries its generator, inputs and provenance class; a human edit pins a block and recompilation produces a diff proposal, never an overwrite |

INV-10 is new because the product now publishes prose about the business. Prose that
cannot be traced to its inputs is a liability in a regulated environment.

---

## 2. Store topology

| Store | Role | Change from today |
|---|---|---|
| **PostgreSQL** | Authoritative state, all modules, schema-per-module | Keep |
| **PostgreSQL + pgvector** | Embeddings for assets, wiki blocks, document sections | **Add.** Not optional |
| **PostgreSQL (edge tables + recursive CTE)** | Graph projection: lineage, relationships, business tree | **Replaces Neo4j.** Traversal is bounded to 1–4 hops by policy; a well-indexed edge table serves this. Keep the projection interface so a graph store can be reintroduced if traversal requirements genuinely change |
| **Temporal** | Durable workflows: onboarding, discovery, analysis DAG, batch ingestion, document parsing, knowledge compilation, projection rebuild | Keep — it earns its place |
| **Object storage** | Uploaded documents, large artefacts, evidence bundles | Keep |
| **Redis** | Budgets, rate limits, ephemeral locks | Keep |
| **DuckDB (in-process, ephemeral)** | Federated join layer only | **Add.** Per-request, destroyed with the request |
| ~~Neo4j~~ | — | **Remove.** See design brief §6 |
| ~~Kafka~~ | — | **Defer.** Transactional outbox stays (it is the hard part); a worker drains it. Reintroduce when a real external consumer exists — the envelope and topic names are already designed for it |

Net: two fewer stateful services to run, secure, monitor and drill inside a bank.

---

## 3. Modules

Sixteen, down from twenty-one. Four are new; several current modules merge because
their boundaries were finer than their coupling justified.

| # | Module | Layer | Owns | Status vs today |
|---|---|---|---|---|
| 1 | **identity-tenancy** | Foundation | Org, workspace, membership, principal, workload identity, secret refs | Re-scoped (workspace replaces the 6-level path) |
| 2 | **policy-governance** | Foundation (cross-cutting) | Policies, ABAC engine, entitlements, unified review queue, maker-checker, cross-boundary grants | Extended with ABAC |
| 3 | **observability-audit** | Foundation (cross-cutting) | Audit ledger, outbox, evidence, SLO state, compliance packs | Keep |
| 4 | **connectivity** | Foundation | Datasources, connections, capability certification, source bindings | Keep + source_binding |
| 5 | **ingestion** | Foundation | Metadata envelope, batches, chunks, idempotency | Envelope → v1.1 (views, procedures, comments, grants) |
| 6 | **catalog** | Intelligence | Objects, stable identity, fingerprints, drift, tombstones — now incl. views, procedures, functions with their text | Extended |
| 7 | **profiling-classification** | Intelligence | Value-free stats, classification, key inference | Keep (merge of 05) |
| 8 | **structural-analysis** | Intelligence | Relationship candidates, table roles, grain, families, canonicalisation, negative knowledge | Merge of current 06 |
| 9 | **transformation-analysis** | Intelligence | **View DDL parsing, procedure body parsing, dataflow extraction, dialect parser certification** | **NEW** |
| 10 | **lineage** | Intelligence | Edges, evidence, proposals, review diffs, versions, impact, agent-decision edges | Substantially extended |
| 11 | **semantics-glossary** | Intelligence | Business graph, domains, entities, annotations, metrics, terms, ownership, conflicts, certification | Merge of current 07 + 08 + business graph |
| 12 | **document-ingestion** | Intelligence | Upload, parse, sections, mappings, claim extraction | **NEW** |
| 13 | **knowledge** | Intelligence | Knowledge bases, pages, blocks, compilation, provenance, staleness, publication | **NEW** |
| 14 | **retrieval** | Runtime | Lexical + vector + graph candidate generation, policy-before-ranking, fusion | Extended (vector, graph) |
| 15 | **execution** | Runtime | Query gateway, validation pipeline, `validate_sql`, cost, masking, **federation planner** | Extended (federation) |
| 16 | **capability** | Runtime | Tool registry + 4 generators, agent registry, context products, MCP server, model gateway | Merge of current 14 + 15 + 19 + agent registry |
| — | data-quality | folded into 7 + 2 | Baselines are profiling; gates are policy conditions | **Merged.** Quality-gates-runtime becomes an ABAC condition rather than a subsystem |
| — | studio, experience-shell | UI | Authoring surfaces | Rebuilt (framework decision) |

**Why the merges.** Current modules 07 and 08 both own "what things mean" and cannot
be versioned independently; 14/15/19 all serve "what an agent may do" and share a
lifecycle; 11 (data quality) has no independent consumer once quality becomes a policy
condition and a profiling output. Fewer modules with real boundaries beats more
modules with paper ones — especially given 1 of 21 currently exists.

**Layering, with the current cycle resolved.** The `09 lineage ↔ 16 query-gateway`
cycle and the L2→L3 edges flagged as `ST-11` disappear under one rule: **intelligence
modules never call runtime modules.** The gateway *emits* events; lineage, profiling
and quality *consume* them. Profiling does not call the gateway to run a profile
query — it enqueues a profiling task that the execution module drains. One direction,
no cycle, mechanically checkable.

---

## 4. Flows

### Onboarding a source

```
register datasource → verify connection → certify capabilities (drives the honest
capability matrix) → request source_binding → source-owner approval → discovery scan
→ envelope v1.1 (tables, columns, constraints, VIEWS+DDL, PROCEDURES+BODY, comments,
grants) → catalog upsert with stable identity → drift detection → outbox
```

### Understanding

```
structural analysis (deterministic: keys, roles, grain, families, candidates)
   ↓
transformation analysis (view + procedure parse → dataflow → lineage proposals)
   ↓
document ingestion (if uploaded: parse → map → extract claims)
   ↓
meaning inference (deterministic rules → model proposals for language fields only,
                   one call per table family, ordered by usage × impact × deficit)
   ↓
business graph assignment (rules + proposals)
   ↓
review queue (ordered by blast radius; bulk decisions; rejection → negative knowledge)
   ↓
publish (immutable versions)
```

### Publishing knowledge

```
input fingerprint changes → affected pages marked stale
   → deterministic blocks recompile automatically
   → inferred blocks recompile into proposals
   → pinned (human) blocks produce diff proposals, never overwrites
   → review → publish page version
   → context products referencing those pages recompile → new version
   → agents pick up the new version at next session
```

### Answering (interactive)

```
RECEIVED → AUTHORIZED (workspace + policy + purpose)
        → SCREENED (deterministic prompt-risk, before retrieval)
        → RESOLVED (hybrid retrieval, policy-filtered before ranking)
        → PLANNED (approved tool preferred; generation only if no tool fits)
        → GENERATED (if needed; model output is inert structured proposal)
        → VALIDATED (deterministic AST pipeline — the model's influence ends here)
        → COSTED  → EXECUTED (gateway; per-leaf if federated)
        → EXPLAINED → COMPLETED
```

Each of the last five must be an independently gated checkpoint that can refuse, not
a retroactive trace entry written in a loop after execution returned. That is a real
change from the current implementation.

---

## 5. The indirect-injection gap

The current design screens the user's question before retrieval — correct — and then
retrieves metadata that may itself contain adversarial text. A malicious column
description reaching model context is flagged in four separate documents (ADR-0013,
threat model T7, AI-safety AS-1, agent-runtime AG-1) and is not addressed anywhere.

The new capabilities make this worse, not better: uploaded documents and compiled wiki
prose are much richer injection surfaces than column names.

**Three controls, in order of value:**

1. **Screen at ingestion, not at retrieval.** Any text entering the system that can
   later reach model context — column comments, document sections, wiki blocks,
   glossary definitions — is screened once, at write time, by the same deterministic
   classifier. Text that fails is quarantined and flagged for review. Screening once
   at write is cheaper and more complete than screening on every read.
2. **Structural separation in the prompt.** Retrieved content is delivered as data in
   a typed structure, never concatenated into instruction position, and the model
   contract states that retrieved content is untrusted.
3. **The real backstop is INV-3.** Even a fully successful injection produces a
   structured proposal that cannot execute, cannot publish, and cannot bind a tool.
   This is why the architectural claim is stronger than any classifier — but it is
   only true while INV-2 is mechanically enforced, which returns to the import-linter
   contract that does not yet exist.

---

## 6. What "production-grade" requires, concretely

Architecture is necessary and not sufficient. None of the following is a design
question; all of it is currently absent.

| Item | Why it blocks production |
|---|---|
| **CI pipeline** | Nothing enforces anything today. First item, before any new feature |
| **Import-linter: module boundaries + gateway exclusivity** | The invariants are conventions until this exists |
| **Tier-0 invariant test suite, all 10** | 4 of 9 are formalised; the rest need harnesses that do not exist |
| **Projection rebuild drill** | Never run. INV-1 is untested |
| **PITR restore drill** | Never run |
| **Temporal failover drill** | Never run |
| **Credential rotation drill** | Never run |
| **Kill-switch drill** | Never run — and the AI-safety argument leans on it |
| **Load/soak at 1M objects** | p95 targets are published and unmeasured |
| **Penetration test** | Not run |
| **Agent evaluation benchmark suite** | Without it, "the agent works" is an opinion |
| **Connector certification corpus, incl. lineage parsers** | INV-9 is unverifiable without it |

---

## 7. Deliberate non-goals, reaffirmed

Carried forward from the current design and worth restating because the new
capabilities create pressure on each:

- **No write-back.** Read-only stays read-only. Procedure-derived tools are eligible
  only when proven read-only by parse.
- **No BI/dashboard builder.** The wiki is documentation, not a dashboard canvas.
- **No ETL execution.** Transformation analysis *reads* transformation logic; it never
  runs it.
- **No fully autonomous agents.** Every capability an agent has was approved by a
  human.
- **No copying source business data.** Federated intermediates are ephemeral;
  embeddings cover metadata and customer documentation only.
- **No multi-tenant shared metadata plane.** Self-hosted remains the deployment target.
