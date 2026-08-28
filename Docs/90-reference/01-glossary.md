# Glossary

> Status: Authoritative. Owner: Architecture.
> Terms that mean something specific in Atlas. Where a term is used loosely in the industry, this document states which meaning Atlas uses.

## Platform concepts

**Analysis run** — A scoped unit of metadata work that expands into a DAG of deterministic tasks. Not an agent, and not one-per-table.

**Approved tool** — A published, versioned, parameterized governed capability an agent may invoke instead of generating SQL. See *governed tool*.

**Atlas** — The product. Internally also the name of the user-facing portal.

**Bounded** — Having an explicit configured limit and returning an explicit truncation reason when the limit is reached. Unbounded is a defect, not a performance characteristic.

**Capability negotiation** — Determining what a connector can genuinely do, derived from its certification result rather than declared by hand.

**Context product** — A curated, versioned, owned, approved package of governed context (semantics, glossary, lineage, quality, policy, eligible tools) for consumption by an external AI client. Distinct from a metadata API: policy is evaluated at every read, and consumption is recorded as lineage.

**Control plane** — Metadata, semantics, policy, tools, models, lineage, audit, orchestration. Contrast *data plane*.

**Data plane** — Source queries, result sets, temporary analytical data. Executes in the source's compute.

**Deployment unit** — One of `atlas-api`, `atlas-worker`, `atlas-projector`, `atlas-scheduler` — the same image with different entrypoints.

**Envelope** — The canonical versioned metadata payload every ingestion transport converges on (ADR-0012).

**Evidence** — Value-free, replayable records of how a decision was reached, including what was refused. Distinct from a *log*, which records what happened without the guarantee of atomicity or value-freedom.

**Fail closed** — Denying rather than degrading when identity, secrets, policy, or model-route posture is incomplete.

**Governed tool** — A versioned capability with deterministic parameterized SQL, a typed parameter schema, RBAC bindings, and a maker-checker lifecycle. Executes through the query gateway like everything else.

**Maker-checker** — Platform-enforced separation between the identity that proposes a governed change and the identity that approves it. Never per-feature.

**Module** — A bounded context with a published interface, its own PostgreSQL schema, and its own tests. One of 21.

**Negative knowledge** — Retained record of inferences that were rejected, so the system does not re-propose them.

**Projection** — A rebuildable derived store (Neo4j, vector, search, cache). Never authoritative, never read for an authorization, approval, or correctness decision.

**Proposal** — Model output after schema validation. Structurally inert: it cannot be coerced into an executable command.

**Purpose** — A declared reason for accessing data, used in purpose-bound authorization.

**Screened** — The explicit runtime state at which prompt-risk classification runs, **before** retrieval, model context construction, or tool selection.

**Tenancy hierarchy** — organization → legal entity → line of business → data domain → project → datasource.

**Tool-first execution** — Preferring an approved governed tool over generating SQL. The mechanism by which cost and risk fall as usage rises.

**Trust signal** — The composite quality, freshness, and certification state of an asset, consumed by retrieval ranking, answer warnings, and tool gating.

**Value-free** — Containing no source business values. Applies to platform state, logs, traces, events, evidence, and model context.

**Workload identity** — A machine principal (connector agent, MCP consumer, batch producer) rather than a human user.

## Data concepts

**Canonical table** — The member of a table family an agent should use by default (for example, "current customer" rather than a history table).

**Drift** — Detected change between scans: created, changed, or deprecated objects.

**Fingerprint** — A content hash used for change detection and idempotent reapplication.

**Grain** — The level of detail one row represents. A metric whose grain does not match its physical mapping is wrong even when its SQL runs.

**Table family** — A group of related tables representing the same entity at different temporal semantics: history, snapshot, delta/CDC, SCD, append-only, reference.

**Tombstone** — A soft-deletion record. Reactivation restores the same stable object ID.

**Watermark** — An approved source column proving when business data last changed. Required before freshness is reported at all.

## Lineage concepts

**AI decision lineage** — Edges recording why the agent chose a path: retrieval selections and rejections with reasons, tool selection, pinned versions, and refusals with the control that fired.

**Edge kind** — `QUERY`, `VIEW`, `PROCEDURE`, `ETL`, `DBT`, `BI`, `AI_DECISION`.

**Impact analysis** — Downstream blast radius across metrics, tools, annotations, dbt models, reports, and quality policies.

**OpenLineage** — An open standard for lineage events; Atlas ingests it.

## Governance concepts

**ABAC** — Attribute-based access control, evaluating classification, purpose, residency, identity attributes, and agent-vs-human request context.

**Certification** — (a) Connector certification: deterministic conformance evidence for a source adapter. (b) Asset certification: a steward vouching for an asset until an expiry date. Context distinguishes them.

**Compliance pack** — A reproducible evidence bundle generated from runtime evidence for a named period, WORM-archived on generation.

**Entitlement** — Edition or licence gating, evaluated alongside permissions.

**Review queue** — The single, cross-object-type queue through which every governed change passes.

**WORM** — Write once, read many. Immutable archival storage for audit evidence.

## Terms Atlas uses differently from common industry usage

| Term | Common usage | Atlas usage |
|---|---|---|
| **Agent** | An autonomous LLM loop that acts | Any principal invoking approved tools; it proposes, it never authorizes |
| **Data contract** | A registered document | A named bundle of enforceable clauses over catalog, quality, semantics, and governance |
| **Freshness** | Often conflated with scan time | Only reported when an approved watermark exists; otherwise `NOT_CONFIGURED` |
| **Governance** | Documentation and workflow | Enforcement in the execution path |
| **Lineage** | Data provenance | Data provenance **plus** AI decision provenance |
| **Semantic layer** | Metric definitions | Metric definitions that carry policy and compile into executable governed tools |
| **Tool** | A function an LLM may call | A versioned, approved, parameter-typed capability that executes through the query gateway |
