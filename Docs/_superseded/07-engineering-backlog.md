# 07 — Engineering Backlog and Acceptance Criteria

## Epic 1 — Project and Data Source Management

### Capabilities

- Create project.
- Register source.
- Store credential references securely.
- Test source connection.
- Discover connector capabilities.
- Configure scan policies.

### Acceptance

- User can connect at least two SQL dialects.
- Credentials are never returned through API.
- Source can be disabled without deleting metadata.
- Source-level concurrency is configurable.

## Epic 2 — Metadata Discovery

### Capabilities

- catalogs
- schemas
- tables
- views
- columns
- constraints
- indexes
- partitions

### Acceptance

- Initial scan persists normalized metadata.
- Re-scan detects changes.
- Deleted source objects are soft-deprecated.
- Every object has a stable internal ID.

## Epic 3 — Profiling Engine

### Capabilities

- adaptive sampling
- null/distinct statistics
- range statistics
- top values
- patterns
- date ranges
- PII candidates
- table row/freshness analysis

### Acceptance

- Large tables are not full-scanned without explicit policy.
- Profiling tasks are retryable and idempotent.
- Profile artifacts can be offloaded to object storage.
- Worker concurrency can be changed per source.

## Epic 4 — Analysis Scheduler

### Capabilities

- AnalysisRun
- AnalysisTask
- dependency DAG
- queueing
- priority
- retry
- pause/cancel/resume
- incremental scans

### Acceptance

- A 1,000-table scan can be represented as tasks without creating autonomous agents.
- Failed tasks do not corrupt the whole run.
- Scheduler waits for table profiling before cross-table relationship validation.
- Unchanged tables can be skipped.

## Epic 5 — Relationship Engine

### Capabilities

- PK detection
- composite key detection
- FK candidate generation
- candidate pruning
- evidence scoring
- human approval

### Acceptance

- No brute-force N×N full-value comparison.
- Relationship evidence is inspectable.
- Rejected candidates are remembered.
- Verified relationships are persisted into Neo4j.

## Epic 6 — Temporal and Table-Family Intelligence

### Capabilities

- history detection
- snapshots
- delta/CDC
- SCD detection
- table families
- canonical table resolution

### Acceptance

- Agent can distinguish "current customer" from "customer as of date."
- Table-family relationships are graph-visible.
- Canonical-table decisions include evidence and override support.

## Epic 7 — Lineage

### Capabilities

- SQL parsing
- view lineage
- ETL lineage
- query-history lineage
- OpenLineage event ingest
- column lineage
- impact analysis

### Acceptance

- User can navigate upstream/downstream.
- Metric and tool dependencies appear in impact analysis.
- Lineage includes source of evidence.

## Epic 8 — Semantic Layer

### Capabilities

- domains
- business entities
- glossary
- measures
- dimensions
- metrics
- synonyms
- semantic versions

### Acceptance

- Metric has explicit grain and time semantic.
- Semantic objects map to physical tables/columns.
- Semantic version can be rolled back.
- LLM-generated descriptions are marked as inferred until approved or auto-approved.

## Epic 9 — Hybrid Retrieval

### Capabilities

- lexical search
- vector search
- graph expansion
- security filtering
- confidence ranking

### Acceptance

- Agent does not receive metadata it cannot access.
- Query context can be limited to the highest-relevance objects.
- Retrieval response includes why objects were selected.

## Epic 10 — Agent Orchestrator

### Capabilities

- intent
- plan
- tool selection
- context building
- query generation
- result analysis
- error handling

### Acceptance

- Multi-step questions generate inspectable logical plans.
- Agent uses approved tools before generating equivalent SQL where applicable.
- Model loops have strict step limits.
- Every model call is traceable.

## Epic 11 — SQL Guard and Execution Gateway

### Capabilities

- AST parse
- dialect validation
- policy validation
- cost validation
- EXPLAIN
- execution
- timeout/cancel

### Acceptance

- Generated SQL cannot directly bypass the guard.
- Write operations are denied by default.
- Queries exceeding configured cost can be blocked.
- Warehouse query IDs are stored for audit.

## Epic 12 — Query Memory

### Capabilities

- successful query storage
- semantic similarity
- adaptation
- feedback
- usage scoring

### Acceptance

- Similar questions can reuse previously successful patterns.
- Memory entries are semantic-version-aware.
- Bad/user-rejected queries are down-ranked.

## Epic 13 — Tool Registry

### Capabilities

- save analysis as tool
- parameter extraction
- testing
- approval
- versioning
- RBAC
- agent bindings

### Acceptance

- Tool can be called deterministically by an agent.
- Tool lineage identifies all referenced tables/metrics.
- A tool version can be deprecated without deleting history.
- Tool execution is auditable.

## Epic 14 — Security and Governance

### Capabilities

- SSO
- RBAC
- ABAC
- agent identity
- tool identity
- policy engine
- sensitive metadata filtering

### Acceptance

- Effective permission is intersection-based.
- Source permissions are not bypassed.
- Unauthorized columns are not sent to LLM context.
- Policy decisions are auditable.

## Epic 15 — Review Workbench

### Capabilities

- relationship review
- semantic review
- canonical-table review
- metric review
- tool approval
- evidence visualization

### Acceptance

- Reviewer can approve/reject/edit.
- Decisions become durable evidence.
- Rejected candidates do not reappear unless new evidence materially changes confidence.

## Epic 16 — UI

Primary navigation:

```text
Projects
Data Sources
Data Explorer
Semantic Model
Knowledge Graph
Business Glossary
Metrics
Relationships
Lineage
Analyst
Agents
Models
Tools
Policies
Review Queue
Query History
Observability
```

## Epic 17 — Evaluation Framework

Create a benchmark set containing:

- simple filtering
- joins
- aggregation
- time-series
- current/history
- multi-step analytics
- ambiguous business terminology
- cross-schema joins
- sensitive-data restrictions

Metrics:

```text
semantic_resolution_accuracy
join_path_accuracy
SQL syntax validity
SQL execution accuracy
result correctness
policy compliance
latency
tokens
warehouse cost
tool reuse rate
```

## Epic 18 — Production Readiness

Acceptance:

- backup/restore tested
- audit retention configured
- secrets externalized
- load tests passed
- queue recovery tested
- idempotent jobs verified
- SLO dashboards available
- disaster recovery documented
- semantic rollback tested
- source throttling tested

## Suggested MVP Definition

A useful MVP is not the full platform.

It should demonstrate:

1. Connect a warehouse.
2. Discover 100–1,000 tables.
3. Profile in parallel.
4. Detect PK/FK candidates.
5. Detect history/delta/snapshot patterns.
6. Build Neo4j relationships.
7. Generate semantic descriptions for prioritized tables.
8. Review ambiguous relationships.
9. Ask an NLP question.
10. Retrieve semantic context.
11. Generate and guard SQL.
12. Execute against source.
13. Show lineage.
14. Save a successful analysis as a reusable tool.

That establishes the core product thesis before adding broad connector coverage and advanced governance.
